"""The agent's side of the hub HTTP contract (§1), stdlib urllib only.

Every request carries ``Authorization: Bearer <token>``, ``User-Agent:
gc-agent/<version>`` and no ``Origin``. The hub is reached over the internet
(``https://gc.asaplabs.net``, a Cloudflare tunnel), so:

* **User-Agent.** Cloudflare answers ``403 error code: 1010`` to urllib's
  default ``Python-urllib/*``; the agent always names itself.
* **TLS** is verified with the default ``ssl`` context (the system store).
* **Timeouts**: the TCP/TLS connect gets ``CONNECT_TIMEOUT`` (10 s), then the
  socket's read timeout is the call's (``DEFAULT_TIMEOUT`` 30 s,
  ``INGEST_TIMEOUT`` 95 s for uploads and the package zip, under
  Cloudflare's 100 s origin limit).
* **Cloudflare blocks** (a challenge, a WAF or bot rule) are recognised
  (``HubResponse.cloudflare_blocked``): a ``cf-mitigated`` header, or a
  403/503 with ``cf-ray`` whose body is HTML or ``error code: N``. Their
  ``error_text()`` is ``CLOUDFLARE_BLOCKED``, and the client remembers the
  last one (``HubClient.cloudflare_blocked``) so the tray can show it.
* **LAN fallback**: with a ``lan_url`` (``install.json``/``agent.json``), a
  request that fails with a network or TLS error against ``hub_url`` is
  tried once against ``lan_url``. An HTTP status (any, including a
  Cloudflare block) never falls back.

HTTP error statuses come back as a HubResponse; only transport failures
(refused, reset, timeout, DNS, TLS) raise NetworkError. Proxies are ignored.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import util

log = logging.getLogger("gc_agent.client")

CONNECT_TIMEOUT = 10
DEFAULT_TIMEOUT = 30
INGEST_TIMEOUT = 95
USER_AGENT_PREFIX = "gc-agent/"
CLOUDFLARE_BLOCKED = "Cloudflare blocked the agent: add the WAF skip rule in DEPLOY.md"
_CF_ERROR_CODE = re.compile(br"^\s*error code: \d+")


class NetworkError(Exception):
    pass


def _header(headers, name):
    name = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return v
    return None


def is_cloudflare_block(status, headers, body):
    """True for an answer from Cloudflare's edge rather than the hub: a
    ``cf-mitigated`` header (a challenge), or a 403/503 carrying ``cf-ray``
    whose body is HTML or ``error code: N`` (the hub itself answers JSON)."""
    if _header(headers, "cf-mitigated") is not None:
        return True
    if status not in (403, 503) or _header(headers, "cf-ray") is None:
        return False
    ctype = (_header(headers, "content-type") or "").lower()
    head = (body or b"")[:64].lstrip()
    return "text/html" in ctype or head.startswith(b"<") or bool(_CF_ERROR_CODE.match(head))


class HubResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.cloudflare_blocked = is_cloudflare_block(status, self.headers, body)
        try:
            self.json = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            self.json = None

    def error_text(self):
        if self.cloudflare_blocked:
            return CLOUDFLARE_BLOCKED
        if isinstance(self.json, dict) and isinstance(self.json.get("error"), str):
            return self.json["error"]
        return "HTTP %d" % self.status

    def __repr__(self):
        return "HubResponse(%d, %r)" % (self.status, self.json)


# ── connections: a connect timeout separate from the read timeout ─────────

def _connect_timeout(read):
    if isinstance(read, (int, float)) and not isinstance(read, bool):
        return min(CONNECT_TIMEOUT, read)
    return CONNECT_TIMEOUT


class _HTTPConnection(http.client.HTTPConnection):
    def connect(self):
        read = self.timeout
        self.timeout = _connect_timeout(read)
        try:
            super().connect()
        finally:
            self.timeout = read
        if self.sock is not None and isinstance(read, (int, float)):
            self.sock.settimeout(read)


class _HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        read = self.timeout
        self.timeout = _connect_timeout(read)        # TCP connect and TLS handshake
        try:
            super().connect()
        finally:
            self.timeout = read
        if self.sock is not None and isinstance(read, (int, float)):
            self.sock.settimeout(read)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__(context=ssl.create_default_context())

    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


def build_opener():
    """No proxies, the timeouts above, TLS verified with the system store."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler(),
                                       _HTTPSHandler())


class HubClient:
    def __init__(self, hub_url, token, timeout=DEFAULT_TIMEOUT, version=None, lan_url=""):
        self.hub_url = hub_url.rstrip("/")
        self.lan_url = (lan_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout
        self.user_agent = USER_AGENT_PREFIX + (version or "unknown")
        self.cloudflare_blocked = False      # the last HTTP answer was Cloudflare's block
        self.using_lan = False               # the last request went to lan_url
        self._opener = build_opener()

    def _once(self, base, method, path, body, headers, timeout):
        req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as r:
                return HubResponse(r.status, r.read(), dict(r.headers.items()))
        except urllib.error.HTTPError as e:
            try:
                data = e.read()
            except Exception:
                data = b""
            return HubResponse(e.code, data, dict(e.headers.items()) if e.headers else {})
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            raise NetworkError(str(getattr(e, "reason", e)) or type(e).__name__)
        except Exception as e:  # http.client.RemoteDisconnected, IncompleteRead, ...
            raise NetworkError("%s: %s" % (type(e).__name__, e))

    def _request(self, method, path, body=None, headers=None, timeout=None):
        h = {"Authorization": "Bearer " + self.token, "Accept": "application/json",
             "User-Agent": self.user_agent}
        h.update(headers or {})
        try:
            r = self._once(self.hub_url, method, path, body, h, timeout)
            if self.using_lan:
                log.info("%s answers again", self.hub_url)
                self.using_lan = False
        except NetworkError as exc:
            if not self.lan_url or self.lan_url == self.hub_url:
                raise
            if not self.using_lan:        # logged once per switch, not per request
                log.warning("%s unreachable (%s); using the LAN address %s", self.hub_url,
                            exc, self.lan_url)
            try:
                r = self._once(self.lan_url, method, path, body, h, timeout)
            except NetworkError as exc2:
                raise NetworkError("%s; LAN %s: %s" % (exc, self.lan_url, exc2))
            self.using_lan = True
        if r.cloudflare_blocked and not self.cloudflare_blocked:
            log.warning("%s %s: %s (HTTP %d, cf-ray %s)", method, path, CLOUDFLARE_BLOCKED,
                        r.status, _header(r.headers, "cf-ray"))
        self.cloudflare_blocked = r.cloudflare_blocked
        return r

    def ingest(self, filename, data, sha256, mtime_ts):
        headers = {
            "X-GC-SHA256": sha256.lower(),
            "X-GC-Mtime": util.local_iso(mtime_ts),
            "X-GC-Filename": urllib.parse.quote(os.path.basename(filename)),
            "Content-Type": "application/octet-stream",
        }
        return self._request("POST", "/api/ingest", data, headers, timeout=INGEST_TIMEOUT)

    def heartbeat(self, payload):
        body = json.dumps(payload).encode("utf-8")
        return self._request("POST", "/api/agent/heartbeat", body,
                             {"Content-Type": "application/json"})

    def results(self, after, limit=500):
        q = urllib.parse.urlencode({"after": int(after), "limit": int(limit)})
        return self._request("GET", "/api/agent/results?" + q)

    def package_info(self):
        return self._request("GET", "/api/agent/package")

    def package_zip(self):
        return self._request("GET", "/api/agent/package.zip", timeout=INGEST_TIMEOUT)
