"""Schema v4 (v3.1, the setup guide): ``instrument_events``, one additive step
(``user_version`` 3 → 4). The v3 step is untouched, and v3.0.0's own store.py
(frozen in ``tests/store/v3_0_0``) still starts on a v4 database and ignores
the new table.

No ``__init__.py`` here (see test_store.py).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import store

HERE = Path(__file__).resolve().parent
V300 = HERE / "v3_0_0"
EVENT_COLUMNS = {"id", "instrument_id", "kind", "by", "at", "detail"}


def _cols(conn, table) -> set:
    return {r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _v3_db(path: Path) -> Path:
    conn = store.open_db(path, create=True)
    try:
        for version, step in enumerate(store.MIGRATIONS[:3]):
            with store.write_txn(conn):
                for stmt in step:
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {version + 1}")
    finally:
        conn.close()
    return path


@pytest.fixture()
def db(tmp_path) -> Path:
    path = tmp_path / "gc.db"
    store.migrate(path)
    store.instruments.upsert({"id": "gc2", "name": "GC-2"}, db=path)
    return path


def test_schema_version_is_4():
    assert store.SCHEMA_VERSION == 4 == len(store.MIGRATIONS)


def test_the_v4_step_is_additive_and_only_adds_instrument_events():
    for stmt in store.MIGRATIONS[3]:
        assert stmt.lstrip().upper().startswith(("CREATE TABLE", "CREATE INDEX"))
    assert "CREATE TABLE instrument_events" in "\n".join(store.MIGRATIONS[3])
    assert "instrument_events" not in "\n".join("\n".join(s) for s in store.MIGRATIONS[:3])


def test_v3_to_v4_keeps_data_and_backs_up(tmp_path):
    db = _v3_db(tmp_path / "gc.db")
    with store.connection(db) as conn:
        conn.execute("INSERT INTO instruments(id, name) VALUES ('gc1', 'GC-1')")
    assert store.migrate(db) == 4
    with store.connection(db) as conn:
        assert EVENT_COLUMNS <= _cols(conn, "instrument_events")
        assert conn.execute("SELECT name FROM instruments").fetchone()[0] == "GC-1"
    assert len(list((tmp_path / "backups").glob("pre-migrate-3-*.db"))) == 1


def test_v3_0_0_store_starts_on_a_v4_database(tmp_path):
    db = tmp_path / "gc.db"
    store.migrate(db)
    store.instruments.upsert({"id": "gc1", "name": "GC-1"}, db=db)
    store.instrument_events.add(db, "gc1", "created", by="Ryan C (10.0.0.5)")
    old = tmp_path / "old"
    shutil.copytree(V300, old, ignore=shutil.ignore_patterns("__pycache__"))
    script = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {str(old)!r})
        import store
        assert store.__file__.startswith({str(old)!r}), store.__file__
        v = store.migrate({str(db)!r})
        sid = store.samples.insert_received("gc1", "40306", "2026-09-25 01:00:00", "cdf",
                                            cdf_sha256="sha-new", cdf_path="cdf/x.CDF",
                                            method_name="SIMDISB.M", db={str(db)!r})
        store.instruments.upsert({{"id": "gc1", "live_since": "2026-10-01 08:00:00"}},
                                 db={str(db)!r})
        print(json.dumps({{"version": v, "sample": sid, "schema": store.SCHEMA_VERSION}}))
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=tmp_path, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["schema"] == 3 and got["version"] == 4
    assert [e["kind"] for e in store.instrument_events.list(db=db)] == ["created"]
    assert store.migrate(db) == 4


# ── the helpers ─────────────────────────────────────────────────────────────

def test_add_and_list_newest_first(db):
    a = store.instrument_events.add(db, "gc2", "created", by="Ryan C (10.0.0.5)")
    b = store.instrument_events.add(db, "gc2", "installer", by="Ryan C (10.0.0.5)",
                                    detail={"confirm_revoke": False})
    rows = store.instrument_events.list(db=db)
    assert [r["id"] for r in rows] == [b, a]
    assert rows[0]["detail"] == {"confirm_revoke": False} and rows[1]["detail"] is None
    assert rows[0]["instrument_id"] == "gc2" and rows[0]["at"].endswith("+00:00")
    assert store.instrument_events.list(instrument_id="gc1", db=db) == []
    assert len(store.instrument_events.list(limit=1, db=db)) == 1


def test_add_inside_a_write_txn_joins_it(db):
    with store.connection(db) as conn:
        with pytest.raises(RuntimeError):
            with store.write_txn(conn):
                store.instrument_events.add(conn, "gc2", "live_since", by="x",
                                            detail={"live_since": "2026-10-01 08:00:00"})
                raise RuntimeError("roll back")
    assert store.instrument_events.list(db=db) == []


def test_unknown_kind_is_refused(db):
    with pytest.raises(ValueError):
        store.instrument_events.add(db, "gc2", "nonsense", by="x")


def test_latest_by_kind(db):
    store.instrument_events.add(db, "gc2", "calibration_saved", by="A (1)", at="2026-09-30T10:00:00+00:00")
    store.instrument_events.add(db, "gc2", "calibration_saved", by="B (2)", at="2026-09-30T11:00:00+00:00")
    store.instrument_events.add(db, "gc2", "installer", by="C (3)")
    latest = store.instrument_events.latest_by_kind("gc2", db=db)
    assert latest["calibration_saved"]["by"] == "B (2)"
    assert set(latest) == {"calibration_saved", "installer"}


def test_the_event_kinds_cover_the_spec():
    assert {"installer", "calibration_saved", "corrections_saved", "method_mapped", "export_path",
            "live_since", "token_revoked"} <= set(store.EVENT_KINDS)


def test_detail_must_be_json_serialisable(db):
    with pytest.raises((TypeError, ValueError)):
        store.instrument_events.add(db, "gc2", "created", by="x", detail={"x": object()})
    with store.connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM instrument_events").fetchone()[0] == 0


def test_required_columns_include_the_new_table():
    assert store.REQUIRED_COLUMNS["instrument_events"] == frozenset(EVENT_COLUMNS)


def test_a_database_missing_the_table_is_refused_by_this_code(tmp_path):
    db = _v3_db(tmp_path / "gc.db")
    with sqlite3.connect(db) as raw:
        raw.execute("PRAGMA user_version = 4")        # claims v4 but lacks the table
    with pytest.raises(store.SchemaError):
        store.migrate(db)
