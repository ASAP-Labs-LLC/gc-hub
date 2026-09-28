#!/usr/bin/env python3
"""
app.py -- Flask backend of the GC hub (phase 2: hub mode only).

It exposes the JSON REST API consumed by the single-page frontend and
delegates the heavy lifting to the backend modules (store, pipeline,
exports, distill, settings, qbench_pdf_uploader). Every piece of state lives
in GC_DATA_DIR; without it the app refuses to start (DEPLOY.md). hub.start()
(called from _init_app) owns the background work.
"""
from __future__ import annotations

import sys as _sys
# Force unbuffered stdout/stderr so all print() statements appear
# immediately in the terminal (Flask dev server buffers by default)
if hasattr(_sys.stdout, 'reconfigure'):
    _sys.stdout.reconfigure(line_buffering=True)
    _sys.stderr.reconfigure(line_buffering=True)

import base64
import csv
import hmac
import io
import json
import logging
import os
import platform
import queue
import re
import shutil
import subprocess
import tempfile
import zipfile
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import numpy as np
from flask import (
    Flask,
    Response,
    jsonify,
    request,
    send_file,
    stream_with_context,
)

# ---------------------------------------------------------------------------
# Backend modules (already in the webapp/ folder)
# ---------------------------------------------------------------------------
# ── Port ─────────────────────────────────────────────────────────────────
# Resolved before the rest is imported and published back to the
# environment, so every module and every subprocess we spawn agrees on it.
import instance
import paths
import restart_policy
import restart_update
import supervisor
import version

# ── Command line ──────────────────────────────────────────────────────────
# The ASAPSV1 updater launches ``app.py --no-tray`` (its health_args); --dev
# turns on the Flask debugger for local work only. --port is still resolved
# by instance.resolve_port() below; it is declared here so it isn't "unknown".
# Only a direct launch (``__main__``) parses the real argv: when app is
# imported, sys.argv belongs to someone else.
import argparse
import atexit

_ap = argparse.ArgumentParser(prog="app.py", description="GC Hub web app",
                              allow_abbrev=False)  # --d must not mean --dev
_ap.add_argument("--port", type=int, help="port (PORT env wins when deployed)")
_ap.add_argument("--dev", action="store_true",
                 help="Flask debug mode with the interactive debugger (never in production)")
_ap.add_argument("--no-tray", action="store_true",
                 help="accepted for updater compatibility; there is no tray")
ARGS, _unknown_args = _ap.parse_known_args(_sys.argv[1:] if __name__ == "__main__" else [])
_bad_flags = [a for a in _unknown_args if a.startswith("-")]
if _bad_flags:
    # A typo in the updater config must fail the health check loudly,
    # not boot with the flag silently ignored.
    _ap.error(f"unrecognized arguments: {' '.join(_bad_flags)}")

# ── Hub mode only (spec D14): no GC_DATA_DIR, no start ─────────────────────
# Checked before anything reads settings or writes a file: every path the
# app uses lives under that folder. (The share copies run v1.x.)
if paths.data_dir() is None:
    if __name__ == "__main__":
        print(f"ERROR: {paths.MISSING_TEXT}", file=_sys.stderr)
        _sys.exit(2)
    raise paths.DataDirMissing()

GC_PORT = instance.resolve_port()
os.environ["GC_PORT"] = str(GC_PORT)

import distill
import sample_flags
import settings as settings_mod

try:
    import fuel_fit
except Exception:  # pragma: no cover - scipy.optimize missing
    fuel_fit = None
import notifications as notifications_mod
import reprocess_query
import hub
import instruments
import pipeline
import store

try:
    import qbench_pdf_uploader
except ImportError:
    qbench_pdf_uploader = None  # type: ignore[assignment]
import qbench_secrets
try:  # needs jwt + requests; importing it no longer needs credentials
    import qbench_client
    import requests.exceptions as requests_exc
except ImportError:
    qbench_client = None  # type: ignore[assignment]
    requests_exc = None  # type: ignore[assignment]

# Optional -- used for analysis trend-line smoothing
try:
    from scipy.ndimage import gaussian_filter1d
except ImportError:
    gaussian_filter1d = None  # type: ignore[assignment]

# Optional -- PDF / chart generation
try:
    import plotly.graph_objects as go
    import plotly.io as pio
except ImportError:
    go = None  # type: ignore[assignment]
    pio = None  # type: ignore[assignment]

# Optional -- HTML-to-PDF rendering (replaces QPrinter from desktop app)
try:
    from xhtml2pdf import pisa as xhtml2pdf_pisa
except ImportError:
    xhtml2pdf_pisa = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger("webapp")

# Also log to GC_DATA_DIR/app.log, rotating, so the updater-supervised
# process (which has no console anyone watches) leaves a trail.
import logging.handlers  # noqa: E402

_LOG_FILE = paths.log_file()
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_fh = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(_fh)

# The health check runs against an empty data dir: create the report export
# folder up front so nothing downstream trips over its absence.
try:
    paths.default_export_dir().mkdir(parents=True, exist_ok=True)
except OSError:
    LOGGER.exception("Could not create %s", paths.default_export_dir())

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB upload limit
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # disable static file caching in dev

import admin_auth  # noqa: E402  (2B1: admin password + /admin/setup)
import ingest_api  # noqa: E402  (2B1: the agent API, contract §1)
app.register_blueprint(admin_auth.bp)
app.register_blueprint(ingest_api.bp)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_hub_runtime: Optional["hub.HubRuntime"] = None   # set by _init_app (hub.start)

# SSE queues -- one per connected client
_upload_subscribers: list[queue.Queue] = []
_upload_sub_lock = threading.Lock()

# Background-task handles
_upload_thread: Optional[threading.Thread] = None
_upload_stop = threading.Event()

# Shared upload queue — items can be appended while the thread is running.
_upload_items: list[dict] = []           # full queue (append-only while alive)
_upload_items_lock = threading.Lock()
_upload_item_status: list[dict] = []     # per-item last-known status (mirrors indices)
_upload_skipped: set[int] = set()        # indices the user asked to skip
_upload_credentials: dict = {}           # {username, password, client_id, client_secret}
_upload_new_items = threading.Event()    # pulsed when new items are appended

# Credential re-prompt mechanism: when login fails twice, the upload thread
# pauses and waits for the user to supply new credentials via the frontend.
_creds_needed = threading.Event()   # set by upload thread when it needs creds
_creds_ready  = threading.Event()   # set by API when user submits new creds
_creds_lock   = threading.Lock()
_creds_new: dict = {}               # {"username": ..., "password": ...}

# ── Activity tracking & auto-restart ─────────────────────────────────
_last_activity: float = time.time()
_last_activity_lock = threading.Lock()
_recent_clients: dict[str, float] = {}   # remote_addr -> last-seen time, for /healthz active_sessions
_server_start_time: float = time.time()
_auto_restart_done_today: str = ""          # date string e.g. "2026-05-07"
AUTO_RESTART_HOUR = restart_policy.AUTO_RESTART_HOUR   # 3 AM local time
AUTO_RESTART_IDLE_SECONDS = 600  # 10 minutes with no requests


def _monotonic() -> float:
    """Indirection seam so tests can freeze time."""
    return time.monotonic()


# ── Flags and best-fit: the store's sample_cache ─────────────────────
# One row per sample (flags keyed on the rules fingerprint, best-fit on the
# best-fit config + standards fingerprint). The file list only reads it; stale
# or missing rows are filled by one background thread that reads the CDFs off
# the request path (_schedule_cache_refresh), so /api/files never reads a CDF.

def _bestfit_config(conf: dict) -> dict:
    """The classify() kwargs from settings (shared by route + refresher)."""
    return {
        "threshold": float(conf.get("bestfit_threshold", 0.93)),
        "shift_tolerance_min": float(conf.get("bestfit_shift_tolerance_min", 0.05)),
        "mix_min_frac": float(conf.get("bestfit_mix_min_frac", 0.10)),
        "x_max_min": float(conf.get("analysis_x_max_min", 7.0)),
    }


def _standards_dir(conf: dict) -> Path:
    return Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))


def _classify_cdf(cdf_path: str, conf: dict) -> dict | None:
    """Full best-fit classification of one CDF (None when unavailable)."""
    if fuel_fit is None:
        return None
    standards = fuel_fit.load_standards(_standards_dir(conf), distill.gc_xy_from_cdf)
    if not standards:
        return None
    p = Path(cdf_path)
    if not p.is_file():
        return None
    try:
        t, y = distill.gc_xy_from_cdf(p)
        return fuel_fit.classify(t, y, standards, **_bestfit_config(conf))
    except Exception as exc:
        LOGGER.debug("Best-fit failed for %s: %s", cdf_path, exc)
        return None


def _cache_fingerprints(conf: dict) -> dict:
    """``{rules, rules_fp, bestfit_fp}`` for sample_cache. The best-fit
    fingerprint covers the config and the standards folder's file names and
    mtimes (listed, never read); ``None`` when best-fit is off."""
    rules = sample_flags.load_rules(conf)
    bestfit_fp = None
    if fuel_fit is not None and str(conf.get("bestfit_enabled", "true")).lower() == "true":
        stds = []
        try:
            for entry in os.scandir(_standards_dir(conf)):
                if entry.is_file() and entry.name.lower().endswith(".cdf"):
                    stds.append([entry.name, entry.stat().st_mtime_ns])
        except OSError:
            pass
        if stds:
            bestfit_fp = sample_flags.rules_fingerprint(
                [_bestfit_config(conf), {"standards": sorted(stds)}])
    return {"rules": rules, "rules_fp": sample_flags.rules_fingerprint(rules),
            "bestfit_fp": bestfit_fp}


_cache_refresh_lock = threading.Lock()
_cache_refresh_pending: set[int] = set()
_cache_refresh_running = False
# sample id -> monotonic time its CDF last failed to read: not retried for
# CACHE_FAILURE_TTL seconds, so an unreadable file isn't re-read on every
# list request.
_cache_failures: dict[int, float] = {}
CACHE_FAILURE_TTL = 300.0
CACHE_REFRESH_PAUSE = 0.2      # seconds between batches: leave the store and disk to requests


def _schedule_cache_refresh(sample_ids) -> None:
    """Queue samples for the background sample_cache refresher (single flight).
    Samples whose CDF failed to read in the last ``CACHE_FAILURE_TTL`` seconds
    are skipped."""
    global _cache_refresh_running
    now = _monotonic()
    with _cache_refresh_lock:
        for sid in sample_ids:
            failed = _cache_failures.get(int(sid))
            if failed is not None and now - failed < CACHE_FAILURE_TTL:
                continue
            _cache_failures.pop(int(sid), None)
            _cache_refresh_pending.add(int(sid))
        if _cache_refresh_running or not _cache_refresh_pending:
            return
        _cache_refresh_running = True
    threading.Thread(target=_refresh_sample_cache, daemon=True, name="sample-cache").start()


def _refresh_sample_cache() -> None:
    """Fill sample_cache for the queued samples, 50 at a time, until none are left."""
    global _cache_refresh_running
    try:
        while True:
            with _cache_refresh_lock:
                batch = sorted(_cache_refresh_pending)[:50]
                _cache_refresh_pending.difference_update(batch)
                if not batch:
                    _cache_refresh_running = False
                    return
            try:
                data, db = _hub()
                conf = settings_mod.load_settings()
                fps = _cache_fingerprints(conf)
                standards = None
                if fps["bestfit_fp"] is not None:
                    standards = fuel_fit.load_standards(_standards_dir(conf), distill.gc_xy_from_cdf)
                for sid in batch:
                    _refresh_one(sid, data, db, conf, fps, standards)
            except Exception:
                LOGGER.exception("sample_cache refresh failed")
            time.sleep(CACHE_REFRESH_PAUSE)
    except BaseException:
        with _cache_refresh_lock:
            _cache_refresh_running = False
        raise


def _refresh_one(sid: int, data: Path, db: Path, conf: dict, fps: dict, standards) -> None:
    """Evaluate the flag rules (and best-fit) on one sample's current CDF and
    store them with their fingerprints."""
    s = store.samples.get(sid, db=db)
    if s is None:
        return
    flags: list = []
    best = None
    rel = s["cdf_path"]
    if rel:     # a result-only sample (no CDF) is cached as "no flags"
        try:
            t, y = distill.gc_xy_from_cdf(data / rel)
            flags = sample_flags.evaluate_rules(t, y, fps["rules"])
            if standards:
                best = fuel_fit.classify(t, y, standards, **_bestfit_config(conf))
        except Exception as exc:
            # Unreadable now (a share blip, a file being replaced): cache
            # nothing, and don't try again for CACHE_FAILURE_TTL seconds.
            LOGGER.warning("sample_cache: sample %s: %s", sid, exc)
            with _cache_refresh_lock:
                _cache_failures[sid] = _monotonic()
            return
    fields = {"rules_fingerprint": fps["rules_fp"], "flags": json.dumps(flags)}
    if fps["bestfit_fp"] is not None:
        fields.update(bestfit_fingerprint=fps["bestfit_fp"],
                      best_fit=best["label"] if best else None,
                      fit_score=float(best["score"]) if best else None)
    store.sample_cache.put(sid, db=db, **fields)


# ===================================================================== #
#  Helpers
# ===================================================================== #

def _publish(subscribers: list[queue.Queue], lock: threading.Lock, msg: str) -> None:
    """Push *msg* to every SSE subscriber queue."""
    with lock:
        dead: list[queue.Queue] = []
        for q in subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            subscribers.remove(q)


def _sse_stream(
    subscribers: list[queue.Queue],
    lock: threading.Lock,
    timeout: float = 0.5,
) -> Generator[str, None, None]:
    """Yield SSE-formatted lines from a per-client queue."""
    q: queue.Queue = queue.Queue(maxsize=500)
    with lock:
        subscribers.append(q)
    try:
        while True:
            try:
                msg = q.get(timeout=timeout)
                yield f"data: {msg}\n\n"
            except queue.Empty:
                # keep-alive comment so the connection isn't dropped
                yield ": keepalive\n\n"
    except GeneratorExit:
        pass
    finally:
        with lock:
            if q in subscribers:
                subscribers.remove(q)


def _publish_json(
    subscribers: list[queue.Queue], lock: threading.Lock, data: dict
) -> None:
    """Push a JSON-encoded message to all SSE subscribers."""
    _publish(subscribers, lock, json.dumps(data))


def _upload_log(msg: str) -> None:
    """Emit an upload progress message to all SSE subscribers."""
    LOGGER.info("[upload] %s", msg)
    _publish(_upload_subscribers, _upload_sub_lock, msg)


def _safe_path(p: str) -> Path:
    """Return a Path, handling UNC paths with spaces."""
    return Path(p)


_STANDARD_NAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _standard_name_problem(name) -> Optional[str]:
    """Why ``name`` can't be a comparison-standard file name (it becomes
    ``<standards dir>/<name>.CDF``), or None. No path separators, no
    Windows-reserved characters, not ``.``/``..`` or hidden."""
    if not isinstance(name, str) or not name.strip():
        return "A standard name is required"
    if _STANDARD_NAME_BAD.search(name) or name.strip().startswith("."):
        return f"Invalid standard name {name!r}: no slashes, dots first or characters like :*?\"<>|"
    return None


_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def _safe_filename(text) -> str:
    """``text`` as one safe file-name component (lab IDs in export names)."""
    out = _FILENAME_UNSAFE.sub("_", str(text or "")).replace("..", "_").strip(" .")
    return out or "sample"


def _error(msg: str, status: int = 400) -> tuple:
    return jsonify({"error": msg}), status


# ===================================================================== #
#  The hub store: samples are addressed by sample_id (phase 2, 2A1 T4)
# ===================================================================== #
# Routes read the store in GC_DATA_DIR/gc.db. They never create or migrate
# it: instruments.startup() does, once, at start-up (T5 wires it into
# _init_app). Until it exists every store route answers 503.

class HubUnavailable(RuntimeError):
    """The hub store (or the gc1 instrument row) doesn't exist yet → 503."""


class SampleNotFound(LookupError):
    """No such sample (or it has no stored CDF where one is needed) → 404."""


@app.errorhandler(HubUnavailable)
def _hub_unavailable(exc):
    return _error(str(exc), 503)


@app.errorhandler(SampleNotFound)
def _sample_not_found(exc):
    return _error(str(exc), 404)


@app.errorhandler(404)
def _not_found(exc):
    """JSON 404 for the API (a removed route answers ``{"error": "Not found"}``)."""
    if request.path.startswith("/api/"):
        return _error("Not found", 404)
    return exc


def _hub() -> tuple[Path, Path]:
    """``(data_dir, db_path)`` of the hub store; ``HubUnavailable`` if absent."""
    data = paths.data_dir()
    if data is None:
        raise HubUnavailable("The hub store needs GC_DATA_DIR (hub mode)")
    db = Path(data) / store.DB_FILENAME
    if not db.is_file():
        raise HubUnavailable("The hub store has not been created yet")
    return Path(data), db


class BadSampleId(ValueError):
    """A sample_id that isn't an integer → 400."""


@app.errorhandler(BadSampleId)
def _bad_sample_id(exc):
    return _error(str(exc), 400)


MAX_SAMPLE_ID = 2 ** 63 - 1     # SQLite INTEGER; larger ids can't exist (and overflow the driver)


@app.errorhandler(OverflowError)
def _overflow(exc):
    """A number too large for SQLite reached a query: it can't name a row."""
    return _error("Not found", 404)


def _valid_id(value) -> Optional[int]:
    """``value`` as a sample id (1..2^63-1), or None."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        i = int(value)
    except (TypeError, ValueError):
        return None
    return i if 1 <= i <= MAX_SAMPLE_ID else None


def _sample_or_404(sample_id, db, *, from_path: bool = False) -> dict:
    """The ``samples`` row. An id that isn't an integer in 1..2^63-1 is a 400
    (``BadSampleId``) from a request body, a 404 from a URL path (which can
    only name existing samples)."""
    sid = _valid_id(sample_id)
    if sid is None:
        if from_path:
            raise SampleNotFound(f"Sample {sample_id} not found")
        raise BadSampleId(f"sample_id must be an integer from 1 to {MAX_SAMPLE_ID}, "
                          f"not {sample_id!r}")
    s = store.samples.get(sid, db=db)
    if s is None:
        raise SampleNotFound(f"Sample {sample_id} not found")
    return s


def _sample_ids(raw) -> list[int]:
    """A request's ``sample_ids`` as ints, in order, without repeats.
    ``ValueError`` if it isn't a list of integers in 1..2^63-1."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("sample_ids must be a list of integers")
    out: list[int] = []
    for v in raw:
        i = _valid_id(v)
        if i is None:
            raise ValueError(f"sample_ids must be integers from 1 to {MAX_SAMPLE_ID}, not {v!r}")
        if i not in out:
            out.append(i)
    return out


def _json_col(value, default=None):
    """A store JSON column decoded (``default`` for NULL or bad JSON)."""
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _revision_cdf(sample: dict, rev: Optional[dict], data: Path) -> Path:
    """The CDF a revision was computed from (``sample_results.cdf_path``),
    else the sample's current file. ``SampleNotFound`` if there is none."""
    rel = (rev or {}).get("cdf_path") or sample.get("cdf_path")
    if not rel:
        raise SampleNotFound(f"Sample {sample['id']} has no stored CDF (result-only import)")
    p = data / rel
    if not p.is_file():
        raise SampleNotFound(f"Sample {sample['id']}'s CDF is missing: {rel}")
    return p


def _revision_blank_path(data: Path, db: Path, rev: dict) -> Optional[Path]:
    """The blank CDF a revision subtracted, or None: the blank file recorded
    on the revision (``pipeline.revision_blank_path``), never the blank
    sample's current file, which a later conflict Replace may have swapped.
    A revision written before that record existed falls back to the blank
    sample's file. ``FileNotFoundError`` if the file is gone (the curve would
    no longer match the revision's numbers)."""
    if rev.get("blank_used") is None:
        return None
    p = pipeline.revision_blank_path(rev["sample_id"], rev["revision"], db=db, data_dir=data)
    if p is None and not rev.get("blank_cdf_path"):
        blank = store.samples.get(rev["blank_used"], db=db)
        p = data / blank["cdf_path"] if blank is not None and blank.get("cdf_path") else None
    if p is None or not p.is_file():
        raise FileNotFoundError(f"The blank CDF revision {rev['revision']} subtracted is missing: {p}")
    return p


def _gc1(db) -> dict:
    row = store.instruments.get(instruments.GC1, db=db)
    if row is None:
        raise HubUnavailable("Instrument gc1 has not been set up yet")
    return row


def _instrument_ctx(instrument_id: str, conf: dict, db, data: Path) -> dict:
    """``instruments.context`` for a sample's instrument (every calibration
    consumer gets this merged conf; GC_CAL_CDF plays no part)."""
    row = store.instruments.get(instrument_id, db=db) or _gc1(db)
    return instruments.context(row, conf, data_dir=data)


def _revision_ladder(sample: dict, conf: dict, db, data: Path) -> tuple[list, list]:
    """``(times, carbons)`` for labelling a sample's carbon ranges: the anchor
    pairs its current revision was computed with (``calibration_used``), else
    the instrument's calibration ladder."""
    rev = store.get_revision(sample["id"], db=db) if sample.get("current_revision") else None
    cal = _json_col(rev.get("calibration_used"), {}) if rev else {}
    anchors = cal.get("anchors") if isinstance(cal, dict) else None
    if anchors and len(anchors) >= 2:
        return [float(a[0]) for a in anchors], [int(a[1]) for a in anchors]
    return distill.calibration_ladder(_instrument_ctx(sample["instrument_id"], conf, db, data))


def _who() -> str:
    return request.remote_addr or "unknown"


def _gate_reason(s: dict) -> str:
    """Why a sample fails the export/QBench gate (``store.GATE_SQL``)."""
    if s["status"] != "final":
        return f"not final (status {s['status']}" + (f": {s['error']})" if s["error"] else ")")
    if s["backfill"] and not s["released_at"]:
        return "backfill sample that has not been released"
    return "not exportable"


def _record_qbench_upload(sample_id, revision: Optional[int], db) -> None:
    """After a successful QBench upload: which revision the PDF was built from."""
    if sample_id is None or revision is None:
        return
    try:
        store.samples.update(int(sample_id), qbench_revision=revision,
                             qbench_uploaded_at=store.now_iso(), db=db)
    except Exception:
        LOGGER.exception("Could not record the QBench upload of sample %s", sample_id)


def _check_admin(body) -> bool:
    """True when the request body carries the admin password. Every gated
    route goes through here. ``admin_auth`` (D13) checks the salted PBKDF2
    hash with hmac.compare_digest and a per-client backoff; until a password
    is set at /admin/setup, every admin action is refused with that message."""
    return admin_auth.check_admin_body(body)


# ===================================================================== #
#  Cross-site write guard
# ===================================================================== #
# There is no login or session, and the app listens on the lab LAN, so any
# page open in a lab browser could otherwise POST here: get_json(force=True)
# parses a text/plain body, which is a CORS "simple" request (no preflight).
# Browsers mark every fetch with Origin (on non-GET) and Sec-Fetch-Site; the
# app's own pages send a matching Origin and "same-origin". Requests with
# neither header (curl, the updater, tests) are not from a browser page and
# pass. DNS rebinding is not covered (see the spec's Open items).

_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _host_port(netloc: str, scheme: str):
    """``(hostname, port)`` from a ``host[:port]`` string, or None."""
    try:
        parts = urllib.parse.urlsplit(f"{scheme}://{netloc}")
        host = (parts.hostname or "").lower()
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None
    return (host, port) if host else None


def _is_cross_site_request() -> bool:
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None and site.strip().lower() not in _ALLOWED_FETCH_SITES:
        return True
    origin = request.headers.get("Origin")
    if origin is not None:
        try:
            parts = urllib.parse.urlsplit(origin.strip())
        except ValueError:
            return True
        if parts.scheme not in _DEFAULT_PORTS or not parts.netloc:
            return True  # includes the opaque origin "null"
        mine = _host_port(request.host, request.scheme)
        return mine is None or _host_port(parts.netloc, parts.scheme) != mine
    return False


@app.before_request
def _refuse_cross_site_writes():
    """Refuse state-changing /api/ requests that a browser marks as coming
    from another site. Registered before activity tracking, so a refused
    request does not count as use."""
    if request.method in _STATE_CHANGING_METHODS and request.path.startswith("/api/") \
            and _is_cross_site_request():
        LOGGER.warning("Refused cross-site %s %s from %s (Origin %r, Sec-Fetch-Site %r)",
                       request.method, request.path, request.remote_addr,
                       request.headers.get("Origin"), request.headers.get("Sec-Fetch-Site"))
        return jsonify({"error": "Cross-site request refused"}), 403
    return None


def _admin_json_body():
    """The JSON body of an admin-gated route, or an error response. These
    routes require ``Content-Type: application/json`` on top of the
    cross-site guard, so no form or text/plain post can reach them."""
    admin_auth.limit_json_body()     # 64 KiB, before anything reads the body (2B1 review I4)
    if not request.is_json:
        return None, _error("Expected Content-Type: application/json", 415)
    try:
        return request.get_json(silent=True) or {}, None
    except RecursionError:                       # nested too deep (2B1 re-review G2)
        return None, _error("The request body is nested too deeply", 400)
    except admin_auth.RequestEntityTooLarge:
        return None, _error("The request body is too large.", 413)


# ===================================================================== #
#  Activity tracking & server restart
# ===================================================================== #

# Paths that are machines checking on the app, not a person using it. Counting
# them as activity would keep the idle timer from ever advancing: the updater
# polls /healthz continuously, static assets load on every page view, and
# these are the endpoints app.js hits on its own timers for the life of an
# open tab (not from a click): /api/notifications every 30s
# (setInterval(loadNotifications, 30000)); /healthz every 2s while waiting
# for a restart (_waitForServerAndReload; /api/server-status, which it used
# to poll, stays excluded for tabs still running an older app.js);
# /api/reprocess/status every 2s while a reprocess runs
# (_pollReprocessStatus); /api/qbench-upload-status once on load to
# reconnect to an in-progress upload. Excluding /static/ covers page assets;
# excluding paths ending in /stream covers the SSE routes' *reconnects* —
# before_request fires once per connection attempt, not per keep-alive byte
# sent over an already-open one, so a stream that free-runs for hours without
# reconnecting doesn't need (and can't get) re-exclusion after the first hit.
_NON_ACTIVITY_PATHS = {
    "/healthz",
    "/api/notifications",
    "/api/server-status",
    "/api/reprocess/status",
    "/api/qbench-upload-status",
    # 2B1: GC-PC agents are machines, never users (ingest_api)
    "/api/ingest", "/api/agent/heartbeat", "/api/agent/results",
    "/api/agent/package", "/api/agent/package.zip",
}


@app.before_request
def _track_activity():
    """Record the timestamp of every incoming request for idle detection."""
    global _last_activity
    path = request.path
    if path in _NON_ACTIVITY_PATHS or path.startswith("/static/") or path.endswith("/stream"):
        return
    with _last_activity_lock:
        _last_activity = time.time()
        _recent_clients[request.remote_addr] = _last_activity


def _is_server_idle() -> bool:
    """Return True if no one is actively using the server.

    "Idle" means:
      - No HTTP requests in the last AUTO_RESTART_IDLE_SECONDS
      - No user-initiated upload thread running
      - No queued reprocess / rebuild tasks being worked on

    The background file **watcher** and auto-scan are infrastructure —
    they run 24/7 and do NOT block the restart.  SSE keep-alive pings
    also don't count; the idle timer only advances on real requests.
    """
    with _last_activity_lock:
        idle_seconds = time.time() - _last_activity
    if idle_seconds < AUTO_RESTART_IDLE_SECONDS:
        return False
    # Block if user-initiated upload is running
    if _upload_thread and _upload_thread.is_alive():
        return False
    return True


APP_DIR = Path(__file__).resolve().parent

# ── Restart: plain, or by installing a staged release ─────────────────────
# One restart per process (button, second click or 3 AM): _restart_claimed is
# the single-flight flag. Under the updater a restart only ever EXITS — the
# updater's supervise() relaunches within ~20 s, and a replacement we spawned
# would race it for the port. Legacy mode spawns its replacement, except
# while a switch is under way (restart_policy.may_respawn).
_restart_lock = threading.RLock()
_restart_claimed = False
_restart_decision: tuple = ("restart", None)   # what the pending restart is doing


def _claim_restart() -> bool:
    """Take the single restart this process gets. False if one is under way."""
    global _restart_claimed
    with _restart_lock:
        if _restart_claimed:
            return False
        _restart_claimed = True
        return True


CSV_LOCK_EXIT_TIMEOUT_SECONDS = 30.0
_csv_lock_held_for_exit = False


def _hold_csv_lock_for_exit() -> bool:
    """Take distill._CSV_LOCK for the rest of this process's life, so an exit
    (ours, or the updater's taskkill /F) cannot land mid-write. Idempotent:
    the lock is not reentrant, and both the switch watcher and _do_restart
    call this. False if it stayed busy past the timeout."""
    global _csv_lock_held_for_exit
    if _csv_lock_held_for_exit:
        return True
    if distill._CSV_LOCK.acquire(timeout=CSV_LOCK_EXIT_TIMEOUT_SECONDS):
        _csv_lock_held_for_exit = True
        return True
    LOGGER.error("Results CSV still busy after %.0fs — exiting anyway",
                 CSV_LOCK_EXIT_TIMEOUT_SECONDS)
    return False


def _release_csv_lock_for_exit() -> None:
    global _csv_lock_held_for_exit
    if _csv_lock_held_for_exit:
        _csv_lock_held_for_exit = False
        distill._CSV_LOCK.release()


def _do_restart(reason: str = "restart") -> None:
    """Exit so a fresh process takes over: the updater relaunches us when
    deployed; legacy mode (or a paused updater) spawns its own replacement
    first — never while a switch is under way (restart_policy.should_respawn).

    Holds distill._CSV_LOCK through the exit so os._exit cannot land in the
    middle of a results-CSV write."""
    global _restart_claimed
    LOGGER.info("=== SERVER RESTART INITIATED (%s) ===", reason)
    # Give a moment for any in-flight response to finish
    time.sleep(1.0)

    def _exit() -> None:
        for h in logging.root.handlers:   # os._exit skips logging's atexit flush
            try:
                h.flush()
            except Exception:
                pass
        os._exit(0)

    try:
        spawn = restart_policy.should_respawn(paths.data_dir() or APP_DIR)
    except Exception:
        LOGGER.exception("could not decide whether to respawn — not respawning")
        spawn = False

    _stop_hub()        # let the Worker finish its job and the exporter its append
    _hold_csv_lock_for_exit()
    if not spawn:
        LOGGER.info("Exiting without a respawn; the updater restarts the app")
        _exit()
    try:
        # Spawn a new process *then* exit.  On Windows os.execv can be
        # unreliable, so use subprocess + os._exit instead.
        args, cwd, flags = restart_policy.respawn_command(
            _sys.executable, _sys.argv, deployed=paths.data_dir() is not None,
            app_dir=APP_DIR, cwd=os.getcwd(), windows=platform.system() == "Windows")
        subprocess.Popen(args, cwd=cwd, close_fds=True, creationflags=flags)
    except Exception:
        LOGGER.exception("Failed to spawn new server process")
        _release_csv_lock_for_exit()
        with _restart_lock:   # stay up; a later request may try again
            _restart_claimed = False
        return  # don't exit if we couldn't start the replacement
    LOGGER.info("New process spawned — shutting down old process")
    _exit()


def _await_switch_then_restart(data_dir: Path, tag: str, at: float) -> None:
    """Watch for the updater's answer, then restart. When it has the request
    this only exits (should_respawn: the switch files are gone by now, so
    only a paused updater makes us start our own replacement)."""
    # By design: once the updater has taken the request, on_taken holds
    # distill._CSV_LOCK until this process dies, so the updater's taskkill /F
    # cannot cut a results-CSV write in half. Until then every request that
    # reads the CSV (/api/table, /api/distillation-curve, the Looker's appends,
    # ...) blocks, for up to restart_policy.ACCEPTED_WAIT_SECONDS (~45 s) if
    # the updater is slow to stop us. Normally the stop comes within seconds.
    action = restart_policy.await_switch(data_dir, tag, at,
                                         on_taken=_hold_csv_lock_for_exit)
    _do_restart(f"switch to {tag}: {action}")


def request_restart(by: str) -> tuple:
    """Restart because someone clicked. Returns ``(mode, tag)``: ``("switch",
    tag)`` when a newer healthy release is staged and the updater was asked to
    install it, else ``("restart", None)``. The work happens on a thread so
    the caller answers first. Single-flight: a second click while one is
    pending changes nothing and reports the same answer."""
    global _restart_decision
    with _restart_lock:
        if not _claim_restart():
            LOGGER.info("Restart by %s ignored: one is already under way %s", by,
                        _restart_decision)
            return _restart_decision
        data_dir = paths.data_dir()
        try:
            mode, tag = restart_policy.decide(data_dir, version.APP_VERSION)
            if mode == "switch":
                at = time.time()
                if restart_update.write_switch_request(data_dir, tag, by=by, now=at):
                    LOGGER.info("Asked the updater to install %s; waiting for its answer", tag)
                    threading.Thread(target=_await_switch_then_restart,
                                     args=(data_dir, tag, at), daemon=True,
                                     name="await-switch").start()
                    _restart_decision = (mode, tag)
                    return _restart_decision
                LOGGER.warning("Could not ask the updater to install %s — restarting "
                               "normally", tag)
                restart_update.clear_switch_files(data_dir)
            else:
                LOGGER.info("Manual restart by %s: no newer release staged — plain restart", by)
        except Exception:
            LOGGER.exception("Could not prepare the restart — restarting normally")
            try:
                if data_dir is not None:
                    restart_update.clear_switch_files(data_dir)
            except Exception:
                LOGGER.exception("could not clear switch files")
        _restart_decision = ("restart", None)
        threading.Thread(target=_do_restart, args=("manual restart",), daemon=True,
                         name="restart").start()
        return _restart_decision


def _tidy_switch_files() -> None:
    """A new serving process means whatever the switch files describe
    already happened, one way or another. Called from ``__main__`` only
    after the port guard, so a duplicate launch (which exits there) never
    deletes the live process's pending request."""
    data_dir = paths.data_dir()
    if data_dir is None:
        return
    try:
        removed = restart_update.clear_switch_files(data_dir)
        if removed:
            LOGGER.warning("Removed %d leftover switch file(s) — a new process means "
                           "the restart already happened", removed)
    except Exception:
        LOGGER.exception("Could not tidy switch files (non-fatal)")


def _auto_restart_loop() -> None:
    """Background thread: once per day, restart the server if idle at the
    configured hour.  Checks every 60 seconds."""
    global _auto_restart_done_today
    while True:
        time.sleep(60)
        try:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            if not restart_policy.should_auto_restart(
                    hour=now.hour, today=today_str, done_today=_auto_restart_done_today,
                    uptime_seconds=time.time() - _server_start_time,
                    idle=now.hour == AUTO_RESTART_HOUR and _is_server_idle()):
                continue
            _auto_restart_done_today = today_str
            if not _claim_restart():
                LOGGER.info("Auto-restart: a restart is already under way")
                continue
            LOGGER.info("Auto-restart: server idle at %02d:00 — restarting", AUTO_RESTART_HOUR)
            _do_restart("auto-restart")
        except Exception:
            LOGGER.exception("Auto-restart check failed (non-fatal)")


# ===================================================================== #
#  Analysis helpers (trend, deviation, conclusion)
# ===================================================================== #

import analysis_core  # noqa: E402  (analysis numerics live there)
from analysis_core import (  # noqa: E402
    analyze_pair,
    compute_trend_line,
    detect_deviation_segments,
    detect_spike_segments,
    merge_overlapping_segments,
    carbon_to_time,
    segment_carbon_range,
    generate_conclusion,
)


# ===================================================================== #
#  PDF / report helpers
# ===================================================================== #

def _make_chromatogram_figure(
    t: np.ndarray, y: np.ndarray, title: str
) -> "go.Figure":
    """Build a Plotly chromatogram figure."""
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=t.tolist(), y=y.tolist(), mode="lines", name=title))
    fig.update_layout(
        title=title,
        xaxis_title="Time (min)",
        yaxis_title="Intensity",
        template="plotly_white",
    )
    return fig


def _figure_to_png_bytes(fig: "go.Figure", width: int = 1200, height: int = 500) -> bytes:
    """Render a Plotly figure to PNG bytes via kaleido."""
    return pio.to_image(fig, format="png", width=width, height=height)


def _generate_chromatogram_pdf(cdf_path: Path, title: Optional[str] = None) -> bytes:
    """Generate a single-page chromatogram PDF for *cdf_path* (``title``
    defaults to the CDF's own name and time)."""
    t, y = distill.gc_xy_from_cdf(cdf_path)
    if title is None:
        sample, inj_dt = distill.cdf_metadata(cdf_path)
        title = f"{sample} - {inj_dt.strftime('%Y-%m-%d %H:%M')}"
    fig = _make_chromatogram_figure(t, y, title)
    return pio.to_image(fig, format="pdf", width=1200, height=600)


def _generate_comparison_html(
    sample_path: Path, standard_paths: list[Path], sample_name: Optional[str] = None
) -> str:
    """Generate an HTML page with overlaid chromatograms (sample vs standards)."""
    fig = go.Figure()
    t_s, y_s = distill.gc_xy_from_cdf(sample_path)
    if sample_name is None:
        sample_name, _ = distill.cdf_metadata(sample_path)
    fig.add_trace(go.Scatter(
        x=t_s.tolist(), y=y_s.tolist(), mode="lines",
        name=f"Sample: {sample_name}",
    ))
    for std_path in standard_paths:
        t_std, y_std = distill.gc_xy_from_cdf(std_path)
        std_name, _ = distill.cdf_metadata(std_path)
        fig.add_trace(go.Scatter(
            x=t_std.tolist(), y=y_std.tolist(), mode="lines",
            name=f"Std: {std_name}",
        ))
    fig.update_layout(
        title=f"Comparison: {sample_name}",
        xaxis_title="Time (min)",
        yaxis_title="Intensity",
        template="plotly_white",
    )
    return pio.to_html(fig, full_html=True)


def _generate_analysis_report_pdf(
    params: dict,
    analysis_result: dict,
    ranges: list[dict] | None = None,
    ladder: tuple[list, list] | None = None,
) -> bytes:
    """Generate a styled PDF report matching the old desktop app's
    ``_send_to_analysis_queue`` output 1:1.

    Layout: two Plotly charts (chromatogram overlay + difference plot),
    rendered as PNG, embedded in an HTML template with header/logo,
    deviation bullets, conclusion, and footer — converted to PDF via
    xhtml2pdf (replacing the desktop app's QPrinter).
    """
    # ── Extract parameters ────────────────────────────────────────────
    doc_name = params.get("doc_name", "GC Analysis")
    lab_id = params.get("lab_id", "")
    conclusion = params.get("conclusion", analysis_result.get("conclusion", ""))
    bullets_raw = params.get("bullets", analysis_result.get("bullets", ""))
    std_name = params.get("standard_name", "Standard")
    overlay_standards = params.get("overlay_standards", [])

    sample_name = lab_id or "Sample"

    date_display = datetime.now().strftime("%B %d, %Y")
    datetime_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Load config & calibration ─────────────────────────────────────
    conf = settings_mod.load_settings()
    x_max_min = float(conf.get("analysis_x_max_min", 7.0))

    # The sample revision's anchors when the caller has them (every hub
    # route does); the configured calibration otherwise.
    cal_times, cal_carbons = ladder if ladder is not None else distill.calibration_ladder(conf)

    # ── Data arrays ───────────────────────────────────────────────────
    t_common = np.array(analysis_result["sample_raw"]["x"])
    std_raw = np.array(analysis_result["std_raw"]["y"])
    smp_raw = np.array(analysis_result["sample_raw"]["y"])
    diff = np.array(analysis_result["difference"]["y"])

    # ── Region list (operator-defined; falls back to saved/legacy) ────
    if ranges is None:
        ranges = analysis_core.resolve_report_ranges(params.get("ranges"), conf)

    # ── Calibration shapes + annotations (shared by both figures) ─────
    cal_shapes: list[dict] = []
    cal_annotations: list[dict] = []
    for ci, peak_time in enumerate(cal_times):
        try:
            tval = float(peak_time)
        except Exception:
            continue
        cal_shapes.append(dict(
            type="line", x0=tval, y0=0, x1=tval, y1=1,
            xref="x", yref="paper",
            line=dict(color="#e3b341", dash="dot", width=1),
            layer="below",
        ))
        if ci < len(cal_carbons):
            lbl = f"C{cal_carbons[ci]}"
        else:
            lbl = f"#{ci + 1}"
        cal_annotations.append(dict(
            x=tval, y=1.03, xref="x", yref="paper",
            text=f"<b>{lbl}</b>", showarrow=False,
            font=dict(color="#e3b341", size=14),
            xanchor="center", yanchor="bottom",
        ))

    # ── Gas / Oil range rectangles + labels ───────────────────────────
    range_shapes: list[dict] = []
    range_poly_rects: list[dict] = []
    range_annotations: list[dict] = []

    def _c_to_time(c: int) -> "float | None":
        if not cal_times:
            return None
        cn = np.array(cal_carbons, dtype=float)
        ct = np.array(cal_times, dtype=float)
        if len(cn) < 2:
            return float(ct[0]) if len(ct) else None
        if c <= cn[0]:
            slope = (ct[1] - ct[0]) / (cn[1] - cn[0])
            return float(max(0.0, ct[0] + slope * (c - cn[0])))
        if c >= cn[-1]:
            slope = (ct[-1] - ct[-2]) / (cn[-1] - cn[-2])
            return float(ct[-1] + slope * (c - cn[-1]))
        return float(np.interp(c, cn, ct))

    _rgba = analysis_core.range_color_rgba
    for rng in ranges:
        c_start, c_end = int(rng["c_start"]), int(rng["c_end"])
        color = rng.get("color", "#f0a500")
        fill_col = _rgba(color, 0.12)
        fill_poly = _rgba(color, 0.18)
        line_poly = _rgba(color, 0.70)
        line_col = _rgba(color, 0.55)
        label_txt = f"{rng.get('label', 'Range')} C{c_start}\u2013C{c_end}"
        t_lo = _c_to_time(c_start)
        t_hi = _c_to_time(c_end)
        if t_lo is not None and t_hi is not None and t_hi > t_lo:
            range_shapes.append(dict(
                type="rect", x0=t_lo, x1=t_hi, y0=0, y1=1,
                xref="x", yref="paper",
                fillcolor=fill_col,
                line=dict(color=line_col, width=1, dash="dash"),
                layer="below",
            ))
            range_poly_rects.append(dict(
                t_lo=t_lo, t_hi=t_hi,
                fill=fill_poly, border=line_poly,
            ))
            range_annotations.append(dict(
                x=(t_lo + t_hi) / 2, y=0.98, xref="x", yref="paper",
                text=f"<b>{label_txt}</b>", showarrow=False,
                font=dict(color="#555555", size=18),
                xanchor="center", yanchor="top",
                bgcolor="rgba(255,255,255,0.75)", borderpad=2,
            ))

    all_shapes = range_shapes + cal_shapes
    all_annotations = range_annotations + cal_annotations

    # ── Chart styling constants (match old desktop app exactly) ───────
    PLOT_W, PLOT_H, PLOT_H2 = 1400, 520, 480
    BG = "#ffffff"
    PLOT_BG = "#f7f7f7"
    FG = "#2c2c2c"
    GRID = "rgba(0,0,0,0.08)"
    AXIS = dict(
        title_font=dict(color=FG, size=22), tickfont=dict(color=FG, size=16),
        linecolor="#cccccc", gridcolor=GRID, zeroline=False,
    )

    # ── Figure 1: raw chromatograms ──────────────────────────────────
    fig1 = go.Figure()
    fig1.add_trace(go.Scatter(
        x=t_common.tolist(), y=std_raw.tolist(),
        name=f"Standard: {std_name}",
        mode="lines", line=dict(color="#888888", width=1),
        hovertemplate="Time: %{x:.2f} min<br>Intensity: %{y:.0f}<extra></extra>",
    ))
    fig1.add_trace(go.Scatter(
        x=t_common.tolist(), y=smp_raw.tolist(),
        name=f"Sample: {sample_name}",
        mode="lines", line=dict(color="#c0392b", width=1),
        hovertemplate="Time: %{x:.2f} min<br>Intensity: %{y:.0f}<extra></extra>",
    ))

    # Overlay additional comparison standards
    _ov_colors = ["#3498db", "#27ae60", "#8e44ad", "#e67e22", "#16a085"]
    comp_dir = Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))
    for oi, ov_name in enumerate(overlay_standards):
        try:
            ov_path = comp_dir / f"{ov_name}.CDF"
            if not ov_path.is_file():
                ov_path = comp_dir / f"{ov_name}.cdf"
            if not ov_path.is_file():
                ov_path = Path(ov_name)
            if not ov_path.is_file():
                continue
            try:
                ox, oy = distill.gc_xy_from_cdf(ov_path)
            except Exception:
                _tr = distill.gc_trace_from_cdf(ov_path)
                ox, oy = _tr.x, _tr.y
            ox = np.array(ox, dtype=float)
            oy = np.array(oy, dtype=float)
            oy_i = np.interp(t_common, ox, oy)
            fig1.add_trace(go.Scatter(
                x=t_common.tolist(), y=oy_i.tolist(),
                name=ov_path.stem,
                mode="lines",
                line=dict(color=_ov_colors[oi % len(_ov_colors)], width=1, dash="dot"),
                hovertemplate="Time: %{x:.2f} min<br>Intensity: %{y:.0f}<extra></extra>",
            ))
        except Exception:
            LOGGER.warning("Could not load overlay standard %s", ov_name)

    fig1.update_layout(
        template="none",
        paper_bgcolor=BG, plot_bgcolor=PLOT_BG,
        font=dict(color=FG, size=18),
        margin=dict(l=80, r=80, t=150, b=60),
        legend=dict(
            orientation="h", x=0.5, xanchor="center",
            y=1.18, yanchor="bottom",
            font=dict(size=14),
            bgcolor="rgba(255,255,255,0.9)", bordercolor="#cccccc", borderwidth=1,
        ),
        shapes=all_shapes, annotations=all_annotations,
        xaxis=dict(AXIS, title="Time (min)", range=[float(t_common[0]), x_max_min]),
        yaxis=dict(AXIS, title="Intensity"),
    )

    # ── Figure 2: difference ─────────────────────────────────────────
    diff_pos = np.where(diff > 0, diff, 0.0)
    diff_neg = np.where(diff < 0, diff, 0.0)

    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(
        x=t_common.tolist(), y=diff_pos.tolist(), fill="tozeroy",
        fillcolor="rgba(192,57,43,0.12)", line=dict(width=0),
        showlegend=False, hoverinfo="skip",
    ))
    fig2.add_trace(go.Scatter(
        x=t_common.tolist(), y=diff_neg.tolist(), fill="tozeroy",
        fillcolor="rgba(52,152,219,0.12)", line=dict(width=0),
        showlegend=False, hoverinfo="skip",
    ))
    fig2.add_trace(go.Scatter(
        x=t_common.tolist(), y=diff.tolist(),
        name="Difference (sample \u2212 std)",
        mode="lines", line=dict(color="#c0392b", width=1.5),
        hovertemplate="Time: %{x:.2f} min<br>Diff: %{y:.0f}<extra></extra>",
    ))
    fig2.add_hline(y=0, line=dict(color="#aaaaaa", width=1, dash="dash"))

    _diff_ypad = float(np.max(np.abs(diff))) * 1.5 if len(diff) else 1.0
    for pr in range_poly_rects:
        xl, xh = pr["t_lo"], pr["t_hi"]
        fig2.add_trace(go.Scatter(
            x=[xl, xl, xh, xh, xl],
            y=[-_diff_ypad, _diff_ypad, _diff_ypad, -_diff_ypad, -_diff_ypad],
            fill="toself", fillcolor=pr["fill"],
            line=dict(color=pr["border"], width=2),
            mode="lines", showlegend=False, hoverinfo="skip",
        ))
    fig2.update_layout(
        template="none",
        paper_bgcolor=BG, plot_bgcolor=PLOT_BG,
        font=dict(color=FG, size=18),
        margin=dict(l=80, r=80, t=130, b=60),
        legend=dict(
            orientation="h", x=0.5, xanchor="center",
            y=1.18, yanchor="bottom",
            font=dict(size=14),
            bgcolor="rgba(255,255,255,0.9)", bordercolor="#cccccc", borderwidth=1,
        ),
        shapes=cal_shapes, annotations=all_annotations,
        xaxis=dict(AXIS, title="Time (min)", range=[float(t_common[0]), x_max_min]),
        yaxis=dict(AXIS, title="Difference (sample \u2212 std)"),
    )

    # ── Render figures to PNG bytes ───────────────────────────────────
    try:
        png1_bytes = fig1.to_image(format="png", width=PLOT_W, height=PLOT_H, scale=2)
        png2_bytes = fig2.to_image(format="png", width=PLOT_W, height=PLOT_H2, scale=2)
    except Exception as exc:
        LOGGER.error("Chart render failed: %s", exc)
        raise RuntimeError(f"Could not render Plotly charts: {exc}") from exc

    img1_b64 = base64.b64encode(png1_bytes).decode("ascii")
    img2_b64 = base64.b64encode(png2_bytes).decode("ascii")

    # ── Logo (optional) ──────────────────────────────────────────────
    logo_path = conf.get("analysis_report_logo", "").strip()
    has_logo = False
    logo_b64 = ""
    if logo_path:
        try:
            logo_b64 = base64.b64encode(Path(logo_path).read_bytes()).decode("ascii")
            has_logo = True
        except Exception:
            pass

    # ── Bullets HTML ─────────────────────────────────────────────────
    bullet_lines_final = [
        l.strip().lstrip("\u2022").strip()
        for l in (bullets_raw or "").splitlines()
        if l.strip()
    ]
    if bullet_lines_final:
        items = "".join(
            f'<li style="margin-bottom:5px; color:#2c2c2c;">{b}</li>'
            for b in bullet_lines_final
        )
        bullets_html = (
            f'<ul style="margin:0; padding-left:20px; font-size:9pt;">{items}</ul>'
        )
    else:
        bullets_html = (
            '<p style="color:#888888; font-style:italic; margin:0; font-size:9pt;">'
            "No deviations detected above the marginal threshold."
            "</p>"
        )

    logo_cell_html = (
        f'<img src="data:image/png;base64,{logo_b64}" height="56">'
        if has_logo
        else '<span style="color:#1c1c1c;">&#8203;</span>'
    )
    lab_id_html = (
        f'<span style="font-size:9pt; font-weight:bold; color:#1c1c1c;">Lab ID:&nbsp;{lab_id}</span><br>'
        if lab_id else ""
    )
    conc_html = (
        conclusion.replace("\n", "<br>")
        if conclusion
        else '<em style="color:#888888;">No conclusion available.</em>'
    )

    # ── Assemble HTML (1:1 match with old desktop app template) ──────
    html = f"""<!DOCTYPE HTML>
<html><head><meta charset="utf-8"></head>
<body style="font-family:'Segoe UI',Arial,sans-serif; color:#2c2c2c; background:#ffffff; margin:0; padding:0;">

<!-- HEADER -->
<table width="100%" cellspacing="0" cellpadding="0"
       style="border-bottom:3px solid #c0392b;">
<tr>
  <td width="140" style="padding:8px 10px 8px 12px; vertical-align:middle;">{logo_cell_html}</td>
  <td style="padding:8px 6px; text-align:center; vertical-align:middle;">
    <div style="font-size:13pt; font-weight:bold; color:#1c1c1c; letter-spacing:0.5px;">{doc_name}</div>
  </td>
  <td width="140" style="padding:8px 12px 8px 6px; text-align:right; vertical-align:middle;">
    {lab_id_html}
    <span style="font-size:8pt; color:#555555;">{date_display}</span>
  </td>
</tr>
</table>

<!-- TREND PLOT -->
<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:8px;">
<tr><td style="background-color:#c0392b; padding:3px 10px; color:#ffffff; font-size:9pt; font-weight:bold; letter-spacing:0.5px;">
  GC Trend Analysis &mdash; Sample vs {std_name}
</td></tr>
</table>
<table width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #dddddd; background-color:#ffffff; margin-top:0;">
<tr><td style="text-align:center; padding:1px;">
  <img src="data:image/png;base64,{img1_b64}" width="810" height="260">
</td></tr>
</table>

<!-- DIFFERENCE PLOT -->
<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:6px;">
<tr><td style="background-color:#c0392b; padding:3px 10px; color:#ffffff; font-size:9pt; font-weight:bold; letter-spacing:0.5px;">
  Difference Plot &mdash; Sample &minus; {std_name}
</td></tr>
</table>
<table width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #dddddd; background-color:#ffffff; margin-top:0;">
<tr><td style="text-align:center; padding:1px;">
  <img src="data:image/png;base64,{img2_b64}" width="810" height="240">
</td></tr>
</table>

<!-- DEVIATION ANALYSIS -->
<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:6px;">
<tr><td style="background-color:#c0392b; padding:3px 10px; color:#ffffff; font-size:9pt; font-weight:bold; letter-spacing:0.5px;">
  Deviation Analysis
</td></tr>
</table>
<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:0;">
<tr>
  <td width="4" style="background-color:#c0392b;"></td>
  <td style="background-color:#f8f8f8; padding:6px 12px 8px 12px; border:1px solid #e8e8e8; border-left:none;">
    {bullets_html}
  </td>
</tr>
</table>

<!-- CONCLUSION -->
<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:6px;">
<tr><td style="background-color:#555555; padding:3px 10px; color:#ffffff; font-size:9pt; font-weight:bold; letter-spacing:0.5px;">
  Conclusion
</td></tr>
</table>
<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:0;">
<tr>
  <td width="4" style="background-color:#888888;"></td>
  <td style="background-color:#f3f3f3; padding:6px 12px 8px 12px; font-size:8.5pt; font-style:italic; color:#333333; border:1px solid #e0e0e0; border-left:none;">
    {conc_html}
  </td>
</tr>
</table>

<!-- FOOTER -->
<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:8px; border-top:1px solid #cccccc;">
<tr><td style="padding-top:4px; text-align:center; font-size:7pt; color:#999999;">
  Generated by GC Viewer &bull; {datetime_str}
</td></tr>
</table>

</body></html>"""

    # ── Convert HTML to PDF ──────────────────────────────────────────
    # Use xhtml2pdf (replaces QPrinter from the desktop app).
    # Page: US Letter with 12mm margins — matching the old app exactly.
    if xhtml2pdf_pisa is not None:
        pdf_buffer = io.BytesIO()
        # Wrap HTML with @page CSS to match old QPrinter Letter + 12mm margins
        styled_html = f"""<!DOCTYPE HTML>
<html><head><meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 12mm;
}}
</style>
</head>
<body style="font-family:'Segoe UI',Arial,sans-serif; color:#2c2c2c; background:#ffffff; margin:0; padding:0;">
{html.split('<body', 1)[1].split('>', 1)[1].rsplit('</body>', 1)[0]}
</body></html>"""
        status = xhtml2pdf_pisa.CreatePDF(styled_html, dest=pdf_buffer)
        if not status.err:
            return pdf_buffer.getvalue()
        LOGGER.warning("xhtml2pdf conversion had errors, falling back to PNG")

    # Fallback: return first chart as PDF via kaleido
    LOGGER.warning("xhtml2pdf not available, falling back to kaleido single-chart PDF")
    try:
        return pio.to_image(fig1, format="pdf", width=1920, height=1080, scale=2)
    except Exception:
        # Last resort: return the HTML itself
        return html.encode("utf-8")


# ===================================================================== #
#  API: Settings
# ===================================================================== #

def _gc1_calibration_cdf() -> Optional[str]:
    """The gc1 row's calibration CDF (None when there is no store yet)."""
    try:
        _data, db = _hub()
        row = store.instruments.get(instruments.GC1, db=db)
    except HubUnavailable:
        return None
    return None if row is None else (row.get("calibration_cdf") or "")


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    try:
        conf = settings_mod.load_settings()
        cal = _gc1_calibration_cdf()
        if cal is not None:
            conf = dict(conf, calibration_cdf=cal)
        return jsonify(conf)
    except Exception as exc:
        return _error(str(exc), 500)


# Per-instrument configuration lives in the store's instruments rows (the
# calibration page / Instruments page, admin-gated); settings.json's copies
# are read-only through /api/settings. A save may echo them back unchanged.
READ_ONLY_SETTINGS = ("calibration_cdf", "calibration_assignments", "calibration_sensitivity",
                      "correction_factors_json")


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    """Save the global settings. JSON only (415 otherwise: a text/plain post
    is a CORS "simple" request). ``READ_ONLY_SETTINGS`` keep their saved
    values; a body that changes one is refused (400) and nothing is saved."""
    if not request.is_json:
        return _error("Expected Content-Type: application/json", 415)
    try:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return _error("Expected JSON object")
        current = settings_mod.load_settings()
        gc1_cal = _gc1_calibration_cdf()
        effective = dict(current, calibration_cdf=gc1_cal) if gc1_cal is not None else current
        changed = [k for k in READ_ONLY_SETTINGS
                   if k in body and str(body[k] or "") != str(effective.get(k) or "")]
        if changed:
            return _error(f"{', '.join(changed)} cannot be changed here: calibration and "
                          f"correction factors are per instrument now (the Calibration and "
                          f"Instruments pages, admin).", 400)
        body = {k: v for k, v in body.items() if k not in READ_ONLY_SETTINGS}
        on_disk = {}
        if settings_mod.CONFIG_PATH is not None and settings_mod.CONFIG_PATH.is_file():
            try:
                on_disk = json.loads(settings_mod.CONFIG_PATH.read_text(encoding="utf-8"))
            except ValueError:
                on_disk = {}
        body.update({k: on_disk[k] for k in READ_ONLY_SETTINGS if k in on_disk})
        # Flag rules and best-fit settings need no cache clearing: sample_cache
        # rows carry the fingerprint they were computed with.
        settings_mod.save_settings(body)

        conf = settings_mod.load_settings()
        cal = _gc1_calibration_cdf()
        if cal is not None:
            conf = dict(conf, calibration_cdf=cal)
        return jsonify(conf)
    except Exception as exc:
        return _error(str(exc), 500)


@app.route("/api/save-analysis-defaults", methods=["POST"])
def api_save_analysis_defaults():
    """Save current analysis parameters as persistent defaults (password-protected)."""
    try:
        body, err = _admin_json_body()
        if err:
            return err
        if not _check_admin(body):
            return _error("Incorrect password", 403)
        params = body.get("params", {})
        overlays = body.get("range_overlays", [])
        conf = settings_mod.load_settings()
        # Trend line + thresholds
        for key in ("quantile", "window", "sigma", "thresh_marginal",
                     "thresh_moderate", "thresh_significant", "x_max_min"):
            if key in params:
                conf[f"analysis_{key}"] = str(params[key])
        # Full range overlays as JSON
        conf["analysis_range_overlays"] = json.dumps(overlays)
        settings_mod.save_settings(conf)
        return jsonify({"ok": True})
    except Exception as exc:
        return _error(str(exc), 500)


# ===================================================================== #
#  API: Files
# ===================================================================== #

@app.route("/api/files", methods=["GET"])
def api_files():
    """The sample list: a store query, newest injection first, with paging and
    filters (``instrument``, ``status`` and ``method`` take comma lists; ``q``,
    ``date_from``, ``date_to``, ``backfill``, ``limit``, ``offset``). Flags and
    best-fit come from ``sample_cache`` (and the current revision's recorded
    best-fit); this route never reads a CDF. Stale cache rows are refreshed
    in the background and counted in ``cache_pending``."""
    _data, db = _hub()
    args = request.args
    try:
        limit = min(max(int(args.get("limit", FILES_DEFAULT_LIMIT)), 1), FILES_MAX_LIMIT)
        offset = max(int(args.get("offset", 0)), 0)
    except ValueError:
        return _error("limit and offset must be integers")
    backfill = args.get("backfill")
    filters = {
        "q": (args.get("q") or "").strip() or None,
        "instrument": _list_arg(args.get("instrument")),
        "date_from": args.get("date_from") or None,
        "date_to": args.get("date_to") or None,
        "status": _list_arg(args.get("status")),
        "method_name": _list_arg(args.get("method")),
        "backfill": None if backfill in (None, "") else backfill.lower() in ("1", "true", "yes"),
    }
    fps = _cache_fingerprints(settings_mod.load_settings())
    try:
        with store.connection(db) as conn:
            rows = store.samples.search(limit=limit, offset=offset, db=conn, **filters)
            total = store.samples.count(db=conn, **filters)
            insts = [i["id"] for i in store.instruments.list(db=conn)]
            ids = [r["id"] for r in rows]
            marks = ",".join("?" * len(ids))
            cache = {c["sample_id"]: dict(c) for c in conn.execute(
                f"SELECT * FROM sample_cache WHERE sample_id IN ({marks})", ids)} if ids else {}
            recorded = {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT r.sample_id, r.best_fit, r.fit_score FROM sample_results r "
                "JOIN samples s ON s.id = r.sample_id AND r.revision = s.current_revision "
                f"WHERE s.id IN ({marks})", ids)} if ids else {}
            run_no = _run_numbers(conn, rows)
    except ValueError as exc:            # a malformed date bound
        return _error(str(exc))
    samples, stale = [], []
    for s in rows:
        entry, is_stale = _sample_entry(s, cache.get(s["id"]), recorded.get(s["id"]), fps,
                                        run_no.get(s["id"], 1))
        samples.append(entry)
        if is_stale:
            stale.append(s["id"])
    if stale:
        _schedule_cache_refresh(stale)
    return jsonify({"samples": samples, "total": total, "limit": limit, "offset": offset,
                    "instruments": insts, "cache_pending": len(stale)})


FILES_DEFAULT_LIMIT = 500
FILES_MAX_LIMIT = 5000


def _list_arg(raw: Optional[str]) -> Optional[list]:
    items = [x.strip() for x in (raw or "").split(",") if x.strip()]
    return items or None


def _run_numbers(conn, rows: list[dict]) -> dict:
    """``{sample id: run number}``: the order of each injection among its
    instrument's injections of the same lab ID (oldest = 1), for the
    ``AF25 (2)`` labels."""
    labs = sorted({r["lab_id"] for r in rows})
    if not labs:
        return {}
    marks = ",".join("?" * len(labs))
    return {r[0]: r[1] for r in conn.execute(
        "SELECT id, ROW_NUMBER() OVER (PARTITION BY instrument_id, lab_id "
        f"ORDER BY injection_dt, id) FROM samples WHERE lab_id IN ({marks})", labs)}


def _sample_entry(s: dict, cache: Optional[dict], recorded, fps: dict, run_no: int):
    """One /api/files entry and whether its sample_cache row is stale."""
    flags = None
    if cache and cache.get("rules_fingerprint") == fps["rules_fp"]:
        flags = _json_col(cache.get("flags"), [])
    best = None
    bestfit_fresh = fps["bestfit_fp"] is None or (
        cache is not None and cache.get("bestfit_fingerprint") == fps["bestfit_fp"])
    if fps["bestfit_fp"] is not None and bestfit_fresh and cache.get("best_fit"):
        best = {"label": cache["best_fit"], "score": cache.get("fit_score")}
    elif recorded and recorded[0]:
        best = {"label": recorded[0], "score": recorded[1]}
    stale = flags is None or not bestfit_fresh
    flags = flags or []
    name = s["lab_id"]
    return {
        "sample_id": s["id"],
        "uid": str(s["id"]),
        "instrument": s["instrument_id"],
        "lab_id": name,
        "name": name,
        "display_name": name if run_no <= 1 else f"{name} ({run_no})",
        "injection_dt": s["injection_dt"],
        "status": s["status"],
        "error": s["error"],
        "review_note": s.get("review_note"),
        "flags": flags,
        "early_signal": bool(flags),
        "best_fit": best,
        "backfill": s["backfill"],
        "released": s["released_at"] is not None,
        "time_corrected": s["time_corrected"],
        "method_name": s["method_name"],
        "current_revision": s["current_revision"],
    }, stale


@app.route("/api/samples/<int:sample_id>/metadata", methods=["GET"])
def api_sample_metadata(sample_id: int):
    _data, db = _hub()
    s = _sample_or_404(sample_id, db, from_path=True)
    revisions = [{"revision": r["revision"], "reason": r["reason"], "by": r["by"],
                  "processed_at": r["processed_at"]}
                 for r in store.list_revisions(sample_id, db=db)]
    return jsonify({
        "sample_id": s["id"],
        "instrument": s["instrument_id"],
        "lab_id": s["lab_id"],
        "sample_name": s["lab_id"],
        "injection_datetime": s["injection_dt"],
        "injection_dt_source": s["injection_dt_source"],
        "legacy_injection_dt": s["legacy_injection_dt"],
        "time_corrected": s["time_corrected"],
        "method_name": s["method_name"],
        "source_name": s["source_name"],
        "status": s["status"],
        "error": s["error"],
        "review_note": s.get("review_note"),
        "backfill": s["backfill"],
        "released_at": s["released_at"],
        "current_revision": s["current_revision"],
        "qbench_revision": s["qbench_revision"],
        "qbench_uploaded_at": s["qbench_uploaded_at"],
        "revisions": revisions,
    })


def _requested_revision(sample: dict, db) -> Optional[dict]:
    """The revision named by ``?revision=`` (404 if it doesn't exist), else the
    current one (None if the sample has none)."""
    raw = request.args.get("revision")
    if raw in (None, ""):
        return store.get_revision(sample["id"], db=db) if sample["current_revision"] else None
    try:
        rev = store.get_revision(sample["id"], int(raw), db=db)
    except ValueError:
        rev = None
    if rev is None:
        raise SampleNotFound(f"Sample {sample['id']} has no revision {raw}")
    return rev


# ===================================================================== #
#  API: Chromatogram trace
# ===================================================================== #

@app.route("/api/samples/<int:sample_id>/trace", methods=["GET"])
def api_sample_trace(sample_id: int):
    """The chromatogram of the CDF the (current or ``?revision=``) revision was
    computed from; the sample's stored file when it has no revision."""
    data, db = _hub()
    s = _sample_or_404(sample_id, db, from_path=True)
    p = _revision_cdf(s, _requested_revision(s, db), data)
    try:
        t, y = distill.gc_xy_from_cdf(p)
        return jsonify({"sample_id": s["id"], "x": t.tolist(), "y": y.tolist(), "name": s["lab_id"]})
    except Exception as exc:
        return _error(str(exc), 500)


# ===================================================================== #
#  API: Distillation curve
# ===================================================================== #

_D2887_COLS = distill.CSV_HEADER[2:15]
_D86_COLS = distill.CSV_HEADER[15:28]
_D86_COL_FOR_CUT = {
    "IBP": "D86 IBP", "5%": "D86 T5", "10%": "D86 T10", "20%": "D86 T20",
    "30%": "D86 T30", "40%": "D86 T40", "50%": "D86 T50", "60%": "D86 T60",
    "70%": "D86 T70", "80%": "D86 T80", "90%": "D86 T90", "95%": "D86 T95",
    "FBP": "D86 FBP",
}


def _numbers(src: dict, keys) -> dict:
    out = {}
    for k in keys:
        v = src.get(k)
        if v in (None, ""):
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def _revision_curve(cdf: Path, anchors: list, blank_path: Optional[Path]):
    """(percent, temperature) exactly as ``distill.compute`` built them for the
    revision: its blank subtracted (or none), then its anchor pairs."""
    t, y = distill.gc_xy_from_cdf(cdf)
    blank = distill.gc_xy_from_cdf(blank_path) if blank_path is not None else None
    t, y = distill._apply_blank_and_clip(t, y, blank)
    cbp = distill.carbon_bp_map()
    rt = np.array([float(a[0]) for a in anchors], float)
    bp = np.array([cbp[int(a[1])] for a in anchors], float)
    cal = distill.build_calibration_from_anchors(rt, bp)
    return distill._cumulative_percent(t, y), cal(t)


@app.route("/api/samples/<int:sample_id>/distillation-curve", methods=["GET"])
def api_sample_distillation_curve(sample_id: int):
    """The distillation curve and numbers of one revision (current, or
    ``?revision=``). The numbers are the revision's stored results, never
    recomputed; the curve is rebuilt from the revision's CDF, its recorded
    ``blank_used`` and its ``calibration_used`` anchors. 409 while the sample
    has no revision (the hold reason is in the message)."""
    data, db = _hub()
    s = _sample_or_404(sample_id, db, from_path=True)
    rev = _requested_revision(s, db)
    if rev is None:
        why = s["status"] + (f": {s['error']}" if s["error"] else "")
        return _error(f"Sample {sample_id} has no result yet ({why})", 409)
    cdf = _revision_cdf(s, rev, data)
    try:
        cal = _json_col(rev["calibration_used"], {}) or {}
        anchors = cal.get("anchors") or []
        blank_path = _revision_blank_path(data, db, rev)
        if len(anchors) >= 2:
            pct, temp = _revision_curve(cdf, anchors, blank_path)
            calibration = {"cdf": cal.get("cdf"), "anchors": anchors, "source": "revision"}
        else:   # a legacy (imported) revision records no anchors
            ctx = _instrument_ctx(s["instrument_id"], settings_mod.load_settings(), db, data)
            pct, temp = distill.distillation_curve_from_cdf(cdf, blank_path=blank_path, conf=ctx)
            calibration = {"cdf": ctx.get("calibration_cdf"), "anchors": [], "source": "instrument"}
        results = _json_col(rev["results"], {}) or {}
        unc = _json_col(rev["d86_uncorrected"], {}) or {}
        d86_unc = {}
        for cut, v in unc.items():
            col = _D86_COL_FOR_CUT.get(cut)
            if col and v not in (None, ""):
                try:
                    d86_unc[col] = float(v)
                except (TypeError, ValueError):
                    pass
        return jsonify({
            "sample_id": s["id"],
            "revision": rev["revision"],
            "percent": pct.tolist(),
            "temperature": temp.tolist(),
            "d2887": _numbers(results, _D2887_COLS),
            "d86": _numbers(results, _D86_COLS),       # corrected: the revision's reported values
            "d86_uncorrected": d86_unc,                # before the correction factors
            "blank_used": rev["blank_used"],
            "calibration": calibration,
        })
    except Exception as exc:
        return _error(str(exc), 500)


# ===================================================================== #
#  API: Table data (current revisions)
# ===================================================================== #

def _csv_cell(v) -> str:
    return "" if v is None else str(v)


@app.route("/api/table", methods=["GET"])
def api_table():
    """Every sample's current revision in the results-CSV columns (cells as
    the CSV writes them), oldest injection first; ``sample_ids`` runs
    parallel to ``rows``. ``InjectionDateTime`` is the sample's corrected
    ``injection_dt``, never the revision's stored cell: an imported v1
    revision keeps v1's (possibly misparsed) time verbatim as the record.
    Other cells are shown as stored (numbers from the Worker, v1's exact
    strings from an import)."""
    _data, db = _hub()
    header = list(distill.CSV_HEADER)
    inj_col = header.index("InjectionDateTime")
    rows, ids = [], []
    with store.connection(db) as conn:
        for sid, injection_dt, results in conn.execute(
                "SELECT s.id, s.injection_dt, r.results FROM samples s JOIN sample_results r "
                "ON r.sample_id = s.id AND r.revision = s.current_revision "
                "ORDER BY s.injection_dt, s.id"):
            vals = _json_col(results, {}) or {}
            row = [_csv_cell(vals.get(c)) for c in header]
            row[inj_col] = _csv_cell(injection_dt)
            rows.append(row)
            ids.append(sid)
    return jsonify({"columns": header, "rows": rows, "sample_ids": ids})


# ===================================================================== #
#  API: Calibration (the gc1 instrument row)
# ===================================================================== #

@app.route("/api/calibration", methods=["GET"])
def api_calibration():
    """Return detected peaks, compound choices, and any saved assignments.

    Powers the manual calibration page. The calibration is the gc1
    instrument row's (``instruments.context``); ``peak_times``/
    ``carbon_numbers``/``boiling_points`` are retained for backward
    compatibility.
    """
    data, db = _hub()
    row = _gc1(db)
    try:
        ctx = instruments.context(row, settings_mod.load_settings(), data_dir=data)
        cal_path = distill.active_calibration_path(ctx, honour_env=False)
        if cal_path is None:
            return _error("No calibration CDF configured", 404)
        if not cal_path.is_file():
            return _error(f"Calibration file not found: {cal_path}", 404)

        # Sensitivity: query param overrides the saved one (default 50).
        try:
            sensitivity = float(
                request.args.get("sensitivity")
                or ctx.get("calibration_sensitivity", "50")
            )
        except (TypeError, ValueError):
            sensitivity = 50.0

        t, y = distill.gc_xy_from_cdf(cal_path)
        peak_times = distill.calibration_peak_times(cal_path, sensitivity)
        peak_intensity = (
            np.interp(peak_times, t, y).tolist() if peak_times else []
        )
        peaks = [
            {"index": i, "rt": round(float(rt), 4),
             "intensity": round(float(inten), 1)}
            for i, (rt, inten) in enumerate(zip(peak_times, peak_intensity))
        ]
        compounds = [
            {"carbon": c, "bp": bp}
            for c, bp in zip(distill.N_ALKANE_CARBON, distill.N_ALKANE_BP)
        ]
        amap = distill.parse_assignment_map(ctx.get("calibration_assignments", ""))
        saved = amap.get(distill._cal_key(cal_path), [])

        # Overlay arrays (peak_times/carbon_numbers/boiling_points) drive the
        # dashboard chromatogram markers: the saved manual assignments (the
        # same distill._assignment_pairs source calibration_ladder/anchors_for
        # use), else sequential auto-detection when nothing is assigned.
        cbp = distill.carbon_bp_map()
        assigned = distill._assignment_pairs(amap, cal_path)
        if assigned:
            overlay_times = [rt for rt, _ in assigned]
            overlay_carbons = [c for _, c in assigned]
            overlay_bp = [cbp.get(c) for c in overlay_carbons]
        else:
            n = min(len(peak_times), len(distill.N_ALKANE_CARBON))
            overlay_times = peak_times[:n]
            overlay_carbons = distill.N_ALKANE_CARBON[:n]
            overlay_bp = distill.N_ALKANE_BP[:n]

        # Downsample the trace for plotting (keep payload small).
        step = max(1, len(t) // 3000)
        return jsonify({
            "instrument": row["id"],
            "cdf_name": cal_path.name,
            "cdf_path": str(cal_path),
            "sensitivity": sensitivity,
            "trace": {"x": t[::step].tolist(), "y": y[::step].tolist()},
            "peaks": peaks,
            "compounds": compounds,
            "assignments": saved,
            # overlay keys — reflect manual assignments when present
            "peak_times": overlay_times,
            "carbon_numbers": overlay_carbons,
            "boiling_points": overlay_bp,
        })
    except Exception as exc:
        return _error(str(exc), 500)


@app.route("/api/calibration", methods=["POST"])
def api_calibration_save():
    """Persist manual peak→carbon assignments (and the sensitivity) on the gc1
    instrument row, then queue gc1's ``awaiting_calibration`` samples (only
    those: nothing final is reprocessed). Admin-gated (JSON body with the
    admin ``password``): the calibration decides every result."""
    body, err = _admin_json_body()
    if err:
        return err
    if not _check_admin(body):
        return _error("Incorrect password", 403)
    data, db = _hub()
    row = _gc1(db)
    try:
        ctx = instruments.context(row, settings_mod.load_settings(), data_dir=data)
        cal_path = distill.active_calibration_path(ctx, honour_env=False)
        if cal_path is None:
            return _error("No calibration CDF configured", 404)

        assignments = body.get("assignments")
        if not isinstance(assignments, list):
            return _error("Body must contain an 'assignments' list")

        valid_carbons = set(distill.N_ALKANE_CARBON)
        clean: list = []
        for entry in assignments:
            if not isinstance(entry, dict) or "rt" not in entry:
                return _error("Each assignment needs an 'rt'")
            rt = float(entry["rt"])
            if entry.get("ignore"):
                clean.append({"rt": rt, "ignore": True})
            elif "carbon" in entry and entry["carbon"] is not None:
                carbon = int(entry["carbon"])
                if carbon not in valid_carbons:
                    return _error(f"Unknown carbon number: {carbon}")
                clean.append({"rt": rt, "carbon": carbon})
            # peaks left unassigned (no carbon, not ignored) are simply omitted

        errors = distill.validate_assignments(clean)
        if errors:
            return _error(
                "Assigned carbons must increase with retention time: "
                + "; ".join(errors),
                400,
            )

        fields = {"id": row["id"], "calibration_assignments": json.dumps(clean) if clean else None}
        # Remember the sensitivity so reopening re-detects the same peaks.
        if body.get("sensitivity") is not None:
            try:
                fields["calibration_sensitivity"] = float(body["sensitivity"])
            except (TypeError, ValueError):
                pass
        store.instruments.upsert(fields, db=db)

        # Drop any cached calibration so the next build uses the new mapping.
        with distill._CAL_LOCK:
            distill._CAL_CACHE.clear()
        queued = pipeline.on_calibration_saved(row["id"], db=db)

        anchors = distill.anchors_for(
            distill.parse_assignment_map(distill.upsert_assignments("", cal_path, clean)),
            cal_path,
        )
        return jsonify({
            "ok": True,
            "saved": len(clean),
            "anchors": 0 if anchors is None else int(anchors[0].size),
            "queued": queued,
        })
    except Exception as exc:
        return _error(str(exc), 500)


@app.route("/api/calibration/active", methods=["GET"])
def api_calibration_active():
    """Diagnostic: the calibration gc1 processes with (read-only). ``mode`` is
    ``manual`` when it is usable (a CDF and at least two assignment pairs) and
    ``unusable`` otherwise, with the reason in ``problem``; the hub never
    falls back to auto-detection."""
    data, db = _hub()
    row = _gc1(db)
    try:
        ctx = instruments.context(row, settings_mod.load_settings(), data_dir=data)
        resolved = distill.active_calibration_path(ctx, honour_env=False)
        cal_path = resolved if resolved is not None else Path("")
        amap = distill.parse_assignment_map(ctx.get("calibration_assignments", ""))
        key = distill._cal_key(cal_path)
        saved = amap.get(key, [])
        carbon_count = sum(
            1 for e in saved
            if isinstance(e, dict) and e.get("carbon") is not None
        )
        problem = instruments.calibration_problem(ctx)
        anchors = distill.anchors_for(amap, cal_path)
        out = {
            "instrument": row["id"],
            "calibration_cdf": str(resolved) if resolved is not None else "",
            "cal_key": key,
            "assignment_map_keys": list(amap.keys()),
            "key_present_in_map": key in amap,
            "saved_carbon_assignments": carbon_count,
            "mode": "manual" if problem is None else "unusable",
            "problem": problem,
        }
        if anchors is not None:
            rt, bp = anchors
            out["anchor_count"] = int(rt.size)
            out["anchors_preview"] = [
                [round(float(r), 4), round(float(b), 1)]
                for r, b in zip(rt[:8], bp[:8])
            ]
        return jsonify(out)
    except Exception as exc:
        return _error(str(exc), 500)


# ===================================================================== #
#  API: Reprocess (pipeline jobs, by sample_id)
# ===================================================================== #

def _resolve_lab_query(query: str, instrument_id: Optional[str], db) -> dict:
    """Expand a Re-process query (IDs, lists, integer ranges) against one
    instrument's lab IDs. ``{matched, missing, sample_ids, instrument}``:
    ``sample_ids`` holds the latest injection of each matched lab ID.
    ``ValueError`` (400) without an instrument or for a bad query;
    ``SampleNotFound`` (404) for an unknown instrument."""
    inst = (instrument_id or "").strip()
    if not inst:
        raise ValueError("A lab-ID selection needs an instrument")
    if store.instruments.get(inst, db=db) is None:
        raise SampleNotFound(f"Unknown instrument {inst!r}")
    tokens = reprocess_query.parse_reprocess_query(query)
    latest: dict[str, int] = {}
    with store.connection(db) as conn:
        for sid, lab in conn.execute(
                "SELECT id, lab_id FROM samples WHERE instrument_id=? ORDER BY injection_dt, id",
                (inst,)):
            latest[lab] = sid           # later injections win
    result = reprocess_query.resolve_query(tokens, list(latest))
    result["sample_ids"] = [latest[name] for name in result["matched"]]
    result["instrument"] = inst
    return result


@app.route("/api/reprocess", methods=["POST"])
def api_reprocess():
    """Queue reprocess jobs (``pipeline.request_reprocess``) by ``sample_ids``,
    or by ``{query, instrument}`` (a lab-ID selection must name its
    instrument). The recorded blank and corrections are kept (D5) unless
    ``use_current_blank``/``use_current_corrections``. Result-only samples
    are refused; an unknown id fails the whole request (404)."""
    _data, db = _hub()
    body = request.get_json(silent=True) or {}
    # ``missing`` = Lab IDs the user asked for (e.g. inside a typed range) that
    # had no matching sample. Surface them in the persistent notification tray.
    missing = [str(m) for m in body.get("missing", []) if str(m).strip()]
    try:
        if body.get("query") is not None:
            resolved = _resolve_lab_query(str(body.get("query") or ""), body.get("instrument"), db)
            ids = resolved["sample_ids"]
            missing += [m for m in resolved["missing"] if m not in missing]
        else:
            ids = _sample_ids(body.get("sample_ids"))
    except ValueError as exc:
        return _error(str(exc))
    if missing:
        preview = ", ".join(missing[:50]) + ("…" if len(missing) > 50 else "")
        notifications_mod.get_store().add(
            "warning",
            f"Re-process: {len(missing)} Lab ID(s) not found and skipped: {preview}",
        )
    if not ids:
        if missing:
            return jsonify({"status": "no-match", "missing": missing})
        return _error("No samples provided (sample_ids)")
    samples = [_sample_or_404(sid, db) for sid in ids]

    queued, job_ids, refused = [], [], []
    for s in samples:
        if not s["cdf_path"]:
            refused.append({"sample_id": s["id"], "error": "a result-only sample has no CDF to reprocess"})
            continue
        job_ids.append(pipeline.request_reprocess(
            s["id"], by=_who(), use_current_blank=bool(body.get("use_current_blank")),
            use_current_corrections=bool(body.get("use_current_corrections")), db=db))
        queued.append(s["id"])
    return jsonify({"status": "queued", "count": len(queued), "sample_ids": queued,
                    "job_ids": job_ids, "refused": refused})


@app.route("/api/reprocess/status", methods=["GET"])
def api_reprocess_status():
    """Progress of the reprocess of ``?sample_ids=1,2,3`` for the toast:
    ``pending`` = their queued/running process jobs; once none is left the
    phase is ``done`` with ``processed`` (final, last run succeeded) and
    ``errors``. Without ids: ``idle`` (even with no store yet: it is polled)."""
    try:
        ids = _sample_ids(_list_arg(request.args.get("sample_ids")))
    except ValueError as exc:
        return _error(str(exc))
    if not ids:
        return jsonify({"phase": "idle", "total": 0, "processed": 0, "errors": 0,
                        "pending": 0, "samples": []})
    _data, db = _hub()
    samples = [_sample_or_404(sid, db) for sid in ids]
    busy = {j["sample_id"] for state in ("queued", "running")
            for j in store.jobs.list(state=state, kind=pipeline.PROCESS, db=db)}
    pending = processed = errors = 0
    out = []
    for s in samples:
        failed = s["status"] == "error" or (s["error"] or "").startswith("last reprocess failed")
        if s["id"] in busy:
            pending += 1
        elif failed:
            errors += 1
        elif s["status"] == "final":
            processed += 1
        out.append({"sample_id": s["id"], "status": s["status"],
                    "current_revision": s["current_revision"], "error": s["error"]})
    return jsonify({"phase": "processing" if pending else "done", "total": len(samples),
                    "processed": processed, "errors": errors, "pending": pending,
                    "samples": out})


@app.route("/api/reprocess/preview", methods=["POST"])
def api_reprocess_preview():
    """Expand a reprocess query (single IDs, lists, integer ranges) against one
    instrument's samples and return which lab IDs match (with the sample ids
    that would be queued) and which are missing, so the modal can preview
    before the user confirms. ``instrument`` is required."""
    _data, db = _hub()
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(_resolve_lab_query(str(body.get("query") or ""), body.get("instrument"), db))
    except ValueError as exc:
        return _error(str(exc))


# ── Persistent system-notification tray ──────────────────────────────
@app.route("/api/notifications", methods=["GET"])
def api_notifications():
    return jsonify(notifications_mod.get_store().list_all())


@app.route("/api/notifications/<notif_id>/dismiss", methods=["POST"])
def api_notifications_dismiss(notif_id: str):
    removed = notifications_mod.get_store().dismiss(notif_id)
    return jsonify({"status": "ok", "removed": removed})


@app.route("/api/notifications/dismiss-all", methods=["POST"])
def api_notifications_dismiss_all():
    count = notifications_mod.get_store().dismiss_all()
    return jsonify({"status": "ok", "removed": count})


# ===================================================================== #
#  API: Server restart
# ===================================================================== #

@app.route("/api/restart", methods=["POST"])
def api_restart():
    """Restart the server — installing the staged release when the updater
    has a newer healthy one. ``{"dry_run": true}`` only reports what a
    restart would do (the UI labels its button with it).

    Returns ``{"mode": "switch"|"restart", "tag": <tag or null>, "pid"}``;
    the browser polls /healthz until ``pid`` changes."""
    body = request.get_json(silent=True) or {}
    if body.get("dry_run"):
        with _restart_lock:
            if _restart_claimed:
                mode, tag = _restart_decision
            else:
                mode, tag = restart_policy.decide(paths.data_dir(), version.APP_VERSION)
    else:
        wait = restart_policy.manual_restart_wait(restart_policy.restart_mode(),
                                                  time.time() - _server_start_time)
        if wait is not None:
            # Each exit under the updater spends one of its few starts per
            # 15 minutes; a process that just came up doesn't need another.
            return jsonify({"error": f"The server just restarted, try again in {wait} s"}), 409
        mode, tag = request_restart(request.remote_addr or "unknown")
    return jsonify({"mode": mode, "tag": tag, "pid": os.getpid()})


@app.route("/api/server-status", methods=["GET"])
def api_server_status():
    """Return server uptime and idle info for the UI."""
    now = time.time()
    with _last_activity_lock:
        idle = now - _last_activity
    return jsonify({
        "uptime_seconds": round(now - _server_start_time),
        "idle_seconds": round(idle),
        "auto_restart_hour": AUTO_RESTART_HOUR,
        "auto_restart_idle_threshold": AUTO_RESTART_IDLE_SECONDS,
    })


# ===================================================================== #
#  API: Comparison Standards
# ===================================================================== #

@app.route("/api/comparison-standards", methods=["GET"])
def api_comparison_standards():
    try:
        conf = settings_mod.load_settings()
        comp_dir = Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))
        if not comp_dir.is_dir():
            return jsonify([])
        files = []
        for fp in sorted(comp_dir.iterdir()):
            if fp.suffix.lower() == ".cdf" and fp.is_file():
                files.append({
                    "name": fp.stem,
                    "path": str(fp),
                })
        return jsonify(files)
    except Exception as exc:
        return _error(str(exc), 500)


@app.route("/api/comparison-standard", methods=["POST"])
def api_add_comparison_standard():
    body = request.get_json(force=True) or {}
    src = None
    if body.get("sample_id") is not None:
        # A sample from the list: its current revision's CDF.
        data, db = _hub()
        s = _sample_or_404(body["sample_id"], db)
        src = _revision_cdf(s, store.get_revision(s["id"], db=db) if s["current_revision"] else None,
                            data)
    try:
        source_path = str(body.get("source_path") or src or "").strip()
        name = str(body.get("name") or "").strip()
        problem = _standard_name_problem(name)
        if problem:
            return _error(problem)
        if not source_path:
            return _error("source_path (or sample_id) and name are required")

        src = src or _safe_path(source_path)
        if not src.is_file():
            return _error(f"Source file not found: {source_path}", 404)

        conf = settings_mod.load_settings()
        comp_dir = Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))
        comp_dir.mkdir(parents=True, exist_ok=True)

        dest = comp_dir / f"{name}.CDF"
        shutil.copy2(str(src), str(dest))
        return jsonify({"status": "ok", "path": str(dest)})
    except Exception as exc:
        return _error(str(exc), 500)


@app.route("/api/comparison-standard/<name>", methods=["DELETE"])
def api_delete_comparison_standard(name: str):
    problem = _standard_name_problem(name)
    if problem:
        return _error(problem)
    try:
        conf = settings_mod.load_settings()
        comp_dir = Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))
        target = comp_dir / f"{name}.CDF"
        if not target.is_file():
            # Try lowercase
            target = comp_dir / f"{name}.cdf"
        if not target.is_file():
            return _error(f"Standard not found: {name}", 404)
        target.unlink()
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _error(str(exc), 500)


@app.route("/api/comparison-standard/rename", methods=["POST"])
def api_rename_comparison_standard():
    try:
        body = request.get_json(force=True)
        old_name = str(body.get("old_name") or "").strip()
        new_name = str(body.get("new_name") or "").strip()
        problem = _standard_name_problem(old_name) or _standard_name_problem(new_name)
        if problem:
            return _error(problem)

        conf = settings_mod.load_settings()
        comp_dir = Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))
        old_path = comp_dir / f"{old_name}.CDF"
        if not old_path.is_file():
            old_path = comp_dir / f"{old_name}.cdf"
        if not old_path.is_file():
            return _error(f"Standard not found: {old_name}", 404)

        new_path = comp_dir / f"{new_name}{old_path.suffix}"
        old_path.rename(new_path)
        return jsonify({"status": "ok", "path": str(new_path)})
    except Exception as exc:
        return _error(str(exc), 500)


# ===================================================================== #
#  API: Analysis
# ===================================================================== #

@app.route("/api/analysis", methods=["POST"])
def api_analysis():
    """Sample-vs-standard trend analysis for one sample (``sample_id``): the
    CDF of its current revision, labelled with that revision's calibration
    anchors."""
    body = request.get_json(force=True) or {}
    standard_name = str(body.get("standard_name") or "").strip()
    if body.get("sample_id") is None:
        return _error("sample_id is required")
    if not standard_name:
        return _error("standard_name is required")
    data, db = _hub()
    s = _sample_or_404(body["sample_id"], db)
    sample_p = _revision_cdf(s, store.get_revision(s["id"], db=db) if s["current_revision"] else None,
                             data)
    try:
        conf = settings_mod.load_settings()

        # Resolve standard CDF path
        std_path = _standard_path(conf, standard_name)
        if std_path is None:
            return _error(f"Standard not found: {standard_name}", 404)

        # Parameters with defaults from settings
        quantile = float(body.get("quantile", conf.get("analysis_quantile", 0.20)))
        window = int(body.get("window", conf.get("analysis_window", 301)))
        sigma = float(body.get("sigma", conf.get("analysis_sigma", 34.0)))
        thresh_marginal = float(body.get("thresh_marginal", conf.get("analysis_thresh_marginal", 100)))
        thresh_moderate = float(body.get("thresh_moderate", conf.get("analysis_thresh_moderate", 500)))
        thresh_significant = float(body.get("thresh_significant", conf.get("analysis_thresh_significant", 2000)))
        # Dynamic ranges (replaces hardcoded gas/oil)
        ranges_raw = body.get("ranges", [
            {"label": "Gas", "c_start": 5, "c_end": 11},
            {"label": "Oil", "c_start": 20, "c_end": 44},
        ])
        ranges = []
        for r in ranges_raw:
            ranges.append({
                "label": str(r.get("label", "Range")),
                "c_start": int(r.get("c_start", 5)),
                "c_end": int(r.get("c_end", 15)),
            })

        t_common, y_sample, y_std_interp = _load_pair(sample_p, std_path)

        # Calibration data for carbon mapping: the revision's anchors
        cal_times, cal_carbons = _revision_ladder(s, conf, db, data)

        # Trend difference + both detection channels (trend + raw-diff spikes)
        spike_min_width = float(conf.get(
            "analysis_spike_min_width_min",
            analysis_core.DEFAULT_SPIKE_MIN_WIDTH_MIN,
        ))
        pair = analyze_pair(
            t_common, y_sample, y_std_interp,
            thresh_marginal=thresh_marginal,
            thresh_moderate=thresh_moderate,
            thresh_significant=thresh_significant,
            quantile=quantile, window=window, sigma=sigma,
            cal_times=cal_times, cal_carbons=cal_carbons,
            spike_min_width_min=spike_min_width,
        )
        diff = pair["diff"]
        segments = pair["segments"]

        # Generate conclusion using dynamic ranges
        conclusion, bullets = generate_conclusion(
            segments, ranges, cal_times, cal_carbons,
            standard_name=standard_name,
        )

        x_max = float(body.get("x_max_min", conf.get("analysis_x_max_min", 7.0)))
        result = {
            "sample_id": s["id"],
            "trend": {
                "sample_x": t_common.tolist(),
                "sample_y": y_sample.tolist(),
                "standard_x": t_common.tolist(),
                "standard_y": y_std_interp.tolist(),
                "x_range": [0, x_max],
            },
            "diff": {
                "x": t_common.tolist(),
                "y": diff.tolist(),
                "x_range": [0, x_max],
            },
            "segments": segments,
            "conclusion": conclusion,
            "report": bullets,
            "cal_times": cal_times,
            "cal_carbons": cal_carbons,
        }
        return jsonify(result)
    except Exception as exc:
        LOGGER.exception("Analysis failed")
        return _error(str(exc), 500)


def _standard_path(conf: dict, standard_name: str) -> Optional[Path]:
    if _standard_name_problem(standard_name):
        return None
    comp_dir = _standards_dir(conf)
    for cand in (comp_dir / f"{standard_name}.CDF", comp_dir / f"{standard_name}.cdf"):
        if cand.is_file():
            return cand
    return None


def _load_pair(sample_p: Path, std_path: Path):
    """``(t, y_sample, y_standard)`` with the standard on the sample's time axis."""
    t_sample, y_sample = distill.gc_xy_from_cdf(sample_p)
    t_std, y_std = distill.gc_xy_from_cdf(std_path)
    if len(t_sample) != len(t_std) or not np.allclose(t_sample, t_std, atol=1e-6):
        y_std = np.interp(t_sample, t_std, y_std)
    return t_sample, y_sample, y_std


@app.route("/api/best-fit", methods=["POST"])
def api_best_fit():
    """Full fuel-type best-fit classification for one sample (``sample_id``):
    label, score, per-standard ranking and mix breakdown (the Analysis tab),
    plus ``recorded``: the best fit its current revision reported."""
    body = request.get_json(force=True) or {}
    if body.get("sample_id") is None:
        return _error("sample_id is required")
    data, db = _hub()
    s = _sample_or_404(body["sample_id"], db)
    rev = store.get_revision(s["id"], db=db) if s["current_revision"] else None
    p = _revision_cdf(s, rev, data)
    recorded = {"best_fit": rev["best_fit"] if rev else None,
                "fit_score": rev["fit_score"] if rev else None,
                "revision": rev["revision"] if rev else None}
    try:
        if fuel_fit is None:
            return _error("fuel_fit module not available", 500)
        conf = settings_mod.load_settings()
        res = _classify_cdf(str(p), conf)
        if res is None:
            return jsonify({"label": "", "best_standard": "", "score": 0.0,
                            "ranking": [], "mix": None, "recorded": recorded})
        fp = _cache_fingerprints(conf)["bestfit_fp"]
        if fp is not None:
            store.sample_cache.put(s["id"], bestfit_fingerprint=fp, best_fit=res["label"],
                                   fit_score=float(res["score"]), db=db)
        return jsonify(dict(res, recorded=recorded))
    except Exception as exc:
        LOGGER.exception("Best-fit classification failed")
        return _error(str(exc), 500)


# ===================================================================== #
#  API: Export
# ===================================================================== #

@app.route("/api/export-lims", methods=["POST"])
def api_export_lims():
    """Export to LIMS by ``sample_ids``: for each sample that passes the gate
    (final, and not backfill unless released) ``pipeline.export_to_lims``
    writes a new revision (``export-lims``, the current values copied, never
    recomputed) and its export row in one transaction. Samples that fail the
    gate are refused and listed; 409 if none was exported. Any unknown id
    fails the whole request (404) before anything is written."""
    data, db = _hub()
    body = request.get_json(silent=True) or {}
    try:
        ids = _sample_ids(body.get("sample_ids"))
    except ValueError as exc:
        return _error(str(exc))
    if not ids:
        return _error("No samples provided (sample_ids)")
    for sid in ids:
        _sample_or_404(sid, db)
    exported, refused = [], []
    for sid in ids:
        try:
            r = pipeline.export_to_lims(sid, by=_who(), db=db, data_dir=data)
            exported.append({"sample_id": sid, "revision": r["revision"], "seq": r["seq"]})
        except pipeline.NotExportable as exc:
            refused.append({"sample_id": sid, "error": str(exc)})
    if exported:
        _wake_exports()
    if refused:
        notifications_mod.get_store().add(
            "warning", f"Export to LIMS: {len(refused)} of {len(ids)} sample(s) refused — "
                       f"{refused[0]['error']}")
    return jsonify({"exported": exported, "refused": refused}), (200 if exported else 409)


@app.route("/api/export-pdf", methods=["POST"])
def api_export_pdf():
    body = request.get_json(force=True) or {}
    if body.get("sample_id") is None:
        return _error("sample_id is required")
    data, db = _hub()
    s = _sample_or_404(body["sample_id"], db)
    p = _revision_cdf(s, store.get_revision(s["id"], db=db) if s["current_revision"] else None, data)
    try:
        if go is None or pio is None:
            return _error("Plotly/kaleido not installed for PDF generation", 500)

        pdf_bytes = _generate_chromatogram_pdf(p, title=_sample_title(s))
        filename = f"{_safe_filename(s['lab_id'])}_chromatogram.pdf"

        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as exc:
        return _error(str(exc), 500)


def _sample_title(s: dict) -> str:
    return f"{s['lab_id']} - {str(s['injection_dt'])[:16]}"


@app.route("/api/export-comparison", methods=["POST"])
def api_export_comparison():
    body = request.get_json(force=True) or {}
    try:
        ids = _sample_ids(body.get("sample_ids"))
    except ValueError as exc:
        return _error(str(exc))
    if not ids:
        return _error("sample_ids is required")
    data, db = _hub()
    samples = [_sample_or_404(sid, db) for sid in ids]
    try:
        if go is None or pio is None:
            return _error("Plotly/kaleido not installed", 500)

        conf = settings_mod.load_settings()
        comp_dir = _standards_dir(conf)
        standard_paths = []
        if comp_dir.is_dir():
            for fp in comp_dir.iterdir():
                if fp.suffix.lower() == ".cdf" and fp.is_file():
                    standard_paths.append(fp)

        export_dir = Path(conf.get("export_folder", str(paths.default_export_dir())))
        export_dir.mkdir(parents=True, exist_ok=True)

        generated_files: list[str] = []
        for s in samples:
            try:
                p = _revision_cdf(s, store.get_revision(s["id"], db=db) if s["current_revision"]
                                  else None, data)
            except SampleNotFound:
                continue
            sample_name = s["lab_id"]
            html_content = _generate_comparison_html(p, standard_paths, sample_name=sample_name)

            out_file = export_dir / f"{_safe_filename(sample_name)}_comparison.html"
            out_file.write_text(html_content, encoding="utf-8")
            generated_files.append(str(out_file))

            # Also generate PDF for each
            try:
                fig = go.Figure()
                t_s, y_s = distill.gc_xy_from_cdf(p)
                fig.add_trace(go.Scatter(x=t_s.tolist(), y=y_s.tolist(), mode="lines", name=sample_name))
                for std_p in standard_paths:
                    t_st, y_st = distill.gc_xy_from_cdf(std_p)
                    std_nm, _ = distill.cdf_metadata(std_p)
                    fig.add_trace(go.Scatter(x=t_st.tolist(), y=y_st.tolist(), mode="lines", name=std_nm))
                fig.update_layout(
                    title=f"Comparison: {sample_name}",
                    xaxis_title="Time (min)", yaxis_title="Intensity",
                    template="plotly_white",
                )
                pdf_bytes = pio.to_image(fig, format="pdf", width=1200, height=600)
                pdf_file = export_dir / f"{_safe_filename(sample_name)}_comparison.pdf"
                pdf_file.write_bytes(pdf_bytes)
                generated_files.append(str(pdf_file))
            except Exception as exc:
                LOGGER.warning("PDF generation failed for %s: %s", sample_name, exc)

        return jsonify({"status": "ok", "files": generated_files})
    except Exception as exc:
        return _error(str(exc), 500)


def _run_export_analysis(sample: dict, params: dict, conf: dict, data: Path, db):
    """Shared analysis pass for the report export routes.

    Resolves the standard, runs both detection channels (trend + spike, same
    as ``/api/analysis``) on the sample's current-revision CDF, and returns
    ``(analysis_result, ranges, ladder)`` ready for
    ``_generate_analysis_report_pdf``; the ladder is the revision's
    calibration anchors. Raises ``ValueError`` with a user-facing message on
    a missing standard, ``SampleNotFound`` on a missing CDF.
    """
    standard_name = str(params.get("standard_name") or "").strip()
    if not standard_name:
        raise ValueError("standard_name is required")
    std_path = _standard_path(conf, standard_name)
    if std_path is None:
        raise ValueError(f"Standard not found: {standard_name}")
    sample_p = _revision_cdf(sample, store.get_revision(sample["id"], db=db)
                             if sample["current_revision"] else None, data)

    quantile = float(params.get("quantile", conf.get("analysis_quantile", 0.20)))
    window = int(params.get("window", conf.get("analysis_window", 301)))
    sigma = float(params.get("sigma", conf.get("analysis_sigma", 34.0)))
    thresh_marginal = float(params.get("thresh_marginal", conf.get("analysis_thresh_marginal", 100)))
    thresh_moderate = float(params.get("thresh_moderate", conf.get("analysis_thresh_moderate", 500)))
    thresh_significant = float(params.get("thresh_significant", conf.get("analysis_thresh_significant", 2000)))
    ranges = analysis_core.resolve_report_ranges(params.get("ranges"), conf)

    t_common, y_sample, y_std_interp = _load_pair(sample_p, std_path)

    cal_times, cal_carbons = _revision_ladder(sample, conf, db, data)

    spike_min_width = float(conf.get(
        "analysis_spike_min_width_min",
        analysis_core.DEFAULT_SPIKE_MIN_WIDTH_MIN,
    ))
    pair = analyze_pair(
        t_common, y_sample, y_std_interp,
        thresh_marginal=thresh_marginal,
        thresh_moderate=thresh_moderate,
        thresh_significant=thresh_significant,
        quantile=quantile, window=window, sigma=sigma,
        cal_times=cal_times, cal_carbons=cal_carbons,
        spike_min_width_min=spike_min_width,
    )
    diff = pair["diff"]
    segments = pair["segments"]

    conclusion_text, bullets_text = generate_conclusion(
        segments, ranges, cal_times, cal_carbons,
        standard_name=standard_name,
    )

    analysis_result = {
        "sample_trend": {"x": t_common.tolist(), "y": pair["trend_sample"].tolist()},
        "std_trend": {"x": t_common.tolist(), "y": pair["trend_std"].tolist()},
        "sample_raw": {"x": t_common.tolist(), "y": y_sample.tolist()},
        "std_raw": {"x": t_common.tolist(), "y": y_std_interp.tolist()},
        "difference": {"x": t_common.tolist(), "y": diff.tolist()},
        "segments": segments,
        "conclusion": params.get("conclusion") or conclusion_text,
        "bullets": params.get("bullets") or bullets_text,
    }
    return analysis_result, ranges, (cal_times, cal_carbons)


@app.route("/api/export-analysis-report", methods=["POST"])
def api_export_analysis_report():
    body = request.get_json(force=True) or {}
    if body.get("sample_id") is None:
        return _error("sample_id is required")
    data, db = _hub()
    s = _sample_or_404(body["sample_id"], db)
    try:
        if go is None or pio is None:
            return _error("Plotly/kaleido not installed", 500)

        conf = settings_mod.load_settings()
        try:
            analysis_result, ranges, ladder = _run_export_analysis(s, body, conf, data, db)
        except ValueError as exc:
            return _error(str(exc), 404 if "not found" in str(exc) else 400)

        params = dict(body, lab_id=s["lab_id"])
        report_bytes = _generate_analysis_report_pdf(params, analysis_result, ranges=ranges,
                                                     ladder=ladder)

        doc_name = body.get("doc_name", "analysis_report")
        filename = f"{_safe_filename(s['lab_id'])}_{_safe_filename(doc_name)}.pdf"

        return send_file(
            io.BytesIO(report_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
    except SampleNotFound:
        raise
    except Exception as exc:
        LOGGER.exception("Analysis report generation failed")
        return _error(str(exc), 500)


@app.route("/api/export-analysis-reports-zip", methods=["POST"])
def api_export_analysis_reports_zip():
    """Generate multiple analysis report PDFs (items by ``sample_id``) and
    return them in a single ZIP. Unknown samples and missing standards are
    skipped."""
    body = request.get_json(force=True) or {}
    items = body.get("items", [])
    if not items:
        return _error("items list is required")
    data, db = _hub()
    try:
        if go is None or pio is None:
            return _error("Plotly/kaleido not installed", 500)

        conf = settings_mod.load_settings()

        buf = io.BytesIO()
        written = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in items:
                try:
                    s = _sample_or_404(item.get("sample_id"), db)
                    analysis_result, ranges, ladder = _run_export_analysis(s, item, conf, data, db)
                except (ValueError, TypeError, SampleNotFound) as exc:
                    LOGGER.warning("Skipping ZIP item: %s", exc)
                    continue

                params = dict(item, lab_id=s["lab_id"])
                report_bytes = _generate_analysis_report_pdf(params, analysis_result, ranges=ranges,
                                                             ladder=ladder)
                doc_name = item.get("doc_name", "analysis_report")
                filename = f"{_safe_filename(s['lab_id'])}_{_safe_filename(doc_name)}.pdf"
                zf.writestr(filename, report_bytes)
                written += 1

        if not written:
            return _error(f"All {len(items)} report(s) were skipped (unknown sample, "
                          "no CDF or standard not found); see the log", 409)
        buf.seek(0)
        return send_file(
            buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name="analysis_reports.zip",
        )
    except Exception as exc:
        LOGGER.exception("Batch analysis report ZIP generation failed")
        return _error(str(exc), 500)


# ===================================================================== #
#  API: QBench Upload
# ===================================================================== #

_QBENCH_CREDS_FILE = r"\\ASAPServer\Labsharedrive\ASAP Lab Results\qbenchlogin.txt"


def _load_qbench_credentials() -> tuple[str, str]:
    """Load QBench username/password from the shared credentials file."""
    try:
        with open(_QBENCH_CREDS_FILE, "r") as fh:
            lines = fh.readlines()
        if len(lines) >= 2:
            return lines[0].strip(), lines[1].strip()
    except Exception as exc:
        LOGGER.warning("Could not read QBench credentials: %s", exc)
    return "", ""


def _save_qbench_credentials(username: str, password: str) -> None:
    """Persist credentials back to the shared file (called on first successful upload)."""
    try:
        with open(_QBENCH_CREDS_FILE, "w") as fh:
            fh.write(f"{username}\n{password}\n")
        LOGGER.info("QBench credentials saved to %s", _QBENCH_CREDS_FILE)
    except Exception as exc:
        LOGGER.warning("Could not save QBench credentials: %s", exc)


@app.route("/api/qbench-credentials", methods=["GET"])
def api_qbench_credentials():
    """Return saved QBench credentials (auto-fill the upload modal)."""
    u, p = _load_qbench_credentials()
    return jsonify({"username": u, "has_password": bool(p)})


@app.route("/api/qbench-upload", methods=["POST"])
def api_qbench_upload():
    """Queue analysis-report uploads to QBench by ``sample_id``. The server
    resolves each sample's lab ID and CDF and builds the PDF itself (a
    client ``pdf_path``/``sample_path`` is ignored). Every sample must pass
    the export gate (final; backfill only once released), checked here and
    again just before its upload: any refusal refuses the whole request (409)
    and nothing is queued. A successful upload records ``qbench_revision``
    (the revision the PDF was built from) and ``qbench_uploaded_at``."""
    body = request.get_json(force=True) or {}
    data, db = _hub()
    new_queue, refused = [], []
    for item in body.get("queue") or []:
        if not isinstance(item, dict):
            return _error("queue items must be objects {sample_id, standard_name, ...}")
        s = _sample_or_404(item.get("sample_id"), db)
        sid = s["id"]
        if not s["cdf_path"]:
            refused.append({"sample_id": sid, "lab_id": s["lab_id"],
                            "error": "a result-only sample has no CDF to build the report from"})
        elif not store.samples.is_gated(sid, db=db):
            refused.append({"sample_id": sid, "lab_id": s["lab_id"], "error": _gate_reason(s)})
        clean = {k: v for k, v in item.items() if k not in ("pdf_path", "sample_path", "lab_id")}
        new_queue.append(dict(clean, sample_id=sid, lab_id=s["lab_id"]))
    if not new_queue:
        return _error("queue is required (list of {sample_id, standard_name})")
    if refused:
        return jsonify({"error": f"{len(refused)} sample(s) can't be uploaded to QBench",
                        "refused": refused}), 409
    if qbench_pdf_uploader is None:
        return _error("qbench_pdf_uploader module not available", 500)

    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    client_id = body.get("client_id", "").strip() or None
    client_secret = body.get("client_secret", "").strip() or None

    # Auto-load credentials from shared file if not provided by the user
    if not username or not password:
        auto_u, auto_p = _load_qbench_credentials()
        if not username:
            username = auto_u
        if not password:
            password = auto_p

    global _upload_thread

    # ── If the upload thread is already running, APPEND items ─────────
    if _upload_thread and _upload_thread.is_alive():
        with _upload_items_lock:
            base_idx = len(_upload_items)
            _upload_items.extend(new_queue)
            for i, item in enumerate(new_queue):
                _upload_item_status.append({
                    "t": "item", "idx": base_idx + i,
                    "lab_id": item.get("lab_id", "?"),
                    "status": "waiting", "step": 0, "steps": 1, "msg": "Waiting",
                })
        _upload_new_items.set()  # wake the thread
        # Tell all SSE clients about the new items
        with _upload_items_lock:
            total = len(_upload_items)
        _publish_json(_upload_subscribers, _upload_sub_lock, {
            "t": "items_added", "base_idx": base_idx, "count": len(new_queue),
            "total": total,
            "items": [{"idx": base_idx + i, "lab_id": it.get("lab_id", "?")}
                      for i, it in enumerate(new_queue)],
        })
        return jsonify({"status": "appended", "count": len(new_queue),
                        "base_idx": base_idx, "total": total})

    # ── Fresh upload ──────────────────────────────────────────────────
    _upload_stop.clear()
    _creds_needed.clear()
    _creds_ready.clear()
    _upload_new_items.clear()
    with _creds_lock:
        _creds_new.clear()
    with _upload_items_lock:
        _upload_items.clear()
        _upload_item_status.clear()
        _upload_skipped.clear()
        _upload_items.extend(new_queue)
        for i, item in enumerate(new_queue):
            _upload_item_status.append({
                "t": "item", "idx": i,
                "lab_id": item.get("lab_id", "?"),
                "status": "waiting", "step": 0, "steps": 1, "msg": "Waiting",
            })
    _upload_credentials.update({
        "username": username, "password": password,
        "client_id": client_id, "client_secret": client_secret,
    })

    hub_data, hub_db = data, db
    item_revs: dict[int, Optional[int]] = {}   # queue index -> revision its PDF was built from

    def _do_upload():
      login_fail_count = 0

      try:
        steps_per = getattr(qbench_pdf_uploader, "STEPS_PER_SAMPLE", 10)
        ok_count = 0
        fail_count = 0
        skip_count = 0
        _creds_saved = False

        def _get_total():
            with _upload_items_lock:
                return len(_upload_items)

        def _emit_item(idx, lab_id, status, step=0, msg=""):
            total = _get_total()
            evt = {
                "t": "item", "idx": idx, "total": total,
                "lab_id": lab_id, "status": status,
                "step": step, "steps": steps_per + 2, "msg": msg,
            }
            with _upload_items_lock:
                if idx < len(_upload_item_status):
                    _upload_item_status[idx] = evt
            print(f"[UPLOAD] [{idx+1}/{total}] {lab_id}: {status} — {msg}", flush=True)
            try:
                _publish_json(_upload_subscribers, _upload_sub_lock, evt)
            except Exception as pub_exc:
                print(f"[UPLOAD] SSE publish error: {pub_exc}", flush=True)

        def _emit_overall(status, msg=""):
            total = _get_total()
            print(f"[UPLOAD] === {status}: {msg} (ok={ok_count} fail={fail_count} skip={skip_count}) ===", flush=True)
            try:
                _publish_json(_upload_subscribers, _upload_sub_lock, {
                    "t": "overall", "status": status, "msg": msg,
                    "ok": ok_count, "fail": fail_count, "total": total,
                })
            except Exception as pub_exc:
                print(f"[UPLOAD] SSE publish error: {pub_exc}", flush=True)

        with _upload_items_lock:
            total = len(_upload_items)
        print("=" * 60, flush=True)
        print(f"[UPLOAD] Upload thread started — {total} item(s)", flush=True)
        print("=" * 60, flush=True)

        _emit_overall("started", f"Uploading {total} item(s)")

        conf = settings_mod.load_settings()
        export_dir = Path(conf.get("export_folder", str(paths.default_export_dir())))
        export_dir.mkdir(parents=True, exist_ok=True)

        # Prime ChromeDriver once
        try:
            qbench_pdf_uploader.prime_chromedriver()
            _emit_overall("primed", "ChromeDriver ready")
        except Exception as exc:
            _emit_overall("error", f"ChromeDriver failed: {exc}")

        processed_up_to = 0
        while True:
            with _upload_items_lock:
                total = len(_upload_items)

            if processed_up_to >= total:
                # Wait briefly for new items to be appended
                _upload_new_items.clear()
                got_new = _upload_new_items.wait(timeout=3)
                with _upload_items_lock:
                    total = len(_upload_items)
                if processed_up_to >= total:
                    break  # No new items arrived — done

            for idx in range(processed_up_to, total):
                if _upload_stop.is_set():
                    _emit_overall("cancelled", "Upload cancelled")
                    return  # exit thread

                # ── Skip check ────────────────────────────────────────
                if idx in _upload_skipped:
                    with _upload_items_lock:
                        item = _upload_items[idx]
                    lab_id = item.get("lab_id", "?")
                    skip_count += 1
                    _emit_item(idx, lab_id, "skipped", msg="Skipped by user")
                    continue

                with _upload_items_lock:
                    item = _upload_items[idx]
                lab_id = item.get("lab_id", "").strip()
                if not lab_id:
                    fail_count += 1
                    _emit_item(idx, "?", "error", msg="No lab_id")
                    continue

                pdf_path = ""
                sid = item.get("sample_id")

                # ── Step 0: Generate the report from the stored sample ─
                _emit_item(idx, lab_id, "generating", step=0, msg="Generating report...")
                # The gate again: the sample may have changed since it was queued.
                sample_row = store.samples.get(sid, db=hub_db)
                if sample_row is None or not store.samples.is_gated(sid, db=hub_db):
                    fail_count += 1
                    _emit_item(idx, lab_id, "error", msg="Not exportable: " + (
                        _gate_reason(sample_row) if sample_row else "sample no longer exists"))
                    continue
                rev_no = sample_row["current_revision"]
                item_revs[idx] = rev_no
                standard_name = item.get("standard_name", "").strip()
                if not standard_name:
                    fail_count += 1
                    _emit_item(idx, lab_id, "error", msg="No standard")
                    continue
                try:
                    sample_p = _revision_cdf(sample_row, store.get_revision(sid, rev_no, db=hub_db),
                                             hub_data)
                    report_params = {
                        "lab_id": lab_id,
                        "doc_name": item.get("sample_name", "GC Analysis"),
                        "standard_name": standard_name,
                        "conclusion": item.get("conclusion", ""),
                        "bullets": item.get("bullets", ""),
                        "overlay_standards": item.get("overlay_standards", []),
                    }
                    comp_dir = Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))
                    std_path = comp_dir / f"{standard_name}.CDF"
                    if not std_path.is_file():
                        std_path = comp_dir / f"{standard_name}.cdf"
                    if not std_path.is_file():
                        fail_count += 1
                        _emit_item(idx, lab_id, "error", msg=f"Standard '{standard_name}' not found")
                        continue

                    q = float(conf.get("analysis_quantile", 0.20))
                    w = int(conf.get("analysis_window", 301))
                    sig = float(conf.get("analysis_sigma", 34.0))
                    tm = float(conf.get("analysis_thresh_marginal", 100))
                    tmod = float(conf.get("analysis_thresh_moderate", 500))
                    ts = float(conf.get("analysis_thresh_significant", 2000))

                    t_s, y_s = distill.gc_xy_from_cdf(sample_p)
                    t_st, y_st = distill.gc_xy_from_cdf(std_path)
                    if len(t_s) != len(t_st) or not np.allclose(t_s, t_st, atol=1e-6):
                        y_st = np.interp(t_s, t_st, y_st)
                    trend_s = compute_trend_line(t_s, y_s, q, w, sig)
                    trend_st = compute_trend_line(t_s, y_st, q, w, sig)
                    diff = trend_s - trend_st
                    cal_times, cal_carbons = _revision_ladder(sample_row, conf, hub_db, hub_data)
                    segs = detect_deviation_segments(diff, t_s, tm, tmod, ts, cal_times, cal_carbons) if cal_times else []

                    ar = {
                        "sample_trend": {"x": t_s.tolist(), "y": trend_s.tolist()},
                        "std_trend": {"x": t_s.tolist(), "y": trend_st.tolist()},
                        "sample_raw": {"x": t_s.tolist(), "y": y_s.tolist()},
                        "std_raw": {"x": t_s.tolist(), "y": y_st.tolist()},
                        "difference": {"x": t_s.tolist(), "y": diff.tolist()},
                        "segments": segs,
                        "conclusion": report_params.get("conclusion", ""),
                        "bullets": report_params.get("bullets", ""),
                    }
                    report_bytes = _generate_analysis_report_pdf(report_params, ar,
                                                                 ladder=(cal_times, cal_carbons))
                    safe_id = _safe_filename(lab_id)
                    out_file = export_dir / f"{safe_id}_analysis.pdf"
                    out_file.write_bytes(report_bytes)
                    pdf_path = str(out_file)
                except Exception as exc:
                    fail_count += 1
                    _emit_item(idx, lab_id, "error", msg=f"Report failed: {exc}")
                    LOGGER.exception("Report generation failed for %s", lab_id)
                    # Soft precheck: on very first item error, emit globally
                    if idx == 0:
                        _emit_overall("precheck_failed",
                                      f"First sample failed: {exc}")
                    continue

                _emit_item(idx, lab_id, "report_ok", step=1, msg="Report ready")

                # ── Steps 2+: Upload to QBench ────────────────────────
                _emit_item(idx, lab_id, "uploading", step=2, msg="Uploading to QBench...")
                _current_step = [2]
                username = _upload_credentials.get("username", "")
                password = _upload_credentials.get("password", "")
                client_id = _upload_credentials.get("client_id")
                client_secret = _upload_credentials.get("client_secret")

                def _step_cb(step_num, _idx=idx, _lid=lab_id):
                    _current_step[0] = 2 + step_num
                    _emit_item(_idx, _lid, "uploading", step=_current_step[0],
                               msg=f"Step {step_num + 1}/{steps_per}")

                def _progress_cb(msg, _idx=idx, _lid=lab_id):
                    _emit_item(_idx, _lid, "uploading", step=_current_step[0], msg=msg)

                try:
                    result = qbench_pdf_uploader.attach_pdf_to_sample(
                        lab_id, pdf_path,
                        headless=True,
                        username=username or None,
                        password=password or None,
                        client_id=client_id,
                        client_secret=client_secret,
                        progress_callback=_progress_cb,
                        step_callback=_step_cb,
                    )
                    if result:
                        ok_count += 1
                        login_fail_count = 0
                        _record_qbench_upload(item.get("sample_id"), item_revs.get(idx), hub_db)
                        _emit_item(idx, lab_id, "ok", step=steps_per + 2, msg="Uploaded")
                        if not _creds_saved and username and password:
                            _save_qbench_credentials(username, password)
                            _creds_saved = True
                    else:
                        fail_count += 1
                        _emit_item(idx, lab_id, "failed", msg="Upload returned false")
                        # Soft precheck: first item failure
                        if idx == 0 and ok_count == 0:
                            _emit_overall("precheck_failed",
                                          f"First sample upload failed for '{lab_id}'")
                except qbench_pdf_uploader.LoginFailedError as login_exc:
                    login_fail_count += 1
                    print(f"[UPLOAD] Login failure #{login_fail_count}: {login_exc}", flush=True)

                    # Soft precheck: first sample login failure → immediate prompt
                    if idx == 0 or login_fail_count >= 2:
                        _emit_item(idx, lab_id, "login_failed",
                                   msg=f"Login failed: {login_exc}")
                        if idx == 0:
                            _emit_overall("precheck_failed",
                                          f"Login failed on first sample: {login_exc}")
                        _emit_overall("credentials_needed",
                                      "Login failed — please re-enter your QBench credentials")
                        _creds_ready.clear()
                        _creds_needed.set()

                        print("[UPLOAD] Waiting for new credentials from user...", flush=True)
                        got_creds = _creds_ready.wait(timeout=300)
                        _creds_needed.clear()

                        if _upload_stop.is_set():
                            _emit_overall("cancelled", "Upload cancelled")
                            return

                        if got_creds:
                            with _creds_lock:
                                _upload_credentials["username"] = _creds_new.get("username", username)
                                _upload_credentials["password"] = _creds_new.get("password", password)
                            login_fail_count = 0
                            _creds_saved = False
                            _emit_overall("credentials_updated",
                                          "Credentials updated — retrying...")

                            _current_step[0] = 2
                            username = _upload_credentials["username"]
                            password = _upload_credentials["password"]
                            _emit_item(idx, lab_id, "uploading", step=2,
                                       msg="Retrying with new credentials...")
                            try:
                                result = qbench_pdf_uploader.attach_pdf_to_sample(
                                    lab_id, pdf_path,
                                    headless=True,
                                    username=username or None,
                                    password=password or None,
                                    client_id=client_id,
                                    client_secret=client_secret,
                                    progress_callback=_progress_cb,
                                    step_callback=_step_cb,
                                )
                                if result:
                                    ok_count += 1
                                    _record_qbench_upload(item.get("sample_id"),
                                                          item_revs.get(idx), hub_db)
                                    _emit_item(idx, lab_id, "ok",
                                               step=steps_per + 2, msg="Uploaded")
                                    if not _creds_saved and username and password:
                                        _save_qbench_credentials(username, password)
                                        _creds_saved = True
                                else:
                                    fail_count += 1
                                    _emit_item(idx, lab_id, "failed",
                                               msg="Upload returned false after retry")
                            except Exception as retry_exc:
                                fail_count += 1
                                _emit_item(idx, lab_id, "failed",
                                           msg=f"Retry failed: {retry_exc}")
                        else:
                            _emit_item(idx, lab_id, "failed",
                                       msg="No new credentials provided — aborting")
                            fail_count += 1
                            _emit_overall("allfailed",
                                          "Upload aborted: no credentials within 5 min")
                            return
                    else:
                        fail_count += 1
                        _emit_item(idx, lab_id, "failed",
                                   msg=f"Login failed: {login_exc}")
                except Exception as exc:
                    fail_count += 1
                    _emit_item(idx, lab_id, "failed", msg=str(exc))
                    # Soft precheck: first item error
                    if idx == 0 and ok_count == 0:
                        _emit_overall("precheck_failed",
                                      f"First sample error: {exc}")

            processed_up_to = total

        # Final summary
        total = _get_total()
        done_count = ok_count + fail_count + skip_count
        if ok_count == done_count - skip_count and fail_count == 0:
            _emit_overall("done", f"All {ok_count} uploaded successfully"
                          + (f" ({skip_count} skipped)" if skip_count else ""))
        elif ok_count == 0 and fail_count > 0:
            _emit_overall("allfailed", f"All {fail_count} failed"
                          + (f" ({skip_count} skipped)" if skip_count else ""))
        else:
            _emit_overall("partial", f"{ok_count} OK, {fail_count} failed"
                          + (f", {skip_count} skipped" if skip_count else ""))

      except Exception as fatal:
        print(f"[UPLOAD] FATAL ERROR in upload thread: {fatal}", flush=True)
        import traceback; traceback.print_exc()
        try:
            _publish_json(_upload_subscribers, _upload_sub_lock, {
                "t": "overall", "status": "allfailed",
                "msg": f"Fatal error: {fatal}", "ok": 0, "fail": 0, "total": 0,
            })
        except Exception:
            pass

    _upload_thread = threading.Thread(target=_do_upload, daemon=True)
    _upload_thread.start()
    return jsonify({"status": "started", "count": len(new_queue)})


@app.route("/api/qbench-upload/stream", methods=["GET"])
def api_qbench_upload_stream():
    return Response(
        stream_with_context(_sse_stream(_upload_subscribers, _upload_sub_lock)),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/qbench-upload-status", methods=["GET"])
def api_qbench_upload_status():
    """Return the current state of the upload queue (for modal re-open)."""
    with _upload_items_lock:
        return jsonify({
            "active": _upload_thread is not None and _upload_thread.is_alive(),
            "items": list(_upload_item_status),
            "skipped": list(_upload_skipped),
        })


@app.route("/api/qbench-skip-item", methods=["POST"])
def api_qbench_skip_item():
    """Mark a specific queue item to be skipped."""
    body = request.get_json(force=True)
    idx = body.get("idx")
    if idx is None:
        return _error("idx is required")
    _upload_skipped.add(int(idx))
    # Update the item status immediately so the UI reflects it
    with _upload_items_lock:
        if idx < len(_upload_item_status):
            lab_id = _upload_item_status[idx].get("lab_id", "?")
            cur = _upload_item_status[idx].get("status", "waiting")
        else:
            lab_id = "?"
            cur = "waiting"
    # Only mark as skipped if not already done/failed
    if cur not in ("ok", "failed", "error"):
        evt = {
            "t": "item", "idx": idx, "total": _get_upload_total(),
            "lab_id": lab_id, "status": "skipped",
            "step": 0, "steps": 1, "msg": "Skipped by user",
        }
        with _upload_items_lock:
            if idx < len(_upload_item_status):
                _upload_item_status[idx] = evt
        _publish_json(_upload_subscribers, _upload_sub_lock, evt)
    return jsonify({"status": "ok"})


def _get_upload_total() -> int:
    with _upload_items_lock:
        return len(_upload_items)


@app.route("/api/qbench-cancel", methods=["POST"])
def api_qbench_cancel():
    _upload_stop.set()
    _creds_ready.set()  # unblock credential wait so the thread can exit
    _upload_new_items.set()  # unblock the "wait for new items" sleep
    if qbench_pdf_uploader is not None:
        try:
            qbench_pdf_uploader.cancel_upload()
        except Exception:
            pass
    _publish_json(_upload_subscribers, _upload_sub_lock,
                  {"t": "overall", "status": "cancelled", "msg": "Upload cancelled",
                   "ok": 0, "fail": 0, "total": 0})
    return jsonify({"status": "cancel_requested"})


@app.route("/api/qbench-update-credentials", methods=["POST"])
def api_qbench_update_credentials():
    """Receive new credentials from the user after a login failure pause."""
    body = request.get_json(force=True)
    u = body.get("username", "").strip()
    p = body.get("password", "").strip()
    if not u or not p:
        return _error("Username and password are required")
    with _creds_lock:
        _creds_new["username"] = u
        _creds_new["password"] = p
    _creds_ready.set()   # unblock the upload thread
    return jsonify({"status": "ok"})


# QBench *API* (OAuth client) credentials, entered from Settings. Distinct
# from the Selenium web login above (/api/qbench-credentials, qbenchlogin.txt).
# The secret is never returned, logged, or echoed: describe() carries only the
# last four characters of the client id.

def _token_failure_message(exc: Exception) -> str:
    """A reason for a failed token request that cannot contain the secret or
    the signed assertion: only the exception class and the HTTP status."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if status is not None and 400 <= status < 500:
        return f"QBench rejected these credentials (HTTP {status})"
    if status is not None:
        return f"QBench could not check these credentials right now (HTTP {status})"
    if requests_exc is not None and isinstance(exc, requests_exc.Timeout):
        return "Could not reach QBench to check these credentials (timed out)"
    if requests_exc is not None and isinstance(exc, requests_exc.ConnectionError):
        return "Could not reach QBench to check these credentials (connection failed)"
    return f"QBench did not accept these credentials ({type(exc).__name__})"


@app.route("/api/qbench-api-credentials", methods=["GET"])
def api_qbench_api_credentials_get():
    return jsonify(qbench_secrets.describe())


@app.route("/api/qbench-api-credentials", methods=["POST"])
def api_qbench_api_credentials_post():
    """Test the submitted pair by requesting a token; save it only if QBench
    accepts it. Admin-gated."""
    body, err = _admin_json_body()
    if err:
        return err
    if not _check_admin(body):
        return _error("Incorrect password", 403)
    cid = body.get("client_id")
    sec = body.get("client_secret")
    cid = cid.strip() if isinstance(cid, str) else ""
    sec = sec.strip() if isinstance(sec, str) else ""
    if not cid or not sec:
        return _error("Client ID and Client Secret are both required", 400)
    if qbench_client is None:
        return _error("QBench client unavailable (jwt/requests not installed)", 500)
    try:
        # Bounded so a request thread is not held for a minute: short
        # (connect, read) timeout, its own rate limiter (never queued behind
        # an upload), and one token request (no clock-skew retry).
        probe = qbench_client.QBenchAPIClient(client_id=cid, client_secret=sec,
                                              timeout=(5, 10),  # (connect, read) s
                                              private_rate_limiter=True)
        probe.get_access_token(force=True, retry_skew=False)
    except Exception as exc:
        msg = _token_failure_message(exc)
        LOGGER.warning("QBench credential test failed: %s", msg)
        return _error(msg, 400)
    try:
        qbench_secrets.save_default(cid, sec)
    except Exception as exc:
        LOGGER.error("Could not save QBench API credentials: %s", type(exc).__name__)
        return _error(f"QBench accepted the credentials but saving them failed "
                      f"({type(exc).__name__}) at {qbench_secrets.describe()['store_path']}", 500)
    LOGGER.info("QBench API credentials updated from %s (client id ...%s)",
                request.remote_addr, cid[-4:])
    # QBenchAPIClient resolves the pair on construction and nothing holds a
    # long-lived client, so the next QBench call uses the new pair.
    return jsonify(qbench_secrets.describe())


# ===================================================================== #
#  API: Utility
# ===================================================================== #

@app.route("/api/browse", methods=["POST"])
def api_browse():
    """Open a native file/folder picker dialog (runs on the server machine).
    Uses tkinter which is available in standard Python."""
    body = request.get_json(force=True)
    browse_type = body.get("type", "dir")  # "dir" or "file"
    title = body.get("title", "Select")
    initial = body.get("initial", "")

    result = {"path": ""}
    err = [None]

    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            if browse_type == "dir":
                path = filedialog.askdirectory(title=title, initialdir=initial or None)
            else:
                path = filedialog.askopenfilename(title=title, initialdir=initial or None)
            root.destroy()
            result["path"] = path or ""
        except Exception as exc:
            err[0] = str(exc)

    # tkinter must run on the main thread on some OSes, but on Windows
    # it works fine from any thread. Run in a thread with a timeout.
    t = threading.Thread(target=_pick, daemon=True)
    t.start()
    t.join(timeout=120)  # 2 min timeout for user to pick

    if err[0]:
        return _error(f"Browse dialog failed: {err[0]}", 500)
    return jsonify(result)


@app.route("/api/open-folder", methods=["GET"])
def api_open_folder():
    folder = request.args.get("path", "").strip()
    if not folder:
        return _error("Missing 'path' query parameter")
    try:
        p = _safe_path(folder)
        if not p.is_dir():
            return _error(f"Not a directory: {folder}", 404)
        if platform.system() == "Windows":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _error(str(exc), 500)


# ===================================================================== #
#  Serve the SPA index
# ===================================================================== #

@app.route("/healthz")
def healthz():
    """Updater health contract (see coa-reviewer/RELEASING.md): 200 + status
    ok + version == tag. No auth, no outbound calls, not activity."""
    now = time.time()
    with _last_activity_lock:
        idle = now - _last_activity
        # Prune clients not seen in 5 minutes so _recent_clients doesn't grow
        # without bound over an uptime of weeks (scanners, monitors, anyone
        # who has since gone away).
        stale = [addr for addr, t in _recent_clients.items() if now - t >= 300]
        for addr in stale:
            del _recent_clients[addr]
        active = len(_recent_clients)
    return jsonify({"status": "ok", "version": version.APP_VERSION, "pid": os.getpid(),
                    "active_sessions": active, "idle_seconds": round(idle, 1)})


@app.route("/")
def index():
    from flask import render_template
    try:
        return render_template("index.html", app_version=version.APP_VERSION)
    except Exception:
        return (
            "<h1>GC Viewer &amp; Distillation Parser</h1>"
            "<p>API is running. Place <code>index.html</code> in <code>templates/</code>.</p>"
        )


@app.route("/calibration")
def calibration_page():
    from flask import render_template
    try:
        return render_template("calibration.html", app_version=version.APP_VERSION)
    except Exception:
        return (
            "<h1>Calibration</h1>"
            "<p>Place <code>calibration.html</code> in <code>templates/</code>.</p>"
        )


# ===================================================================== #
#  Startup
# ===================================================================== #

def _init_app() -> None:
    """Start the hub on a background thread (``_start_hub``) and the 3 AM
    auto-restart loop, so the server answers /healthz at once: the start-up
    may read the phase-1 corrections file on the share (seeding gc1), which
    can stall on an unreachable UNC path. Store routes answer 503 until the
    store exists."""
    threading.Thread(target=_start_hub, daemon=True, name="hub-start").start()
    threading.Thread(target=_auto_restart_loop, daemon=True, name="auto-restart").start()


def _start_hub() -> None:
    """``hub.start``: store migrate, gc1 bootstrap and corrections seed, the
    pipeline Worker, the export flusher, the nightly backup and job prune;
    then prime ``sample_cache``. A hub that cannot start (e.g. an unreadable
    database) is logged and notified, and the app still serves, so /healthz
    and the logs can say why."""
    global _hub_runtime
    try:
        _hub_runtime = hub.start(settings_mod.load_settings(), on_final=_on_sample_final)
        atexit.register(_stop_hub)
    except Exception as exc:
        LOGGER.exception("The hub did not start (processing and exports are stopped)")
        try:
            notifications_mod.get_store().add(
                "error", f"The GC hub did not start: {exc}. Nothing is processed or exported "
                         f"until it is fixed and the app restarted; see app.log.")
        except Exception:
            LOGGER.exception("Could not raise the start-up notification")
        return
    _prime_sample_cache()


def _stop_hub() -> None:
    """Stop the Worker, exporter and maintenance threads (best effort)."""
    rt = _hub_runtime
    if rt is not None:
        try:
            rt.stop(timeout=5.0)
        except Exception:
            LOGGER.exception("Could not stop the hub cleanly")


def _wake_exports() -> None:
    """A ledger row was written outside the Worker: flush it now."""
    rt = _hub_runtime
    if rt is not None:
        rt.wake_exports()


def _on_sample_final(sample_id: int) -> None:
    """Worker hook: a sample became final; compute its flags/best-fit off
    the request path."""
    _schedule_cache_refresh([sample_id])


CACHE_PRIME_LIMIT = 2000


def _prime_sample_cache() -> None:
    """At start, queue the newest samples with no ``sample_cache`` row (or
    one from other rules) so the first list after a start is warm."""
    try:
        _data, db = _hub()
        fps = _cache_fingerprints(settings_mod.load_settings())
        with store.connection(db) as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT s.id FROM samples s LEFT JOIN sample_cache c ON c.sample_id = s.id "
                "WHERE c.sample_id IS NULL OR c.rules_fingerprint IS NOT ? "
                "ORDER BY s.injection_dt DESC LIMIT ?", (fps["rules_fp"], CACHE_PRIME_LIMIT))]
        if ids:
            _schedule_cache_refresh(ids)
    except HubUnavailable:
        pass
    except Exception:
        LOGGER.exception("Could not prime sample_cache (non-fatal)")


if __name__ != "__main__":
    # Imported (in-process tests): start the background work as before.
    _init_app()


if __name__ == "__main__":
    # Port guard first, before any background work: Werkzeug sets
    # allow_reuse_address, so on Windows a second bind to a served port
    # succeeds silently and the duplicate runs forever serving nothing
    # (the 2026-07-31 incident) — while its watcher processes files alongside
    # the live instance. The wait also covers a just-respawned replacement
    # racing the old process's exit.
    if not supervisor.wait_until_free(GC_PORT, timeout=15.0):
        LOGGER.error("Port %d is still in use after 15s — cannot start. Exiting.", GC_PORT)
        _sys.exit(1)
    # Only now are we the serving process.
    _tidy_switch_files()
    _init_app()
    # debug=True would expose the Werkzeug interactive debugger — remote code
    # execution — on 0.0.0.0. Only an explicit --dev turns it on.
    app.run(host="0.0.0.0", port=GC_PORT, debug=ARGS.dev, threaded=True,
            use_reloader=False)  # reloader kills background threads on file changes
