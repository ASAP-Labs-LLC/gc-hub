"""live.py (v3.1 live updates): the in-memory event ring, its cursor and
``poll`` (what ``GET /api/live`` answers). Stdlib only, no app import."""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import live  # noqa: E402


def _bus(size=1000):
    return live.Bus(size=size, boot_id="b00t")


def test_cursor_is_boot_id_and_seq():
    b = _bus()
    assert b.cursor() == "b00t:0"
    b.publish("sample", {"sample_id": 7})
    assert b.cursor() == "b00t:1"


def test_boot_id_is_random_per_bus():
    assert live.Bus().boot_id != live.Bus().boot_id


def test_since_collects_changed_ids_deduped_and_sorted():
    b = _bus()
    start = b.cursor()
    b.publish("sample", {"sample_id": 9})
    b.publish("sample", {"sample_id": 3})
    b.publish("sample", {"sample_id": 9})
    b.publish("instrument", {"instrument_id": "gc2"})
    b.publish("agent", {"instrument_id": "gc1"})
    b.publish("notification", {})
    b.publish("hub", {})
    out = b.since(start)
    assert out["reset"] is False
    assert out["samples"] == [3, 9]
    assert out["instruments"] == ["gc2"]
    assert out["cursor"] == "b00t:7"
    assert set(out["kinds"]) == {"sample", "instrument", "agent", "notification", "hub"}


def test_unchanged_cursor_is_empty_and_not_reset():
    b = _bus()
    b.publish("sample", {"sample_id": 1})
    out = b.since(b.cursor())
    assert _poll(b, "b00t:0")["kinds"] == ["sample"]
    assert out == {"cursor": "b00t:1", "reset": False, "samples": [], "instruments": [],
                   "kinds": []}


def test_missing_or_garbled_cursor_resets():
    b = _bus()
    for c in (None, "", "nonsense", "b00t:", "b00t:x", "b00t:-1", ":5", "a:b:c", "x" * 500):
        out = b.since(c)
        assert out["reset"] is True, c
        assert out["cursor"] == b.cursor()
        assert out["samples"] == [] and out["instruments"] == []


def test_boot_id_mismatch_resets():
    b = _bus()
    b.publish("sample", {"sample_id": 1})
    assert b.since("other:0")["reset"] is True
    assert b.since("other:1")["reset"] is True


def test_cursor_from_the_future_resets():
    b = _bus()
    assert b.since("b00t:5")["reset"] is True


def test_cursor_older_than_the_ring_resets():
    b = _bus(size=5)
    for i in range(5):
        b.publish("sample", {"sample_id": i})
    # every event after seq 0 is still in the ring: no reset
    assert b.since("b00t:0")["reset"] is False
    b.publish("sample", {"sample_id": 99})    # seq 1 falls out of the ring
    out = b.since("b00t:0")
    assert out["reset"] is True and out["samples"] == []
    ok = b.since("b00t:1")                    # seq 2..6 (samples 1..4, 99) are all still there
    assert ok["reset"] is False and ok["samples"] == [1, 2, 3, 4, 99]


def test_publish_never_raises_and_ignores_bad_input():
    b = _bus()
    b.publish("sample", None)
    b.publish("sample", {"sample_id": "12"})           # coerced
    b.publish("sample", {"sample_id": "not-a-number"})  # dropped from ids
    b.publish("sample", {"sample_ids": [4, 5, "x"]})
    b.publish(object(), object())                       # nonsense: swallowed
    out = b.since("b00t:0")
    assert out["samples"] == [4, 5, 12]


def test_publish_swallows_internal_errors():
    b = _bus()
    with mock.patch.object(b, "_ring", new=None):       # would raise inside
        b.publish("sample", {"sample_id": 1})           # must not raise


def test_publish_is_thread_safe():
    b = _bus(size=100000)

    def burst(base):
        for i in range(1000):
            b.publish("sample", {"sample_id": base + i})

    threads = [threading.Thread(target=burst, args=(k * 1000,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.cursor() == "b00t:8000"
    assert len(b.since("b00t:0")["samples"]) == 8000


def test_module_level_publish_uses_the_process_bus():
    before = live.BUS.cursor()
    live.publish("sample", {"sample_id": 424242})
    out = live.BUS.since(before)
    assert 424242 in out["samples"]


# ── poll: the /api/live answer ──────────────────────────────────────────────

AGENTS = [{"instrument_id": "gc1", "last_seen": "2026-09-30T10:00:00+00:00",
           "version": "1.2.0", "host": "GC1-PC", "status": "idle"}]


def _poll(b, cursor, **kw):
    kw.setdefault("agents", lambda: list(AGENTS))
    kw.setdefault("unread", lambda: 3)
    kw.setdefault("hub", lambda: {"state": "running", "staged_update": None})
    return live.poll(cursor, bus=b, **kw)


def test_poll_shape():
    b = _bus()
    out = _poll(b, None)
    assert set(out) == {"cursor", "reset", "samples", "instruments", "kinds", "agents",
                        "notifications_unread", "hub", "version", "tasks",
                        "server_now", "server_today"}
    import version
    assert out["version"] == version.APP_VERSION
    assert out["kinds"] == []
    assert out["reset"] is True
    assert out["agents"] == AGENTS
    assert out["notifications_unread"] == 3
    assert out["hub"] == {"state": "running", "staged_update": None}


def test_poll_survives_broken_snapshots():
    b = _bus()

    def boom():
        raise RuntimeError("x")

    out = _poll(b, b.cursor(), agents=boom, unread=boom, hub=boom)
    assert out["agents"] == [] and out["notifications_unread"] == 0
    assert out["hub"] == {"state": None, "staged_update": None}
    assert out["reset"] is False


def test_unchanged_cursor_poll_is_fast_and_never_opens_sqlite():
    """The request path answers from memory: the real snapshot sources
    (hub_control's cache, the notification store) never touch SQLite."""
    import hub_control
    import notifications

    hub_control.reset()
    b = _bus()
    b.publish("sample", {"sample_id": 1})
    cur = b.cursor()

    def no_sqlite(*a, **k):
        raise AssertionError("SQLite opened on the /api/live path")

    class Store:
        def count(self):
            return 2

    with mock.patch.object(sqlite3, "connect", side_effect=no_sqlite), \
            mock.patch.object(notifications, "get_store", return_value=Store()):
        t0 = time.perf_counter()
        for _ in range(200):
            out = live.poll(cur, bus=b, agents=hub_control.live_agents,
                            hub=hub_control.live_hub, unread=live.notifications_unread)
        elapsed = time.perf_counter() - t0
    assert out["reset"] is False and out["samples"] == []
    assert out["notifications_unread"] == 2
    assert elapsed < 0.5, elapsed          # 200 polls: well under 2.5 ms each
