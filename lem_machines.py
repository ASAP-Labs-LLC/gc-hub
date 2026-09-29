"""LEM's machine list for the Instruments page's dropdown (spec D10).

LEM serves its machines without a session at ``GET <lem_url>/api/machines``
(``{machines: [{machine_uid, title, status, closed_reason, ...}], stale,
labcore_online, ...}``). The browser never calls LEM (no CORS):
``GET /api/lem/machines`` (``instruments_api``) asks this module, which
fetches the list server-side. **Read-only: nothing here ever writes to LEM.**

* stdlib only (urllib; plus the local ``version``), an 8 s timeout (also a
  deadline for the whole read), at most 1 MB read, redirects not followed;
* it sends ``User-Agent: gc-hub/<version>`` and ``Accept: application/json``:
  Cloudflare in front of lem.asaplabs.net refuses urllib's default agent
  (403 "error code: 1010"). Any non-200 answer, or one carrying a
  ``cf-mitigated`` header (a challenge page), counts as a failure;
* one answer cached for 60 s per URL; when LEM fails, the last good answer is
  served stale;
* thread-safe, one fetch in flight at a time: concurrent callers wait for it
  and share its outcome;
* machines are normalised to ``{uid, title, status, closed}`` and sorted by
  title: an entry whose uid is not a string matching ``^[A-Za-z0-9_-]{1,128}$``
  is dropped (so is a repeated uid); title and status are capped at 200/32
  characters; ``closed`` is ``bool(closed_reason)`` (``status`` is a QC
  colour, RED/GREEN/YELLOW/UNKNOWN). Titles are untrusted: the page inserts
  them with ``textContent``;
* LEM's own ``stale`` and ``labcore_online`` flags are passed through
  (``None`` when absent, not booleans, or there is no answer).

``MachineCache.get(url)`` returns
``{machines, source, age_seconds, stale, labcore_online[, error]}`` where
``source`` is ``live`` (an answer at most 60 s old), ``cached`` (older,
because LEM failed just now; ``error`` says so) or ``unavailable`` (no answer
at all; ``machines`` is ``[]``, ``age_seconds`` is ``None``). ``error`` is a
short generic sentence, never a URL or exception text (those go to the log).

``lem_url`` (settings, admin-only) and the ``LEM_URL`` environment variable
name LEM; ``resolve_url`` picks one (env wins) and ignores an invalid value.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from typing import Callable, Mapping, Optional

import version

log = logging.getLogger("lem_machines")

DEFAULT_URL = "https://lem.asaplabs.net"
ENV_VAR = "LEM_URL"
PATH = "/api/machines"
TIMEOUT_SECONDS = 8
MAX_BYTES = 1024 * 1024
CACHE_SECONDS = 60
TITLE_MAX, STATUS_MAX = 200, 32
UID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
FLAGS = ("stale", "labcore_online")
USER_AGENT = f"gc-hub/{version.APP_VERSION}"

ERR_UNREACHABLE = "LEM could not be reached."
ERR_UNREADABLE = "LEM's answer could not be read."

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_URL_RE = re.compile(rf"https?://({_LABEL}(?:\.{_LABEL})*)(?::(\d{{1,5}}))?")


def valid_url(value) -> bool:
    """A bare ``http(s)://host[:port]``: no path, query, credentials or
    whitespace; the port 1–65535."""
    if not isinstance(value, str):
        return False
    m = _URL_RE.fullmatch(value)
    if not m:
        return False
    return m.group(2) is None or 1 <= int(m.group(2)) <= 65535


def resolve_url(conf: Optional[Mapping] = None, env: Optional[Mapping] = None) -> str:
    """``LEM_URL`` if valid, else the ``lem_url`` setting if valid, else the
    default. An invalid value is logged and skipped, never fetched."""
    env = os.environ if env is None else env
    for source, value in ((ENV_VAR, env.get(ENV_VAR)), ("lem_url", (conf or {}).get("lem_url"))):
        if value in (None, ""):
            continue
        if valid_url(value):
            return value
        log.warning("Ignoring an invalid %s (expected http(s)://host[:port])", source)
    return DEFAULT_URL


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # urllib hook: never follow
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class _Unreadable(Exception):
    pass


def _text(value, cap: int) -> str:
    return value.strip()[:cap] if isinstance(value, str) else ""


def normalise(payload) -> list:
    """LEM's ``/api/machines`` body → ``[{uid, title, status, closed}]``
    sorted by title. Raises ``_Unreadable`` when there is no machine list."""
    if not isinstance(payload, dict) or not isinstance(payload.get("machines"), list):
        raise _Unreadable("no machines list")
    out, seen = [], set()
    for m in payload["machines"]:
        if not isinstance(m, dict):
            continue
        uid = m.get("machine_uid")
        if not isinstance(uid, str) or not UID_RE.fullmatch(uid) or uid in seen:
            continue
        seen.add(uid)
        out.append({"uid": uid, "title": _text(m.get("title"), TITLE_MAX),
                    "status": _text(m.get("status"), STATUS_MAX),
                    "closed": bool(m.get("closed_reason"))})
    out.sort(key=lambda r: (r["title"].casefold(), r["title"], r["uid"]))
    return out


def flags(payload) -> dict:
    """LEM's ``stale``/``labcore_online``, each a bool or ``None``."""
    return {k: payload.get(k) if isinstance(payload.get(k), bool) else None for k in FLAGS}


def fetch(base_url: str, timeout: float = TIMEOUT_SECONDS, max_bytes: int = MAX_BYTES):
    """One GET of ``<base_url>/api/machines`` → ``(machines, flags)``.
    Raises on any failure (``_Unreadable`` for a bad answer)."""
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(base_url + PATH, headers={"Accept": "application/json",
                                                           "User-Agent": USER_AGENT})
    with _OPENER.open(req, timeout=timeout) as resp:
        mitigated = resp.headers.get("cf-mitigated")
        if resp.status != 200 or mitigated:
            raise _Unreadable(f"HTTP {resp.status}, cf-mitigated={mitigated!r}")
        length = resp.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise _Unreadable("answer too large")
        chunks, total = [], 0
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("read deadline")
            chunk = resp.read(min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise _Unreadable("answer too large")
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _Unreadable("not JSON") from exc
    return normalise(payload), flags(payload)


class MachineCache:
    """The 60 s cache with a single fetch in flight (see the module doc)."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 ttl: float = CACHE_SECONDS, timeout: float = TIMEOUT_SECONDS,
                 max_bytes: int = MAX_BYTES):
        self._clock = clock
        self._ttl = ttl
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._url: Optional[str] = None        # whose answer is cached
        self._machines: Optional[list] = None
        self._flags: dict = dict.fromkeys(FLAGS)
        self._fetched_at = 0.0
        self._inflight: Optional[threading.Event] = None
        self._inflight_url: Optional[str] = None
        self._last_error: Optional[str] = None  # the latest fetch's outcome

    def _answer(self, url: str, error: Optional[str]) -> dict:
        # called with the lock held
        if self._url != url or self._machines is None:
            return dict({"machines": [], "source": "unavailable", "age_seconds": None,
                         "error": error or ERR_UNREACHABLE}, **dict.fromkeys(FLAGS))
        age = max(0.0, self._clock() - self._fetched_at)
        out = {"machines": [dict(m) for m in self._machines],
               "source": "live" if age <= self._ttl else "cached",
               "age_seconds": round(age), **self._flags}
        if out["source"] == "cached":
            out["error"] = error or ERR_UNREACHABLE
        return out

    def _fresh(self, url: str) -> bool:
        return (self._url == url and self._machines is not None
                and self._clock() - self._fetched_at <= self._ttl)

    def get(self, url: str) -> dict:
        while True:
            with self._lock:
                if self._fresh(url):
                    return self._answer(url, None)
                if self._inflight is None:
                    event = self._inflight = threading.Event()
                    self._inflight_url = url
                    break
                event, same = self._inflight, self._inflight_url == url
            # another fetch is in flight: wait for it; share its outcome when
            # it is for this URL, else try again once it is done
            event.wait(self._timeout + 5)
            if same:
                with self._lock:
                    return self._answer(url, self._last_error)
        error = None
        try:
            machines, lem_flags = fetch(url, self._timeout, self._max_bytes)
        except _Unreadable as exc:
            log.warning("LEM machine list from %s unreadable: %s", url, exc)
            error = ERR_UNREADABLE
        except Exception as exc:  # network, HTTP status, timeout
            log.warning("LEM machine list from %s failed: %s", url, exc)
            error = ERR_UNREACHABLE
        with self._lock:
            if error is None:
                self._url, self._machines, self._fetched_at = url, machines, self._clock()
                self._flags = lem_flags
            self._last_error = error
            self._inflight = None
            self._inflight_url = None
            event.set()
            return self._answer(url, error)


CACHE = MachineCache()


def machines(url: str) -> dict:
    """The process-wide cache's answer for ``url``."""
    return CACHE.get(url)
