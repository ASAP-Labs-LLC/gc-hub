"""v4.0 lane E on the server: one agent-liveness rule (``live.agent_liveness``:
live = ``last_seen`` at most 90 s old on the hub's clock), the agent snapshot's
``name``/``enabled``/``live``/``last_seen_age_s``, the hub's queue counts and
processing pause in ``/api/live``, and ``tasks`` in ``live.poll``. In process
(never ``import app``)."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import live  # noqa: E402

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def _ago(seconds: float) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat(timespec="microseconds")


# ── the rule ────────────────────────────────────────────────────────────────

def test_live_means_seen_within_90_seconds_on_the_hub_clock():
    now = NOW.timestamp()
    assert live.LIVE_SECONDS == 90
    assert live.agent_liveness(_ago(0), now) == (True, 0)
    assert live.agent_liveness(_ago(89.6), now) == (True, 90)
    assert live.agent_liveness(_ago(90), now) == (True, 90)
    assert live.agent_liveness(_ago(91), now) == (False, 91)
    assert live.agent_liveness(_ago(3600), now) == (False, 3600)
    # a last_seen "from the future" (the clock stepped back) is live, age 0
    assert live.agent_liveness(_ago(-30), now) == (True, 0)


def test_never_seen_or_unreadable_is_not_live():
    now = NOW.timestamp()
    for bad in (None, "", "garbage", 12, "2026-09-29T12:00:00"):   # naive: no offset
        assert live.agent_liveness(bad, now) == (False, None), bad


# ── the agent snapshot ──────────────────────────────────────────────────────

pytest.importorskip("flask")

import admin_auth  # noqa: E402
import hub_control  # noqa: E402
import store  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("GC_DATA_DIR", str(data))
    db = admin_auth.hub_db()
    store.instruments.upsert({"id": "gc1", "name": "GC-1"}, db=db)
    store.instruments.upsert({"id": "gc2", "name": "GC-2", "enabled": 0}, db=db)
    with store.connection(db) as conn:
        with store.write_txn(conn):
            conn.execute("INSERT INTO agents(instrument_id, version, host, state, last_seen) "
                         "VALUES ('gc1', 'v1.2.0', 'GC1-PC', 'idle', ?)", (_ago(30),))
    hub_control.reset()
    try:
        yield db
    finally:
        hub_control.reset()


def test_live_agents_carry_name_enabled_live_and_age(db):
    assert hub_control.refresh_cache()
    agents = {a["instrument_id"]: a for a in hub_control.live_agents(now=NOW.timestamp())}
    gc1, gc2 = agents["gc1"], agents["gc2"]
    assert gc1["name"] == "GC-1" and gc1["enabled"] is True
    assert gc1["live"] is True and gc1["last_seen_age_s"] == 30
    assert gc1["version"] == "v1.2.0" and gc1["host"] == "GC1-PC" and gc1["status"] == "idle"
    assert gc2["name"] == "GC-2" and gc2["enabled"] is False
    assert gc2["live"] is False and gc2["last_seen_age_s"] is None
    # 2 minutes later on the hub's clock, gc1 is no longer live
    later = {a["instrument_id"]: a for a in
             hub_control.live_agents(now=NOW.timestamp() + 120)}
    assert later["gc1"]["live"] is False and later["gc1"]["last_seen_age_s"] == 150


def test_a_heartbeat_keeps_the_name_and_is_live_at_once(db):
    import ingest_api
    assert hub_control.refresh_cache()
    ingest_api.record_heartbeat("gc2", {"version": "v2", "host": "GC2-PC", "state": "idle"},
                                db=db)
    a = [x for x in hub_control.live_agents() if x["instrument_id"] == "gc2"][0]
    assert a["name"] == "GC-2" and a["live"] is True and a["last_seen_age_s"] <= 2
    assert a["version"] == "v2"


def test_live_agents_never_open_sqlite(db):
    assert hub_control.refresh_cache()

    def boom(*a, **k):
        raise AssertionError("SQLite on the /api/live path")

    with mock.patch.object(sqlite3, "connect", side_effect=boom):
        assert len(hub_control.live_agents()) == 2
        hub_control.live_hub()


def test_the_classic_instruments_api_uses_the_same_rule(db):
    import ingest_api
    rows = {r["instrument_id"]: r for r in ingest_api.agents_status(db=db, now=NOW.timestamp())}
    assert rows["gc1"]["live"] is True and rows["gc1"]["last_seen_age_s"] == 30
    assert rows["gc2"]["live"] is False and rows["gc2"]["last_seen_age_s"] is None


# ── the hub's state ─────────────────────────────────────────────────────────

def test_live_hub_carries_queue_counts_and_the_pause(db):
    with store.connection(db) as conn:
        with store.write_txn(conn):
            for state in ("queued", "queued", "running", "done"):
                conn.execute("INSERT INTO jobs(kind, state, attempts, created_at) "
                             "VALUES ('process', ?, 0, ?)", (state, _ago(5)))
    assert hub_control.refresh_cache()
    h = hub_control.live_hub()
    assert h["processing_paused"] is False
    assert h["queue"] == {"waiting": 2, "running": 1}
    assert h["exports_pending"] == 0
    assert h["paused_by"] is None and h["paused_since"] is None
    import hub
    hub.set_processing_paused(db, True, by="Ryan C (10.0.0.5)")
    assert hub_control.refresh_cache()
    h = hub_control.live_hub()
    assert h["processing_paused"] is True
    assert h["paused_by"] == "Ryan C"               # never the address
    assert h["paused_since"]
    # the tray without a user name records only an address: no "who" then
    hub.set_processing_paused(db, True, by="127.0.0.1")
    assert hub_control.refresh_cache()
    assert hub_control.live_hub()["paused_by"] is None
    hub.set_processing_paused(db, True, by="::1")
    assert hub_control.refresh_cache()
    assert hub_control.live_hub()["paused_by"] is None


# ── live.poll ───────────────────────────────────────────────────────────────

def test_poll_carries_tasks_and_the_full_hub_state():
    b = live.Bus(boot_id="b00t")
    hub = {"state": "processing-paused", "staged_update": None, "processing_paused": True,
           "queue": {"waiting": 3, "running": 1}, "exports_pending": 2,
           "paused_by": "Ryan C", "paused_since": "2026-09-29T12:00:00+00:00", "junk": "x"}
    tasks = [{"id": "reprocess:1", "state": "running"}]
    out = live.poll(None, bus=b, agents=lambda: [], unread=lambda: 0, hub=lambda: hub,
                    tasks=lambda: tasks)
    assert out["tasks"] == tasks
    assert out["hub"] == {k: v for k, v in hub.items() if k != "junk"}


def test_poll_without_tasks_or_with_a_broken_feed():
    b = live.Bus(boot_id="b00t")

    def boom():
        raise RuntimeError("x")

    assert live.poll(None, bus=b)["tasks"] == []
    assert live.poll(None, bus=b, tasks=boom)["tasks"] == []
