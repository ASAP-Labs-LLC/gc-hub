"""store.samples.with_lab_id (sendable links, v3.1): an indexed lookup of a
lab ID, exact or ASCII case-insensitive, newest injection first, with no row
limit; and the ``samples_lab_nocase`` index, which ``migrate`` ensures on
every start (``CREATE INDEX IF NOT EXISTS``, outside the numbered steps: no
``user_version`` bump, no backup, nothing for the other lanes to order).

No ``__init__.py`` in this folder on purpose (see test_store.py).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import store


@pytest.fixture()
def db(tmp_path) -> Path:
    path = tmp_path / "gc.db"
    store.migrate(path)
    store.instruments.upsert({"id": "gc1", "name": "GC-1"}, db=path)
    store.instruments.upsert({"id": "gc2", "name": "GC-2"}, db=path)
    return path


_n = [0]


def add(db, lab_id, dt, instrument="gc1", status="final") -> int:
    _n[0] += 1
    return store.samples.insert_received(instrument, lab_id, dt, "cdf",
                                         cdf_sha256=f"sha-lab-{_n[0]}", cdf_path=None,
                                         status=status, db=db)


def _indexes(db) -> set:
    with store.connection(db) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _plan(db, sql, args) -> str:
    with store.connection(db) as conn:
        return " ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args))


def test_exact_newest_first_and_never_a_substring(db):
    a = add(db, "40329", "2026-09-27 09:00:00")
    b = add(db, "40329", "2026-09-28 09:00:00", "gc2")
    add(db, "403290", "2026-09-29 09:00:00")
    add(db, "4032", "2026-09-29 09:00:00")
    assert [r["id"] for r in store.samples.with_lab_id("40329", db=db)] == [b, a]
    assert store.samples.with_lab_id("4032_", db=db) == []
    assert store.samples.with_lab_id("%", db=db) == []


def test_nocase_is_ascii_only(db):
    upper = add(db, "40318-RERUN-2", "2026-09-28 09:00:00")
    ete = add(db, "ÉTÉ", "2026-09-28 10:00:00")
    assert store.samples.with_lab_id("40318-rerun-2", db=db) == []
    assert [r["id"] for r in store.samples.with_lab_id("40318-rerun-2", nocase=True, db=db)] \
        == [upper]
    assert [r["id"] for r in store.samples.with_lab_id("ÉTÉ", db=db)] == [ete]
    # SQLite's NOCASE folds A-Z only: é/É are different characters
    assert store.samples.with_lab_id("été", nocase=True, db=db) == []


def test_no_row_limit(db):
    old = add(db, "4032", "2020-01-01 00:00:00")
    with store.connection(db) as conn:
        with store.write_txn(conn):
            conn.executemany(
                "INSERT INTO samples(instrument_id, lab_id, injection_dt, injection_dt_source, "
                "status, received_at) VALUES ('gc1', ?, ?, 'cdf', 'final', '2026-01-01')",
                [(f"4032{i % 10}", f"2026-01-01 {i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d}")
                 for i in range(6000)])
            conn.executemany(
                "INSERT INTO samples(instrument_id, lab_id, injection_dt, injection_dt_source, "
                "status, received_at) VALUES ('gc2', '4032', ?, 'cdf', 'error', '2026-01-01')",
                [(f"2025-01-01 {i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d}",)
                 for i in range(5100)])
    rows = store.samples.with_lab_id("4032", db=db)
    assert len(rows) == 5101 and rows[-1]["id"] == old


def test_both_lookups_use_an_index(db):
    assert "samples_lab_nocase" in _indexes(db)
    exact = _plan(db, "SELECT * FROM samples WHERE lab_id=?", ("x",))
    folded = _plan(db, "SELECT * FROM samples WHERE lab_id=? COLLATE NOCASE", ("x",))
    assert "USING INDEX samples_lab " in exact + " "
    assert "USING INDEX samples_lab_nocase" in folded


def test_migrate_ensures_the_index_without_a_version_step(tmp_path, db):
    with store.connection(db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute("DROP INDEX samples_lab_nocase")
    assert "samples_lab_nocase" not in _indexes(db)
    assert store.migrate(db) == version == store.SCHEMA_VERSION
    assert "samples_lab_nocase" in _indexes(db)
    assert not (tmp_path / "backups").exists() or not list((tmp_path / "backups").iterdir())
    store.migrate(db)                               # idempotent
    assert "samples_lab_nocase" in _indexes(db)


def test_a_newer_database_gets_the_index_too(db):
    with store.connection(db) as conn:
        conn.execute("DROP INDEX samples_lab_nocase")
        conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 3}")
    store.migrate(db)
    assert "samples_lab_nocase" in _indexes(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION + 3
