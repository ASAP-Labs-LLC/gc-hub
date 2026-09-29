"""Schema v2 (phase 4): comment presets (seeded once), sample comments and
the report log. The migration is additive, and v2.0.0's own store.py (frozen
verbatim in ``tests/store/v2_0_0/``) still starts on a v2 database, so a
rollback to v2.0.0 is safe.

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
ROOT = HERE.parent.parent
V200 = HERE / "v2_0_0"

SEEDED = [
    "Sample appears to be a renewable fuel, not conventional petroleum diesel.",
    "Sample appears to be gasoline.",
    "Possible contamination; results should be interpreted with caution.",
    "Re-run requested.",
]
NEW_TABLES = ("comment_presets", "sample_comments", "report_log")


def _v1_db(path: Path) -> Path:
    """A database built by v2.0.0's own store.py (the frozen copy), in a
    subprocess: exactly what a v2.0.0 hub leaves behind."""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(V200)!r})
        import store
        assert store.__file__.startswith({str(V200)!r}), store.__file__
        assert store.migrate({str(path)!r}) == 1
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=path.parent, timeout=60)
    assert out.returncode == 0, out.stderr
    with store.connection(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    return path


def _add_sample(db, lab_id="40305", dt="2026-09-25 00:24:50"):
    if store.instruments.get("gc1", db=db) is None:
        store.instruments.upsert({"id": "gc1", "name": "GC-1"}, db=db)
    return store.samples.insert_received("gc1", lab_id, dt, "cdf", cdf_sha256=f"sha-{lab_id}-{dt}",
                                         cdf_path=f"cdf/{lab_id}.CDF", method_name="SIMDISB.M",
                                         db=db)


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _dump(conn, table) -> list:
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


# ── migration v1 → v2 ───────────────────────────────────────────────────────

def test_schema_version_is_2():
    assert store.SCHEMA_VERSION == 2 == len(store.MIGRATIONS)


def test_v1_to_v2_is_additive(tmp_path):
    db = _v1_db(tmp_path / "gc.db")
    sid = _add_sample(db)
    with store.connection(db) as conn:
        with store.write_txn(conn):
            store.add_revision(conn, sid, {"IBP": 100}, reason="processed")
        store.settings_kv.set("k", "v", db=conn)
        before = {t: _dump(conn, t) for t in _tables(conn) if not t.startswith("sqlite_")}
        cols_before = {t: [tuple(r) for r in conn.execute(f'PRAGMA table_info("{t}")')]
                       for t in before}
        idx_before = _indexes(conn)

    assert store.migrate(db) == 2

    with store.connection(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        for t, rows in before.items():            # nothing lost or rewritten
            assert _dump(conn, t) == rows, t
            assert [tuple(r) for r in conn.execute(f'PRAGMA table_info("{t}")')] == \
                cols_before[t], t
        assert idx_before <= _indexes(conn)
        assert set(NEW_TABLES) <= _tables(conn)
        assert {"sample_comments_sample", "report_log_sample"} <= _indexes(conn)
    # a pre-migrate backup of the v1 file
    assert len(list((tmp_path / "backups").glob("pre-migrate-1-*.db"))) == 1


def test_presets_are_seeded_in_the_v2_step(tmp_path):
    db = tmp_path / "gc.db"
    store.migrate(db)
    presets = store.comment_presets.list(db=db)
    assert [p["text"] for p in presets] == SEEDED
    assert all(p["active"] == 1 for p in presets)
    assert [p["sort"] for p in presets] == sorted(p["sort"] for p in presets)
    assert all(p["created_by"] == "seed" and p["created_at"] for p in presets)
    # store timestamp form (UTC, microseconds, +00:00): sorts with now_iso()
    assert presets[0]["created_at"].endswith("+00:00") and "T" in presets[0]["created_at"]
    assert len(presets[0]["created_at"]) == len(store.now_iso())


def test_presets_are_seeded_exactly_once(tmp_path):
    db = tmp_path / "gc.db"
    store.migrate(db)
    with store.connection(db) as conn:
        conn.execute("DELETE FROM comment_presets WHERE text=?", (SEEDED[1],))
        conn.execute("UPDATE comment_presets SET text='edited' WHERE text=?", (SEEDED[0],))
    store.migrate(db)
    store.migrate(db)
    texts = [p["text"] for p in store.comment_presets.list(include_inactive=True, db=db)]
    assert texts == ["edited", SEEDED[2], SEEDED[3]]


def test_v1_database_gets_the_seeds_on_upgrade(tmp_path):
    db = _v1_db(tmp_path / "gc.db")
    store.migrate(db)
    assert [p["text"] for p in store.comment_presets.list(db=db)] == SEEDED


def test_a_failed_v2_step_rolls_back_its_seeds(tmp_path, monkeypatch):
    db = _v1_db(tmp_path / "gc.db")
    broken = store.MIGRATIONS[1] + ("THIS IS NOT SQL",)
    monkeypatch.setattr(store, "MIGRATIONS", (store.MIGRATIONS[0], broken))
    with pytest.raises(sqlite3.OperationalError):
        store.migrate(db)
    with store.connection(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not set(NEW_TABLES) & _tables(conn)


def test_frozen_v2_0_0_copy_matches_the_tag():
    """The frozen copy is v2.0.0's, byte for byte (when the tag is here)."""
    for name in ("store.py", "paths.py"):
        try:
            tagged = subprocess.run(["git", "show", f"v2.0.0:{name}"], cwd=ROOT,
                                    capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("tag v2.0.0 is not available in this checkout")
        assert (V200 / name).read_bytes() == tagged, name


def test_v2_0_0_store_starts_on_a_v2_database(tmp_path):
    db = tmp_path / "gc.db"
    store.migrate(db)
    sid = _add_sample(db)
    store.sample_comments.add(sid, text="kept", source="free", author_initials="RB",
                              author_ip="10.0.0.5", revision=None, db=db)
    old = tmp_path / "old"
    shutil.copytree(V200, old)
    script = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {str(old)!r})
        import store
        assert store.__file__.startswith({str(old)!r}), store.__file__
        v = store.migrate({str(db)!r})
        sid = store.samples.insert_received("gc1", "40306", "2026-09-25 01:00:00", "cdf",
                                            cdf_sha256="sha-new", cdf_path="cdf/x.CDF",
                                            method_name="SIMDISB.M", db={str(db)!r})
        print(json.dumps({{"version": v, "sample": sid, "schema": store.SCHEMA_VERSION}}))
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=tmp_path, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["schema"] == 1 and got["version"] == 2
    # v2.0.0 left the v2 tables and their rows alone
    assert [c["text"] for c in store.sample_comments.list(sid, db=db)] == ["kept"]
    assert len(store.comment_presets.list(db=db)) == len(SEEDED)
    # and today's code is happy with the database v2.0.0 wrote to
    assert store.migrate(db) == 2


# ── helpers ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path) -> Path:
    path = tmp_path / "gc.db"
    store.migrate(path)
    return path


def test_comment_presets_helpers(db):
    pid = store.comment_presets.add("New preset", created_by="admin@1.2.3.4", db=db)
    p = store.comment_presets.get(pid, db=db)
    assert p["text"] == "New preset" and p["active"] == 1 and p["created_by"] == "admin@1.2.3.4"
    assert p["sort"] > max(q["sort"] for q in store.comment_presets.list(db=db) if q["id"] != pid)
    assert p["created_at"] == p["updated_at"]

    store.comment_presets.update(pid, text="Renamed", db=db)
    assert store.comment_presets.get(pid, db=db)["text"] == "Renamed"
    store.comment_presets.update(pid, active=0, db=db)
    assert pid not in [q["id"] for q in store.comment_presets.list(db=db)]
    assert pid in [q["id"] for q in store.comment_presets.list(include_inactive=True, db=db)]
    assert store.comment_presets.count_active(db=db) == len(SEEDED)
    with pytest.raises(ValueError):
        store.comment_presets.update(pid, colour="red", db=db)
    with pytest.raises(ValueError):
        store.comment_presets.update(99999, text="x", db=db)

    ids = [q["id"] for q in store.comment_presets.list(include_inactive=True, db=db)]
    store.comment_presets.reorder(list(reversed(ids)), db=db)
    assert [q["id"] for q in store.comment_presets.list(include_inactive=True, db=db)] == \
        list(reversed(ids))


def test_sample_comments_helpers(db):
    sid = _add_sample(db)
    other = _add_sample(db, lab_id="40400")
    a = store.sample_comments.add(sid, text="first", source="free", author_initials="RB",
                                  author_ip="10.0.0.5", revision=3, db=db)
    b = store.sample_comments.add(sid, text="span", source="annotation", t0=1.2, t1=1.5,
                                  author_initials="JD", author_ip="10.0.0.6", revision=3, db=db)
    store.sample_comments.add(other, text="elsewhere", source="free", author_initials="X",
                              author_ip=None, revision=None, db=db)
    rows = store.sample_comments.list(sid, db=db)
    assert [r["id"] for r in rows] == [a, b]
    assert rows[1]["t0"] == 1.2 and rows[1]["t1"] == 1.5 and rows[1]["source"] == "annotation"
    assert rows[0]["author_ip"] == "10.0.0.5" and rows[0]["revision"] == 3
    assert store.sample_comments.count_active(sid, db=db) == 2

    assert store.sample_comments.soft_delete(a, deleted_by_initials="JD",
                                             deleted_by_ip="10.0.0.7", db=db) is True
    assert store.sample_comments.soft_delete(a, deleted_by_initials="JD",
                                             deleted_by_ip="10.0.0.7", db=db) is False
    gone = store.sample_comments.get(a, db=db)          # the row is kept
    assert gone["deleted_at"] and gone["deleted_by_initials"] == "JD"
    assert gone["deleted_by_ip"] == "10.0.0.7" and gone["text"] == "first"
    assert [r["id"] for r in store.sample_comments.list(sid, db=db)] == [b]
    assert [r["id"] for r in store.sample_comments.list(sid, include_deleted=True, db=db)] == [a, b]
    assert store.sample_comments.count_active(sid, db=db) == 1
    with pytest.raises(ValueError):
        store.sample_comments.add(sid, text="x", source="bogus", author_initials="RB",
                                  author_ip=None, revision=None, db=db)
    with pytest.raises(sqlite3.IntegrityError):              # FK to samples
        store.sample_comments.add(987654, text="x", source="free", author_initials="RB",
                                  author_ip=None, revision=None, db=db)


def test_report_log_helpers(db):
    sid = _add_sample(db)
    rid = store.report_log.add(sid, kind="qbench", revision=2, standard_name="Diesel #2",
                               params_json='{"quantile": 0.2}', ranges_json="[]",
                               windows_json="[]", bullets_json="[]", bullets_text="• x",
                               conclusion="c", conclusion_edited=1, comment_ids_json="[1]",
                               app_version="v3.0.0", pdf_sha256="ab" * 32,
                               author_initials="RB", author_ip="10.0.0.5", db=db)
    rows = store.report_log.list(sid, db=db)
    assert [r["id"] for r in rows] == [rid]
    r = rows[0]
    assert r["kind"] == "qbench" and r["revision"] == 2 and r["conclusion_edited"] == 1
    assert r["created_at"] and r["pdf_sha256"] == "ab" * 32 and r["comment_ids_json"] == "[1]"
    with pytest.raises(ValueError):
        store.report_log.add(sid, kind="email", db=db)
    with pytest.raises(ValueError):
        store.report_log.add(sid, kind="zip", colour="red", db=db)


def test_seed_literals_are_quoted():
    assert store._sql_text("it's") == "'it''s'"
    assert store._sql_text("plain") == "'plain'"
