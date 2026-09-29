"""hub_control.py: the server side of the hub tray (tray/hub_tray.pyw).

A Blueprint registered by ``app.py``, which also calls ``configure`` with
the hooks only it has (the running ``HubRuntime`` and how to shut the
process down).

Open, read-only (no secrets, not counted as activity)::

    GET  /api/hub/status   → status_snapshot(): version, pid, uptime, state
         (running | processing-paused | starting | stopping), processing and
         updater pause, queue sizes, exporter pending rows, CPU % and RSS of
         this process, the staged update (for "Restart & install vX")

State-changing, **loopback only** (``request.remote_addr`` in 127.0.0.0/8 or
::1, checked first, so a LAN host cannot even spend password attempts), then
same-origin, JSON (415), 64 KiB (413) and the admin password (403)::

    POST /api/admin/hub/pause-processing   {password} → 200 {processing_paused: true}
    POST /api/admin/hub/resume-processing  {password} → 200 {processing_paused: false}
    POST /api/admin/hub/stop               {password} → 202 {stopping: true, marker}

Pause stops the Worker, the exporter and maintenance (``HubRuntime.pause``)
while the web app keeps serving and ingest keeps accepting (samples wait as
``received``, their jobs queued). The choice is persisted in ``settings_kv``
(``hub.set_processing_paused``), so a restart comes back paused, and a
warning notification stays up until Resume.

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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from flask import Blueprint, jsonify, request

import admin_auth
import hub
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
LOOPBACK_ONLY_MESSAGE = ("Hub control is only available on the server itself "
                         "(http://localhost:5560 on ASAPSV1).")
PAUSED_NOTICE = ("Processing is paused (from the hub tray by {by}, {since}). Samples are "
                 "still received and queued, but nothing is processed, exported to the "
                 "results CSV or backed up until processing is resumed (hub tray: Resume "
                 "processing).")
RESUMED_NOTICE = "Processing resumed: queued samples are being processed again."


def _default_notices():
    import notifications
    return notifications.get_store()


class _Hooks:
    def __init__(self) -> None:
        self.runtime: Callable[[], Any] = hub.running
        self.shutdown: Optional[Callable[[], None]] = None
        self.notices: Callable[[], Any] = _default_notices
        self.started_at: float = time.time()


_hooks = _Hooks()
_lock = threading.Lock()


def configure(*, runtime: Optional[Callable[[], Any]] = None,
              shutdown: Optional[Callable[[], None]] = None,
              notices: Any = None, started_at: Optional[float] = None) -> None:
    """``runtime()`` returns the running ``HubRuntime`` or None (default
    ``hub.running``); ``shutdown()`` stops the hub and exits the process
    (the app's; without it Stop answers 503); ``notices`` is a notification
    store (default ``notifications.get_store()``); ``started_at`` the
    process start (``time.time()``)."""
    if runtime is not None:
        _hooks.runtime = runtime
    if shutdown is not None:
        _hooks.shutdown = shutdown
    if notices is not None:
        _hooks.notices = lambda: notices
    if started_at is not None:
        _hooks.started_at = started_at
        _sampler.started_at = started_at


def reset() -> None:
    """Back to the defaults (tests)."""
    global _hooks
    _hooks = _Hooks()
    _sampler.started_at = _hooks.started_at


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


# ── status ────────────────────────────────────────────────────────────────

def _db_path() -> Optional[Path]:
    d = paths.data_dir()
    if d is None:
        return None
    p = Path(d) / store.DB_FILENAME
    return p if p.is_file() else None


def _counts(db: Optional[Path]) -> tuple:
    empty = {"jobs_due": None, "jobs_queued": None, "jobs_running": None,
             "received_samples": None}
    if db is None:
        return empty, None
    stamp = store._ts(datetime.now().astimezone())
    try:
        with store.connection(db) as conn:
            r = conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM jobs WHERE state='queued' AND "
                "  (not_before IS NULL OR not_before <= ?)), "
                "(SELECT COUNT(*) FROM jobs WHERE state='queued'), "
                "(SELECT COUNT(*) FROM jobs WHERE state='running'), "
                "(SELECT COUNT(*) FROM samples WHERE status='received'), "
                "(SELECT COUNT(*) FROM export_rows WHERE hub_appended_at IS NULL)",
                (stamp,)).fetchone()
    except Exception:  # noqa: BLE001 - a status never fails on the store
        log.exception("hub_control: could not count the queues")
        return empty, None
    return ({"jobs_due": r[0], "jobs_queued": r[1], "jobs_running": r[2],
             "received_samples": r[3]}, r[4])


def _alive(fn) -> bool:
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return False


def status_snapshot() -> dict:
    """What ``GET /api/hub/status`` answers (and ``/healthz``'s ``hub`` and
    the diagnostics bundle carry): cheap (a few indexed COUNTs, no CDF, no
    network), never raises, no secrets."""
    now = time.time()
    db = _db_path()
    try:
        flag = hub.processing_paused(db) if db is not None else None
    except Exception:  # noqa: BLE001
        flag = None
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
    queue, pending = _counts(db)
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
        "queue": queue,
        "exporter": {"pending_rows": pending,
                     "alive": _alive(rt.exporter_alive) if rt is not None else False},
        "cpu_percent": proc["cpu_percent"],
        "rss_bytes": proc["rss_bytes"],
        "cpu_count": os.cpu_count(),
        "staged_update": tag if mode == "switch" else None,
    }


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
    """Make the runtime match the persisted flag (the app calls this once
    the hub has started: a pause that landed during the start-up, and the
    notification after a restart while paused)."""
    db = _db_path()
    if db is None:
        return
    rt = rt if rt is not None else _hooks.runtime()
    flag = hub.processing_paused(db)
    with _lock:
        if rt is not None:
            if flag is not None and not rt.paused:
                rt.pause()
            elif flag is None and rt.paused:
                rt.resume()
        if flag is not None:
            _ensure_paused_notice(db, flag)


def pause_processing(by: str) -> dict:
    db = admin_auth.hub_db()
    with _lock:
        flag = hub.processing_paused(db)
        if flag is None:
            hub.set_processing_paused(db, True, by=by)
            flag = hub.processing_paused(db)
            log.warning("hub_control: processing paused by %s", by)
        rt = _hooks.runtime()
        if rt is not None:
            rt.pause()
        _ensure_paused_notice(db, flag)
    return {"processing_paused": True, "applied": rt is not None}


def resume_processing(by: str) -> dict:
    db = admin_auth.hub_db()
    with _lock:
        was = hub.processing_paused(db)
        hub.set_processing_paused(db, False)
        rt = _hooks.runtime()
        if rt is not None:
            rt.resume()
        if was is not None:
            log.warning("hub_control: processing resumed by %s", by)
            _clear_paused_notice(db)
    return {"processing_paused": False, "applied": rt is not None}


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
    such as ``localhost`` is never a remote address)."""
    try:
        ip = ipaddress.ip_address((addr or "").split("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback


def _err(msg: str, status: int):
    return jsonify({"error": msg}), status


def _guard():
    """``(by, None)`` or ``(None, error response)``: loopback, same-origin,
    JSON ≤ 64 KiB, admin password, in that order."""
    addr = request.remote_addr
    if not is_loopback(addr):
        log.warning("hub_control: refused %s %s from %s (not loopback)",
                    request.method, request.path, addr)
        return None, _err(LOOPBACK_ONLY_MESSAGE, 403)
    if admin_auth._cross_site():
        return None, _err("Cross-site request refused", 403)
    body, err = admin_auth._json_body()
    if err:
        return None, err
    try:
        ok = admin_auth.check_admin_body(body)
    except Exception:  # noqa: BLE001 - the gate never opens on an error
        log.exception("hub_control: admin check failed")
        ok = False
    if not ok:
        return None, _err("Incorrect password", 403)
    return addr, None


@bp.route(STATUS_PATH, methods=["GET"])
def api_hub_status():
    return jsonify(status_snapshot())


@bp.route("/api/admin/hub/pause-processing", methods=["POST"])
def api_pause_processing():
    by, err = _guard()
    if err:
        return err
    return jsonify(pause_processing(by))


@bp.route("/api/admin/hub/resume-processing", methods=["POST"])
def api_resume_processing():
    by, err = _guard()
    if err:
        return err
    return jsonify(resume_processing(by))


@bp.route("/api/admin/hub/stop", methods=["POST"])
def api_stop():
    by, err = _guard()
    if err:
        return err
    shutdown = _hooks.shutdown
    data = paths.data_dir()
    if shutdown is None or data is None:
        return _err("This process cannot stop itself (no shutdown hook or data folder).", 503)
    try:
        _write_marker(Path(data), by)
    except Exception as exc:  # noqa: BLE001 - never exit without the marker
        log.exception("hub_control: could not write the updater's paused marker")
        return _err(f"Could not write {Path(data) / UPDATER_PAUSED_MARKER}: {exc}. "
                    "The hub was not stopped (the updater would restart it).", 500)
    restart_policy.request_stop()
    log.warning("hub_control: STOP requested by %s; updater paused, exiting without a "
                "respawn", by)
    threading.Thread(target=_stop_later, args=(shutdown,), daemon=True,
                     name="hub-stop").start()
    return jsonify({"stopping": True, "marker": UPDATER_PAUSED_MARKER,
                    "start": "updater.py resume --app gc (or the hub tray's Start hub); the "
                             "updater starts the hub within ~20 s"}), 202
