"""hub_admin.py: the hub's admin jobs and export actions (phase 2, 2A1 T5).

A Blueprint registered by ``app.py``. Every ``/api/admin/*`` route here is a
POST with a JSON body carrying the admin ``password`` (``ingest_api._admin_body``:
415 without JSON, 403 on a wrong password, 64 KiB cap); the app's cross-site
guard covers them too.

Admin jobs (one at a time, in a background thread, polled status)::

    POST /api/admin/load-folder          {password, instrument, folder, backfill?}
         → 202 {job}; 400 bad folder; 404 unknown instrument; 409 a job is running
    POST /api/admin/jobs/status          {password} → {job} (the current or last job)
    POST /api/admin/jobs/stop            {password} → {job}; asks the running job (of
         either kind) to stop between batches, by raising from its own progress
         callback (``jobs.load_folder``/``jobs.import_history`` both re-raise with
         the summary so far attached; a re-run resumes); 409 if no job is running

``job`` is ``{id, kind, state: running|done|failed|stopped, params, started_at,
finished_at, progress: {phase, done, total, file}, counts: {outcome: n},
recent: [{file, outcome, sample_id, message}], summary, error}``.
``jobs.load_folder.load_folder`` does the work (read-only on the folder,
resumable, injection-time order); the running hub Worker processes what it
submits. ``AdminJobs.start(kind, fn, params)`` is generic: the 2D history
import registers its route the same way (``fn(progress=...) -> summary``).

The 2D history import (read-only on the v1 folder and CSV; every imported
sample is backfill and is never exported automatically, D11)::

    POST /api/admin/import-history/dry-run
         {password, instrument, processed_dir, results_csv?, aliases?, batch_size?}
         → 200 {summary}; nothing is written. 400 bad folder/CSV/aliases; 404
           unknown instrument.
    POST /api/admin/import-history/start
         {password, instrument, processed_dir, results_csv?, aliases?, batch_size?,
          confirm: true}
         → 202 {job} (kind ``import-history``, the same ``AdminJobs`` runner as
           load-folder: one job at a time, of either kind); 400 missing/false
           ``confirm`` or a bad folder/CSV/aliases; 404 unknown instrument; 409 a
           job is running
    POST /api/admin/import-history/last-run
         {password, instrument} → {last_run} (``jobs.import_history.last_run``,
         ``None`` if the instrument was never imported); the admin page uses it to
         default the form's processed folder, CSV and aliases. Submitting a
         different CSV path than last time is not refused; the run's own summary
         carries a warning (rows are matched to stored revisions by CSV path and
         line number).

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

Diagnostics (``diagnostics.py``; one bundle at a time)::

    POST /api/admin/diagnostics/estimate   {password}
         → {options: [{key, label, default, bytes, files, note}]} (in
           ``diagnostics.OPTION_KEYS`` order; cached a minute); 403 wrong password
    POST /api/admin/diagnostics/bundle     {password, options?: {key: bool}}
         → 200 {download, name, size, files, skipped}: the zip is built to a
           temp file under ``<data>/diagnostics-tmp`` (the password given is
           redacted from it too); 400 bad options; 409 another bundle is being
           built; 507 not enough free disk (``diagnostics.check_disk``)
    GET  /api/admin/diagnostics/download/<token>
         → the zip (attachment, Cache-Control: no-store), streamed once and
           deleted; the token is random, single-use and expires after 10
           minutes (404 after that). No password: the POST that made it had one.

``GET /admin/hub`` is the small admin page (templates/hub_admin.html).
"""
from __future__ import annotations

import contextlib
import itertools
import logging
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from flask import Blueprint, Response, jsonify, render_template, request

import diagnostics
import exports
import netctx
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


class JobStopped(RuntimeError):
    """Raised from an admin job's progress callback when a stop was requested
    (``AdminJobs.request_stop``); ``jobs.load_folder``/``jobs.import_history``
    re-raise it with the summary so far attached."""


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
                   "recent": [], "summary": None, "error": None, "stop_requested": False}
            self._job = job
        threading.Thread(target=self._run, args=(job, fn), daemon=True,
                         name=f"admin-{kind}").start()
        return _copy(job)

    def request_stop(self) -> Optional[dict]:
        """Ask the running job to stop (between batches, at its own next
        progress event). ``None`` if no job is running."""
        with self._lock:
            if self._job is None or self._job["state"] != "running":
                return None
            self._job["stop_requested"] = True
            return _copy(self._job)

    def _progress(self, job: dict, event: dict) -> None:
        with self._lock:
            if job.get("stop_requested"):
                raise JobStopped("stopped by admin")
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
        except JobStopped as exc:
            summary = getattr(exc, "import_summary", None) or getattr(exc, "load_summary", None)
            state, error = "stopped", None
            log.info("admin job %s (%s) stopped by request", job["id"], job["kind"])
        except BaseException as exc:  # noqa: BLE001 - report it, never kill the hub
            log.error("admin job %s (%s) failed:\n%s", job["id"], job["kind"],
                      traceback.format_exc())
            summary = getattr(exc, "import_summary", None) or getattr(exc, "load_summary", None)
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
    return netctx.client_ip() or "unknown"


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


@bp.route("/api/admin/jobs/stop", methods=["POST"])
def api_admin_job_stop():
    """Ask the running job (either kind) to stop between batches."""
    _body, err = _admin()
    if err:
        return err
    job = JOBS.request_stop()
    if job is None:
        return _err("no job is running", 409, job=JOBS.current())
    log.info("admin: stop requested for job %s (%s) by %s", job["id"], job["kind"], _who())
    return jsonify({"job": job})


# ── history import (2D) ──────────────────────────────────────────────────────

def _import_history_params(body: dict):
    """Validated ``(instrument, processed_dir, results_csv, aliases, batch_size)``,
    or ``(None, err_response)``."""
    inst = str(body.get("instrument") or "").strip()
    if not inst:
        return None, _err("instrument is required", 400)
    if store.instruments.get(inst, db=_db()) is None:
        return None, _err(f"Unknown instrument {inst!r}", 404)
    raw_dir = str(body.get("processed_dir") or "").strip()
    processed_dir = Path(raw_dir) if raw_dir else None
    if processed_dir is None or not processed_dir.is_absolute() or not processed_dir.is_dir():
        return None, _err("processed_dir must be an existing folder, as an absolute path", 400)
    raw_csv = body.get("results_csv")
    results_csv = None
    if raw_csv not in (None, ""):
        p = Path(str(raw_csv).strip())
        if not p.is_absolute() or p.suffix.lower() != ".csv" or not p.is_file():
            return None, _err("results_csv must be an existing .csv file, as an absolute path", 400)
        results_csv = p
    aliases = body.get("aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        return None, _err("aliases must be a list of strings", 400)
    aliases = [a.strip() for a in aliases if a.strip()]
    batch_size = body.get("batch_size", 500)
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        return None, _err("batch_size must be a positive integer", 400)
    return (inst, processed_dir, results_csv, aliases, batch_size), None


@bp.route("/api/admin/import-history/dry-run", methods=["POST"])
def api_admin_import_history_dry_run():
    """Classify a v1 processed folder and results CSV against the store, without
    writing anything (``jobs.import_history.import_history(..., dry_run=True)``)."""
    body, err = _admin()
    if err:
        return err
    params, err = _import_history_params(body)
    if err:
        return err
    inst, processed_dir, results_csv, aliases, batch_size = params
    from jobs.import_history import import_history
    summary = import_history(inst, processed_dir, results_csv, instrument_folder_aliases=aliases,
                             db=_db(), data_dir=paths.require_data_dir(), dry_run=True,
                             batch_size=batch_size)
    return jsonify({"summary": summary})


@bp.route("/api/admin/import-history/start", methods=["POST"])
def api_admin_import_history_start():
    """Start the real history import (``jobs.import_history.import_history``) as
    an admin job. Requires ``confirm: true``; refuses (409) while another admin
    job (of either kind) is running."""
    body, err = _admin()
    if err:
        return err
    if body.get("confirm") is not True:
        return _err("confirm must be true", 400)
    params, err = _import_history_params(body)
    if err:
        return err
    inst, processed_dir, results_csv, aliases, batch_size = params
    by = f"admin@{_who()}"      # recorded on the run and its revisions (captured here:
                                # the job runs outside the request)

    def run(progress):
        from jobs.import_history import import_history
        return import_history(inst, processed_dir, results_csv, instrument_folder_aliases=aliases,
                              db=_db(), data_dir=paths.require_data_dir(), progress=progress,
                              dry_run=False, batch_size=batch_size, by=by)

    try:
        job = JOBS.start("import-history", run, {
            "instrument": inst, "processed_dir": str(processed_dir),
            "results_csv": str(results_csv) if results_csv else None, "aliases": aliases,
            "batch_size": batch_size, "by": _who()})
    except RuntimeError as exc:
        return _err(str(exc), 409, job=JOBS.current())
    log.info("admin: import-history %s from %s (csv %s) started by %s", inst, processed_dir,
             results_csv, _who())
    return jsonify({"job": job}), 202


@bp.route("/api/admin/import-history/last-run", methods=["POST"])
def api_admin_import_history_last_run():
    """The instrument's latest real import run, to default the admin page's form."""
    body, err = _admin()
    if err:
        return err
    inst = str(body.get("instrument") or "").strip()
    if not inst:
        return _err("instrument is required", 400)
    if store.instruments.get(inst, db=_db()) is None:
        return _err(f"Unknown instrument {inst!r}", 404)
    from jobs.import_history import last_run
    return jsonify({"last_run": last_run(inst, db=_db())})


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


# ── diagnostics ─────────────────────────────────────────────────────────────

DIAG_CHUNK = 1024 * 1024


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/api/admin/diagnostics/estimate", methods=["POST"])
def api_admin_diagnostics_estimate():
    """Expected (uncompressed) size of each bundle option."""
    _body, err = _admin()
    if err:
        return err
    est = diagnostics.estimate(data_dir=paths.require_data_dir(), db=_db())
    return _no_store(jsonify({"options": [dict(v, key=k) for k, v in est.items()]}))


def _bundle_name() -> str:
    import re
    import socket
    import version
    import secrets as _secrets
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    raw = (f"gc-diagnostics-{socket.gethostname()}-{version.APP_VERSION}-{stamp}-"
           f"{_secrets.token_hex(3)}")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw)


@bp.route("/api/admin/diagnostics/bundle", methods=["POST"])
def api_admin_diagnostics_bundle():
    """Build the diagnostics zip; answer a one-time download URL. The build
    runs in this request's thread (the app keeps serving on its others) and
    never takes the store's write lock; one build at a time."""
    body, err = _admin()
    if err:
        return err
    try:
        options = diagnostics.normalize_options(body.get("options"))
    except ValueError as exc:
        return _err(str(exc), 400)
    data_dir = paths.require_data_dir()
    name = _bundle_name()
    password = body.get("password")
    try:
        with diagnostics.exclusive():
            tmp = diagnostics.cleanup_tmp(data_dir)
            try:
                diagnostics.check_disk(tmp, options, data_dir=data_dir, db=_db())
            except diagnostics.NoSpace as exc:
                return _err(str(exc), 507)
            part = tmp / f"{name}.zip.part"
            try:
                import hub
                manifest = diagnostics.build_bundle(
                    options, data_dir=data_dir, db=_db(), out_path=part,
                    who=f"admin@{_who()}", runtime=hub.running(),
                    extra_secrets=[password] if isinstance(password, str) else [])
                final = tmp / f"{name}.zip"
                part.replace(final)
            except Exception as exc:  # noqa: BLE001 - report it, never kill the hub
                log.error("diagnostics bundle failed:\n%s", traceback.format_exc())
                for leftover in (part, tmp / f"{name}.zip"):
                    _unlink_quietly(leftover)
                return _err(f"The diagnostics bundle failed: {type(exc).__name__}: {exc}", 500)
            token = diagnostics.register_download(final, f"{name}.zip")
    except diagnostics.Busy as exc:
        return _err(str(exc), 409)
    size = final.stat().st_size
    log.warning("admin: diagnostics bundle %s (%d bytes, %d files, options %s) built for %s",
                name, size, len(manifest.get("files", [])), options, _who())
    return _no_store(jsonify({
        "download": f"/api/admin/diagnostics/download/{token}", "name": f"{name}.zip",
        "size": size, "files": len(manifest.get("files", [])),
        "skipped": len(manifest.get("skipped", []))}))


@bp.route("/api/admin/diagnostics/download/<token>", methods=["GET"])
def api_admin_diagnostics_download(token):
    """Stream a built bundle once, then delete it."""
    d = diagnostics.claim_download(token)
    if d is None:
        return _err("Unknown or expired download; build the bundle again", 404)
    path = Path(d["path"])
    try:
        fh = open(path, "rb")
        size = path.stat().st_size
    except OSError:
        _unlink_quietly(path)
        return _err("The bundle is gone; build it again", 404)
    log.warning("admin: diagnostics bundle %s downloaded by %s", d["name"], _who())

    def stream():
        with diagnostics.streaming():
            try:
                for chunk in iter(lambda: fh.read(DIAG_CHUNK), b""):
                    yield chunk
            finally:
                fh.close()
                _unlink_quietly(path)

    resp = Response(stream(), mimetype="application/zip", direct_passthrough=True, headers={
        "Content-Disposition": f'attachment; filename="{d["name"]}"',
        "Content-Length": str(size)})
    resp.call_on_close(lambda: (fh.close(), _unlink_quietly(path)))
    return _no_store(resp)


def _unlink_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


# ── the page ────────────────────────────────────────────────────────────────

@bp.route("/admin/hub", methods=["GET"])
def admin_hub_page():
    import version
    diag_options = [{"key": k, "label": diagnostics.LABELS[k], "default": diagnostics.OPTIONS[k]}
                    for k in diagnostics.OPTION_KEYS]
    return render_template("hub_admin.html", app_version=version.APP_VERSION,
                           diag_options=diag_options)
