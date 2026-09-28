"""exports.HubExporter: fixes from the a1/exports re-review.

The sidecar is linked to the ledger (db_id + the last line's hash), the
append handle is checked against the verified offset, writes are chunked,
admin calls don't stall, and the daily full check backs off on failure.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import exports
import store
from exports_testlib import add_final, header_bytes, ledger_lines, make_db, pending


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def db(tmp_path) -> Path:
    return make_db(tmp_path)


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def exp(db, tmp_path, clock):
    return exports.HubExporter(db=db, data_dir=tmp_path / "data", clock=clock,
                               retry_sleep=lambda s: None)


def _lines(db, instrument="gc1") -> bytes:
    return b"".join(r["line"].encode("utf-8") for r in ledger_lines(db, instrument))


def _sidecar(path: Path) -> dict:
    return json.loads(exports.sidecar_path(path).read_text(encoding="utf-8"))


def _backup(db: Path, dest: Path) -> None:
    src = sqlite3.connect(db)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()


def _restore(backup: Path, db: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    shutil.copyfile(backup, db)


# ── 1. the sidecar is linked to the ledger ─────────────────────────────────

def test_sidecar_carries_db_id_and_last_line_hash(exp, db):
    add_final(db)
    last = add_final(db)
    exp.flush("gc1")
    side = _sidecar(exp.export_path("gc1"))
    db_id = store.settings_kv.get("exports_db_id", db=db)
    assert db_id and side["db_id"] == db_id
    import hashlib
    line = [r for r in ledger_lines(db) if r["seq"] == last][0]["line"].encode()
    assert side["last_line_sha256"] == hashlib.sha256(line).hexdigest()


def test_db_id_is_created_once(exp, db, tmp_path):
    add_final(db)
    exp.flush("gc1")
    first = store.settings_kv.get("exports_db_id", db=db)
    exp2 = exports.HubExporter(db=db, data_dir=tmp_path / "data")
    add_final(db)
    exp2.flush("gc1")
    assert store.settings_kv.get("exports_db_id", db=db) == first


def test_restored_database_is_refused_not_silently_marked(exp, db, tmp_path):
    """The critic's probe: flush 1-5, restore the backup taken after 2, add a
    new row. Its seq (3) is <= the sidecar's (5), but it is NOT in the file."""
    backup = tmp_path / "backup.db"
    for i in range(5):
        add_final(db)
        exp.flush("gc1")
        if i == 1:
            _backup(db, backup)
    path = exp.export_path("gc1")
    before = path.read_bytes()
    _restore(backup, db)
    new = add_final(db, lab_id="NEWROW")
    assert new <= _sidecar(path)["seq"]
    exp2 = exports.HubExporter(db=db, data_dir=tmp_path / "data")
    with pytest.raises(exports.ExportRefused) as ei:
        exp2.flush("gc1")
    assert ei.value.reason == "ledger-mismatch"
    assert "restored from a backup" in str(ei.value)
    assert pending(db) == [new]
    assert path.read_bytes() == before


def test_another_database_is_refused(exp, db, tmp_path):
    add_final(db)
    exp.flush("gc1")
    other = make_db(tmp_path / "other")
    store.instruments.upsert({"id": "gc1", "export_path": str(exp.export_path("gc1"))}, db=other)
    add_final(other)
    with pytest.raises(exports.ExportRefused) as ei:
        exports.HubExporter(db=other, data_dir=tmp_path / "data").flush("gc1")
    assert ei.value.reason == "ledger-mismatch"


def test_a_rewritten_last_ledger_line_is_refused(exp, db):
    s1 = add_final(db)
    exp.flush("gc1")
    with store.connection(db) as conn:
        conn.execute('UPDATE export_rows SET "row"=? WHERE seq=?',
                     (exports.format_line({"Lab ID": "other"}, "x"), s1))
    add_final(db)
    with pytest.raises(exports.ExportRefused) as ei:
        exp.flush("gc1")
    assert ei.value.reason == "ledger-mismatch"


def test_adopt_does_not_keep_a_seq_the_ledger_cannot_vouch_for(exp, db, tmp_path):
    backup = tmp_path / "backup.db"
    s1 = add_final(db)
    exp.flush("gc1")
    _backup(db, backup)
    for _ in range(3):
        add_final(db)
    exp.flush("gc1")
    _restore(backup, db)
    new = add_final(db, lab_id="NEWROW")
    exp2 = exports.HubExporter(db=db, data_dir=tmp_path / "data")
    side = exp2.adopt("gc1")
    assert side["seq"] == s1                       # not the old sidecar's 4
    exp2.flush("gc1")
    assert pending(db) == []
    line = [r for r in ledger_lines(db) if r["seq"] == new][0]["line"].encode()
    assert exp2.export_path("gc1").read_bytes().endswith(line)


def test_write_fresh_crash_recovery_still_works_with_the_ledger_link(exp, db, tmp_path,
                                                                    monkeypatch):
    add_final(db)
    add_final(db)

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store.export_rows, "mark_hub_appended", staticmethod(boom))
    with pytest.raises(sqlite3.OperationalError):
        exp.write_fresh("gc1", tmp_path / "fresh.csv")
    monkeypatch.undo()
    exp.flush("gc1")
    assert pending(db) == []
    assert (tmp_path / "fresh.csv").read_bytes() == header_bytes() + _lines(db)


# ── 2. the append handle must be at the verified offset ────────────────────

def test_an_append_between_verify_and_open_is_not_written_after(exp, db, monkeypatch):
    add_final(db)
    exp.flush("gc1")
    add_final(db)
    path = exp.export_path("gc1")
    real = exports._open_append
    sneaked = []

    def sneaky(p):
        if not sneaked:
            with open(p, "ab") as fh:
                fh.write(b"v1,sneaked,in\r\n")
            sneaked.append(1)
        return real(p)

    monkeypatch.setattr(exports, "_open_append", sneaky)
    with pytest.raises(exports.ExportRefused) as ei:
        exp.flush("gc1")
    assert ei.value.reason == "grown"
    assert path.read_bytes().endswith(b"v1,sneaked,in\r\n")   # nothing written after it
    assert len(pending(db)) == 1


def test_the_empty_file_branch_checks_the_offset_too(exp, db, monkeypatch):
    path = exp.export_path("gc1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    add_final(db)
    real = exports._open_append

    def sneaky(p):
        with open(p, "ab") as fh:
            fh.write(b"x\r\n")
        return real(p)

    monkeypatch.setattr(exports, "_open_append", sneaky)
    with pytest.raises(exports.ExportRefused):
        exp.flush("gc1")
    assert header_bytes() not in path.read_bytes()


# ── 3. chunked, unbuffered writes ──────────────────────────────────────────

class _Recorder:
    def __init__(self, path, max_write=None):
        self.fh = open(path, "ab", buffering=0)
        self.max_write = max_write
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.fh.close()

    def write(self, data):
        data = bytes(data)
        n = len(data) if self.max_write is None else min(len(data), self.max_write)
        self.calls.append(data[:n])
        return self.fh.write(data[:n])

    def fileno(self):
        return self.fh.fileno()

    def flush(self):
        pass


def _many_rows(db, exp, n=400):
    add_final(db)
    exp.flush("gc1")                  # the file exists; later appends use _open_append
    for _ in range(n):
        add_final(db)


def test_writes_are_at_most_64k_on_row_boundaries_with_one_fsync(exp, db, monkeypatch):
    _many_rows(db, exp)
    recs = []
    monkeypatch.setattr(exports, "_open_append",
                        lambda p: recs.append(_Recorder(p)) or recs[-1])
    fsyncs = []
    real_fsync = os.fsync
    monkeypatch.setattr(exports.os, "fsync", lambda fd: (fsyncs.append(fd), real_fsync(fd)))
    assert exp.flush("gc1").appended == 400
    calls = recs[0].calls
    assert len(calls) > 1
    assert all(len(c) <= exports.WRITE_CHUNK_BYTES for c in calls)
    assert all(c.endswith(b"\r\n") for c in calls)
    assert len(fsyncs) == 1 + 1        # the data file once, the sidecar temp once
    assert exp.export_path("gc1").read_bytes() == header_bytes() + _lines(db)


def test_short_writes_are_continued(exp, db, monkeypatch):
    _many_rows(db, exp, 40)
    monkeypatch.setattr(exports, "_open_append", lambda p: _Recorder(p, max_write=1000))
    assert exp.flush("gc1").appended == 40
    assert exp.export_path("gc1").read_bytes() == header_bytes() + _lines(db)


def test_default_handles_are_unbuffered(tmp_path):
    p = tmp_path / "f.csv"
    with exports._open_new(p) as fh:
        assert isinstance(fh, __import__("io").FileIO)
    with exports._open_append(p) as fh:
        assert isinstance(fh, __import__("io").FileIO)


# ── minor ───────────────────────────────────────────────────────────────────

def test_admin_calls_do_not_stall_on_a_held_lock(exp, db, tmp_path):
    path = exp.export_path("gc1")
    path.parent.mkdir(parents=True)
    path.write_bytes(header_bytes())
    held, release = threading.Event(), threading.Event()

    def holder():
        with exports._file_lock(path, 5):
            held.set()
            release.wait(20)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert held.wait(5)
        t0 = time.monotonic()
        with pytest.raises(exports.ExportRefused) as ei:
            exp.adopt("gc1")
        assert ei.value.reason == "busy"
        assert time.monotonic() - t0 < exports.ADMIN_LOCK_WAIT_SECONDS + 2
    finally:
        release.set()
        t.join(10)


def test_a_transient_not_found_is_read_again_before_refusing(exp, db, monkeypatch):
    add_final(db)
    exp.flush("gc1")
    add_final(db)
    real = exports._read_sidecar
    calls = []

    def flap(p):
        calls.append(p)
        return None if len(calls) == 1 else real(p)

    monkeypatch.setattr(exports, "_read_sidecar", flap)
    res = exp.flush("gc1")
    assert res.error is None and res.appended == 1


def test_verify_full_failures_back_off(exp, db, clock, monkeypatch):
    add_final(db)
    exp.flush("gc1")
    calls = []

    def failing(p, n):
        calls.append(n)
        raise OSError(59, "network error")

    monkeypatch.setattr(exports, "_hash_prefix", failing)
    clock.t += exports.FULL_VERIFY_SECONDS + 1
    exp.tick()
    assert len(calls) == 1
    clock.t += 60
    exp.tick()
    assert len(calls) == 1                      # not every tick
    clock.t += exports.FULL_VERIFY_RETRY_SECONDS
    exp.tick()
    assert len(calls) == 2


def test_the_cheap_check_uses_the_mtime_this_process_saw(exp, db, monkeypatch):
    """A sidecar written by another process (different mtime_ns) does not
    matter: the cache remembers what *this* process verified."""
    add_final(db)
    exp.flush("gc1")
    path = exp.export_path("gc1")
    side = _sidecar(path)
    side["mtime_ns"] = 1
    exports._write_sidecar(path, side)
    full = []
    real = exports._hash_prefix
    monkeypatch.setattr(exports, "_hash_prefix", lambda p, n: (full.append(n), real(p, n))[1])
    add_final(db)
    exp.flush("gc1")
    assert full == []


def test_docs_and_rights_text():
    doc = exports.__doc__
    assert "only on the Windows hub" in doc and "ignores locks is unsupported" in doc
    assert "lock file" in exports.FOLDER_RIGHTS_TEXT


def test_windows_ci_workflow_runs_the_export_tests():
    wf = (Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"
          / "exports-ci.yml").read_text(encoding="utf-8")
    for needle in ("windows-latest", "3.14", "pytest tests/exports -q", "contents: read",
                   "persist-credentials: false", "timeout-minutes: 20"):
        assert needle in wf, needle
