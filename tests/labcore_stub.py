"""A local stand-in for LabCore's ``POST /api/login`` (never the real one).

``LabCoreStub()`` serves on 127.0.0.1 in a thread; ``.url`` is its base URL.
``accounts`` maps username → (password, canonical name); a registered card
code logs in as ``cards[code]`` when sent as both username and password (as
COA's ``authenticate_card`` does). ``mode`` switches the reply: ``"ok"`` (the
real behaviour), ``"500"``, ``"429"``, ``"cf-1010"`` (Cloudflare's plain-text
403 ``error code: 1010``), ``"cf-challenge"`` (403 with ``cf-mitigated``),
``"html200"`` (a 200 that isn't JSON), ``"no-username"`` (JSON 200 without it),
``"slow"`` (sleeps ``delay`` seconds). Every request is recorded in
``requests`` as ``(headers dict, body dict)``.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LabCoreStub:
    def __init__(self, accounts=None, cards=None):
        self.accounts = dict(accounts or {"ryan c": ("labpass-1", "Ryan C"),
                                          "jane doe": ("labpass-2", "Jane Doe")})
        self.cards = dict(cards or {"CARD-0042": "Ryan C"})
        self.mode = "ok"
        self.delay = 0.0
        self.requests: list = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code, body, ctype="application/json", headers=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                stub.requests.append((dict(self.headers.items()), body))
                if self.path != "/api/login":
                    return self._send(404, {"error": "not found"})
                mode = stub.mode
                if mode == "slow":
                    time.sleep(stub.delay)
                if mode == "500":
                    return self._send(500, {"error": "boom"})
                if mode == "429":
                    return self._send(429, {"error": "slow down"})
                if mode == "cf-1010":
                    return self._send(403, b"error code: 1010", "text/plain; charset=UTF-8",
                                      {"cf-ray": "8c-DFW"})
                if mode == "cf-challenge":
                    return self._send(403, b"<!DOCTYPE html><title>Just a moment...</title>",
                                      "text/html", {"cf-mitigated": "challenge",
                                                    "cf-ray": "8c-DFW"})
                if mode == "html200":
                    return self._send(200, b"<html>login</html>", "text/html")
                if mode == "no-username":
                    return self._send(200, {"ok": True})
                user = str(body.get("username", ""))
                pw = str(body.get("password", ""))
                if user and user == pw and user in stub.cards:
                    return self._send(200, {"username": stub.cards[user]})
                acct = stub.accounts.get(user.strip().lower())
                if acct and acct[0] == pw:
                    return self._send(200, {"username": acct[1]})
                if not user or not pw:
                    return self._send(400, {"error": "username and password required"})
                return self._send(401, {"error": "Invalid credentials"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._t.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
