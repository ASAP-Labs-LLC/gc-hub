"""The hub tray's pure logic: no I/O beyond reading tray.json, no pystray,
no tkinter, so it is unit-tested on every platform.

The tray polls ``GET /api/hub/status`` (hub_control.status_snapshot) and
turns the answer into a *view*::

    {reachable, state, version, cpu_percent, rss_bytes, queue, jobs_due,
     pending_rows, staged_update, processing_paused, updater_paused}

``state`` is ``running``, ``paused`` (processing paused), ``starting``,
``stopping``, or, when the hub does not answer, ``stopped`` (the updater's
``paused`` marker is there: someone stopped it) or ``down``.

Colours: green running; amber busy (CPU above ``cpu_busy_percent`` of one
core for ``cpu_busy_seconds``, or more than ``queue_busy_threshold`` jobs
due) or processing paused / starting; red stopped, stopping or unreachable.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

COLOURS = {
    "green": (46, 160, 67),
    "amber": (230, 160, 0),
    "red": (200, 40, 40),
}

DEFAULTS = {
    "port": 5560,
    "app_name": "gc",
    "data_dir": "C:\\ASAPApps\\gc\\data",
    "updater_python": "",                      # "" = the tray's own interpreter
    "updater_script": "C:\\ASAPApps\\updater\\updater.py",
    "updater_config": "C:\\ASAPApps\\updater\\config.json",
    "poll_seconds": 5,
    "cpu_busy_percent": 50,
    "cpu_busy_seconds": 60,
    "queue_busy_threshold": 25,
    "remember_password": True,
}

_SERVER_STATES = {"running": "running", "processing-paused": "paused",
                  "starting": "starting", "stopping": "stopping"}
TOOLTIP_MAX = 127                                # Windows' NOTIFYICONDATA limit - 1


class ConfigError(ValueError):
    pass


# ── config ────────────────────────────────────────────────────────────────

def _num(cfg, key, lo, hi, integer=False):
    v = cfg[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ConfigError(f"tray.json: {key} must be a number from {lo} to {hi}")
    if integer and int(v) != v:
        raise ConfigError(f"tray.json: {key} must be a whole number")


def load_config(path) -> dict:
    """``DEFAULTS`` overlaid with the known keys of ``path`` (a missing file
    is fine; unknown keys, e.g. ``_comment``, are ignored)."""
    cfg = dict(DEFAULTS)
    raw: dict = {}
    p = Path(path) if path else None
    if p is not None and p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"{p}: {exc}")
        if not isinstance(raw, dict):
            raise ConfigError(f"{p}: expected a JSON object")
        cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    if "port" not in raw:
        cfg["port"] = updater_port(cfg.get("updater_config"), cfg.get("app_name")) \
            or DEFAULTS["port"]
    _num(cfg, "port", 1, 65535, integer=True)
    _num(cfg, "poll_seconds", 1, 3600)
    _num(cfg, "cpu_busy_percent", 1, 10000)
    _num(cfg, "cpu_busy_seconds", 0, 86400)
    _num(cfg, "queue_busy_threshold", 0, 10 ** 9)
    if not isinstance(cfg["remember_password"], bool):
        raise ConfigError("tray.json: remember_password must be true or false")
    for key in ("app_name", "data_dir", "updater_python", "updater_script", "updater_config"):
        if not isinstance(cfg[key], str):
            raise ConfigError(f"tray.json: {key} must be a string")
    return cfg


def updater_port(updater_config, app_name) -> Optional[int]:
    """The ``port`` of the ``app_name`` entry in the updater's config.json,
    or None when it cannot be read (it is often Administrators-only)."""
    try:
        data = json.loads(Path(updater_config).read_text(encoding="utf-8-sig"))
        for app in data.get("apps") or []:
            if isinstance(app, dict) and app.get("name") == app_name:
                port = app.get("port")
                if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
                    return port
    except Exception:  # noqa: BLE001 - missing, unreadable, malformed
        return None
    return None


def status_url(cfg) -> str:
    """Where the tray talks to the hub: IPv4 loopback (the hub listens on
    0.0.0.0, and ``localhost`` may try ::1 first), which is also what
    hub_control's loopback-only routes require."""
    return f"http://127.0.0.1:{int(cfg['port'])}"


def browser_url(cfg) -> str:
    return f"http://localhost:{int(cfg['port'])}"


def marker_path(cfg) -> Optional[Path]:
    """The updater's ``paused`` marker (``<data_dir>\\paused``), or None."""
    d = cfg.get("data_dir") or ""
    if not d:
        return None
    return Path(d) / "paused"


def start_command(cfg, *, fallback_python: str) -> list:
    """The updater CLI that clears the marker: ``resume --app gc``. The
    updater is stdlib-only, so the tray's own interpreter can run it."""
    return [cfg.get("updater_python") or fallback_python, cfg["updater_script"], "resume",
            "--app", cfg["app_name"], "--config", cfg["updater_config"]]


# ── status → view ─────────────────────────────────────────────────────────

def _number(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def parse_status(body, *, marker_present: bool) -> dict:
    """The view of one poll: ``body`` is ``/api/hub/status``'s JSON, or None
    when the hub did not answer."""
    if not isinstance(body, dict):
        return {"reachable": False, "state": "stopped" if marker_present else "down",
                "version": None, "cpu_percent": None, "rss_bytes": None, "queue": None,
                "jobs_due": None, "pending_rows": None, "staged_update": None,
                "processing_paused": False, "updater_paused": bool(marker_present)}
    queue = body.get("queue") if isinstance(body.get("queue"), dict) else {}
    exporter = body.get("exporter") if isinstance(body.get("exporter"), dict) else {}
    state = body.get("state")
    state = _SERVER_STATES.get(state, "running") if isinstance(state, str) else "running"
    version = body.get("version")
    staged = body.get("staged_update")
    return {
        "reachable": True,
        "state": state,
        "version": version if isinstance(version, str) else None,
        "cpu_percent": _number(body.get("cpu_percent")),
        "rss_bytes": _number(body.get("rss_bytes")),
        "queue": _number(queue.get("jobs_queued")),
        "jobs_due": _number(queue.get("jobs_due")),
        "pending_rows": _number(exporter.get("pending_rows")),
        "staged_update": staged if isinstance(staged, str) and staged else None,
        "processing_paused": body.get("processing_paused") is True or state == "paused",
        "updater_paused": body.get("updater_paused") is True,
    }


class BusyTracker:
    """CPU busy = above ``cpu_percent`` (of one core) for ``seconds`` without
    a dip; an unknown reading resets it."""

    def __init__(self, cpu_percent: float = 50, seconds: float = 60) -> None:
        self.cpu_percent = cpu_percent
        self.seconds = seconds
        self._since: Optional[float] = None

    def update(self, now: float, cpu: Optional[float]) -> bool:
        if cpu is None or cpu <= self.cpu_percent:
            self._since = None
            return False
        if self._since is None:
            self._since = now
        return now - self._since >= self.seconds


def colour(view: dict, *, cpu_busy: bool, queue_threshold: float) -> str:
    if not view.get("reachable") or view.get("state") in ("stopped", "down", "stopping"):
        return "red"
    if view.get("state") in ("paused", "starting") or cpu_busy:
        return "amber"
    due = view.get("jobs_due")
    if due is not None and due > queue_threshold:
        return "amber"
    return "green"


# ── text ──────────────────────────────────────────────────────────────────

def format_bytes(n) -> str:
    if n is None:
        return "?"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{int(n / 1024 ** 2 + 0.5)} MB"


_STATE_WORDS = {"running": "running", "paused": "processing paused",
                "starting": "starting", "stopping": "stopping"}


def status_text(view: dict) -> str:
    """The menu's status line: ``GC hub v3.0.0 · running · CPU 12% · RAM
    190 MB · queue 3``."""
    if not view.get("reachable"):
        if view.get("state") == "stopped":
            return "GC hub · stopped (updater paused)"
        return "GC hub · not responding"
    cpu = view.get("cpu_percent")
    queue = view.get("queue")
    parts = [f"GC hub {view.get('version') or '?'}",
             _STATE_WORDS.get(view.get("state"), view.get("state") or "?"),
             "CPU ?" if cpu is None else f"CPU {cpu:.0f}%",
             f"RAM {format_bytes(view.get('rss_bytes'))}",
             "queue ?" if queue is None else f"queue {queue:.0f}"]
    return " · ".join(parts)


def tooltip(view: dict) -> str:
    text = status_text(view)
    return text if len(text) <= TOOLTIP_MAX else text[:TOOLTIP_MAX - 1] + "…"


def menu_state(view: dict) -> dict:
    up = bool(view.get("reachable")) and view.get("state") != "stopping"
    staged = view.get("staged_update")
    return {
        "pause_label": "Resume processing" if view.get("processing_paused")
        else "Pause processing",
        "pause_enabled": up,
        "restart_label": f"Restart & install {staged}" if staged else "Restart",
        "restart_enabled": up,
        "stop_enabled": up,
        "open_enabled": up,
        "start_enabled": not view.get("reachable"),
    }


# ── relaunch after an update ──────────────────────────────────────────────

def should_relaunch(own_version: Optional[str], disk_version: Optional[str],
                    hub_version: Optional[str]) -> bool:
    """True when the updater switched the hub to a new release and the
    tray's own files (read again through the ``current`` junction) are that
    release: restart the tray to run the new code and let go of the old
    release's files (the updater prunes old releases). Never when the disk
    still has the version the tray is running (launched from a fixed
    release folder), so it cannot loop."""
    if not own_version or not disk_version or not hub_version:
        return False
    return own_version != disk_version and disk_version == hub_version


def read_version(release_root) -> str:
    """First line of ``VERSION`` in the release (``dev`` when absent), as
    version.py reads it."""
    try:
        text = (Path(release_root) / "VERSION").read_text(encoding="utf-8")
    except OSError:
        return "dev"
    first = text.splitlines()[0].strip() if text.strip() else ""
    return first or "dev"
