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

import os
import sys
from typing import Optional, Sequence

DEFAULT_PORT = 5560
PORT_MIN = 1024
PORT_MAX = 65535


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
