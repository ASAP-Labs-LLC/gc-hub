"""exports.py: append-only results CSVs for the hub (phase 2, spec D8/D9b).

The ``export_rows`` ledger in the store holds, for each result that became
final, the exact CSV line frozen in v1's 31-column format. This module turns
those lines into bytes on disk: it appends each instrument's pending rows, in
``seq`` order, to that instrument's ``export_path``, which can be the share
CSV LEM already tails (D9b), and it never truncates or rewrites a byte.

Public API
==========

::

    format_line(results_json, source_file) -> str   # one frozen CSV line, "\\r\\n"-terminated
    header_line() -> str                             # CSV_HEADER as the same kind of line
    sidecar_path(csv_path) -> Path                   # <csv>.gchub.json
    lock_path(csv_path) -> Path                      # <csv>.gchub.lock

    class ExportRefused(Exception):                  # .reason (code), .path, .detail
    class ExportLocked(OSError):                     # another process holds the export lock
    class FlushResult:                               # .appended, .pending, .error, .path

    HubExporter(db=None, *, data_dir=None, notifier=None, clock=time.time,
                tail_bytes=TAIL_BYTES, retry_sleep=time.sleep)
        .export_path(instrument) -> Path     # instruments.export_path or <data>/results/<inst>_results.csv
        .flush(instrument) -> FlushResult    # raises ExportRefused; any OSError -> result.error
        .verify_full(instrument) -> dict | None      # full hash of the recorded region now
        .adopt(instrument, *, by=None) -> dict       # admin: accept the file as it is now
        .new_path(instrument, path) -> Path          # admin: switch the export to another file
        .write_fresh(instrument, path, *, by=None) -> int   # admin: new file of gated current revisions
        .after_purge(instrument) -> {path, relinked}   # purge.py: unlink a purged ledger row
        .tick() -> {instrument: {...}}       # one background pass over every instrument
        .start(interval=FLUSH_INTERVAL_SECONDS) / .stop() / .is_alive()
        .wake()                              # flush now (a row just became pending)
        .status(instrument) -> dict          # for the admin page

``notifier(level, message)`` has the signature of
``notifications.NotificationStore.add``; it is called once when an export is
refused (``error``, until a verified flush, adopt or new path clears it;
``info`` "adopt it next" instead when the refusal is ``no-sidecar`` on the
existing file ``new_path`` just switched to, the "New path, then Adopt"
step) and once when an instrument's rows have been pending for more than
``PENDING_ALERT_SECONDS`` (``warning``, until the backlog clears).

Frozen row bytes
================

``format_line`` is exactly what v1's ``distill._append_csv_row`` writes for
the same values: a default-dialect ``csv.writer`` (``\\r\\n`` terminator,
minimal quoting, ``None`` as an empty cell) into a file opened with
``newline=""`` and UTF-8. ``results_json`` is the ``sample_results.results``
JSON (or the dict), keyed by ``CSV_HEADER`` names; floats survive the JSON
round-trip exactly (``repr``), so the text of every cell is v1's. Writers
append ``line.encode("utf-8")`` verbatim. Before appending, each ledger line
must end in ``\\r\\n`` and parse as exactly one 31-field record (``bad-line``).

The sidecar and the refusal rules
=================================

``<file>.gchub.json`` is written atomically (temp file, fsync, replace) after
each append::

    {"instrument", "db_id", "size", "sha256", "seq", "last_line_sha256",
     "last_line_end", "tail_len", "tail_sha256", "mtime_ns", "updated_at",
     ["new_file"], ["adopted_at", "adopted_by"]}

``size``/``sha256`` describe the whole file as the hub last left it; ``seq``
is the last ledger row known to be in it; ``instrument`` is whose file it is.

**The ledger link.** ``seq`` alone would be trusted blindly after the hub
database is restored from a backup: new rows reuse seqs the sidecar already
covers and would be marked appended without ever being written. So the
sidecar also records ``db_id`` (a random id created once in ``settings_kv``
as ``exports_db_id``; it tells *separate* databases apart, but a restored
backup carries the same id, so restores are caught by the last line's hash,
not by it), ``last_line_sha256`` (the sha256 of the ledger line
at ``seq``, or null when ``seq`` names no row of this instrument) and
``last_line_end`` (the byte offset where that line ends in the file, or null
when it is not in the file, e.g. after ``write_fresh`` or an adopt that took
``seq`` from the ledger). Before any pending row at or below ``seq`` is
marked done, the ``db_id`` must match, the ledger row at ``seq`` must hash to
``last_line_sha256`` and, when ``last_line_end`` is set, the file must hold
that line ending there; with no ``last_line_sha256`` no pending row may sit
at or below ``seq``. Otherwise ``ledger-mismatch`` ("the hub database doesn't
match this export file — was it restored from a backup?"). ``adopt`` applies
the same test before keeping an old sidecar's ``seq``. A pending row whose
exact line already sits in the file after the last marked row (a row that was
pending when the backup was taken, flushed, then pending again after the
restore and an adopt) is marked appended, not written a second time; a copy
further up the file doesn't count.
``flush`` refuses (``ExportRefused``) when:

* ``no-sidecar``: the file exists, is not empty and has no sidecar (a v1 file
  must be **adopted**; an empty file is treated as new);
* ``foreign-sidecar``: the sidecar belongs to another instrument (or names
  none); ``adopt`` refuses this too;
* ``missing``: the sidecar records content but the file is gone;
* ``shrunk``: the file is smaller than recorded;
* ``grown``: it is larger, and the extra bytes are not the start of what the
  hub was about to append (see recovery below);
* ``changed``: the recorded region's hash no longer matches;
* ``ledger-mismatch``: the sidecar's ``db_id``/last line don't match the
  ledger (see above);
* ``sidecar-unreadable``, and ``bad-line``.

A not-found from the sidecar read or the stat is read once more before it is
believed (a flapping share).

A flush with nothing pending still checks the file (cheaply) when it or its
sidecar exists, and only a check that passes clears a refusal.

``adopt`` refuses ``missing``, ``foreign-sidecar``, ``header-mismatch``
(naming a UTF-8 BOM if Excel re-saved the file), ``no-trailing-newline`` (v1's
last line cut off mid-record would have the hub's first row glued onto it)
``permission`` (``FOLDER_RIGHTS_TEXT``) and ``busy`` (admin calls wait at
most ``ADMIN_LOCK_WAIT_SECONDS`` for the export lock instead of stalling). It keeps the old sidecar's ``seq`` only if that sidecar is this
instrument's, its size is at most the current size and the file's first
``size`` bytes still hash to its ``sha256``; otherwise ``seq`` is the last
row the ledger has marked appended. ``new_path`` and ``write_fresh`` refuse
``in-use`` (another instrument's export path); ``write_fresh`` refuses
``exists``.

**Cheap verification (why not a full sha256 on every append).** The share
CSV is several MB and grows forever; hashing it over SMB every minute is
slow and holds the file open while LEM and Excel want it. So:

* the *first* check of a file in a process (start-up), ``adopt`` and
  ``verify_full`` hash the whole recorded region and compare it with
  ``sha256``. The exporter keeps that ``hashlib`` state in memory; ``tick``
  calls ``verify_full`` at most once per ``FULL_VERIFY_SECONDS`` (24 h);
* otherwise each flush checks ``size``, the mtime this process last saw
  and the sha256 of the last ``tail_len`` bytes (``TAIL_BYTES``, 64 KiB)
  only. A changed mtime at the recorded size escalates to a full hash. A
  failed daily check is retried after ``FULL_VERIFY_RETRY_SECONDS``, not on
  every tick;
* each append extends the in-memory hash with the bytes it wrote, so the
  sidecar's ``sha256`` is always the true whole-file hash without re-reading.

Trade-off: an edit **before** the last 64 KiB that keeps the size *and* puts
the mtime back is not seen until the next full check (start-up or the daily
one). Anything LEM is exposed to by an in-place edit (a shrink, an append, a
save by Excel, which changes size or mtime) is caught at once.

**Recovery (exactly once).** The order is: append + fsync, then the sidecar,
then ``mark_hub_appended``. A crash (or error) between any two steps heals:

* rows in the file but not in the sidecar: the file is longer than recorded
  and the extra bytes are a prefix of what the hub is about to write, so it
  writes only the rest. The same covers a partial write (a dropped share
  connection mid-write);
* rows in the sidecar but not marked: pending rows with ``seq <=
  sidecar.seq`` are marked without being written again.

**New files and stale not-found.** Over SMB a flapping share can make an
existing file look missing (``ERROR_BAD_NETPATH`` → ``FileNotFoundError``).
So a new file never goes through ``open("ab")``: the sidecar is created first
with ``O_EXCL`` (``new_file: true``, size 0) and the CSV with
``open(path, "xb")``. Either ``FileExistsError`` means the earlier "missing"
was wrong; the state is re-read (a few times, then the flush reports an
error and the rows stay pending). ``"ab"`` is used only when the sidecar
records content, or the file was seen and confirmed empty or holding the
start of this very append; and even then the opened handle's size
(``os.fstat``) must equal the verified offset, or it is closed unwritten and
the state re-read (an append that sneaked in between the check and the open).

**Writes** go through an unbuffered handle, at most ``WRITE_CHUNK_BYTES``
(64 KiB) per call, split on row boundaries, looping on short writes, with one
fsync at the end.

**Exclusion.** Within a process a per-instrument lock serialises flushes.
Across processes (two hubs, or a hub and a stray tool) ``<file>.gchub.lock``
next to the sidecar is locked (``fcntl.flock`` on POSIX, ``msvcrt.locking``
on Windows, an SMB byte-range lock on a share; released by the OS when the
process dies, so there is no stale lock to take over) around verify →
append → sidecar → mark. A lock not obtained within ``LOCK_WAIT_SECONDS`` is
an ``ExportLocked`` error, retried next time.

**Share paths are supported only on the Windows hub** (SMB-enforced
``msvcrt`` byte-range locks). A mount that ignores locks is unsupported:
``flock`` on a POSIX SMB/NFS mount may be local-only, and then nothing stops
two hosts appending at once.

**Locks by others.** A ``PermissionError`` opening the CSV (Excel, antivirus,
a backup) is retried ``APPEND_OPEN_ATTEMPTS`` times in the call, then
reported in ``FlushResult.error`` like every other ``OSError`` (and a
``sqlite3.OperationalError``) with the rows left pending; the background loop
retries every ``FLUSH_INTERVAL_SECONDS``. ``pending_since`` is in memory, so
after a restart the 10-minute alert clock starts again.

**Temp files.** The sidecar's temp file is ``.gchub-<sidecar name>.<random>.part``.
v1 (and ``distill.sweep_stale_csv_temps``) deletes ``.<csv name>.*.tmp`` next
to the results CSV at start-up; the hub's name can never match that glob, so
a v1 instance starting beside an adopted file cannot delete a sidecar write
in flight.

**Write fresh** writes the header and every gated sample's current revision
(its ledger line, or ``format_line`` of the stored results when there is
none) to a new file, in ledger order, creates its sidecar at the ledger's
max ``seq`` for that instrument, switches ``export_path`` to it, and only
then marks the pending rows up to that ``seq`` as appended. A crash before
the marking loses nothing: the next flush marks them from the sidecar's
``seq``. Never point LEM at a fresh file without setting its tail offset to
the end, or it re-reads all of history.

LEM (read-only notes, lab-equipment-manager ``lem_station_module.py``)
======================================================================

LEM's ``tail_new_text`` opens the file with plain ``open(path, "rb")``
(Windows share mode read+write, so it never blocks the hub's appends), reads
to EOF and advances ``last_position`` to EOF, not to the last newline. A
partial append that is later completed can therefore be read as two broken
lines; the hub keeps that window small (row-aligned chunks, fsync at the end)
but cannot close it. A shrink sends LEM back to offset 0 (it re-reads everything),
which is one more reason the hub never truncates. LEM splits each line on the
delimiter without CSV quoting, so a header line in the middle of the file
would reach its parser as a print with Lab ID ``"Lab ID"``: the hub writes
the header only to an empty file.
"""
from __future__ import annotations

import collections
import contextlib
import csv
import errno
import hashlib
import io
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Union

import distill
import live
import paths
import store

LOGGER = logging.getLogger(__name__)

CSV_HEADER = distill.CSV_HEADER
SIDECAR_SUFFIX = ".gchub.json"
LOCK_SUFFIX = ".gchub.lock"
TAIL_BYTES = 64 * 1024
FLUSH_INTERVAL_SECONDS = 60
PENDING_ALERT_SECONDS = 600
FULL_VERIFY_SECONDS = 24 * 3600
LOCK_WAIT_SECONDS = 30.0
APPEND_OPEN_ATTEMPTS = 3
APPEND_OPEN_BACKOFF_SECONDS = 0.2
ADMIN_LOCK_WAIT_SECONDS = 2.0
FULL_VERIFY_RETRY_SECONDS = 3600
WRITE_CHUNK_BYTES = 64 * 1024
DB_ID_KEY = "exports_db_id"
FOLDER_RIGHTS_TEXT = ("the hub needs create/rename/delete rights in the folder (for the "
                      "sidecar, its temp file and the lock file)")
LEDGER_MISMATCH_TEXT = ("the hub database doesn't match this export file — was it restored "
                        "from a backup? Adopt the file (the hub appends after it) or choose "
                        "a new path")
REEVALUATE_ATTEMPTS = 3
_CHUNK = 1024 * 1024
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()

PathLike = Union[str, os.PathLike]


# ── the frozen line ─────────────────────────────────────────────────────────

def _csv_line(values) -> str:
    buf = io.StringIO(newline="")
    csv.writer(buf).writerow(values)
    return buf.getvalue()


def header_line() -> str:
    """``CSV_HEADER`` as v1 writes it (``\\r\\n``-terminated)."""
    return _csv_line(CSV_HEADER)


def format_line(results_json: Union[str, Mapping[str, Any]], source_file: Any) -> str:
    """The frozen CSV line for one result, byte-identical to v1's append.

    ``results_json`` is the revision's results (JSON text or a dict keyed by
    ``CSV_HEADER`` names); a missing column is an empty cell. ``source_file``
    fills ``Source File`` whatever the results say. ``ValueError`` if the
    input is not a results row.
    """
    results = json.loads(results_json) if isinstance(results_json, (str, bytes)) else results_json
    if not isinstance(results, Mapping) or "Lab ID" not in results:
        raise ValueError("format_line needs a results dict keyed by CSV_HEADER names")
    values = [results.get(col, "") for col in CSV_HEADER[:-1]]
    values.append("" if source_file is None else str(source_file))
    return _csv_line(values)


def with_injection_dt(results_json: Union[str, Mapping[str, Any]], injection_dt: Optional[str]) -> str:
    """``results_json`` (JSON text or dict) with ``InjectionDateTime`` set to
    ``injection_dt`` (unchanged when ``None``), as JSON text. Hub-written
    lines use the sample's corrected time; stored revisions stay verbatim."""
    results = json.loads(results_json) if isinstance(results_json, (str, bytes)) else dict(results_json)
    if injection_dt is not None and isinstance(results, dict):
        results["InjectionDateTime"] = injection_dt
    return json.dumps(results)


def _is_one_record(line: str) -> bool:
    if not line.endswith("\r\n"):
        return False
    try:
        records = list(csv.reader(io.StringIO(line, newline="")))
    except csv.Error:
        return False
    return len(records) == 1 and len(records[0]) == len(CSV_HEADER)


# ── errors and results ──────────────────────────────────────────────────────

class ExportRefused(Exception):
    """The hub will not write to this file until an admin acts."""

    def __init__(self, reason: str, path: PathLike, detail: str) -> None:
        self.reason = reason
        self.path = Path(path)
        self.detail = detail
        super().__init__(f"Export to {path} refused ({reason}): {detail}")


class ExportLocked(OSError):
    """Another process holds the export lock; try again later."""


class _Reevaluate(Exception):
    """The file system contradicted itself (stale not-found); read the state again."""


@dataclass
class FlushResult:
    appended: int
    pending: int
    error: Optional[str] = None
    path: Optional[Path] = None


# ── file helpers (module level so tests can inject failures) ───────────────

def sidecar_path(csv_path: PathLike) -> Path:
    csv_path = Path(csv_path)
    return csv_path.with_name(csv_path.name + SIDECAR_SUFFIX)


def lock_path(csv_path: PathLike) -> Path:
    csv_path = Path(csv_path)
    return csv_path.with_name(csv_path.name + LOCK_SUFFIX)


def _open_append(path: Path):
    """Open an existing (confirmed) file for appending (``O_APPEND``, unbuffered)."""
    return open(path, "ab", buffering=0)


def _open_new(path: Path):
    """Create *path* (unbuffered); ``FileExistsError`` if anything is already there."""
    return open(path, "xb", buffering=0)


def _chunks(pieces: list, skip: int) -> Iterator[bytes]:
    """*pieces* (header and rows) minus the first *skip* bytes, joined into
    writes of at most ``WRITE_CHUNK_BYTES`` that end on a row boundary (a
    single longer row is written on its own)."""
    buf: list = []
    n = 0
    for p in pieces:
        if skip:
            if skip >= len(p):
                skip -= len(p)
                continue
            p, skip = p[skip:], 0
        if n and n + len(p) > WRITE_CHUNK_BYTES:
            yield b"".join(buf)
            buf, n = [], 0
        buf.append(p)
        n += len(p)
    if buf:
        yield b"".join(buf)


def _write_all(fh, chunks) -> None:
    """Write every chunk, continuing short writes; one fsync at the end."""
    for chunk in chunks:
        view = memoryview(chunk)
        off = 0
        while off < len(view):
            n = fh.write(view[off:])
            if not n:
                raise OSError(errno.EIO, "short write made no progress")
            off += n
    fh.flush()
    os.fsync(fh.fileno())


def _hash_prefix(path: Path, nbytes: int) -> "hashlib._Hash":
    """sha256 state of the first *nbytes* of *path* (the one full read)."""
    return _hash_extend(path, hashlib.sha256(), 0, nbytes)


def _hash_extend(path: Path, h: "hashlib._Hash", start: int, end: int) -> "hashlib._Hash":
    left = end - start
    if left <= 0:
        return h
    with open(path, "rb") as fh:
        fh.seek(start)
        while left > 0:
            chunk = fh.read(min(_CHUNK, left))
            if not chunk:
                raise OSError(f"{path} is shorter than {end} bytes")
            h.update(chunk)
            left -= len(chunk)
    return h


def _read_range(path: Path, offset: int, length: int) -> bytes:
    if length <= 0:
        return b""
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(length)


def _rfind_line_end(path: Path, line: bytes, limit: int) -> Optional[int]:
    """Offset just past the last whole-line copy of ``line`` in the first
    ``limit`` bytes of ``path`` (``None`` if there is none); read backwards."""
    pos = limit
    carry = b""
    while pos > 0:
        start = max(0, pos - _CHUNK)
        buf = _read_range(path, start, pos - start) + carry
        i = len(buf)
        while True:
            i = buf.rfind(line, 0, i)
            if i < 0:
                break
            before = buf[i - 1:i] if i else _read_range(path, start - 1, 1) if start else b"\n"
            if before == b"\n":
                return start + i + len(line)
            if i == 0:
                break
        carry = buf[:len(line)]
        pos = start
    return None


def _read_sidecar(csv_path: Path) -> Optional[dict]:
    sp = sidecar_path(csv_path)
    try:
        raw = sp.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        side = json.loads(raw)
        side["size"] = int(side["size"])
        side["seq"] = int(side.get("seq") or 0)
        if not isinstance(side.get("sha256"), str) or side["size"] < 0:
            raise ValueError("sha256/size")
        return side
    except (ValueError, TypeError, KeyError) as exc:
        raise ExportRefused("sidecar-unreadable", csv_path,
                            f"the sidecar {sp.name} is not valid ({exc}); "
                            "adopt the file or choose a new path") from exc


def _write_sidecar(csv_path: Path, side: dict) -> None:
    """Replace the sidecar atomically (temp file in the same folder, fsync, replace).

    The temp name ``.gchub-<sidecar>.<random>.part`` never matches v1's
    ``.<csv>.*.tmp`` start-up sweep."""
    sp = sidecar_path(csv_path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".gchub-{sp.name}.", suffix=".part",
                                    dir=str(sp.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            json.dump(side, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        distill._replace_retrying(tmp, sp)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _create_sidecar(csv_path: Path, side: dict) -> None:
    """Create the sidecar with ``O_EXCL``; ``FileExistsError`` if one is there."""
    sp = sidecar_path(csv_path)
    fd = os.open(sp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        json.dump(side, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())


def _tail_fields(tail: bytes, mtime_ns: Optional[int]) -> dict:
    return {"tail_len": len(tail), "tail_sha256": hashlib.sha256(tail).hexdigest(),
            "mtime_ns": mtime_ns}


def _stat(path: Path) -> Optional[os.stat_result]:
    try:
        return path.stat()
    except FileNotFoundError:
        return None


def _same_file_path(a: PathLike, b: PathLike) -> bool:
    norm = lambda p: os.path.normcase(os.path.abspath(os.fspath(p)))  # noqa: E731
    return norm(a) == norm(b)


# ── cross-process lock ──────────────────────────────────────────────────────

if os.name == "nt":  # pragma: no cover - exercised on Windows CI
    import msvcrt

    def _try_lock(fh) -> bool:
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", -1)):
                return False
            raise

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fh) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _file_lock(csv_path: PathLike, wait: Optional[float] = None) -> Iterator[None]:
    """Hold ``<csv>.gchub.lock`` exclusively (OS lock: freed if the process dies)."""
    wait = LOCK_WAIT_SECONDS if wait is None else wait
    lp = lock_path(csv_path)
    fh = open(lp, "a+b")
    try:
        deadline = time.monotonic() + wait
        while not _try_lock(fh):
            if time.monotonic() >= deadline:
                raise ExportLocked(errno.EAGAIN, "the export lock is held by another process "
                                   "(another hub?); will retry", str(lp))
            time.sleep(0.02)
        try:
            yield
        finally:
            _unlock(fh)
    finally:
        fh.close()


@contextlib.contextmanager
def _admin_lock(csv_path: Path) -> Iterator[None]:
    """The export lock for an admin call: a short wait, then ``busy``."""
    lock = _file_lock(csv_path, ADMIN_LOCK_WAIT_SECONDS)
    try:
        lock.__enter__()
    except ExportLocked as exc:
        raise ExportRefused("busy", csv_path, "another process holds the export lock; "
                            "try again in a moment") from exc
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


# ── the exporter ────────────────────────────────────────────────────────────

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict = collections.defaultdict(threading.RLock)


def _instrument_lock(instrument: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS[instrument]


@dataclass
class _Verified:
    """What this process has itself hashed: the first ``size`` bytes of a file."""
    size: int
    hasher: Any
    tail: bytes
    mtime_ns: Optional[int] = None


_TRANSIENT = (OSError, sqlite3.OperationalError)


def _publish_appended(instrument: str, rows) -> None:
    """Live update (v3.1): these ledger rows are in the results CSV now."""
    try:
        live.publish_samples([r["sample_id"] for r in rows])
    except Exception:  # noqa: BLE001 - never into the exporter
        pass
    live.publish("instrument", {"instrument_id": instrument})


class HubExporter:
    def __init__(self, db: store.Db = None, *, data_dir: Optional[PathLike] = None,
                 notifier: Optional[Callable[[str, str], Any]] = None,
                 clock: Callable[[], float] = time.time, tail_bytes: int = TAIL_BYTES,
                 retry_sleep: Callable[[float], Any] = time.sleep) -> None:
        self.db = db
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._notifier = notifier
        self._clock = clock
        self.tail_bytes = max(1, int(tail_bytes))
        self._sleep = retry_sleep
        self._verified: dict[str, _Verified] = {}
        self._last_full: dict[str, float] = {}
        self._full_attempt: dict[str, float] = {}
        self._refused: dict[str, ExportRefused] = {}
        # instrument -> the existing, sidecar-less file New path just pointed
        # it at: its no-sidecar refusal is the expected "Adopt it next" step
        # (an info, not an error) until Adopt, another path or a clean flush.
        self._adopt_next: dict[str, Path] = {}
        self._last_error: dict[str, Optional[str]] = {}
        self._pending_since: dict[str, float] = {}
        self._alerted: set[str] = set()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.ticking = False          # a background pass (append) is in progress

    # ── paths ──

    def _default_path(self, instrument: str) -> Path:
        base = self._data_dir or paths.data_dir()
        if base is None:
            raise RuntimeError("no export_path for %r and GC_DATA_DIR is unset" % instrument)
        return Path(base) / "results" / f"{instrument}_results.csv"

    def _configured_path(self, instrument: str) -> Optional[Path]:
        row = store.instruments.get(instrument, db=self.db)
        if row is None:
            raise ValueError(f"unknown instrument {instrument!r}")
        configured = (row.get("export_path") or "").strip()
        return Path(configured) if configured else None

    def export_path(self, instrument: str) -> Path:
        return self._configured_path(instrument) or self._default_path(instrument)

    def _check_free(self, instrument: str, path: Path) -> None:
        for other in store.instruments.list(db=self.db):
            if other["id"] == instrument:
                continue
            try:
                theirs = self.export_path(other["id"])
            except RuntimeError:
                continue
            if _same_file_path(theirs, path):
                raise ExportRefused("in-use", path, f"it is {other['id']}'s export file; "
                                    "two instruments must never share one")

    # ── notifications and state ──

    def _notify(self, level: str, message: str) -> None:
        LOGGER.log(logging.ERROR if level == "error" else logging.WARNING, "%s", message)
        if self._notifier is not None:
            try:
                self._notifier(level, message)
            except Exception:  # a notification failure must not stop exports
                LOGGER.exception("exports: notifier failed")

    def _record_refusal(self, instrument: str, err: ExportRefused) -> None:
        prev = self._refused.get(instrument)
        self._refused[instrument] = err
        if prev is not None and (prev.reason, prev.path) == (err.reason, err.path):
            return
        awaiting = self._adopt_next.get(instrument)
        if (err.reason == "no-sidecar" and awaiting is not None
                and _same_file_path(err.path, awaiting)):
            self._notify("info", f"GC export for {instrument} now points at {err.path}, which "
                                 "already has content: Adopt it next (Admin > Exports) so the "
                                 "hub appends after it. Nothing is written to it until then.")
            return
        self._notify("error", f"GC export for {instrument} refused: {err}. "
                              "Admin > Exports: Adopt the file or choose a new path.")

    def _clear_refusal(self, instrument: str) -> None:
        self._refused.pop(instrument, None)
        self._adopt_next.pop(instrument, None)

    def _pending_count(self, instrument: str) -> int:
        try:
            return len(store.export_rows.pending_hub_appends(instrument, db=self.db))
        except _TRANSIENT:
            return 1  # unknown, but assume a backlog so the alert clock keeps running

    def _track_pending(self, instrument: str, pending: int) -> None:
        if pending <= 0:
            self._pending_since.pop(instrument, None)
            self._alerted.discard(instrument)
            return
        now = self._clock()
        since = self._pending_since.setdefault(instrument, now)
        if now - since > PENDING_ALERT_SECONDS and instrument not in self._alerted:
            self._alerted.add(instrument)
            why = self._last_error.get(instrument) or (
                str(self._refused[instrument]) if instrument in self._refused else "unknown")
            self._notify("warning", f"GC export for {instrument}: {pending} row(s) pending "
                                    f"for over {PENDING_ALERT_SECONDS // 60} minutes ({why}).")

    def status(self, instrument: str) -> dict:
        refused = self._refused.get(instrument)
        since = self._pending_since.get(instrument)
        return {
            "instrument": instrument,
            "path": str(self.export_path(instrument)),
            "pending": len(store.export_rows.pending_hub_appends(instrument, db=self.db)),
            "pending_since": (datetime.fromtimestamp(since, timezone.utc).isoformat()
                              if since is not None else None),
            "refused": refused.reason if refused else None,
            "refused_detail": str(refused) if refused else None,
            "last_error": self._last_error.get(instrument),
        }

    # ── the ledger link ──

    def _db_id(self) -> str:
        """This database's export id (created once, in ``settings_kv``).

        It distinguishes separate databases only: a backup restored over this
        one carries the same id. Restores are caught by ``last_line_sha256``.
        """
        value = store.settings_kv.get(DB_ID_KEY, db=self.db)
        if value:
            return value
        with store.connection(self.db) as conn, store.write_txn(conn):
            value = store.settings_kv.get(DB_ID_KEY, db=conn)
            if not value:
                value = uuid.uuid4().hex
                store.settings_kv.set(DB_ID_KEY, value, db=conn)
        return value

    def _ledger_line(self, instrument: str, seq: int) -> Optional[str]:
        with store.connection(self.db) as conn:
            r = conn.execute('SELECT "row" FROM export_rows WHERE seq=? AND instrument_id=?',
                             (int(seq), instrument)).fetchone()
        return r[0] if r else None

    def _link(self, instrument: str, seq: int, end: Optional[int]) -> dict:
        line = self._ledger_line(instrument, seq) if seq else None
        return {"db_id": self._db_id(),
                "last_line_sha256": (hashlib.sha256(line.encode("utf-8")).hexdigest()
                                     if line is not None else None),
                "last_line_end": end if line is not None else None}

    def _link_ok(self, instrument: str, path: Path, side: dict, rows: list) -> bool:
        """Whether the sidecar's ``seq`` is vouched for by this ledger."""
        if side.get("db_id") != self._db_id():
            return False
        sha = side.get("last_line_sha256")
        if not sha:
            return not any(r["seq"] <= side["seq"] for r in rows)
        line = self._ledger_line(instrument, side["seq"])
        if line is None or hashlib.sha256(line.encode("utf-8")).hexdigest() != sha:
            return False
        end = side.get("last_line_end")
        if end is not None:
            b = line.encode("utf-8")
            if end > side["size"] or end < len(b) or _read_range(path, end - len(b), len(b)) != b:
                return False
        return True

    # ── verification ──

    @staticmethod
    def _check_owner(path: Path, side: dict, instrument: str) -> None:
        owner = side.get("instrument")
        if owner != instrument:
            raise ExportRefused("foreign-sidecar", path, f"the sidecar belongs to "
                                f"{owner or 'no instrument'}, not {instrument}; choose "
                                "this instrument's own export path")

    def _verify(self, path: Path, side: dict, st: Optional[os.stat_result]) -> _Verified:
        """Check that the first ``side['size']`` bytes are what the sidecar
        records; return that region's hash state and tail. Refuses ``changed``."""
        rec = side["size"]
        size = st.st_size if st else 0
        key = str(path)
        cached = self._verified.get(key)
        cheap = (cached is not None and cached.size == rec
                 and cached.hasher.hexdigest() == side["sha256"]
                 and isinstance(side.get("tail_sha256"), str)
                 and isinstance(side.get("tail_len"), int)
                 and (size != rec or cached.mtime_ns == (st.st_mtime_ns if st else None)))
        want_tail = min(rec, max(self.tail_bytes, int(side.get("tail_len") or 0)))
        tail = _read_range(path, rec - want_tail, want_tail) if rec else b""
        if cheap:
            n = side["tail_len"]
            if n > len(tail) or hashlib.sha256(tail[len(tail) - n:]).hexdigest() != side["tail_sha256"]:
                self._verified.pop(key, None)
                raise ExportRefused("changed", path, "the end of the file is not what the hub "
                                    "last wrote; it was edited by someone else")
            return _Verified(rec, cached.hasher, tail, cached.mtime_ns)
        hasher = _hash_prefix(path, rec) if rec else hashlib.sha256()
        if rec:
            self._last_full[key] = self._clock()
        if hasher.hexdigest() != side["sha256"]:
            self._verified.pop(key, None)
            raise ExportRefused("changed", path, f"the first {rec} bytes no longer match the "
                                "sidecar's sha256; the file was edited by someone else")
        v = _Verified(rec, hasher, tail, st.st_mtime_ns if (st and size == rec) else None)
        self._verified[key] = v
        return v

    def verify_full(self, instrument: str) -> Optional[dict]:
        """Hash the whole recorded region now (the daily check). ``None`` if
        there is no sidecar yet. Raises ``ExportRefused``; ``OSError`` propagates."""
        with _instrument_lock(instrument):
            path = self.export_path(instrument)
            try:
                side = _read_sidecar(path)
                if side is None:
                    return None
                self._check_owner(path, side, instrument)
                with _file_lock(path):
                    side = _read_sidecar(path) or side
                    st = _stat(path)
                    self._check_size(path, side, st)
                    rec = side["size"]
                    # Hash afresh; an I/O failure leaves the cached state alone.
                    hasher = _hash_prefix(path, rec) if rec else hashlib.sha256()
                    if hasher.hexdigest() != side["sha256"]:
                        self._verified.pop(str(path), None)
                        raise ExportRefused("changed", path, f"the first {rec} bytes no longer "
                                            "match the sidecar's sha256; the file was edited "
                                            "by someone else")
                    n = min(rec, self.tail_bytes)
                    self._verified[str(path)] = _Verified(
                        rec, hasher, _read_range(path, rec - n, n),
                        st.st_mtime_ns if (st and st.st_size == rec) else None)
            except ExportRefused as err:
                self._record_refusal(instrument, err)
                raise
            self._last_full[str(path)] = self._clock()
            return {"size": rec, "sha256": hasher.hexdigest()}

    @staticmethod
    def _check_size(path: Path, side: dict, st: Optional[os.stat_result]) -> None:
        rec = side["size"]
        if st is None and rec > 0:
            raise ExportRefused("missing", path, f"the sidecar records {rec} bytes but the file "
                                "is gone; choose a new path (or restore the file)")
        size = st.st_size if st else 0
        if size < rec:
            raise ExportRefused("shrunk", path, f"the file is {size} bytes, smaller than the "
                                f"{rec} the hub last recorded; it was truncated or replaced")

    # ── flush ──

    def flush(self, instrument: str) -> FlushResult:
        """Append this instrument's pending ledger rows to its export file.

        Raises ``ExportRefused`` (and notifies once) when the file is not in
        the state the sidecar records. Any ``OSError`` (a lock, a dropped
        share, the export lock held elsewhere) or ``sqlite3.OperationalError``
        is returned in ``FlushResult.error`` with the rows left pending.
        """
        with _instrument_lock(instrument):
            path: Optional[Path] = None
            try:
                path = self.export_path(instrument)
                result = self._flush(instrument, path)
            except ExportRefused as err:
                self._record_refusal(instrument, err)
                self._track_pending(instrument, self._pending_count(instrument))
                raise
            except _TRANSIENT as exc:
                result = FlushResult(0, self._pending_count(instrument),
                                     f"{type(exc).__name__}: {exc}", path)
            if result.error:
                self._last_error[instrument] = result.error
                LOGGER.warning("exports: %s append to %s failed: %s", instrument, path, result.error)
            else:
                self._last_error.pop(instrument, None)
                self._clear_refusal(instrument)
            self._track_pending(instrument, result.pending)
            return result

    def _flush(self, instrument: str, path: Path) -> FlushResult:
        rows = store.export_rows.pending_hub_appends(instrument, db=self.db)
        if not rows and _read_sidecar(path) is None and _stat(path) is None:
            return FlushResult(0, 0, None, path)          # nothing written, nothing to check
        if self._configured_path(instrument) is None:
            # Only the hub's own folder is created; a configured (share)
            # folder that is missing is an error to retry.
            path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(path):
            rows = store.export_rows.pending_hub_appends(instrument, db=self.db)
            for _ in range(REEVALUATE_ATTEMPTS):
                try:
                    return self._flush_once(instrument, path, rows)
                except _Reevaluate:
                    continue
        raise OSError(f"{path}: the file system kept contradicting itself (a flapping "
                      "share?); the rows stay pending")

    def _flush_once(self, instrument: str, path: Path, rows: list) -> FlushResult:
        for r in rows:
            if not _is_one_record(r["line"]):
                raise ExportRefused("bad-line", path, f"ledger row seq {r['seq']} is not one "
                                    f"{len(CSV_HEADER)}-field CSV record ending in CRLF")
        side = _read_sidecar(path)
        st = _stat(path)
        if side is None or st is None:     # a not-found is read once more (a flapping share)
            side = _read_sidecar(path)
            st = _stat(path)
        if side is not None:
            self._check_owner(path, side, instrument)
        else:
            if st is not None and st.st_size > 0:
                raise ExportRefused("no-sidecar", path, "the file exists but the hub has no "
                                    "record of it (no sidecar); Adopt it to append after its "
                                    "current content, or choose a new path")
            if not rows:
                return FlushResult(0, 0, None, path)
            # A new (or empty) file: record the intent first, exclusively, so
            # a partial first write is a recoverable "grown" state and a stale
            # not-found can't lead to writing over someone's sidecar.
            side = {"instrument": instrument, "size": 0, "sha256": _EMPTY_SHA,
                    "seq": rows[0]["seq"] - 1, "new_file": True,
                    "db_id": self._db_id(), "last_line_sha256": None, "last_line_end": None,
                    **_tail_fields(b"", None), "updated_at": store.now_iso()}
            try:
                _create_sidecar(path, side)
            except FileExistsError:
                raise _Reevaluate() from None
            self._verified[str(path)] = _Verified(0, hashlib.sha256(), b"")
            self._last_full[str(path)] = self._clock()   # everything in it is hashed by us
        self._check_size(path, side, st)
        rec = side["size"]
        size = st.st_size if st else 0
        v = self._verify(path, side, st)
        if not self._link_ok(instrument, path, side, rows):
            raise ExportRefused("ledger-mismatch", path, LEDGER_MISMATCH_TEXT)

        done = [r["seq"] for r in rows if r["seq"] <= side["seq"]]
        todo = [r for r in rows if r["seq"] > side["seq"]]
        if todo:
            # A restore + adopt can leave a row pending that the file already
            # holds; its exact line after the last marked row means it's there.
            there = self._already_after_last(instrument, path, side)
            done += [r["seq"] for r in todo if r["line"].encode("utf-8") in there]
            todo = [r for r in todo if r["line"].encode("utf-8") not in there]
        if done:  # in the file per the sidecar, not yet marked (a crash in between)
            store.export_rows.mark_hub_appended(done, db=self.db)
            _publish_appended(instrument, [r for r in rows if r["seq"] in set(done)])
        pieces = [r["line"].encode("utf-8") for r in todo]
        if pieces and rec == 0:
            pieces.insert(0, header_line().encode("utf-8"))
        planned = b"".join(pieces)
        extra = size - rec
        if extra:
            ours = extra <= len(planned) and _read_range(path, rec, extra) == planned[:extra]
            if not ours:
                if rec == 0 and side.get("new_file"):
                    raise ExportRefused("no-sidecar", path, "a file the hub did not write "
                                        "appeared at this path; Adopt it or choose a new path")
                raise ExportRefused("grown", path, f"{extra} bytes were added after the hub's "
                                    "last append by someone else (is v1 still writing?); "
                                    "Adopt to append after them, or choose a new path")
        if not todo:
            return FlushResult(0, 0, None, path)

        if st is None:
            try:
                self._write_new(path, _chunks(pieces, extra))
            except FileExistsError:
                raise _Reevaluate() from None
        elif extra < len(planned):
            self._append(path, _chunks(pieces, extra), expected_size=size)
        new_size = rec + len(planned)
        st2 = _stat(path)
        if st2 is None or st2.st_size != new_size:
            landed = st2 is not None and _read_range(path, rec, len(planned)) == planned
            if not landed:
                self._verified.pop(str(path), None)
                raise ExportRefused("changed", path, "the file changed while the hub was "
                                    "appending; its rows stay pending (check for duplicates "
                                    "before adopting)")
        hasher = v.hasher.copy()
        hasher.update(planned)
        tail = (v.tail + planned)[-self.tail_bytes:]
        new_side = {"instrument": instrument, "size": new_size, "sha256": hasher.hexdigest(),
                    "seq": todo[-1]["seq"], **self._link(instrument, todo[-1]["seq"], new_size),
                    **_tail_fields(tail, st2.st_mtime_ns if st2 else None),
                    "updated_at": store.now_iso()}
        for k in ("adopted_at", "adopted_by"):
            if k in side:
                new_side[k] = side[k]
        try:
            _write_sidecar(path, new_side)
        except BaseException:
            self._verified.pop(str(path), None)
            raise
        self._verified[str(path)] = _Verified(
            new_size, hasher, tail,
            st2.st_mtime_ns if (st2 and st2.st_size == new_size) else None)
        store.export_rows.mark_hub_appended([r["seq"] for r in todo], db=self.db)
        _publish_appended(instrument, todo)
        return FlushResult(len(todo), 0, None, path)

    def _already_after_last(self, instrument: str, path: Path, side: dict) -> set:
        """The lines (bytes, CRLF included) that sit in the recorded region after
        the sidecar's last marked row: empty in the normal case, where that row
        ends the region, and when the row can't be found in the file."""
        rec = side["size"]
        end = side.get("last_line_end")
        if end is not None and end >= rec:
            return set()
        line = self._ledger_line(instrument, side["seq"]) if side.get("last_line_sha256") else None
        if line is None:
            return set()
        if end is None:
            end = _rfind_line_end(path, line.encode("utf-8"), rec)
            if end is None:
                return set()
        region = _read_range(path, end, rec - end)
        return {ln + b"\n" for ln in region.split(b"\n")[:-1]}

    @staticmethod
    def _write_new(path: Path, chunks) -> None:
        with _open_new(path) as fh:
            _write_all(fh, chunks)

    def _append(self, path: Path, chunks, *, expected_size: int) -> None:
        for attempt in range(APPEND_OPEN_ATTEMPTS):
            try:
                fh = _open_append(path)
                break
            except PermissionError:
                if attempt == APPEND_OPEN_ATTEMPTS - 1:
                    raise
                self._sleep(APPEND_OPEN_BACKOFF_SECONDS * (attempt + 1))
        with fh:
            if os.fstat(fh.fileno()).st_size != expected_size:
                # Someone appended between the check and this open: write
                # nothing and look again.
                raise _Reevaluate()
            _write_all(fh, chunks)

    # ── admin actions ──

    def _max_appended_seq(self, instrument: str) -> int:
        with store.connection(self.db) as conn:
            r = conn.execute("SELECT MAX(seq) FROM export_rows WHERE instrument_id=? "
                             "AND hub_appended_at IS NOT NULL", (instrument,)).fetchone()
        return int(r[0] or 0)

    def adopt(self, instrument: str, *, by: Optional[str] = None) -> dict:
        """Accept the export file as it is now: pending rows go after its content.

        Refuses ``missing``, ``foreign-sidecar``, ``header-mismatch``,
        ``no-trailing-newline`` and ``permission``. Returns the sidecar written.
        """
        with _instrument_lock(instrument):
            path = self.export_path(instrument)
            if _stat(path) is None:
                raise ExportRefused("missing", path, "there is no file to adopt")
            try:
                with _admin_lock(path):
                    side = self._adopt(instrument, path, by)
            except PermissionError as exc:
                raise ExportRefused("permission", path, f"{FOLDER_RIGHTS_TEXT} ({exc})") from exc
            self._clear_refusal(instrument)
            return side

    def _adopt(self, instrument: str, path: Path, by: Optional[str]) -> dict:
        st = _stat(path)
        if st is None:
            raise ExportRefused("missing", path, "there is no file to adopt")
        size = st.st_size
        if size:
            head = _read_range(path, 0, min(size, TAIL_BYTES))
            self._check_header(path, head)
            if _read_range(path, size - 1, 1) != b"\n":
                raise ExportRefused("no-trailing-newline", path, "the last line has no "
                                    "newline, so the hub's first row would be glued onto "
                                    "it; end the file with a newline (or choose a new path)")
        try:
            old = _read_sidecar(path)
        except ExportRefused:
            old = None
        if old is not None and "instrument" in old and old["instrument"] != instrument:
            self._check_owner(path, old, instrument)
        seq, end = self._max_appended_seq(instrument), None
        if old is not None and old.get("instrument") == instrument and old["size"] <= size:
            hasher = _hash_prefix(path, old["size"]) if old["size"] else hashlib.sha256()
            pend = store.export_rows.pending_hub_appends(instrument, db=self.db)
            if (hasher.hexdigest() == old["sha256"]
                    and self._link_ok(instrument, path, old, pend)):
                # The old record still describes this file and this ledger:
                # keep its seq, and count the hub's own complete rows written
                # after it (an append whose sidecar update failed), so adopt
                # never makes the hub write them twice.
                if old["seq"] >= seq:
                    seq, end = old["seq"], old.get("last_line_end")
                own_seq, own_end = self._own_rows_after(instrument, path, old, size)
                if own_seq > seq:
                    seq, end = own_seq, own_end
            _hash_extend(path, hasher, old["size"], size)
        else:
            hasher = _hash_prefix(path, size) if size else hashlib.sha256()
        n = min(size, self.tail_bytes)
        tail = _read_range(path, size - n, n)
        stamp = store.now_iso()
        side = {"instrument": instrument, "size": size, "sha256": hasher.hexdigest(),
                "seq": seq, **self._link(instrument, seq, end),
                **_tail_fields(tail, st.st_mtime_ns), "updated_at": stamp,
                "adopted_at": stamp, "adopted_by": by}
        _write_sidecar(path, side)
        self._verified[str(path)] = _Verified(size, hasher, tail, st.st_mtime_ns)
        self._last_full[str(path)] = self._clock()
        LOGGER.info("exports: %s adopted %s at %d bytes (seq %d) by %s",
                    instrument, path, size, seq, by)
        return side

    def _own_rows_after(self, instrument: str, path: Path, old: dict, size: int) -> tuple:
        """``(seq, end offset)`` of the last pending row that sits, whole and in
        order, right after the old sidecar's size (``(0, None)`` if none)."""
        rows = [r for r in store.export_rows.pending_hub_appends(instrument, db=self.db)
                if r["seq"] > old["seq"]]
        if not rows or size <= old["size"]:
            return 0, None
        expect = b"".join(r["line"].encode("utf-8") for r in rows)
        pos = 0
        if old["size"] == 0:
            expect = header_line().encode("utf-8") + expect
            pos = len(header_line().encode("utf-8"))
        extra = _read_range(path, old["size"], min(size - old["size"], len(expect)))
        if old["size"] == 0 and extra[:pos] != expect[:pos]:
            return 0, None
        last, end = 0, None
        for r in rows:
            line = r["line"].encode("utf-8")
            if extra[pos:pos + len(line)] != line:
                break
            pos += len(line)
            last, end = r["seq"], old["size"] + pos
        return last, end

    @staticmethod
    def _check_header(path: Path, head: bytes) -> None:
        if head.startswith(b"\xef\xbb\xbf"):
            raise ExportRefused("header-mismatch", path, "the file starts with a UTF-8 BOM "
                                "(was it re-saved by Excel?); v1 never wrote one")
        first = head.split(b"\n", 1)[0]
        try:
            cells = next(csv.reader([first.decode("utf-8").rstrip("\r")]), [])
        except UnicodeDecodeError:
            cells = None
        if cells != CSV_HEADER:
            raise ExportRefused("header-mismatch", path, "the first line is not the 31-column "
                                f"results header (got {first[:120]!r})")

    def new_path(self, instrument: str, path: PathLike) -> Path:
        """Switch this instrument's export to *path* (an existing file there
        must then be adopted before the hub appends to it). Refuses ``in-use``."""
        with _instrument_lock(instrument):
            path = Path(path)
            self._check_free(instrument, path)
            store.instruments.upsert({"id": instrument, "export_path": str(path)}, db=self.db)
            self._clear_refusal(instrument)
            if _stat(path) is not None and not sidecar_path(path).exists():
                self._adopt_next[instrument] = path      # "New path, then Adopt"
            LOGGER.info("exports: %s now exports to %s", instrument, path)
            return path

    def after_purge(self, instrument: str) -> dict:
        """After ``purge.py`` deleted this instrument's ledger rows: when the
        sidecar's ``seq`` names a row that is gone, drop the line link
        (``last_line_sha256``/``last_line_end``) and keep everything else.

        Without this the next flush would refuse ``ledger-mismatch`` (the
        restore-from-backup check). ``seq``, size and hash stay, so the file is
        still verified as the hub left it, and new rows (``export_rows.seq`` is
        AUTOINCREMENT: never reused) append after it; the next append links
        the sidecar to a ledger row again. The file itself is never touched.
        Returns ``{path, relinked}``; refuses ``busy`` like the other admin
        calls. The caller holds the instrument across its delete and this call
        (``_instrument_lock``) so no flush sees the gap in between.
        """
        with _instrument_lock(instrument):
            path = self.export_path(instrument)
            with _admin_lock(path):
                side = _read_sidecar(path)
                if (side is None or side.get("instrument") != instrument
                        or not side.get("last_line_sha256")
                        or self._ledger_line(instrument, side["seq"]) is not None):
                    return {"path": str(path), "relinked": False}
                side = dict(side, last_line_sha256=None, last_line_end=None,
                            updated_at=store.now_iso(), purged_at=store.now_iso())
                _write_sidecar(path, side)
            LOGGER.warning("exports: %s sidecar of %s unlinked from purged ledger row %d",
                           instrument, path, side["seq"])
            return {"path": str(path), "relinked": True}

    def _fresh_lines(self, instrument: str) -> tuple:
        """(lines, max ledger seq) for every gated sample's current revision,
        read in one snapshot."""
        with store.connection(self.db) as conn:
            own_txn = not conn.in_transaction
            if own_txn:
                conn.execute("BEGIN")
            try:
                max_seq = conn.execute("SELECT MAX(seq) FROM export_rows WHERE instrument_id=?",
                                       (instrument,)).fetchone()[0] or 0
                rows = conn.execute(
                    'SELECT s.id, s.cdf_path, s.injection_dt, r.results, '
                    '(SELECT e."row" FROM export_rows e WHERE e.sample_id=s.id '
                    '   AND e.revision=s.current_revision ORDER BY e.seq DESC LIMIT 1) AS line, '
                    '(SELECT MAX(e.seq) FROM export_rows e WHERE e.sample_id=s.id '
                    '   AND e.revision=s.current_revision) AS lseq '
                    'FROM samples s JOIN sample_results r '
                    '  ON r.sample_id=s.id AND r.revision=s.current_revision '
                    f'WHERE s.instrument_id=? AND {store.GATE_SQL}',
                    (instrument,)).fetchall()
            finally:
                if own_txn:
                    conn.execute("COMMIT")
        with_seq = sorted((r for r in rows if r["lseq"] is not None), key=lambda r: r["lseq"])
        without = sorted((r for r in rows if r["lseq"] is None), key=lambda r: r["id"])
        lines = [r["line"] for r in with_seq]
        lines += [format_line(with_injection_dt(r["results"], r["injection_dt"]), r["cdf_path"])
                  for r in without]
        return lines, int(max_seq)

    def write_fresh(self, instrument: str, path: PathLike, *, by: Optional[str] = None) -> int:
        """Admin: write every gated current revision to a **new** file, switch
        the export to it, then mark the covered pending rows. Refuses
        ``in-use`` and ``exists``. Returns the row count."""
        with _instrument_lock(instrument):
            path = Path(path)
            self._check_free(instrument, path)
            if path.exists() or sidecar_path(path).exists():
                raise ExportRefused("exists", path, "write fresh only creates a new file")
            lines, max_seq = self._fresh_lines(instrument)
            pieces = [header_line().encode("utf-8")] + [ln.encode("utf-8") for ln in lines]
            data = b"".join(pieces)
            with _admin_lock(path):
                try:
                    self._write_new(path, _chunks(pieces, 0))
                    stamp = store.now_iso()
                    st = path.stat()
                    hasher = hashlib.sha256(data)
                    tail = data[-self.tail_bytes:]
                    _create_sidecar(path, {
                        "instrument": instrument, "size": len(data),
                        "sha256": hasher.hexdigest(), "seq": max_seq,
                        **self._link(instrument, max_seq, None),
                        **_tail_fields(tail, st.st_mtime_ns), "updated_at": stamp,
                        "adopted_at": stamp, "adopted_by": by})
                except FileExistsError as exc:
                    raise ExportRefused("exists", path,
                                        "write fresh only creates a new file") from exc
                self._verified[str(path)] = _Verified(len(data), hasher, tail, st.st_mtime_ns)
            # Switch first: a crash before the marking below leaves the rows
            # pending with seq <= the sidecar's, which the next flush marks.
            self.new_path(instrument, path)
            covered = [r["seq"] for r in store.export_rows.pending_hub_appends(instrument, db=self.db)
                       if r["seq"] <= max_seq]
            store.export_rows.mark_hub_appended(covered, db=self.db)
            live.publish("instrument", {"instrument_id": instrument})
            LOGGER.info("exports: %s wrote a fresh file %s (%d rows, seq %d) by %s",
                        instrument, path, len(lines), max_seq, by)
            return len(lines)

    # ── background ──

    def tick(self) -> dict:
        """One pass: the daily full check where due, then flush every
        instrument; never raises."""
        out = {}
        now = self._clock()
        for inst in store.instruments.list(db=self.db):
            iid = inst["id"]
            try:
                key = str(self.export_path(iid))
                last = self._last_full.get(key)
                if (last is not None and now - last >= FULL_VERIFY_SECONDS
                        and now - self._full_attempt.get(key, float("-inf"))
                        >= FULL_VERIFY_RETRY_SECONDS):
                    self._full_attempt[key] = now     # a failure waits, not every tick
                    self.verify_full(iid)
                r = self.flush(iid)
                out[iid] = {"appended": r.appended, "pending": r.pending, "error": r.error,
                            "refused": None}
            except ExportRefused as err:
                out[iid] = {"appended": 0, "pending": None, "error": str(err),
                            "refused": err.reason}
            except Exception as exc:  # the loop must survive anything
                LOGGER.exception("exports: flush of %s failed", iid)
                out[iid] = {"appended": 0, "pending": None, "error": repr(exc), "refused": None}
        return out

    def start(self, interval: float = FLUSH_INTERVAL_SECONDS,
              join_timeout: float = 60.0) -> threading.Thread:
        """Start the flush loop. A previous thread whose ``stop`` timed out
        (an append in progress) is waited for first, so two loops never run
        at once; ``RuntimeError`` if it is still running after
        ``join_timeout``."""
        if self._thread is not None and self._thread.is_alive():
            if not self._stop.is_set():
                return self._thread
            self._thread.join(join_timeout)
            if self._thread.is_alive():
                raise RuntimeError("the previous export thread is still running")
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self._wake.clear()
                self.ticking = True
                try:
                    self.tick()
                finally:
                    self.ticking = False
                self._wake.wait(interval)

        self._thread = threading.Thread(target=loop, name="gc-hub-exports", daemon=True)
        self._thread.start()
        return self._thread

    def wake(self) -> None:
        """Run the next background pass now (a result just became final)
        instead of waiting out the interval."""
        self._wake.set()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float = 10.0) -> None:
        """Ask the loop to end and wait up to ``timeout``. A thread still
        busy after that is kept (``is_alive`` stays true) and finishes on
        its own; ``start`` waits for it."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if not self._thread.is_alive():
                self._thread = None
