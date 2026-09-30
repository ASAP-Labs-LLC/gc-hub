"""LEM's machine list for the Instruments page's dropdown (spec D10).

LEM serves its machines without a session at ``GET <lem_url>/api/machines``
(``{machines: [{machine_uid, title, status, closed_reason, ...}], stale,
labcore_online, ...}``). The browser never calls LEM (no CORS):
``GET /api/lem/machines`` (``instruments_api``) asks this module, which
fetches the list server-side. **Read-only: nothing here ever writes to LEM.**

The fetch (``fetch``):

* stdlib only (urllib; plus the local ``version``); no system proxy
  (``ProxyHandler({})``), redirects not followed;
* **a hard deadline**: the request runs on a daemon thread and the caller
  waits at most ``timeout`` (8 s). The socket timeout alone would not do:
  it restarts with every byte, so a server trickling its headers or body a
  byte at a time could hold a fetch for ever. At the deadline the socket is
  shut down (which ends the thread) and ``TimeoutError`` is raised. The body
  is read with ``read1`` and the deadline checked on every read. (DNS
  resolution is not interruptible; the caller still stops waiting.)
* at most 1 MB read; a body shorter than its Content-Length is a failure;
* ``User-Agent: gc-hub/<version>`` and ``Accept: application/json``:
  Cloudflare in front of lem.asaplabs.net refuses urllib's default agent
  (403 "error code: 1010"). Any non-200 answer, or one carrying a
  ``cf-mitigated`` header (a challenge page), is a failure;
* machines are normalised to ``{uid, title, status, closed}`` and sorted by
  title, at most 500: an entry whose uid is not a string matching
  ``^[A-Za-z0-9_-]{1,128}$`` is dropped (so is a repeated uid); title and
  status lose control/format characters (Unicode Cc, Cf) and lone
  surrogates and are capped at 200/32 characters; ``closed`` is
  ``bool(closed_reason)`` (``status`` is a QC colour,
  RED/GREEN/YELLOW/UNKNOWN). Titles stay untrusted: the page inserts them
  with ``textContent``;
* LEM's own ``stale`` and ``labcore_online`` flags are passed through
  (``None`` when absent, not booleans, or there is no answer).

The cache (``MachineCache.get(url)``, thread-safe): one answer per URL,
**live** for 60 s. At most one fetch is in flight. While one is, or within
30 s of a failed one, a caller gets the last good answer at once
(``cached``) or, with none, ``unavailable`` at once; a caller with no
answer to fall back on waits for the fetch in flight (for a different URL,
once, then gives up). The answer is
``{machines, source, age_seconds, stale, labcore_online[, error]}``:
``source`` is ``live`` (at most 60 s old), ``cached`` (older; ``error`` when
LEM failed) or ``unavailable`` (``machines`` ``[]``, ``age_seconds``
``None``, ``error``). ``error`` is a short generic sentence, never a URL or
exception text (those go to the log).

``lem_url`` (settings, admin-only; ``valid_setting_url``: not this machine,
not link-local) and the ``LEM_URL`` environment variable (operator-set,
``valid_url``) name LEM; ``resolve_url`` picks one (env wins) and ignores an
invalid value.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import unicodedata
import urllib.request
from typing import Callable, Mapping, Optional

import version

log = logging.getLogger("lem_machines")

DEFAULT_URL = "https://lem.asaplabs.net"
ENV_VAR = "LEM_URL"
PATH = "/api/machines"
TIMEOUT_SECONDS = 8
MAX_BYTES = 1024 * 1024
MAX_MACHINES = 500
CACHE_SECONDS = 60
RETRY_AFTER_FAILURE_SECONDS = 30
TITLE_MAX, STATUS_MAX = 200, 32
UID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
FLAGS = ("stale", "labcore_online")
USER_AGENT = f"gc-hub/{version.APP_VERSION}"

ERR_UNREACHABLE = "LEM could not be reached."
ERR_UNREADABLE = "LEM's answer could not be read."

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_URL_RE = re.compile(rf"https?://({_LABEL}(?:\.{_LABEL})*)(?::(\d{{1,5}}))?")


# ── the address ─────────────────────────────────────────────────────────────

def valid_url(value) -> bool:
    """A bare ``http(s)://host[:port]``: no path, query, credentials or
    whitespace; the port 1–65535."""
    if not isinstance(value, str):
        return False
    m = _URL_RE.fullmatch(value)
    if not m:
        return False
    return m.group(2) is None or 1 <= int(m.group(2)) <= 65535


def _local_host(host: str) -> bool:
    """This machine, link-local or unspecified: ``localhost`` (and
    ``*.localhost``) or an IPv4 address in any form the resolver accepts
    (``127.1``, ``2130706433``, ``0x7f.1``, ``0``)."""
    h = host.lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.IPv4Address(socket.inet_aton(h))
    except (OSError, ValueError):
        return False
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified


def valid_setting_url(value) -> bool:
    """``valid_url`` and not a loopback, link-local or unspecified host: the
    ``lem_url`` setting (saved over HTTP) must not point the hub's
    server-side fetch at itself or at a metadata service."""
    if not valid_url(value):
        return False
    return not _local_host(_URL_RE.fullmatch(value).group(1))


def resolve_url(conf: Optional[Mapping] = None, env: Optional[Mapping] = None) -> str:
    """``LEM_URL`` if valid, else the ``lem_url`` setting if valid (for a
    setting), else the default. An invalid value is logged and skipped,
    never fetched."""
    env = os.environ if env is None else env
    for source, value, ok in ((ENV_VAR, env.get(ENV_VAR), valid_url),
                              ("lem_url", (conf or {}).get("lem_url"), valid_setting_url)):
        if value in (None, ""):
            continue
        if ok(value):
            return value
        log.warning("Ignoring an invalid %s (expected http(s)://host[:port])", source)
    return DEFAULT_URL


# ── one fetch ───────────────────────────────────────────────────────────────

class _Unreadable(Exception):
    pass


class _Sockets:
    """The sockets one fetch opened, so the deadline can shut them down."""

    def __init__(self):
        self._lock = threading.Lock()
        self._socks: list = []
        self._aborted = False

    def add(self, sock) -> None:
        with self._lock:
            self._socks.append(sock)
            aborted = self._aborted
        if aborted:
            self._kill(sock)

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            socks = list(self._socks)
        for s in socks:
            self._kill(s)

    @staticmethod
    def _kill(sock) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def _tracked(base, holder: _Sockets):
    class Conn(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            create = self._create_connection

            def create_tracked(*a, **kw):
                sock = create(*a, **kw)
                holder.add(sock)
                return sock
            self._create_connection = create_tracked
    return Conn


class _HTTP(urllib.request.HTTPHandler):
    def __init__(self, holder):
        super().__init__()
        self._holder = holder

    def http_open(self, req):
        return self.do_open(_tracked(http.client.HTTPConnection, self._holder), req)


class _HTTPS(urllib.request.HTTPSHandler):
    def __init__(self, holder):
        super().__init__()
        self._holder = holder

    def https_open(self, req):
        return self.do_open(_tracked(http.client.HTTPSConnection, self._holder), req,
                            context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # urllib hook: never follow
        return None


def _opener(holder: _Sockets):
    """No system proxy, no redirects, every socket tracked."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect,
                                       _HTTP(holder), _HTTPS(holder))


_STRIP = frozenset(("Cc", "Cf", "Cs"))


def _text(value, cap: int) -> str:
    if not isinstance(value, str):
        return ""
    clean = "".join(ch for ch in value if unicodedata.category(ch) not in _STRIP)
    return clean.strip()[:cap]


def normalise(payload) -> list:
    """LEM's ``/api/machines`` body → ``[{uid, title, status, closed}]``
    sorted by title, at most ``MAX_MACHINES``. Raises ``_Unreadable`` when
    there is no machine list."""
    if not isinstance(payload, dict) or not isinstance(payload.get("machines"), list):
        raise _Unreadable("no machines list")
    out, seen = [], set()
    for m in payload["machines"]:
        if len(out) >= MAX_MACHINES:
            break
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


def _fetch_once(base_url: str, timeout: float, max_bytes: int, holder: _Sockets):
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(base_url + PATH, headers={"Accept": "application/json",
                                                           "User-Agent": USER_AGENT})
    with _opener(holder).open(req, timeout=timeout) as resp:
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
            chunk = resp.read1(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise _Unreadable("answer too large")
        if getattr(resp, "length", None):
            raise _Unreadable(f"answer ended {resp.length} bytes short")
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _Unreadable(f"not JSON: {type(exc).__name__}") from None
    return normalise(payload), flags(payload)


def fetch(base_url: str, timeout: float = TIMEOUT_SECONDS, max_bytes: int = MAX_BYTES):
    """One GET of ``<base_url>/api/machines`` → ``(machines, flags)``, ending
    within ``timeout`` whatever the server does. Raises on any failure
    (``_Unreadable`` for a bad answer, ``TimeoutError`` at the deadline)."""
    holder = _Sockets()
    box: dict = {}

    def run():
        try:
            box["ok"] = _fetch_once(base_url, timeout, max_bytes, holder)
        except BaseException as exc:  # handed to the caller
            box["err"] = exc

    worker = threading.Thread(target=run, name="lem-fetch", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        holder.abort()                # ends the stuck read; the thread exits
        raise TimeoutError(f"no complete answer within {timeout} s")
    if "err" in box:
        raise box["err"]
    return box["ok"]


# ── the cache ───────────────────────────────────────────────────────────────

class MachineCache:
    """See the module doc."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 ttl: float = CACHE_SECONDS, timeout: float = TIMEOUT_SECONDS,
                 max_bytes: int = MAX_BYTES,
                 retry_after: float = RETRY_AFTER_FAILURE_SECONDS):
        self._clock = clock
        self._ttl = ttl
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._retry_after = retry_after
        self._lock = threading.Lock()
        self._url: Optional[str] = None        # whose answer is cached
        self._machines: Optional[list] = None
        self._flags: dict = dict.fromkeys(FLAGS)
        self._fetched_at = 0.0
        self._inflight: Optional[threading.Event] = None
        self._inflight_url: Optional[str] = None
        self._failed_url: Optional[str] = None  # the latest failed fetch
        self._failed_at = 0.0
        self._last_error: Optional[str] = None

    # called with the lock held
    def _has(self, url: str) -> bool:
        return self._url == url and self._machines is not None

    def _answer(self, url: str, error: Optional[str]) -> dict:
        if not self._has(url):
            return dict({"machines": [], "source": "unavailable", "age_seconds": None,
                         "error": error or ERR_UNREACHABLE}, **dict.fromkeys(FLAGS))
        age = max(0.0, self._clock() - self._fetched_at)
        out = {"machines": [dict(m) for m in self._machines],
               "source": "live" if age <= self._ttl else "cached",
               "age_seconds": round(age), **self._flags}
        if out["source"] == "cached" and error:
            out["error"] = error
        return out

    def title(self, url: str, uid: Optional[str]) -> Optional[str]:
        """The cached title of machine ``uid`` from ``url``'s list, or None.
        Never fetches (the setup guide must not wait on LEM)."""
        if not uid:
            return None
        with self._lock:
            if not self._has(url):
                return None
            for m in self._machines:
                if m.get("uid") == uid:
                    return m.get("title") or None
        return None

    def _recent_failure(self, url: str) -> bool:
        return (self._failed_url == url
                and self._clock() - self._failed_at < self._retry_after)

    def get(self, url: str) -> dict:
        waited = False
        while True:
            with self._lock:
                if self._has(url) and self._clock() - self._fetched_at <= self._ttl:
                    return self._answer(url, None)
                if self._recent_failure(url):
                    return self._answer(url, self._last_error)
                if self._inflight is None:
                    event = self._inflight = threading.Event()
                    self._inflight_url = url
                    break
                if self._has(url):              # stale while a fetch is in flight
                    return self._answer(url, None)
                if waited:                      # still busy (another URL): give up
                    return self._answer(url, ERR_UNREACHABLE)
                event = self._inflight
            event.wait(self._timeout + 1)
            waited = True
        error = None
        try:
            machines, lem_flags = fetch(url, self._timeout, self._max_bytes)
        except _Unreadable as exc:
            log.warning("LEM machine list from %s unreadable: %s", url, exc)
            error = ERR_UNREADABLE
        except BaseException as exc:  # network, HTTP status, deadline
            log.warning("LEM machine list from %s failed: %s: %s", url,
                        type(exc).__name__, exc)
            error = ERR_UNREACHABLE
        with self._lock:
            if error is None:
                self._url, self._machines, self._fetched_at = url, machines, self._clock()
                self._flags = lem_flags
                if self._failed_url == url:
                    self._failed_url = None
            else:
                self._failed_url, self._failed_at = url, self._clock()
            self._last_error = error
            self._inflight = None
            self._inflight_url = None
            event.set()
            return self._answer(url, error)


CACHE = MachineCache()


def machines(url: str) -> dict:
    """The process-wide cache's answer for ``url``."""
    return CACHE.get(url)
