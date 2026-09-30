"""web_auth.py: sign-in, sessions and the session gate (spec D1-D8, rev 2),
in process on a bare Flask app wired the way app.py wires it (https
redirect → cross-site guard → gate), never ``import app``.

LabCore is ``tests/labcore_stub.py`` on 127.0.0.1 (``LABCORE_URL``), never the
real one. The tunnel is simulated as cloudflared presents it: a loopback
peer with ``Host: gc.asaplabs.net``, ``CF-Connecting-IP``, ``CF-Ray`` and
``X-Forwarded-Proto``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")

from flask import Flask, jsonify, render_template_string  # noqa: E402

import admin_auth  # noqa: E402
import netctx  # noqa: E402
import store  # noqa: E402
import web_auth  # noqa: E402
from labcore_stub import LabCoreStub  # noqa: E402

LAN = {"REMOTE_ADDR": "10.0.0.25"}
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
JSON = {"Content-Type": "application/json"}
ADMIN_PW = "break-glass-pw"
HTTPS_MESSAGE = "Sign in over https: open https://gc.asaplabs.net"


def tunnel(ip="203.0.113.9", proto="https", **extra):
    h = {"Host": "gc.asaplabs.net", "CF-Connecting-IP": ip, "CF-Ray": "8c1d-DFW",
         "X-Forwarded-Proto": proto, "X-Forwarded-For": ip}
    h.update(extra)
    return h


def make_app():
    app = Flask("web_auth_test", template_folder=str(ROOT / "templates"),
                static_folder=str(ROOT / "static"))
    app.register_blueprint(admin_auth.bp)
    app.register_blueprint(web_auth.bp)
    app.before_request(web_auth.require_https)

    @app.before_request
    def _cross_site():
        from flask import request
        if request.method in ("POST", "PUT", "PATCH", "DELETE") \
                and request.path.startswith("/api/"):
            refusal = netctx.cross_site_refusal()      # app._refuse_cross_site_writes
            if refusal:
                return jsonify({"error": refusal}), 403
        return None

    app.before_request(web_auth.gate)

    @app.before_request
    def _track():                 # app._track_activity's session part
        web_auth.note_seen(web_auth.current_user())

    app.after_request(web_auth.add_security_headers)
    app.context_processor(web_auth.template_context)

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.route("/")
    def index():
        return render_template_string('<p>home</p>{% include "_signed_in.html" %}')

    @app.route("/instruments")
    def instruments():
        return "instruments"

    @app.route("/api/thing", methods=["GET", "POST"])
    def thing():
        return jsonify({"who": web_auth.actor()})

    @app.route("/api/agents")
    def agents():
        return jsonify({"agents": []})

    @app.route("/api/agent/heartbeat", methods=["POST"])
    def heartbeat():
        return jsonify({"ok": True})

    @app.route("/api/ingest", methods=["POST"])
    def ingest():
        return jsonify({"ok": True})

    @app.route("/api/hub/status")
    def hub_status():
        return jsonify({"state": "running"})

    @app.route("/api/admin/hub/stop", methods=["POST"])
    def hub_stop():
        return jsonify({"stopping": True}), 202

    @app.route("/api/restart", methods=["POST"])
    def restart():
        return jsonify({"mode": "restart"})

    return app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("GC_DATA_DIR", str(data))
    with LabCoreStub() as stub:
        monkeypatch.setenv("LABCORE_URL", stub.url)
        admin_auth.reset_throttle()
        web_auth.reset()
        netctx._proxy_cache.clear()
        db = admin_auth.hub_db()
        app = make_app()
        yield {"app": app, "client": app.test_client(), "stub": stub, "db": db, "data": data}
        web_auth.reset()
        admin_auth.reset_throttle()


def set_admin(env):
    admin_auth.setup(ADMIN_PW, admin_auth.ensure_setup_code(db=env["db"]), db=env["db"])


def post(c, path, body=None, environ=LAN, headers=None):
    h = dict(JSON)
    h.update(headers or {})
    r = c.post(path, data=json.dumps(body if body is not None else {}), headers=h,
               environ_base=environ)
    return r


def get(c, path, environ=LAN, headers=None):
    return c.get(path, headers=headers or {}, environ_base=environ)


def login(c, user="ryan c", pw="labpass-1", environ=LAN, headers=None, **extra):
    return post(c, "/api/login", dict({"username": user, "password": pw}, **extra),
                environ=environ, headers=headers)


def cookie_of(resp, name):
    for h in resp.headers.getlist("Set-Cookie"):
        if h.startswith(name + "="):
            return h
    return None


# ── the gate ────────────────────────────────────────────────────────────────

def test_a_page_redirects_to_login_with_next(env):
    r = get(env["client"], "/instruments?x=1")
    assert r.status_code == 302
    assert r.headers["Location"] == "/login?next=/instruments%3Fx%3D1"


def test_an_api_request_gets_401_with_the_marker(env):
    for method in ("GET", "POST"):
        r = env["client"].open("/api/thing", method=method, environ_base=LAN)
        assert r.status_code == 401
        assert r.get_json() == {"error": "Sign in required", "login_required": True}
        assert r.headers[web_auth.LOGIN_REQUIRED_HEADER] == "1"


def test_open_paths(env):
    c = env["client"]
    assert get(c, "/healthz").status_code == 200
    assert get(c, "/login").status_code == 200
    assert get(c, "/static/js/session.js").status_code == 200
    assert post(c, "/api/agent/heartbeat").status_code == 200
    assert post(c, "/api/ingest").status_code == 200
    assert post(c, "/api/logout").status_code == 200
    assert get(c, "/api/agents").status_code == 401          # not an agent path
    assert get(c, "/api/agent/heartbeat/../../thing").status_code in (401, 404)


@pytest.mark.parametrize("path,cls", [
    ("/healthz", "open"), ("/login", "open"), ("/api/login", "open"),
    ("/api/login/card", "open"), ("/api/login/admin", "open"), ("/api/logout", "open"),
    ("/static/js/app.js", "open"), ("/favicon.ico", "open"), ("/api/ingest", "open"),
    ("/api/agent/heartbeat", "open"), ("/api/agent/results", "open"),
    ("/api/agent/package", "open"), ("/api/agent/package.zip", "open"),
    ("/api/agents", "session"), ("/api/agent/other", "session"), ("/api/ingestx", "session"),
    ("/api/login/other", "session"), ("/api/loginx", "session"), ("/loginx", "session"),
    ("/api/hub/status", "local"), ("/api/admin/hub/stop", "local"),
    ("/api/admin/hub/pause-processing", "local"), ("/api/admin/hub/resume-processing", "local"),
    ("/api/restart", "local"), ("/api/admin/hub/other", "session"),
    ("/admin/setup", "setup"), ("/api/admin/setup", "setup"), ("/admin/hub", "session"),
    ("/", "session"), ("/api/session", "session"), ("/api/lem/machines", "session"),
])
def test_route_class(path, cls):
    assert web_auth.route_class(path) == cls


@pytest.mark.parametrize("raw,want", [
    ("/instruments?x=1", "/instruments?x=1"), ("/", "/"), ("//evil.example", "/"),
    ("/\\evil.example", "/"), ("https://evil.example/", "/"), ("/api/files", "/"),
    ("/API/x", "/"), ("/login", "/"), ("/login?next=/x", "/"), ("/a\nb", "/"),
    ("/a\\b", "/"), ("", "/"), (None, "/"), ("instruments", "/"), ("/" + "x" * 2100, "/"),
])
def test_safe_next(raw, want):
    assert web_auth.safe_next(raw) == want


# ── password sign-in ────────────────────────────────────────────────────────

def test_password_sign_in_over_the_lan(env):
    c = env["client"]
    r = login(c, next="/instruments")
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {"ok": True, "name": "Ryan C", "method": "password",
                            "next": "/instruments"}
    ck = cookie_of(r, "gc_session")
    assert ck and "HttpOnly" in ck and "SameSite=Lax" in ck and "Path=/" in ck
    assert "Secure" not in ck and f"Max-Age={14 * 86400}" in ck
    assert cookie_of(r, "__Host-gc_session") is None
    assert get(c, "/api/session").get_json() == {"name": "Ryan C", "method": "password"}
    assert get(c, "/instruments").status_code == 200
    assert get(c, "/api/thing").get_json() == {"who": "Ryan C (10.0.0.25)"}
    row = store.web_sessions.list_active(db=env["db"])[0]
    token = ck.split(";")[0].split("=", 1)[1]
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert row["ip"] == "10.0.0.25" and row["method"] == "password"


def test_the_login_page_redirects_a_signed_in_person(env):
    c = env["client"]
    login(c)
    r = get(c, "/login?next=/instruments")
    assert r.status_code == 302 and r.headers["Location"] == "/instruments"
    r = get(c, "/login?next=//evil.example")
    assert r.headers["Location"] == "/"


def test_the_signed_in_line_is_on_the_page_and_escaped(env):
    env["stub"].accounts["x"] = ("pw", "<b>Mallory</b>")
    c = env["client"]
    assert "signed-in" not in get(c, "/login").get_data(as_text=True)
    login(c, "x", "pw")
    html = get(c, "/").get_data(as_text=True)
    assert "Signed in as" in html and 'id="sign-out"' in html
    assert "&lt;b&gt;Mallory&lt;/b&gt;" in html and "<b>Mallory" not in html


def test_wrong_password_is_401_without_the_marker_and_logs_no_secret(env, caplog):
    with caplog.at_level(logging.INFO, logger="web_auth"):
        r = login(env["client"], "ryan c", "not-it")
    assert r.status_code == 401 and web_auth.LOGIN_REQUIRED_HEADER not in r.headers
    assert r.get_json()["error"] == "Wrong username or password."
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "not-it" not in text and "ryan c" not in text.lower()
    assert "user#" + hashlib.sha256(b"ryan c").hexdigest()[:8] in text
    assert "10.0.0.25" in text


def test_sign_in_needs_json_and_both_fields(env):
    c = env["client"]
    assert post(c, "/api/login", {"username": "ryan c"}).status_code == 400
    r = c.post("/api/login", data="username=a&password=b",
               headers={"Content-Type": "application/x-www-form-urlencoded"}, environ_base=LAN)
    assert r.status_code == 415
    assert env["stub"].requests == []


def test_labcore_unreachable_is_503_and_not_a_failure(env):
    env["stub"].mode = "500"
    c = env["client"]
    for _ in range(8):
        r = login(c)
        assert r.status_code == 503
        body = r.get_json()
        assert body["labcore_unavailable"] is True and "admin password" in body["error"]
    env["stub"].mode = "ok"
    assert login(c).status_code == 200


def test_labcore_unreachable_through_the_tunnel_does_not_offer_the_admin_password(env):
    env["stub"].mode = "cf-1010"
    r = login(env["client"], environ=LOCAL, headers=tunnel())
    assert r.status_code == 503 and "admin password" not in r.get_json()["error"]


def test_sign_in_revokes_the_presented_session(env):
    c = env["client"]
    first = cookie_of(login(c), "gc_session")
    login(c, "jane doe", "labpass-2")
    rows = {r["name"]: r for r in store.web_sessions.list_active(db=env["db"])}
    assert set(rows) == {"Jane Doe"}
    assert first


# ── the tunnel ──────────────────────────────────────────────────────────────

def test_sign_in_through_the_tunnel_sets_the_host_cookie(env):
    c = env["client"]
    r = login(c, environ=LOCAL, headers=tunnel(Origin="https://gc.asaplabs.net"))
    assert r.status_code == 200, r.get_json()
    ck = cookie_of(r, "__Host-gc_session")
    assert ck and "Secure" in ck and "HttpOnly" in ck and "Path=/" in ck and "Domain" not in ck
    assert f"Max-Age={7 * 86400}" in ck
    assert r.headers["Strict-Transport-Security"] == "max-age=31536000"
    row = store.web_sessions.list_active(db=env["db"])[0]
    assert row["ip"] == "203.0.113.9"
    # the cookie works through the tunnel, and not as the LAN cookie name
    token = ck.split(";")[0].split("=", 1)[1]
    ok = c.get("/api/session", headers=dict(tunnel(), Cookie=f"__Host-gc_session={token}"),
               environ_base=LOCAL)
    assert ok.status_code == 200 and ok.get_json()["name"] == "Ryan C"
    c.delete_cookie("__Host-gc_session")


def test_plain_http_through_the_tunnel_is_redirected_to_https(env):
    c = env["client"]
    for method in ("GET", "POST"):
        r = c.open("/instruments?a=1", method=method, headers=tunnel(proto="http"),
                   environ_base=LOCAL)
        assert r.status_code == 308
        assert r.headers["Location"] == "https://gc.asaplabs.net/instruments?a=1"
    r = post(c, "/api/login", {"username": "ryan c", "password": "labpass-1"}, environ=LOCAL,
             headers=tunnel(proto="http"))
    assert r.status_code == 308 and env["stub"].requests == []


def test_no_hsts_on_plain_lan_http(env):
    assert "Strict-Transport-Security" not in get(env["client"], "/login").headers


def test_spoofed_cloudflare_headers_from_the_lan_are_just_the_lan(env):
    set_admin(env)
    h = tunnel(ip="8.8.8.8", proto="http")
    r = post(env["client"], "/api/login/admin", {"password": ADMIN_PW}, environ=LAN, headers=h)
    assert r.status_code == 200                       # not redirected, not "through Cloudflare"
    assert store.web_sessions.list_active(db=env["db"])[0]["ip"] == "10.0.0.25"


def test_a_cross_site_post_through_the_tunnel_is_refused(env):
    r = login(env["client"], environ=LOCAL, headers=tunnel(Origin="https://evil.example"))
    assert r.status_code == 403 and env["stub"].requests == []


# ── card ────────────────────────────────────────────────────────────────────

def test_card_sign_in(env, caplog):
    c = env["client"]
    with caplog.at_level(logging.INFO, logger="web_auth"):
        r = post(c, "/api/login/card", {"code": " CARD-0042 "})
        assert r.status_code == 200 and r.get_json()["name"] == "Ryan C"
        assert r.get_json()["method"] == "card"
        c.delete_cookie("gc_session")
        bad = post(c, "/api/login/card", {"code": "CARD-9999"})
    assert bad.status_code == 401 and bad.get_json()["error"] == "Card not recognised."
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "CARD-0042" not in text and "CARD-9999" not in text
    assert post(c, "/api/login/card", {"code": "   "}).status_code == 400   # a stray Enter


# ── throttling (D8 rev 2) ───────────────────────────────────────────────────

def test_five_failures_lock_the_user_on_this_address_for_60s(env, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(web_auth.throttle, "_clock", lambda: now[0])
    c = env["client"]
    for _ in range(5):
        assert login(c, pw="nope").status_code == 401
    r = login(c)                                  # even the right password
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 59
    assert login(c, "jane doe", "labpass-2").status_code == 200    # another user, same address
    c.delete_cookie("gc_session")
    assert login(c, environ={"REMOTE_ADDR": "10.0.0.26"}).status_code == 200  # another address
    c.delete_cookie("gc_session")
    now[0] += 61
    assert login(c).status_code == 200
    assert len(env["stub"].requests) == 5 + 1 + 1 + 1


def test_thirty_failures_lock_the_address(env, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(web_auth.throttle, "_clock", lambda: now[0])
    c = env["client"]
    for i in range(30):
        assert login(c, f"user{i}", "nope").status_code == 401
    assert login(c, "jane doe", "labpass-2").status_code == 429
    assert login(c, environ={"REMOTE_ADDR": "10.0.0.26"}).status_code == 200


def test_the_hub_wide_limit_applies_only_through_cloudflare(env, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(web_auth.throttle, "_clock", lambda: now[0])
    c = env["client"]
    for i in range(100):
        h = tunnel(ip=f"198.51.{i // 200}.{i % 200 + 1}")
        assert login(c, f"u{i}", "nope", environ=LOCAL, headers=h).status_code == 401
    r = login(c, environ=LOCAL, headers=tunnel(ip="192.0.2.77"))
    assert r.status_code == 429
    assert login(c, environ=LAN).status_code == 200        # the LAN is never refused by it


def test_ipv6_is_throttled_per_64(env, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(web_auth.throttle, "_clock", lambda: now[0])
    c = env["client"]
    for i in range(5):
        h = tunnel(ip=f"2001:db8:1:2::{i + 1}")
        assert login(c, pw="nope", environ=LOCAL, headers=h).status_code == 401
    assert login(c, environ=LOCAL, headers=tunnel(ip="2001:db8:1:2::99")).status_code == 429
    assert login(c, environ=LOCAL, headers=tunnel(ip="2001:db8:1:3::1")).status_code == 200


def test_card_failures_are_keyed_by_address_and_card(env, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(web_auth.throttle, "_clock", lambda: now[0])
    c = env["client"]
    for i in range(5):
        assert post(c, "/api/login/card", {"code": f"BAD-{i}"}).status_code == 401
    assert post(c, "/api/login/card", {"code": "CARD-0042"}).status_code == 429
    assert login(c).status_code == 200                     # the password path is its own key


def test_throttle_maps_are_capped():
    t = web_auth.LoginThrottle(clock=lambda: 5.0)
    t.MAX_KEYS = 50
    for i in range(120):
        ticket, _ = t.begin([(f"k{i}", 5)], proxied=False)
        t.finish(ticket, "fail")
    assert len(t._keys) <= 50


# ── break-glass ─────────────────────────────────────────────────────────────

def test_admin_break_glass_on_the_lan(env):
    set_admin(env)
    c = env["client"]
    assert post(c, "/api/login/admin", {"password": "wrong-one"}).status_code == 401
    r = post(c, "/api/login/admin", {"password": ADMIN_PW})
    assert r.status_code == 200
    assert r.get_json()["name"] == "Admin (break-glass)" and r.get_json()["method"] == "admin"
    assert get(c, "/api/session").get_json()["name"] == "Admin (break-glass)"


def test_admin_break_glass_is_refused_through_the_tunnel(env):
    set_admin(env)
    r = post(env["client"], "/api/login/admin", {"password": ADMIN_PW}, environ=LOCAL,
             headers=tunnel())
    assert r.status_code == 403
    assert store.web_sessions.list_active(db=env["db"]) == []


def test_admin_break_glass_before_setup(env):
    r = post(env["client"], "/api/login/admin", {"password": ADMIN_PW})
    assert r.status_code == 409 and r.get_json()["setup_url"] == "/admin/setup"


def test_changing_the_admin_password_revokes_admin_sessions(env):
    set_admin(env)
    c = env["client"]
    assert post(c, "/api/login/admin", {"password": ADMIN_PW}).status_code == 200
    other = env["app"].test_client()
    assert login(other).status_code == 200
    r = post(c, "/api/admin/password", {"password": ADMIN_PW, "new_password": "a-new-password"})
    assert r.status_code == 200 and r.get_json()["revoked_admin_sessions"] == 1
    assert get(c, "/api/session").status_code == 401
    assert get(other, "/api/session").status_code == 200


# ── sessions ────────────────────────────────────────────────────────────────

def test_logout_revokes_and_is_post_only(env):
    c = env["client"]
    login(c)
    assert get(c, "/api/logout").status_code == 405
    r = post(c, "/api/logout")
    assert r.status_code == 200 and "gc_session=;" in (cookie_of(r, "gc_session") or "")
    row = store.web_sessions.get(1, db=env["db"])
    assert row["revoked_at"]
    assert get(c, "/api/session").status_code == 401


def test_revoking_takes_effect_at_once_despite_the_cache(env):
    c = env["client"]
    login(c)
    assert get(c, "/api/session").status_code == 200       # now cached
    sid = store.web_sessions.list_active(db=env["db"])[0]["id"]
    web_auth.revoke(sid)
    assert get(c, "/api/session").status_code == 401


def test_revoke_all_for_a_name(env):
    a, b = env["app"].test_client(), env["app"].test_client()
    login(a)
    login(b, environ={"REMOTE_ADDR": "10.0.0.26"})
    assert len(web_auth.revoke_name("RYAN C")) == 2
    assert get(a, "/api/session").status_code == 401 and get(b, "/api/session").status_code == 401


def test_idle_and_absolute_expiry(env, monkeypatch):
    c = env["client"]
    login(c)
    base = web_auth._clock()
    monkeypatch.setattr(web_auth, "_clock", lambda: base + 11 * 3600)
    assert get(c, "/api/session").status_code == 200      # used: last_seen moves on
    web_auth.clear_cache()
    monkeypatch.setattr(web_auth, "_clock", lambda: base + 11 * 3600 + 12 * 3600 - 60)
    assert get(c, "/api/session").status_code == 200
    web_auth.clear_cache()
    monkeypatch.setattr(web_auth, "_clock", lambda: base + 14 * 86400 + 1)
    assert get(c, "/api/session").status_code == 401


def test_an_idle_session_ends_after_12_hours(env, monkeypatch):
    c = env["client"]
    login(c)
    base = web_auth._clock()
    web_auth.clear_cache()
    monkeypatch.setattr(web_auth, "_clock", lambda: base + 12 * 3600 + 5)
    assert get(c, "/api/session").status_code == 401


def test_last_seen_is_written_by_the_refresher_not_the_request(env, monkeypatch):
    c = env["client"]
    login(c)
    row = store.web_sessions.list_active(db=env["db"])[0]
    base = web_auth._clock()
    monkeypatch.setattr(web_auth, "_clock", lambda: base + 120)
    s = web_auth.lookup(None)
    assert s is None
    web_auth.note_seen({"id": row["id"]})
    assert store.web_sessions.get(row["id"], db=env["db"])["last_seen"] == row["last_seen"]
    assert web_auth.flush_seen() == 1
    assert store.web_sessions.get(row["id"], db=env["db"])["last_seen"] > row["last_seen"]


def test_a_store_error_is_503_not_401(env, monkeypatch):
    c = env["client"]
    login(c)
    web_auth.clear_cache()

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store.web_sessions, "get_by_hash", staticmethod(boom))
    assert get(c, "/api/thing").status_code == 503
    assert get(c, "/instruments").status_code == 503
    assert get(c, "/login").status_code == 200             # the login page still renders


def test_sign_in_answers_503_when_the_store_cannot_be_written(env, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(store.web_sessions, "add", staticmethod(boom))
    assert login(env["client"]).status_code == 503


def test_a_huge_or_unknown_cookie_is_just_signed_out(env):
    c = env["client"]
    c.set_cookie("gc_session", "x" * 5000)
    assert get(c, "/api/thing").status_code == 401
    c.set_cookie("gc_session", "made-up")
    assert get(c, "/api/thing").status_code == 401


# ── the tray's local paths and setup ────────────────────────────────────────

def test_tray_paths_are_open_only_when_local(env):
    c = env["client"]
    local = {"Host": "127.0.0.1:5560"}
    assert get(c, "/api/hub/status", environ=LOCAL, headers=local).status_code == 200
    assert post(c, "/api/admin/hub/stop", environ=LOCAL, headers=local).status_code == 202
    assert post(c, "/api/restart", environ=LOCAL, headers=local).status_code == 200
    # the LAN: status/restart need a session; the stop is refused regardless
    assert get(c, "/api/hub/status").status_code == 401
    assert post(c, "/api/restart").status_code == 401
    assert post(c, "/api/admin/hub/stop").status_code == 403
    login(c)
    assert get(c, "/api/hub/status").status_code == 200
    assert post(c, "/api/restart").status_code == 200
    assert post(c, "/api/admin/hub/stop").status_code == 403
    # the tunnel: never local, even signed in
    tok = store.web_sessions.list_active(db=env["db"])  # noqa: F841
    assert post(c, "/api/admin/hub/stop", environ=LOCAL, headers=tunnel()).status_code == 403


def test_setup_is_open_on_the_lan_until_a_password_is_set(env):
    c = env["client"]
    assert get(c, "/admin/setup").status_code == 200
    set_admin(env)
    assert get(c, "/admin/setup").status_code == 302


def test_setup_through_the_tunnel_needs_a_lablink_session(env):
    c = env["client"]
    r = c.get("/admin/setup", headers=tunnel(), environ_base=LOCAL)
    assert r.status_code == 302 and r.headers["Location"].startswith("/login")
    code = admin_auth.ensure_setup_code(db=env["db"])
    r = post(c, "/api/admin/setup", {"password": "tunnel-admin-pw", "setup_code": code},
             environ=LOCAL, headers=tunnel(Origin="https://gc.asaplabs.net"))
    assert r.status_code == 401 and not admin_auth.is_set(db=env["db"])
    # signed in through the tunnel, with the code: the Host gc.asaplabs.net is accepted
    ck = cookie_of(login(c, environ=LOCAL, headers=tunnel()), "__Host-gc_session")
    token = ck.split(";")[0].split("=", 1)[1]
    h = tunnel(Origin="https://gc.asaplabs.net", Cookie=f"__Host-gc_session={token}")
    assert c.get("/admin/setup", headers=h, environ_base=LOCAL).status_code == 200
    r = post(c, "/api/admin/setup", {"password": "tunnel-admin-pw", "setup_code": "wrong"},
             environ=LOCAL, headers=h)
    assert r.status_code == 403 and not admin_auth.is_set(db=env["db"])
    r = post(c, "/api/admin/setup", {"password": "tunnel-admin-pw", "setup_code": code},
             environ=LOCAL, headers=h)
    assert r.status_code == 201, r.get_json()
    assert admin_auth.is_set(db=env["db"])
    # once set, setup is closed everywhere
    r = post(c, "/api/admin/setup", {"password": "another-pw-1", "setup_code": code},
             environ=LOCAL, headers=h)
    assert r.status_code == 409


def test_a_foreign_host_is_refused_by_setup_even_signed_in(env):
    c = env["client"]
    ck = cookie_of(login(c, environ=LOCAL, headers=tunnel()), "__Host-gc_session")
    token = ck.split(";")[0].split("=", 1)[1]
    code = admin_auth.ensure_setup_code(db=env["db"])
    h = tunnel(Host="evil.example", Origin="https://evil.example",
               Cookie=f"__Host-gc_session={token}")
    bare = env["app"].test_client(use_cookies=False)
    r = post(bare, "/api/admin/setup", {"password": "tunnel-admin-pw", "setup_code": code},
             environ=LOCAL, headers=h)
    assert r.status_code == 403 and not admin_auth.is_set(db=env["db"])


def test_the_hub_url_host_is_not_accepted_on_plain_http(env):
    assert admin_auth.host_allowed("gc.asaplabs.net", db=env["db"]) is False
    assert admin_auth.host_allowed("gc.asaplabs.net", db=env["db"], tunnel_session=True) is True
    assert admin_auth.host_allowed("evil.example", db=env["db"], tunnel_session=True) is False
    assert admin_auth.host_allowed("localhost:5560", db=env["db"]) is True


# ── the sessions admin helpers ──────────────────────────────────────────────

def test_active_sessions_list_has_no_token_hashes(env):
    login(env["client"])
    rows = web_auth.active_sessions()
    assert len(rows) == 1 and rows[0]["name"] == "Ryan C"
    assert "token_hash" not in rows[0] and set(rows[0]) >= {"id", "method", "ip", "last_seen"}


def test_no_redirect_loop_when_the_proxy_names_no_scheme(env):
    """Only an explicit http from the proxy is redirected; with no scheme
    header the request is served as not-https (no loop), and sign-in through
    it is refused rather than setting a cookie without Secure."""
    c = env["client"]
    h = {"Host": "gc.asaplabs.net", "CF-Connecting-IP": "203.0.113.9", "CF-Ray": "x"}
    r = c.get("/instruments", headers=h, environ_base=LOCAL)
    assert r.status_code == 302 and r.headers["Location"].startswith("/login")
    r = post(c, "/api/login", {"username": "ryan c", "password": "labpass-1"}, environ=LOCAL,
             headers=h)
    assert r.status_code == 403 and env["stub"].requests == []
    assert r.get_json()["error"] == HTTPS_MESSAGE
    # what a browser sends: its Origin is https, which the hub (taking the
    # request for http) would otherwise call cross-site
    for path in ("/api/login", "/api/login/card"):
        r = post(c, path, {"username": "ryan c", "password": "labpass-1", "code": "CARD-1"},
                 environ=LOCAL, headers=dict(h, Origin="https://gc.asaplabs.net",
                                             **{"Sec-Fetch-Site": "same-origin"}))
        assert r.status_code == 403 and r.get_json()["error"] == HTTPS_MESSAGE, path
    assert env["stub"].requests == []
    ok = dict(h, **{"CF-Visitor": '{"scheme":"https"}'})
    r = post(c, "/api/login", {"username": "ryan c", "password": "labpass-1"}, environ=LOCAL,
             headers=ok)
    assert r.status_code == 200 and cookie_of(r, "__Host-gc_session")
