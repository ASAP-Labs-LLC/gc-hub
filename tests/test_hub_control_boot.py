"""hub_control with the real app booted (tests/bootapp.py), as the tray uses it.

* Pause processing survives a restart (settings_kv), Resume undoes it.
* Stop writes the updater's ``paused`` marker and the process exits for good:
  nothing self-respawns (a paused updater normally means "respawn
  yourself"), so nothing listens on the port afterwards.
* ``/healthz`` keeps the updater's contract and carries the same numbers
  as ``/api/hub/status`` under ``hub``; polling the status is not activity.
"""
from __future__ import annotations

import socket
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

from bootapp import booted, get, post, setup_admin, wait_for  # noqa: E402


def _status(port):
    code, body = get(port, "/api/hub/status")
    assert code == 200
    return body


def _listening(port) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def test_pause_survives_a_restart_and_resume_undoes_it(tmp_path):
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        assert wait_for(lambda: _status(port)["state"] == "running", timeout=30)
        code, body = post(port, "/api/admin/hub/pause-processing", {"password": pw})
        assert code == 202 and body["processing_paused"] is True
        assert wait_for(lambda: _status(port)["worker_alive"] is False, timeout=30)
        s = _status(port)
        assert s["state"] == "processing-paused" and s["processing_paused"] is True
        code, notes = get(port, "/api/notifications")
        assert code == 200
        assert any("processing is paused" in n["message"].lower() for n in notes)
    with booted(tmp_path) as (port, _proc, data, _home):
        assert wait_for(lambda: _status(port)["state"] != "starting", timeout=30)
        s = _status(port)
        assert s["processing_paused"] is True
        assert s["state"] == "processing-paused" and s["worker_alive"] is False
        code, body = post(port, "/api/admin/hub/resume-processing", {"password": pw})
        assert code == 202 and body["processing_paused"] is False
        assert wait_for(lambda: _status(port)["worker_alive"], timeout=15)
        assert _status(port)["state"] == "running"


def test_stop_writes_the_marker_and_exits_without_respawn(tmp_path):
    with booted(tmp_path) as (port, proc, data, _home):
        pw = setup_admin(port, data)
        code, body = post(port, "/api/admin/hub/stop", {"password": pw})
        assert code == 202, body
        assert (data / "paused").is_file()
        assert proc.wait(15) == 0
        # the updater is paused, which normally makes the app respawn itself:
        # a stop must not. Nothing may come up on the port.
        deadline = time.time() + 15
        while time.time() < deadline:
            assert not _listening(port), "something respawned on the port"
            time.sleep(0.5)
        log = (data / "app.log").read_text(encoding="utf-8", errors="replace")
        assert "STOP requested" in log
        assert "New process spawned" not in log


def test_healthz_keeps_the_contract_and_carries_the_hub_numbers(tmp_path):
    with booted(tmp_path) as (port, _proc, _data, _home):
        assert wait_for(lambda: _status(port)["state"] == "running", timeout=30)
        time.sleep(1.2)
        for _ in range(3):
            _status(port)                       # the tray's poll is not activity
        code, body = get(port, "/healthz")
    assert code == 200 and body["status"] == "ok"
    for k in ("version", "pid", "active_sessions", "idle_seconds"):
        assert k in body
    assert body["active_sessions"] == 0 and body["idle_seconds"] >= 1
    h = body["hub"]
    for k in ("state", "processing_paused", "updater_paused", "uptime_seconds",
              "cpu_percent", "rss_bytes", "queue", "exporter"):
        assert k in h, k
    assert h["processing_paused"] is False
    assert h["queue"]["jobs_queued"] == 0


def test_healthz_answers_quickly_while_the_store_is_exclusively_locked(tmp_path):
    # The updater gives /healthz 3 s (and may roll back on a slow one): the
    # `hub` numbers come from a cache refreshed off the request path.
    with booted(tmp_path) as (port, _proc, data, _home):
        assert wait_for(lambda: _status(port)["state"] == "running", timeout=30)
        lock = sqlite3.connect(str(data / "gc.db"), isolation_level=None, timeout=30)
        try:
            lock.execute("PRAGMA locking_mode=EXCLUSIVE")
            lock.execute("BEGIN EXCLUSIVE")
            lock.execute("CREATE TABLE IF NOT EXISTS _lock_probe(a)")
            for _ in range(3):
                t0 = time.time()
                code, body = get(port, "/healthz", timeout=5)
                assert code == 200 and body["status"] == "ok"
                assert time.time() - t0 < 1.0, time.time() - t0
                t0 = time.time()
                s = _status(port)
                assert time.time() - t0 < 1.0 and "stale" in s
                time.sleep(0.5)
        finally:
            lock.execute("ROLLBACK")
            lock.close()
