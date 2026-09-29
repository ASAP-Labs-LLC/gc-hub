"""The tray's HTTP client for the hub (stdlib urllib).

Requests carry no ``Origin``/``Sec-Fetch-Site`` (the hub's cross-site guard
lets such non-browser clients through) and go to 127.0.0.1, which is what
hub_control's loopback-only routes require.

Neither call ever raises: a broken or truncated response (``http.client``'s
BadStatusLine, IncompleteRead, ...), a socket error or undecodable JSON is
"the hub did not answer", so the poll thread survives anything on the wire.
"""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Optional, Tuple

STATUS_PATH = "/api/hub/status"
MAX_RESPONSE = 1024 * 1024
_NETWORK_ERRORS = (OSError, http.client.HTTPException, ValueError)


class HubClient:
    def __init__(self, base_url: str, *, timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # never through a proxy: this is the machine talking to itself
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def status(self) -> Optional[dict]:
        """``/api/hub/status``'s JSON, or None when the hub does not answer
        properly."""
        try:
            with self._opener.open(self.base_url + STATUS_PATH, timeout=self.timeout) as r:
                body = json.loads(r.read(MAX_RESPONSE) or b"null")
        except _NETWORK_ERRORS:
            return None
        except Exception:  # noqa: BLE001 - never kill the caller's thread
            return None
        return body if isinstance(body, dict) else None

    def post(self, path: str, body: dict, *, timeout: float = 30.0) -> Tuple[Optional[int], dict]:
        """``(status, JSON object)``; ``(None, {"error": ...})`` when the hub
        cannot be reached or the answer is broken."""
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode("utf-8"),
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            try:
                with self._opener.open(req, timeout=timeout) as r:
                    code, raw = r.status, r.read(MAX_RESPONSE)
            except urllib.error.HTTPError as e:
                code, raw = e.code, e.read(MAX_RESPONSE)
        except Exception as e:  # noqa: BLE001 - OSError, HTTPException, ...
            return None, {"error": f"The hub did not answer properly ({type(e).__name__}: {e})."}
        try:
            parsed = json.loads(raw or b"null")
        except ValueError:
            parsed = None
        return code, parsed if isinstance(parsed, dict) else {}
