"""The hub's ingest pipeline (phase 2, 2A1): ``submit`` and the ``Worker``.

Every CDF enters through ``submit``. It is stored once (by sha256) under the
data folder, recorded as a ``received`` sample, and a durable ``process``
job is queued. One ``Worker`` thread per process claims those jobs and runs
the status machine (spec, "Status machine"):

1. the CDF's method name is missing → ``review_method``; not in the
   instrument's ``method_map``, or mapped to a hub method that isn't
   registered → ``other_method`` (stored, never processed);
2. the instrument's calibration is unusable → ``awaiting_calibration``
   (queued again by ``on_calibration_saved``);
3. the blank is the latest genuine blank on the same instrument injected at
   or before the sample, whose method maps to the same hub method. A sample
   whose name is a blank name (``is_blank_name``) is never blank-subtracted,
   genuine or not (v1);
4. corrections come from the provider; ``CorrectionsUnavailable`` →
   ``pending_corrections`` and the job is retried in 5 minutes; a
   ``sqlite3.Error`` or ``OSError`` reading them leaves the sample as it was
   and retries in 60 s (never ``error``); anything else is an ``error``;
5. otherwise the method computes the result (outside any transaction, with
   ``strict_blank``), then **one** ``write_txn`` writes the revision, the
   export row (only when the gate passes: ``backfill=0`` or released) and
   ``status='final'``, and completes the job. If the sample's file or
   current revision changed while it was computed, nothing is written and
   the job is requeued;
6. any other exception → ``error`` with the message.

**The blank recorded is the blank subtracted.** ``blank_used`` is set only
when ``compute`` reports ``blank_applied``. A blank that can't be read
(``BlankUnreadable``, or its stored file is missing) is an ``error``. A blank
``compute`` rejects at subtraction (``BlankRejected``: it carries too much
signal for this sample, or leaves no elution window) is dropped: the sample
is computed with **no** blank (never an earlier one), ``blank_used`` is NULL,
the revision's ``notes`` say ``{"blank_rejected": {"sample_id", "reason"}}``,
and a warning is logged.

**Late blanks.** When a genuine blank arrives whose injection time is before
samples already processed, those ``final`` samples (same instrument, same
hub method, injected from this blank up to the next genuine blank, whose
recorded blank is older or none) get ``samples.review_note`` and one
notification is raised. Nothing is reprocessed; a reprocess that picks the
current blank clears the note.

Public API
==========

::

    submit(instrument_id, cdf, mtime=None, source_name=None, *, conf=None,
           data_dir=None, db=None, notifier=None) -> SubmitResult
        # cdf: bytes, or a path (read only; mtime and source_name default to
        # the file's). Raises UnknownInstrument, SubmitRejected.
    SubmitResult(outcome, sha256, sample_id, status, conflict_id, instrument_id, message)
        # outcome: 'created' | 'duplicate' | 'cross_instrument' | 'conflict'
    is_blank_name(name) -> bool
    default_format_line(results_json, source_file) -> str   # v1's csv.writer row, \\r\\n
    request_reprocess(sample_id, *, by=None, use_current_blank=False,
                      use_current_corrections=False, db=None) -> int (job id)
    on_calibration_saved(instrument_id, *, db=None) -> int      # queues awaiting_calibration
    on_method_mapped(instrument_id, method_name, *, db=None) -> int   # queues other_method
    requeue_on_start(*, db=None) -> dict
    sweep_incoming(data_dir) -> int                             # start-up only

    Worker(*, db=None, data_dir=None, conf_fn=None, corrections_provider=None,
           format_line=None, notifier=None, poll_seconds=2.0, now_fn=None)
        .start()            # one per process (another running → RuntimeError);
                            # sweeps .incoming, requeue_on_start, then the thread.
                            # After a stop() that timed out, waits for the old thread.
        .stop(timeout=10)   .wake()   .is_alive()
        .run_once() -> bool       # one due job, in the caller's thread
        .run_until_idle() -> int  # every due job; returns how many ran

``instruments.startup(app_conf, notifier, ...)`` does migrate → gc1
bootstrap → ``Worker.start()`` in one call.

Injection points:

* ``corrections_provider``: an object with ``get(instrument_row) ->
  corrections.Corrections`` (e.g. ``corrections.StoreProvider(read_fn)`` in
  D4b), or a callable ``(global_conf) -> provider``. Default: 2A1's interim
  ``corrections.FileProvider(conf['correction_factors_json'])``, built per
  job, which serves ``gc1`` only (other instruments stay
  ``pending_corrections``).
* ``format_line(results_json, source_file) -> str``: the frozen export line.
  ``results_json`` is the revision's ``results`` column (JSON text keyed by
  ``CSV_HEADER``), ``source_file`` the hub-relative ``cdf_path``. Default
  ``default_format_line``; the hub wires ``exports.format_line``.
* ``conf_fn() -> dict``: the global settings (default
  ``settings.load_settings``), overlaid per instrument by
  ``instruments.context``.
* ``notifier(level, message)``: e.g. ``notifications.get_store().add``.
  Raised once per instrument when a job has retried a transient failure
  ``STUCK_ATTEMPTS`` times (cleared by the next success on that
  instrument), and by ``submit`` for a late blank. ``None`` = log only.

Decisions (where the spec left a choice):

* Files live at ``<data>/cdf/<inst>/<YYYY>/<MM>/<lab>_<sample id>.CDF``
  (``<lab>`` made filename-safe; year and month of the injection time);
  a conflicting file at ``<data>/cdf/<inst>/conflicts/<YYYY>/<MM>/<lab>_<sha12>.CDF``.
  ``cdf_path`` is stored relative to the data folder with ``/``. The stored
  file's mtime is set to the sender's (whole seconds).
* ``lab_id`` is the CDF's sample name with outer whitespace stripped (the
  import matcher's ``normalise_lab_id``); an empty name falls back to the
  sender's file stem (v1). The export row keeps the name as
  ``distill.compute`` reads it, except that an empty one is the ``lab_id``.
* ``injection_dt`` without a CDF stamp is the sender's ``mtime``, truncated
  to whole seconds; bytes with neither are refused (the hub's receive time is
  never used). The results' ``InjectionDateTime`` is always ``injection_dt``.
* A re-sent file that was once held as a conflict (resolved or not) answers
  ``conflict`` with that conflict's id and creates nothing.
* A reprocess (``request_reprocess``) keeps the recorded blank and
  corrections (D5) unless asked for current ones, or the revision is
  legacy (``reason='import'`` or corrections ``source='legacy'``). A
  reprocess of a ``final`` sample that fails for any reason leaves it
  ``final`` at its revision, fails the job, and sets ``error`` to
  ``"last reprocess failed: ..."`` (cleared by the next success).
* The hold statuses keep their reason in ``samples.error`` for the UI;
  ``final`` clears it.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union

import corrections as corrections_mod
import distill
import instruments
import methods
import paths
import store

log = logging.getLogger("pipeline")

PROCESS = "process"
PENDING_CORRECTIONS_RETRY = timedelta(minutes=5)
TRANSIENT_RETRY = timedelta(seconds=60)
STUCK_ATTEMPTS = 10
INCOMING_DIR = ".incoming"
LATE_BLANK_NOTE = "earlier-injected blank arrived after processing (blank sample {blank_id})"

# A genuine blank's name: "Blank", "blank2", "Blank - 1", "(Blank)", "[b] Blank2".
_BLANK_NAME = re.compile(
    r"^(?:\[b\]\s*)?(?:blank[\s_\-]*\d*|\(\s*blank[\s_\-]*\d*\s*\))$", re.IGNORECASE)
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

Notifier = Callable[[str, str], Any]


class UnknownInstrument(LookupError):
    """``submit`` to an instrument the store doesn't have."""


class SubmitRejected(ValueError):
    """The hub won't take this file: the instrument is disabled, or the body is
    not a CDF it can identify (unreadable, or no injection time)."""


@dataclass(frozen=True)
class SubmitResult:
    """What ``submit`` did.

    * ``created``: a new sample (``sample_id``, ``status='received'``).
    * ``duplicate``: this instrument already holds the file (``sample_id``,
      its ``status``).
    * ``cross_instrument``: another instrument holds it (``sample_id`` and
      ``instrument_id`` are that sample's; ``sample_id`` is None when it is
      held there as a conflict).
    * ``conflict``: same (lab ID, injection time) as ``sample_id`` with a
      different file; held in ``conflicts`` (``conflict_id``) for review.
    """
    outcome: str
    sha256: str
    sample_id: Optional[int] = None
    status: Optional[str] = None
    conflict_id: Optional[int] = None
    instrument_id: Optional[str] = None
    message: str = ""


# ── small helpers ───────────────────────────────────────────────────────────

def is_blank_name(name: Optional[str]) -> bool:
    """True for the exact blank names (case-insensitive): ``blank``, ``blank2``,
    ``blank_3``, ``(blank)``, ``[b] blank``... never a substring match."""
    return bool(_BLANK_NAME.match((name or "").strip()))


def default_format_line(results_json: Union[str, bytes, dict], source_file: str) -> str:
    """The export line v1 would append for these results: ``csv.writer``'s
    default dialect (``\\r\\n`` terminator), the ``CSV_HEADER`` columns in
    order, ``Source File`` = ``source_file``. ``results_json`` is the stored
    JSON text (a dict is accepted too)."""
    results = (json.loads(results_json) if isinstance(results_json, (str, bytes))
               else dict(results_json))
    results["Source File"] = source_file
    buf = io.StringIO()
    csv.writer(buf).writerow([results.get(col, "") for col in distill.CSV_HEADER])
    return buf.getvalue()


def _data_dir(data_dir) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    d = paths.data_dir()
    if d is None:
        raise RuntimeError(f"{paths.DATA_ENV} is not set; the hub pipeline needs a data folder")
    return d


def _db(db, data_dir: Path):
    return db if db is not None else data_dir / store.DB_FILENAME


def _load_conf() -> dict:
    import settings
    return settings.load_settings()


def _naive_local(value) -> Optional[datetime]:
    """The sender's file time as a naive local datetime in whole seconds (None stays None)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
    if not isinstance(value, datetime):
        raise TypeError(f"mtime must be a datetime, ISO string or epoch seconds, not {value!r}")
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.replace(microsecond=0)


def _safe_stem(lab_id: str) -> str:
    stem = _UNSAFE_FILENAME.sub("_", lab_id).strip().rstrip(". ")
    return (stem or "Sample")[:80]


def _rel(p: Path, data_dir: Path) -> str:
    return p.relative_to(data_dir).as_posix()


def _notify(notifier: Optional[Notifier], level: str, message: str) -> None:
    (log.error if level == "error" else log.warning)("pipeline: %s", message)
    if notifier is None:
        return
    try:
        notifier(level, message)
    except Exception:  # noqa: BLE001 - a notification must never break processing
        log.exception("pipeline: notifier failed")


def sweep_incoming(data_dir) -> int:
    """Delete everything left in ``<data>/cdf/.incoming`` (a crash mid-submit).
    Start-up only, before any submit can run. Returns how many were removed."""
    incoming = Path(data_dir) / "cdf" / INCOMING_DIR
    n = 0
    if incoming.is_dir():
        for p in incoming.iterdir():
            try:
                if p.is_file():
                    p.unlink()
                    n += 1
            except OSError:
                log.warning("pipeline: could not remove %s", p)
    if n:
        log.info("pipeline: removed %d leftover incoming file(s)", n)
    return n


# ── submit ──────────────────────────────────────────────────────────────────

def _existing_result(sha: str, instrument_id: str, db) -> Optional[SubmitResult]:
    s = store.samples.find_by_sha(sha, db=db)
    if s is not None:
        if s["instrument_id"] == instrument_id:
            return SubmitResult("duplicate", sha, s["id"], s["status"], instrument_id=instrument_id,
                                message="already received")
        return SubmitResult("cross_instrument", sha, s["id"], s["status"],
                            instrument_id=s["instrument_id"],
                            message=f"this file is already held by instrument {s['instrument_id']}")
    c = store.conflicts.find_by_sha(sha, unresolved_only=False, db=db)
    if c is not None:
        if c["instrument_id"] == instrument_id:
            return SubmitResult("conflict", sha, c["existing_sample_id"], None, c["id"],
                                instrument_id, "held for review (conflict)")
        return SubmitResult("cross_instrument", sha, None, None, c["id"], c["instrument_id"],
                            f"this file is held for review on instrument {c['instrument_id']}")
    return None


def _flag_late_blank(conn, inst: dict, blank_id: int, blank_dt: str, method_name: str) -> list:
    """Mark the final samples a newly received genuine blank would have served
    (see the module docstring); return their ids. Runs inside ``write_txn``."""
    mm = instruments.method_map(inst)
    hub_method = mm.get(method_name)
    if hub_method is None:
        return []
    names = methods.names_mapped_to(mm, hub_method)
    marks = ", ".join("?" for _ in names)
    nxt = conn.execute(
        f"SELECT MIN(injection_dt) FROM samples WHERE instrument_id=? AND is_blank=1 "
        f"AND cdf_path IS NOT NULL AND method_name IN ({marks}) AND injection_dt>? AND id<>?",
        [inst["id"], *names, blank_dt, blank_id]).fetchone()[0]
    sql = (f"SELECT s.id, s.lab_id FROM samples s JOIN sample_results r "
           f"ON r.sample_id=s.id AND r.revision=s.current_revision "
           f"LEFT JOIN samples b ON b.id=r.blank_used "
           f"WHERE s.instrument_id=? AND s.status='final' AND s.is_blank=0 "
           f"AND s.method_name IN ({marks}) AND s.injection_dt>=? "
           f"AND (r.blank_used IS NULL OR b.injection_dt<?)")
    args: list = [inst["id"], *names, blank_dt, blank_dt]
    if nxt is not None:
        sql += " AND s.injection_dt<?"
        args.append(nxt)
    flagged = [r["id"] for r in conn.execute(sql + " ORDER BY s.injection_dt", args)
               if not is_blank_name(r["lab_id"])]
    note = LATE_BLANK_NOTE.format(blank_id=blank_id)
    for sid in flagged:
        store.samples.update(sid, review_note=note, db=conn)
    return flagged


def submit(instrument_id: str, cdf: Union[bytes, bytearray, memoryview, str, os.PathLike],
           mtime: Union[None, datetime, str, float] = None, source_name: Optional[str] = None, *,
           conf: Optional[dict] = None, data_dir=None, db: store.Db = None,
           notifier: Optional[Notifier] = None) -> SubmitResult:
    """Receive one CDF for ``instrument_id``.

    ``cdf`` is the file's bytes or a path (read only, never moved or
    modified; ``mtime`` and ``source_name`` then default to the file's).
    ``mtime`` is the sender's file time (naive local ``datetime``, the
    ``X-GC-Mtime`` ISO string, or epoch seconds), used only when the CDF has
    no injection stamp. ``conf`` is the global settings (default
    ``settings.load_settings()``; only ``blank_max_intensity_pa`` is read).
    ``notifier`` hears about a late blank (see the module docstring).
    """
    data_dir = _data_dir(data_dir)
    db = _db(db, data_dir)
    inst = store.instruments.get(instrument_id, db=db)
    if inst is None:
        raise UnknownInstrument(f"unknown instrument {instrument_id!r}")
    if not inst.get("enabled", 1):
        raise SubmitRejected(f"instrument {instrument_id} is disabled")
    if isinstance(cdf, (bytes, bytearray, memoryview)):
        body = bytes(cdf)
    else:
        p = Path(cdf)
        body = p.read_bytes()
        if source_name is None:
            source_name = p.name
        if mtime is None:
            mtime = p.stat().st_mtime
    sender_mtime = _naive_local(mtime)
    sha = hashlib.sha256(body).hexdigest()

    known = _existing_result(sha, instrument_id, db)
    if known is not None:
        return known

    incoming = data_dir / "cdf" / INCOMING_DIR
    incoming.mkdir(parents=True, exist_ok=True)
    tmp = incoming / f"{uuid.uuid4().hex}.CDF"
    tmp.write_bytes(body)
    final: Optional[Path] = None
    flagged: list = []
    try:
        fallback = Path(source_name).stem if source_name else None
        try:
            sample, inj, dt_source, method_name, raw_stamp = distill.cdf_identity(
                tmp, mtime=sender_mtime, fallback_name=fallback)
        except Exception as exc:  # noqa: BLE001 - netCDF raises many kinds
            raise SubmitRejected(f"not a readable CDF: {exc}") from exc
        if dt_source == "mtime" and sender_mtime is None:
            raise SubmitRejected("the CDF has no injection time and no file time was sent")
        if sender_mtime is not None:
            ts = sender_mtime.timestamp()
            os.utime(tmp, (ts, ts))
        lab_id = sample.strip()
        injection_dt = inj.isoformat(sep=" ")
        v1 = distill.v1_parse_injection_datetime(raw_stamp)
        if v1 is not None:
            legacy = v1.isoformat(sep=" ")
        else:   # v1 fell back to the file time
            legacy = sender_mtime.isoformat(sep=" ") if sender_mtime is not None else injection_dt
        conf = conf if conf is not None else _load_conf()
        is_blank = 0
        if is_blank_name(lab_id):
            try:
                limit = float(conf.get("blank_max_intensity_pa", distill.BLANK_MAX_INTENSITY_PA))
            except (TypeError, ValueError):
                limit = distill.BLANK_MAX_INTENSITY_PA
            is_blank = int(distill.is_plausible_blank(tmp, limit))
        backfill = int(store.is_backfill(inst.get("live_since"), injection_dt))
        month_dir = data_dir / "cdf" / instrument_id / f"{inj.year:04d}" / f"{inj.month:02d}"

        with store.connection(db) as conn:
            with store.write_txn(conn):
                known = _existing_result(sha, instrument_id, conn)
                if known is not None:
                    return known
                existing = store.samples.find_by_key(instrument_id, lab_id, injection_dt, db=conn)
                if existing is not None:
                    cdir = (data_dir / "cdf" / instrument_id / "conflicts"
                            / f"{inj.year:04d}" / f"{inj.month:02d}")
                    cdir.mkdir(parents=True, exist_ok=True)
                    final = cdir / f"{_safe_stem(lab_id)}_{sha[:12]}.CDF"
                    cid = store.conflicts.add(instrument_id, lab_id, injection_dt, existing["id"],
                                              sha, _rel(final, data_dir), db=conn)
                    os.replace(tmp, final)
                    log.warning("pipeline: %s %s at %s conflicts with sample %s; held as conflict %s",
                                instrument_id, lab_id, injection_dt, existing["id"], cid)
                    return SubmitResult("conflict", sha, existing["id"], None, cid, instrument_id,
                                        "same lab ID and injection time as an existing sample "
                                        "with a different file; held for review")
                sid = store.samples.insert_received(
                    instrument_id, lab_id, injection_dt, dt_source, cdf_sha256=sha, cdf_path=None,
                    method_name=method_name, source_name=source_name, is_blank=is_blank,
                    backfill=backfill, legacy_injection_dt=legacy,
                    time_corrected=int(legacy != injection_dt), db=conn)
                month_dir.mkdir(parents=True, exist_ok=True)
                final = month_dir / f"{_safe_stem(lab_id)}_{sid}.CDF"
                store.samples.update(sid, cdf_path=_rel(final, data_dir), db=conn)
                store.jobs.enqueue(PROCESS, {"sample_id": sid}, sample_id=sid, db=conn)
                if is_blank:
                    flagged = _flag_late_blank(conn, inst, sid, injection_dt, method_name)
                os.replace(tmp, final)
        log.info("pipeline: received %s %s at %s as sample %s", instrument_id, lab_id,
                 injection_dt, sid)
        if flagged:
            _notify(notifier, "warning",
                    f"{len(flagged)} final sample(s) on {inst.get('name') or instrument_id} were "
                    f"processed before an earlier-injected blank arrived (blank sample {sid}, "
                    f"{lab_id}, injected {injection_dt}). They are marked for review and were not "
                    f"reprocessed.")
        return SubmitResult("created", sha, sid, "received", instrument_id=instrument_id)
    except BaseException:
        # The transaction rolled back (or never started): drop a file it placed.
        if final is not None and final.exists() and not tmp.exists():
            try:
                with store.connection(db) as conn:
                    held = conn.execute(
                        "SELECT 1 FROM samples WHERE cdf_sha256=? UNION ALL "
                        "SELECT 1 FROM conflicts WHERE cdf_sha256=?", (sha, sha)).fetchone()
                if held is None:
                    final.unlink()
            except Exception:  # noqa: BLE001
                log.exception("pipeline: could not tidy %s", final)
        raise
    finally:
        if tmp.exists():
            tmp.unlink()


# ── hooks ───────────────────────────────────────────────────────────────────

def request_reprocess(sample_id: int, *, by: Optional[str] = None, use_current_blank: bool = False,
                      use_current_corrections: bool = False, db: store.Db = None) -> int:
    """Queue a reprocess of one sample (last request wins while it is queued)."""
    return store.jobs.enqueue(PROCESS, {
        "sample_id": sample_id, "reason": "reprocess", "by": by,
        "use_current_blank": bool(use_current_blank),
        "use_current_corrections": bool(use_current_corrections),
    }, sample_id=sample_id, db=db)


def on_calibration_saved(instrument_id: str, *, db: store.Db = None) -> int:
    """After a calibration save: queue that instrument's ``awaiting_calibration`` samples."""
    return store.jobs.enqueue_for_status(instrument_id, "awaiting_calibration", db=db)


def on_method_mapped(instrument_id: str, method_name: str, *, db: store.Db = None) -> int:
    """After mapping a method name: queue that instrument's ``other_method`` samples with it."""
    return store.jobs.enqueue_for_status(instrument_id, "other_method",
                                         method_name=methods.normalise_method_name(method_name), db=db)


def requeue_on_start(*, db: store.Db = None) -> dict:
    """Start-up: stale ``running`` jobs back to ``queued``, and every ``received``
    and ``pending_corrections`` sample queued (due now)."""
    counts = {"stale_running": store.jobs.requeue_stale_running(db=db), "received": 0,
              "pending_corrections": 0}
    for inst in store.instruments.list(db=db):
        for status in ("received", "pending_corrections"):
            counts[status] += store.jobs.enqueue_for_status(inst["id"], status, db=db)
    return counts


# ── the worker ──────────────────────────────────────────────────────────────

class _Hold(Exception):
    """End the job with the sample in a hold status."""

    def __init__(self, status: str, message: Optional[str] = None, retry: Optional[timedelta] = None):
        super().__init__(message or status)
        self.status = status
        self.message = message
        self.retry = retry


class _Transient(Exception):
    """Leave the sample as it is and retry the job later."""


class _Stale(Exception):
    """The sample changed while it was computed: write nothing, requeue now."""


def _legacy_revision(rev: Optional[dict]) -> bool:
    if rev is None:
        return True
    if rev.get("reason") == "import":
        return True
    try:
        used = json.loads(rev.get("corrections_used") or "null")
    except ValueError:
        return True
    return not isinstance(used, dict) or used.get("source") == "legacy"


class Worker:
    """The one processing thread per process. See the module docstring."""

    _active_lock = threading.Lock()
    _active: Optional["Worker"] = None

    def __init__(self, *, db: store.Db = None, data_dir=None,
                 conf_fn: Optional[Callable[[], dict]] = None,
                 corrections_provider: Any = None,
                 format_line: Optional[Callable[[str, str], str]] = None,
                 notifier: Optional[Notifier] = None,
                 poll_seconds: float = 2.0,
                 now_fn: Optional[Callable[[], datetime]] = None) -> None:
        self.data_dir = _data_dir(data_dir)
        self.db = _db(db, self.data_dir)
        self.conf_fn = conf_fn or _load_conf
        self.corrections_provider = corrections_provider
        self.format_line = format_line or default_format_line
        self.notifier = notifier
        self.poll_seconds = poll_seconds
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stuck: set = set()      # instruments already notified about stuck retries

    # lifecycle
    def start(self, join_timeout: float = 60.0) -> None:
        """Sweep ``.incoming``, requeue, and start the thread. Raises
        ``RuntimeError`` if another Worker is running in this process, or if
        this one's previous thread (a ``stop`` that timed out) doesn't finish
        within ``join_timeout``."""
        with Worker._active_lock:
            other = Worker._active
            if other is not None and other is not self and other.is_alive():
                raise RuntimeError("a pipeline Worker is already running in this process")
            if self._thread is not None and self._thread.is_alive():
                if not self._stop.is_set():
                    return
                self._thread.join(join_timeout)
                if self._thread.is_alive():
                    raise RuntimeError("the previous worker thread has not stopped")
            Worker._active = self
            sweep_incoming(self.data_dir)
            counts = requeue_on_start(db=self.db)
            log.info("pipeline: worker starting (requeued %s)", counts)
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(target=self._loop, name="gc-pipeline-worker", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
        with Worker._active_lock:
            if Worker._active is self and not self.is_alive():
                Worker._active = None

    def wake(self) -> None:
        self._wake.set()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self.run_once()
            except Exception:  # noqa: BLE001 - the thread must survive a bad moment
                log.exception("pipeline: worker loop error")
                ran = False
            if not ran and not self._stop.is_set():
                self._wake.wait(self.poll_seconds)
                self._wake.clear()

    def run_until_idle(self, max_jobs: int = 100000) -> int:
        n = 0
        while n < max_jobs and self.run_once():
            n += 1
        return n

    def run_once(self) -> bool:
        """Claim and handle one due ``process`` job; False if none was due."""
        job = store.jobs.claim_next(self.now_fn(), kind=PROCESS, db=self.db)
        if job is None:
            return False
        try:
            self._handle(job)
        except Exception:  # noqa: BLE001
            log.exception("pipeline: job %s crashed", job["id"])
            try:
                store.jobs.fail(job["id"], "worker crash", self.now_fn() + TRANSIENT_RETRY, db=self.db)
            except Exception:  # noqa: BLE001
                log.exception("pipeline: could not reschedule job %s", job["id"])
        return True

    # one job
    def _provider(self, conf: dict):
        p = self.corrections_provider
        if p is None:
            return corrections_mod.FileProvider(conf.get("correction_factors_json", "") or "")
        if hasattr(p, "get"):
            return p
        return p(conf)

    def _retry(self, job: dict, sample: dict, message: str) -> None:
        log.warning("pipeline: sample %s: %s; retrying", sample["id"], message)
        store.jobs.fail(job["id"], message, self.now_fn() + TRANSIENT_RETRY, db=self.db)
        inst_id = sample["instrument_id"]
        if (job.get("attempts") or 0) >= STUCK_ATTEMPTS and inst_id not in self._stuck:
            self._stuck.add(inst_id)
            try:
                inst = store.instruments.get(inst_id, db=self.db) or {}
            except sqlite3.Error:
                inst = {}
            _notify(self.notifier, "error",
                    f"Processing on {inst.get('name') or inst_id} keeps failing and is being "
                    f"retried (sample {sample['id']}, {job.get('attempts')} attempts): {message}")

    def _handle(self, job: dict) -> None:
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        sid = payload.get("sample_id", job.get("sample_id"))
        try:
            sample = store.samples.get(sid, db=self.db) if sid is not None else None
        except sqlite3.Error as exc:
            store.jobs.fail(job["id"], f"store read failed: {exc}", self.now_fn() + TRANSIENT_RETRY,
                            db=self.db)
            return
        if sample is None:
            store.jobs.fail(job["id"], f"sample {sid} does not exist", db=self.db)
            return
        reprocess = payload.get("reason") == "reprocess"
        was_final = sample["status"] == "final"
        if was_final and not reprocess:
            store.jobs.complete(job["id"], db=self.db)
            return
        try:
            self._process(job, sample, payload, reprocess)
        except _Stale as exc:
            log.info("pipeline: sample %s %s; requeued", sid, exc)
            store.jobs.fail(job["id"], str(exc), self.now_fn(), db=self.db)
        except _Transient as exc:
            self._retry(job, sample, str(exc))
        except _Hold as hold:
            if was_final:
                self._fail_reprocess(job, sample, hold.message or hold.status)
                return
            store.samples.set_status(sid, hold.status, error=hold.message, db=self.db)
            if hold.retry is not None:
                store.jobs.fail(job["id"], hold.message, self.now_fn() + hold.retry, db=self.db)
            else:
                store.jobs.complete(job["id"], db=self.db)
            log.info("pipeline: sample %s -> %s (%s)", sid, hold.status, hold.message or "")
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            if was_final:
                self._fail_reprocess(job, sample, message)
                return
            log.exception("pipeline: sample %s failed", sid)
            store.samples.set_status(sid, "error", error=message, db=self.db)
            store.jobs.fail(job["id"], message, db=self.db)

    def _fail_reprocess(self, job: dict, sample: dict, message: str) -> None:
        log.warning("pipeline: reprocess of sample %s failed, left final at revision %s: %s",
                    sample["id"], sample["current_revision"], message)
        store.samples.update(sample["id"], error=f"last reprocess failed: {message}", db=self.db)
        store.jobs.fail(job["id"], message, db=self.db)

    def _process(self, job: dict, sample: dict, payload: dict, reprocess: bool) -> None:
        sid = sample["id"]
        try:
            inst = store.instruments.get(sample["instrument_id"], db=self.db)
            prev = store.get_revision(sid, db=self.db) if reprocess else None
        except sqlite3.Error as exc:
            raise _Transient(f"store read failed: {exc}") from exc

        # 1 method
        name = methods.normalise_method_name(sample.get("method_name"))
        if not name:
            raise _Hold("review_method")
        mm = instruments.method_map(inst)
        hub_method = mm.get(name)
        if hub_method is None:
            raise _Hold("other_method")
        try:
            method = methods.get(hub_method)
        except methods.UnknownMethod:
            raise _Hold("other_method", f"{name} is mapped to {hub_method}, which is not a hub "
                                        f"method this release can process") from None

        # 2 calibration
        conf = self.conf_fn()
        ctx = instruments.context(inst, conf, data_dir=self.data_dir)
        problem = instruments.calibration_problem(ctx)
        if problem:
            raise _Hold("awaiting_calibration", problem)

        # 3 blank (never for a blank-named sample, genuine or not: v1)
        legacy = _legacy_revision(prev)
        blank_named = bool(sample["is_blank"]) or is_blank_name(sample["lab_id"])
        keep_blank = (not blank_named and reprocess and prev is not None and not legacy
                      and not payload.get("use_current_blank"))
        fresh_blank = not blank_named and not keep_blank
        try:
            if blank_named:
                blank = None
            elif keep_blank:
                blank = (store.samples.get(prev["blank_used"], db=self.db)
                         if prev["blank_used"] is not None else None)
            else:
                blank = store.samples.latest_blank(
                    sample["instrument_id"], sample["injection_dt"],
                    methods.names_mapped_to(mm, hub_method), exclude_sample_id=sid, db=self.db)
        except sqlite3.Error as exc:
            raise _Transient(f"store read failed: {exc}") from exc
        blank_path = self.data_dir / blank["cdf_path"] if blank is not None else None
        if blank_path is not None and not blank_path.is_file():
            raise distill.BlankUnreadable(f"blank sample {blank['id']}'s CDF is missing: {blank_path}")

        # 4 corrections (read before any transaction)
        corr = None
        if reprocess and prev is not None and not legacy and not payload.get("use_current_corrections"):
            used = json.loads(prev["corrections_used"])
            corr = corrections_mod.Corrections(
                source=used.get("source", ""), updated_at=used.get("updated_at", ""),
                values=dict(used.get("values") or {}), updated_by=used.get("updated_by", "") or "")
        if corr is None:
            try:
                corr = self._provider(conf).get(inst)
            except corrections_mod.CorrectionsUnavailable as exc:
                raise _Hold("pending_corrections", exc.reason, PENDING_CORRECTIONS_RETRY) from exc
            except (sqlite3.Error, OSError) as exc:   # a read failure is not "not set"
                raise _Transient(f"corrections read failed: {exc}") from exc

        # 5 compute (outside the transaction)
        cdf_path = self.data_dir / sample["cdf_path"]
        notes = None
        try:
            try:
                result = method.compute(cdf_path, ctx, blank_path=blank_path,
                                        corrections=dict(corr.values))
            except distill.BlankRejected as exc:
                if blank is None:
                    raise
                log.warning("pipeline: sample %s: blank sample %s rejected (%s); computed with no "
                            "blank", sid, blank["id"], exc)
                notes = {"blank_rejected": {"sample_id": blank["id"], "reason": str(exc)}}
                blank = None
                result = method.compute(cdf_path, ctx, blank_path=None,
                                        corrections=dict(corr.values))
        except distill.AutoCalibrationRefused as exc:
            raise _Hold("awaiting_calibration", str(exc)) from exc
        row = dict(result["row"])
        if not distill._cdf_names(cdf_path)[0].strip():
            row["Lab ID"] = sample["lab_id"]           # v1 used the sender's file name
        row["InjectionDateTime"] = sample["injection_dt"]
        row["Source File"] = sample["cdf_path"]
        results_json = json.dumps(row)
        line = self.format_line(results_json, sample["cdf_path"])
        score = row.get("Fit Score")
        cal = result["calibration"]
        applied = blank is not None and bool(result.get("blank_applied"))
        extra = {
            "d86_uncorrected": result["d86_uncorrected"],
            "calibration_used": {"cdf": cal["cdf"], "sensitivity": float(ctx["calibration_sensitivity"]),
                                 "anchors_source": cal["anchors_source"], "anchors": cal["anchors"]},
            "blank_used": blank["id"] if applied else None,
            "corrections_used": {"source": corr.source, "updated_at": corr.updated_at,
                                 "updated_by": corr.updated_by, "values": dict(corr.values)},
            "best_fit": row.get("Best Fit") or None,
            "fit_score": float(score) if score not in (None, "") else None,
        }
        reason = "reprocess" if (reprocess and sample["current_revision"] is not None) else "processed"
        self._write_final(sample, job, results_json=results_json, line=line, reason=reason,
                          by=payload.get("by"), extra=extra, notes=notes, clear_review=fresh_blank)

    def _write_final(self, sample: dict, job: dict, *, results_json: str, line: str, reason: str,
                     by: Optional[str], extra: dict, notes: Any = None,
                     clear_review: bool = False) -> int:
        """One transaction: revision, export row (if gated), status final, job done.
        Raises ``_Stale`` (writing nothing) if the sample's file or current
        revision changed since ``sample`` was read."""
        sid = sample["id"]
        with store.connection(self.db) as conn:
            with store.write_txn(conn):
                cur = store.samples.get(sid, db=conn)
                if (cur is None or cur["cdf_sha256"] != sample["cdf_sha256"]
                        or cur["current_revision"] != sample["current_revision"]):
                    raise _Stale("changed while it was being computed")
                rev = store.add_revision(conn, sid, results_json, reason=reason, by=by,
                                         notes=notes, **extra)
                if not cur["backfill"] or cur["released_at"] is not None:
                    store.export_rows.append_pending(conn, cur["instrument_id"], sid, rev, line)
                store.samples.set_status(sid, "final", db=conn)
                if clear_review and cur.get("review_note"):
                    store.samples.update(sid, review_note=None, db=conn)
                store.jobs.complete(job["id"], db=conn)
        self._stuck.discard(sample["instrument_id"])
        log.info("pipeline: sample %s final at revision %s", sid, rev)
        return rev
