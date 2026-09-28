"""hub_admin.py: the hub's admin jobs and export actions (phase 2, 2A1 T5).

A Blueprint registered by ``app.py``. Every ``/api/admin/*`` route here is a
POST with a JSON body carrying the admin ``password`` (``ingest_api._admin_body``:
415 without JSON, 403 on a wrong password, 64 KiB cap); the app's cross-site
guard covers them too.

Admin jobs (one at a time, in a background thread, polled status)::

    POST /api/admin/load-folder          {password, instrument, folder, backfill?}
         → 202 {job}; 400 bad folder; 404 unknown instrument; 409 a job is running
    POST /api/admin/jobs/status          {password} → {job} (the current or last job)

``job`` is ``{id, kind, state: running|done|failed, params, started_at,
finished_at, progress: {phase, done, total, file}, counts: {outcome: n},
recent: [{file, outcome, sample_id, message}], summary, error}``.
``jobs.load_folder.load_folder`` does the work (read-only on the folder,
resumable, injection-time order); the running hub Worker processes what it
submits. ``AdminJobs.start(kind, fn, params)`` is generic: the 2D history
import registers its route the same way (``fn(progress=...) -> summary``).

Exports (need the running ``exports.HubExporter``: ``set_exporter`` is
called by ``hub.start``; 503 until then)::

    POST /api/admin/exports                         {password} → {instruments: [status]}
    POST /api/admin/exports/<id>/adopt              {password} → {status, sidecar}
    POST /api/admin/exports/<id>/new-path           {password, path} → {status}
    POST /api/admin/exports/<id>/write-fresh        {password, path} → {status, rows}

``status`` is ``HubExporter.status``: ``{instrument, path, pending,
pending_since, refused, refused_detail, last_error}``. A refusal
(``exports.ExportRefused``) is a 409 ``{error, reason}``; an unknown
instrument 404. ``path`` must be an absolute ``.csv`` path in an existing
folder.

``GET /admin/hub`` is the small admin page (templates/hub_admin.html).
"""
from __future__ import annotations

import itertools
import logging
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from flask import Blueprint, jsonify, render_template, request

import exports
import paths
import store

log = logging.getLogger("hub_admin")

bp = Blueprint("hub_admin", __name__)

RECENT_FILES = 20

_exporter_lock = threading.Lock()
_exporter: Optional[exports.HubExporter] = None


def set_exporter(exporter: Optional[exports.HubExporter]) -> None:
    """The running exporter (``hub.start``), or None when it stops."""
    global _exporter
    with _exporter_lock:
        _exporter = exporter


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AdminJobs:
    """One admin job at a time, run on a daemon thread, its progress kept in
    memory for polling."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._job: Optional[dict] = None

    def current(self) -> Optional[dict]:
        with self._lock:
            return _copy(self._job)

    def start(self, kind: str, fn: Callable[..., dict], params: dict) -> dict:
        """Run ``fn(progress=callback)`` in the background. ``RuntimeError``
        if a job is running."""
        with self._lock:
            if self._job is not None and self._job["state"] == "running":
                raise RuntimeError(f"a {self._job['kind']} job is already running")
            job = {"id": next(self._ids), "kind": kind, "state": "running", "params": params,
                   "started_at": _now(), "finished_at": None, "progress": {}, "counts": {},
                   "recent": [], "summary": None, "error": None}
            self._job = job
        threading.Thread(target=self._run, args=(job, fn), daemon=True,
                         name=f"admin-{kind}").start()
        return _copy(job)

    def _progress(self, job: dict, event: dict) -> None:
        with self._lock:
            phase = event.get("phase")
            job["progress"] = {k: event.get(k) for k in ("phase", "done", "total", "file")
                               if k in event}
            if phase == "submit":
                outcome = str(event.get("outcome") or "unknown")
                job["counts"][outcome] = job["counts"].get(outcome, 0) + 1
                job["recent"] = (job["recent"] + [{k: event.get(k) for k in
                                                   ("file", "outcome", "sample_id", "message")}
                                                  ])[-RECENT_FILES:]

    def _run(self, job: dict, fn: Callable[..., dict]) -> None:
        try:
            summary = fn(progress=lambda e: self._progress(job, e))
            state, error = "done", None
        except BaseException as exc:  # noqa: BLE001 - report it, never kill the hub
            log.error("admin job %s (%s) failed:\n%s", job["id"], job["kind"],
                      traceback.format_exc())
            summary = getattr(exc, "load_summary", None)
            state, error = "failed", f"{type(exc).__name__}: {exc}"
        with self._lock:
            job.update(state=state, error=error, summary=summary, finished_at=_now())


def _copy(job: Optional[dict]) -> Optional[dict]:
    if job is None:
        return None
    out = dict(job)
    out["progress"] = dict(job["progress"])
    out["counts"] = dict(job["counts"])
    out["recent"] = [dict(r) for r in job["recent"]]
    return out


JOBS = AdminJobs()


def _err(message: str, status: int, **extra):
    return jsonify(dict(extra, error=message)), status


def _admin():
    import ingest_api
    return ingest_api._admin_body()


def _db() -> Path:
    return paths.require_data_dir() / store.DB_FILENAME


def _who() -> str:
    return request.remote_addr or "unknown"


# ── admin jobs ──────────────────────────────────────────────────────────────

@bp.route("/api/admin/load-folder", methods=["POST"])
def api_admin_load_folder():
    """Start **Load CDFs from a folder** (``jobs.load_folder``) for one instrument."""
    body, err = _admin()
    if err:
        return err
    inst = str(body.get("instrument") or "").strip()
    raw = str(body.get("folder") or "").strip()
    backfill = body.get("backfill", False)
    if not isinstance(backfill, bool):
        return _err("backfill must be true or false", 400)
    if not inst:
        return _err("instrument is required", 400)
    if store.instruments.get(inst, db=_db()) is None:
        return _err(f"Unknown instrument {inst!r}", 404)
    folder = Path(raw) if raw else None
    if folder is None or not folder.is_absolute() or not folder.is_dir():
        return _err("folder must be an existing folder, as an absolute path", 400)

    def run(progress):
        from jobs.load_folder import load_folder
        return load_folder(inst, folder, backfill=backfill, progress=progress,
                           db=_db(), data_dir=paths.require_data_dir())

    try:
        job = JOBS.start("load-folder", run, {"instrument": inst, "folder": str(folder),
                                              "backfill": backfill, "by": _who()})
    except RuntimeError as exc:
        return _err(str(exc), 409, job=JOBS.current())
    log.info("admin: load-folder %s from %s (backfill %s) started by %s", inst, folder,
             backfill, _who())
    return jsonify({"job": job}), 202


@bp.route("/api/admin/jobs/status", methods=["POST"])
def api_admin_job_status():
    _body, err = _admin()
    if err:
        return err
    return jsonify({"job": JOBS.current()})


# ── exports ─────────────────────────────────────────────────────────────────

def _exporter_or_503():
    with _exporter_lock:
        exp = _exporter
    if exp is None:
        return None, _err("The hub's exporter is not running", 503)
    return exp, None


def _known(instrument_id: str):
    if store.instruments.get(instrument_id, db=_db()) is None:
        return _err(f"Unknown instrument {instrument_id!r}", 404)
    return None


def _csv_path(body: dict):
    raw = str(body.get("path") or "").strip()
    p = Path(raw) if raw else None
    if (p is None or not p.is_absolute() or p.suffix.lower() != ".csv"
            or not p.parent.is_dir()):
        return None, _err("path must be an absolute .csv path in an existing folder", 400)
    return p, None


def _refused(exc: exports.ExportRefused):
    return _err(str(exc), 409, reason=exc.reason)


@bp.route("/api/admin/exports", methods=["POST"])
def api_admin_exports():
    """Every instrument's export status (path, pending rows, refusal)."""
    _body, err = _admin()
    if err:
        return err
    exp, err = _exporter_or_503()
    if err:
        return err
    out = []
    for inst in store.instruments.list(db=_db()):
        try:
            out.append(exp.status(inst["id"]))
        except Exception as exc:  # noqa: BLE001 - one bad row must not hide the rest
            out.append({"instrument": inst["id"], "error": str(exc)})
    return jsonify({"instruments": out})


@bp.route("/api/admin/exports/<instrument_id>/adopt", methods=["POST"])
def api_admin_exports_adopt(instrument_id):
    """Adopt the export file as it is now (pending rows go after it)."""
    _body, err = _admin()
    if err:
        return err
    exp, err = _exporter_or_503()
    if err or (err := _known(instrument_id)):
        return err
    try:
        side = exp.adopt(instrument_id, by=_who())
    except exports.ExportRefused as exc:
        return _refused(exc)
    exp.wake()
    return jsonify({"status": exp.status(instrument_id), "sidecar": side})


@bp.route("/api/admin/exports/<instrument_id>/new-path", methods=["POST"])
def api_admin_exports_new_path(instrument_id):
    """Point the instrument's export at another file (an existing file there
    must be adopted before the hub appends to it)."""
    body, err = _admin()
    if err:
        return err
    exp, err = _exporter_or_503()
    if err or (err := _known(instrument_id)):
        return err
    path, err = _csv_path(body)
    if err:
        return err
    try:
        exp.new_path(instrument_id, path)
    except exports.ExportRefused as exc:
        return _refused(exc)
    log.info("admin: %s export path set to %s by %s", instrument_id, path, _who())
    exp.wake()
    return jsonify({"status": exp.status(instrument_id)})


@bp.route("/api/admin/exports/<instrument_id>/write-fresh", methods=["POST"])
def api_admin_exports_write_fresh(instrument_id):
    """Write every gated current revision to a NEW file and switch to it.
    Never point LEM at it without setting its tail offset to the end."""
    body, err = _admin()
    if err:
        return err
    exp, err = _exporter_or_503()
    if err or (err := _known(instrument_id)):
        return err
    path, err = _csv_path(body)
    if err:
        return err
    try:
        rows = exp.write_fresh(instrument_id, path, by=_who())
    except exports.ExportRefused as exc:
        return _refused(exc)
    exp.wake()
    return jsonify({"status": exp.status(instrument_id), "rows": rows})


# ── the page ────────────────────────────────────────────────────────────────

@bp.route("/admin/hub", methods=["GET"])
def admin_hub_page():
    import version
    return render_template("hub_admin.html", app_version=version.APP_VERSION)
