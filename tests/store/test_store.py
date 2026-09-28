"""Tests for store.py: schema v1, additive migrations, typed helpers, backups.

Every test gets its own database under pytest's ``tmp_path``. Nothing here
imports ``app``.

No ``__init__.py`` in this folder on purpose: a ``tests/store`` *package*
would shadow the top-level ``store`` module.
"""
from __future__ import annotations

import itertools
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import pytest

import store

STATUSES = ("received", "awaiting_calibration", "pending_corrections", "final",
            "raw_only", "error", "other_method", "review_method")


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path) -> Path:
    path = tmp_path / "gc.db"
    store.migrate(path)
    return path


@pytest.fixture()
def gc1(db) -> Path:
    store.instruments.upsert({"id": "gc1", "name": "GC-1"}, db=db)
    store.instruments.upsert({"id": "gc2", "name": "GC-2"}, db=db)
    return db


_counter = itertools.count(1)


def _sample(db, instrument="gc1", lab_id="40305", dt="2026-09-25 00:24:50", sha=None, **kw):
    n = next(_counter)  # itertools.count: atomic under the GIL, safe from threads
    kw.setdefault("method_name", "SIMDISB.M")
    return store.samples.insert_received(
        instrument, lab_id, dt, "cdf",
        cdf_sha256=sha if sha is not None else f"sha-{uuid.uuid4().hex}",
        cdf_path=f"cdf/{instrument}/2026/09/{lab_id}_{n}.CDF",
        source_name=f"{lab_id}.CDF", db=db, **kw)


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


# ── schema and migrations ───────────────────────────────────────────────────

SPEC_COLUMNS = {
    "instruments": {"id", "name", "method", "enabled", "live_since", "calibration_cdf",
                    "calibration_assignments", "calibration_sensitivity", "lem_machine_uid",
                    "token_hash", "token_issued_at", "export_path",
                    "method_map", "created_at", "updated_at"},
    "samples": {"id", "instrument_id", "lab_id", "injection_dt", "injection_dt_source",
                "method_name", "legacy_injection_dt", "time_corrected", "cdf_sha256",
                "cdf_path", "legacy_unverified", "source_name", "is_blank", "status",
                "backfill", "error", "current_revision", "released_at", "released_by",
                "qbench_revision", "qbench_uploaded_at", "received_at"},
    "sample_results": {"sample_id", "revision", "results", "d86_uncorrected",
                       "calibration_used", "blank_used", "corrections_used", "best_fit",
                       "fit_score", "flags", "reason", "by", "processed_at"},
    "conflicts": {"id", "instrument_id", "lab_id", "injection_dt", "existing_sample_id",
                  "cdf_sha256", "cdf_path", "received_at", "resolved", "resolved_by",
                  "resolved_at"},
    "export_rows": {"seq", "instrument_id", "sample_id", "revision", "row", "hub_appended_at"},
    "jobs": {"id", "kind", "payload", "state", "attempts", "not_before", "last_error",
             "created_at", "sample_id", "finished_at"},
    "agents": {"instrument_id", "version", "state", "queue_size", "rejected_count",
               "last_file", "last_error", "host", "agent_time", "last_seen", "results_seq",
               "pending_command"},
    "instrument_corrections": {"instrument_id", "cut", "value", "updated_at", "updated_by"},
    "corrections_audit": {"id", "instrument_id", "cut", "old_value", "new_value", "changed_at",
                          "changed_by", "reason"},
    "standards": {"id", "name", "instrument_id", "cdf_path", "added_at"},
    "sample_cache": {"sample_id", "rules_fingerprint", "flags", "bestfit_fingerprint",
                     "best_fit", "fit_score"},
    "settings_kv": {"key", "value"},
}


def test_schema_has_every_spec_table_and_column(db):
    with store.connection(db) as conn:
        for table, cols in SPEC_COLUMNS.items():
            assert cols <= _columns(conn, table), (table, cols - _columns(conn, table))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    assert store.SCHEMA_VERSION == len(store.MIGRATIONS) >= 1


def test_required_columns_equal_fresh_db(db):
    with store.connection(db) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert tables == set(store.REQUIRED_COLUMNS) == set(SPEC_COLUMNS)
        for t in tables:
            assert _columns(conn, t) == set(store.REQUIRED_COLUMNS[t]) == SPEC_COLUMNS[t], t
        assert "corrections_cache" not in tables
    assert not hasattr(store, "CorrectionsCacheStore")


def test_migrate_again_is_a_noop(db):
    backups = db.parent / "backups"
    before = sorted(backups.iterdir()) if backups.exists() else []
    store.migrate(db)
    store.migrate(db)
    after = sorted(backups.iterdir()) if backups.exists() else []
    assert before == after  # nothing to migrate -> no backup either
    with store.connection(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION


def test_newer_user_version_is_tolerated(db, caplog):
    with store.connection(db) as conn:
        conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 7}")
    with caplog.at_level(logging.WARNING, logger="store"):
        store.migrate(db)  # must not raise
    assert any("newer" in r.getMessage() for r in caplog.records)
    with store.connection(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION + 7
    # and the helpers still work against it
    store.settings_kv.set("k", "v", db=db)
    assert store.settings_kv.get("k", db=db) == "v"


def test_missing_needed_column_is_refused(tmp_path):
    path = tmp_path / "gc.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings_kv(key TEXT PRIMARY KEY)")  # no 'value'
    conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(store.SchemaError):
        store.migrate(path)


def test_pre_migrate_backup_is_written(tmp_path):
    path = tmp_path / "gc.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE marker(x)")
    conn.execute("INSERT INTO marker VALUES (42)")
    conn.commit()
    conn.close()
    store.migrate(path)
    backups = list((tmp_path / "backups").glob("pre-migrate-0-*.db"))
    assert len(backups) == 1
    copy = sqlite3.connect(backups[0])
    assert copy.execute("SELECT x FROM marker").fetchone()[0] == 42
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 0
    copy.close()


def _wal_v1_with_unflushed_rows(tmp_path):
    path = tmp_path / "gc.db"
    store.migrate(path)
    holder = sqlite3.connect(path)  # an open reader keeps the WAL from being checkpointed away
    holder.execute("PRAGMA wal_autocheckpoint=0")
    holder.execute("SELECT 1").fetchone()
    w = sqlite3.connect(path, isolation_level=None)
    w.execute("PRAGMA wal_autocheckpoint=0")
    for i in range(20):
        w.execute("INSERT INTO settings_kv(key, value) VALUES (?, ?)", (f"k{i}", str(i)))
    w.close()
    assert Path(str(path) + "-wal").stat().st_size > 0
    return path, holder


def test_pre_migrate_backup_captures_wal_frames(tmp_path, monkeypatch):
    path, holder = _wal_v1_with_unflushed_rows(tmp_path)
    try:
        v2 = ("CREATE TABLE extra_v2(x)", "INSERT INTO extra_v2 VALUES (1)")
        monkeypatch.setattr(store, "MIGRATIONS", store.MIGRATIONS + (v2,))
        assert store.migrate(path) == 2
    finally:
        holder.close()
    backups = list((tmp_path / "backups").glob("pre-migrate-1-*.db"))
    assert len(backups) == 1
    copy = sqlite3.connect(backups[0])
    try:
        assert copy.execute("SELECT COUNT(*) FROM settings_kv").fetchone()[0] == 20
        assert copy.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        copy.close()


def test_failed_migration_step_is_atomic(tmp_path, monkeypatch):
    path, holder = _wal_v1_with_unflushed_rows(tmp_path)
    holder.close()
    v2 = ("CREATE TABLE extra_v2(x)", "THIS IS NOT SQL")
    monkeypatch.setattr(store, "MIGRATIONS", store.MIGRATIONS + (v2,))
    with pytest.raises(sqlite3.OperationalError):
        store.migrate(path)
    with store.connection(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='extra_v2'").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM settings_kv").fetchone()[0] == 20


def test_open_db_does_not_create_but_migrate_does(tmp_path):
    missing = tmp_path / "nope" / "gc.db"
    with pytest.raises(sqlite3.OperationalError):
        store.open_db(missing)
    assert not missing.exists()
    store.migrate(missing)
    assert missing.exists()


def test_fresh_database_needs_no_backup(tmp_path):
    store.migrate(tmp_path / "gc.db")
    assert not list((tmp_path / "backups").glob("pre-migrate-*")) if (tmp_path / "backups").exists() else True


def test_open_db_pragmas(db):
    conn = store.open_db(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_default_path_is_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GC_DATA_DIR", str(tmp_path / "data"))
    assert store.default_db_path() == (tmp_path / "data").resolve() / "gc.db"
    store.migrate()
    store.settings_kv.set("a", "b")
    assert store.settings_kv.get("a") == "b"
    assert ((tmp_path / "data") / "gc.db").exists()


def test_default_path_without_data_dir_raises(monkeypatch):
    monkeypatch.delenv("GC_DATA_DIR", raising=False)
    with pytest.raises(RuntimeError):
        store.default_db_path()


# ── constraints ─────────────────────────────────────────────────────────────

def test_statuses_validated_in_code_not_by_check(gc1):
    assert set(store.STATUSES) == set(STATUSES)
    for i, status in enumerate(STATUSES):
        sid = _sample(gc1, lab_id=f"L{i}", status=status)
        assert store.samples.get(sid, db=gc1)["status"] == status
    with pytest.raises(ValueError):
        store.samples.set_status(sid, "bogus", db=gc1)
    with pytest.raises(ValueError):
        store.samples.set_status(sid, "Final", db=gc1)
    with pytest.raises(ValueError):
        _sample(gc1, lab_id="bad", status="bogus")
    with pytest.raises(ValueError):
        store.samples.update(sid, status="bogus", db=gc1)
    with store.connection(gc1) as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='samples'").fetchone()[0]
        assert "CHECK" not in sql.upper()
        # a newer release may write a status this code doesn't know (additive)
        conn.execute("UPDATE samples SET status='future_status' WHERE id=?", (sid,))
    assert store.samples.get(sid, db=gc1)["status"] == "future_status"
    assert sid in {r["id"] for r in store.samples.search(db=gc1)}
    assert store.samples.count(status="future_status", db=gc1) == 1


def test_injection_dt_source_validated(gc1):
    assert set(store.INJECTION_DT_SOURCES) == {"cdf", "mtime", "csv"}
    with pytest.raises(ValueError):
        store.samples.insert_received("gc1", "1", "2026-01-01 00:00:00", "guess",
                                      cdf_sha256="s", cdf_path="p", db=gc1)


def test_foreign_keys_enforced(gc1):
    with pytest.raises(sqlite3.IntegrityError):
        _sample(gc1, instrument="nope")
    with store.connection(gc1) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            with store.write_txn(conn):
                store.add_revision(conn, 9999, {"Lab ID": "x"}, reason="processed")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO export_rows(instrument_id, sample_id, revision, \"row\") "
                         "VALUES ('gc1', 9999, 1, 'x')")
        with pytest.raises(ValueError):
            with store.write_txn(conn):
                store.export_rows.append_pending(conn, "gc1", 9999, 1, "x\r\n")


def test_sha256_unique_across_instruments(gc1):
    _sample(gc1, instrument="gc1", sha="abc")
    with pytest.raises(sqlite3.IntegrityError):
        _sample(gc1, instrument="gc2", lab_id="other", sha="abc")
    assert store.samples.find_by_sha("abc", db=gc1)["instrument_id"] == "gc1"
    assert store.samples.find_by_sha("zzz", db=gc1) is None


def test_null_sha_allowed_for_result_only_imports(gc1):
    a = store.samples.insert_received("gc1", "1", "2026-01-01 00:00:00", "csv",
                                      cdf_sha256=None, cdf_path=None, status="final",
                                      backfill=1, legacy_unverified=1, db=gc1)
    b = store.samples.insert_received("gc1", "2", "2026-01-01 00:00:00", "csv",
                                      cdf_sha256=None, cdf_path=None, status="final",
                                      backfill=1, legacy_unverified=1, db=gc1)
    assert a != b
    assert store.samples.get(a, db=gc1)["method_name"] is None  # NULL only for result-only


def test_method_name_absent_is_empty_string_for_cdf_samples(gc1):
    sid = _sample(gc1, method_name=None)
    assert store.samples.get(sid, db=gc1)["method_name"] == ""


def test_key_unique_per_instrument(gc1):
    sid = _sample(gc1, instrument="gc1", lab_id="40305", dt="2026-09-25 00:24:50")
    with pytest.raises(sqlite3.IntegrityError):
        _sample(gc1, instrument="gc1", lab_id="40305", dt="2026-09-25 00:24:50")
    # same key on another instrument is a different sample
    _sample(gc1, instrument="gc2", lab_id="40305", dt="2026-09-25 00:24:50")
    assert store.samples.find_by_key("gc1", "40305", "2026-09-25 00:24:50", db=gc1)["id"] == sid
    assert store.samples.find_by_key("gc1", "40305", "2026-09-25 00:24:51", db=gc1) is None


def test_insert_received_defaults(gc1):
    sid = _sample(gc1)
    row = store.samples.get(sid, db=gc1)
    assert row["status"] == "received"
    assert row["current_revision"] is None
    assert row["backfill"] == 0 and row["time_corrected"] == 0 and row["legacy_unverified"] == 0
    assert row["received_at"]
    assert store.samples.get(99999, db=gc1) is None


def test_update_whitelisted_fields(gc1):
    sid = _sample(gc1)
    store.samples.update(sid, cdf_path="cdf/gc1/x.CDF", released_at="2026-09-28T00:00:00+00:00",
                         released_by="ryan", db=gc1)
    row = store.samples.get(sid, db=gc1)
    assert row["cdf_path"] == "cdf/gc1/x.CDF" and row["released_by"] == "ryan"
    with pytest.raises(ValueError):
        store.samples.update(sid, id=5, db=gc1)
    with pytest.raises(ValueError):
        store.samples.update(sid, current_revision=3, db=gc1)


def test_set_status_with_error(gc1):
    sid = _sample(gc1)
    store.samples.set_status(sid, "error", error="boom", db=gc1)
    assert store.samples.get(sid, db=gc1)["error"] == "boom"
    store.samples.set_status(sid, "final", db=gc1)
    row = store.samples.get(sid, db=gc1)
    assert row["status"] == "final" and row["error"] is None


# ── instruments ─────────────────────────────────────────────────────────────

def test_instruments_upsert_get_list(db):
    store.instruments.upsert({"id": "gc1", "name": "GC-1",
                              "method_map": {"SIMDISB.M": "D2887"}}, db=db)
    row = store.instruments.get("gc1", db=db)
    assert row["name"] == "GC-1" and row["method"] == "D2887" and row["enabled"] == 1
    assert row["calibration_sensitivity"] == 50
    assert row["method_map"] == '{"SIMDISB.M": "D2887"}'  # JSON columns come back as TEXT
    created = row["created_at"]
    store.instruments.upsert({"id": "gc1", "live_since": "2026-10-01T00:00:00"}, db=db)
    row = store.instruments.get("gc1", db=db)
    assert row["name"] == "GC-1" and row["live_since"] == "2026-10-01 00:00:00"
    assert row["created_at"] == created and row["updated_at"] >= created
    store.instruments.upsert({"id": "gc2", "name": "GC-2", "enabled": 0}, db=db)
    assert [r["id"] for r in store.instruments.list(db=db)] == ["gc1", "gc2"]
    assert [r["id"] for r in store.instruments.list(enabled_only=True, db=db)] == ["gc1"]
    assert store.instruments.get("gc9", db=db) is None
    with pytest.raises(ValueError):
        store.instruments.upsert({"id": "gc1", "bogus": 1}, db=db)
    with pytest.raises(ValueError):
        store.instruments.upsert({"id": "gc1", "correction_map": "{}"}, db=db)  # dropped (D4b)


def test_live_since_boundary_day_backfill(db):
    store.instruments.upsert({"id": "gc1", "name": "GC-1", "live_since": "2026-10-01T00:00:00"}, db=db)
    live = store.instruments.get("gc1", db=db)["live_since"]
    assert live == "2026-10-01 00:00:00"
    # the ISO 'T' form would sort after '2026-10-01 00:00:00' and wrongly mark it backfill
    assert store.is_backfill(live, "2026-10-01 00:00:00") is False
    assert store.is_backfill(live, "2026-10-01 08:00:00") is False
    assert store.is_backfill(live, "2026-09-30 23:59:59") is True
    assert store.is_backfill(None, "2030-01-01 00:00:00") is True  # unset: everything is backfill
    assert store.is_backfill("2026-10-01", "2026-10-01 00:00:00") is False
    store.instruments.upsert({"id": "gc1", "live_since": datetime(2026, 10, 2, 9, 30)}, db=db)
    assert store.instruments.get("gc1", db=db)["live_since"] == "2026-10-02 09:30:00"


# ── search ──────────────────────────────────────────────────────────────────

def test_search_filters_and_paging(gc1):
    ids = []
    for day in range(1, 11):
        ids.append(_sample(gc1, instrument="gc1", lab_id=f"4030{day % 10}",
                           dt=f"2026-09-{day:02d} 10:00:00"))
    other = _sample(gc1, instrument="gc2", lab_id="99999", dt="2026-09-05 11:00:00")
    store.samples.set_status(ids[2], "final", db=gc1)
    store.samples.set_status(ids[3], "error", error="x", db=gc1)

    everything = store.samples.search(db=gc1)
    assert len(everything) == 11
    # newest injection first
    assert [r["injection_dt"] for r in everything] == sorted(
        (r["injection_dt"] for r in everything), reverse=True)
    assert store.samples.count(db=gc1) == 11

    assert {r["id"] for r in store.samples.search(instrument="gc2", db=gc1)} == {other}
    assert [r["id"] for r in store.samples.search(q="40303", db=gc1)] == [ids[2]]
    assert store.samples.search(q="9999", db=gc1)[0]["id"] == other
    rng = store.samples.search(instrument="gc1", date_from="2026-09-03", date_to="2026-09-05", db=gc1)
    assert {r["id"] for r in rng} == {ids[2], ids[3], ids[4]}  # date_to is inclusive of the day
    assert [r["id"] for r in store.samples.search(status="final", db=gc1)] == [ids[2]]
    assert {r["id"] for r in store.samples.search(status=["final", "error"], db=gc1)} == {ids[2], ids[3]}
    page1 = store.samples.search(instrument="gc1", limit=4, offset=0, db=gc1)
    page2 = store.samples.search(instrument="gc1", limit=4, offset=4, db=gc1)
    page3 = store.samples.search(instrument="gc1", limit=4, offset=8, db=gc1)
    assert len(page1) == 4 and len(page2) == 4 and len(page3) == 2
    assert len({r["id"] for r in page1 + page2 + page3}) == 10
    assert store.samples.count(instrument="gc1", status="received", db=gc1) == 8
    # LIKE wildcards in q are literal
    assert store.samples.search(q="%", db=gc1) == []
    # ISO 'T' bounds are normalised to the injection_dt form
    t = store.samples.search(instrument="gc1", date_from="2026-09-03T10:00:00",
                             date_to="2026-09-04T10:00:00", db=gc1)
    assert {r["id"] for r in t} == {ids[2], ids[3]}


def test_search_method_and_backfill_filters(gc1):
    a = _sample(gc1, lab_id="A", method_name="SIMDISB.M")
    b = _sample(gc1, lab_id="B", method_name="D7096.M", backfill=1)
    c = _sample(gc1, lab_id="C", method_name="")
    assert [r["id"] for r in store.samples.search(method_name="D7096.M", db=gc1)] == [b]
    assert {r["id"] for r in store.samples.search(method_name=["", "SIMDISB.M"], db=gc1)} == {a, c}
    assert [r["id"] for r in store.samples.search(backfill=True, db=gc1)] == [b]
    assert {r["id"] for r in store.samples.search(backfill=False, db=gc1)} == {a, c}
    assert store.samples.count(backfill=False, method_name="", db=gc1) == 1


# ── revisions ───────────────────────────────────────────────────────────────

def test_add_revision_increments_and_sets_current(gc1):
    sid = _sample(gc1)
    with store.connection(gc1) as conn:
        with store.write_txn(conn):
            r1 = store.add_revision(conn, sid, {"Lab ID": "40305", "IBP": "100.0"},
                                    reason="processed", corrections_used={"source": "file"})
        assert r1 == 1
        assert store.samples.get(sid, db=conn)["current_revision"] == 1
        with store.write_txn(conn):
            r2 = store.add_revision(conn, sid, {"Lab ID": "40305", "IBP": "101.0"},
                                    reason="reprocess", by="ryan", blank_used=None)
        assert r2 == 2
    assert store.samples.get(sid, db=gc1)["current_revision"] == 2
    cur = store.get_revision(sid, db=gc1)
    assert cur["revision"] == 2 and cur["reason"] == "reprocess" and cur["by"] == "ryan"
    assert cur["results"] == '{"Lab ID": "40305", "IBP": "101.0"}'
    assert store.get_revision(sid, 1, db=gc1)["reason"] == "processed"
    assert [r["revision"] for r in store.list_revisions(sid, db=gc1)] == [1, 2]


def test_add_revision_requires_a_transaction(gc1):
    sid = _sample(gc1)
    with store.connection(gc1) as conn:
        with pytest.raises(RuntimeError):
            store.add_revision(conn, sid, {}, reason="processed")


def test_write_txn_rolls_back_everything(gc1):
    sid = _sample(gc1)
    with store.connection(gc1) as conn:
        with pytest.raises(ZeroDivisionError):
            with store.write_txn(conn):
                rev = store.add_revision(conn, sid, {"a": 1}, reason="processed")
                store.export_rows.append_pending(conn, "gc1", sid, rev, "line\r\n")
                store.samples.set_status(sid, "final", db=conn)
                1 / 0
    row = store.samples.get(sid, db=gc1)
    assert row["status"] == "received" and row["current_revision"] is None
    assert store.list_revisions(sid, db=gc1) == []
    assert store.export_rows.pending_hub_appends("gc1", db=gc1) == []


def test_nested_write_txn_is_a_savepoint(gc1):
    with store.connection(gc1) as conn:
        with store.write_txn(conn):
            store.settings_kv.set("outer", "1", db=conn)
            with pytest.raises(KeyError):
                with store.write_txn(conn):
                    store.settings_kv.set("inner", "1", db=conn)
                    raise KeyError
    assert store.settings_kv.get("outer", db=gc1) == "1"
    assert store.settings_kv.get("inner", db=gc1) is None


# ── export rows ─────────────────────────────────────────────────────────────

def _final(db, instrument, lab_id, dt):
    sid = _sample(db, instrument=instrument, lab_id=lab_id, dt=dt)
    with store.connection(db) as conn:
        with store.write_txn(conn):
            rev = store.add_revision(conn, sid, {"Lab ID": lab_id}, reason="processed")
            seq = store.export_rows.append_pending(conn, instrument, sid, rev, f"{lab_id}\r\n")
    return sid, seq


def test_export_seq_increases_in_insert_order_across_instruments(gc1):
    seqs = []
    for i, inst in enumerate(["gc1", "gc2", "gc1", "gc2", "gc2", "gc1"]):
        seqs.append(_final(gc1, inst, f"L{i}", f"2026-09-0{i + 1} 00:00:00")[1])
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    rows = store.export_rows.rows_after("gc1", 0, 500, db=gc1)
    assert [r["line"] for r in rows] == ["L0\r\n", "L2\r\n", "L5\r\n"]
    assert [r["seq"] for r in rows] == [seqs[0], seqs[2], seqs[5]]
    after = store.export_rows.rows_after("gc1", seqs[0], 500, db=gc1)
    assert [r["seq"] for r in after] == [seqs[2], seqs[5]]
    assert [r["seq"] for r in store.export_rows.rows_after("gc2", 0, 2, db=gc1)] == [seqs[1], seqs[3]]


def test_pending_and_mark_hub_appended(gc1):
    _, s1 = _final(gc1, "gc1", "A", "2026-09-01 00:00:00")
    _, s2 = _final(gc1, "gc1", "B", "2026-09-02 00:00:00")
    _, s3 = _final(gc1, "gc2", "C", "2026-09-03 00:00:00")
    pend = store.export_rows.pending_hub_appends("gc1", db=gc1)
    assert [p["seq"] for p in pend] == [s1, s2]
    assert pend[0]["line"] == "A\r\n" and pend[0]["revision"] == 1
    store.export_rows.mark_hub_appended(s1, db=gc1)
    assert [p["seq"] for p in store.export_rows.pending_hub_appends("gc1", db=gc1)] == [s2]
    store.export_rows.mark_hub_appended([s2, s3], db=gc1)
    assert store.export_rows.pending_hub_appends("gc1", db=gc1) == []
    assert store.export_rows.pending_hub_appends("gc2", db=gc1) == []


def test_mark_hub_appended_keeps_first_stamp(gc1):
    _, s1 = _final(gc1, "gc1", "A", "2026-09-01 00:00:00")
    store.export_rows.mark_hub_appended(s1, at="2026-09-01T00:00:00+00:00", db=gc1)
    store.export_rows.mark_hub_appended(s1, at="2026-09-02T00:00:00+00:00", db=gc1)
    row = store.export_rows.rows_after("gc1", 0, db=gc1)[0]
    assert row["hub_appended_at"] == "2026-09-01T00:00:00+00:00"


def test_append_pending_checks_instrument(gc1):
    sid = _sample(gc1, instrument="gc1")
    with store.connection(gc1) as conn:
        with pytest.raises(ValueError):
            with store.write_txn(conn):
                rev = store.add_revision(conn, sid, {}, reason="processed")
                store.export_rows.append_pending(conn, "gc2", sid, rev, "x\r\n")


def test_export_seq_never_reused(gc1):
    _, s1 = _final(gc1, "gc1", "A", "2026-09-01 00:00:00")
    with store.connection(gc1) as conn:
        conn.execute("DELETE FROM export_rows WHERE seq=?", (s1,))
    _, s2 = _final(gc1, "gc1", "B", "2026-09-02 00:00:00")
    assert s2 > s1


# ── jobs ────────────────────────────────────────────────────────────────────

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def test_jobs_lifecycle(db):
    j1 = store.jobs.enqueue("process", {"sample_id": 1}, db=db)
    j2 = store.jobs.enqueue("process", {"sample_id": 2}, not_before=T0 + timedelta(minutes=5), db=db)
    job = store.jobs.claim_next(T0, db=db)
    assert job["id"] == j1 and job["payload"] == {"sample_id": 1}
    assert job["state"] == "running" and job["attempts"] == 1
    assert store.jobs.claim_next(T0, db=db) is None  # j2 not due yet
    store.jobs.complete(j1, db=db)
    assert store.jobs.get(j1, db=db)["state"] == "done"
    job = store.jobs.claim_next(T0 + timedelta(minutes=5), db=db)
    assert job["id"] == j2
    store.jobs.fail(j2, "LEM down", retry_at=T0 + timedelta(minutes=10), db=db)
    row = store.jobs.get(j2, db=db)
    assert row["state"] == "queued" and row["last_error"] == "LEM down"
    assert store.jobs.claim_next(T0 + timedelta(minutes=9), db=db) is None
    job = store.jobs.claim_next((T0 + timedelta(minutes=10)).isoformat(), db=db)
    assert job["id"] == j2 and job["attempts"] == 2
    store.jobs.fail(j2, "gave up", db=db)
    assert store.jobs.get(j2, db=db)["state"] == "failed"


def test_jobs_reject_naive_times(db):
    with pytest.raises(ValueError):
        store.jobs.enqueue("k", {}, not_before=datetime(2026, 9, 28, 12, 0), db=db)
    with pytest.raises(ValueError):
        store.jobs.enqueue("k", {}, not_before="2026-09-28T12:00:00", db=db)
    with pytest.raises(ValueError):
        store.jobs.claim_next(datetime(2026, 9, 28, 12, 0), db=db)
    with pytest.raises(ValueError):
        store.jobs.claim_next("2026-09-28 12:00:00", db=db)
    assert store.jobs.claim_next("2026-09-28T12:00:00Z", db=db) is None
    assert store.jobs.claim_next("2026-09-28T14:00:00+02:00", db=db) is None


def test_claim_next_is_fifo(db):
    ids = [store.jobs.enqueue("k", {"i": i}, db=db) for i in range(5)]
    got = [store.jobs.claim_next(T0, db=db)["id"] for _ in range(5)]
    assert got == ids


def test_claim_next_atomic_across_threads(db):
    n = 200
    ids = {store.jobs.enqueue("process", {"i": i}, db=db) for i in range(n)}
    claimed: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait()
            while True:
                job = store.jobs.claim_next(datetime.now(timezone.utc), db=db)
                if job is None:
                    return
                claimed.append(job["id"])
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    assert len(claimed) == n and set(claimed) == ids  # every job once, none twice


def test_requeue_stale_running(db):
    a = store.jobs.enqueue("process", {}, db=db)
    b = store.jobs.enqueue("process", {}, db=db)
    store.jobs.claim_next(T0, db=db)
    store.jobs.claim_next(T0, db=db)
    store.jobs.complete(b, db=db)
    assert store.jobs.requeue_stale_running(db=db) == 1
    assert store.jobs.get(a, db=db)["state"] == "queued"
    assert store.jobs.get(b, db=db)["state"] == "done"
    assert store.jobs.claim_next(T0, db=db)["id"] == a


def test_enqueue_dedupes_queued_jobs_per_sample(gc1):
    sid = _sample(gc1)
    j1 = store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid,
                            not_before=T0 + timedelta(minutes=5), db=gc1)
    j2 = store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid, db=gc1)
    assert j1 == j2
    # the duplicate brought the queued job forward (NULL = due now)
    assert store.jobs.get(j1, db=gc1)["not_before"] is None
    j3 = store.jobs.enqueue("other", {"sample_id": sid}, sample_id=sid, db=gc1)
    assert j3 != j1  # a different kind is a different job
    claimed = store.jobs.claim_next(T0, kind="process", db=gc1)
    assert claimed["id"] == j1 and claimed["sample_id"] == sid
    j4 = store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid, db=gc1)
    assert j4 != j1  # the first is running, so a new one may queue


def test_enqueue_for_status(gc1):
    a = _sample(gc1, lab_id="A", status="awaiting_calibration")
    b = _sample(gc1, lab_id="B", status="awaiting_calibration")
    _sample(gc1, lab_id="C", status="awaiting_calibration", instrument="gc2")
    _sample(gc1, lab_id="D", status="final")
    o1 = _sample(gc1, lab_id="E", status="other_method", method_name="SIMDISX.M")
    _sample(gc1, lab_id="F", status="other_method", method_name="D7096.M")
    store.jobs.enqueue("process", {"sample_id": a}, sample_id=a,
                       not_before=T0 + timedelta(hours=1), db=gc1)
    with store.connection(gc1) as conn:
        with store.write_txn(conn):
            n = store.jobs.enqueue_for_status(conn, "gc1", "awaiting_calibration")
    assert n == 2
    queued = store.jobs.list(state="queued", db=gc1)
    assert sorted(j["sample_id"] for j in queued) == [a, b]
    assert all(j["payload"] == {"sample_id": j["sample_id"]} and j["kind"] == "process" for j in queued)
    assert all(j["not_before"] is None for j in queued)
    assert store.jobs.enqueue_for_status(gc1, "gc1", "awaiting_calibration") == 2  # idempotent
    assert len(store.jobs.list(state="queued", db=gc1)) == 2
    assert store.jobs.enqueue_for_status(gc1, "gc1", "other_method", method_name="SIMDISX.M") == 1
    assert o1 in {j["sample_id"] for j in store.jobs.list(state="queued", db=gc1)}


def test_requeue_stale_running_with_queued_duplicate(gc1):
    sid = _sample(gc1)
    j1 = store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid, db=gc1)
    store.jobs.claim_next(T0, db=gc1)
    j2 = store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid,
                            not_before=T0 + timedelta(hours=1), db=gc1)
    assert j2 != j1
    store.jobs.requeue_stale_running(db=gc1)  # must not raise a UNIQUE error
    queued = store.jobs.list(state="queued", db=gc1)
    assert [j["sample_id"] for j in queued] == [sid]
    assert queued[0]["not_before"] is None  # the stale run was due now
    assert store.jobs.get(j1, db=gc1)["state"] != "running"


def test_fail_retry_with_queued_twin_supersedes(gc1):
    sid = _sample(gc1)
    j1 = store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid, db=gc1)
    store.jobs.claim_next(T0, db=gc1)
    j2 = store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid,
                            not_before=T0 + timedelta(hours=1), db=gc1)
    store.jobs.fail(j1, "LEM down", retry_at=T0 + timedelta(minutes=5), db=gc1)  # no UNIQUE error
    assert store.jobs.get(j1, db=gc1)["state"] == "superseded"
    twin = store.jobs.get(j2, db=gc1)
    assert twin["state"] == "queued"
    assert twin["not_before"] == (T0 + timedelta(minutes=5)).isoformat(timespec="microseconds")


def test_claim_next_skips_poison_payload(db):
    with store.connection(db) as conn:
        conn.execute("INSERT INTO jobs(kind, payload, state, attempts, created_at) "
                     "VALUES ('process', '{not json', 'queued', 0, ?)", (store.now_iso(),))
    good = store.jobs.enqueue("process", {"ok": 1}, db=db)
    job = store.jobs.claim_next(T0, db=db)
    assert job["id"] == good
    bad = [j for j in store.jobs.list(db=db) if j["id"] != good][0]
    assert bad["state"] == "failed" and "payload" in bad["last_error"]
    assert store.jobs.claim_next(T0, db=db) is None


def test_prune_done(db):
    a = store.jobs.enqueue("k", {}, db=db)
    b = store.jobs.enqueue("k", {}, db=db)
    c = store.jobs.enqueue("k", {}, db=db)
    for _ in range(3):
        store.jobs.claim_next(T0, db=db)
    store.jobs.complete(a, db=db)
    store.jobs.fail(b, "x", db=db)
    assert store.jobs.get(a, db=db)["finished_at"]
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    assert store.jobs.prune_done(later, db=db) == 1
    assert store.jobs.get(a, db=db) is None
    assert store.jobs.get(b, db=db)["state"] == "failed"
    assert store.jobs.get(c, db=db)["state"] == "running"
    with pytest.raises(ValueError):
        store.jobs.prune_done(datetime(2026, 1, 1), db=db)


# ── blanks, gate, legacy lookup, methods seen ───────────────────────────────

def test_latest_blank_at_or_before(gc1):
    b1 = _sample(gc1, lab_id="blank", dt="2026-09-01 08:00:00", is_blank=1)
    b2 = _sample(gc1, lab_id="blank", dt="2026-09-01 12:00:00", is_blank=1)
    _sample(gc1, lab_id="blank", dt="2026-09-01 11:00:00", is_blank=1, method_name="D7096.M")
    _sample(gc1, lab_id="blank", dt="2026-09-01 10:00:00", is_blank=1, instrument="gc2")
    _sample(gc1, lab_id="notblank", dt="2026-09-01 10:30:00", is_blank=0)
    m = ["SIMDISB.M", "SIMDISTB.M"]
    assert store.samples.latest_blank("gc1", "2026-09-01 11:30:00", m, db=gc1)["id"] == b1
    assert store.samples.latest_blank("gc1", "2026-09-01 12:00:00", m, db=gc1)["id"] == b2  # at
    assert store.samples.latest_blank("gc1", "2026-09-01 07:00:00", m, db=gc1) is None
    assert store.samples.latest_blank("gc1", "2026-09-01 12:00:00", m,
                                      exclude_sample_id=b2, db=gc1)["id"] == b1
    assert store.samples.latest_blank("gc1", "2026-09-02 00:00:00", [], db=gc1) is None
    assert store.samples.latest_blank("gc1", "2026-09-01 11:30:00", ["D7096.M"], db=gc1)["lab_id"] == "blank"
    with store.connection(gc1) as conn:
        names = {r[1] for r in conn.execute("PRAGMA index_list(samples)")}
        assert "samples_blanks" in names


def test_is_gated(gc1):
    live = _sample(gc1, lab_id="1", status="final")
    back = _sample(gc1, lab_id="2", status="final", backfill=1)
    rel = _sample(gc1, lab_id="3", status="final", backfill=1)
    store.samples.update(rel, released_at=store.now_iso(), released_by="ryan", db=gc1)
    pend = _sample(gc1, lab_id="4", status="pending_corrections")
    assert store.samples.is_gated(live, db=gc1) is True
    assert store.samples.is_gated(back, db=gc1) is False
    assert store.samples.is_gated(rel, db=gc1) is True
    assert store.samples.is_gated(pend, db=gc1) is False
    assert store.samples.is_gated(99999, db=gc1) is False
    with store.connection(gc1) as conn:
        got = {r[0] for r in conn.execute(f"SELECT id FROM samples WHERE {store.GATE_SQL}")}
    assert got == {live, rel}


def test_find_by_legacy(gc1):
    fixed = _sample(gc1, lab_id="40305", dt="2026-09-25 00:24:50",
                    legacy_injection_dt="2026-09-25 02:45:00", time_corrected=1)
    plain = _sample(gc1, lab_id="40306", dt="2026-09-25 01:00:00",
                    legacy_injection_dt="2026-09-25 01:00:00")
    assert [r["id"] for r in store.samples.find_by_legacy("gc1", "40305", "2026-09-25 02:45:00", db=gc1)] == [fixed]
    assert [r["id"] for r in store.samples.find_by_legacy("gc1", "40305", "2026-09-25 00:24:50", db=gc1)] == [fixed]
    assert [r["id"] for r in store.samples.find_by_legacy("gc1", "40306", "2026-09-25 01:00:00", db=gc1)] == [plain]
    assert store.samples.find_by_legacy("gc2", "40305", "2026-09-25 02:45:00", db=gc1) == []
    with store.connection(gc1) as conn:
        cols = [r[2] for r in conn.execute("PRAGMA index_info(samples_legacy)")]
    assert cols == ["instrument_id", "lab_id", "legacy_injection_dt"]


def test_methods_seen(gc1):
    _sample(gc1, lab_id="1", dt="2026-09-01 00:00:00", method_name="SIMDISB.M")
    _sample(gc1, lab_id="2", dt="2026-09-03 00:00:00", method_name="SIMDISB.M")
    _sample(gc1, lab_id="3", dt="2026-09-02 00:00:00", method_name="D7096.M")
    _sample(gc1, lab_id="4", dt="2026-09-02 00:00:00", method_name="")
    _sample(gc1, lab_id="5", dt="2026-09-02 00:00:00", method_name="X.M", instrument="gc2")
    seen = {r["method_name"]: r for r in store.samples.methods_seen("gc1", db=gc1)}
    assert set(seen) == {"SIMDISB.M", "D7096.M", ""}
    assert seen["SIMDISB.M"]["count"] == 2
    assert seen["SIMDISB.M"]["first_seen"] == "2026-09-01 00:00:00"
    assert seen["SIMDISB.M"]["last_seen"] == "2026-09-03 00:00:00"


# ── corrections (D4b) ───────────────────────────────────────────────────────

CUTS = ["IBP", "5%", "10%", "20%", "30%", "50%", "70%", "80%", "90%", "95%", "FBP"]


def test_corrections_set_all_read_audit(gc1):
    assert store.corrections.read("gc1", db=gc1) is None
    vals = {c: 0.0 for c in CUTS}
    vals["IBP"] = 1.5
    with store.connection(gc1) as conn:
        with pytest.raises(RuntimeError):
            store.corrections.set_all(conn, "gc1", vals, by="ryan", reason="seed")
        with store.write_txn(conn):
            n = store.corrections.set_all(conn, "gc1", vals, by="ryan",
                                          reason="seeded from correction_factors.json")
    assert n == 11
    got = store.corrections.read("gc1", db=gc1)
    assert got["values"] == vals and got["updated_by"] == "ryan" and got["updated_at"]
    audit = store.corrections.audit("gc1", 100, db=gc1)
    assert len(audit) == 11 and all(a["old_value"] is None for a in audit)
    vals2 = dict(vals, **{"IBP": 2.0, "FBP": -1.0})
    with store.connection(gc1) as conn:
        with store.write_txn(conn):
            n = store.corrections.set_all(conn, "gc1", vals2, by="amy", reason="re-cal")
    assert n == 2
    got = store.corrections.read("gc1", db=gc1)
    assert got["values"] == vals2 and got["updated_by"] == "amy"
    audit = store.corrections.audit("gc1", 100, db=gc1)
    assert len(audit) == 13
    assert {(a["cut"], a["old_value"], a["new_value"]) for a in audit[:2]} == {("IBP", 1.5, 2.0), ("FBP", 0.0, -1.0)}
    assert audit[0]["id"] > audit[-1]["id"]  # newest first
    assert len(store.corrections.audit("gc1", 5, db=gc1)) == 5
    assert store.corrections.read("gc2", db=gc1) is None


def test_corrections_rules(gc1):
    with store.connection(gc1) as conn:
        for bad_reason in ("", "   ", None):
            with pytest.raises(ValueError):
                with store.write_txn(conn):
                    store.corrections.set_all(conn, "gc1", {"IBP": 1.0}, by="r", reason=bad_reason)
        with pytest.raises(ValueError):
            with store.write_txn(conn):
                store.corrections.set_all(conn, "gc1", {"IBP": float("nan")}, by="r", reason="x")
        with pytest.raises(sqlite3.IntegrityError):
            with store.write_txn(conn):
                store.corrections.set_all(conn, "nope", {"IBP": 1.0}, by="r", reason="x")
    assert store.corrections.audit("gc1", 10, db=gc1) == []


# ── transaction guard ───────────────────────────────────────────────────────

def test_new_connection_inside_write_txn_fails_fast(gc1):
    sid = _sample(gc1)
    with store.connection(gc1) as conn:
        with store.write_txn(conn):
            store.samples.set_status(sid, "error", error="x", db=conn)
            t = time.monotonic()
            with pytest.raises(RuntimeError, match="db=conn"):
                store.settings_kv.set("k", "v", db=gc1)  # would deadlock for busy_timeout
            with pytest.raises(RuntimeError, match="db=conn"):
                store.samples.get(sid, db=gc1)  # would read the stale, pre-txn row
            assert time.monotonic() - t < 2
            assert store.samples.get(sid, db=conn)["status"] == "error"
        # released after the transaction
        assert store.samples.get(sid, db=gc1)["status"] == "error"


def test_write_txn_commit_failure_rolls_back(gc1):
    with store.connection(gc1) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            with store.write_txn(conn):
                conn.execute("PRAGMA defer_foreign_keys=ON")  # FK checked at COMMIT
                conn.execute("INSERT INTO sample_cache(sample_id) VALUES (424242)")
        assert not conn.in_transaction
    store.settings_kv.set("after", "ok", db=gc1)  # no lock left behind
    assert store.sample_cache.get(424242, db=gc1) is None


# ── small tables ────────────────────────────────────────────────────────────

def test_sample_cache_get_put_merges(gc1):
    sid = _sample(gc1)
    assert store.sample_cache.get(sid, db=gc1) is None
    store.sample_cache.put(sid, rules_fingerprint="r1", flags='["low"]', db=gc1)
    store.sample_cache.put(sid, bestfit_fingerprint="b1", best_fit="Diesel", fit_score=0.9, db=gc1)
    row = store.sample_cache.get(sid, db=gc1)
    assert row["flags"] == '["low"]' and row["best_fit"] == "Diesel" and row["fit_score"] == 0.9
    store.sample_cache.put(sid, flags=["high"], db=gc1)  # non-str JSON-encoded
    assert store.sample_cache.get(sid, db=gc1)["flags"] == '["high"]'
    with pytest.raises(ValueError):
        store.sample_cache.put(sid, bogus=1, db=gc1)


def test_settings_kv(db):
    assert store.settings_kv.get("LEM_URL", db=db) is None
    assert store.settings_kv.get("LEM_URL", "dflt", db=db) == "dflt"
    store.settings_kv.set("LEM_URL", "http://x", db=db)
    store.settings_kv.set("LEM_URL", "http://y", db=db)
    assert store.settings_kv.get("LEM_URL", db=db) == "http://y"
    store.settings_kv.delete("LEM_URL", db=db)
    assert store.settings_kv.get("LEM_URL", db=db) is None


def test_conflicts_add_list_resolve(gc1):
    sid = _sample(gc1)
    cid = store.conflicts.add("gc1", "40305", "2026-09-25 00:24:50", sid, "othersha",
                              "cdf/gc1/conflicts/x.CDF", db=gc1)
    open_ = store.conflicts.list(db=gc1)
    assert [c["id"] for c in open_] == [cid] and open_[0]["resolved"] is None
    store.conflicts.resolve(cid, "kept-existing", by="ryan", db=gc1)
    assert store.conflicts.list(db=gc1) == []
    done = store.conflicts.list(unresolved_only=False, db=gc1)
    assert done[0]["resolved"] == "kept-existing" and done[0]["resolved_by"] == "ryan"
    assert done[0]["resolved_at"]
    with pytest.raises(ValueError):
        store.conflicts.resolve(cid, "replaced", by="ryan", db=gc1)  # already resolved
    cid2 = store.conflicts.add("gc2", "1", "2026-09-25 00:00:00", None, "s", "p", db=gc1)
    with pytest.raises(ValueError):
        store.conflicts.resolve(cid2, "whatever", by="ryan", db=gc1)
    assert [c["id"] for c in store.conflicts.list(instrument_id="gc2", db=gc1)] == [cid2]
    assert store.conflicts.find_by_sha("s", db=gc1)["id"] == cid2
    assert store.conflicts.find_by_sha("othersha", db=gc1) is None  # resolved
    assert store.conflicts.find_by_sha("othersha", unresolved_only=False, db=gc1)["id"] == cid




# ── concurrency and handles ─────────────────────────────────────────────────

def test_concurrent_writers_do_not_lock(gc1):
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def writer(t):
        try:
            barrier.wait()
            for i in range(50):
                store.settings_kv.set(f"t{t}-{i}", str(i), db=gc1)
                store.jobs.enqueue("k", {"t": t, "i": i}, db=gc1)
                _sample(gc1, lab_id=f"T{t}-{i}")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not errors, errors
    assert store.samples.count(db=gc1) == 200


def _open_fds():
    for d in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(d):
            return len(os.listdir(d))
    return None


def test_short_lived_connections_leak_no_handles(gc1, monkeypatch):
    store.settings_kv.set("x", "1", db=gc1)
    opened = []
    real_connect = sqlite3.connect

    def tracking_connect(*a, **k):
        c = real_connect(*a, **k)
        opened.append(c)
        return c

    monkeypatch.setattr(store.sqlite3, "connect", tracking_connect)
    before = _open_fds()
    for i in range(1000):
        with store.connection(gc1) as conn:
            conn.execute("SELECT 1").fetchone()
        store.settings_kv.get("x", db=gc1)
        conn2 = store.open_db(gc1)
        conn2.close()
    after = _open_fds()
    monkeypatch.undo()
    assert len(opened) >= 3000
    for c in opened:  # every connection the store opened is closed
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")
    if before is not None:
        assert after <= before + 2, (before, after)
    if sys.platform == "win32":  # an open handle would block the delete
        for suffix in ("-wal", "-shm", ""):
            p = Path(str(gc1) + suffix)
            if p.exists():
                p.unlink()


# ── backups ─────────────────────────────────────────────────────────────────

def test_backup_nightly_is_openable_and_copies_settings(gc1, monkeypatch):
    monkeypatch.setenv("GC_DATA_DIR", str(gc1.parent))  # settings path = paths.settings_file()
    (gc1.parent / "settings.json").write_text('{"a": 1}', encoding="utf-8")
    store.settings_kv.set("marker", "yes", db=gc1)
    out = store.backup_nightly(gc1, now=datetime(2026, 9, 28, 2, 0))
    assert out == gc1.parent / "backups" / "gc-2026-09-28.db"
    copy = store.open_db(out)
    try:
        assert copy.execute("SELECT value FROM settings_kv WHERE key='marker'").fetchone()[0] == "yes"
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        copy.close()
    assert (gc1.parent / "backups" / "settings-2026-09-28.json").read_text(encoding="utf-8") == '{"a": 1}'
    # a second run the same day replaces it, never fails
    assert store.backup_nightly(gc1, now=datetime(2026, 9, 28, 3, 0)) == out


def test_backup_nightly_prunes_to_keep(gc1):
    backups = gc1.parent / "backups"
    backups.mkdir(exist_ok=True)
    start = datetime(2026, 8, 1)
    for i in range(20):
        day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        (backups / f"gc-{day}.db").write_bytes(b"old")
        (backups / f"settings-{day}.json").write_text("{}", encoding="utf-8")
    (backups / "pre-migrate-0-20260101T000000Z.db").write_bytes(b"keep me")
    store.backup_nightly(gc1, keep=14, now=datetime(2026, 9, 28))
    dbs = sorted(p.name for p in backups.glob("gc-*.db"))
    assert len(dbs) == 14
    assert dbs[-1] == "gc-2026-09-28.db"
    assert dbs[0] == "gc-2026-08-08.db"
    assert len(list(backups.glob("settings-*.json"))) <= 14
    assert not (backups / "settings-2026-08-01.json").exists()
    with pytest.raises(ValueError):
        store.backup_nightly(gc1, keep=0, now=datetime(2026, 9, 29))
    assert (backups / "pre-migrate-0-20260101T000000Z.db").exists()
