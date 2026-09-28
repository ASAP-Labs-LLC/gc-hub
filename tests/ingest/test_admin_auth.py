"""2B1 T2: the admin password (D13): salted PBKDF2 in ``settings_kv``,
first-use setup, constant-time check, in-memory backoff."""
from __future__ import annotations

import ast
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


def test_not_set_on_a_new_store(db):
    assert admin_auth.is_set(db=db) is False
    res = admin_auth.check("anything", db=db)
    assert (res.ok, res.reason) == (False, "not-set")
    assert "/admin/setup" in res.message


def test_setup_stores_a_salted_pbkdf2_hash(db, tmp_path):
    admin_auth.setup("correct horse", db=db)
    stored = store.settings_kv.get(admin_auth.KEY, db=db)
    algo, iters, salt, digest = stored.split("$")
    assert algo == "pbkdf2_sha256"
    assert int(iters) >= 200_000
    assert len(bytes.fromhex(salt)) >= 16
    assert len(bytes.fromhex(digest)) == 32
    assert "correct horse" not in stored
    other = tmp_path / "other.db"
    store.migrate(other)
    admin_auth.setup("correct horse", db=other)
    assert store.settings_kv.get(admin_auth.KEY, db=other).split("$")[2] != salt   # per-install salt


def test_check_right_and_wrong(db):
    admin_auth.setup("correct horse", db=db)
    assert admin_auth.is_set(db=db)
    assert admin_auth.check("correct horse", db=db).ok
    wrong = admin_auth.check("admin", db=db)
    assert (wrong.ok, wrong.reason) == (False, "wrong")
    for bad in (None, 12345, b"correct horse", ["x"]):
        assert admin_auth.check(bad, db=db).ok is False


def test_check_admin_body_shape(db):
    admin_auth.setup("correct horse", db=db)
    assert admin_auth.check_admin_body({"password": "correct horse"}, db=db) is True
    assert admin_auth.check_admin_body({"password": "nope"}, db=db) is False
    assert admin_auth.check_admin_body(None, db=db) is False
    assert admin_auth.check_admin_body("correct horse", db=db) is False


def test_setup_refuses_short_and_non_str(db):
    with pytest.raises(admin_auth.PasswordError):
        admin_auth.setup("short", db=db)
    with pytest.raises(admin_auth.PasswordError):
        admin_auth.setup(None, db=db)
    assert admin_auth.is_set(db=db) is False


def test_setup_only_once(db):
    admin_auth.setup("correct horse", db=db)
    with pytest.raises(admin_auth.AlreadySet):
        admin_auth.setup("another one", db=db)
    assert admin_auth.check("correct horse", db=db).ok


def test_change_needs_the_current_password(db):
    admin_auth.setup("correct horse", db=db)
    with pytest.raises(admin_auth.PasswordError):
        admin_auth.change("wrong one", "battery staple", db=db)
    admin_auth.change("correct horse", "battery staple", db=db)
    assert admin_auth.check("battery staple", db=db).ok
    assert not admin_auth.check("correct horse", db=db).ok


def test_iterations_are_read_back_from_the_stored_hash(db, monkeypatch):
    admin_auth.setup("correct horse", db=db)
    monkeypatch.setattr(admin_auth, "ITERATIONS", 999_999)
    assert admin_auth.check("correct horse", db=db).ok


def test_backoff_after_repeated_failures(db, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(admin_auth, "_clock", clock)
    admin_auth.setup("correct horse", db=db)
    for _ in range(admin_auth.FREE_ATTEMPTS):
        assert admin_auth.check("wrong", client="10.0.0.9", db=db).reason == "wrong"
    res = admin_auth.check("correct horse", client="10.0.0.9", db=db)   # even the right one
    assert (res.ok, res.reason) == (False, "throttled")
    assert res.retry_after > 0 and "try again" in res.message.lower()
    # another client is not affected
    assert admin_auth.check("correct horse", client="10.0.0.8", db=db).ok
    clock.t += res.retry_after + 0.01
    assert admin_auth.check("correct horse", client="10.0.0.9", db=db).ok
    # a success resets the count
    assert admin_auth.check("wrong", client="10.0.0.9", db=db).reason == "wrong"


def test_backoff_grows_and_is_capped(db, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(admin_auth, "_clock", clock)
    admin_auth.setup("correct horse", db=db)
    delays = []
    for _ in range(admin_auth.FREE_ATTEMPTS + 12):
        res = admin_auth.check("wrong", client="c", db=db)
        if res.reason == "throttled":
            clock.t += res.retry_after + 0.01
            res = admin_auth.check("wrong", client="c", db=db)
            assert res.reason == "wrong"
        delays.append(admin_auth._throttle.retry_after("c"))
    positive = [d for d in delays if d > 0]
    assert positive == sorted(positive)
    assert max(positive) <= admin_auth.MAX_BACKOFF_SECONDS


def test_uses_constant_time_compare():
    src = (ROOT / "admin_auth.py").read_text(encoding="utf-8")
    assert "compare_digest" in src
    assert "pbkdf2_hmac" in src
    tree = ast.parse(src)
    for node in ast.walk(tree):     # no plain == on the digest
        if isinstance(node, ast.Compare) and any(isinstance(o, (ast.Eq, ast.NotEq)) for o in node.ops):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert not ({"digest", "expected", "derived"} & names), ast.unparse(node)


def test_no_store_means_not_set(monkeypatch):
    monkeypatch.delenv("GC_DATA_DIR", raising=False)
    res = admin_auth.check("whatever")
    assert res.ok is False and res.reason == "no-store"
