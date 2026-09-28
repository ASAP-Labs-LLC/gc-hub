"""The hub's ingest pipeline (phase 2, 2A1): ``submit`` and the ``Worker``.

Every CDF enters through ``submit``. It is stored once (by sha256) under the
data folder, recorded as a ``received`` sample, and a durable ``process``
job is queued. One ``Worker`` thread claims those jobs and runs the status
machine (spec, "Status machine"):

1. the CDF's method name is missing → ``review_method``; not in the
   instrument's ``method_map`` → ``other_method`` (stored, never processed);
2. the instrument's calibration is unusable → ``awaiting_calibration``
   (queued again by ``on_calibration_saved``);
3. the blank is the latest genuine blank on the same instrument injected at
   or before the sample, whose method maps to the same hub method;
4. corrections come from the provider; ``CorrectionsUnavailable`` →
   ``pending_corrections`` and the job is retried in 5 minutes; any other
   error reading them (a database error) leaves the sample as it was and
   retries, never ``error``;
5. otherwise the method computes the result (outside any transaction), then
   **one** ``write_txn`` writes the revision, the export row (only when the
   gate passes: ``backfill=0`` or released) and ``status='final'``, and
   completes the job;
6. any other exception → ``error`` with the message.

Public API
==========

::

    submit(instrument_id, cdf, mtime=None, source_name=None, *, conf=None,
           data_dir=None, db=None) -> SubmitResult
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

    Worker(*, db=None, data_dir=None, conf_fn=None, corrections_provider=None,
           format_line=None, poll_seconds=2.0, now_fn=None)
        .start()            # requeue_on_start, then the thread
        .stop(timeout=10)   .wake()   .is_alive()
        .run_once() -> bool       # one due job, in the caller's thread
        .run_until_idle() -> int  # every due job; returns how many ran

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

Decisions (where the spec left a choice):

* Files live at ``<data>/cdf/<inst>/<YYYY>/<MM>/<lab>_<sample id>.CDF``
  (``<lab>`` made filename-safe; year and month of the injection time);
  a conflicting file at ``<data>/cdf/<inst>/conflicts/<YYYY>/<MM>/<lab>_<sha12>.CDF``.
  ``cdf_path`` is stored relative to the data folder with ``/``.
* ``lab_id`` is the CDF's sample name with outer whitespace stripped (the
  import matcher's ``normalise_lab_id``); the export row keeps the name as
  ``distill.compute`` reads it.
* ``injection_dt`` without a CDF stamp is the sender's ``mtime``; bytes with
  neither are refused (the hub's receive time is never used).
* A blank is never blank-subtracted itself.
* A reprocess (``request_reprocess``) keeps the recorded blank and
  corrections (D5) unless asked for current ones, or the revision is
  legacy (``reason='import'`` or corrections ``source='legacy'``). A
  reprocess of a ``final`` sample that fails for any reason leaves it
  ``final`` at its revision and fails the job with the message.
* The hold statuses (``awaiting_calibration``, ``pending_corrections``)
  keep their reason in ``samples.error`` for the UI; ``final`` clears it.
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
INCOMING_DIR = ".incoming"

# A genuine blank's name: "Blank", "blank2", "Blank - 1", "(Blank)", "[b] Blank2".
_BLANK_NAME = re.compile(
    r"^(?:\[b\]\s*)?(?:blank[\s_\-]*\d*|\(\s*blank[\s_\-]*\d*\s*\))$", re.IGNORECASE)
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class UnknownInstrument(LookupError):
    """``submit`` to an instrument the store doesn't have."""


class SubmitRejected(ValueError):
    """The body is not a CDF the hub can identify (unreadable, or no injection time)."""


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
    """The sender's file time as a naive local datetime (None stays None)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
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
    return value


def _safe_stem(lab_id: str) -> str:
    stem = _UNSAFE_FILENAME.sub("_", lab_id).strip().rstrip(". ")
    return (stem or "Sample")[:80]


def _rel(p: Path, data_dir: Path) -> str:
    return p.relative_to(data_dir).as_posix()


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
    c = store.conflicts.find_by_sha(sha, db=db)
    if c is not None:
        if c["instrument_id"] == instrument_id:
            return SubmitResult("conflict", sha, c["existing_sample_id"], None, c["id"],
                                instrument_id, "held for review (conflict)")
        return SubmitResult("cross_instrument", sha, None, None, c["id"], c["instrument_id"],
                            f"this file is held for review on instrument {c['instrument_id']}")
    return None


def submit(instrument_id: str, cdf: Union[bytes, bytearray, memoryview, str, os.PathLike],
           mtime: Union[None, datetime, str, float] = None, source_name: Optional[str] = None, *,
           conf: Optional[dict] = None, data_dir=None, db: store.Db = None) -> SubmitResult:
    """Receive one CDF for ``instrument_id``.

    ``cdf`` is the file's bytes or a path (read only, never moved or
    modified; ``mtime`` and ``source_name`` then default to the file's).
    ``mtime`` is the sender's file time (naive local ``datetime``, the
    ``X-GC-Mtime`` ISO string, or epoch seconds), used only when the CDF has
    no injection stamp. ``conf`` is the global settings (default
    ``settings.load_settings()``; only ``blank_max_intensity_pa`` is read).
    """
    data_dir = _data_dir(data_dir)
    db = _db(db, data_dir)
    inst = store.instruments.get(instrument_id, db=db)
    if inst is None:
        raise UnknownInstrument(f"unknown instrument {instrument_id!r}")
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
    try:
        try:
            sample, inj, dt_source, method_name, raw_stamp = distill.cdf_identity(
                tmp, mtime=sender_mtime)
        except Exception as exc:  # noqa: BLE001 - netCDF raises many kinds
            raise SubmitRejected(f"not a readable CDF: {exc}") from exc
        if dt_source == "mtime" and sender_mtime is None:
            raise SubmitRejected("the CDF has no injection time and no file time was sent")
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
                os.replace(tmp, final)
        log.info("pipeline: received %s %s at %s as sample %s", instrument_id, lab_id,
                 injection_dt, sid)
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
    """The one processing thread. See the module docstring."""

    def __init__(self, *, db: store.Db = None, data_dir=None,
                 conf_fn: Optional[Callable[[], dict]] = None,
                 corrections_provider: Any = None,
                 format_line: Optional[Callable[[str, str], str]] = None,
                 poll_seconds: float = 2.0,
                 now_fn: Optional[Callable[[], datetime]] = None) -> None:
        self.data_dir = _data_dir(data_dir)
        self.db = _db(db, self.data_dir)
        self.conf_fn = conf_fn or _load_conf
        self.corrections_provider = corrections_provider
        self.format_line = format_line or default_format_line
        self.poll_seconds = poll_seconds
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # lifecycle
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        counts = requeue_on_start(db=self.db)
        log.info("pipeline: worker starting (requeued %s)", counts)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="gc-pipeline-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

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
            if not ran:
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
        except _Transient as exc:
            log.warning("pipeline: sample %s: %s; retrying", sid, exc)
            store.jobs.fail(job["id"], str(exc), self.now_fn() + TRANSIENT_RETRY, db=self.db)
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
        method = methods.get(hub_method)

        # 2 calibration
        conf = self.conf_fn()
        ctx = instruments.context(inst, conf, data_dir=self.data_dir)
        problem = instruments.calibration_problem(ctx)
        if problem:
            raise _Hold("awaiting_calibration", problem)

        # 3 blank
        legacy = _legacy_revision(prev)
        keep_blank = reprocess and prev is not None and not legacy and not payload.get("use_current_blank")
        try:
            if sample["is_blank"]:
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
            # distill.compute would quietly carry on without a blank while the
            # revision recorded one.
            raise FileNotFoundError(f"blank sample {blank['id']}'s CDF is missing: {blank_path}")

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
            except Exception as exc:  # noqa: BLE001 - a read error is not "not set"
                raise _Transient(f"corrections read failed: {exc}") from exc

        # 5 compute (outside the transaction)
        try:
            result = method.compute(self.data_dir / sample["cdf_path"], ctx, blank_path=blank_path,
                                    corrections=dict(corr.values))
        except distill.AutoCalibrationRefused as exc:
            raise _Hold("awaiting_calibration", str(exc)) from exc
        row = dict(result["row"])
        row["Source File"] = sample["cdf_path"]
        results_json = json.dumps(row)
        line = self.format_line(results_json, sample["cdf_path"])
        score = row.get("Fit Score")
        cal = result["calibration"]
        extra = {
            "d86_uncorrected": result["d86_uncorrected"],
            "calibration_used": {"cdf": cal["cdf"], "sensitivity": float(ctx["calibration_sensitivity"]),
                                 "anchors_source": cal["anchors_source"], "anchors": cal["anchors"]},
            "blank_used": blank["id"] if blank is not None else None,
            "corrections_used": {"source": corr.source, "updated_at": corr.updated_at,
                                 "updated_by": corr.updated_by, "values": dict(corr.values)},
            "best_fit": row.get("Best Fit") or None,
            "fit_score": float(score) if score not in (None, "") else None,
        }
        reason = "reprocess" if (reprocess and sample["current_revision"] is not None) else "processed"
        self._write_final(sample, job, results_json=results_json, line=line, reason=reason,
                          by=payload.get("by"), extra=extra)

    def _write_final(self, sample: dict, job: dict, *, results_json: str, line: str, reason: str,
                     by: Optional[str], extra: dict) -> int:
        """One transaction: revision, export row (if gated), status final, job done."""
        sid = sample["id"]
        with store.connection(self.db) as conn:
            with store.write_txn(conn):
                cur = store.samples.get(sid, db=conn)
                rev = store.add_revision(conn, sid, results_json, reason=reason, by=by, **extra)
                if not cur["backfill"] or cur["released_at"] is not None:
                    store.export_rows.append_pending(conn, cur["instrument_id"], sid, rev, line)
                store.samples.set_status(sid, "final", db=conn)
                store.jobs.complete(job["id"], db=conn)
        log.info("pipeline: sample %s final at revision %s", sid, rev)
        return rev
