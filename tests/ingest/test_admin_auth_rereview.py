"""Security re-review of 2B1: G1 (loopback is exempt from the hub-wide
budget), G2 (deeply nested JSON is a 400), G3 (unpaired surrogates are
invalid, never a 500)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import admin_auth
import store

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "gc.db"
    store.migrate(p)
    admin_auth.reset_throttle()
    yield p
    admin_auth.reset_throttle()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _exhaust_budget():
    for i in range(admin_auth.GLOBAL_FAILURE_BUDGET):
        assert admin_auth.check("wrong", client=f"172.16.{i // 200}.{i % 200}").reason == "wrong"


# ── G1 ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("loopback", ["127.0.0.1", "::1", "127.0.0.5"])
def test_loopback_still_authenticates_when_the_budget_is_spent(db, monkeypatch, loopback):
    monkeypatch.setattr(admin_auth, "_clock", Clock())
    monkeypatch.setattr(admin_auth, "ITERATIONS", 1000)
    monkeypatch.setattr(admin_auth, "hub_db", lambda: db)
    admin_auth.setup("correct horse", admin_auth.ensure_setup_code(db=db), db=db)
    admin_auth.reset_throttle()
    _exhaust_budget()
    assert admin_auth.check("correct horse", client="10.9.9.9", db=db).reason == "throttled"
    assert admin_auth.check("correct horse", client=loopback, db=db).ok
    # loopback's own wrong guesses don't spend the hub-wide budget either
    for _ in range(3):
        assert admin_auth.check("nope", client=loopback, db=db).reason == "wrong"


def test_loopback_setup_code_allowed_when_the_budget_is_spent(db, monkeypatch):
    monkeypatch.setattr(admin_auth, "_clock", Clock())
    monkeypatch.setattr(admin_auth, "ITERATIONS", 1000)
    code = admin_auth.ensure_setup_code(db=db)
    for i in range(admin_auth.GLOBAL_FAILURE_BUDGET):
        with pytest.raises(admin_auth.SetupCodeError):
            admin_auth.setup("correct horse", "guess", db=db, client=f"172.16.0.{i}")
    with pytest.raises(admin_auth.SetupCodeError, match="(?i)try again"):
        admin_auth.setup("correct horse", code, db=db, client="10.9.9.9")
    admin_auth.setup("correct horse", code, db=db, client="127.0.0.1")
    assert admin_auth.is_set(db=db)


# ── G3 ──────────────────────────────────────────────────────────────────────

def test_unpaired_surrogates_are_invalid_not_errors(db):
    bad = "abc\ud800defgh"
    with pytest.raises(admin_auth.PasswordError):
        admin_auth.setup(bad, admin_auth.ensure_setup_code(db=db), db=db)
    code = admin_auth.ensure_setup_code(db=db)
    with pytest.raises(admin_auth.SetupCodeError):
        admin_auth.setup("correct horse", code[:-1] + "\udfff", db=db)
    admin_auth.reset_throttle()
    admin_auth.setup("correct horse", code, db=db)
    res = admin_auth.check(bad, db=db)
    assert (res.ok, res.reason) == (False, "wrong")
    assert admin_auth.check_admin_body({"password": bad}, db=db) is False
    with pytest.raises(admin_auth.PasswordError):
        admin_auth.change("correct horse", bad, db=db)


# ── G2 / G3 over HTTP ───────────────────────────────────────────────────────

def test_nested_json_and_surrogates_over_http(tmp_path):
    pytest.importorskip("flask")
    from bootapp import booted, send, setup_admin
    nested = b'{"password": ' + b"[" * 20000 + b"]" * 20000 + b"}"
    assert len(nested) < 64 * 1024
    surrogate = b'{"password": "abc\\ud800defgh", "setup_code": "x\\udfff", "params": {}}'
    js = {"Content-Type": "application/json"}
    with booted(tmp_path) as (port, _proc, data, _home):
        for path in ("/api/admin/setup", "/api/admin/password"):
            code, body = send(port, path, nested, js)
            assert code == 400, (path, code, body)
        code, body = send(port, "/api/admin/setup", surrogate, js)
        assert code in (400, 403), (code, body)
        setup_admin(port, data)
        for path in ("/api/save-analysis-defaults", "/api/admin/hub-url",
                     "/api/admin/instruments/gc1/revoke-token"):
            code, body = send(port, path, nested, js)
            assert code == 400, (path, code, body)
            code, body = send(port, path, surrogate, js)
            assert code == 403, (path, code, body)
        code, body = send(port, "/api/admin/password",
                          json.dumps({"password": "test-admin-pw"}).encode()[:-1]
                          + b', "new_password": "abc\\ud800defgh"}', js)
        assert code == 400, (code, body)
