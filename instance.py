"""instance.py — per-instance identity for the GC Viewer webapp.

One machine can run several copies of this app at once (one per GC
workstation folder). Everything that must differ between those copies — the
TCP port, the settings file, the launcher's pidfile — is derived here from a
single value: the port.

Stdlib only and free of import-time side effects, so the launcher, the Flask
app and the test suite can all import it. (``app.py`` cannot be imported by
tests: ``_init_app()`` starts background threads at import time.)
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

DEFAULT_PORT = 5560
PORT_MIN = 1024
PORT_MAX = 65535

SETTINGS_DIR = Path.home()
SETTINGS_STEM = ".gc_viewer_settings"
PIDFILE_STEM = ".gc_server"

RECENT_PORTS_PATH = Path.home() / ".gc_launcher_ports.json"
RECENT_LIMIT = 8


def validate_port(value) -> int:
    """Coerce *value* to a usable TCP port, or raise ``ValueError``.

    The message is shown verbatim to the operator in the launcher dialog, so
    keep it plain-language.
    """
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"Port must be a whole number — got {value!r}")
    if not (PORT_MIN <= port <= PORT_MAX):
        raise ValueError(
            f"Port must be between {PORT_MIN} and {PORT_MAX} — got {port}"
        )
    return port


def resolve_port(argv: Optional[Sequence[str]] = None, env=None) -> int:
    """Decide this process's port: ``--port`` → ``GC_PORT`` → the default.

    Raises ``ValueError`` on a malformed value rather than falling back — a
    typo that silently became 5560 would put a second server on the first
    instance's port.
    """
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    env = os.environ if env is None else env

    for i, arg in enumerate(argv):
        if arg == "--port":
            if i + 1 >= len(argv):
                raise ValueError("--port requires a port number")
            return validate_port(argv[i + 1])
        if arg.startswith("--port="):
            return validate_port(arg.split("=", 1)[1])

    raw = (env.get("GC_PORT") or "").strip()
    if raw:
        return validate_port(raw)
    return DEFAULT_PORT


def active_port(env=None) -> int:
    """The port of the running process, read from ``GC_PORT``.

    Lenient by design: ``settings.py`` calls this at import time and must not
    raise, so a malformed value degrades to the default.
    """
    env = os.environ if env is None else env
    raw = (env.get("GC_PORT") or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return validate_port(raw)
    except ValueError:
        return DEFAULT_PORT


def settings_path(port=None, env=None) -> Path:
    """Config file for *port*. The default port keeps the historic filename."""
    port = active_port(env) if port is None else validate_port(port)
    if port == DEFAULT_PORT:
        return SETTINGS_DIR / f"{SETTINGS_STEM}.json"
    return SETTINGS_DIR / f"{SETTINGS_STEM}-{port}.json"


def pidfile_name(port=None, env=None) -> str:
    """Launcher pidfile name for *port*, relative to the webapp folder.

    Per-port so a second launcher's stale-process cleanup cannot kill the
    first instance's Flask subprocess.
    """
    port = active_port(env) if port is None else validate_port(port)
    if port == DEFAULT_PORT:
        return f"{PIDFILE_STEM}.pid"
    return f"{PIDFILE_STEM}-{port}.pid"


def load_recent_ports() -> Dict[str, object]:
    """Ports the operator has used before, most recent first.

    Launcher-level and shared by every instance — it is a list of choices,
    not per-instance config. Any unreadable or malformed file degrades to
    empty rather than blocking launch.
    """
    fallback: Dict[str, object] = {"recent": [], "last": DEFAULT_PORT}
    try:
        data = json.loads(RECENT_PORTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    if not isinstance(data, dict):
        return fallback

    recent: List[int] = []
    raw_recent = data.get("recent")
    if isinstance(raw_recent, list):
        for item in raw_recent:
            try:
                port = validate_port(item)
            except ValueError:
                continue
            if port not in recent:
                recent.append(port)
    recent = recent[:RECENT_LIMIT]

    try:
        last = validate_port(data.get("last"))
    except ValueError:
        last = recent[0] if recent else DEFAULT_PORT

    return {"recent": recent, "last": last}


def remember_port(port) -> None:
    """Record *port* as the most recently used. Never raises."""
    port = validate_port(port)
    state = load_recent_ports()
    recent = [p for p in state["recent"] if p != port]
    recent.insert(0, port)
    payload = {"recent": recent[:RECENT_LIMIT], "last": port}
    try:
        RECENT_PORTS_PATH.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def port_in_use(port, host: str = "") -> bool:
    """True if *port* cannot be bound — i.e. something is already serving it.

    The launcher spawns Flask with CREATE_NEW_CONSOLE, so without this check a
    port clash kills the server inside a console window the operator never
    sees while the tray icon still looks healthy.

    ``host=""`` means INADDR_ANY, matching ``app.run(host="0.0.0.0")``.
    SO_REUSEADDR is deliberately NOT set: on Windows it permits binding an
    address already in use, which would report a busy port as free.
    """
    port = validate_port(port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False
