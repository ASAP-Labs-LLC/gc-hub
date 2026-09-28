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

    class ExportRefused(Exception):                  # .reason (code), .path, .detail
    class FlushResult:                               # .appended, .pending, .error, .path

    HubExporter(db=None, *, data_dir=None, notifier=None, clock=time.time,
                tail_bytes=TAIL_BYTES, retry_sleep=time.sleep)
        .export_path(instrument) -> Path     # instruments.export_path or <data>/results/<inst>_results.csv
        .flush(instrument) -> FlushResult    # raises ExportRefused; OSError -> result.error, rows stay pending
        .adopt(instrument, *, by=None) -> dict       # admin: accept the file as it is now
        .new_path(instrument, path) -> Path          # admin: switch the export to another file
        .write_fresh(instrument, path, *, by=None) -> int   # admin: new file of gated current revisions
        .tick() -> {instrument: {...}}       # one background pass over every instrument
        .start(interval=FLUSH_INTERVAL_SECONDS) / .stop()
        .status(instrument) -> dict          # for the admin page

``notifier(level, message)`` has the signature of
``notifications.NotificationStore.add``; it is called once when an export is
refused (``error``, until adopt/new path/success clears it) and once when an
instrument's rows have been pending for more than ``PENDING_ALERT_SECONDS``
(``warning``, until the backlog clears).

Frozen row bytes
================

``format_line`` is exactly what v1's ``distill._append_csv_row`` writes for
the same values: a default-dialect ``csv.writer`` (``\\r\\n`` terminator,
minimal quoting, ``None`` as an empty cell) into a file opened with
``newline=""`` and UTF-8. ``results_json`` is the ``sample_results.results``
JSON (or the dict), keyed by ``CSV_HEADER`` names; floats survive the JSON
round-trip exactly (``repr``), so the text of every cell is v1's. Writers
append ``line.encode("utf-8")`` verbatim.

The sidecar and the refusal rules
=================================

``<file>.gchub.json`` is written atomically (temp file, fsync, replace) after
each append::

    {"size", "sha256", "seq", "tail_len", "tail_sha256", "mtime_ns", "updated_at",
     ["adopted_at", "adopted_by"]}

``size``/``sha256`` describe the whole file as the hub last left it; ``seq``
is the last ledger row known to be in it. ``flush`` refuses
(``ExportRefused``) when:

* ``no-sidecar``: the file exists, is not empty and has no sidecar (a v1 file
  must be **adopted**; an empty file is treated as new);
* ``missing``: the sidecar records content but the file is gone;
* ``shrunk``: the file is smaller than recorded;
* ``grown``: it is larger, and the extra bytes are not the start of what the
  hub was about to append (see recovery below);
* ``changed``: the recorded region's hash no longer matches;
* ``sidecar-unreadable``, and ``bad-line`` (a ledger line without a newline
  terminator would corrupt the next append).

``adopt`` refuses ``missing``, ``header-mismatch`` (naming a UTF-8 BOM if
Excel re-saved the file) and ``no-trailing-newline``: v1's last line cut off
mid-record would have the hub's first row glued onto it, so the file has to
be fixed by hand (or a new path chosen) first. ``write_fresh`` refuses
``exists``.

**Cheap verification (why not a full sha256 on every append).** The share
CSV is several MB and grows forever; hashing it over SMB every minute is
slow and holds the file open while LEM and Excel want it. So:

* the *first* check of a file in a process (start-up) and ``adopt`` hash the
  whole recorded region and compare it with ``sha256``. The exporter keeps
  that ``hashlib`` state in memory;
* after that, each flush checks ``size``, ``mtime_ns`` and the sha256 of the
  last ``tail_len`` bytes (``TAIL_BYTES``, 64 KiB) only. A changed mtime at
  the recorded size escalates to a full hash; an unchanged file is accepted
  on the tail alone;
* each append extends the in-memory hash with the bytes it wrote, so the
  sidecar's ``sha256`` is always the true whole-file hash without re-reading.

Trade-off: an edit **before** the last 64 KiB that keeps the size *and* puts
the mtime back is not seen by a running exporter; the next start-up's full
check refuses it. Everything LEM is exposed to by an in-place edit (a
shrink, an append, a save by Excel, which changes size or mtime) is caught at
once.

**Recovery (exactly once).** The order is: append + fsync, then the sidecar,
then ``mark_hub_appended``. A crash (or error) between any two steps heals:

* rows in the file but not in the sidecar: the file is longer than recorded
  and the extra bytes are a prefix of what the hub is about to write, so it
  writes only the rest. The same covers a partial write (a dropped share
  connection mid-write);
* rows in the sidecar but not marked: pending rows with ``seq <=
  sidecar.seq`` are marked without being written again.

A brand-new file gets its sidecar (size 0) *before* the file is created, so
a partial first write is also a recoverable "grown" state rather than an
unexplained file without a sidecar.

**Locks.** A ``PermissionError`` opening the file (Excel, antivirus, a backup)
is retried ``APPEND_OPEN_ATTEMPTS`` times in the call, then reported in
``FlushResult.error`` with the rows left pending; the background loop retries
every ``FLUSH_INTERVAL_SECONDS``. ``pending_since`` is in memory only, so
after a restart the 10-minute alert clock starts again.

**Write fresh** writes the header and every gated sample's current revision
(its ledger line, or ``format_line`` of the stored results when there is
none) to a new file, in ledger order, records the sidecar at the ledger's
max ``seq`` for that instrument, marks the pending rows up to that ``seq``
as appended (the fresh file covers them), and switches ``export_path`` to
it. Never point LEM at a fresh file without setting its tail offset to the
end, or it re-reads all of history.

Limits: two instruments must not share an ``export_path``; nothing stops a
v1 writer still appending to an adopted file except the refusal that follows
(D9b: stop v1's writer before adopting).
"""
from __future__ import annotations

import collections
import csv
import hashlib
import io
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Union

import distill
import paths
import store

LOGGER = logging.getLogger(__name__)

CSV_HEADER = distill.CSV_HEADER
SIDECAR_SUFFIX = ".gchub.json"
TAIL_BYTES = 64 * 1024
FLUSH_INTERVAL_SECONDS = 60
PENDING_ALERT_SECONDS = 600
APPEND_OPEN_ATTEMPTS = 3
APPEND_OPEN_BACKOFF_SECONDS = 0.2
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


# ── errors and results ──────────────────────────────────────────────────────

class ExportRefused(Exception):
    """The hub will not append to this file until an admin acts."""

    def __init__(self, reason: str, path: PathLike, detail: str) -> None:
        self.reason = reason
        self.path = Path(path)
        self.detail = detail
        super().__init__(f"Export to {path} refused ({reason}): {detail}")


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


def _open_append(path: Path):
    """Open *path* for appending (``O_APPEND``; creates it)."""
    return open(path, "ab")


def _hash_prefix(path: Path, nbytes: int) -> "hashlib._Hash":
    """sha256 state of the first *nbytes* of *path* (the one full read)."""
    h = hashlib.sha256()
    left = nbytes
    with open(path, "rb") as fh:
        while left > 0:
            chunk = fh.read(min(_CHUNK, left))
            if not chunk:
                raise OSError(f"{path} is shorter than {nbytes} bytes")
            h.update(chunk)
            left -= len(chunk)
    return h


def _read_range(path: Path, offset: int, length: int) -> bytes:
    if length <= 0:
        return b""
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(length)


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
        if not isinstance(side.get("sha256"), str):
            raise ValueError("sha256")
        return side
    except (ValueError, TypeError, KeyError) as exc:
        raise ExportRefused("sidecar-unreadable", csv_path,
                            f"the sidecar {sp.name} is not valid ({exc}); "
                            "adopt the file or choose a new path") from exc


def _write_sidecar(csv_path: Path, side: dict) -> None:
    """Replace the sidecar atomically (temp file in the same folder, fsync, replace)."""
    sp = sidecar_path(csv_path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{sp.name}.", suffix=".tmp", dir=str(sp.parent))
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


def _tail_fields(tail: bytes, mtime_ns: Optional[int]) -> dict:
    return {"tail_len": len(tail), "tail_sha256": hashlib.sha256(tail).hexdigest(),
            "mtime_ns": mtime_ns}


def _stat(path: Path) -> Optional[os.stat_result]:
    try:
        return path.stat()
    except FileNotFoundError:
        return None


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
        self._refused: dict[str, ExportRefused] = {}
        self._last_error: dict[str, Optional[str]] = {}
        self._pending_since: dict[str, float] = {}
        self._alerted: set[str] = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

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
        if prev is None or (prev.reason, prev.path) != (err.reason, err.path):
            self._notify("error", f"GC export for {instrument} refused: {err}. "
                                  "Admin > Exports: Adopt the file or choose a new path.")

    def _clear_refusal(self, instrument: str) -> None:
        self._refused.pop(instrument, None)

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

    # ── verification ──

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
                 and (size != rec or side.get("mtime_ns") == (st.st_mtime_ns if st else None)))
        want_tail = min(rec, max(self.tail_bytes, int(side.get("tail_len") or 0)))
        tail = _read_range(path, rec - want_tail, want_tail) if rec else b""
        if cheap:
            n = side["tail_len"]
            if n > len(tail) or hashlib.sha256(tail[len(tail) - n:]).hexdigest() != side["tail_sha256"]:
                self._verified.pop(key, None)
                raise ExportRefused("changed", path, "the end of the file is not what the hub "
                                    "last wrote; it was edited by someone else")
            return _Verified(rec, cached.hasher, tail)
        hasher = _hash_prefix(path, rec) if rec else hashlib.sha256()
        if hasher.hexdigest() != side["sha256"]:
            self._verified.pop(key, None)
            raise ExportRefused("changed", path, f"the first {rec} bytes no longer match the "
                                "sidecar's sha256; the file was edited by someone else")
        v = _Verified(rec, hasher, tail)
        self._verified[key] = v
        return v

    # ── flush ──

    def flush(self, instrument: str) -> FlushResult:
        """Append this instrument's pending ledger rows to its export file.

        Raises ``ExportRefused`` (and notifies once) when the file is not in
        the state the sidecar records. An ``OSError`` on the file (a lock, a
        dropped share) is returned in ``FlushResult.error`` with the rows left
        pending for the next attempt.
        """
        with _instrument_lock(instrument):
            path = self.export_path(instrument)
            rows = store.export_rows.pending_hub_appends(instrument, db=self.db)
            if not rows:
                self._last_error.pop(instrument, None)
                self._clear_refusal(instrument)
                self._track_pending(instrument, 0)
                return FlushResult(0, 0, None, path)
            try:
                result = self._flush_rows(instrument, path, rows)
            except ExportRefused as err:
                self._record_refusal(instrument, err)
                self._track_pending(instrument, len(rows))
                raise
            if result.error:
                self._last_error[instrument] = result.error
                LOGGER.warning("exports: %s append to %s failed: %s", instrument, path, result.error)
            else:
                self._last_error.pop(instrument, None)
                self._clear_refusal(instrument)
            self._track_pending(instrument, result.pending)
            return result

    def _flush_rows(self, instrument: str, path: Path, rows: list) -> FlushResult:
        def failed(exc: OSError, pending: int) -> FlushResult:
            return FlushResult(0, pending, f"{type(exc).__name__}: {exc}", path)

        for r in rows:
            if not r["line"].endswith("\n"):
                raise ExportRefused("bad-line", path, f"ledger row seq {r['seq']} has no line "
                                    "terminator; appending it would corrupt the next row")
        side = _read_sidecar(path)
        st = _stat(path)
        if side is None:
            if st is not None and st.st_size > 0:
                raise ExportRefused("no-sidecar", path, "the file exists but the hub has no "
                                    "record of it (no sidecar); Adopt it to append after its "
                                    "current content, or choose a new path")
            # New (or empty) file: record the intent first, so a partial first
            # write is a recoverable "grown" state.
            side = {"size": 0, "sha256": _EMPTY_SHA, "seq": rows[0]["seq"] - 1,
                    **_tail_fields(b"", None), "updated_at": store.now_iso()}
            try:
                if not self._configured_path(instrument):
                    # Only the hub's own folder is created; a configured
                    # (share) folder that is missing is an error to retry.
                    path.parent.mkdir(parents=True, exist_ok=True)
                _write_sidecar(path, side)
            except OSError as exc:
                return failed(exc, len(rows))
            self._verified[str(path)] = _Verified(0, hashlib.sha256(), b"")
        rec = side["size"]
        if st is None and rec > 0:
            raise ExportRefused("missing", path, f"the sidecar records {rec} bytes but the file "
                                "is gone; choose a new path (or restore the file)")
        size = st.st_size if st else 0
        if size < rec:
            raise ExportRefused("shrunk", path, f"the file is {size} bytes, smaller than the "
                                f"{rec} the hub last recorded; it was truncated or replaced")
        try:
            v = self._verify(path, side, st)
        except OSError as exc:
            return failed(exc, len(rows))

        done = [r["seq"] for r in rows if r["seq"] <= side["seq"]]
        todo = [r for r in rows if r["seq"] > side["seq"]]
        if done:  # in the file per the sidecar, not yet marked (a crash in between)
            store.export_rows.mark_hub_appended(done, db=self.db)
        planned = b"".join(r["line"].encode("utf-8") for r in todo)
        if planned and rec == 0:
            planned = header_line().encode("utf-8") + planned
        extra = size - rec
        if extra:
            ours = extra <= len(planned) and _read_range(path, rec, extra) == planned[:extra]
            if not ours:
                raise ExportRefused("grown", path, f"{extra} bytes were added after the hub's "
                                    "last append by someone else (is v1 still writing?); "
                                    "Adopt to append after them, or choose a new path")
        if not todo:
            return FlushResult(0, 0, None, path)

        rest = planned[extra:]
        if rest:
            try:
                self._append(path, rest)
            except OSError as exc:
                return failed(exc, len(todo))
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
        new_side = {"size": new_size, "sha256": hasher.hexdigest(), "seq": todo[-1]["seq"],
                    **_tail_fields(tail, st2.st_mtime_ns if st2 else None),
                    "updated_at": store.now_iso()}
        for k in ("adopted_at", "adopted_by"):
            if k in side:
                new_side[k] = side[k]
        try:
            _write_sidecar(path, new_side)
        except OSError as exc:
            self._verified.pop(str(path), None)
            return failed(exc, len(todo))
        self._verified[str(path)] = _Verified(new_size, hasher, tail)
        store.export_rows.mark_hub_appended([r["seq"] for r in todo], db=self.db)
        return FlushResult(len(todo), 0, None, path)

    def _append(self, path: Path, data: bytes) -> None:
        for attempt in range(APPEND_OPEN_ATTEMPTS):
            try:
                fh = _open_append(path)
                break
            except PermissionError:
                if attempt == APPEND_OPEN_ATTEMPTS - 1:
                    raise
                self._sleep(APPEND_OPEN_BACKOFF_SECONDS * (attempt + 1))
        with fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

    # ── admin actions ──

    def _max_appended_seq(self, instrument: str) -> int:
        with store.connection(self.db) as conn:
            r = conn.execute("SELECT MAX(seq) FROM export_rows WHERE instrument_id=? "
                             "AND hub_appended_at IS NOT NULL", (instrument,)).fetchone()
        return int(r[0] or 0)

    def adopt(self, instrument: str, *, by: Optional[str] = None) -> dict:
        """Accept the export file as it is now: pending rows go after its content.

        Refuses ``missing``, ``header-mismatch`` and ``no-trailing-newline``.
        Returns the sidecar written.
        """
        with _instrument_lock(instrument):
            path = self.export_path(instrument)
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
            hasher = _hash_prefix(path, size) if size else hashlib.sha256()
            n = min(size, self.tail_bytes)
            tail = _read_range(path, size - n, n)
            seq = self._max_appended_seq(instrument)
            try:
                old = _read_sidecar(path)
            except ExportRefused:
                old = None
            if old:
                seq = max(seq, old["seq"])
            stamp = store.now_iso()
            side = {"size": size, "sha256": hasher.hexdigest(), "seq": seq,
                    **_tail_fields(tail, st.st_mtime_ns), "updated_at": stamp,
                    "adopted_at": stamp, "adopted_by": by}
            _write_sidecar(path, side)
            self._verified[str(path)] = _Verified(size, hasher, tail)
            self._clear_refusal(instrument)
            LOGGER.info("exports: %s adopted %s at %d bytes (seq %d) by %s",
                        instrument, path, size, seq, by)
            return side

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
        must then be adopted before the hub appends to it)."""
        with _instrument_lock(instrument):
            path = Path(path)
            store.instruments.upsert({"id": instrument, "export_path": str(path)}, db=self.db)
            self._clear_refusal(instrument)
            LOGGER.info("exports: %s now exports to %s", instrument, path)
            return path

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
                    'SELECT s.id, s.cdf_path, r.results, '
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
        lines += [format_line(r["results"], r["cdf_path"]) for r in without]
        return lines, int(max_seq)

    def write_fresh(self, instrument: str, path: PathLike, *, by: Optional[str] = None) -> int:
        """Admin: write every gated current revision to a **new** file and
        switch the export to it. Refuses ``exists``. Returns the row count."""
        with _instrument_lock(instrument):
            path = Path(path)
            if path.exists() or sidecar_path(path).exists():
                raise ExportRefused("exists", path, "write fresh only creates a new file")
            lines, max_seq = self._fresh_lines(instrument)
            data = header_line().encode("utf-8") + "".join(lines).encode("utf-8")
            try:
                fh = open(path, "xb")
            except FileExistsError as exc:
                raise ExportRefused("exists", path, "write fresh only creates a new file") from exc
            with fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            st = path.stat()
            hasher = hashlib.sha256(data)
            tail = data[-self.tail_bytes:]
            stamp = store.now_iso()
            side = {"size": len(data), "sha256": hasher.hexdigest(), "seq": max_seq,
                    **_tail_fields(tail, st.st_mtime_ns), "updated_at": stamp,
                    "adopted_at": stamp, "adopted_by": by}
            _write_sidecar(path, side)
            self._verified[str(path)] = _Verified(len(data), hasher, tail)
            covered = [r["seq"] for r in store.export_rows.pending_hub_appends(instrument, db=self.db)
                       if r["seq"] <= max_seq]
            store.export_rows.mark_hub_appended(covered, db=self.db)
            self.new_path(instrument, path)
            LOGGER.info("exports: %s wrote a fresh file %s (%d rows, seq %d) by %s",
                        instrument, path, len(lines), max_seq, by)
            return len(lines)

    # ── background ──

    def tick(self) -> dict:
        """One pass: flush every instrument; never raises."""
        out = {}
        for inst in store.instruments.list(db=self.db):
            iid = inst["id"]
            try:
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

    def start(self, interval: float = FLUSH_INTERVAL_SECONDS) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, name="gc-hub-exports", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
