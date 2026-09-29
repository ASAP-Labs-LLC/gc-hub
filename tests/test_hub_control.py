"""hub_control: the hub tray's server side, in process.

The Blueprint is mounted on a bare Flask app (never ``import app``) with the
admin password set through ``admin_auth``. ``remote_addr`` is faked with the
test client's ``environ_base`` (the WSGI ``REMOTE_ADDR``), which is exactly
what ``request.remote_addr`` reads, so the loopback-only rule is tested for
real without any switch in production code.

State-changing routes: loopback only (checked first), same-origin, JSON,
64 KiB, admin password. ``GET /api/hub/status`` is open and read-only.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")

from flask import Flask  # noqa: E402

import admin_auth  # noqa: E402
import hub  # noqa: E402
import hub_control  # noqa: E402
import notifications  # noqa: E402
import restart_policy  # noqa: E402
import store  # noqa: E402

PW = "tray-admin-pw"
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
REMOTE = {"REMOTE_ADDR": "10.1.2.3"}
ROUTES = ("/api/admin/hub/pause-processing", "/api/admin/hub/resume-processing",
          "/api/admin/hub/stop")


class FakeWorker:
    def __init__(self, rt):
        self.rt = rt

    def is_alive(self):
        return not self.rt.paused


class FakeRuntime:
    def __init__(self, paused=False):
        self.paused = paused
        self.calls = []
        self.worker = FakeWorker(self)

    def pause(self):
        self.calls.append("pause")
        self.paused = True

    def resume(self):
        self.calls.append("resume")
        self.paused = False

    def exporter_alive(self):
        return not self.paused


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("GC_DATA_DIR", str(data))
    admin_auth.reset_throttle()
    db = admin_auth.hub_db()
    admin_auth.setup(PW, admin_auth.ensure_setup_code(db=db), db=db)
    rt = FakeRuntime()
    shut = threading.Event()
    notices = notifications.NotificationStore(data / "notifications.json")
    hub_control.configure(runtime=lambda: rt, shutdown=shut.set, notices=notices,
                          started_at=time.time() - 42)
    app = Flask("hub_control_test")
    app.register_blueprint(admin_auth.bp)
    app.register_blueprint(hub_control.bp)
    try:
        yield {"client": app.test_client(), "rt": rt, "shut": shut, "data": data, "db": db,
               "notices": notices}
    finally:
        hub_control.reset()
        restart_policy._reset_stop_for_tests()
        admin_auth.reset_throttle()


def _post(c, path, body=None, *, environ=LOCAL, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(body if body is not None else {"password": PW})
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    r = c.post(path, data=data, headers=h, environ_base=environ)
    return r.status_code, r.get_json(silent=True)


# ── loopback only ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ROUTES)
def test_a_non_loopback_client_is_refused_even_with_the_password(env, path):
    code, body = _post(env["client"], path, environ=REMOTE)
    assert code == 403
    assert "localhost" in body["error"] or "loopback" in body["error"]
    assert env["rt"].calls == []
    assert not env["shut"].is_set()
    assert not (env["data"] / "paused").exists()
    assert hub.processing_paused(env["db"]) is None


@pytest.mark.parametrize("addr,ok", [
    ("127.0.0.1", True), ("127.8.9.10", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("10.0.0.5", False), ("192.168.1.20", False), ("::ffff:10.0.0.5", False),
    ("fe80::1", False), ("", False), (None, False), ("localhost", False), ("garbage", False),
])
def test_is_loopback(addr, ok):
    assert hub_control.is_loopback(addr) is ok


@pytest.mark.parametrize("path", ROUTES)
def test_wrong_password_json_and_size_rules(env, path):
    c = env["client"]
    assert _post(c, path, {"password": "wrong-one"})[0] == 403
    code, _ = _post(c, path, headers={"Content-Type": "text/plain"})
    assert code == 415
    code, _ = _post(c, path, raw=json.dumps({"password": PW, "pad": "x" * (70 * 1024)}))
    assert code == 413
    code, _ = _post(c, path, headers={"Origin": "http://evil.example"})
    assert code == 403
    code, _ = _post(c, path, headers={"Sec-Fetch-Site": "cross-site"})
    assert code == 403
    assert env["rt"].calls == [] and not env["shut"].is_set()


# ── pause / resume ────────────────────────────────────────────────────────

def test_pause_then_resume(env):
    c, rt, db, notices = env["client"], env["rt"], env["db"], env["notices"]
    code, body = _post(c, "/api/admin/hub/pause-processing")
    assert code == 200 and body["processing_paused"] is True
    assert rt.calls == ["pause"]
    assert hub.processing_paused(db)["by"] == "127.0.0.1"
    paused_notes = [n for n in notices.list_all() if "paused" in n["message"].lower()]
    assert len(paused_notes) == 1 and paused_notes[0]["level"] == "warning"
    # idempotent: no second notice
    assert _post(c, "/api/admin/hub/pause-processing")[0] == 200
    assert len([n for n in notices.list_all() if "paused" in n["message"].lower()]) == 1

    snap = hub_control.status_snapshot()
    assert snap["processing_paused"] is True and snap["state"] == "processing-paused"

    code, body = _post(c, "/api/admin/hub/resume-processing")
    assert code == 200 and body["processing_paused"] is False
    assert rt.calls[-1] == "resume"
    assert hub.processing_paused(db) is None
    # the paused notice is gone; a resumed one says so
    assert not any(n["id"] == paused_notes[0]["id"] for n in notices.list_all())
    assert any("resumed" in n["message"].lower() for n in notices.list_all())
    assert hub_control.status_snapshot()["state"] == "running"


def test_pause_before_the_hub_has_started_is_remembered(env):
    hub_control.configure(runtime=lambda: None)
    code, body = _post(env["client"], "/api/admin/hub/pause-processing")
    assert code == 200 and body["processing_paused"] is True
    assert hub.processing_paused(env["db"]) is not None
    assert hub_control.status_snapshot()["state"] == "starting"


def test_reconcile_brings_a_runtime_in_line_with_the_flag(env):
    rt = env["rt"]
    hub.set_processing_paused(env["db"], True, by="x")
    hub_control.reconcile(rt)
    assert rt.paused is True
    hub.set_processing_paused(env["db"], False)
    hub_control.reconcile(rt)
    assert rt.paused is False
    assert rt.calls == ["pause", "resume"]


def test_the_notice_is_restored_if_paused_at_start(env):
    hub.set_processing_paused(env["db"], True, by="x")
    hub_control.reconcile(env["rt"])
    assert any("paused" in n["message"].lower() for n in env["notices"].list_all())
    hub_control.reconcile(env["rt"])            # still just one
    assert len([n for n in env["notices"].list_all()
                if "paused" in n["message"].lower()]) == 1


# ── stop ──────────────────────────────────────────────────────────────────

def test_stop_writes_the_updater_marker_then_shuts_down_without_respawn(env):
    code, body = _post(env["client"], "/api/admin/hub/stop")
    assert code == 202, body
    marker = env["data"] / "paused"
    assert marker.is_file()
    text = marker.read_text(encoding="utf-8")
    assert "resume" in text and "127.0.0.1" in text
    assert restart_policy.stop_requested()
    assert env["shut"].wait(10)
    assert body["marker"] == "paused"


def test_stop_refused_when_the_marker_cannot_be_written(env, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("read-only")
    monkeypatch.setattr(hub_control, "_write_marker", boom)
    code, body = _post(env["client"], "/api/admin/hub/stop")
    assert code == 500 and "read-only" in body["error"]
    assert not restart_policy.stop_requested()
    time.sleep(0.3)
    assert not env["shut"].is_set()


def test_stop_without_a_shutdown_hook_is_503(env):
    hub_control.reset()
    hub_control.configure(runtime=lambda: env["rt"], notices=env["notices"])
    code, _ = _post(env["client"], "/api/admin/hub/stop")
    assert code == 503
    assert not (env["data"] / "paused").exists()
    assert not restart_policy.stop_requested()


# ── status ────────────────────────────────────────────────────────────────

def _seed_queue(db):
    store.instruments.upsert({"id": "gc1", "name": "GC-1"}, db=db)
    store.jobs.enqueue("process", {"x": 1}, db=db)                          # due now
    store.jobs.enqueue("process", {"x": 2}, not_before="2999-01-01T00:00:00+00:00", db=db)
    sid = store.samples.insert_received("gc1", "L1", "2026-09-25 14:23:00", "cdf",
                                        cdf_sha256="a" * 64, cdf_path="cdf/x.CDF", db=db)
    store.samples.insert_received("gc1", "L2", "2026-09-25 14:24:00", "cdf",
                                  cdf_sha256="b" * 64, cdf_path="cdf/y.CDF", db=db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute('INSERT INTO export_rows(instrument_id, sample_id, revision, "row") '
                     "VALUES ('gc1', ?, 1, 'line')", (sid,))
        conn.commit()
    finally:
        conn.close()


def test_status_is_open_read_only_and_complete(env):
    _seed_queue(env["db"])
    r = env["client"].get("/api/hub/status", environ_base=REMOTE)
    assert r.status_code == 200
    s = r.get_json()
    for key in ("version", "pid", "uptime_seconds", "state", "processing_paused",
                "updater_paused", "queue", "exporter", "cpu_percent", "rss_bytes",
                "cpu_count", "staged_update", "worker_alive"):
        assert key in s, key
    assert s["uptime_seconds"] >= 42
    assert s["state"] == "running" and s["processing_paused"] is False
    assert s["updater_paused"] is False
    assert s["queue"] == {"jobs_due": 1, "jobs_queued": 2, "jobs_running": 0,
                          "received_samples": 2}
    assert s["exporter"]["pending_rows"] == 1
    assert isinstance(s["cpu_percent"], (int, float)) and s["cpu_percent"] >= 0
    assert s["rss_bytes"] is None or s["rss_bytes"] > 1_000_000
    assert "password" not in json.dumps(s).lower()
    (env["data"] / "paused").write_text("x")
    assert env["client"].get("/api/hub/status").get_json()["updater_paused"] is True


def test_status_with_no_store_yet(tmp_path, monkeypatch):
    monkeypatch.setenv("GC_DATA_DIR", str(tmp_path))
    hub_control.configure(runtime=lambda: None)
    try:
        s = hub_control.status_snapshot()
    finally:
        hub_control.reset()
    assert s["state"] == "starting"
    assert s["queue"] == {"jobs_due": None, "jobs_queued": None, "jobs_running": None,
                          "received_samples": None}
    assert s["exporter"]["pending_rows"] is None
    json.dumps(s)


def test_cpu_sampler_measures_over_a_window():
    t = {"wall": 100.0, "cpu": 10.0}
    s = hub_control.ProcessSampler(clock=lambda: t["wall"], cpu_seconds=lambda: t["cpu"],
                                   rss=lambda: 123, started_at=0.0, window=15.0)
    first = s.sample()
    assert first["rss_bytes"] == 123
    assert first["cpu_percent"] == pytest.approx(10.0)        # lifetime average: 10 s / 100 s
    t.update(wall=105.0, cpu=12.5)
    assert s.sample()["cpu_percent"] == pytest.approx(50.0)   # 2.5 s over 5 s
    t.update(wall=110.0, cpu=17.5)
    assert s.sample()["cpu_percent"] == pytest.approx(75.0)   # 7.5 s over 10 s
    t.update(wall=130.0, cpu=17.5)                            # idle; old samples age out
    assert s.sample()["cpu_percent"] == pytest.approx(0.0)
