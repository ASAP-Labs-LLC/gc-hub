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
