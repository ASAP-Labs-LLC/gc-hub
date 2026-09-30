"""Regression (v3.1.0): first-use setup and password change accept passwords
with any characters, over the LAN and through the Cloudflare tunnel.

The owner's first setup through gc.asaplabs.net failed with "Unexpected
token '<'" and his password had special characters. This walks every
character that could plausibly upset a JSON body, an HTML form, a proxy or a
WAF (``" ' < > & % \\ / ; --``, ``é``, ``💥``, a space, a tab) and a 128-char
password through the real app booted in a subprocess: setup (reset between
passwords the way DEPLOY.md resets a forgotten one: delete the
``settings_kv`` row, open the setup page for a new code), then a change, each
answered in JSON; the stored hash matches; app.log has the trail and never a
password.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")

import admin_auth  # noqa: E402
import store  # noqa: E402
from bootapp import booted, cookie_header, wait_for  # noqa: E402
from labcore_stub import LabCoreStub  # noqa: E402
from test_tunnel_boot import _cookie, call  # noqa: E402

SPECIALS = ['"', "'", "<", ">", "&", "%", "\\", "/", ";", "--", "é", "💥", " ", "\t"]
LONG = ("Aa1" + "".join(SPECIALS) + "x" * 128)[:128]
PASSWORDS = [f"pw{ch}Setup{ch}9" for ch in SPECIALS] + [LONG]


def test_the_password_list_is_what_the_regression_promises():
    assert len(LONG) == 128
    for ch in SPECIALS:
        assert any(ch in pw for pw in PASSWORDS[:-1])


@pytest.fixture(scope="module")
def hub():
    tmp = Path(tempfile.mkdtemp(prefix="gc-pwchars-"))
    stub = LabCoreStub()
    try:
        with booted(tmp, extra_env={"LABCORE_URL": stub.url}) as (port, _proc, data, _home):
            db = data / store.DB_FILENAME
            assert wait_for(lambda: db.is_file()
                            and store.instruments.get("gc1", db=db) is not None, timeout=30)
            yield port, data, db
    finally:
        stub.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _reset(db):
    store.settings_kv.delete(admin_auth.KEY, db=db)
    admin_auth.reset_throttle()


def _stored_matches(db, pw) -> bool:
    return admin_auth._matches(pw, store.settings_kv.get(admin_auth.KEY, db=db))


def _code(data) -> str:
    return (data / admin_auth.SETUP_CODE_FILE).read_text(encoding="utf-8").strip()


def _set_and_change(port, data, db, pw, *, tunnel, cookie):
    _reset(db)
    code, _, page = call(port, "GET", "/admin/setup", cookie=cookie, tunnel=tunnel, raw=True)
    assert code == 200 and b"Setup code" in page, (code, page[:200])
    code, h, body = call(port, "POST", "/api/admin/setup",
                         {"password": pw, "setup_code": _code(data)}, cookie=cookie,
                         tunnel=tunnel)
    assert h["content-type"].startswith("application/json")
    assert code == 201, (repr(pw), body)
    assert _stored_matches(db, pw)
    new = pw + "2"
    code, h, body = call(port, "POST", "/api/admin/password",
                         {"password": pw, "new_password": new}, cookie=cookie, tunnel=tunnel)
    assert h["content-type"].startswith("application/json")
    assert code == 200 and body["ok"] is True, (repr(pw), body)
    assert _stored_matches(db, new) and not _stored_matches(db, pw)


def test_setup_and_change_over_the_lan(hub):
    port, data, db = hub
    cookie = cookie_header(port)["Cookie"]
    for pw in PASSWORDS:
        _set_and_change(port, data, db, pw, tunnel=False, cookie=cookie)
    log = (data / "app.log").read_text(encoding="utf-8")
    assert "admin setup (/api/admin/setup) from 127.0.0.1" in log
    assert "HTTP 201, ok: password set" in log and "HTTP 200, ok: password changed" in log
    for pw in PASSWORDS:
        assert pw not in log and (pw + "2") not in log


def test_setup_and_change_through_the_tunnel_with_a_session(hub):
    port, data, db = hub
    code, h, body = call(port, "POST", "/api/login", {"username": "ryan c",
                                                      "password": "labpass-1"})
    assert code == 200, body
    cookie = _cookie(h)
    for pw in PASSWORDS:
        _set_and_change(port, data, db, pw, tunnel=True, cookie=cookie)
    log = (data / "app.log").read_text(encoding="utf-8")
    assert "from 203.0.113.9 via Cloudflare, https: yes, Host 'gc.asaplabs.net': HTTP 201" in log
    for pw in PASSWORDS:
        assert pw not in log and (pw + "2") not in log
