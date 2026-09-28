"""Load CDFs from a folder (phase 2, 2A1 T6): the admin job **Load CDFs from a
folder**, one-shot and read-only on the source (spec, Delivery 2A1).

``load_folder`` finds every ``*.CDF`` under ``folder`` (any case, any depth),
reads each file's identity with ``distill.cdf_identity`` and submits them to
``pipeline.submit`` **in injection-time order** (blanks first at a tie, then
by path). The hub's worker may be processing while the load runs, so a
history blank must arrive before the samples injected after it; otherwise
those samples would be final before their blank arrived and be flagged with
a late-blank review note.

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

::

    load_folder(instrument_id, folder, *, backfill, progress=None, db=None,
                data_dir=None, conf=None) -> dict

``progress(event)`` receives dicts, for the admin page's SSE stream:
``{"phase": "scan", "total"}`` once, ``{"phase": "submit", "done", "total",
"file", "outcome", "sample_id", "message"}`` after each file (``outcome`` is
a ``SubmitResult.outcome``, ``"rejected"`` or ``"failed"``) and
``{"phase": "done", "summary"}`` at the end. An exception raised by the
callback stops the load.

The summary::

    {"instrument", "folder", "backfill_forced", "files", "created", "duplicate",
     "conflict", "cross_instrument", "rejected", "truncated", "failed",
     "backfill", "late_blank_review_notes", "rejected_files": [{file, reason}],
     "failed_files": [{file, reason}], "conflicts": [{file, conflict_id,
     existing_sample_id}], "cross_instrument_files": [{file, instrument_id,
     sample_id}], "started_at", "seconds"}

``rejected`` counts files ``submit`` refused (``SubmitRejected``: unreadable,
no injection time, truncated, no intensity data); ``truncated`` is the part
of those ``pipeline.cdf_problem`` found cut short. ``failed`` counts files
that could not be read at all (``OSError``). ``backfill`` counts the created
samples that are backfill.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import distill
import paths
import pipeline
import store

log = logging.getLogger("jobs.load_folder")

Progress = Callable[[dict], object]
_LATE_BLANK_PREFIX = pipeline.LATE_BLANK_NOTE.split("{", 1)[0]
_FAR_FUTURE = datetime.max


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
    """(injection time, blank first, relative path); an unreadable file sorts
    last and is left for ``submit`` to reject."""
    rel = path.relative_to(root).as_posix()
    try:
        sample, inj, _source, _method, _raw = distill.cdf_identity(path)
    except Exception as exc:  # noqa: BLE001 - netCDF raises many kinds
        log.info("load_folder: %s: identity unreadable (%s); submitted last", rel, exc)
        return (_FAR_FUTURE, 1, rel)
    return (inj, 0 if pipeline.is_blank_name(sample) else 1, rel)


def _late_blank_notes(instrument_id: str, db) -> dict:
    with store.connection(db) as conn:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT id, review_note FROM samples WHERE instrument_id=? AND review_note IS NOT NULL",
            (instrument_id,)) if r[1].startswith(_LATE_BLANK_PREFIX)}


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
    _emit(progress, {"phase": "scan", "total": len(files)})
    ordered = sorted(files, key=lambda p: _order_key(p, root))
    notes_before = _late_blank_notes(instrument_id, db)

    summary = {
        "instrument": instrument_id, "folder": str(root), "backfill_forced": bool(backfill),
        "files": len(ordered), "created": 0, "duplicate": 0, "conflict": 0,
        "cross_instrument": 0, "rejected": 0, "truncated": 0, "failed": 0, "backfill": 0,
        "late_blank_review_notes": 0, "rejected_files": [], "failed_files": [],
        "conflicts": [], "cross_instrument_files": [], "started_at": started_at,
    }
    for done, path in enumerate(ordered, 1):
        sample_id, message = None, ""
        try:
            st = path.stat()
            res = pipeline.submit(instrument_id, path, mtime=st.st_mtime, source_name=path.name,
                                  conf=conf, data_dir=data_dir, db=db, notifier=None,
                                  force_backfill=bool(backfill))
        except pipeline.InstrumentDisabled:
            raise
        except pipeline.SubmitRejected as exc:
            outcome, message = "rejected", str(exc)
            summary["rejected"] += 1
            if message.startswith("truncated"):
                summary["truncated"] += 1
            summary["rejected_files"].append({"file": str(path), "reason": message})
            log.warning("load_folder: %s rejected: %s", path, message)
        except OSError as exc:
            outcome, message = "failed", f"{type(exc).__name__}: {exc}"
            summary["failed"] += 1
            summary["failed_files"].append({"file": str(path), "reason": message})
            log.warning("load_folder: %s could not be read: %s", path, exc)
        else:
            outcome, sample_id, message = res.outcome, res.sample_id, res.message
            summary[outcome] += 1
            if outcome == "created":
                s = store.samples.get(res.sample_id, db=db)
                if s is not None and s["backfill"]:
                    summary["backfill"] += 1
            elif outcome == "conflict":
                summary["conflicts"].append({"file": str(path), "conflict_id": res.conflict_id,
                                             "existing_sample_id": res.sample_id})
            elif outcome == "cross_instrument":
                summary["cross_instrument_files"].append({
                    "file": str(path), "instrument_id": res.instrument_id,
                    "sample_id": res.sample_id})
        _emit(progress, {"phase": "submit", "done": done, "total": len(ordered), "file": str(path),
                         "outcome": outcome, "sample_id": sample_id, "message": message})

    notes_after = _late_blank_notes(instrument_id, db)
    summary["late_blank_review_notes"] = sum(
        1 for sid, note in notes_after.items() if notes_before.get(sid) != note)
    summary["seconds"] = round(time.monotonic() - started, 3)
    log.info("load_folder: %s", format_summary(summary))
    _emit(progress, {"phase": "done", "summary": summary})
    return summary


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
    return line
