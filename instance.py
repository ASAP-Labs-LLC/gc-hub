"""instance.py — the hub's TCP port.

``resolve_port`` decides it (the updater's ``PORT`` wins, then ``--port``,
then ``GC_PORT``, then 5560); ``port_in_use`` probes it. Stdlib only and free
of import-time side effects, so the app, the tools and the tests can import
it. (v1's per-port settings files, pidfiles and remembered ports went with
``run.pyw`` in v2; spec D14.)
"""
from __future__ import annotations

import os
import socket
import sys
from typing import Optional, Sequence

DEFAULT_PORT = 5560
PORT_MIN = 1024
PORT_MAX = 65535


def validate_port(value) -> int:
    """Coerce *value* to a usable TCP port, or raise ``ValueError``."""
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
    """Decide this process's port: ``PORT`` (set by the ASAPSV1 updater, which
    launches the app with no ``--port`` and expects exactly the port it chose
    for the health check / switch handshake) → ``--port`` → ``GC_PORT`` →
    ``DEFAULT_PORT``.

    Raises ``ValueError`` on a malformed value rather than falling back: a
    typo that silently became 5560 would put a second server on the live
    port.
    """
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    env = os.environ if env is None else env

    raw_port_env = (env.get("PORT") or "").strip()
    if raw_port_env:
        return validate_port(raw_port_env)

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


def port_in_use(port, host: str = "") -> bool:
    """True if *port* cannot be bound — i.e. something is already serving it.

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
