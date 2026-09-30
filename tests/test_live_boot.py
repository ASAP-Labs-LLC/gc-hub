"""``GET /api/live`` in the real app (booted in a subprocess; never
``import app``): session-gated, cursor/reset semantics, an ingested CDF and a
heartbeat show up on the next poll, and polling it is never activity."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS, TESTS / "ingest", TESTS / "pipeline"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

from bootapp import booted, get, post, wait_for  # noqa: E402
from ingest_helpers import heartbeat, ingest, prepared  # noqa: E402


def _live(port, cursor=None):
    path = "/api/live" + (f"?since={cursor}" if cursor is not None else "")
    code, body = get(port, path)
    assert code == 200, body
    return body


def test_live_end_to_end(tmp_path):
    hub = prepared(tmp_path)
    sample = hub.cdf(name="40304").read_bytes()
    with booted(tmp_path) as (port, _proc, _data, _home):
        # session-gated
        code, body = get(port, "/api/live", auth=False)
        assert code == 401 and body.get("login_required")

        first = _live(port)
        assert first["reset"] is True
        assert set(first) == {"cursor", "reset", "samples", "instruments", "kinds", "agents",
                              "notifications_unread", "hub", "version"}
        boot, _, seq = first["cursor"].partition(":")
        assert boot and seq.isdigit()
        assert set(first["hub"]) == {"state", "staged_update"}
        assert isinstance(first["notifications_unread"], int)

        # the refresher's agent snapshot: every instrument, none heard from yet
        assert wait_for(lambda: {a["instrument_id"] for a in _live(port)["agents"]}
                        >= {"gc1", "gc2", "gc3"}, timeout=15)

        # an unchanged cursor: nothing, no reset, fast
        cur = _live(port)["cursor"]
        t0 = time.time()
        for _ in range(10):
            same = _live(port, cur)
            if same["cursor"] != cur:        # a boot-time event landed: follow it
                cur = same["cursor"]
                continue
            assert same["reset"] is False and same["samples"] == [] and same["instruments"] == []
        assert time.time() - t0 < 3.0

        # another boot's cursor, or one from the future: reset
        assert _live(port, "0123456789abcdef:0")["reset"] is True
        assert _live(port, f"{boot}:999999999")["reset"] is True
        assert _live(port, "garbage")["reset"] is True

        # an agent sends a CDF: the next poll names the new sample
        code, res = ingest(port, hub.tokens["gc1"], sample)
        assert code == 201, res
        sid = res["sample_id"]
        seen = _live(port, cur)
        assert sid in seen["samples"] and seen["reset"] is False
        # the client fetches just that row; the Worker then moves it on
        code, files = get(port, f"/api/files?ids={sid}")
        assert code == 200 and [s["sample_id"] for s in files["samples"]] == [sid]
        assert wait_for(lambda: get(port, f"/api/files?ids={sid}")[1]["samples"][0]["status"]
                        != "received", timeout=30)
        assert sid in _live(port, cur)["samples"]

        # a heartbeat is in the very next poll's agent snapshot
        code, _ = heartbeat(port, hub.tokens["gc1"], version="v7.7.7", host="GC1-PC",
                            state="sending")
        assert code == 200
        agents = {a["instrument_id"]: a for a in _live(port)["agents"]}
        a = agents["gc1"]
        assert a["version"] == "v7.7.7" and a["host"] == "GC1-PC" and a["status"] == "sending"
        assert a["last_seen"]


BG = {"X-GC-Background": "1"}
FOLLOW_UPS = ("/api/files?ids=1,2", "/api/instruments/gc1", "/api/table", "/api/notifications",
              "/api/files?limit=5000&q=4")


def _last_seen(data):
    import sqlite3
    con = sqlite3.connect(str(data / "gc.db"))
    try:
        return con.execute("SELECT last_seen FROM web_sessions").fetchall()
    finally:
        con.close()


def test_live_follow_up_fetches_with_the_background_header_are_not_activity(tmp_path):
    with booted(tmp_path) as (port, _proc, data, _home):
        before = _last_seen(data)
        time.sleep(1.2)
        cur = _live(port)["cursor"]
        _live(port, cur)
        for path in FOLLOW_UPS:
            code, _ = get(port, path, headers=BG)
            assert code == 200, path
        _, body = get(port, "/healthz")
        assert body["idle_seconds"] >= 1.1 and body["active_sessions"] == 0
        time.sleep(0.5)
        for path in FOLLOW_UPS:
            get(port, path, headers=BG)
        _, again = get(port, "/healthz")
        assert again["idle_seconds"] > body["idle_seconds"] and again["active_sessions"] == 0
        assert _last_seen(data) == before


def test_the_same_get_without_the_header_is_activity(tmp_path):
    with booted(tmp_path) as (port, _proc, _data, _home):
        time.sleep(1.2)
        code, _ = get(port, "/api/files?ids=1,2")
        assert code == 200
        _, body = get(port, "/healthz")
        assert body["idle_seconds"] < 1 and body["active_sessions"] == 1


def test_a_post_with_the_background_header_is_still_activity(tmp_path):
    with booted(tmp_path) as (port, _proc, _data, _home):
        time.sleep(1.2)
        post(port, "/api/reprocess/preview", {"query": "", "instrument": "gc1"}, headers=BG)
        _, body = get(port, "/healthz")
        assert body["idle_seconds"] < 1 and body["active_sessions"] == 1


def test_polling_live_is_not_activity(tmp_path):
    with booted(tmp_path) as (port, _proc, _data, _home):
        time.sleep(1.2)
        cur = None
        for _ in range(5):
            cur = _live(port, cur)["cursor"]
        _, body = get(port, "/healthz")
        assert body["idle_seconds"] >= 1.1          # the idle timer kept running
        assert body["active_sessions"] == 0         # the session was never touched
        time.sleep(0.5)
        _live(port, cur)
        _, again = get(port, "/healthz")
        assert again["idle_seconds"] > body["idle_seconds"]
        assert again["active_sessions"] == 0
