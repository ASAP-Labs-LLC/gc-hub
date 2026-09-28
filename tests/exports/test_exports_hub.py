"""exports.HubExporter: append-only results CSV with a sidecar (spec D8, D9b).

Every test gets its own store and data folder under ``tmp_path``. Nothing
here imports ``app``.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

import store
import exports
from exports_testlib import add_final, header_bytes, ledger_lines, make_db, pending


# ── fixtures ────────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def db(tmp_path) -> Path:
    return make_db(tmp_path)


@pytest.fixture()
def notes() -> list:
    return []


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def exp(db, tmp_path, notes, clock):
    return exports.HubExporter(db=db, data_dir=tmp_path / "data",
                               notifier=lambda level, msg: notes.append((level, msg)),
                               clock=clock, retry_sleep=lambda s: None)


def _lines(db, instrument="gc1") -> bytes:
    return b"".join(r["line"].encode("utf-8") for r in ledger_lines(db, instrument))


def _sidecar(path: Path) -> dict:
    return json.loads(exports.sidecar_path(path).read_text(encoding="utf-8"))


def _write_v1_file(path: Path, n_rows: int = 3, *, trailing_newline: bool = True) -> bytes:
    body = header_bytes() + b"".join(
        f"v1-{i},2026-09-01 00:00:0{i}".encode() + b"," * 29 + b"\r\n" for i in range(n_rows))
    if not trailing_newline:
        body = body[:-2]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return body


# ── paths ───────────────────────────────────────────────────────────────────

def test_default_export_path_is_under_data_results(exp, tmp_path):
    assert exp.export_path("gc1") == tmp_path / "data" / "results" / "gc1_results.csv"


def test_instrument_export_path_wins(exp, db, tmp_path):
    target = tmp_path / "share" / "LEVEL 1" / "distill_results.csv"
    store.instruments.upsert({"id": "gc1", "export_path": str(target)}, db=db)
    assert exp.export_path("gc1") == target


def test_sidecar_path_appends_suffix(tmp_path):
    p = tmp_path / "distill_results.csv"
    assert exports.sidecar_path(p) == tmp_path / "distill_results.csv.gchub.json"


def test_no_data_dir_and_no_export_path_is_an_error(db, monkeypatch):
    monkeypatch.delenv("GC_DATA_DIR", raising=False)
    exp = exports.HubExporter(db=db)
    with pytest.raises(RuntimeError):
        exp.export_path("gc1")


# ── appending ───────────────────────────────────────────────────────────────

def test_flush_writes_header_then_rows_in_seq_order(exp, db):
    seqs = [add_final(db) for _ in range(3)]
    res = exp.flush("gc1")
    path = exp.export_path("gc1")
    assert res.appended == 3 and res.pending == 0 and res.error is None
    assert path.read_bytes() == header_bytes() + _lines(db)
    assert pending(db) == []
    side = _sidecar(path)
    data = path.read_bytes()
    assert side["size"] == len(data)
    assert side["sha256"] == hashlib.sha256(data).hexdigest()
    assert side["seq"] == max(seqs)


def test_header_is_written_once(exp, db):
    add_final(db)
    exp.flush("gc1")
    add_final(db)
    add_final(db)
    exp.flush("gc1")
    data = exp.export_path("gc1").read_bytes()
    assert data.count(header_bytes()) == 1
    assert data == header_bytes() + _lines(db)


def test_flush_with_nothing_pending_does_not_touch_the_file(exp, db):
    assert exp.flush("gc1").appended == 0
    assert not exp.export_path("gc1").exists()


def test_instruments_are_kept_apart_and_ordered(exp, db):
    for inst in ["gc1", "gc2", "gc1", "gc2", "gc2", "gc1"]:
        add_final(db, inst)
    exp.flush("gc2")
    exp.flush("gc1")
    for inst in ("gc1", "gc2"):
        data = exp.export_path(inst).read_bytes()
        assert data == header_bytes() + _lines(db, inst)
        assert f"cdf/{'gc2' if inst == 'gc1' else 'gc1'}/".encode() not in data


def test_rows_appended_after_a_flush_go_after_the_earlier_ones(exp, db):
    first = add_final(db)
    exp.flush("gc1")
    second = add_final(db)
    exp.flush("gc1")
    data = exp.export_path("gc1").read_bytes()
    lines = {r["seq"]: r["line"].encode() for r in ledger_lines(db)}
    assert data.index(lines[first]) < data.index(lines[second])
    assert _sidecar(exp.export_path("gc1"))["seq"] == second


def test_empty_existing_file_without_sidecar_gets_the_header(exp, db):
    path = exp.export_path("gc1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    add_final(db)
    exp.flush("gc1")
    assert path.read_bytes() == header_bytes() + _lines(db)


def test_sidecar_write_leaves_no_temp_files(exp, db):
    add_final(db)
    exp.flush("gc1")
    add_final(db)
    exp.flush("gc1")
    folder = exp.export_path("gc1").parent
    assert sorted(p.name for p in folder.iterdir()) == ["gc1_results.csv",
                                                        "gc1_results.csv.gchub.json",
                                                        "gc1_results.csv.gchub.lock"]


def test_a_ledger_line_without_terminator_is_refused(exp, db):
    seq = add_final(db)
    with store.connection(db) as conn:
        conn.execute('UPDATE export_rows SET "row"=? WHERE seq=?', ("no,newline", seq))
    with pytest.raises(exports.ExportRefused) as ei:
        exp.flush("gc1")
    assert ei.value.reason == "bad-line"
    assert not exp.export_path("gc1").exists()
    assert pending(db) == [seq]


# ── refusals ────────────────────────────────────────────────────────────────

def _refused(exp, reason):
    with pytest.raises(exports.ExportRefused) as ei:
        exp.flush("gc1")
    assert ei.value.reason == reason, str(ei.value)
    assert str(exp.export_path("gc1")) in str(ei.value)
    return ei.value


def test_refuses_an_existing_file_without_sidecar(exp, db):
    path = exp.export_path("gc1")
    before = _write_v1_file(path)
    add_final(db)
    err = _refused(exp, "no-sidecar")
    assert "adopt" in str(err).lower()
    assert path.read_bytes() == before
    assert len(pending(db)) == 1


def test_refuses_a_file_grown_by_someone_else(exp, db):
    add_final(db)
    exp.flush("gc1")
    path = exp.export_path("gc1")
    with path.open("ab") as fh:
        fh.write(b"someone,else\r\n")
    before = path.read_bytes()
    add_final(db)
    _refused(exp, "grown")
    assert path.read_bytes() == before
    assert len(pending(db)) == 1


def test_refuses_a_shrunk_file(exp, db):
    add_final(db)
    add_final(db)
    exp.flush("gc1")
    path = exp.export_path("gc1")
    path.write_bytes(path.read_bytes()[:-10])
    before = path.read_bytes()
    add_final(db)
    _refused(exp, "shrunk")
    assert path.read_bytes() == before


def test_refuses_a_changed_file_of_the_same_size(exp, db):
    add_final(db)
    exp.flush("gc1")
    path = exp.export_path("gc1")
    data = bytearray(path.read_bytes())
    data[-5] = ord("X") if data[-5] != ord("X") else ord("Y")
    path.write_bytes(bytes(data))
    add_final(db)
    _refused(exp, "changed")
    assert path.read_bytes() == bytes(data)


def test_refuses_when_the_file_is_gone_but_the_sidecar_is_not(exp, db):
    add_final(db)
    exp.flush("gc1")
    exp.export_path("gc1").unlink()
    add_final(db)
    _refused(exp, "missing")
    assert not exp.export_path("gc1").exists()


def test_refuses_an_unreadable_sidecar(exp, db):
    add_final(db)
    exp.flush("gc1")
    exports.sidecar_path(exp.export_path("gc1")).write_text("{not json", encoding="utf-8")
    add_final(db)
    _refused(exp, "sidecar-unreadable")


def test_a_refusal_notifies_once_not_every_flush(exp, db, notes):
    _write_v1_file(exp.export_path("gc1"))
    add_final(db)
    for _ in range(3):
        with pytest.raises(exports.ExportRefused):
            exp.flush("gc1")
    refusals = [n for n in notes if "refus" in n[1].lower()]
    assert len(refusals) == 1
    assert refusals[0][0] == "error"
    assert "gc1" in refusals[0][1]
    assert exp.status("gc1")["refused"] == "no-sidecar"


# ── adopt ───────────────────────────────────────────────────────────────────

def test_adopt_records_the_current_state_and_appends_after_it(exp, db):
    path = exp.export_path("gc1")
    before = _write_v1_file(path)
    add_final(db)
    side = exp.adopt("gc1", by="admin")
    assert side["size"] == len(before)
    assert side["sha256"] == hashlib.sha256(before).hexdigest()
    assert _sidecar(path)["size"] == len(before)
    exp.flush("gc1")
    assert path.read_bytes() == before + _lines(db)      # no second header
    assert pending(db) == []


def test_adopt_clears_a_refusal(exp, db):
    add_final(db)
    exp.flush("gc1")
    path = exp.export_path("gc1")
    with path.open("ab") as fh:
        fh.write(b"v1,still,running\r\n")
    add_final(db)
    with pytest.raises(exports.ExportRefused):
        exp.flush("gc1")
    grown = path.read_bytes()
    exp.adopt("gc1")
    assert exp.status("gc1")["refused"] is None
    exp.flush("gc1")
    assert path.read_bytes().startswith(grown)
    assert pending(db) == []


def test_adopt_refuses_a_wrong_header(exp, db):
    path = exp.export_path("gc1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"Lab ID,Something else\r\n1,2\r\n")
    with pytest.raises(exports.ExportRefused) as ei:
        exp.adopt("gc1")
    assert ei.value.reason == "header-mismatch"
    assert not exports.sidecar_path(path).exists()


def test_adopt_names_a_bom_in_the_header(exp, db):
    path = exp.export_path("gc1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf" + header_bytes())
    with pytest.raises(exports.ExportRefused) as ei:
        exp.adopt("gc1")
    assert ei.value.reason == "header-mismatch"
    assert "BOM" in str(ei.value)


def test_adopt_refuses_a_last_line_without_newline(exp, db):
    path = exp.export_path("gc1")
    before = _write_v1_file(path, trailing_newline=False)
    with pytest.raises(exports.ExportRefused) as ei:
        exp.adopt("gc1")
    assert ei.value.reason == "no-trailing-newline"
    assert "newline" in str(ei.value)
    assert path.read_bytes() == before
    assert not exports.sidecar_path(path).exists()


def test_adopt_refuses_a_missing_file(exp):
    with pytest.raises(exports.ExportRefused) as ei:
        exp.adopt("gc1")
    assert ei.value.reason == "missing"


def test_adopt_of_an_empty_file_then_header(exp, db):
    path = exp.export_path("gc1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    exp.adopt("gc1")
    add_final(db)
    exp.flush("gc1")
    assert path.read_bytes() == header_bytes() + _lines(db)


def test_adopt_keeps_seq_at_the_last_appended_row(exp, db):
    s1 = add_final(db)
    exp.flush("gc1")
    path = exp.export_path("gc1")
    with path.open("ab") as fh:
        fh.write(b"x\r\n")
    add_final(db)
    assert exp.adopt("gc1")["seq"] == s1


# ── new path, write fresh ──────────────────────────────────────────────────

def test_new_path_switches_the_export(exp, db, tmp_path):
    add_final(db)
    exp.flush("gc1")
    old = exp.export_path("gc1")
    old_bytes = old.read_bytes()
    target = tmp_path / "elsewhere" / "gc1_new.csv"
    target.parent.mkdir()
    assert exp.new_path("gc1", target) == target
    assert store.instruments.get("gc1", db=db)["export_path"] == str(target)
    s2 = add_final(db)
    exp.flush("gc1")
    assert old.read_bytes() == old_bytes
    line = [r for r in ledger_lines(db) if r["seq"] == s2][0]["line"].encode()
    assert target.read_bytes() == header_bytes() + line


def test_new_path_clears_a_refusal(exp, db, tmp_path):
    _write_v1_file(exp.export_path("gc1"))
    add_final(db)
    with pytest.raises(exports.ExportRefused):
        exp.flush("gc1")
    exp.new_path("gc1", tmp_path / "fresh.csv")
    assert exp.status("gc1")["refused"] is None
    exp.flush("gc1")
    assert pending(db) == []


def test_write_fresh_refuses_an_existing_path(exp, db, tmp_path):
    target = tmp_path / "exists.csv"
    target.write_bytes(b"keep me")
    with pytest.raises(exports.ExportRefused) as ei:
        exp.write_fresh("gc1", target)
    assert ei.value.reason == "exists"
    assert target.read_bytes() == b"keep me"


def test_write_fresh_writes_gated_current_revisions_and_switches(exp, db, tmp_path):
    a = add_final(db)
    exp.flush("gc1")
    b = add_final(db)                               # still pending
    add_final(db, "gc2")                            # other instrument
    # A backfill sample that is final but not released is not gated.
    c = add_final(db)
    rows = {r["seq"]: r for r in ledger_lines(db)}
    store.samples.update(rows[c]["sample_id"], backfill=1, db=db)
    target = tmp_path / "fresh" / "gc1_fresh.csv"
    target.parent.mkdir()
    n = exp.write_fresh("gc1", target)
    assert n == 2
    assert target.read_bytes() == header_bytes() + b"".join(
        rows[s]["line"].encode() for s in (a, b))
    assert exp.export_path("gc1") == target
    side = _sidecar(target)
    assert side["size"] == target.stat().st_size
    # The ledger is covered up to the snapshot: nothing is appended twice.
    exp.flush("gc1")
    assert target.read_bytes() == header_bytes() + b"".join(
        rows[s]["line"].encode() for s in (a, b))
    assert pending(db) == []


def test_write_fresh_uses_the_latest_revision(exp, db, tmp_path):
    seq = add_final(db)
    sid = [r for r in ledger_lines(db) if r["seq"] == seq][0]["sample_id"]
    newer = {"Lab ID": "40999", "InjectionDateTime": "2026-09-25 01:00:00", "2887 IBP": 1.5}
    with store.connection(db) as conn, store.write_txn(conn):
        rev = store.add_revision(conn, sid, newer, reason="reprocess")
        store.export_rows.append_pending(conn, "gc1", sid, rev, exports.format_line(newer, "p.CDF"))
    target = tmp_path / "f.csv"
    assert exp.write_fresh("gc1", target) == 1
    assert target.read_bytes() == header_bytes() + exports.format_line(newer, "p.CDF").encode()


# ── retry: locks, partial writes, crashes ──────────────────────────────────

def test_a_locked_file_leaves_rows_pending_and_is_retried(exp, db, monkeypatch):
    add_final(db)
    exp.flush("gc1")
    path = exp.export_path("gc1")
    before = path.read_bytes()
    add_final(db)
    real = exports._open_append
    calls = []

    def locked(p):
        calls.append(p)
        raise PermissionError(13, "The process cannot access the file", str(p))

    monkeypatch.setattr(exports, "_open_append", locked)
    res = exp.flush("gc1")
    assert res.appended == 0 and res.pending == 1
    assert "PermissionError" in res.error or "cannot access" in res.error
    assert len(calls) >= 2                          # a short in-call retry first
    assert path.read_bytes() == before
    monkeypatch.setattr(exports, "_open_append", real)
    res = exp.flush("gc1")
    assert res.appended == 1 and res.error is None
    assert path.read_bytes() == header_bytes() + _lines(db)


def test_a_missing_share_folder_is_a_retryable_error(exp, db, tmp_path):
    store.instruments.upsert({"id": "gc1", "export_path": str(tmp_path / "nope" / "r.csv")}, db=db)
    add_final(db)
    res = exp.flush("gc1")
    assert res.error and res.pending == 1
    assert not (tmp_path / "nope").exists()


class _PartialWriter:
    """A file object that writes only the first ``n`` bytes, then fails."""

    def __init__(self, path, n):
        self.fh = open(path, "ab")
        self.n = n

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.fh.close()

    def write(self, data):
        self.fh.write(data[: self.n])
        self.fh.flush()
        raise OSError(64, "The specified network name is no longer available")

    def fileno(self):
        return self.fh.fileno()

    def flush(self):
        self.fh.flush()


def test_a_partial_append_is_completed_not_duplicated(exp, db, monkeypatch):
    add_final(db)
    exp.flush("gc1")
    add_final(db)
    add_final(db)
    real = exports._open_append
    monkeypatch.setattr(exports, "_open_append", lambda p: _PartialWriter(p, 50))
    res = exp.flush("gc1")
    assert res.error and res.pending == 2
    monkeypatch.setattr(exports, "_open_append", real)
    res = exp.flush("gc1")
    assert res.error is None and res.pending == 0
    assert exp.export_path("gc1").read_bytes() == header_bytes() + _lines(db)


def test_a_crash_after_append_before_sidecar_does_not_duplicate(exp, db, monkeypatch, tmp_path,
                                                                notes, clock):
    add_final(db)
    exp.flush("gc1")
    add_final(db)
    monkeypatch.setattr(exports, "_write_sidecar",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(5, "boom")))
    res = exp.flush("gc1")
    assert res.error
    monkeypatch.undo()
    # A restart: a fresh exporter with no in-memory state.
    exp2 = exports.HubExporter(db=db, data_dir=tmp_path / "data", notifier=None, clock=clock)
    res = exp2.flush("gc1")
    assert res.error is None and pending(db) == []
    assert exp2.export_path("gc1").read_bytes() == header_bytes() + _lines(db)


def test_a_crash_after_sidecar_before_marking_does_not_duplicate(exp, db, monkeypatch, tmp_path,
                                                                 clock):
    add_final(db)
    exp.flush("gc1")
    add_final(db)
    monkeypatch.setattr(store.export_rows, "mark_hub_appended",
                        staticmethod(lambda *a, **k: (_ for _ in ()).throw(OSError(5, "db"))))
    res = exp.flush("gc1")                          # returned, not raised (review item 6)
    assert res.error and "db" in res.error
    monkeypatch.undo()
    assert len(pending(db)) == 1
    exp2 = exports.HubExporter(db=db, data_dir=tmp_path / "data", clock=clock)
    res = exp2.flush("gc1")
    assert res.appended == 0 and pending(db) == []
    assert exp2.export_path("gc1").read_bytes() == header_bytes() + _lines(db)


# ── pending-too-long notification, background loop ─────────────────────────

def test_pending_over_ten_minutes_notifies_once(exp, db, notes, clock, monkeypatch):
    add_final(db)
    exp.flush("gc1")          # the file exists, so the next append goes through _open_append
    add_final(db)
    monkeypatch.setattr(exports, "_open_append",
                        lambda p: (_ for _ in ()).throw(PermissionError(13, "locked")))
    exp.tick()
    clock.t += 599
    exp.tick()
    assert not [n for n in notes if "pending" in n[1]]
    clock.t += 2
    exp.tick()
    clock.t += 60
    exp.tick()
    alerts = [n for n in notes if "pending" in n[1]]
    assert len(alerts) == 1
    assert alerts[0][0] == "warning" and "gc1" in alerts[0][1]
    assert exp.status("gc1")["pending"] == 1
    monkeypatch.undo()
    exp.tick()
    assert exp.status("gc1")["pending"] == 0
    assert exp.status("gc1")["pending_since"] is None


def test_tick_survives_a_refusal_and_flushes_other_instruments(exp, db):
    _write_v1_file(exp.export_path("gc1"))
    add_final(db, "gc1")
    add_final(db, "gc2")
    out = exp.tick()
    assert out["gc1"]["refused"] == "no-sidecar"
    assert out["gc2"]["appended"] == 1


def test_background_thread_flushes_and_stops(exp, db):
    add_final(db)
    exp.start(interval=0.05)
    try:
        deadline = time.monotonic() + 5
        while pending(db) and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        exp.stop()
    assert pending(db) == []
    assert not any(t.name == "gc-hub-exports" and t.is_alive() for t in threading.enumerate())


def test_default_background_interval_is_sixty_seconds():
    assert exports.FLUSH_INTERVAL_SECONDS == 60
    assert exports.PENDING_ALERT_SECONDS == 600


# ── cheap verification for large files (share-friendly) ────────────────────

def _big_adopted(exp, n_rows=40000) -> Path:
    path = exp.export_path("gc1")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = ("40305,2026-09-25 00:24:50," + ",".join(["123.45"] * 28) + ",Diesel\r\n").encode()
    path.write_bytes(header_bytes() + row * n_rows)
    return path


def test_steady_state_flushes_read_only_the_tail(exp, db, monkeypatch, tmp_path, clock):
    path = _big_adopted(exp)
    assert path.stat().st_size > 3_000_000
    full = []
    real = exports._hash_prefix
    monkeypatch.setattr(exports, "_hash_prefix", lambda p, n: (full.append(n), real(p, n))[1])
    exp.adopt("gc1")
    assert len(full) == 1                           # adopt reads the whole file once
    for _ in range(5):
        add_final(db)
        exp.flush("gc1")
    assert len(full) == 1                           # ...and no flush reads it again
    side = _sidecar(path)
    assert side["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert side["tail_sha256"] and side["tail_len"] <= exports.TAIL_BYTES

    # A restart verifies the whole file once, then goes back to the tail.
    exp2 = exports.HubExporter(db=db, data_dir=tmp_path / "data", clock=clock)
    add_final(db)
    exp2.flush("gc1")
    assert len(full) == 2
    add_final(db)
    exp2.flush("gc1")
    assert len(full) == 2


def test_a_touched_mtime_escalates_to_a_full_check(exp, db, monkeypatch):
    path = _big_adopted(exp, 2000)
    exp.adopt("gc1")
    full = []
    real = exports._hash_prefix
    monkeypatch.setattr(exports, "_hash_prefix", lambda p, n: (full.append(n), real(p, n))[1])
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    add_final(db)
    exp.flush("gc1")                                # unchanged content: accepted after a full check
    assert len(full) == 1
    assert pending(db) == []


def test_a_middle_edit_with_mtime_change_is_refused(exp, db):
    path = _big_adopted(exp, 2000)
    exp.adopt("gc1")
    data = bytearray(path.read_bytes())
    data[1000] = ord("9") if data[1000] != ord("9") else ord("8")
    path.write_bytes(bytes(data))
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    add_final(db)
    with pytest.raises(exports.ExportRefused) as ei:
        exp.flush("gc1")
    assert ei.value.reason == "changed"


def test_a_middle_edit_hidden_from_the_cheap_check_is_caught_on_restart(exp, db, tmp_path, clock):
    """The documented trade-off: same size, same tail and the mtime put back
    is not seen by a running exporter's cheap check, but the next start-up's
    full check refuses."""
    path = _big_adopted(exp, 2000)
    exp.adopt("gc1")
    st = path.stat()
    data = bytearray(path.read_bytes())
    data[1000] = ord("9") if data[1000] != ord("9") else ord("8")
    path.write_bytes(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    exp2 = exports.HubExporter(db=db, data_dir=tmp_path / "data", clock=clock)
    add_final(db)
    with pytest.raises(exports.ExportRefused) as ei:
        exp2.flush("gc1")
    assert ei.value.reason == "changed"
