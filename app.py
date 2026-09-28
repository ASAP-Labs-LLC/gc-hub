#!/usr/bin/env python3
"""
app.py -- Flask backend for the GC Viewer & Distillation Parser webapp.

This is a 1:1 web clone of the PyQt5 desktop application.  It exposes a
JSON REST API consumed by the single-page frontend and delegates all
heavy lifting to the existing backend modules (distill, looker, settings,
qbench_pdf_uploader).
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
# ── Per-instance identity ─────────────────────────────────────────────────
# Resolve this instance's port BEFORE importing settings: settings.CONFIG_PATH
# is derived from GC_PORT at import time, and distill/looker import settings.
# Publishing it back to the environment means every module — and every
# subprocess we spawn, including the daily auto-restart — agrees on the port.
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
# Only a direct launch (``__main__``, which includes run.pyw's runpy
# bootstrap whose sys.argv is ['-c']) parses the real argv: when app is
# imported (tests), sys.argv belongs to someone else.
import argparse

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

GC_PORT = instance.resolve_port()
os.environ["GC_PORT"] = str(GC_PORT)

import distill
import sample_flags
import settings as settings_mod

try:
    import fuel_fit
except Exception:  # pragma: no cover - scipy.optimize missing
    fuel_fit = None
import looker as looker_mod
import notifications as notifications_mod
import reprocess_query
import library_view

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

# Deployed (GC_DATA_DIR set): also log to DATA_DIR/app.log, rotating, so the
# updater-supervised process — which has no console anyone watches — leaves
# a trail. Legacy mode stays console-only, as before.
_LOG_FILE = paths.log_file()
if _LOG_FILE is not None:
    import logging.handlers

    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _fh = logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(_fh)

    # The health check runs against an empty data dir: create the default
    # folders up front so nothing downstream trips over their absence.
    for _d in (paths.default_processed_dir(), paths.default_export_dir()):
        try:
            _d.mkdir(parents=True, exist_ok=True)
        except OSError:
            LOGGER.exception("Could not create %s", _d)

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
import instruments_api  # noqa: E402  (2A2: the Instruments page)
app.register_blueprint(instruments_api.bp)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_looker: Optional[looker_mod.Looker] = None
_looker_lock = threading.Lock()

# SSE queues -- one per connected client
_scan_subscribers: list[queue.Queue] = []
_scan_sub_lock = threading.Lock()

_upload_subscribers: list[queue.Queue] = []
_upload_sub_lock = threading.Lock()

# Background-task handles
_scan_thread: Optional[threading.Thread] = None
_scan_stop = threading.Event()

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

# Background file watcher — queues new CDFs and processes in batches
_watcher_thread: Optional[threading.Thread] = None
_watcher_start_lock = threading.Lock()
_watcher_stop = threading.Event()
_scan_halt = threading.Event()   # stop processing (checked per-file, not per-batch)
SCAN_BATCH_SIZE = 50
WATCHER_POLL_SECONDS = 5  # how often to check for new files

# Backlog abandoned by a user Stop. The watcher loop used to clear _scan_halt at
# the top of every iteration and re-scan the whole backlog after WATCHER_POLL
# seconds — so "Stop" only paused for ~5s. Now a Stop records its not-yet-
# processed candidates here and the watcher excludes them, so the backlog stays
# stopped while genuinely NEW files (never seen, never suppressed) still process.
# Pressing "Scan & Parse" (/api/scan) clears this set to re-attack the backlog.
_suppressed_paths: set[str] = set()
_suppressed_lock = threading.Lock()


def _filter_candidates(all_cdfs, seen) -> list:
    """Discovered CDFs minus already-seen and user-suppressed paths.

    ``all_cdfs`` items may be Path or str; ``seen`` holds whatever the Looker
    stores. Suppression compares on str(path)."""
    with _suppressed_lock:
        suppressed = set(_suppressed_paths)
    return [fp for fp in all_cdfs
            if fp not in seen and str(fp) not in suppressed]


def _suppress_backlog(paths) -> None:
    """Record un-processed backlog paths so the watcher won't auto-resume them."""
    with _suppressed_lock:
        _suppressed_paths.update(str(p) for p in paths)

# ── Fast in-memory file list cache ────────────────────────────────────
# Built once at startup with os.scandir (much faster than Path.glob on
# network shares).  Updated incrementally as the watcher processes files.
_files_cache: list[dict] = []
_files_cache_lock = threading.Lock()
_files_cache_ready = threading.Event()  # signalled once first scan completes

# ── Activity tracking & auto-restart ─────────────────────────────────
_last_activity: float = time.time()
_last_activity_lock = threading.Lock()
_recent_clients: dict[str, float] = {}   # remote_addr -> last-seen time, for /healthz active_sessions
_server_start_time: float = time.time()
_auto_restart_done_today: str = ""          # date string e.g. "2026-05-07"
AUTO_RESTART_HOUR = restart_policy.AUTO_RESTART_HOUR   # 3 AM local time
AUTO_RESTART_IDLE_SECONDS = 600  # 10 minutes with no requests


def _rebuild_files_cache() -> list[dict]:
    """Build the sample list from the distillation CSV + processed CDF dir.

    Each row in the CSV becomes one entry: name = plain Lab ID, path = Source File.
    Entries are deduplicated by (Lab ID, InjectionDateTime) and sorted
    chronologically by injection datetime (oldest first).

    If the CSV is missing the ``Source File`` column (old header format) or
    any entry lacks a valid file path, the processed-CDF directory is scanned
    to resolve real paths.  If the CSV is empty/missing entirely, the
    processed-CDF directory is scanned as a complete fallback so the sample
    list is never empty when files actually exist on disk.
    """
    from datetime import datetime as _dt
    conf = settings_mod.load_settings()
    csv_path_str = conf.get("distill_output", str(paths.default_results_csv()))
    csv_path = Path(csv_path_str)
    proc_dir = Path(conf.get("processed_cdf_dir", str(paths.default_processed_dir())))
    files: list[dict] = []
    t0 = time.time()
    seen: set[tuple] = set()

    # ── Strategy 1: Read CSV (per-row error handling, lock-protected) ──
    try:
        if csv_path.is_file():
            with distill._CSV_LOCK:
                with csv_path.open("r", encoding="utf-8", newline="") as fh:
                    csv_rows = list(csv.DictReader(fh))
            for row in csv_rows:
                try:
                    lab_id = (row.get("Lab ID") or "").strip()
                    inj_dt_str = (row.get("InjectionDateTime") or "").strip()
                    src_file = (row.get("Source File") or "").strip()
                    if not lab_id:
                        continue
                    key = (lab_id, inj_dt_str)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        inj_dt = _dt.fromisoformat(inj_dt_str)
                        mtime = inj_dt.timestamp()
                    except Exception:
                        mtime = 0.0
                    files.append({
                        "name": lab_id,
                        "path": src_file if src_file else lab_id,
                        "mtime": mtime,
                        "inj_dt": inj_dt_str,
                    })
                except Exception as row_exc:
                    LOGGER.debug("Skipping bad CSV row: %s", row_exc)
    except Exception as exc:
        LOGGER.warning("File cache build from CSV failed: %s", exc)

    # ── Strategy 2: Resolve missing paths from processed-CDF dir ──────
    # Entries whose path == name have no real file path (old CSV format
    # without "Source File" column).  Try to find the actual CDF on disk.
    needs_fixup = any(f["path"] == f["name"] for f in files)
    if needs_fixup and proc_dir and proc_dir.is_dir():
        try:
            cdf_names: list[str] = [
                e.name for e in os.scandir(proc_dir)
                if e.is_file() and e.name.upper().endswith(".CDF")
            ]
            for f in files:
                if f["path"] != f["name"]:
                    continue  # already has a real path
                lab = f["name"]
                for cn in cdf_names:
                    # Processed filenames look like "{lab_id}_{MMDDYYYY}.CDF"
                    if cn.startswith(lab) or cn.startswith(
                        lab.replace(" ", "_")
                    ):
                        f["path"] = str(proc_dir / cn)
                        break
        except Exception as exc:
            LOGGER.warning("Processed-CDF path fixup failed: %s", exc)

    # ── Strategy 3: Full fallback — scan processed-CDF dir ────────────
    # If the CSV was empty, missing, or had zero usable rows, build the
    # list directly from the CDF files so the UI is never blank.
    if not files and proc_dir and proc_dir.is_dir():
        LOGGER.info("CSV empty/missing — falling back to processed-CDF scan")
        try:
            for entry in os.scandir(proc_dir):
                if not (entry.is_file() and entry.name.upper().endswith(".CDF")):
                    continue
                fp = proc_dir / entry.name
                try:
                    sample, inj_dt = distill.cdf_metadata(fp)
                    mtime = inj_dt.timestamp()
                    inj_dt_str = inj_dt.isoformat(sep=" ")
                except Exception:
                    sample = entry.name.rsplit(".", 1)[0]
                    mtime = entry.stat().st_mtime
                    inj_dt_str = ""
                key = (sample, inj_dt_str)
                if key in seen:
                    continue
                seen.add(key)
                files.append({
                    "name": sample,
                    "path": str(fp),
                    "mtime": mtime,
                    "inj_dt": inj_dt_str,
                })
        except Exception as exc:
            LOGGER.warning("Processed-CDF dir fallback scan failed: %s", exc)

    # Show every injection, not just the latest per name. Re-runs of the same
    # sample (including multiple runs in one day) are distinct rows in the CSV
    # — keyed on (Lab ID, InjectionDateTime), already deduped above — and must
    # all appear. Repeats get a run-order counter suffix for display only; the
    # raw ``name`` stays the bare Lab ID for CSV/QBench/reprocess use.
    library_view.assign_duplicate_labels(files)

    # Sort chronologically: newest first
    files.sort(key=lambda f: f["mtime"], reverse=True)

    elapsed = time.time() - t0
    LOGGER.info("File cache built: %d entries in %.1fs", len(files), elapsed)

    global _files_cache
    with _files_cache_lock:
        _files_cache = files
    _files_cache_ready.set()
    return files


# ── Throttled rebuild ────────────────────────────────────────────────
# Mid-scan, the watcher used to call _rebuild_files_cache() after every batch,
# each re-reading the whole CSV under distill._CSV_LOCK — the same lock
# /api/files and /api/table need — which starved request handling on a network
# share. Throttle it so the library stays responsive during a long scan.
CACHE_REBUILD_MIN_INTERVAL = 10.0  # seconds between mid-scan full rebuilds
_last_cache_rebuild: float = 0.0
_cache_rebuild_lock = threading.Lock()


def _monotonic() -> float:
    """Indirection seam so tests can freeze time."""
    return time.monotonic()


def _maybe_rebuild_files_cache(force: bool = False) -> None:
    """Rebuild the file cache at most once per CACHE_REBUILD_MIN_INTERVAL unless
    ``force`` is set (used for the final end-of-scan refresh)."""
    global _last_cache_rebuild
    with _cache_rebuild_lock:
        now = _monotonic()
        if not force and (now - _last_cache_rebuild) < CACHE_REBUILD_MIN_INTERVAL:
            return
        _last_cache_rebuild = now
    _rebuild_files_cache()


def _add_to_files_cache(name: str, path: str, mtime: float) -> None:
    """Append a newly processed file to the in-memory cache."""
    entry = {"name": name, "path": path, "mtime": mtime}
    with _files_cache_lock:
        # Avoid duplicates (by path)
        _files_cache[:] = [f for f in _files_cache if f["path"] != path]
        _files_cache.insert(0, entry)  # newest first


# ── Sample flag-rules cache (generalizes early high-signal) ──────────
# path → {"fp": rules-fingerprint, "flags": [{"name", "color"}]}
_early_signal_cache: dict[str, dict] = {}
_early_signal_cache_lock = threading.Lock()
_early_signal_cache_file: Path | None = None


def _load_early_signal_cache() -> None:
    """Load the on-disk sample-flags cache into memory."""
    global _early_signal_cache_file
    conf = settings_mod.load_settings()
    proc_dir = Path(conf.get("processed_cdf_dir", str(paths.default_processed_dir())))
    _early_signal_cache_file = proc_dir / ".sample_flags_cache.json"
    if _early_signal_cache_file.is_file():
        try:
            with _early_signal_cache_file.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            # Only keep entries in the current shape (old bool entries dropped)
            data = {k: v for k, v in data.items()
                    if isinstance(v, dict) and "fp" in v and "flags" in v}
            with _early_signal_cache_lock:
                _early_signal_cache.update(data)
            LOGGER.info("Loaded sample-flags cache (%d entries)", len(data))
        except Exception as exc:
            LOGGER.warning("Failed to read sample-flags cache: %s", exc)


def _save_early_signal_cache() -> None:
    """Persist the in-memory sample-flags cache to disk."""
    if _early_signal_cache_file is None:
        return
    try:
        _early_signal_cache_file.parent.mkdir(parents=True, exist_ok=True)
        with _early_signal_cache_lock:
            snapshot = dict(_early_signal_cache)
        with _early_signal_cache_file.open("w", encoding="utf-8") as fh:
            json.dump(snapshot, fh)
    except Exception as exc:
        LOGGER.debug("Failed to write sample-flags cache: %s", exc)


def _compute_sample_flags(cdf_path: str, rules: list[dict]) -> list[dict]:
    """Evaluate the flag rules against one CDF (empty list on any failure)."""
    p = Path(cdf_path)
    if not p.is_file():
        return []
    try:
        t, y = distill.gc_xy_from_cdf(p)
        return sample_flags.evaluate_rules(t, y, rules)
    except Exception as exc:
        LOGGER.debug("Flag-rule check failed for %s: %s", cdf_path, exc)
        return []


def _get_sample_flags(cdf_path: str, rules: list[dict], fp: str) -> list[dict]:
    """Return cached flags for *cdf_path*, recomputing when the rules changed."""
    with _early_signal_cache_lock:
        entry = _early_signal_cache.get(cdf_path)
        if entry and entry.get("fp") == fp:
            return entry["flags"]
    flags = _compute_sample_flags(cdf_path, rules)
    with _early_signal_cache_lock:
        _early_signal_cache[cdf_path] = {"fp": fp, "flags": flags}
    return flags


# ── Fuel-type best-fit cache ─────────────────────────────────────────
# path → {"fp": config-fingerprint, "label": str, "score": float}
_bestfit_cache: dict[str, dict] = {}
_bestfit_cache_lock = threading.Lock()
_bestfit_cache_file: Path | None = None


def _load_bestfit_cache() -> None:
    """Load the on-disk best-fit cache into memory."""
    global _bestfit_cache_file
    conf = settings_mod.load_settings()
    proc_dir = Path(conf.get("processed_cdf_dir", str(paths.default_processed_dir())))
    _bestfit_cache_file = proc_dir / ".bestfit_cache.json"
    if _bestfit_cache_file.is_file():
        try:
            with _bestfit_cache_file.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            data = {k: v for k, v in data.items()
                    if isinstance(v, dict) and "fp" in v}
            with _bestfit_cache_lock:
                _bestfit_cache.update(data)
            LOGGER.info("Loaded best-fit cache (%d entries)", len(data))
        except Exception as exc:
            LOGGER.warning("Failed to read best-fit cache: %s", exc)


def _save_bestfit_cache() -> None:
    """Persist the in-memory best-fit cache to disk."""
    if _bestfit_cache_file is None:
        return
    try:
        _bestfit_cache_file.parent.mkdir(parents=True, exist_ok=True)
        with _bestfit_cache_lock:
            snapshot = dict(_bestfit_cache)
        with _bestfit_cache_file.open("w", encoding="utf-8") as fh:
            json.dump(snapshot, fh)
    except Exception as exc:
        LOGGER.debug("Failed to write best-fit cache: %s", exc)


def _bestfit_config(conf: dict) -> dict:
    """The classify() kwargs from settings (shared by route + enrichment)."""
    return {
        "threshold": float(conf.get("bestfit_threshold", 0.93)),
        "shift_tolerance_min": float(conf.get("bestfit_shift_tolerance_min", 0.05)),
        "mix_min_frac": float(conf.get("bestfit_mix_min_frac", 0.10)),
        "x_max_min": float(conf.get("analysis_x_max_min", 7.0)),
    }


def _classify_cdf(cdf_path: str, conf: dict) -> dict | None:
    """Full best-fit classification of one CDF (None when unavailable)."""
    if fuel_fit is None:
        return None
    comp_dir = Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))
    standards = fuel_fit.load_standards(comp_dir, distill.gc_xy_from_cdf)
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


def _get_best_fit(cdf_path: str, conf: dict, fp: str) -> dict | None:
    """Cached {label, score} for the file lists (recomputes on config change)."""
    with _bestfit_cache_lock:
        entry = _bestfit_cache.get(cdf_path)
        if entry and entry.get("fp") == fp:
            return {"label": entry["label"], "score": entry["score"]}
    res = _classify_cdf(cdf_path, conf)
    if res is None:
        return None
    slim = {"label": res["label"], "score": res["score"]}
    with _bestfit_cache_lock:
        _bestfit_cache[cdf_path] = dict(slim, fp=fp)
    return slim


def _enrich_files_with_early_signal(files: list[dict]) -> None:
    """Add 'flags' (matched rules), legacy 'early_signal' bool, and the
    'best_fit' fuel classification to each file entry (cached, lazy)."""
    conf = settings_mod.load_settings()
    rules = sample_flags.load_rules(conf)
    fp = sample_flags.rules_fingerprint(rules)
    for f in files:
        flags = _get_sample_flags(f["path"], rules, fp)
        f["flags"] = flags
        f["early_signal"] = bool(flags)

    if fuel_fit is not None and str(conf.get("bestfit_enabled", "true")).lower() == "true":
        comp_dir = Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))
        standards = fuel_fit.load_standards(comp_dir, distill.gc_xy_from_cdf)
        if standards:
            bf_fp = sample_flags.rules_fingerprint([
                _bestfit_config(conf),
                {"standards": [s["name"] for s in standards]},
            ])
            for f in files:
                f["best_fit"] = _get_best_fit(f["path"], conf, bf_fp)


def _migrate_csv_header() -> None:
    """Upgrade an old-format CSV to the current CSV_HEADER column set.

    Reads the existing CSV, detects the old header, and rewrites the file with
    the full ``CSV_HEADER``.  For old rows that lack the new intermediate
    temperature columns, empty values are filled in.  ``Source File`` is
    back-filled by scanning the processed-CDF directory for matching files.
    """
    conf = settings_mod.load_settings()
    csv_path = Path(conf.get("distill_output", str(paths.default_results_csv())))
    if not csv_path.is_file():
        return
    proc_dir = Path(conf.get("processed_cdf_dir", str(paths.default_processed_dir())))

    with distill._CSV_LOCK:
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                if reader.fieldnames and "Best Fit" in reader.fieldnames:
                    return  # already migrated (newest column set present)
                rows = list(reader)
                old_fields = list(reader.fieldnames or [])
        except Exception as exc:
            LOGGER.warning("CSV migration: read failed: %s", exc)
            return

        if not old_fields or not rows:
            return

        LOGGER.info("Migrating CSV from %d-column to %d-column format (%d rows)",
                     len(old_fields), len(distill.CSV_HEADER), len(rows))

        # Build a lookup of processed CDF files for Source File back-fill
        cdf_lookup: dict[str, str] = {}
        if proc_dir and proc_dir.is_dir():
            try:
                for entry in os.scandir(proc_dir):
                    if entry.is_file() and entry.name.upper().endswith(".CDF"):
                        cdf_lookup[entry.name] = str(proc_dir / entry.name)
            except Exception:
                pass

        new_rows: list[dict] = []
        for row in rows:
            new_row: dict[str, str] = {}
            for col in distill.CSV_HEADER:
                new_row[col] = row.get(col, "")
            # Back-fill Source File by matching processed CDF filename
            if not new_row.get("Source File"):
                lab_id = new_row.get("Lab ID", "").strip()
                if lab_id:
                    for cn, full_path in cdf_lookup.items():
                        if cn.startswith(lab_id) or cn.startswith(
                            lab_id.replace(" ", "_")
                        ):
                            new_row["Source File"] = full_path
                            break
            new_rows.append(new_row)

        try:
            distill._atomic_write_csv(csv_path, distill.CSV_HEADER, new_rows)
            LOGGER.info("CSV migration complete — %d rows written with new header",
                        len(new_rows))
        except Exception as exc:
            LOGGER.warning("CSV migration: write failed: %s", exc)


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


def _scan_log(msg: str) -> None:
    """Emit a scan progress message to all SSE subscribers and to the log."""
    LOGGER.info("[scan] %s", msg)
    _publish(_scan_subscribers, _scan_sub_lock, msg)


def _publish_json(
    subscribers: list[queue.Queue], lock: threading.Lock, data: dict
) -> None:
    """Push a JSON-encoded message to all SSE subscribers."""
    _publish(subscribers, lock, json.dumps(data))


def _upload_log(msg: str) -> None:
    """Emit an upload progress message to all SSE subscribers."""
    LOGGER.info("[upload] %s", msg)
    _publish(_upload_subscribers, _upload_sub_lock, msg)


WATCH_DIR_NOT_CONFIGURED = "Watch folder is not configured — set it in Settings"


class WatchDirNotConfigured(RuntimeError):
    """``watch_dir`` is empty or not an existing folder.

    Raised by ``_get_looker()`` — the one door every Looker user goes
    through — so nothing can scan a bogus folder. Routes turn it into a 409
    with ``WATCH_DIR_NOT_CONFIGURED``; background loops skip the cycle.
    """

    def __init__(self, raw: Any = None) -> None:
        super().__init__(f"{WATCH_DIR_NOT_CONFIGURED} (watch_dir={raw!r})")
        self.raw = raw


def _watch_dir_configured(conf: Dict[str, Any]) -> Optional[Path]:
    """The configured watch folder, or ``None`` if it is unusable.

    Tests the *raw* string before building a Path: ``Path("")`` is ``.``
    (cwd — the release folder when deployed) and ``Path("").is_dir()`` is
    True, so an empty setting would otherwise mean "watch the app itself".
    """
    raw = conf.get("watch_dir", paths.default_watch_dir())
    if raw is None or not str(raw).strip():
        return None
    watch = Path(str(raw).strip())
    try:
        return watch if watch.is_dir() else None
    except OSError:  # unreachable share, permission denied, ...
        return None


def _looker_or_409():
    """``(looker, None)`` or ``(None, 409 response)`` when unconfigured."""
    try:
        return _get_looker(), None
    except WatchDirNotConfigured:
        return None, _error(WATCH_DIR_NOT_CONFIGURED, 409)


def _get_looker() -> looker_mod.Looker:
    """Return (and lazily create) the singleton Looker instance.

    Raises ``WatchDirNotConfigured`` while ``watch_dir`` is empty or not an
    existing folder — checked on every call, so clearing the setting (or the
    share disappearing) stops scanning too, not just a fresh start.
    """
    global _looker
    conf = settings_mod.load_settings()
    watch = _watch_dir_configured(conf)
    if watch is None:
        raise WatchDirNotConfigured(conf.get("watch_dir", paths.default_watch_dir()))
    with _looker_lock:
        if _looker is None:
            proc = Path(conf.get("processed_cdf_dir", str(paths.default_processed_dir())))
            blank = Path(conf.get("blank_cache_file", proc / ".blank_cache.json"))
            _looker = looker_mod.Looker(
                watch_dir=watch,
                processed_dir=proc,
                blank_cache=blank,
            )
        return _looker


WATCH_DIR_IDLE_WARNING = "Watch folder is not set or not found — watcher idle"


def _refresh_looker_paths() -> Optional[str]:
    """Re-apply settings to the Looker after a save; return a warning or None.

    With a usable ``watch_dir`` this updates the existing Looker's folders —
    or, if there was none yet (first configuration of a fresh deploy),
    creates it — and makes sure the watcher is running, so no restart is
    needed. With an unusable one the watcher stays idle (``_get_looker()``
    refuses), but an existing Looker still takes the new processed folder,
    and the caller gets ``WATCH_DIR_IDLE_WARNING`` to show the operator.
    """
    conf = settings_mod.load_settings()
    watch = _watch_dir_configured(conf)
    processed = Path(conf.get("processed_cdf_dir", str(paths.default_processed_dir())))
    with _looker_lock:
        existing = _looker
    if watch is None:
        if existing is not None:
            existing.update_paths(watch_dir=existing.watch_dir, processed_dir=processed)
        LOGGER.warning("Watch folder %r is not set or missing - the watcher stays idle "
                       "until it is configured in Settings",
                       conf.get("watch_dir", paths.default_watch_dir()))
        return WATCH_DIR_IDLE_WARNING
    if existing is not None:
        existing.update_paths(watch_dir=watch, processed_dir=processed)
    else:
        _get_looker()
    _start_watcher()
    return None


def _safe_path(p: str) -> Path:
    """Return a Path, handling UNC paths with spaces."""
    return Path(p)


def _error(msg: str, status: int = 400) -> tuple:
    return jsonify({"error": msg}), status


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
# /api/scan/status every 2s
# while a scan runs (startScanStatusPolling); /api/reprocess/status likewise
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
    "/api/scan/status",
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
    # Block if reprocess / rebuild tasks are queued
    if _task_worker and _task_worker.is_alive() and not _task_queue.empty():
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


def _generate_chromatogram_pdf(cdf_path: Path) -> bytes:
    """Generate a single-page chromatogram PDF for *cdf_path*."""
    t, y = distill.gc_xy_from_cdf(cdf_path)
    sample, inj_dt = distill.cdf_metadata(cdf_path)
    title = f"{sample} - {inj_dt.strftime('%Y-%m-%d %H:%M')}"
    fig = _make_chromatogram_figure(t, y, title)
    return pio.to_image(fig, format="pdf", width=1200, height=600)


def _generate_comparison_html(
    sample_path: Path, standard_paths: list[Path]
) -> str:
    """Generate an HTML page with overlaid chromatograms (sample vs standards)."""
    fig = go.Figure()
    t_s, y_s = distill.gc_xy_from_cdf(sample_path)
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

    cal_times, cal_carbons = distill.calibration_ladder(conf)

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

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    try:
        conf = settings_mod.load_settings()
        return jsonify(conf)
    except Exception as exc:
        return _error(str(exc), 500)


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    try:
        body = request.get_json(force=True)
        if not isinstance(body, dict):
            return _error("Expected JSON object")
        # Detect if flag-rule settings changed — clear the cache
        old_conf = settings_mod.load_settings()
        es_keys = ("sample_flag_rules", "early_signal_enabled",
                   "early_signal_time_min", "early_signal_intensity_threshold")
        es_changed = any(
            k in body and str(body.get(k, "")) != str(old_conf.get(k, ""))
            for k in es_keys
        )

        settings_mod.save_settings(body)
        warning = _refresh_looker_paths()

        if es_changed:
            with _early_signal_cache_lock:
                _early_signal_cache.clear()
            _save_early_signal_cache()

        conf = settings_mod.load_settings()
        if warning:
            conf = dict(conf, warning=warning)
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
    """Return the in-memory file list (built at startup, updated incrementally).
    Falls back to a quick os.scandir rebuild if the cache is empty.
    Each entry includes an ``early_signal`` boolean flag."""
    try:
        # If the background build hasn't finished yet, wait briefly then
        # return whatever we have (even if empty — the UI will auto-refresh).
        if not _files_cache_ready.is_set():
            _files_cache_ready.wait(timeout=2.0)

        with _files_cache_lock:
            files = [dict(f) for f in _files_cache]

        # Enrich with early-signal flags (uses cache, fast)
        _enrich_files_with_early_signal(files)
        # Persist cache after enrichment (background, non-blocking)
        threading.Thread(target=_save_early_signal_cache, daemon=True).start()
        threading.Thread(target=_save_bestfit_cache, daemon=True).start()

        return jsonify(files)
    except Exception as exc:
        return _error(str(exc), 500)


@app.route("/api/files/refresh", methods=["POST"])
def api_files_refresh():
    """Force a rebuild of the in-memory file cache."""
    try:
        threading.Thread(target=_rebuild_files_cache, daemon=True).start()
        return jsonify({"status": "rebuilding"})
    except Exception as exc:
        return _error(str(exc), 500)


@app.route("/api/metadata/<path:filepath>", methods=["GET"])
def api_metadata(filepath: str):
    try:
        p = _safe_path(filepath)
        if not p.is_file():
            return _error(f"File not found: {filepath}", 404)
        sample, inj_dt = distill.cdf_metadata(p)
        return jsonify({
            "sample_name": sample,
            "injection_datetime": inj_dt.isoformat(sep=" "),
        })
    except Exception as exc:
        return _error(str(exc), 500)


# ===================================================================== #
#  API: Chromatogram trace
# ===================================================================== #

@app.route("/api/trace", methods=["GET"])
def api_trace():
    cdf_path = request.args.get("path", "").strip()
    if not cdf_path:
        return _error("Missing 'path' query parameter")
    try:
        p = _safe_path(cdf_path)
        if not p.is_file():
            return _error(f"File not found: {cdf_path}", 404)
        t, y = distill.gc_xy_from_cdf(p)
        sample, _ = distill.cdf_metadata(p)
        return jsonify({
            "x": t.tolist(),
            "y": y.tolist(),
            "name": sample,
        })
    except Exception as exc:
        return _error(str(exc), 500)


# ===================================================================== #
#  API: Distillation curve
# ===================================================================== #

@app.route("/api/distillation-curve", methods=["GET"])
def api_distillation_curve():
    cdf_path = request.args.get("path", "").strip()
    if not cdf_path:
        return _error("Missing 'path' query parameter")
    try:
        p = _safe_path(cdf_path)
        if not p.is_file():
            return _error(f"File not found: {cdf_path}", 404)

        # Use cached blank for consistency with process_cdf
        blank_path = None
        try:
            # The existing Looker's blank, not _get_looker(): that is gated on
            # the watch folder, and a down share must not silently drop blank
            # subtraction (or stat the share on every request).
            lk = _looker
            if lk is not None and lk._latest_blank_path and lk._latest_blank_path.is_file():
                sample_name, _ = distill.cdf_metadata(p)
                if "blank" not in sample_name.lower():
                    blank_path = lk._latest_blank_path
        except Exception:
            pass

        pct, temp = distill.distillation_curve_from_cdf(p, blank_path=blank_path)

        # Also return the pre-computed D2887/D86 from CSV so the dashboard
        # can use authoritative values instead of re-computing client-side
        d2887_csv = {}
        d86_csv = {}
        d86_uncorrected_csv: dict = {}
        try:
            sample_name, _ = distill.cdf_metadata(p)
            conf = settings_mod.load_settings()
            csv_path = Path(conf.get("distill_output", str(paths.default_results_csv())))
            if csv_path.is_file():
                import csv as csv_mod
                # Read under the lock (a reader's open handle makes a
                # rewrite's os.replace fail on Windows); process after.
                with distill._CSV_LOCK:
                    with csv_path.open("r", encoding="utf-8", newline="") as fh:
                        csv_rows = list(csv_mod.DictReader(fh))
                best_row = None
                for row in csv_rows:
                    if (row.get("Lab ID", "").strip() == sample_name.strip()):
                        best_row = row  # keep last match (most recent)
                if best_row:
                    for k in distill.CSV_HEADER[2:15]:
                        v = best_row.get(k, "")
                        if v:
                            try: d2887_csv[k] = float(v)
                            except ValueError: pass
                    for k in distill.CSV_HEADER[15:28]:  # D86 columns only (Source File at [28] excluded)
                        v = best_row.get(k, "")
                        if v:
                            try: d86_csv[k] = float(v)
                            except ValueError: pass

            # Pre-calculate uncorrected D86 from D2887 so the frontend toggle
            # can switch between before/after without recomputing in the browser.
            if d2887_csv:
                _csv_to_label = {
                    "2887 IBP": "IBP", "2887 T5": "5%",  "2887 T10": "10%",
                    "2887 T20": "20%", "2887 T30": "30%", "2887 T40": "40%",
                    "2887 T50": "50%", "2887 T60": "60%", "2887 T70": "70%",
                    "2887 T80": "80%", "2887 T90": "90%", "2887 T95": "95%",
                    "2887 FBP": "FBP",
                }
                _label_to_d86key = {
                    "IBP": "D86 IBP", "5%": "D86 T5",  "10%": "D86 T10",
                    "20%": "D86 T20", "30%": "D86 T30", "50%": "D86 T50",
                    "70%": "D86 T70", "80%": "D86 T80", "90%": "D86 T90",
                    "95%": "D86 T95", "FBP": "D86 FBP",
                }
                d2887_for_conv = {
                    label: d2887_csv[csv_k]
                    for csv_k, label in _csv_to_label.items()
                    if csv_k in d2887_csv
                }
                raw_d86 = distill._convert_to_d86(d2887_for_conv)
                for label, d86_key in _label_to_d86key.items():
                    if label in raw_d86:
                        d86_uncorrected_csv[d86_key] = raw_d86[label]
        except Exception:
            pass

        return jsonify({
            "percent": pct.tolist(),
            "temperature": temp.tolist(),
            "d2887": d2887_csv,
            "d86": d86_csv,                          # corrected (CSV, source of truth)
            "d86_uncorrected": d86_uncorrected_csv,  # before EQM corrections
        })
    except Exception as exc:
        return _error(str(exc), 500)


# ===================================================================== #
#  API: Table data (distillation CSV)
# ===================================================================== #

@app.route("/api/table", methods=["GET"])
def api_table():
    try:
        conf = settings_mod.load_settings()
        csv_path = Path(conf.get("distill_output", str(paths.default_results_csv())))
        if not csv_path.is_file():
            return jsonify({"columns": distill.CSV_HEADER, "rows": []})

        # Under the lock: a reader's open handle makes a rewrite's
        # os.replace fail on Windows. Read into memory, then release.
        with distill._CSV_LOCK:
            with csv_path.open("r", encoding="utf-8", newline="") as fh:
                all_rows = list(csv.reader(fh))
        if not all_rows:
            return jsonify({"columns": distill.CSV_HEADER, "rows": []})
        header, rows = all_rows[0], all_rows[1:]

        # NOTE: D86 corrections are already applied in distill.process_cdf()
        # step 5b before writing to CSV.  Do NOT re-apply them here or the
        # values will be double-corrected.  The CSV is the source of truth.

        return jsonify({"columns": header, "rows": rows})
    except Exception as exc:
        return _error(str(exc), 500)


# ===================================================================== #
#  API: Calibration
# ===================================================================== #

@app.route("/api/calibration", methods=["GET"])
def api_calibration():
    """Return detected peaks, compound choices, and any saved assignments.

    Powers the manual calibration page. ``peak_times``/``carbon_numbers``/
    ``boiling_points`` are retained for backward compatibility.
    """
    try:
        conf = settings_mod.load_settings()
        cal_path = distill.active_calibration_path(conf)
        if cal_path is None:
            return _error("No calibration CDF configured", 404)
        if not cal_path.is_file():
            return _error(f"Calibration file not found: {cal_path}", 404)

        # Sensitivity: query param overrides the saved setting (default 50).
        try:
            sensitivity = float(
                request.args.get("sensitivity")
                or conf.get("calibration_sensitivity", "50")
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
        amap = distill.parse_assignment_map(
            conf.get("calibration_assignments", "")
        )
        saved = amap.get(distill._cal_key(cal_path), [])

        # Overlay arrays (peak_times/carbon_numbers/boiling_points) drive the
        # dashboard chromatogram markers. Prefer the saved manual assignments so
        # the overlay matches the calibration the distillation actually uses
        # (same source as calibration_ladder/anchors_for: distill._assignment_pairs
        # drops ignored/unknown carbons and de-dupes); fall back to sequential
        # auto-detection when nothing is assigned.
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
    """Persist manual peak→carbon assignments for the configured cal CDF."""
    try:
        conf = settings_mod.load_settings()
        cal_path = distill.active_calibration_path(conf)
        if cal_path is None:
            return _error("No calibration CDF configured", 404)

        body = request.get_json(silent=True) or {}
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

        conf["calibration_assignments"] = distill.upsert_assignments(
            conf.get("calibration_assignments", ""), cal_path, clean
        )
        # Remember the sensitivity so reopening re-detects the same peaks.
        if body.get("sensitivity") is not None:
            try:
                conf["calibration_sensitivity"] = str(float(body["sensitivity"]))
            except (TypeError, ValueError):
                pass
        settings_mod.save_settings(conf)

        # Drop any cached calibration so the next build uses the new mapping.
        with distill._CAL_LOCK:
            distill._CAL_CACHE.clear()

        anchors = distill.anchors_for(
            distill.parse_assignment_map(conf["calibration_assignments"]),
            cal_path,
        )
        return jsonify({
            "ok": True,
            "saved": len(clean),
            "anchors": 0 if anchors is None else int(anchors[0].size),
        })
    except Exception as exc:
        return _error(str(exc), 500)


@app.route("/api/calibration/active", methods=["GET"])
def api_calibration_active():
    """Diagnostic: report which calibration the running server actually uses.

    Read-only. Reveals whether manual assignments are being applied to the
    distillation (mode=manual) or whether it falls back to auto-detection, and
    surfaces the resolved settings key so a path mismatch is visible.
    """
    try:
        conf = settings_mod.load_settings()
        resolved = distill.active_calibration_path(conf)
        cal_path = resolved if resolved is not None else Path("")
        cal_cdf = str(resolved) if resolved is not None else ""
        raw = conf.get("calibration_assignments", "")
        amap = distill.parse_assignment_map(raw)
        key = distill._cal_key(cal_path)
        saved = amap.get(key, [])
        carbon_count = sum(
            1 for e in saved
            if isinstance(e, dict) and e.get("carbon") is not None
        )
        anchors = distill.anchors_for(amap, cal_path)
        out = {
            "calibration_cdf": cal_cdf,
            "cal_key": key,
            "assignment_map_keys": list(amap.keys()),
            "key_present_in_map": key in amap,
            "saved_carbon_assignments": carbon_count,
            "mode": "manual" if anchors is not None else "auto-detect (fallback)",
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
#  API: Scanning
# ===================================================================== #

_scan_status: Dict[str, Any] = {
    "phase": "idle",          # idle | scanning | processing | done | stopped
    "total": 0, "new": 0, "already": 0,
    "processed": 0, "errors": 0,
    "current_batch": 0, "total_batches": 0,
    "current_file": "",
}
_force_snapshot = threading.Event()  # set when user clicks Scan & Parse

# Reprocess has its own status, *decoupled from the watcher*. The 24/7 watcher
# rewrites _scan_status (resetting processed=0) every WATCHER_POLL_SECONDS, so a
# reprocess task can't safely report progress through it. The browser reprocess
# toast polls /api/reprocess/status, which returns this dict.
_reprocess_status: Dict[str, Any] = {
    "phase": "idle",          # idle | processing | done | stopped | error
    "total": 0, "processed": 0, "errors": 0, "skipped": 0,
}

# ── Directory snapshot cache ──────────────────────────────────────────
# Stores {folder_path: {"size": total_bytes, "count": num_files}} for
# subfolders 1-2 levels deep under the watch directory.  On each poll
# only folders whose size/count changed are re-scanned for new CDFs.
_DIR_CACHE_PATH = paths.dir_cache_file()


def _load_dir_cache() -> Dict[str, Any]:
    """Load the directory snapshot cache from disk."""
    try:
        if _DIR_CACHE_PATH.is_file():
            data = json.loads(_DIR_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "watch_dir" in data:
                return data
    except Exception as exc:
        print(f"[DIRCACHE] Failed to load: {exc}", flush=True)
    return {}


def _save_dir_cache(cache: Dict[str, Any]) -> None:
    """Persist the directory snapshot cache to disk."""
    try:
        _DIR_CACHE_PATH.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except Exception as exc:
        print(f"[DIRCACHE] Failed to save: {exc}", flush=True)


def _invalidate_dir_cache() -> None:
    """Clear the snapshot timestamp so the next scan does a fresh snapshot."""
    try:
        cache = _load_dir_cache()
        if cache:
            cache["snapshot_ts"] = 0
            _save_dir_cache(cache)
            print("[DIRCACHE] Cache invalidated (will re-snapshot on next scan)", flush=True)
    except Exception as exc:
        print(f"[DIRCACHE] Failed to invalidate: {exc}", flush=True)


def _snapshot_folders(watch: Path) -> Dict[str, Dict[str, int]]:
    """Build a snapshot of subfolders 1-2 levels deep.
    Returns {relative_folder: {"size": total_bytes, "count": num_cdf_files}}.
    Also includes the root watch dir itself (key=".")."""
    snap: Dict[str, Dict[str, int]] = {}

    def _stat_folder(folder: Path, rel_key: str) -> None:
        total_size = 0
        count = 0
        try:
            for f in folder.iterdir():
                if f.is_file() and f.suffix.lower() == ".cdf":
                    try:
                        total_size += f.stat().st_size
                        count += 1
                    except OSError:
                        pass
        except OSError:
            pass
        snap[rel_key] = {"size": total_size, "count": count}

    # Root level CDFs
    _stat_folder(watch, ".")

    # Level 1 subfolders
    try:
        for d1 in sorted(watch.iterdir()):
            if not d1.is_dir() or d1.name.startswith("."):
                continue
            rel1 = d1.name
            _stat_folder(d1, rel1)
            # Level 2 subfolders
            try:
                for d2 in sorted(d1.iterdir()):
                    if not d2.is_dir() or d2.name.startswith("."):
                        continue
                    _stat_folder(d2, f"{rel1}/{d2.name}")
            except OSError:
                pass
    except OSError:
        pass

    return snap


_SNAPSHOT_TTL = 30 * 60   # 30 minutes between full directory snapshots


def _smart_scan_cdfs(watch: Path, force_snapshot: bool = False) -> list[Path]:
    """Use the directory cache to only enumerate CDF files in folders that
    changed since the last scan.  Falls back to full rglob on first run.
    The expensive folder snapshot is only redone every _SNAPSHOT_TTL seconds
    unless force_snapshot=True (e.g. user clicked Scan & Parse)."""
    cache = _load_dir_cache()
    old_watch = cache.get("watch_dir", "")
    old_snap = cache.get("folders", {})
    last_snapshot_ts = cache.get("snapshot_ts", 0)
    now = time.time()
    snapshot_age = now - last_snapshot_ts

    # Decide if we need a fresh snapshot
    need_snapshot = (
        force_snapshot
        or str(watch) != old_watch
        or not old_snap
        or snapshot_age >= _SNAPSHOT_TTL
    )

    if not need_snapshot:
        remaining = _SNAPSHOT_TTL - snapshot_age
        print(f"[DIRCACHE] Using cached snapshot ({snapshot_age:.0f}s old, "
              f"next refresh in {remaining:.0f}s)", flush=True)
        return []

    print(f"[DIRCACHE] Snapshotting {watch} (1-2 levels) ...", flush=True)
    t0 = time.time()
    new_snap = _snapshot_folders(watch)
    snap_time = time.time() - t0
    total_files = sum(v["count"] for v in new_snap.values())
    print(f"[DIRCACHE] Snapshot done in {snap_time:.1f}s: "
          f"{len(new_snap)} folders, {total_files} CDF files total", flush=True)

    # Determine which folders changed
    if str(watch) != old_watch or not old_snap:
        # First run or watch dir changed — scan everything
        changed = set(new_snap.keys())
        print(f"[DIRCACHE] First scan or watch dir changed — scanning all {len(changed)} folders",
              flush=True)
    else:
        changed = set()
        for key, info in new_snap.items():
            old = old_snap.get(key)
            if old is None or old["size"] != info["size"] or old["count"] != info["count"]:
                changed.add(key)
        print(f"[DIRCACHE] {len(changed)}/{len(new_snap)} folders changed", flush=True)

    # Save updated cache with timestamp
    _save_dir_cache({
        "watch_dir": str(watch),
        "folders": new_snap,
        "snapshot_ts": now,
    })

    if not changed:
        print("[DIRCACHE] No changes detected — skipping file enumeration", flush=True)
        return []

    # Only enumerate CDFs in changed folders
    all_cdfs: list[Path] = []
    for key in sorted(changed):
        folder = watch if key == "." else watch / key
        if not folder.is_dir():
            continue
        try:
            for f in folder.iterdir():
                if f.is_file() and f.suffix.lower() == ".cdf":
                    all_cdfs.append(f)
        except OSError as exc:
            print(f"[DIRCACHE] Error listing {folder}: {exc}", flush=True)

    # Sort by mtime
    all_cdfs.sort(key=lambda p: p.stat().st_mtime)
    print(f"[DIRCACHE] Enumerated {len(all_cdfs)} CDF files from {len(changed)} changed folders",
          flush=True)
    return all_cdfs


def _fast_scan_cdfs(watch: Path, force: bool = False) -> list[Path]:
    """Fast CDF enumeration using os.scandir (1-2 levels deep).

    Much faster than rglob or the old snapshot-diff approach on network
    shares because os.scandir returns DirEntry objects with metadata in a
    single round-trip, avoiding per-file stat calls.

    Only re-scans when *force* is True or when at least _SNAPSHOT_TTL
    seconds have elapsed since the last full scan (light-weight polling).
    """
    cache = _load_dir_cache()
    last_ts = cache.get("snapshot_ts", 0)
    now = time.time()

    if not force and (now - last_ts) < _SNAPSHOT_TTL:
        # Return cached file list if recent enough (avoid hammering the FS)
        cached_files = cache.get("cached_cdf_paths", [])
        if cached_files:
            return [Path(p) for p in cached_files]
        # Cache exists but has no paths — fall through to full scan

    all_cdfs: list[Path] = []

    def _scan_dir(d: str) -> None:
        try:
            with os.scandir(d) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False):
                        if entry.name.upper().endswith(".CDF"):
                            all_cdfs.append(Path(entry.path))
                    elif entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                        # Level 2
                        try:
                            with os.scandir(entry.path) as it2:
                                for e2 in it2:
                                    if e2.is_file(follow_symlinks=False) and e2.name.upper().endswith(".CDF"):
                                        all_cdfs.append(Path(e2.path))
                        except OSError:
                            pass
        except OSError as exc:
            LOGGER.warning(f"[SCAN] Error scanning {d}: {exc}")

    _scan_dir(str(watch))

    # Sort by mtime so the oldest files are processed first
    try:
        all_cdfs.sort(key=lambda p: p.stat().st_mtime)
    except OSError:
        pass

    # Persist to cache so subsequent polls (within TTL) are instant
    _save_dir_cache({
        "watch_dir": str(watch),
        "snapshot_ts": now,
        "cached_cdf_paths": [str(p) for p in all_cdfs],
        "folders": {},
    })
    LOGGER.info(f"[SCAN] os.scandir found {len(all_cdfs)} CDF files in {time.time()-now:.1f}s")
    return all_cdfs


def _start_watcher() -> None:
    """Start the background watcher thread (idempotent, thread-safe).

    Init, settings save and /api/scan can call this concurrently; the lock
    makes check-then-start atomic so two watchers can never run.
    """
    global _watcher_thread
    with _watcher_start_lock:
        if _watcher_thread and _watcher_thread.is_alive():
            return
        _watcher_stop.clear()
        _watcher_thread = threading.Thread(target=_watcher_loop, daemon=True, name="watcher")
        _watcher_thread.start()
    LOGGER.info("Background watcher started (poll=%ds, batch=%d)",
                WATCHER_POLL_SECONDS, SCAN_BATCH_SIZE)


def _watcher_loop() -> None:
    """Background watcher using the Looker's rglob discovery (same as the old
    desktop app) but with batch processing, SSE progress, and stop support.
    """
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    LOGGER.info("[WATCHER] Background watcher started")

    unconfigured_logged = False
    while not _watcher_stop.is_set():
        try:
            try:
                lk = _get_looker()
            except WatchDirNotConfigured as exc:
                # Idle until Settings names a real folder (or a missing share
                # comes back); log once per outage, not every poll.
                if not unconfigured_logged:
                    LOGGER.warning("Watcher idle: watch folder %r is not set or missing - "
                                   "configure it in Settings", exc.raw)
                    unconfigured_logged = True
                if _scan_status.get("phase") != "stopped":  # keep the user's Stop visible
                    _scan_status["phase"] = "idle"
                _scan_stop.wait(WATCHER_POLL_SECONDS)
                _scan_stop.clear()
                continue
            if unconfigured_logged:
                LOGGER.info("Watch folder available again: %s", lk.watch_dir)
                unconfigured_logged = False
            # _scan_halt is the *within-cycle* abort; clearing it here lets the
            # next cycle process genuinely-new files. Stickiness of a user Stop
            # comes from _suppressed_paths (set below on halt, excluded by
            # _filter_candidates), NOT from leaving _scan_halt set — which would
            # also block new files. This is the fix for the old auto-resume bug:
            # the abandoned backlog stays in _suppressed_paths until /api/scan.
            _scan_halt.clear()
            lk._stop_event.clear()
            _scan_status["phase"] = "scanning"

            t0 = time.time()
            LOGGER.info(f"[WATCHER] Scanning {lk.watch_dir} (rglob) ...")

            # ── Phase 1: discover files using Looker's rglob (reliable) ──
            try:
                all_cdfs = sorted(
                    (fp for fp in lk.watch_dir.rglob("*.cdf") if fp.is_file()),
                    key=lambda p: p.stat().st_mtime,
                )
            except Exception as exc:
                LOGGER.warning(f"[WATCHER] rglob failed: {exc}")
                _scan_status["phase"] = "idle"
                _scan_stop.wait(WATCHER_POLL_SECONDS)
                _scan_stop.clear()
                continue

            candidates = _filter_candidates(all_cdfs, lk._seen)
            total = len(all_cdfs)
            new_count = len(candidates)
            already = total - new_count
            elapsed = time.time() - t0

            _scan_status.update(total=total, new=new_count, already=already,
                                processed=0, errors=0)
            LOGGER.info(f"[WATCHER] Listed in {elapsed:.1f}s: {total} total, "
                        f"{new_count} new, {already} seen")

            if new_count == 0:
                _scan_status["phase"] = "idle"
                _scan_stop.wait(WATCHER_POLL_SECONDS)
                _scan_stop.clear()
                continue

            _publish_json(_scan_subscribers, _scan_sub_lock,
                          {"type": "total", "total": total, "new": new_count,
                           "already": already})

            # ── Phase 2: process in batches with stop + progress ─────────
            _scan_status["phase"] = "processing"
            total_batches = (new_count + SCAN_BATCH_SIZE - 1) // SCAN_BATCH_SIZE
            processed = 0
            errors = 0
            stopped = False
            max_workers = min(4, lk.max_workers)

            LOGGER.info(f"[WATCHER] Processing {new_count} files in {total_batches} "
                        f"batch(es) ({max_workers} workers)")

            for batch_start in range(0, new_count, SCAN_BATCH_SIZE):
                if _scan_halt.is_set():
                    stopped = True
                    # Abandon the rest of the backlog so it won't auto-resume.
                    _suppress_backlog(candidates[batch_start:])
                    break

                batch = candidates[batch_start:batch_start + SCAN_BATCH_SIZE]
                batch_num = batch_start // SCAN_BATCH_SIZE + 1

                pool = ThreadPoolExecutor(max_workers=max_workers)
                futs = {pool.submit(lk._handle_new, fp): fp for fp in batch
                        if not _scan_halt.is_set()}

                remaining = set(futs.keys())
                while remaining and not _scan_halt.is_set():
                    done, remaining = wait(remaining, timeout=0.5,
                                           return_when=FIRST_COMPLETED)
                    for fut in done:
                        fp = futs[fut]
                        try:
                            fut.result()
                            processed += 1
                            lk._seen.add(fp)
                        except Exception as exc:
                            errors += 1
                            LOGGER.warning(f"[WATCHER]   FAIL {fp.name}: {exc}")

                if _scan_halt.is_set():
                    for f in remaining:
                        f.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    stopped = True
                    # Abandon the cancelled batch members + everything after this
                    # batch so the stopped backlog won't auto-resume next cycle.
                    not_started = candidates[batch_start + len(batch):]
                    cancelled = [futs[f] for f in remaining]
                    _suppress_backlog([*cancelled, *not_started])
                else:
                    pool.shutdown(wait=False)

                if stopped:
                    break

                _scan_status.update(processed=processed, errors=errors)
                _publish_json(_scan_subscribers, _scan_sub_lock, {
                    "type": "progress",
                    "current": already + processed + errors,
                    "total": total,
                    "batch": batch_num, "total_batches": total_batches,
                    "processed": processed, "skipped": already,
                    "errors": errors, "status": "ok",
                })

                # Refresh in-memory file list after each batch so the UI picks
                # up newly processed samples mid-scan (throttled so the full-CSV
                # read doesn't starve request handling).
                if processed > 0:
                    try:
                        _maybe_rebuild_files_cache()
                    except Exception:
                        pass

            # ── Summary ──────────────────────────────────────────────────
            total_el = time.time() - t0
            if stopped:
                LOGGER.info(f"[WATCHER] STOPPED ({total_el:.1f}s): {processed} ok, "
                            f"{errors} err")
                _scan_status["phase"] = "stopped"
                _publish_json(_scan_subscribers, _scan_sub_lock,
                              {"type": "stopped", "processed": processed,
                               "skipped": already, "errors": errors})
            else:
                LOGGER.info(f"[WATCHER] Complete ({total_el:.1f}s): {processed} ok, "
                            f"{errors} err")
                _scan_status["phase"] = "done"
                _publish_json(_scan_subscribers, _scan_sub_lock,
                              {"type": "done", "processed": processed,
                               "skipped": already, "errors": errors,
                               "total": total})

            if processed > 0:
                try:
                    _maybe_rebuild_files_cache(force=True)
                except Exception as fc_exc:
                    LOGGER.warning(f"[WATCHER] File cache refresh failed: {fc_exc}")

            _scan_status["phase"] = "idle"

        except Exception as exc:
            LOGGER.exception(f"[WATCHER] ERROR: {exc}")
            _scan_status["phase"] = "idle"

        _scan_stop.wait(WATCHER_POLL_SECONDS)
        _scan_stop.clear()


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Trigger an immediate scan cycle by waking the background watcher.

    Does NOT clear the Looker's ``_seen`` set — doing so would cause already-
    processed files to be re-submitted, risking duplicate CSV rows.  New files
    (not yet in ``_seen``) are picked up automatically.  For a full rebuild use
    ``/api/rebuild-db`` instead.

    Clears ``_scan_halt`` and ``_suppressed_paths`` so an explicit Scan re-attacks
    any backlog a previous Stop abandoned.
    """
    _, err = _looker_or_409()
    if err:
        return err
    _scan_halt.clear()
    with _suppressed_lock:
        _suppressed_paths.clear()
    _start_watcher()
    _scan_stop.set()        # wake the watcher from its sleep
    return jsonify({"status": "started"})


@app.route("/api/scan/status", methods=["GET"])
def api_scan_status():
    """Return current scan status for polling."""
    return jsonify(_scan_status)


@app.route("/api/scan/stream", methods=["GET"])
def api_scan_stream():
    return Response(
        stream_with_context(_sse_stream(_scan_subscribers, _scan_sub_lock)),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/stop-scan", methods=["POST"])
def api_stop_scan():
    _scan_halt.set()
    # Propagate to the Looker so in-progress _handle_new() calls abort at their
    # next checkpoint. Without this, worker threads run to completion regardless.
    # Use the existing Looker directly, not _get_looker(): Stop must reach
    # in-flight work even if the watch folder has just become unusable.
    try:
        lk = _looker
        if lk is not None:
            lk._stop_event.set()
    except Exception as exc:
        LOGGER.warning(f"[WATCHER] Could not signal Looker stop: {exc}")
    # Invalidate the directory cache so the next scan re-snapshots and finds
    # the unprocessed files that were skipped due to the stop.
    _invalidate_dir_cache()
    _scan_status["phase"] = "stopped"
    LOGGER.info("[WATCHER] HALT requested by user")
    _scan_log("Stop requested")
    return jsonify({"status": "stopped"})


# ── Task queue for reprocess / rebuild (never blocks, always queues) ──
_task_queue: queue.Queue = queue.Queue()
_task_worker: Optional[threading.Thread] = None
_task_worker_lock = threading.Lock()


def _ensure_task_worker() -> None:
    """Start the task worker thread if it isn't running."""
    global _task_worker
    with _task_worker_lock:
        if _task_worker and _task_worker.is_alive():
            return
        _task_worker = threading.Thread(target=_task_worker_loop, daemon=True, name="task-worker")
        _task_worker.start()


def _task_worker_loop() -> None:
    """Drain the task queue sequentially."""
    while True:
        try:
            task_fn = _task_queue.get(timeout=30)
        except queue.Empty:
            return  # idle — thread exits, will be restarted on next submit
        try:
            task_fn()
        except Exception as exc:
            _scan_log(f"Task error: {exc}")
            LOGGER.exception("Task worker error")
        finally:
            _task_queue.task_done()


def _enqueue_reprocess(paths, samples, *, label: str = "Reprocess"):
    """Queue a compute+append run through the distillation tunnel.

    Shared by /api/reprocess and /api/export-lims so there is ONE write path:
    looker.reprocess_paths/reprocess_samples → distill.process_cdf →
    distill._append_csv_row (a new CSV row per run). Surfaces a notification when
    any sample fails (e.g. missing calibration) so silent failures are visible.

    Returns ``(count, pending)``.
    """
    count = len(paths) if paths else len(samples)
    pending = _task_queue.qsize()
    # Mark in-progress synchronously so the toast never catches a stale "idle"
    # (or a previous run's "done") between enqueue and the worker picking it up.
    _reprocess_status.update(phase="processing", total=count,
                             processed=0, errors=0, skipped=0)

    def _do_reprocess():
        _scan_stop.clear()
        total = count
        _reprocess_status.update(phase="processing", total=total,
                                 processed=0, errors=0, skipped=0)
        try:
            lk = _get_looker()
            _scan_log(f"{label}: {total} sample(s)...")
            _publish_json(_scan_subscribers, _scan_sub_lock,
                          {"type": "total", "total": total, "new": total, "already": 0})

            if paths:
                results = lk.reprocess_paths(paths, stop_event=_scan_stop)
            else:
                results = lk.reprocess_samples(samples, stop_event=_scan_stop)

            ok = sum(1 for r in results.values() if r.get("status") == "ok")
            errs = sum(1 for r in results.values() if r.get("status") == "error")
            skipped = sum(1 for r in results.values() if r.get("status") == "missing")
            cancelled = any(r.get("status") == "cancelled" for r in results.values())

            for sid, info in results.items():
                status = info.get("status", "unknown")
                _scan_log(f"  {sid}: {status}")

            # Surface failures so "nothing appended" never looks like a no-op.
            if errs:
                first_err = next((r.get("error") for r in results.values()
                                  if r.get("status") == "error" and r.get("error")),
                                 "see log for details")
                notifications_mod.get_store().add(
                    "error",
                    f"{label}: {errs} of {total} sample(s) failed — {first_err}",
                )

            _scan_log(f"{label} complete: {ok} OK, {skipped} skipped, {errs} errors")
            _reprocess_status.update(
                phase="stopped" if cancelled else "done",
                total=total, processed=ok, errors=errs, skipped=skipped)
            _publish_json(_scan_subscribers, _scan_sub_lock,
                          {"type": "done", "processed": ok, "skipped": skipped,
                           "errors": errs, "total": total})

            if ok > 0:
                try:
                    _rebuild_files_cache()
                except Exception:
                    pass
        except Exception as exc:
            _scan_log(f"{label} error: {exc}")
            notifications_mod.get_store().add("error", f"{label} failed: {exc}")
            _reprocess_status.update(phase="error")
            _publish_json(_scan_subscribers, _scan_sub_lock,
                          {"type": "error", "message": str(exc)})

    _task_queue.put(_do_reprocess)
    _ensure_task_worker()

    if pending > 0:
        _scan_log(f"Queued {label.lower()} of {count} sample(s) ({pending} task(s) ahead)")
    return count, pending


@app.route("/api/reprocess", methods=["POST"])
def api_reprocess():
    body = request.get_json(force=True)
    samples = body.get("samples", [])
    # ``paths`` targets exact CDF files (e.g. a specific daily-QC run) instead of
    # resolving a Lab ID to the newest matching CDF. Takes precedence when given.
    paths = body.get("paths", [])
    # ``missing`` = Lab IDs the user asked for (e.g. inside a typed range) that
    # had no matching sample. Surface them in the persistent notification tray.
    missing = [str(m) for m in body.get("missing", []) if str(m).strip()]
    if missing:
        preview = ", ".join(missing[:50]) + ("…" if len(missing) > 50 else "")
        notifications_mod.get_store().add(
            "warning",
            f"Re-process: {len(missing)} Lab ID(s) not found and skipped: {preview}",
        )
    if not samples and not paths:
        if missing:
            return jsonify({"status": "no-match", "missing": missing})
        return _error("No samples provided")
    _, err = _looker_or_409()
    if err:
        return err

    count, pending = _enqueue_reprocess(paths, samples, label="Reprocess")
    return jsonify({"status": "queued", "count": count, "pending": pending})


@app.route("/api/reprocess/status", methods=["GET"])
def api_reprocess_status():
    """Return reprocess progress for the toast to poll.

    Separate from /api/scan/status because the background watcher continuously
    rewrites _scan_status (resetting processed=0), which would clobber any
    reprocess progress reported through it.
    """
    return jsonify(_reprocess_status)


def _library_lab_ids() -> list[str]:
    """Visible Lab IDs currently in the sample library (file cache)."""
    if not _files_cache_ready.is_set():
        _files_cache_ready.wait(timeout=2.0)
    with _files_cache_lock:
        return [f.get("name", "") for f in _files_cache if f.get("name")]


@app.route("/api/reprocess/preview", methods=["POST"])
def api_reprocess_preview():
    """Expand a reprocess query (single IDs, lists, integer ranges) against the
    library and return which samples match and which IDs are missing, so the
    modal can preview before the user confirms.
    """
    body = request.get_json(force=True)
    query = body.get("query", "")
    try:
        tokens = reprocess_query.parse_reprocess_query(query)
    except ValueError as exc:
        return _error(str(exc))
    result = reprocess_query.resolve_query(tokens, _library_lab_ids())
    return jsonify(result)


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


@app.route("/api/library/reindex-times", methods=["POST"])
def api_library_reindex_times():
    """Re-derive each row's InjectionDateTime from its source CDF and rewrite
    that column, so the library re-sorts into true chronological (run) order.

    Manual (Settings button) and queued on the background worker so it never
    slows the normal cache build. Does NOT recompute distillation results.
    """
    pending = _task_queue.qsize()
    _task_queue.put(_do_reindex_injection_times)
    _ensure_task_worker()
    _scan_log(f"Queued library reorder ({pending} task(s) ahead)")
    return jsonify({"status": "queued", "pending": pending})


def _do_reindex_injection_times() -> None:
    """Worker body for /api/library/reindex-times."""
    conf = settings_mod.load_settings()
    csv_path = Path(conf.get("distill_output", str(paths.default_results_csv())))
    if not csv_path.is_file():
        notifications_mod.get_store().add("warning", "Library reorder: no results CSV found.")
        return

    _scan_log("Re-deriving injection times from CDFs…")
    updated = 0
    unreadable = 0

    def _read_rows():
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            return reader.fieldnames or [], list(reader)

    try:
        # Snapshot under the lock, then read every CDF with it released: that
        # can take minutes on a share, and the table, the distillation curve
        # and the Looker's appends all wait on this lock.
        with distill._CSV_LOCK:
            _fieldnames, snapshot = _read_rows()
        derived, unreadable = distill.derive_injection_times(snapshot)

        # Re-read and apply only to rows still present, so rows appended,
        # deleted or edited meanwhile are kept as they now are.
        with distill._CSV_LOCK:
            fieldnames, rows = _read_rows()
            updated = distill.apply_injection_times(rows, derived)

            # Back up before mutating historical data.
            try:
                shutil.copy2(csv_path, csv_path.with_suffix(csv_path.suffix + ".bak"))
            except Exception as exc:
                LOGGER.warning("Could not back up CSV before reorder: %s", exc)

            distill._atomic_write_csv(csv_path, fieldnames, rows)
    except Exception as exc:
        LOGGER.exception("Library reorder failed")
        notifications_mod.get_store().add("error", f"Library reorder failed: {exc}")
        return

    try:
        _rebuild_files_cache()
    except Exception:
        pass

    msg = f"Library reorder complete: {updated} row(s) updated"
    if unreadable:
        msg += f", {unreadable} CDF(s) unreadable/missing"
    _scan_log(msg)
    notifications_mod.get_store().add("success", msg)


@app.route("/api/rebuild-db", methods=["POST"])
def api_rebuild_db():
    _, err = _looker_or_409()
    if err:
        return err
    pending = _task_queue.qsize()

    def _do_rebuild():
        _scan_stop.clear()
        try:
            lk = _get_looker()
            _scan_log("Rebuilding database (backup + delete + rescan)...")
            lk.rebuild_database(backup=True)
            _scan_log("Database cleared. Starting full rescan...")
            lk.scan_now()
            _scan_log("Rebuild complete")
            _publish_json(_scan_subscribers, _scan_sub_lock,
                          {"type": "done", "processed": 0, "skipped": 0,
                           "errors": 0, "total": 0})
            try:
                _rebuild_files_cache()
            except Exception:
                pass
        except Exception as exc:
            _scan_log(f"Rebuild error: {exc}")

    _task_queue.put(_do_rebuild)
    _ensure_task_worker()
    return jsonify({"status": "queued", "pending": pending})


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
    try:
        body = request.get_json(force=True)
        source_path = body.get("source_path", "").strip()
        name = body.get("name", "").strip()
        if not source_path or not name:
            return _error("source_path and name are required")

        src = _safe_path(source_path)
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
        old_name = body.get("old_name", "").strip()
        new_name = body.get("new_name", "").strip()
        if not old_name or not new_name:
            return _error("old_name and new_name are required")

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
    try:
        body = request.get_json(force=True)
        sample_path = body.get("sample_path", "").strip()
        standard_name = body.get("standard_name", "").strip()
        if not sample_path:
            return _error("sample_path is required")
        if not standard_name:
            return _error("standard_name is required")

        conf = settings_mod.load_settings()

        # Resolve standard CDF path
        comp_dir = Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))
        std_path = comp_dir / f"{standard_name}.CDF"
        if not std_path.is_file():
            std_path = comp_dir / f"{standard_name}.cdf"
        if not std_path.is_file():
            return _error(f"Standard not found: {standard_name}", 404)

        sample_p = _safe_path(sample_path)
        if not sample_p.is_file():
            return _error(f"Sample file not found: {sample_path}", 404)

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

        # Load chromatograms
        t_sample, y_sample = distill.gc_xy_from_cdf(sample_p)
        t_std, y_std = distill.gc_xy_from_cdf(std_path)

        # Interpolate standard onto sample time axis if different
        if len(t_sample) != len(t_std) or not np.allclose(t_sample, t_std, atol=1e-6):
            y_std_interp = np.interp(t_sample, t_std, y_std)
            t_common = t_sample
        else:
            y_std_interp = y_std
            t_common = t_sample

        # Calibration data for carbon mapping
        cal_times, cal_carbons = distill.calibration_ladder(conf)

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


@app.route("/api/best-fit", methods=["POST"])
def api_best_fit():
    """Full fuel-type best-fit classification for one sample: label, score,
    per-standard ranking, and mix breakdown (used by the Analysis tab)."""
    try:
        if fuel_fit is None:
            return _error("fuel_fit module not available", 500)
        body = request.get_json(force=True)
        path = body.get("path", "").strip()
        if not path:
            return _error("path is required")
        p = _safe_path(path)
        if not p.is_file():
            return _error(f"File not found: {path}", 404)

        conf = settings_mod.load_settings()
        res = _classify_cdf(str(p), conf)
        if res is None:
            return jsonify({"label": "", "best_standard": "", "score": 0.0,
                            "ranking": [], "mix": None})
        return jsonify(res)
    except Exception as exc:
        LOGGER.exception("Best-fit classification failed")
        return _error(str(exc), 500)


# ===================================================================== #
#  API: Export
# ===================================================================== #

@app.route("/api/export-lims", methods=["POST"])
def api_export_lims():
    """Export the selected sample(s) to LIMS by sending them down the SAME
    distillation tunnel as reprocess: each is (re)computed and a new row is
    appended to the results CSV (``distill_output``). Batch-aware via the
    library multi-selection. One tunnel, one write path — not a second system.

    Body: ``{"paths": [...], "samples": [...]}`` (same shape as /api/reprocess).
    """
    body = request.get_json(force=True) or {}
    samples = body.get("samples", [])
    paths = body.get("paths", [])
    if not samples and not paths:
        return _error("No samples provided")
    _, err = _looker_or_409()
    if err:
        return err

    count, pending = _enqueue_reprocess(paths, samples, label="Export to LIMS")
    return jsonify({"status": "queued", "count": count, "pending": pending})


@app.route("/api/export-pdf", methods=["POST"])
def api_export_pdf():
    try:
        body = request.get_json(force=True)
        cdf_path = body.get("path", "").strip()
        if not cdf_path:
            return _error("path is required")
        p = _safe_path(cdf_path)
        if not p.is_file():
            return _error(f"File not found: {cdf_path}", 404)

        if go is None or pio is None:
            return _error("Plotly/kaleido not installed for PDF generation", 500)

        pdf_bytes = _generate_chromatogram_pdf(p)
        sample, _ = distill.cdf_metadata(p)
        filename = f"{sample}_chromatogram.pdf"

        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as exc:
        return _error(str(exc), 500)


@app.route("/api/export-comparison", methods=["POST"])
def api_export_comparison():
    try:
        body = request.get_json(force=True)
        sample_paths = body.get("sample_paths", [])
        if not sample_paths:
            return _error("sample_paths is required")

        if go is None or pio is None:
            return _error("Plotly/kaleido not installed", 500)

        conf = settings_mod.load_settings()
        comp_dir = Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))
        standard_paths = []
        if comp_dir.is_dir():
            for fp in comp_dir.iterdir():
                if fp.suffix.lower() == ".cdf" and fp.is_file():
                    standard_paths.append(fp)

        export_dir = Path(conf.get("export_folder", str(paths.default_export_dir())))
        export_dir.mkdir(parents=True, exist_ok=True)

        generated_files: list[str] = []
        for sp in sample_paths:
            p = _safe_path(sp)
            if not p.is_file():
                continue
            sample_name, _ = distill.cdf_metadata(p)
            html_content = _generate_comparison_html(p, standard_paths)

            out_file = export_dir / f"{sample_name}_comparison.html"
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
                pdf_file = export_dir / f"{sample_name}_comparison.pdf"
                pdf_file.write_bytes(pdf_bytes)
                generated_files.append(str(pdf_file))
            except Exception as exc:
                LOGGER.warning("PDF generation failed for %s: %s", sample_name, exc)

        return jsonify({"status": "ok", "files": generated_files})
    except Exception as exc:
        return _error(str(exc), 500)


def _run_export_analysis(params: dict, conf: dict) -> tuple[dict, list[dict]]:
    """Shared analysis pass for the report export routes.

    Resolves the standard, runs both detection channels (trend + spike, same
    as ``/api/analysis``), and returns ``(analysis_result, ranges)`` ready for
    ``_generate_analysis_report_pdf``.  Raises ``ValueError`` with a
    user-facing message on missing files.
    """
    sample_path = params.get("sample_path", "").strip()
    standard_name = params.get("standard_name", "").strip()
    if not sample_path or not standard_name:
        raise ValueError("sample_path and standard_name are required")

    comp_dir = Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))
    std_path = comp_dir / f"{standard_name}.CDF"
    if not std_path.is_file():
        std_path = comp_dir / f"{standard_name}.cdf"
    if not std_path.is_file():
        raise ValueError(f"Standard not found: {standard_name}")

    sample_p = _safe_path(sample_path)
    if not sample_p.is_file():
        raise ValueError(f"Sample not found: {sample_path}")

    quantile = float(params.get("quantile", conf.get("analysis_quantile", 0.20)))
    window = int(params.get("window", conf.get("analysis_window", 301)))
    sigma = float(params.get("sigma", conf.get("analysis_sigma", 34.0)))
    thresh_marginal = float(params.get("thresh_marginal", conf.get("analysis_thresh_marginal", 100)))
    thresh_moderate = float(params.get("thresh_moderate", conf.get("analysis_thresh_moderate", 500)))
    thresh_significant = float(params.get("thresh_significant", conf.get("analysis_thresh_significant", 2000)))
    ranges = analysis_core.resolve_report_ranges(params.get("ranges"), conf)

    t_sample, y_sample = distill.gc_xy_from_cdf(sample_p)
    t_std, y_std = distill.gc_xy_from_cdf(std_path)

    if len(t_sample) != len(t_std) or not np.allclose(t_sample, t_std, atol=1e-6):
        y_std_interp = np.interp(t_sample, t_std, y_std)
        t_common = t_sample
    else:
        y_std_interp = y_std
        t_common = t_sample

    cal_times, cal_carbons = distill.calibration_ladder(conf)

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
    return analysis_result, ranges


@app.route("/api/export-analysis-report", methods=["POST"])
def api_export_analysis_report():
    try:
        body = request.get_json(force=True)

        if go is None or pio is None:
            return _error("Plotly/kaleido not installed", 500)

        conf = settings_mod.load_settings()
        try:
            analysis_result, ranges = _run_export_analysis(body, conf)
        except ValueError as exc:
            return _error(str(exc), 404 if "not found" in str(exc) else 400)

        report_bytes = _generate_analysis_report_pdf(body, analysis_result, ranges=ranges)

        doc_name = body.get("doc_name", "analysis_report")
        lab_id = body.get("lab_id", "sample")
        filename = f"{lab_id}_{doc_name}.pdf"

        return send_file(
            io.BytesIO(report_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as exc:
        LOGGER.exception("Analysis report generation failed")
        return _error(str(exc), 500)


@app.route("/api/export-analysis-reports-zip", methods=["POST"])
def api_export_analysis_reports_zip():
    """Generate multiple analysis report PDFs and return them in a single ZIP."""
    try:
        body = request.get_json(force=True)
        items = body.get("items", [])
        if not items:
            return _error("items list is required")

        if go is None or pio is None:
            return _error("Plotly/kaleido not installed", 500)

        conf = settings_mod.load_settings()
        comp_dir = Path(conf.get("comparison_defaults_dir", str(paths.standards_dir())))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in items:
                sample_path = item.get("sample_path", "").strip()
                standard_name = item.get("standard_name", "").strip()
                if not sample_path or not standard_name:
                    continue

                std_path = comp_dir / f"{standard_name}.CDF"
                if not std_path.is_file():
                    std_path = comp_dir / f"{standard_name}.cdf"
                if not std_path.is_file():
                    continue

                try:
                    analysis_result, ranges = _run_export_analysis(item, conf)
                except ValueError as exc:
                    LOGGER.warning("Skipping ZIP item: %s", exc)
                    continue

                report_bytes = _generate_analysis_report_pdf(item, analysis_result, ranges=ranges)
                lab_id = item.get("lab_id", "sample")
                doc_name = item.get("doc_name", "analysis_report")
                filename = f"{lab_id}_{doc_name}.pdf"
                zf.writestr(filename, report_bytes)

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
    if qbench_pdf_uploader is None:
        return _error("qbench_pdf_uploader module not available", 500)

    body = request.get_json(force=True)
    new_queue = body.get("queue", [])
    if not new_queue:
        return _error("queue is required (list of {lab_id, pdf_path})")

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

                pdf_path = item.get("pdf_path", "").strip()

                # ── Step 0: Generate report if needed ─────────────────
                _emit_item(idx, lab_id, "generating", step=0, msg="Generating report...")
                if not pdf_path or not Path(pdf_path).is_file():
                    sample_path = item.get("sample_path", "").strip()
                    standard_name = item.get("standard_name", "").strip()
                    if not sample_path or not standard_name:
                        fail_count += 1
                        _emit_item(idx, lab_id, "error", msg="No sample/standard")
                        continue
                    try:
                        report_params = {
                            "lab_id": lab_id,
                            "doc_name": item.get("sample_name", "GC Analysis"),
                            "standard_name": standard_name,
                            "conclusion": item.get("conclusion", ""),
                            "bullets": item.get("bullets", ""),
                            "overlay_standards": item.get("overlay_standards", []),
                        }
                        sample_p = _safe_path(sample_path)
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
                        cal_times, cal_carbons = distill.calibration_ladder(conf)
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
                        report_bytes = _generate_analysis_report_pdf(report_params, ar)
                        safe_id = lab_id.replace("/", "_").replace("\\", "_")
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
    """Start Looker + watcher + file cache in background so the server starts instantly."""
    def _bg_init():
        try:
            LOGGER.info("Migrating CSV header if needed ...")
            _migrate_csv_header()
        except Exception:
            LOGGER.exception("CSV migration failed (non-fatal)")

        try:
            LOGGER.info("Loading early-signal cache ...")
            _load_early_signal_cache()
            _load_bestfit_cache()
        except Exception:
            LOGGER.exception("Early-signal cache load failed (non-fatal)")

        try:
            LOGGER.info("Building processed-file cache ...")
            _rebuild_files_cache()
        except Exception:
            LOGGER.exception("File cache build failed")
            _files_cache_ready.set()  # unblock /api/files anyway

        try:
            LOGGER.info("Initialising Looker (this may take a moment with many files)...")
            _get_looker()
            LOGGER.info("Looker initialised — starting watcher")
        except WatchDirNotConfigured as exc:
            # Fresh deploy / health check (empty data dir) or a share that is
            # down. Don't build a Looker; the watcher below idles and picks the
            # folder up as soon as Settings (or the network) provides it.
            LOGGER.warning("Looker not started: watch folder %r is not set or missing - "
                           "configure it in Settings", exc.raw)
        except Exception:
            LOGGER.exception("Looker init failed (will retry on first request)")
        try:
            _start_watcher()
        except Exception:
            LOGGER.exception("Watcher start failed")

    threading.Thread(target=_bg_init, daemon=True, name="init").start()
    threading.Thread(target=_auto_restart_loop, daemon=True, name="auto-restart").start()


def _sweep_csv_temps() -> None:
    """Remove temp files a killed atomic CSV rewrite left beside the results
    CSV. Only after the port guard: a live instance may be mid-rewrite."""
    try:
        conf = settings_mod.load_settings()
        csv_path = Path(conf.get("distill_output", str(paths.default_results_csv())))
        removed = distill.sweep_stale_csv_temps(csv_path)
        if removed:
            LOGGER.warning("Removed %d leftover results-CSV temp file(s)", removed)
    except Exception:
        LOGGER.exception("Could not sweep results-CSV temp files (non-fatal)")


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
    _sweep_csv_temps()
    _init_app()
    # debug=True would expose the Werkzeug interactive debugger — remote code
    # execution — on 0.0.0.0. Only an explicit --dev turns it on.
    app.run(host="0.0.0.0", port=GC_PORT, debug=ARGS.dev, threaded=True,
            use_reloader=False)  # reloader kills background threads on file changes
