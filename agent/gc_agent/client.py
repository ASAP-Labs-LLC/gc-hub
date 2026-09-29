"""The agent's side of the hub HTTP contract (§1), stdlib urllib only.

Every request carries ``Authorization: Bearer <token>`` and no ``Origin``.
HTTP error statuses come back as a HubResponse; only transport failures
(refused, reset, timeout, DNS) raise NetworkError. Proxies are ignored: the
hub is on the LAN.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import util

INGEST_TIMEOUT = 120
DEFAULT_TIMEOUT = 30


class NetworkError(Exception):
    pass


class HubResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        try:
            self.json = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            self.json = None

    def error_text(self):
        if isinstance(self.json, dict) and isinstance(self.json.get("error"), str):
            return self.json["error"]
        return "HTTP %d" % self.status

    def __repr__(self):
        return "HubResponse(%d, %r)" % (self.status, self.json)


class HubClient:
    def __init__(self, hub_url, token, timeout=DEFAULT_TIMEOUT):
        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method, path, body=None, headers=None, timeout=None):
        h = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(self.hub_url + path, data=body, headers=h, method=method)
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
