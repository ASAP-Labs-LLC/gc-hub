"""Schema v3 (sign-in, spec D2/D6 rev 2): ``web_sessions`` and the
attribution columns ``sample_comments.author_name``/``deleted_by_name`` and
``report_log.user_name``. One additive step (``user_version`` 2 → 3); the v2
step is untouched, and v2.0.0's own store.py (frozen in ``tests/store/v2_0_0``)
still starts on a v3 database.

No ``__init__.py`` here (see test_store.py).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import store

HERE = Path(__file__).resolve().parent
V200 = HERE / "v2_0_0"

SESSION_COLUMNS = {"id", "token_hash", "name", "method", "created_at", "last_seen", "ip",
                   "user_agent", "revoked_at", "expires_at"}


def _cols(conn, table) -> set:
    return {r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _v2_db(path: Path) -> Path:
    """A schema-v2 database: today's first two steps only."""
    conn = store.open_db(path, create=True)
    try:
        for version, step in enumerate(store.MIGRATIONS[:2]):
            with store.write_txn(conn):
                for stmt in step:
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {version + 1}")
    finally:
        conn.close()
    return path


def test_schema_version_is_3():
    assert store.SCHEMA_VERSION == 3 == len(store.MIGRATIONS)


def test_v2_to_v3_is_additive(tmp_path):
    db = _v2_db(tmp_path / "gc.db")
    with store.connection(db) as conn:
        conn.execute("INSERT INTO instruments(id, name) VALUES ('gc1', 'GC-1')")
        conn.execute("INSERT INTO samples(instrument_id, lab_id, injection_dt, "
                     "injection_dt_source, status, received_at) VALUES "
                     "('gc1', '1', '2026-09-25 00:00:00', 'cdf', 'received', 'x')")
        conn.execute("INSERT INTO sample_comments(sample_id, text, source, author_initials, "
                     "created_at) VALUES (1, 'kept', 'free', 'RB', 'x')")
        before = [tuple(r) for r in conn.execute("SELECT * FROM sample_comments")]
    assert store.migrate(db) == 3
    with store.connection(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert SESSION_COLUMNS <= _cols(conn, "web_sessions")
        assert {"author_name", "deleted_by_name"} <= _cols(conn, "sample_comments")
        assert "user_name" in _cols(conn, "report_log")
        after = [tuple(r)[:len(before[0])] for r in conn.execute("SELECT * FROM sample_comments")]
        assert after == before
        row = conn.execute("SELECT author_name, deleted_by_name FROM sample_comments").fetchone()
        assert tuple(row) == (None, None)
    backups = list((tmp_path / "backups").glob("pre-migrate-2-*.db"))
    assert len(backups) == 1


def test_the_v2_step_is_unchanged():
    """Rev 2 amendment 15: one new step; the v2 step is not edited."""
    v2 = "\n".join(store.MIGRATIONS[1])
    assert "web_sessions" not in v2 and "author_name" not in v2 and "user_name" not in v2
    v3 = "\n".join(store.MIGRATIONS[2])
    assert "CREATE TABLE web_sessions" in v3
    for stmt in store.MIGRATIONS[2]:
        assert stmt.lstrip().upper().startswith(("CREATE TABLE", "CREATE INDEX",
                                                 "CREATE UNIQUE INDEX", "ALTER TABLE"))
        if stmt.lstrip().upper().startswith("ALTER TABLE"):
            assert "ADD COLUMN" in stmt.upper() and "NOT NULL" not in stmt.upper()


def test_token_hash_is_unique_and_nullable(tmp_path):
    db = tmp_path / "gc.db"
    store.migrate(db)
    now = store.now_iso()
    store.web_sessions.add("h1", name="Ryan C", method="password", ip="10.0.0.5",
                           user_agent="UA", expires_at=now, db=db)
    with pytest.raises(sqlite3.IntegrityError):
        store.web_sessions.add("h1", name="X", method="card", ip=None, user_agent=None,
                               expires_at=now, db=db)
    with store.connection(db) as conn:     # the diagnostics copy nulls it (D7)
        conn.execute("UPDATE web_sessions SET token_hash = NULL")
        assert conn.execute("SELECT COUNT(*) FROM web_sessions WHERE token_hash IS NULL"
                            ).fetchone()[0] == 1


def test_v2_0_0_store_starts_on_a_v3_database(tmp_path):
    db = tmp_path / "gc.db"
    store.migrate(db)
    later = _iso(datetime.now(timezone.utc) + timedelta(days=1))
    store.web_sessions.add("h", name="Ryan C", method="password", ip="10.0.0.5",
                           user_agent="UA", expires_at=later, db=db)
    old = tmp_path / "old"
    shutil.copytree(V200, old)
    script = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {str(old)!r})
        import store
        assert store.__file__.startswith({str(old)!r}), store.__file__
        v = store.migrate({str(db)!r})
        store.instruments.upsert({{"id": "gc1", "name": "GC-1"}}, db={str(db)!r})
        sid = store.samples.insert_received("gc1", "40306", "2026-09-25 01:00:00", "cdf",
                                            cdf_sha256="sha-new", cdf_path="cdf/x.CDF",
                                            method_name="SIMDISB.M", db={str(db)!r})
        print(json.dumps({{"version": v, "sample": sid, "schema": store.SCHEMA_VERSION}}))
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=tmp_path, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["schema"] == 1 and got["version"] == 3
    assert [s["name"] for s in store.web_sessions.list_active(db=db)] == ["Ryan C"]
    assert store.migrate(db) == 3


# ── the helpers ─────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path) -> Path:
    path = tmp_path / "gc.db"
    store.migrate(path)
    return path


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def test_web_sessions_helpers(db):
    now = datetime.now(timezone.utc)
    exp = _iso(now + timedelta(days=14))
    a = store.web_sessions.add("ha", name="Ryan C", method="password", ip="10.0.0.5",
                               user_agent="x" * 500, expires_at=exp, db=db)
    b = store.web_sessions.add("hb", name="Ryan C", method="card", ip="10.0.0.6",
                               user_agent=None, expires_at=exp, db=db)
    c = store.web_sessions.add("hc", name="Admin (break-glass)", method="admin", ip="127.0.0.1",
                               user_agent=None, expires_at=exp, db=db)
    row = store.web_sessions.get_by_hash("ha", db=db)
    assert row["id"] == a and row["name"] == "Ryan C" and len(row["user_agent"]) == 200
    assert row["last_seen"] == row["created_at"] and row["revoked_at"] is None
    assert store.web_sessions.get_by_hash("nope", db=db) is None

    later = _iso(now + timedelta(minutes=5))
    store.web_sessions.touch_many({a: later, b: later}, db=db)
    assert store.web_sessions.get(a, db=db)["last_seen"] == later

    assert store.web_sessions.revoke(a, db=db) is True
    assert store.web_sessions.revoke(a, db=db) is False
    assert store.web_sessions.get(a, db=db)["revoked_at"]
    assert store.web_sessions.revoke_method("admin", db=db) == [c]
    assert store.web_sessions.revoke_name("ryan c", db=db) == [b]
    assert store.web_sessions.list_active(db=db) == []


def test_list_active_skips_expired_and_idle(db):
    now = datetime.now(timezone.utc)
    store.web_sessions.add("live", name="A", method="password", ip=None, user_agent=None,
                           expires_at=_iso(now + timedelta(days=1)), db=db)
    store.web_sessions.add("over", name="B", method="password", ip=None, user_agent=None,
                           expires_at=_iso(now - timedelta(seconds=1)), db=db)
    idle = store.web_sessions.add("idle", name="C", method="password", ip=None, user_agent=None,
                                  expires_at=_iso(now + timedelta(days=1)), db=db)
    with store.connection(db) as conn:
        conn.execute("UPDATE web_sessions SET last_seen=? WHERE id=?",
                     (_iso(now - timedelta(hours=13)), idle))
    store.web_sessions.touch_many({idle: _iso(now - timedelta(hours=14))}, db=db)  # never back
    names = [s["name"] for s in store.web_sessions.list_active(idle_seconds=12 * 3600, db=db)]
    assert names == ["A"]


def test_prune_removes_sessions_expired_over_30_days_ago(db):
    now = datetime.now(timezone.utc)
    old_exp = store.web_sessions.add("o1", name="A", method="password", ip=None, user_agent=None,
                                     expires_at=_iso(now - timedelta(days=31)), db=db)
    recent_exp = store.web_sessions.add("o2", name="B", method="password", ip=None,
                                        user_agent=None, expires_at=_iso(now - timedelta(days=2)),
                                        db=db)
    old_rev = store.web_sessions.add("o3", name="C", method="password", ip=None, user_agent=None,
                                     expires_at=_iso(now + timedelta(days=1)), db=db)
    old_idle = store.web_sessions.add("o4", name="D", method="password", ip=None, user_agent=None,
                                      expires_at=_iso(now + timedelta(days=1)), db=db)
    live = store.web_sessions.add("o5", name="E", method="password", ip=None, user_agent=None,
                                  expires_at=_iso(now + timedelta(days=1)), db=db)
    with store.connection(db) as conn:
        conn.execute("UPDATE web_sessions SET revoked_at=? WHERE id=?",
                     (_iso(now - timedelta(days=40)), old_rev))
        conn.execute("UPDATE web_sessions SET last_seen=? WHERE id=?",
                     (_iso(now - timedelta(days=45)), old_idle))
    n = store.web_sessions.prune(older_than_days=30, idle_seconds=12 * 3600, db=db)
    assert n == 3
    with store.connection(db) as conn:
        left = {r[0] for r in conn.execute("SELECT id FROM web_sessions")}
    assert left == {recent_exp, live}
    assert old_exp not in left
