"""labcore_auth.py: who is signing in, according to LabCore (spec D1, rev 2).

The same endpoint COA Reviewer uses (``coa-reviewer/labcore_client.py``
``_login``): ``POST {base}/api/login {username, password}`` → 200
``{username}``, the canonical LabLink account name. A keycard scan is sent as
both username and password. ``base`` is ``LABCORE_URL`` or
``https://labvision.asaplabs.net``.

Stdlib only (urllib). Every request sends ``User-Agent: gc-hub/<version>``
(Cloudflare answers ``403 error code: 1010`` to ``Python-urllib/*``) and
``Accept: application/json``, with a 10 s timeout.

Classification (rev 2): **only a JSON 4xx other than 429 is a failed
sign-in** (``None``). ``LabCoreUnavailable`` for a network/TLS error or
timeout, a 5xx, a 429, a ``cf-mitigated`` header, a 403 whose body is not
JSON (e.g. ``error code: 1010``), or a 200 that is not JSON or has no usable
``username``. "Wrong password" and "couldn't ask" send the person to
different places, and must never be confused.

Nothing here logs; the caller logs the outcome without the password (and,
for a failure, without the raw username).
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Optional

DEFAULT_BASE_URL = "https://labvision.asaplabs.net"
TIMEOUT = 10.0
MAX_NAME = 128
MAX_BODY = 64 * 1024


class LabCoreUnavailable(RuntimeError):
    """LabCore could not be asked (or gave an answer that isn't one)."""


def base_url() -> str:
    return (os.environ.get("LABCORE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


_env_base_url = base_url   # the parameters below are also called base_url


def user_agent() -> str:
    try:
        from version import APP_VERSION
    except Exception:  # noqa: BLE001
        APP_VERSION = "dev"
    return f"gc-hub/{APP_VERSION}"


def _json(data: bytes):
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _login(username: str, password: str, *, base_url: Optional[str],
           timeout: float) -> Optional[str]:
    url = (base_url or _env_base_url()).rstrip("/") + "/api/login"
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": user_agent()})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            status, headers, data = r.status, r.headers, r.read(MAX_BODY)
    except urllib.error.HTTPError as e:
        status, headers = e.code, e.headers
        try:
            data = e.read(MAX_BODY)
        except Exception:  # noqa: BLE001
            data = b""
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        raise LabCoreUnavailable(f"LabCore unreachable at {url}: "
                                 f"{getattr(e, 'reason', e) or type(e).__name__}") from None
    except Exception as e:  # noqa: BLE001 - RemoteDisconnected, IncompleteRead, ...
        raise LabCoreUnavailable(f"LabCore unreachable at {url}: {type(e).__name__}") from None

    if headers is not None and headers.get("cf-mitigated"):
        raise LabCoreUnavailable(f"Cloudflare challenged the sign-in request to {url}")
    if status >= 500 or status == 429:
        raise LabCoreUnavailable(f"LabCore answered HTTP {status}")
    parsed = _json(data)
    if status != 200:
        if 400 <= status < 500 and parsed is not None:
            return None                            # a real "no"
        raise LabCoreUnavailable(f"LabCore answered HTTP {status} without JSON "
                                 f"({data[:40]!r})")
    if not isinstance(parsed, dict):
        raise LabCoreUnavailable("LabCore answered 200 without JSON")
    name = parsed.get("username")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
        raise LabCoreUnavailable("LabCore answered 200 without a usable username")
    return name.strip()


def authenticate_user(username: str, password: str, *, base_url: Optional[str] = None,
                      timeout: float = TIMEOUT) -> Optional[str]:
    """The canonical account name, ``None`` for wrong credentials (or a blank
    field, which is never sent), or ``LabCoreUnavailable``."""
    username = (username or "").strip() if isinstance(username, str) else ""
    if not username or not isinstance(password, str) or not password:
        return None
    return _login(username, password, base_url=base_url, timeout=timeout)


def authenticate_card(code: str, *, base_url: Optional[str] = None,
                      timeout: float = TIMEOUT) -> Optional[str]:
    """A keycard scan (a keyboard wedge types the code): sent as both fields."""
    code = (code or "").strip() if isinstance(code, str) else ""
    if not code:
        return None
    return _login(code, code, base_url=base_url, timeout=timeout)
