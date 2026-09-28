"""exports.HubExporter: fixes from the a1/exports critic review.

Ownership in the sidecar, stale not-found (SMB), cross-process exclusion,
write_fresh crash safety, adopt's seq rule, OSError handling, strict ledger
lines, verify-before-clear, periodic full hash, temp names, in-use paths.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import distill
import exports
import store
from exports_testlib import add_final, header_bytes, ledger_lines, make_db, pending

REPO = Path(__file__).resolve().parent.parent.parent


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


# ── 1. the sidecar names its instrument ────────────────────────────────────

def test_sidecar_records_the_instrument(exp, db):
    add_final(db)
    exp.flush("gc1")
    assert _sidecar(exp.export_path("gc1"))["instrument"] == "gc1"


def test_another_instruments_sidecar_is_refused(exp, db):
    add_final(db, "gc2")
    exp.flush("gc2")
    gc2_file = exp.export_path("gc2")
    before = gc2_file.read_bytes()
    # Point gc1 at gc2's file behind new_path's back (a hand-edited setting).
    store.instruments.upsert({"id": "gc1", "export_path": str(gc2_file)}, db=db)
    seq = add_final(db, "gc1")
    with pytest.raises(exports.ExportRefused) as ei:
        exp.flush("gc1")
    assert ei.value.reason == "foreign-sidecar"
    assert "gc2" in str(ei.value)
    assert pending(db, "gc1") == [seq]
    assert gc2_file.read_bytes() == before
    with pytest.raises(exports.ExportRefused) as ei:
        exp.adopt("gc1")
    assert ei.value.reason == "foreign-sidecar"
    assert gc2_file.read_bytes() == before


# ── 2. stale not-found (SMB maps a flapping share to FileNotFoundError) ────

def test_stale_not_found_never_appends_onto_existing_content(exp, db, monkeypatch):
    add_final(db)
    exp.flush("gc1")
    path = exp.export_path("gc1")
    before = path.read_bytes()
    side_before = exports.sidecar_path(path).read_bytes()
    add_final(db)
    monkeypatch.setattr(exports, "_stat", lambda p: None)
    monkeypatch.setattr(exports, "_read_sidecar", lambda p: None)
    res = exp.flush("gc1")
    assert res.error and res.pending == 1
    assert path.read_bytes() == before
    assert exports.sidecar_path(path).read_bytes() == side_before
    monkeypatch.undo()
    exp.flush("gc1")
    assert path.read_bytes() == header_bytes() + _lines(db)


def test_stale_not_found_on_a_v1_file_without_sidecar(exp, db, monkeypatch):
    path = exp.export_path("gc1")
    path.parent.mkdir(parents=True)
    v1 = header_bytes() + b"1,2,3\r\n"
    path.write_bytes(v1)
    add_final(db)
    monkeypatch.setattr(exports, "_stat", lambda p: None)
    res = exp.flush("gc1")
    assert res.error and res.pending == 1
    assert path.read_bytes() == v1
    monkeypatch.undo()
    with pytest.raises(exports.ExportRefused) as ei:
        exp.flush("gc1")
    assert ei.value.reason == "no-sidecar"
    assert path.read_bytes() == v1


# ── 3. cross-process and cross-thread exclusion ────────────────────────────

_CHILD = r"""
import sys, pathlib, time
sys.path.insert(0, sys.argv[1])
import exports
# Share latency: widen the verify -> append window the export lock must cover.
_real_open, _real_read = exports._open_append, exports._read_range
exports._open_append = lambda p: (time.sleep(0.005), _real_open(p))[1]
exports._read_range = lambda *a: (time.sleep(0.002), _real_read(*a))[1]
exp = exports.HubExporter(db=sys.argv[2], data_dir=sys.argv[3], retry_sleep=lambda s: None)
stop = pathlib.Path(sys.argv[4])
pathlib.Path(sys.argv[5]).write_text("ready")
n = 0
while True:
    last = stop.exists()
    try:
        r = exp.flush("gc1")
    except exports.ExportRefused as e:
        print("REFUSED", e)
        sys.exit(2)
    n += 1
    if last:
        break
print("flushes", n)
"""


def test_two_processes_flushing_write_each_row_once(db, tmp_path):
    data = tmp_path / "data"
    stop = tmp_path / "stop"
    env = dict(os.environ, PYTHONPATH=str(REPO))
    ready = [tmp_path / f"ready{i}" for i in range(2)]
    kids = [subprocess.Popen([sys.executable, "-c", _CHILD, str(REPO), str(db), str(data),
                              str(stop), str(r)], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True) for r in ready]
    try:
        deadline = time.monotonic() + 60
        while not all(r.exists() for r in ready) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert all(r.exists() for r in ready), "children did not start"
        for _ in range(60):
            add_final(db)
            time.sleep(0.01)
    finally:
        stop.write_text("stop")
        outs = [k.communicate(timeout=120)[0] for k in kids]
    for k, out in zip(kids, outs):
        assert k.returncode == 0, out
    exp = exports.HubExporter(db=db, data_dir=data)
    exp.flush("gc1")
    assert pending(db) == []
    assert exp.export_path("gc1").read_bytes() == header_bytes() + _lines(db)


def test_two_threads_flushing_write_each_row_once(db, tmp_path):
    exps = [exports.HubExporter(db=db, data_dir=tmp_path / "data", retry_sleep=lambda s: None)
            for _ in range(2)]
    stop = threading.Event()
    errors = []

    def run(e):
        while not stop.is_set():
            try:
                e.flush("gc1")
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
                return

    threads = [threading.Thread(target=run, args=(e,)) for e in exps]
    for t in threads:
        t.start()
    for _ in range(40):
        add_final(db)
    stop.set()
    for t in threads:
        t.join(30)
    assert errors == []
    exps[0].flush("gc1")
    assert exps[0].export_path("gc1").read_bytes() == header_bytes() + _lines(db)


def test_a_held_lock_is_a_retryable_error(exp, db, monkeypatch):
    add_final(db)
    exp.flush("gc1")
    add_final(db)
    monkeypatch.setattr(exports, "LOCK_WAIT_SECONDS", 0.2)
    path = exp.export_path("gc1")
    held = threading.Event()
    release = threading.Event()

    def holder():
        with exports._file_lock(path, 5):
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert held.wait(5)
        res = exp.flush("gc1")
        assert res.error and "lock" in res.error.lower() and res.pending == 1
    finally:
        release.set()
        t.join(10)
    assert exp.flush("gc1").appended == 1


# ── 4. write_fresh: a crash between switching and marking loses nothing ────

def test_write_fresh_crash_before_marking_loses_nothing(exp, db, tmp_path, monkeypatch):
    add_final(db)
    exp.flush("gc1")
    add_final(db)
    add_final(db)
    target = tmp_path / "fresh.csv"

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store.export_rows, "mark_hub_appended", staticmethod(boom))
    with pytest.raises(sqlite3.OperationalError):
        exp.write_fresh("gc1", target)
    monkeypatch.undo()
    assert exp.export_path("gc1") == target            # switched before marking
    fresh = target.read_bytes()
    assert fresh == header_bytes() + _lines(db)
    exp2 = exports.HubExporter(db=db, data_dir=tmp_path / "data")
    exp2.flush("gc1")
    assert pending(db) == []
    assert target.read_bytes() == fresh                 # nothing appended twice


# ── 5. adopt honours the old sidecar's seq only when it still describes the file ──

def _crash_before_mark(exp, db):
    """Leave the sidecar ahead of the ledger: rows written, not marked."""
    real = store.export_rows.mark_hub_appended

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    store.export_rows.mark_hub_appended = staticmethod(boom)
    try:
        res = exp.flush("gc1")
    finally:
        store.export_rows.mark_hub_appended = staticmethod(real)
    assert res.error
    return res


def test_adopt_honours_old_seq_when_the_prefix_still_matches(exp, db):
    add_final(db)
    exp.flush("gc1")
    s2 = add_final(db)
    _crash_before_mark(exp, db)
    path = exp.export_path("gc1")
    with path.open("ab") as fh:
        fh.write(b"outsider,row\r\n")
    assert exp.adopt("gc1")["seq"] == s2
    grown = path.read_bytes()
    exp.flush("gc1")
    assert pending(db) == []
    assert path.read_bytes() == grown                   # s2 was not written twice


def test_adopt_after_a_failed_sidecar_write_does_not_duplicate(exp, db, monkeypatch):
    """Rows appended, sidecar update failed, then an outsider appended and an
    admin adopted: the hub's own rows after the old size count as written."""
    add_final(db)
    exp.flush("gc1")
    add_final(db)
    add_final(db)

    def boom(*a, **k):
        raise OSError(5, "sidecar write failed")

    monkeypatch.setattr(exports, "_write_sidecar", boom)
    assert exp.flush("gc1").error
    monkeypatch.undo()
    path = exp.export_path("gc1")
    with path.open("ab") as fh:
        fh.write(b"outsider,row\r\n")
    adopted = path.read_bytes()
    exp.adopt("gc1")
    exp.flush("gc1")
    assert pending(db) == []
    assert path.read_bytes() == adopted
    for r in ledger_lines(db):
        assert adopted.count(r["line"].encode()) == 1


def test_adopt_ignores_old_seq_when_the_file_was_replaced(exp, db):
    s1 = add_final(db)
    exp.flush("gc1")
    add_final(db)
    _crash_before_mark(exp, db)
    path = exp.export_path("gc1")
    other = header_bytes() + b"x" * 30 + b"\r\n" + b"y" * 5000 + b"\r\n"
    path.write_bytes(other)                             # same name, different, larger file
    assert exp.adopt("gc1")["seq"] == s1


def test_adopt_ignores_old_seq_when_the_file_shrank(exp, db):
    s1 = add_final(db)
    exp.flush("gc1")
    add_final(db)
    _crash_before_mark(exp, db)
    path = exp.export_path("gc1")
    path.write_bytes(header_bytes())
    assert exp.adopt("gc1")["seq"] == s1


def test_adopt_without_folder_rights_is_refused_with_a_reason(exp, db, monkeypatch):
    path = exp.export_path("gc1")
    path.parent.mkdir(parents=True)
    path.write_bytes(header_bytes())

    def denied(*a, **k):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(exports, "_write_sidecar", denied)
    with pytest.raises(exports.ExportRefused) as ei:
        exp.adopt("gc1")
    assert ei.value.reason == "permission"
    assert "create/rename/delete rights in the folder" in str(ei.value)


# ── 6. every OSError in flush is a result, not an exception ────────────────

@pytest.mark.parametrize("target", ["_read_sidecar", "_stat", "_read_range", "_hash_prefix"])
def test_oserrors_anywhere_in_flush_are_returned(exp, db, monkeypatch, tmp_path, clock, target):
    add_final(db)
    exp.flush("gc1")
    add_final(db)
    exp = exports.HubExporter(db=db, data_dir=tmp_path / "data", clock=clock)  # cold cache

    def fail(*a, **k):
        raise OSError(59, "An unexpected network error occurred")

    monkeypatch.setattr(exports, target, fail)
    res = exp.flush("gc1")
    assert res.error and "network" in res.error and res.pending == 1
    st = exp.status("gc1")
    assert st["last_error"] == res.error and st["pending_since"] is not None


def test_a_landed_check_oserror_is_returned(exp, db, monkeypatch):
    add_final(db)
    exp.flush("gc1")
    add_final(db)
    real_stat = exports._stat
    calls = []

    def flaky(p):
        calls.append(p)
        if len(calls) >= 2:          # after the append
            raise OSError(64, "network name no longer available")
        return real_stat(p)

    monkeypatch.setattr(exports, "_stat", flaky)
    res = exp.flush("gc1")
    assert res.error
    monkeypatch.undo()
    exp.flush("gc1")
    assert pending(db) == []
    assert exp.export_path("gc1").read_bytes() == header_bytes() + _lines(db)


# ── minor ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line", ["a,b\r\n", ",".join(["x"] * 31) + "\n",
                                  ",".join(["x"] * 31) + "\r\n" + "y\r\n"])
def test_bad_lines_are_refused(exp, db, line):
    seq = add_final(db)
    with store.connection(db) as conn:
        conn.execute('UPDATE export_rows SET "row"=? WHERE seq=?', (line, seq))
    with pytest.raises(exports.ExportRefused) as ei:
        exp.flush("gc1")
    assert ei.value.reason == "bad-line"


def test_a_line_with_a_quoted_newline_is_one_record(exp, db):
    results = {"Lab ID": "40305", "InjectionDateTime": "2026-09-25 00:00:00",
               "Best Fit": "two\r\nlines"}
    seq = add_final(db)
    with store.connection(db) as conn:
        conn.execute('UPDATE export_rows SET "row"=? WHERE seq=?',
                     (exports.format_line(results, "s"), seq))
    assert exp.flush("gc1").appended == 1


def test_nothing_pending_still_verifies_before_clearing_a_refusal(exp, db):
    add_final(db)
    exp.flush("gc1")
    path = exp.export_path("gc1")
    with path.open("ab") as fh:
        fh.write(b"outsider\r\n")
    with pytest.raises(exports.ExportRefused) as ei:
        exp.flush("gc1")                               # nothing pending, still checked
    assert ei.value.reason == "grown"
    assert exp.status("gc1")["refused"] == "grown"
    with pytest.raises(exports.ExportRefused):
        exp.flush("gc1")
    assert exp.status("gc1")["refused"] == "grown"
    exp.adopt("gc1")
    assert exp.flush("gc1").error is None
    assert exp.status("gc1")["refused"] is None


def test_tick_does_a_full_hash_at_most_once_a_day(exp, db, clock, monkeypatch):
    add_final(db)
    exp.flush("gc1")
    full = []
    real = exports._hash_prefix
    monkeypatch.setattr(exports, "_hash_prefix", lambda p, n: (full.append(n), real(p, n))[1])
    clock.t += 3600
    exp.tick()
    assert full == []
    clock.t += 23 * 3600 + 1
    exp.tick()
    assert len(full) == 1
    clock.t += 3600
    exp.tick()
    assert len(full) == 1


def test_verify_full_catches_a_hidden_middle_edit(exp, db):
    for _ in range(3):
        add_final(db)
    exp.flush("gc1")
    path = exp.export_path("gc1")
    st = path.stat()
    data = bytearray(path.read_bytes())
    data[len(header_bytes()) + 3] ^= 1
    path.write_bytes(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    with pytest.raises(exports.ExportRefused) as ei:
        exp.verify_full("gc1")
    assert ei.value.reason == "changed"


def test_sidecar_temp_names_escape_v1s_sweep(tmp_path, monkeypatch):
    csv_path = tmp_path / "distill_results.csv"
    seen = []
    real = distill._replace_retrying

    def spy(tmp, dest):
        seen.append(Path(tmp).name)
        real(tmp, dest)

    monkeypatch.setattr(distill, "_replace_retrying", spy)
    exports._write_sidecar(csv_path, {"size": 0})
    v1_glob = distill._csv_temp_prefix(csv_path) + "*.tmp"
    assert seen and not fnmatch.fnmatch(seen[0], v1_glob)


def test_new_path_refuses_another_instruments_file(exp, db, tmp_path):
    shared = tmp_path / "shared.csv"
    exp.new_path("gc2", shared)
    with pytest.raises(exports.ExportRefused) as ei:
        exp.new_path("gc1", shared)
    assert ei.value.reason == "in-use"
    with pytest.raises(exports.ExportRefused) as ei:
        exp.new_path("gc1", exp.export_path("gc2").parent / "." / "shared.csv")
    assert ei.value.reason == "in-use"
    with pytest.raises(exports.ExportRefused):
        exp.write_fresh("gc1", shared)
    assert store.instruments.get("gc1", db=db)["export_path"] in (None, "")


def test_grown_by_v1_writing_the_same_sample_is_refused(exp, db):
    add_final(db)
    exp.flush("gc1")
    seq = add_final(db)
    row = [r for r in ledger_lines(db) if r["seq"] == seq][0]
    results = json.loads(store.get_revision(row["sample_id"], db=db)["results"])
    path = exp.export_path("gc1")
    v1_source = r"\\ASAPServer\Labsharedrive\processed_cdf\x.CDF"
    distill._append_csv_row(path, [results.get(c, "") for c in distill.CSV_HEADER[:-1]]
                            + [v1_source])
    before = path.read_bytes()
    with pytest.raises(exports.ExportRefused) as ei:
        exp.flush("gc1")
    assert ei.value.reason == "grown"
    assert path.read_bytes() == before
    assert pending(db) == [seq]
