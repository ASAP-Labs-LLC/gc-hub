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


class FakeExporter:
    ticking = False


class FakeRuntime:
    def __init__(self, paused=False):
        self.paused = paused
        self.calls = []
        self.worker = FakeWorker(self)
        self.exporter = FakeExporter()

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
@pytest.mark.parametrize("host", ["evil.example", "evil.example:5560", "10.0.0.5:5560",
                                  "asapsv1:5560"])
def test_a_foreign_host_header_is_refused_even_from_loopback(env, path, host):
    # DNS rebinding: a page on evil.example resolved to 127.0.0.1 reaches us
    # from loopback, but with its own name in Host.
    code, body = _post(env["client"], path, headers={"Host": host})
    assert code == 403
    assert env["rt"].calls == [] and not env["shut"].is_set()


@pytest.mark.parametrize("host", ["localhost", "localhost:5560", "127.0.0.1:5560",
                                  "[::1]:5560", "127.9.9.9"])
def test_loopback_host_headers_are_accepted(env, host):
    code, _ = _post(env["client"], "/api/admin/hub/pause-processing", headers={"Host": host})
    assert code == 202


def test_refusals_are_logged_at_most_once_a_minute_per_address(env, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="hub_control"):
        for _ in range(5):
            _post(env["client"], ROUTES[0], environ=REMOTE)
        _post(env["client"], ROUTES[0], environ={"REMOTE_ADDR": "10.9.9.9"})
    lines = [r for r in caplog.records if "not local" in r.getMessage()]
    assert len(lines) == 2


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

def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


def test_pause_then_resume(env):
    # 202 at once: the threads are stopped/started on a background thread
    # (a pause may wait on a running job), the tray polls the status.
    c, rt, db, notices = env["client"], env["rt"], env["db"], env["notices"]
    code, body = _post(c, "/api/admin/hub/pause-processing")
    assert code == 202 and body["processing_paused"] is True
    assert _wait(lambda: rt.calls == ["pause"])
    assert hub.processing_paused(db)["by"] == "127.0.0.1"
    paused_notes = [n for n in notices.list_all() if "paused" in n["message"].lower()]
    assert len(paused_notes) == 1 and paused_notes[0]["level"] == "warning"
    # idempotent: no second notice
    assert _post(c, "/api/admin/hub/pause-processing")[0] == 202
    assert len([n for n in notices.list_all() if "paused" in n["message"].lower()]) == 1

    snap = hub_control.status_snapshot()
    assert snap["processing_paused"] is True and snap["state"] == "processing-paused"

    code, body = _post(c, "/api/admin/hub/resume-processing")
    assert code == 202 and body["processing_paused"] is False
    assert _wait(lambda: rt.calls[-1] == "resume")
    assert hub.processing_paused(db) is None
    # the paused notice is gone; a resumed one says so
    assert not any(n["id"] == paused_notes[0]["id"] for n in notices.list_all())
    assert any("resumed" in n["message"].lower() for n in notices.list_all())
    assert hub_control.status_snapshot()["state"] == "running"


def test_a_slow_pause_does_not_hold_the_request(env):
    gate = threading.Event()
    rt = env["rt"]
    orig = rt.pause

    def slow():
        gate.wait(10)
        orig()
    rt.pause = slow
    t0 = time.time()
    code, _ = _post(env["client"], "/api/admin/hub/pause-processing")
    assert code == 202 and time.time() - t0 < 2
    gate.set()
    assert _wait(lambda: rt.paused)


def test_the_tray_user_is_recorded_and_sanitised(env):
    code, _ = _post(env["client"], "/api/admin/hub/pause-processing",
                    {"password": PW, "by": "ASAP\\ryan<script>" + "x" * 100})
    assert code == 202
    by = hub.processing_paused(env["db"])["by"]
    assert by.startswith("ASAP\\ryanscript") and by.endswith("(127.0.0.1)")
    assert "<" not in by and len(by) <= 64 + len(" (127.0.0.1)")
    note = [n for n in env["notices"].list_all() if "paused" in n["message"].lower()][0]
    assert "paused by ASAP\\ryanscript" in note["message"]


def test_pause_before_the_hub_has_started_is_remembered(env):
    hub_control.configure(runtime=lambda: None)
    code, body = _post(env["client"], "/api/admin/hub/pause-processing")
    assert code == 202 and body["processing_paused"] is True
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


def test_stop_refuses_while_work_is_in_progress_unless_forced(env, monkeypatch):
    rt = env["rt"]
    rt.exporter.ticking = True
    hub_control.configure(busy_extra=lambda: ["a QBench upload is running"])
    store.instruments.upsert({"id": "gc1", "name": "GC-1"}, db=env["db"])
    store.jobs.enqueue("process", {"x": 1}, db=env["db"])
    with store.connection(env["db"]) as conn:
        conn.execute("UPDATE jobs SET state='running'")
        conn.commit()

    class Jobs:
        def current(self):
            return {"state": "running", "kind": "load-folder"}
    import hub_admin
    monkeypatch.setattr(hub_admin, "JOBS", Jobs())
    code, body = _post(env["client"], "/api/admin/hub/stop")
    assert code == 409, body
    text = " ".join(body["busy"])
    for want in ("QBench", "export", "load-folder", "job"):
        assert want in text, text
    assert not (env["data"] / "paused").exists() and not restart_policy.stop_requested()
    code, body = _post(env["client"], "/api/admin/hub/stop", {"password": PW, "force": True})
    assert code == 202, body
    assert env["shut"].wait(10)


def test_busy_includes_a_diagnostics_build_when_that_module_says_so(env, monkeypatch):
    import types
    fake = types.ModuleType("diagnostics")
    fake.busy = lambda: True
    monkeypatch.setitem(sys.modules, "diagnostics", fake)
    assert any("diagnostics" in b for b in hub_control.busy_reasons())
    fake.busy = lambda: False
    assert hub_control.busy_reasons() == []


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
    hub_control.refresh_cache()
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
    assert s["stale"] is False
    assert isinstance(s["cpu_percent"], (int, float)) and s["cpu_percent"] >= 0
    assert s["rss_bytes"] is None or s["rss_bytes"] > 1_000_000
    assert "password" not in json.dumps(s).lower()
    (env["data"] / "paused").write_text("x")
    assert env["client"].get("/api/hub/status").get_json()["updater_paused"] is True


def test_status_never_touches_sqlite_on_the_request(env, monkeypatch):
    _seed_queue(env["db"])
    hub_control.refresh_cache()

    def boom(*a, **k):
        raise AssertionError("the request path opened the database")
    monkeypatch.setattr(sqlite3, "connect", boom)
    monkeypatch.setattr(store, "connection", boom)
    s = hub_control.status_snapshot()
    assert s["queue"]["jobs_queued"] == 2 and s["stale"] is False


def test_refresh_under_an_exclusive_lock_is_quick_keeps_the_last_values_and_goes_stale(
        env, monkeypatch):
    _seed_queue(env["db"])
    hub_control.refresh_cache()
    lock = sqlite3.connect(str(env["db"]), isolation_level=None)
    try:
        lock.execute("PRAGMA locking_mode=EXCLUSIVE")
        lock.execute("BEGIN EXCLUSIVE")
        t0 = time.time()
        assert hub_control.refresh_cache() is False
        assert time.time() - t0 < 1.0
        s = hub_control.status_snapshot()
        assert s["queue"]["jobs_queued"] == 2          # the last good values
        monkeypatch.setattr(hub_control, "CACHE_STALE_SECONDS", 0.0)
        assert hub_control.status_snapshot()["stale"] is True
    finally:
        lock.execute("ROLLBACK")
        lock.close()


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


# ── the Cloudflare tunnel (spec D4/D5 rev 2): never local ─────────────────

TUNNEL = {"Host": "gc.asaplabs.net", "CF-Connecting-IP": "203.0.113.9", "CF-Ray": "8c-DFW",
          "X-Forwarded-Proto": "https", "Origin": "https://gc.asaplabs.net"}


@pytest.mark.parametrize("path", ROUTES)
def test_the_tunnel_is_refused_even_with_the_password(env, path):
    code, body = _post(env["client"], path, environ=LOCAL, headers=TUNNEL)
    assert code == 403, body
    assert env["rt"].calls == [] and not env["shut"].is_set()
    assert not (env["data"] / "paused").exists()


@pytest.mark.parametrize("hdr", [{"CF-Ray": "x"}, {"X-Forwarded-For": "203.0.113.9"},
                                 {"CF-Connecting-IP": "127.0.0.1"}])
def test_a_single_forwarding_header_on_loopback_is_not_local(env, hdr):
    code, _ = _post(env["client"], ROUTES[0], headers=dict(hdr, Host="localhost:5560"))
    assert code == 403 and env["rt"].calls == []


def test_status_carries_the_effective_hub_url(env):
    import store as _store
    assert hub_control.refresh_cache()
    assert hub_control.status_snapshot()["hub_url"] == "https://gc.asaplabs.net"
    _store.settings_kv.set(admin_auth.HUB_URL_KEY, "http://asapsv1:5560", db=env["db"])
    assert hub_control.refresh_cache()
    assert hub_control.status_snapshot()["hub_url"] == "http://asapsv1:5560"
