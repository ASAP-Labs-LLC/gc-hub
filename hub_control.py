"""hub_control.py: the server side of the hub tray (tray/hub_tray.pyw).

A Blueprint registered by ``app.py``, which also calls ``configure`` with
the hooks only it has (the running ``HubRuntime``, how to shut the process
down, its own busy checks) and starts the status refresher.

Open, read-only (no secrets, not counted as activity)::

    GET  /api/hub/status   → status_snapshot(): version, pid, uptime, state
         (running | processing-paused | starting | stopping), processing and
         updater pause, queue sizes, exporter pending rows, CPU % and RSS of
         this process, the staged update, ``stale``

The store-derived numbers come from a cache that a background thread
refreshes every ``CACHE_REFRESH_SECONDS`` with a ``READ_TIMEOUT`` busy
timeout; the request path (this route, and ``/healthz``'s ``hub``) never
opens SQLite, so a locked store cannot slow the updater's health check.
``stale`` is true when the cache is older than ``CACHE_STALE_SECONDS``.

State-changing, **local only** (``netctx.is_local()``: a loopback peer, a
loopback ``Host`` — DNS rebinding — and none of the forwarding headers the
Cloudflare tunnel adds, whose peer is loopback too; checked first, so no LAN or
internet host can even spend password attempts; the session gate refuses them
too when not local), then same-origin,
JSON (415), 64 KiB (413) and the admin password (403). An optional ``by``
(the tray's Windows user, ≤ 64 chars, sanitised) is recorded as
``"<by> (<address>)"``::

    POST /api/admin/hub/pause-processing   {password, by?} → 202 {processing_paused: true}
    POST /api/admin/hub/resume-processing  {password, by?} → 202 {processing_paused: false}
    POST /api/admin/hub/stop               {password, by?, force?}
         → 202 {stopping: true, marker}; 409 {error, busy: [...]} while work is in
           progress (``busy_reasons``) unless ``force: true``

Pause persists the flag (``hub.set_processing_paused``, so a restart comes
back paused) and raises a warning notification at once, then stops the
Worker, exporter and maintenance on a background thread (a running job is
finished first; the tray polls the status). Ingest keeps accepting. Resume
clears both and starts them again.

Stop writes the updater's ``paused`` marker in the data folder (so its
supervise() leaves the hub down instead of relaunching it within ~20 s),
records the stop (``restart_policy.request_stop``: a paused updater normally
means "respawn yourself", a stop never does), then stops the hub's threads
and exits. **Start** is the updater's ``resume`` (it deletes the marker) —
``updater.py resume --app gc --config C:\\ASAPApps\\updater\\config.json`` or
the tray's "Start hub" — after which the updater starts it within ~20 s.
"""
from __future__ import annotations

import collections
import ipaddress
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

from flask import Blueprint, jsonify, request

import admin_auth
import hub
import netctx
import paths
import restart_policy
import store
import version

log = logging.getLogger("hub_control")

bp = Blueprint("hub_control", __name__)

UPDATER_PAUSED_MARKER = "paused"
NOTICE_KEY = "hub_processing_paused_notice"
STATUS_PATH = "/api/hub/status"
STOP_EXIT_DELAY_SECONDS = 0.5
READ_TIMEOUT = 0.25
CACHE_REFRESH_SECONDS = 5.0
CACHE_STALE_SECONDS = 15.0
LOG_EVERY_SECONDS = 60.0
BY_MAX = 64
LOOPBACK_ONLY_MESSAGE = ("Hub control is only available on the server itself "
                         "(http://localhost:5560 on ASAPSV1).")
PAUSED_NOTICE = ("Processing is paused by {by} (since {since}). Samples are still received "
                 "and queued, but nothing is processed, exported to the results CSV or "
                 "backed up until processing is resumed (hub tray: Resume processing).")
RESUMED_NOTICE = "Processing resumed: queued samples are being processed again."
EMPTY_QUEUE = {"jobs_due": None, "jobs_queued": None, "jobs_running": None,
               "received_samples": None}


def _default_notices():
    import notifications
    return notifications.get_store()


class _Hooks:
    def __init__(self) -> None:
        self.runtime: Callable[[], Any] = hub.running
        self.shutdown: Optional[Callable[[], None]] = None
        self.notices: Callable[[], Any] = _default_notices
        self.busy_extra: Callable[[], List[str]] = lambda: []
        self.started_at: float = time.time()


_hooks = _Hooks()
_lock = threading.Lock()            # flag + notice changes
_apply_lock = threading.Lock()      # runtime pause/resume, one at a time


def configure(*, runtime: Optional[Callable[[], Any]] = None,
              shutdown: Optional[Callable[[], None]] = None,
              notices: Any = None, started_at: Optional[float] = None,
              busy_extra: Optional[Callable[[], List[str]]] = None) -> None:
    """``runtime()`` returns the running ``HubRuntime`` or None (default
    ``hub.running``); ``shutdown()`` stops the hub and exits the process
    (the app's; without it Stop answers 503); ``notices`` is a notification
    store (default ``notifications.get_store()``); ``started_at`` the
    process start (``time.time()``); ``busy_extra()`` the app's own reasons
    not to stop now (a QBench upload)."""
    if runtime is not None:
        _hooks.runtime = runtime
    if shutdown is not None:
        _hooks.shutdown = shutdown
    if notices is not None:
        _hooks.notices = lambda: notices
    if started_at is not None:
        _hooks.started_at = started_at
        _sampler.started_at = started_at
    if busy_extra is not None:
        _hooks.busy_extra = busy_extra


def reset() -> None:
    """Back to the defaults (tests)."""
    global _hooks
    _hooks = _Hooks()
    _sampler.started_at = _hooks.started_at
    with _cache_lock:
        _cache.clear()
        _cache.update(_empty_cache())
    _log_times.clear()


# ── rate-limited logging ──────────────────────────────────────────────────

_log_times: dict = {}


def _log_limited(key, level: int, msg: str, *args, exc_info: bool = False) -> None:
    """At most one line per ``key`` per ``LOG_EVERY_SECONDS``."""
    now = time.monotonic()
    last = _log_times.get(key)
    if last is not None and now - last < LOG_EVERY_SECONDS:
        return
    if len(_log_times) > 1000:
        _log_times.clear()
    _log_times[key] = now
    log.log(level, msg, *args, exc_info=exc_info)


# ── process CPU and memory ────────────────────────────────────────────────

def _psutil_process():
    try:
        import psutil
        return psutil.Process()
    except Exception:  # noqa: BLE001 - the status must work without it
        return None


_PROC = _psutil_process()


def _cpu_seconds() -> float:
    if _PROC is not None:
        t = _PROC.cpu_times()
        return float(t.user + t.system)
    t = os.times()
    return float(t.user + t.system)


def _rss_bytes() -> Optional[int]:
    if _PROC is not None:
        return int(_PROC.memory_info().rss)
    try:                                   # Linux without psutil
        with open("/proc/self/statm", "rb") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:  # noqa: BLE001
        return None


class ProcessSampler:
    """This process's CPU use over the last ``window`` seconds, as a share of
    one core (100 = one core busy, like psutil and ``top``; more than 100 is
    possible on several cores), and its resident memory. Each call records a
    sample; the percentage is measured from the oldest sample still in the
    window (or the newest older one), so any mix of callers (the tray every
    5 s, the updater's /healthz) gets a steady figure. The first call gives
    the lifetime average."""

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 cpu_seconds: Callable[[], float] = _cpu_seconds,
                 rss: Callable[[], Optional[int]] = _rss_bytes,
                 started_at: Optional[float] = None, window: float = 15.0) -> None:
        self.clock = clock
        self.cpu_seconds = cpu_seconds
        self.rss = rss
        self.started_at = started_at if started_at is not None else clock()
        self.window = window
        self._samples: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def sample(self) -> dict:
        try:
            now, cpu = self.clock(), self.cpu_seconds()
        except Exception:  # noqa: BLE001
            return {"cpu_percent": None, "rss_bytes": self._safe_rss()}
        with self._lock:
            while len(self._samples) >= 2 and self._samples[1][0] <= now - self.window:
                self._samples.popleft()
            if self._samples and self._samples[0][0] < now:
                t0, c0 = self._samples[0]
            else:
                t0, c0 = self.started_at, 0.0
            self._samples.append((now, cpu))
        span = now - t0
        pct = max(0.0, (cpu - c0) / span * 100.0) if span > 0 else 0.0
        return {"cpu_percent": round(pct, 1), "rss_bytes": self._safe_rss()}

    def _safe_rss(self) -> Optional[int]:
        try:
            return self.rss()
        except Exception:  # noqa: BLE001
            return None


_sampler = ProcessSampler(started_at=_hooks.started_at)


# ── the store-derived numbers, cached off the request path ────────────────

def _empty_cache() -> dict:
    return {"flag": None, "queue": dict(EMPTY_QUEUE), "pending": None, "hub_url": None,
            "at": None}


_cache: dict = _empty_cache()
_cache_lock = threading.Lock()
_refresher: Optional[threading.Thread] = None


def _db_path() -> Optional[Path]:
    d = paths.data_dir()
    if d is None:
        return None
    return Path(d) / store.DB_FILENAME


def _read_store(db: Path) -> tuple:
    """``(flag, queue, pending_rows)`` read with a ``READ_TIMEOUT`` busy
    timeout (read-only; raises ``sqlite3.Error`` when locked)."""
    stamp = store._ts(datetime.now().astimezone())
    ms = int(READ_TIMEOUT * 1000)
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=READ_TIMEOUT)
    try:
        conn.execute(f"PRAGMA busy_timeout={ms}")
        raw = conn.execute("SELECT value FROM settings_kv WHERE key=?",
                           (hub.PROCESSING_PAUSED_KEY,)).fetchone()
        url = conn.execute("SELECT value FROM settings_kv WHERE key=?",
                           (admin_auth.HUB_URL_KEY,)).fetchone()
        r = conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM jobs WHERE state='queued' AND "
            "  (not_before IS NULL OR not_before <= ?)), "
            "(SELECT COUNT(*) FROM jobs WHERE state='queued'), "
            "(SELECT COUNT(*) FROM jobs WHERE state='running'), "
            "(SELECT COUNT(*) FROM samples WHERE status='received'), "
            "(SELECT COUNT(*) FROM export_rows WHERE hub_appended_at IS NULL)",
            (stamp,)).fetchone()
    finally:
        conn.close()
    return (hub.parse_processing_paused(raw[0] if raw else None),
            {"jobs_due": r[0], "jobs_queued": r[1], "jobs_running": r[2],
             "received_samples": r[3]}, r[4], (url[0] if url else None) or None)


def refresh_cache() -> bool:
    """Read the store into the cache; False (keeping the last values) when
    it cannot be read within ``READ_TIMEOUT``. No store yet = nothing
    queued, known now."""
    db = _db_path()
    try:
        if db is None or not db.is_file():
            fresh = _empty_cache()
        else:
            flag, queue, pending, hub_url = _read_store(db)
            fresh = {"flag": flag, "queue": queue, "pending": pending, "hub_url": hub_url}
        fresh["at"] = time.monotonic()
    except Exception:  # noqa: BLE001 - locked, mid-migration, ...
        _log_limited("refresh", logging.WARNING,
                     "hub_control: could not read the store for the status", exc_info=True)
        return False
    with _cache_lock:
        _cache.update(fresh)
    return True


def start_refresher(interval: float = CACHE_REFRESH_SECONDS) -> None:
    """The background thread that keeps the cache fresh (idempotent)."""
    global _refresher
    if _refresher is not None and _refresher.is_alive():
        return

    def loop() -> None:
        while True:
            try:
                refresh_cache()
            except Exception:  # noqa: BLE001 - the loop must survive
                _log_limited("refresh-loop", logging.ERROR, "hub_control: refresh failed",
                             exc_info=True)
            time.sleep(interval)

    _refresher = threading.Thread(target=loop, name="gc-hub-status", daemon=True)
    _refresher.start()


# ── status ────────────────────────────────────────────────────────────────

def _alive(fn) -> bool:
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return False


def status_snapshot() -> dict:
    """What ``GET /api/hub/status`` answers (and ``/healthz``'s ``hub`` and
    the diagnostics bundle carry): no SQLite on this path (the cache), no
    network, never raises, no secrets."""
    now = time.time()
    with _cache_lock:
        cache = dict(_cache)
    at = cache.get("at")
    stale = at is None or time.monotonic() - at > CACHE_STALE_SECONDS
    flag = cache.get("flag")
    try:
        rt = _hooks.runtime()
    except Exception:  # noqa: BLE001
        rt = None
    rt_paused = bool(getattr(rt, "paused", False)) if rt is not None else False
    if restart_policy.stop_requested():
        state = "stopping"
    elif rt is None:
        state = "starting"
    elif rt_paused:
        state = "processing-paused"
    else:
        state = "running"
    data = paths.data_dir()
    updater_paused = bool(data is not None and (Path(data) / UPDATER_PAUSED_MARKER).exists())
    try:
        mode, tag = restart_policy.decide(data, version.APP_VERSION)
    except Exception:  # noqa: BLE001
        mode, tag = "restart", None
    proc = _sampler.sample()
    return {
        "version": version.APP_VERSION,
        "pid": os.getpid(),
        "started_at": datetime.fromtimestamp(_hooks.started_at, timezone.utc)
                              .isoformat(timespec="seconds"),
        "uptime_seconds": round(now - _hooks.started_at, 1),
        "state": state,
        "processing_paused": flag is not None or rt_paused,
        "processing_paused_since": (flag or {}).get("since"),
        "processing_paused_by": (flag or {}).get("by"),
        "updater_paused": updater_paused,
        "worker_alive": _alive(lambda: rt.worker.is_alive()) if rt is not None else False,
        "queue": dict(cache.get("queue") or EMPTY_QUEUE),
        "exporter": {"pending_rows": cache.get("pending"),
                     "alive": _alive(rt.exporter_alive) if rt is not None else False},
        "cpu_percent": proc["cpu_percent"],
        "rss_bytes": proc["rss_bytes"],
        "cpu_count": os.cpu_count(),
        "staged_update": tag if mode == "switch" else None,
        "stale": stale,
        # the hub's address (the admin-set hub URL, else https://gc.asaplabs.net):
        # the tray's "Open in browser"
        "hub_url": cache.get("hub_url") or admin_auth.DEFAULT_HUB_URL,
    }


# ── busy: what a Stop would cut short ─────────────────────────────────────

def busy_reasons() -> List[str]:
    """Work in progress that a Stop would interrupt (empty = safe)."""
    out: List[str] = []
    try:
        out.extend(_hooks.busy_extra() or [])
    except Exception:  # noqa: BLE001
        log.exception("hub_control: busy check failed")
    try:
        rt = _hooks.runtime()
    except Exception:  # noqa: BLE001
        rt = None
    if rt is not None and getattr(getattr(rt, "exporter", None), "ticking", False):
        out.append("an export to the results CSV is being written")
    try:
        import hub_admin
        job = hub_admin.JOBS.current()
        if job is not None and job.get("state") == "running":
            out.append(f"an admin job ({job.get('kind') or 'job'}) is running")
    except Exception:  # noqa: BLE001
        pass
    db = _db_path()
    worker_alive = rt is not None and _alive(lambda: rt.worker.is_alive())
    if worker_alive and db is not None and db.is_file():
        # a `running` row without a live Worker is a leftover, not work
        try:
            running = _read_store(db)[1]["jobs_running"]
            if running:
                out.append(f"{running} processing job(s) running")
        except Exception:  # noqa: BLE001
            out.append("the processing queue could not be read")
    try:
        import diagnostics  # the diagnostics bundle (when present)
        if getattr(diagnostics, "busy", None) is not None and diagnostics.busy():
            out.append("a diagnostics bundle is being built")
    except Exception:  # noqa: BLE001 - not there yet, or broken: not busy
        pass
    return out


# ── pause / resume ────────────────────────────────────────────────────────

def _notice_store():
    try:
        return _hooks.notices()
    except Exception:  # noqa: BLE001 - notifications never stop a pause
        log.exception("hub_control: notifications unavailable")
        return None


def _ensure_paused_notice(db, flag: dict) -> None:
    ns = _notice_store()
    if ns is None:
        return
    try:
        current = store.settings_kv.get(NOTICE_KEY, db=db)
        if current and any(n.get("id") == current for n in ns.list_all()):
            return
        entry = ns.add("warning", PAUSED_NOTICE.format(by=flag.get("by") or "?",
                                                        since=flag.get("since") or "?"))
        store.settings_kv.set(NOTICE_KEY, entry["id"], db=db)
    except Exception:  # noqa: BLE001
        log.exception("hub_control: could not raise the paused notification")


def _clear_paused_notice(db) -> None:
    ns = _notice_store()
    try:
        current = store.settings_kv.get(NOTICE_KEY, db=db)
        if current and ns is not None:
            ns.dismiss(current)
        store.settings_kv.delete(NOTICE_KEY, db=db)
        if ns is not None:
            ns.add("info", RESUMED_NOTICE)
    except Exception:  # noqa: BLE001
        log.exception("hub_control: could not clear the paused notification")


def reconcile(rt=None) -> None:
    """Make the runtime match the persisted flag: after the pause/resume
    routes (on a thread), and once the hub has started (a pause that landed
    during the start-up; the notification after a restart while paused)."""
    db = _db_path()
    if db is None or not db.is_file():
        return
    with _apply_lock:
        rt = rt if rt is not None else _hooks.runtime()
        flag = hub.processing_paused(db)
        if rt is not None:
            if flag is not None and not rt.paused:
                rt.pause()
            elif flag is None and rt.paused:
                rt.resume()
        if flag is not None:
            with _lock:
                _ensure_paused_notice(db, flag)
    refresh_cache()


def _apply_async() -> None:
    def run() -> None:
        try:
            reconcile()
        except Exception:  # noqa: BLE001
            log.exception("hub_control: applying the pause/resume failed")
    threading.Thread(target=run, name="hub-pause-apply", daemon=True).start()


def pause_processing(by: str) -> dict:
    db = admin_auth.hub_db()
    with _lock:
        flag = hub.processing_paused(db)
        if flag is None:
            hub.set_processing_paused(db, True, by=by)
            flag = hub.processing_paused(db)
            log.warning("hub_control: processing paused by %s", by)
        _ensure_paused_notice(db, flag)
    refresh_cache()
    _apply_async()
    return {"processing_paused": True, "applying": True}


def resume_processing(by: str) -> dict:
    db = admin_auth.hub_db()
    with _lock:
        was = hub.processing_paused(db)
        hub.set_processing_paused(db, False)
        if was is not None:
            log.warning("hub_control: processing resumed by %s", by)
            _clear_paused_notice(db)
    refresh_cache()
    _apply_async()
    return {"processing_paused": False, "applying": True}


# ── stop ──────────────────────────────────────────────────────────────────

def _write_marker(data_dir: Path, by: str) -> Path:
    marker = Path(data_dir) / UPDATER_PAUSED_MARKER
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    marker.write_text(
        f"stopped from the GC hub by {by} at {stamp} (hub tray: Stop hub).\n"
        "The updater will not start the hub again while this file exists. Start it with\n"
        "the hub tray's Start hub, or: updater.py resume --app gc "
        "--config C:\\ASAPApps\\updater\\config.json\n", encoding="utf-8")
    return marker


def _stop_later(shutdown: Callable[[], None]) -> None:
    time.sleep(STOP_EXIT_DELAY_SECONDS)      # let the 202 reach the caller
    try:
        shutdown()
    except Exception:  # noqa: BLE001
        log.exception("hub_control: shutdown failed")


# ── routes ────────────────────────────────────────────────────────────────

def is_loopback(addr: Optional[str]) -> bool:
    """127.0.0.0/8, ::1 or an IPv4-mapped loopback; nothing else (a name
    such as ``localhost`` is never a remote address). ``netctx``'s rule."""
    return netctx.is_loopback_ip(addr)


def host_is_loopback(host: Optional[str]) -> bool:
    """The ``Host`` header names this machine's loopback: ``localhost``,
    127.x or ::1 (with any port). A rebinding page carries its own name."""
    return netctx.host_is_loopback(host)


_BY_BAD = re.compile(r"[^A-Za-z0-9 ._@\\-]")


def _by(body: dict, addr: str) -> str:
    raw = body.get("by") if isinstance(body, dict) else None
    user = _BY_BAD.sub("", raw).strip()[:BY_MAX].strip() if isinstance(raw, str) else ""
    return f"{user} ({addr})" if user else addr


def _err(msg: str, status: int):
    return jsonify({"error": msg}), status


def _guard():
    """``(by, body, None)`` or ``(None, None, error response)``: loopback
    client, loopback Host, same-origin, JSON ≤ 64 KiB, admin password."""
    addr = netctx.client_ip()
    if not netctx.is_local():
        # loopback peer, loopback Host (DNS rebinding) and no forwarding header:
        # the Cloudflare tunnel's peer is loopback too, and is never local
        _log_limited(("refused", addr), logging.WARNING,
                     "hub_control: refused %s %s from %s (Host %r): not local",
                     request.method, request.path, addr, request.host)
        return None, None, _err(LOOPBACK_ONLY_MESSAGE, 403)
    refusal = netctx.cross_site_refusal()
    if refusal:
        return None, None, _err(refusal, 403)
    body, err = admin_auth._json_body()
    if err:
        return None, None, err
    try:
        ok = admin_auth.check_admin_body(body)
    except Exception:  # noqa: BLE001 - the gate never opens on an error
        log.exception("hub_control: admin check failed")
        ok = False
    if not ok:
        return None, None, _err("Incorrect password", 403)
    return _by(body, addr), body, None


@bp.route(STATUS_PATH, methods=["GET"])
def api_hub_status():
    return jsonify(status_snapshot())


@bp.route("/api/admin/hub/pause-processing", methods=["POST"])
def api_pause_processing():
    by, _body, err = _guard()
    if err:
        return err
    return jsonify(pause_processing(by)), 202


@bp.route("/api/admin/hub/resume-processing", methods=["POST"])
def api_resume_processing():
    by, _body, err = _guard()
    if err:
        return err
    return jsonify(resume_processing(by)), 202


@bp.route("/api/admin/hub/stop", methods=["POST"])
def api_stop():
    by, body, err = _guard()
    if err:
        return err
    shutdown = _hooks.shutdown
    data = paths.data_dir()
    if shutdown is None or data is None:
        return _err("This process cannot stop itself (no shutdown hook or data folder).", 503)
    if body.get("force") is not True:
        busy = busy_reasons()
        if busy:
            return jsonify({"error": "The hub is busy: " + "; ".join(busy) + ". Stop anyway "
                                     "with force, or wait.", "busy": busy}), 409
    try:
        _write_marker(Path(data), by)
    except Exception as exc:  # noqa: BLE001 - never exit without the marker
        log.exception("hub_control: could not write the updater's paused marker")
        return _err(f"Could not write {Path(data) / UPDATER_PAUSED_MARKER}: {exc}. "
                    "The hub was not stopped (the updater would restart it).", 500)
    restart_policy.request_stop()
    log.warning("hub_control: STOP requested by %s%s; updater paused, exiting without a "
                "respawn", by, " (forced)" if body.get("force") is True else "")
    threading.Thread(target=_stop_later, args=(shutdown,), daemon=True,
                     name="hub-stop").start()
    return jsonify({"stopping": True, "marker": UPDATER_PAUSED_MARKER,
                    "start": "updater.py resume --app gc (or the hub tray's Start hub); the "
                             "updater starts the hub within ~20 s"}), 202
