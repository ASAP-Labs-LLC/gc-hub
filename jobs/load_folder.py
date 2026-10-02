"""Load CDFs from a folder (phase 2, 2A1 T6): the admin job **Load CDFs from a
folder**, one-shot and read-only on the source (spec, Delivery 2A1).

``load_folder`` finds every ``*.CDF`` under ``folder`` (any case, any depth),
reads each file's identity with ``distill.cdf_identity`` and submits them to
``pipeline.submit`` **in injection-time order** (blanks first at a tie, then
by path). The hub's worker may be processing while the load runs, so a
history blank must arrive before the samples injected after it; otherwise
those samples would be final before their blank arrived and be flagged with
a late-blank review note. The order key is the time ``submit`` will store:
the CDF's stamp, or for a stamp-less file its modification time truncated to
the whole second (``submit``'s rule), so ties are judged on stored times.

* **Read-only.** Files are only read (``submit`` takes the path, reads the
  bytes, and stores its own copy); nothing under ``folder`` is created,
  renamed, touched or deleted.
* **Resumable.** ``submit`` dedupes by sha256, so a second run (or a run
  after an interruption) answers ``duplicate`` for what is already in and
  creates nothing new.
* **Original identity.** ``source_name`` is the file's own name and
  ``mtime`` its own modification time (used only when the CDF has no
  injection stamp).
* **Backfill.** Injections before the instrument's ``live_since`` are
  backfill anyway (``store.is_backfill``). ``backfill=True`` makes every
  created sample backfill (``submit(force_backfill=True)``), for loading
  history into a live instrument. The loader never releases backfill: those
  results are computed but never exported until an admin releases them.
* **No notifications.** ``submit`` gets ``notifier=None``; the summary counts
  the late-blank review notes this run set instead.
* The instrument must exist (``pipeline.UnknownInstrument``) and be enabled
  (``pipeline.InstrumentDisabled`` stops the load); run
  ``instruments.startup``/``bootstrap_gc1`` first.
* **A load that stops partway** (any exception, including a disabled
  instrument or Ctrl-C) re-raises with the summary so far attached as
  ``exc.load_summary`` (``summary["stopped"]`` says why). Re-running resumes.

::

    load_folder(instrument_id, folder, *, backfill, progress=None, db=None,
                data_dir=None, conf=None) -> dict
    process_lock(data_dir)     # context manager: <data>/PROCESS_LOCK, O_EXCL; ProcessLocked if held
    copy_instrument(instrument_id, source_data_dir, *, data_dir, db=None) -> dict
                               # a production instrument into a SCRATCH store (parity runs)

``progress(event)`` receives dicts, for the admin page's SSE stream:
``{"phase": "scan", "total"}`` once; ``{"phase": "identify", "done",
"total", "file"}`` for each file of the identity pre-pass; ``{"phase":
"submit", "done", "total", "file", "outcome", "sample_id", "message"}`` after
each file (``outcome`` is a ``SubmitResult.outcome``, ``"rejected"`` or
``"failed"``) and ``{"phase": "done", "summary"}`` at the end. An exception
raised by the callback stops the load.

The summary::

    {"instrument", "folder", "backfill_forced", "files", "created", "duplicate",
     "conflict", "cross_instrument", "rejected", "truncated", "failed",
     "backfill", "late_blank_review_notes", "sample_ids", "rejected_files":
     [{file, reason}], "failed_files": [{file, reason}], "conflicts": [{file,
     conflict_id, existing_sample_id}], "cross_instrument_files": [{file,
     instrument_id, sample_id}], "started_at", "seconds", ["stopped"]}

``rejected`` counts files ``submit`` refused (``SubmitRejected``: unreadable,
no injection time, truncated, no intensity data); ``truncated`` is the part
of those ``pipeline.cdf_problem`` found cut short. ``failed`` counts files
that could not be read at all (``OSError``). ``backfill`` counts the created
samples that are backfill. ``sample_ids`` are this instrument's samples the
folder's files map to (created or duplicate): the parity report's scope.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Optional

import distill
import paths
import pipeline
import store

log = logging.getLogger("jobs.load_folder")

Progress = Callable[[dict], object]
PROCESS_LOCK = ".gc-load-folder.lock"
_LATE_BLANK_RE = re.compile(re.escape(pipeline.LATE_BLANK_NOTE).replace(
    re.escape("{blank_id}"), r"\d+"))
_FAR_FUTURE = datetime.max


class ProcessLocked(RuntimeError):
    """Another loader holds ``<data>/PROCESS_LOCK``."""


class CopyInstrumentError(RuntimeError):
    """``copy_instrument`` refused: nothing was changed in the target store."""


# What copy_instrument takes from the production row. Never the export path
# (it may be the share CSV LEM tails), the agent token or the LEM uid.
COPIED_COLUMNS = ("name", "method", "live_since", "calibration_cdf", "calibration_assignments",
                  "calibration_sensitivity", "method_map")
COPY_REASON = "copied from {src} for a parity run (tools/load_folder.py --copy-instrument-from)"
CALIBRATION_DIR = "calibration"


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_file(src: Path, dest: Path) -> bool:
    """Copy ``src`` to ``dest`` (with its times) unless ``dest`` already holds the
    same bytes. Refuses to replace a different file. True when copied."""
    if dest.exists():
        if _sha(dest) == _sha(src):
            return False
        raise CopyInstrumentError(f"{dest} already exists with different content; use a fresh "
                                  f"scratch folder")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def copy_instrument(instrument_id: str, source_data_dir, *, data_dir, db: store.Db = None) -> dict:
    """Copy one instrument's configuration from a production data folder into a
    **scratch** store, so a parity run computes with production's calibration,
    method map and corrections (decision I2: the parity gate never runs on
    production). The source is only read: its ``gc.db`` is opened read-only
    and files are copied out of it.

    * The instrument row is created (e.g. GC-2, which a scratch store lacks)
      or updated with ``COPIED_COLUMNS`` and ``enabled=1``. The export path,
      the agent token and the LEM uid are never copied.
    * The calibration CDF is copied into the scratch folder: a data-relative
      path to the same relative path, an absolute one to
      ``calibration/<instrument>/<name>``; the assignments are stored as a
      plain list for the copy.
    * The instrument's hub correction factors are written with
      ``store.corrections.set_all`` (audited, reason ``COPY_REASON``). None in
      production leaves the scratch instrument without (gc1 is then seeded
      from ``settings.json``'s phase-1 file, as the hub does).
    * ``settings.json`` is copied when the scratch folder has none, and the
      comparison standards (``gc_comparison_standards/*.cdf``: the Best Fit
      columns depend on them) that it lacks.
    * The instrument's ``awaiting_calibration`` and ``pending_corrections``
      samples are queued in the same transaction.

    ``CopyInstrumentError`` (nothing changed) when the source has no store or
    no such instrument, its calibration CDF is missing, or a file the copy
    needs already exists with other content. Returns a summary dict."""
    src = Path(source_data_dir)
    data_dir = Path(data_dir)
    db = db if db is not None else data_dir / store.DB_FILENAME
    src_db = src / store.DB_FILENAME
    if src.resolve() == data_dir.resolve():
        raise CopyInstrumentError("the source and the target are the same data folder")
    if not src_db.is_file():
        raise CopyInstrumentError(f"no hub store at {src_db}")
    conn = store.open_db(src_db, readonly=True)
    try:
        row = store.instruments.get(instrument_id, db=conn)
        if row is None:
            raise CopyInstrumentError(f"{src_db} has no instrument {instrument_id!r}")
        corr = store.corrections.read(instrument_id, db=conn)
    finally:
        conn.close()

    import instruments
    fields = {c: row.get(c) for c in COPIED_COLUMNS}
    cal_src = instruments._calibration_path(row.get("calibration_cdf"), src)
    cal_dest_rel = None
    if cal_src is not None:
        if not cal_src.is_file():
            raise CopyInstrumentError(f"{instrument_id}'s calibration CDF is not found: {cal_src}")
        raw = str(row.get("calibration_cdf") or "").strip()
        if not Path(raw).is_absolute():
            cal_dest_rel = Path(raw)
        else:
            try:
                cal_dest_rel = cal_src.resolve().relative_to(src.resolve())
            except ValueError:
                cal_dest_rel = Path(CALIBRATION_DIR) / instrument_id / cal_src.name
        entries = instruments._entries(row.get("calibration_assignments"), cal_src)
        fields["calibration_cdf"] = str(cal_dest_rel)
        fields["calibration_assignments"] = json.dumps(entries) if entries else None
    else:
        fields["calibration_cdf"] = None
        fields["calibration_assignments"] = None
    if not fields.get("name"):
        fields["name"] = instrument_id
    fields["enabled"] = 1

    settings_src = src / "settings.json"
    std_src = src / "gc_comparison_standards"
    standards = sorted(p for p in std_src.iterdir()
                       if p.is_file() and p.suffix.lower() == ".cdf") if std_src.is_dir() else []
    # Refuse before changing anything.
    if cal_dest_rel is not None and (data_dir / cal_dest_rel).exists() \
            and _sha(data_dir / cal_dest_rel) != _sha(cal_src):
        raise CopyInstrumentError(f"{data_dir / cal_dest_rel} already exists with different "
                                  f"content; use a fresh scratch folder")

    out = {"instrument": instrument_id, "source": str(src), "created": False,
           "calibration_cdf": fields["calibration_cdf"],
           "calibration_assignments": len(json.loads(fields["calibration_assignments"] or "[]")),
           "corrections": len(corr["values"]) if corr else 0, "settings_copied": False,
           "standards_copied": 0, "queued": 0}
    if cal_dest_rel is not None:
        _copy_file(cal_src, data_dir / cal_dest_rel)
    if settings_src.is_file() and not (data_dir / "settings.json").exists():
        shutil.copy2(settings_src, data_dir / "settings.json")
        out["settings_copied"] = True
    for p in standards:
        dest = data_dir / "gc_comparison_standards" / p.name
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
            out["standards_copied"] += 1
    with store.connection(db) as tconn:
        with store.write_txn(tconn):
            out["created"] = store.instruments.get(instrument_id, db=tconn) is None
            store.instruments.upsert(dict(fields, id=instrument_id), db=tconn)
            if corr:
                store.corrections.set_all(tconn, instrument_id, corr["values"], by="load_folder",
                                          reason=COPY_REASON.format(src=src))
            for status in ("awaiting_calibration", "pending_corrections"):
                out["queued"] += store.jobs.enqueue_for_status(instrument_id, status, db=tconn)
    log.info("load_folder: copied %s from %s: %s", instrument_id, src, out)
    return out


@contextlib.contextmanager
def process_lock(data_dir) -> Iterator[Path]:
    """Hold ``<data_dir>/PROCESS_LOCK`` (created exclusively, holding our pid)
    for the block; ``ProcessLocked`` if it exists. A lock left by a crash
    must be deleted by hand once no loader is running."""
    lock = Path(data_dir) / PROCESS_LOCK
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise ProcessLocked(f"{lock} exists: another load is processing (or one crashed; "
                            f"delete the file if no loader is running)") from None
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    try:
        yield lock
    finally:
        try:
            lock.unlink()
        except OSError:
            log.warning("load_folder: could not remove %s", lock)


def iter_cdfs(folder) -> list:
    """Every ``*.CDF`` (any case) under ``folder``, recursively, sorted by path.
    Symlinked folders are not followed."""
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        for name in filenames:
            if name.lower().endswith(".cdf"):
                out.append(Path(dirpath) / name)
    return sorted(out)


def _order_key(path: Path, root: Path):
    """(stored injection time, blank first, relative path). A stamp-less file's
    time is its mtime truncated to the second, as ``submit`` stores it. An
    unreadable file sorts last and is left for ``submit`` to reject."""
    rel = path.relative_to(root).as_posix()
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).replace(microsecond=0)
        sample, inj, _source, _method, _raw = distill.cdf_identity(path, mtime=mtime)
    except Exception as exc:  # noqa: BLE001 - netCDF raises many kinds
        log.info("load_folder: %s: identity unreadable (%s); submitted last", rel, exc)
        return (_FAR_FUTURE, 1, rel)
    return (inj, 0 if pipeline.is_blank_name(sample) else 1, rel)


def _late_blank_notes(instrument_id: str, db) -> dict:
    """Sample id -> its late-blank note only: a LEM warning appended to the
    same review note (v5.1.0) is not a late blank and must not count as one."""
    out = {}
    with store.connection(db) as conn:
        for sid, note in conn.execute(
                "SELECT id, review_note FROM samples WHERE instrument_id=? "
                "AND review_note IS NOT NULL", (instrument_id,)):
            m = _LATE_BLANK_RE.match(note)
            if m:
                out[sid] = m.group(0)
    return out


def _emit(progress: Optional[Progress], event: dict) -> None:
    if progress is not None:
        progress(event)


def load_folder(instrument_id: str, folder, *, backfill: bool, progress: Optional[Progress] = None,
                db: store.Db = None, data_dir=None, conf: Optional[dict] = None) -> dict:
    """Submit every CDF under ``folder`` to ``instrument_id`` (see the module docstring)."""
    started = time.monotonic()
    started_at = store.now_iso()
    if data_dir is None:
        data_dir = paths.data_dir()
        if data_dir is None:
            raise RuntimeError(f"{paths.DATA_ENV} is not set; the loader needs the hub's data folder")
    data_dir = Path(data_dir)
    db = db if db is not None else data_dir / store.DB_FILENAME
    inst = store.instruments.get(instrument_id, db=db)
    if inst is None:
        raise pipeline.UnknownInstrument(f"unknown instrument {instrument_id!r}")
    if not inst.get("enabled", 1):
        raise pipeline.InstrumentDisabled(f"instrument {instrument_id} is disabled")
    root = Path(folder)
    if not root.is_dir():
        raise NotADirectoryError(f"not a folder: {root}")
    if conf is None:
        import settings
        conf = settings.load_settings()

    files = iter_cdfs(root)
    summary = {
        "instrument": instrument_id, "folder": str(root), "backfill_forced": bool(backfill),
        "files": len(files), "created": 0, "duplicate": 0, "conflict": 0,
        "cross_instrument": 0, "rejected": 0, "truncated": 0, "failed": 0, "backfill": 0,
        "late_blank_review_notes": 0, "sample_ids": [], "rejected_files": [], "failed_files": [],
        "conflicts": [], "cross_instrument_files": [], "started_at": started_at,
    }
    notes_before = _late_blank_notes(instrument_id, db)
    try:
        _emit(progress, {"phase": "scan", "total": len(files)})
        keys = {}
        for n, p in enumerate(files, 1):
            keys[p] = _order_key(p, root)
            _emit(progress, {"phase": "identify", "done": n, "total": len(files), "file": str(p)})
        ordered = sorted(files, key=keys.__getitem__)
        for done, path in enumerate(ordered, 1):
            outcome, sample_id, message = _submit_one(instrument_id, path, summary, backfill=backfill,
                                                      conf=conf, data_dir=data_dir, db=db)
            _emit(progress, {"phase": "submit", "done": done, "total": len(ordered),
                             "file": str(path), "outcome": outcome, "sample_id": sample_id,
                             "message": message})
    except BaseException as exc:
        summary["stopped"] = f"{type(exc).__name__}: {exc}"
        _finish(summary, instrument_id, db, notes_before, started)
        log.warning("load_folder: stopped partway (%s): %s", summary["stopped"], format_summary(summary))
        exc.load_summary = summary
        raise
    _finish(summary, instrument_id, db, notes_before, started)
    log.info("load_folder: %s", format_summary(summary))
    _emit(progress, {"phase": "done", "summary": summary})
    return summary


def _submit_one(instrument_id, path: Path, summary: dict, *, backfill, conf, data_dir, db):
    sample_id, message = None, ""
    try:
        st = path.stat()
        res = pipeline.submit(instrument_id, path, mtime=st.st_mtime, source_name=path.name,
                              conf=conf, data_dir=data_dir, db=db, notifier=None,
                              force_backfill=bool(backfill))
    except pipeline.InstrumentDisabled:
        raise
    except pipeline.SubmitRejected as exc:
        message = str(exc)
        summary["rejected"] += 1
        if message.startswith("truncated"):
            summary["truncated"] += 1
        summary["rejected_files"].append({"file": str(path), "reason": message})
        log.warning("load_folder: %s rejected: %s", path, message)
        return "rejected", None, message
    except OSError as exc:
        message = f"{type(exc).__name__}: {exc}"
        summary["failed"] += 1
        summary["failed_files"].append({"file": str(path), "reason": message})
        log.warning("load_folder: %s could not be read: %s", path, exc)
        return "failed", None, message
    outcome, sample_id, message = res.outcome, res.sample_id, res.message
    summary[outcome] += 1
    if outcome in ("created", "duplicate") and res.sample_id is not None:
        summary["sample_ids"].append(res.sample_id)
    if outcome == "created":
        s = store.samples.get(res.sample_id, db=db)
        if s is not None and s["backfill"]:
            summary["backfill"] += 1
    elif outcome == "conflict":
        summary["conflicts"].append({"file": str(path), "conflict_id": res.conflict_id,
                                     "existing_sample_id": res.sample_id})
    elif outcome == "cross_instrument":
        summary["cross_instrument_files"].append({
            "file": str(path), "instrument_id": res.instrument_id, "sample_id": res.sample_id})
    return outcome, sample_id, message


def _finish(summary: dict, instrument_id: str, db, notes_before: dict, started: float) -> None:
    try:
        notes_after = _late_blank_notes(instrument_id, db)
        summary["late_blank_review_notes"] = sum(
            1 for sid, note in notes_after.items() if notes_before.get(sid) != note)
    except Exception:  # noqa: BLE001 - a stopped load still reports what it did
        log.exception("load_folder: could not count the late-blank notes")
    summary["seconds"] = round(time.monotonic() - started, 3)


def format_summary(summary: dict) -> str:
    """One line for the log and the CLI."""
    line = (f"{summary['instrument']}: {summary['files']} CDF(s) from {summary['folder']}: "
            f"{summary['created']} created, {summary['duplicate']} duplicate, "
            f"{summary['conflict']} conflict, {summary['cross_instrument']} on another instrument, "
            f"{summary['rejected']} rejected ({summary['truncated']} truncated), "
            f"{summary['failed']} unreadable; {summary['backfill']} backfill"
            f"{' (forced)' if summary['backfill_forced'] else ''}; "
            f"{summary['late_blank_review_notes']} late-blank review note(s)")
    if "seconds" in summary:
        line += f"; {summary['seconds']:.1f} s"
    if summary.get("stopped"):
        line += f"; STOPPED PARTWAY: {summary['stopped']}"
    return line
