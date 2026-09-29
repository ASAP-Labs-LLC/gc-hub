"""Setup-path diagnosis (v3.1.0): ``POST /api/admin/setup`` and
``/api/admin/password`` leave one INFO line in app.log per attempt — the
outcome, which check refused, and the password's character *classes*, never
the password or the setup code. So a failed first setup (the owner's
"Unexpected token '<'") leaves a trail, or proves the request never reached
the hub.

In process on test_web_auth's bare app (https redirect → cross-site guard →
gate, as app.py wires them).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")

import admin_auth  # noqa: E402
import netctx  # noqa: E402
import web_auth  # noqa: E402
from labcore_stub import LabCoreStub  # noqa: E402
from test_web_auth import LAN, LOCAL, cookie_of, login, make_app, post, tunnel  # noqa: E402

SECRET_PW = "Zq9!<x>&'\"é 💥-pw"


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
        yield {"app": app, "client": app.test_client(), "db": db}
        web_auth.reset()
        admin_auth.reset_throttle()


def trail(caplog, path="/api/admin/setup"):
    lines = [r for r in caplog.records if r.name == "admin_auth" and r.levelno == logging.INFO
             and path in r.getMessage()]
    assert lines, caplog.text
    return lines[-1].getMessage()


# ── the character classes ───────────────────────────────────────────────────

def test_password_classes_describe_without_revealing():
    s = admin_auth.password_classes("Abcdefgh12345!")
    assert s.startswith("len 14,")
    assert "non-ASCII: no" in s and "lower: yes" in s and "upper: yes" in s
    assert "digit: yes" in s and "symbol: yes" in s and "whitespace: no" in s
    assert "Abcdefgh" not in s
    s = admin_auth.password_classes("pa ss\twørd💥")
    assert "len 11" in s and "whitespace: yes" in s and "non-ASCII: yes" in s
    assert "emoji/astral: yes" in s and "control: yes" in s     # the tab
    assert admin_auth.password_classes("\ud83d-lone-surrogate").count("unpaired surrogate: yes") == 1
    assert admin_auth.password_classes(None) == "missing"
    assert admin_auth.password_classes(12345678) == "not text (int)"


# ── setup ───────────────────────────────────────────────────────────────────

def test_setup_logs_the_outcome_and_classes_never_the_secrets(env, caplog):
    c = env["client"]
    code = admin_auth.ensure_setup_code(db=env["db"])
    with caplog.at_level(logging.INFO):
        r = post(c, "/api/admin/setup", {"password": SECRET_PW, "setup_code": "wrong-code"})
        assert r.status_code == 403
        line = trail(caplog)
        assert "HTTP 403" in line and "setup code" in line
        assert "10.0.0.25" in line and "len 16" in line and "non-ASCII: yes" in line

        r = post(c, "/api/admin/setup", {"password": "short", "setup_code": code})
        assert r.status_code == 400
        line = trail(caplog)
        assert "HTTP 400" in line and "password rule" in line and "len 5" in line

        r = post(c, "/api/admin/setup", {"password": SECRET_PW, "setup_code": code})
        assert r.status_code == 201
        line = trail(caplog)
        assert "HTTP 201" in line and "ok" in line

        assert login(c).status_code == 200      # once set, setup needs a session
        r = post(c, "/api/admin/setup", {"password": SECRET_PW, "setup_code": code})
        assert r.status_code == 409 and "already set" in trail(caplog)
    assert SECRET_PW not in caplog.text and code not in caplog.text
    assert "wrong-code" not in caplog.text


def test_setup_refused_before_the_route_still_leaves_a_line(env, caplog):
    c = env["client"]
    code = admin_auth.ensure_setup_code(db=env["db"])
    with caplog.at_level(logging.INFO):
        # another site's page: the cross-site guard
        r = post(c, "/api/admin/setup", {"password": SECRET_PW, "setup_code": code},
                 headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        line = trail(caplog)
        assert "HTTP 403" in line and "Cross-site request refused" in line
        assert "before the route" in line
        # through the tunnel without a session: the gate
        r = post(c, "/api/admin/setup", {"password": SECRET_PW, "setup_code": code},
                 environ=LOCAL, headers=tunnel(Origin="https://gc.asaplabs.net"))
        assert r.status_code == 401
        line = trail(caplog)
        assert "HTTP 401" in line and "via Cloudflare" in line and "203.0.113.9" in line
        # not JSON
        r = c.post("/api/admin/setup", data="password=x", environ_base=LAN,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 415 and "request body" in trail(caplog)
    assert SECRET_PW not in caplog.text and code not in caplog.text


def test_setup_through_the_tunnel_logs_https_and_host(env, caplog):
    c = env["client"]
    ck = cookie_of(login(c, environ=LOCAL, headers=tunnel()), "__Host-gc_session")
    token = ck.split(";")[0].split("=", 1)[1]
    code = admin_auth.ensure_setup_code(db=env["db"])
    h = tunnel(Host="evil.example", Origin="https://evil.example",
               Cookie=f"__Host-gc_session={token}")
    bare = env["app"].test_client(use_cookies=False)
    with caplog.at_level(logging.INFO):
        r = post(bare, "/api/admin/setup", {"password": SECRET_PW, "setup_code": code},
                 environ=LOCAL, headers=h)
        assert r.status_code == 403
        line = trail(caplog)
        assert "host" in line and "https: yes" in line


# ── password change ─────────────────────────────────────────────────────────

def test_password_change_logs_the_outcome_never_the_passwords(env, caplog):
    admin_auth.setup("first-password-1", admin_auth.ensure_setup_code(db=env["db"]),
                     db=env["db"])
    c = env["client"]
    assert login(c).status_code == 200
    with caplog.at_level(logging.INFO):
        r = post(c, "/api/admin/password", {"password": "not-the-password",
                                            "new_password": SECRET_PW})
        assert r.status_code == 403
        line = trail(caplog, "/api/admin/password")
        assert "HTTP 403" in line and "current password" in line and "len 16" in line
        r = post(c, "/api/admin/password", {"password": "first-password-1", "new_password": "x"})
        assert r.status_code == 400 and "password rule" in trail(caplog, "/api/admin/password")
        r = post(c, "/api/admin/password", {"password": "first-password-1",
                                            "new_password": SECRET_PW})
        assert r.status_code == 200
        line = trail(caplog, "/api/admin/password")
        assert "HTTP 200" in line and "ok" in line
    for secret in (SECRET_PW, "first-password-1", "not-the-password"):
        assert secret not in caplog.text
