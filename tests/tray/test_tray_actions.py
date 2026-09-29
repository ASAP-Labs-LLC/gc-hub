"""The tray's HTTP client (against a small fake hub) and its actions
(Controller) with a scripted UI: password prompting and caching, confirm
dialogs, Restart & install, Stop, Start via the updater CLI."""
from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gc_tray import client as client_mod
from gc_tray import controller as controller_mod
from gc_tray import logic

PW = "right-password"


class FakeHub:
    """Answers like hub_control: status, the three admin routes, /api/restart."""

    def __init__(self):
        self.requests = []
        self.status = {"version": "v3.0.0", "state": "running", "processing_paused": False,
                       "queue": {"jobs_due": 0, "jobs_queued": 0}, "staged_update": None}
        self.restart_answer = (200, {"mode": "restart", "tag": None, "pid": 1})
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                hub.requests.append(("GET", self.path, None, dict(self.headers)))
                if self.path == "/api/hub/status":
                    return self._send(200, hub.status)
                self._send(404, {"error": "Not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                hub.requests.append(("POST", self.path, body, dict(self.headers)))
                if self.path == "/api/restart":
                    return self._send(*hub.restart_answer)
                if body.get("password") != PW:
                    return self._send(403, {"error": "Incorrect password"})
                if self.path == "/api/admin/hub/pause-processing":
                    return self._send(200, {"processing_paused": True})
                if self.path == "/api/admin/hub/resume-processing":
                    return self._send(200, {"processing_paused": False})
                if self.path == "/api/admin/hub/stop":
                    return self._send(202, {"stopping": True, "marker": "paused"})
                self._send(404, {"error": "Not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def posts(self, path):
        return [r for r in self.requests if r[0] == "POST" and r[1] == path]


@pytest.fixture
def fake():
    h = FakeHub()
    yield h
    h.close()


class ScriptedUI:
    def __init__(self, passwords=(), confirm=True):
        self.passwords = list(passwords)
        self.confirm_answer = confirm
        self.log = []

    def ask_password(self, prompt):
        self.log.append(("ask", prompt))
        return self.passwords.pop(0) if self.passwords else None

    def confirm(self, title, text):
        self.log.append(("confirm", title, text))
        return self.confirm_answer

    def info(self, title, text):
        self.log.append(("info", title, text))

    def error(self, title, text):
        self.log.append(("error", title, text))

    def kinds(self):
        return [e[0] for e in self.log]


def _ctl(fake, ui, **cfg):
    conf = dict(logic.DEFAULTS, port=fake.port, **cfg)
    return controller_mod.Controller(conf, client_mod.HubClient(logic.status_url(conf)), ui,
                                     python="py.exe")


# ── client ────────────────────────────────────────────────────────────────

def test_client_status_and_post(fake):
    c = client_mod.HubClient(f"http://127.0.0.1:{fake.port}")
    assert c.status()["version"] == "v3.0.0"
    code, body = c.post("/api/admin/hub/stop", {"password": PW})
    assert (code, body["stopping"]) == (202, True)
    code, body = c.post("/api/admin/hub/stop", {"password": "nope"})
    assert code == 403 and body["error"] == "Incorrect password"
    method, path, sent, headers = fake.requests[-1]
    assert headers.get("Content-Type") == "application/json"
    assert "Origin" not in headers           # passes the hub's cross-site guard


def test_client_unreachable_is_none():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    c = client_mod.HubClient(f"http://127.0.0.1:{port}", timeout=0.5)
    assert c.status() is None
    code, body = c.post("/api/restart", {})
    assert code is None and "error" in body


# ── pause / resume ────────────────────────────────────────────────────────

def test_pause_asks_once_then_remembers_the_password(fake):
    ui = ScriptedUI(passwords=[PW])
    ctl = _ctl(fake, ui)
    view = logic.parse_status(fake.status, marker_present=False)
    assert ctl.toggle_pause(view) is True
    assert fake.posts("/api/admin/hub/pause-processing")[-1][2] == {"password": PW}
    paused = logic.parse_status(dict(fake.status, state="processing-paused",
                                     processing_paused=True), marker_present=False)
    assert ctl.toggle_pause(paused) is True
    assert len(fake.posts("/api/admin/hub/resume-processing")) == 1
    assert ui.kinds().count("ask") == 1            # remembered for the session


def test_the_password_is_not_remembered_when_disabled(fake):
    ui = ScriptedUI(passwords=[PW, PW])
    ctl = _ctl(fake, ui, remember_password=False)
    view = logic.parse_status(fake.status, marker_present=False)
    ctl.toggle_pause(view)
    ctl.toggle_pause(view)
    assert ui.kinds().count("ask") == 2


def test_a_wrong_password_is_forgotten_and_asked_again(fake):
    ui = ScriptedUI(passwords=["wrong", PW])
    ctl = _ctl(fake, ui)
    assert ctl.toggle_pause(logic.parse_status(fake.status, marker_present=False)) is True
    assert ui.kinds().count("ask") == 2
    assert "error" in ui.kinds()


def test_cancelling_the_prompt_does_nothing(fake):
    ui = ScriptedUI(passwords=[])
    ctl = _ctl(fake, ui)
    assert ctl.toggle_pause(logic.parse_status(fake.status, marker_present=False)) is False
    assert fake.posts("/api/admin/hub/pause-processing") == []


def test_the_password_is_never_written_to_disk(fake, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ui = ScriptedUI(passwords=[PW])
    ctl = _ctl(fake, ui)
    ctl.toggle_pause(logic.parse_status(fake.status, marker_present=False))
    assert list(tmp_path.iterdir()) == []
    assert PW not in repr(ctl)


# ── restart / stop / start ────────────────────────────────────────────────

def test_restart_confirms_and_names_the_staged_release(fake):
    ui = ScriptedUI()
    ctl = _ctl(fake, ui)
    view = logic.parse_status(dict(fake.status, staged_update="v3.1.0"), marker_present=False)
    fake.restart_answer = (200, {"mode": "switch", "tag": "v3.1.0", "pid": 1})
    assert ctl.restart(view) is True
    assert "v3.1.0" in ui.log[0][2]
    assert fake.posts("/api/restart")[-1][2] == {}


def test_restart_declined_or_refused(fake):
    ui = ScriptedUI(confirm=False)
    ctl = _ctl(fake, ui)
    view = logic.parse_status(fake.status, marker_present=False)
    assert ctl.restart(view) is False and fake.posts("/api/restart") == []
    ui.confirm_answer = True
    fake.restart_answer = (409, {"error": "The server just restarted, try again in 200 s"})
    assert ctl.restart(view) is False
    assert ui.log[-1][0] == "error" and "200 s" in ui.log[-1][2]


def test_stop_confirms_explains_start_and_sends_the_password(fake):
    ui = ScriptedUI(passwords=[PW])
    ctl = _ctl(fake, ui)
    assert ctl.stop() is True
    confirm = [e for e in ui.log if e[0] == "confirm"][0]
    assert "Start hub" in confirm[2]
    assert fake.posts("/api/admin/hub/stop")[-1][2] == {"password": PW}


def test_stop_declined_sends_nothing(fake):
    ui = ScriptedUI(passwords=[PW], confirm=False)
    assert _ctl(fake, ui).stop() is False
    assert fake.posts("/api/admin/hub/stop") == []


def test_start_runs_the_updater_resume(fake):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, "gc: resumed", "")

    ui = ScriptedUI()
    ctl = _ctl(fake, ui)
    ctl.run_cmd = run
    assert ctl.start() is True
    cmd = calls[0][0]
    assert cmd[0] == "py.exe" and cmd[2:] == ["resume", "--app", "gc", "--config",
                                              logic.DEFAULTS["updater_config"]]
    assert ui.log[-1][0] == "info" and "20 s" in ui.log[-1][2]


def test_start_falls_back_to_removing_the_marker(fake, tmp_path):
    (tmp_path / "paused").write_text("stopped")

    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "PermissionError: updater.log")

    ui = ScriptedUI()
    ctl = _ctl(fake, ui, data_dir=str(tmp_path))
    ctl.run_cmd = run
    assert ctl.start() is True
    assert not (tmp_path / "paused").exists()


def test_start_reports_when_nothing_works(fake, tmp_path):
    def run(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    ui = ScriptedUI()
    ctl = _ctl(fake, ui, data_dir=str(tmp_path / "missing-dir"))
    ctl.run_cmd = run
    (tmp_path / "missing-dir").mkdir()
    (tmp_path / "missing-dir" / "paused").mkdir()     # cannot be unlinked as a file
    assert ctl.start() is False
    assert ui.log[-1][0] == "error"


def test_start_offers_elevation_when_access_is_denied(fake, tmp_path):
    # C:\ASAPApps\gc\data is Administrators-only; an unelevated tray can
    # neither run the updater (updater.log) nor remove the marker.
    elevated = []

    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "PermissionError: [WinError 5]")

    def elevate(cmd):
        elevated.append(cmd)
        return True

    ui = ScriptedUI(confirm=True)
    ctl = _ctl(fake, ui, data_dir=str(tmp_path / "nope"))
    ctl.run_cmd = run
    ctl.unlink_marker = lambda p: (_ for _ in ()).throw(PermissionError("denied"))
    ctl.elevate = elevate
    assert ctl.start() is True
    assert elevated and elevated[0][2:4] == ["resume", "--app"]
    assert any(e[0] == "confirm" and "administrator" in e[2].lower() for e in ui.log)
    # declined at the confirm: nothing elevated
    ui2 = ScriptedUI(confirm=False)
    ctl2 = _ctl(fake, ui2, data_dir=str(tmp_path / "nope"))
    ctl2.run_cmd, ctl2.elevate = run, elevate
    ctl2.unlink_marker = ctl.unlink_marker
    assert ctl2.start() is False and len(elevated) == 1
    # elevation refused (UAC "No")
    ctl.elevate = lambda cmd: False
    assert ctl.start() is False and ui.log[-1][0] == "error"


def test_open_browser_uses_localhost(fake):
    opened = []
    ctl = _ctl(fake, ScriptedUI())
    ctl.open_url = opened.append
    ctl.open_browser()
    assert opened == [f"http://localhost:{fake.port}"]
