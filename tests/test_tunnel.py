"""The hub behind the Cloudflare tunnel (https://gc.asaplabs.net), in process.

cloudflared forwards to http://localhost:5560, so a tunnel request is faked
here as ``REMOTE_ADDR`` 127.0.0.1 + ``Host: gc.asaplabs.net`` + Cloudflare's
headers. The Blueprints are mounted on a bare Flask app (never ``import
app``); app.py's guard is checked to delegate to ``netctx.is_cross_site``.
"""
from __future__ import annotations

import ast
import json
import sys
import threading
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")

from flask import Flask, jsonify  # noqa: E402

import admin_auth  # noqa: E402
import hub_control  # noqa: E402
import netctx  # noqa: E402
import notifications  # noqa: E402
import restart_policy  # noqa: E402

PW = "tunnel-admin-pw"
PUBLIC = "gc.asaplabs.net"
LOOP = {"REMOTE_ADDR": "127.0.0.1"}
LAN = {"REMOTE_ADDR": "10.1.2.3"}


def tunnel_headers(ip="203.0.113.7", **extra):
    h = {"Host": PUBLIC, "CF-Connecting-IP": ip, "CF-Ray": "8c0ffee-AMS",
         "X-Forwarded-For": ip, "X-Forwarded-Proto": "https"}
    h.update(extra)
    return h


BROWSER = {"Origin": f"https://{PUBLIC}", "Sec-Fetch-Site": "same-origin"}


class FakeRuntime:
    paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


def _app():
    app = Flask("tunnel_test")
    app.register_blueprint(admin_auth.bp)
    app.register_blueprint(hub_control.bp)

    @app.route("/probe", methods=["GET", "POST"])
    def probe():
        return jsonify({"client_ip": netctx.client_ip(), "https": netctx.is_https(),
                        "via_proxy": netctx.via_proxy(), "local": netctx.is_local(),
                        "cross_site": netctx.is_cross_site(),
                        "admin_cross_site": admin_auth._cross_site(),
                        "public_host": netctx.public_host()})
    return app


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("GC_DATA_DIR", str(data))
    monkeypatch.setattr(admin_auth, "ITERATIONS", 1000)     # speed only
    admin_auth.reset_throttle()
    db = admin_auth.hub_db()
    shut = threading.Event()
    hub_control.configure(runtime=FakeRuntime, shutdown=shut.set,
                          notices=notifications.NotificationStore(data / "notifications.json"),
                          started_at=time.time())
    try:
        yield {"client": _app().test_client(), "db": db, "shut": shut}
    finally:
        hub_control.reset()
        restart_policy._reset_stop_for_tests()
        admin_auth.reset_throttle()


def _probe(c, headers=None, environ=LOOP, method="post"):
    r = getattr(c, method)("/probe", headers=headers or {}, environ_base=environ)
    return r.get_json()


def _post(c, path, body, headers=None, environ=LOOP):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    r = c.post(path, data=json.dumps(body), headers=h, environ_base=environ)
    return r.status_code, r.get_json(silent=True)


# ── netctx ────────────────────────────────────────────────────────────────

def test_tunnel_request_context(env):
    p = _probe(env["client"], tunnel_headers())
    assert p["client_ip"] == "203.0.113.7" and p["https"] and p["via_proxy"]
    assert not p["local"] and p["public_host"] == PUBLIC


def test_spoofed_forwarding_headers_from_the_lan_are_ignored(env):
    p = _probe(env["client"], tunnel_headers(), environ=LAN)
    assert p["client_ip"] == "10.1.2.3" and not p["https"] and not p["local"]
    assert p["public_host"] is None


def test_invalid_cf_connecting_ip_falls_back(env):
    p = _probe(env["client"], tunnel_headers(ip="not-an-ip"))
    assert p["client_ip"] == "127.0.0.1" and not p["local"]


def test_local_console_and_lan_unchanged(env):
    p = _probe(env["client"], {"Host": "localhost:5560"})
    assert p["local"] and p["client_ip"] == "127.0.0.1" and not p["https"]
    p = _probe(env["client"], {"Host": "127.0.0.1:5560", "X-Forwarded-For": "1.2.3.4"})
    assert not p["local"]                                   # any proxy header: not local
    p = _probe(env["client"], {"Host": "asapsv1:5560"})
    assert not p["local"]                                   # loopback, but a LAN Host
    p = _probe(env["client"], {"Host": "localhost:5560"}, environ=LAN)
    assert not p["local"]


# ── both origin checks ────────────────────────────────────────────────────

def test_browser_post_through_the_tunnel_passes_both_guards(env):
    p = _probe(env["client"], tunnel_headers(**BROWSER))
    assert p["cross_site"] is False and p["admin_cross_site"] is False


@pytest.mark.parametrize("origin", ["https://evil.example", "http://gc.asaplabs.net",
                                    "https://gc.asaplabs.net:8443", "null"])
def test_foreign_origins_still_refused_through_the_tunnel(env, origin):
    p = _probe(env["client"], tunnel_headers(Origin=origin))
    assert p["cross_site"] and p["admin_cross_site"]
    p = _probe(env["client"], tunnel_headers(**{"Sec-Fetch-Site": "cross-site"}))
    assert p["cross_site"] and p["admin_cross_site"]


def test_https_origin_without_trusted_proxy_is_refused(env):
    # X-Forwarded-Proto from a LAN address is not trusted: the scheme stays http
    h = {"Host": PUBLIC, "X-Forwarded-Proto": "https", **BROWSER}
    p = _probe(env["client"], h, environ=LAN)
    assert p["cross_site"] and p["admin_cross_site"]


def test_lan_same_origin_unchanged(env):
    h = {"Host": "asapsv1:5560", "Origin": "http://asapsv1:5560", "Sec-Fetch-Site": "same-origin"}
    p = _probe(env["client"], h, environ=LAN)
    assert p["cross_site"] is False and p["admin_cross_site"] is False
    h["Origin"] = "http://evil.example"
    p = _probe(env["client"], h, environ=LAN)
    assert p["cross_site"] and p["admin_cross_site"]


def test_app_guard_delegates_to_netctx():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_is_cross_site_request")
    calls = {ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "netctx.is_cross_site" in calls


# ── setup Host check ──────────────────────────────────────────────────────

def _code(env):
    return admin_auth.ensure_setup_code(db=env["db"])


def test_setup_through_the_tunnel(env):
    code, body = _post(env["client"], "/api/admin/setup",
                       {"password": PW, "setup_code": _code(env)}, tunnel_headers(**BROWSER))
    assert code == 201, body
    assert admin_auth.is_set(db=env["db"])


def test_public_host_without_https_is_refused(env):
    h = tunnel_headers()
    del h["X-Forwarded-Proto"]
    code, _ = _post(env["client"], "/api/admin/setup",
                    {"password": PW, "setup_code": _code(env)}, h)
    assert code == 403
    code, _ = _post(env["client"], "/api/admin/setup",           # https claimed from the LAN
                    {"password": PW, "setup_code": _code(env)}, tunnel_headers(), environ=LAN)
    assert code == 403
    assert not admin_auth.is_set(db=env["db"])


def test_foreign_host_still_refused(env):
    code, body = _post(env["client"], "/api/admin/setup",
                       {"password": PW, "setup_code": _code(env)},
                       tunnel_headers(Host="evil.example"))
    assert code == 403 and "Host" in body["error"]
    assert not admin_auth.is_set(db=env["db"])


# ── throttles ─────────────────────────────────────────────────────────────

def _wrong_change(env, headers, environ=LOOP):
    return _post(env["client"], "/api/admin/password",
                 {"password": "wrong-password", "new_password": "whatever-123"},
                 headers, environ)


def test_tunnel_clients_keyed_per_cf_ip_and_not_exempt(env):
    admin_auth.setup(PW, _code(env), db=env["db"])
    admin_auth.reset_throttle()
    _wrong_change(env, tunnel_headers(ip="203.0.113.7"))
    _wrong_change(env, tunnel_headers(ip="198.51.100.9"))
    keys = set(admin_auth._throttle._state)
    assert keys == {"203.0.113.7", "198.51.100.9"}
    assert len(admin_auth._throttle._global) == 2           # both spent the hub-wide budget


def test_tunnel_with_loopback_cf_ip_is_not_exempt(env):
    admin_auth.setup(PW, _code(env), db=env["db"])
    admin_auth.reset_throttle()
    _wrong_change(env, tunnel_headers(ip="127.0.0.1"))
    _wrong_change(env, tunnel_headers(ip="garbage"))
    assert len(admin_auth._throttle._global) == 2


def test_local_console_still_exempt_from_the_global_budget(env):
    admin_auth.setup(PW, _code(env), db=env["db"])
    admin_auth.reset_throttle()
    _wrong_change(env, {"Host": "localhost:5560"})
    assert set(admin_auth._throttle._state) == {"127.0.0.1"}
    assert len(admin_auth._throttle._global) == 0


def test_spoofed_cf_ip_from_the_lan_is_ignored_by_the_throttle(env):
    admin_auth.setup(PW, _code(env), db=env["db"])
    admin_auth.reset_throttle()
    _wrong_change(env, tunnel_headers(ip="203.0.113.7"), environ=LAN)
    assert set(admin_auth._throttle._state) == {"10.1.2.3"}
    assert len(admin_auth._throttle._global) == 1


# ── hub_control tray routes ───────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/admin/hub/pause-processing",
                                  "/api/admin/hub/resume-processing", "/api/admin/hub/stop"])
def test_tray_routes_refused_through_the_tunnel(env, path):
    admin_auth.setup(PW, _code(env), db=env["db"])
    for h in (tunnel_headers(**BROWSER), tunnel_headers(Host="localhost:5560"),
              {"Host": "localhost:5560", "CF-Ray": "x"}):
        code, body = _post(env["client"], path, {"password": PW}, h)
        assert code == 403, (h, body)
    assert not env["shut"].is_set()


def test_tray_route_still_works_locally(env):
    admin_auth.setup(PW, _code(env), db=env["db"])
    code, body = _post(env["client"], "/api/admin/hub/pause-processing", {"password": PW},
                       {"Host": "localhost:5560"})
    assert code in (200, 202), body
