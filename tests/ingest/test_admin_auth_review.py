"""Security review 1 of 2B1, module level: C1 (one-time setup code, Host
check), I1 (parallel guesses, CPU), M8 (atomic change), M10 (403 hook)."""
from __future__ import annotations

import logging
import os
import socket
import stat
import threading
import time
from pathlib import Path

import pytest

import admin_auth
import store

ROOT = Path(__file__).resolve().parents[2]


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


def _setup(db, pw):
    admin_auth.setup(pw, admin_auth.ensure_setup_code(db=db), db=db)


# ── C1: the one-time setup code ─────────────────────────────────────────────

def test_setup_code_file_is_private_and_logged(db, caplog):
    caplog.set_level(logging.WARNING, logger="admin_auth")
    code = admin_auth.ensure_setup_code(db=db)
    f = db.parent / admin_auth.SETUP_CODE_FILE
    assert admin_auth.SETUP_CODE_FILE == "admin-setup-code.txt"
    assert f.read_text(encoding="utf-8").strip() == code
    assert len(code) >= 12
    if os.name == "posix":
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert code in caplog.text and "admin-setup-code.txt" in caplog.text
    assert admin_auth.ensure_setup_code(db=db) == code          # stable until used


def test_setup_requires_the_code(db):
    code = admin_auth.ensure_setup_code(db=db)
    for bad in (None, "", "wrong-code", code + "x", 123):
        with pytest.raises(admin_auth.SetupCodeError):
            admin_auth.setup("correct horse", bad, db=db)
    assert not admin_auth.is_set(db=db)
    admin_auth.reset_throttle()
    admin_auth.setup("correct horse", code, db=db)
    assert admin_auth.is_set(db=db)
    assert not (db.parent / admin_auth.SETUP_CODE_FILE).exists()   # deleted on success
    assert admin_auth.ensure_setup_code(db=db) is None


def test_setup_code_compared_in_constant_time():
    src = (ROOT / "admin_auth.py").read_text(encoding="utf-8")
    start = src.index("\ndef setup(")
    fn = src[start:src.index("\ndef ", start + 1)]
    assert "compare_digest" in fn


def test_reset_regenerates_the_code(db):
    old = admin_auth.ensure_setup_code(db=db)
    admin_auth.setup("correct horse", old, db=db)
    store.settings_kv.delete(admin_auth.KEY, db=db)                # the documented reset
    new = admin_auth.ensure_setup_code(db=db)
    assert new and new != old
    with pytest.raises(admin_auth.SetupCodeError):
        admin_auth.setup("x" * 10, old, db=db)
    admin_auth.setup("x" * 10, new, db=db)


def test_wrong_setup_codes_are_throttled(db):
    admin_auth.ensure_setup_code(db=db)
    for _ in range(admin_auth.FREE_ATTEMPTS + 1):
        with pytest.raises(admin_auth.SetupCodeError):
            admin_auth.setup("correct horse", "guess", db=db, client="9.9.9.9")
    code = admin_auth.ensure_setup_code(db=db)
    with pytest.raises(admin_auth.SetupCodeError, match="(?i)try again"):
        admin_auth.setup("correct horse", code, db=db, client="9.9.9.9")
    assert not admin_auth.is_set(db=db)


def test_host_allowed(db):
    ok = ["127.0.0.1:5560", "localhost", "LOCALHOST:80", "[::1]:5560", "192.168.1.5:5560",
          "10.0.0.7", socket.gethostname(), socket.gethostname().split(".")[0] + ":5560"]
    for h in ok:
        assert admin_auth.host_allowed(h, db=db), h
    for h in ("evil.example", "evil.example:5560", "asapsv1.attacker.net", "", None, "a b"):
        assert not admin_auth.host_allowed(h, db=db), h
    store.settings_kv.set("hub_url", "http://asapsv1.lab:5560", db=db)
    # rev 2 (amendment 12): the hub_url host only for setup through the tunnel
    # (https and a signed-in session)
    assert not admin_auth.host_allowed("asapsv1.lab:5560", db=db)
    assert admin_auth.host_allowed("asapsv1.lab:5560", db=db, tunnel_session=True)
    assert not admin_auth.host_allowed("evil.example", db=db, tunnel_session=True)


# ── I1: parallel guesses and CPU ────────────────────────────────────────────

def test_concurrent_burst_from_one_client_hashes_at_most_free_attempts(db, monkeypatch):
    _setup(db, "correct horse")
    admin_auth.reset_throttle()
    calls = []
    real = admin_auth._hash

    def slow(*a):
        calls.append(1)
        time.sleep(0.05)
        return real(*a)
    monkeypatch.setattr(admin_auth, "_hash", slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        admin_auth.check("wrong", client="6.6.6.6", db=db).reason)) for _ in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) <= admin_auth.FREE_ATTEMPTS, (len(calls), results)
    assert results.count("wrong") <= admin_auth.FREE_ATTEMPTS
    assert set(results) <= {"wrong", "throttled"}


def test_at_most_two_hashes_run_at_once(db, monkeypatch):
    _setup(db, "correct horse")
    admin_auth.reset_throttle()
    live, peak, lock = [0], [0], threading.Lock()
    real = admin_auth._hash

    def slow(*a):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        try:
            return real(*a)
        finally:
            with lock:
                live[0] -= 1
    monkeypatch.setattr(admin_auth, "_hash", slow)
    threads = [threading.Thread(target=admin_auth.check, args=("wrong", f"10.1.1.{i}"),
                                kwargs={"db": db}) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert 1 <= peak[0] <= 2


def test_hub_wide_failure_budget(db, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(admin_auth, "_clock", clock)
    monkeypatch.setattr(admin_auth, "ITERATIONS", 1000)      # speed only
    _setup(db, "correct horse")
    admin_auth.reset_throttle()
    for i in range(admin_auth.GLOBAL_FAILURE_BUDGET):
        assert admin_auth.check("wrong", client=f"172.16.{i // 200}.{i % 200}", db=db).reason == "wrong"
    res = admin_auth.check("correct horse", client="172.17.0.1", db=db)   # a fresh client
    assert (res.ok, res.reason) == (False, "throttled")
    clock.t += admin_auth.GLOBAL_WINDOW_SECONDS + 1
    assert admin_auth.check("correct horse", client="172.17.0.1", db=db).ok


def test_success_refunds_the_reservation(db):
    _setup(db, "correct horse")
    admin_auth.reset_throttle()
    for _ in range(admin_auth.FREE_ATTEMPTS * 3):
        assert admin_auth.check("correct horse", client="1.2.3.4", db=db).ok


# ── M8: change is atomic ────────────────────────────────────────────────────

def test_change_refuses_if_the_password_changed_meanwhile(db, monkeypatch):
    _setup(db, "correct horse")
    real = admin_auth._matches

    def racing(pw, stored):
        ok = real(pw, stored)
        store.settings_kv.set(admin_auth.KEY, admin_auth._encode("someone else!"), db=db)
        return ok
    monkeypatch.setattr(admin_auth, "_matches", racing)
    with pytest.raises(admin_auth.PasswordError):
        admin_auth.change("correct horse", "battery staple", db=db)
    monkeypatch.setattr(admin_auth, "_matches", real)
    assert admin_auth.check("someone else!", db=db).ok


# ── M10: the 403 hook keeps the route's keys ───────────────────────────────

def test_refusal_hook_only_overrides_error(db, monkeypatch):
    from flask import Flask, jsonify
    monkeypatch.setattr(admin_auth, "hub_db", lambda: db)
    app = Flask(__name__)
    app.register_blueprint(admin_auth.bp)

    @app.route("/gated", methods=["POST"])
    def gated():
        ok = admin_auth.check_admin_body({"password": "x"})
        return (jsonify({"ok": True}), 200) if ok else (jsonify({"error": "Incorrect password",
                                                                  "field": "password"}), 403)
    r = app.test_client().post("/gated", json={})
    assert r.status_code == 403
    body = r.get_json()
    assert body["field"] == "password"
    assert "/admin/setup" in body["error"]
