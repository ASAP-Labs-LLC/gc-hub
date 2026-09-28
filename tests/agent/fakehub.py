"""A fake gc-hub implementing the §1 agent contract, for the agent's tests.

Runs a ThreadingHTTPServer on 127.0.0.1 in a daemon thread. Every request is
recorded (method, path, headers, body). Behaviour is scripted through plain
attributes so a test can make any route answer any status.
"""
from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CSV_HEADER = [
    "Lab ID",
    "InjectionDateTime",
    "2887 IBP", "2887 T5", "2887 T10", "2887 T20", "2887 T30", "2887 T40",
    "2887 T50", "2887 T60", "2887 T70", "2887 T80", "2887 T90", "2887 T95", "2887 FBP",
    "D86 IBP", "D86 T5", "D86 T10", "D86 T20", "D86 T30", "D86 T40",
    "D86 T50", "D86 T60", "D86 T70", "D86 T80", "D86 T90", "D86 T95", "D86 FBP",
    "Best Fit", "Fit Score",
    "Source File",
]


class Recorded:
    def __init__(self, method, path, headers, body):
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body

    def header(self, name):
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return None

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class FakeHub:
    def __init__(self, token="tok-123"):
        self.token = token
        self.requests = []
        self.lock = threading.Lock()
        # ingest: list of (status, body-dict|bytes|None) consumed first; None
        # body means "use the default body for that status".
        self.ingest_script = []
        self.received = {}          # sha -> bytes
        self.next_sample_id = 1
        # heartbeat
        self.commands = []          # consumed one per heartbeat
        self.heartbeat_status = 200
        self.agent_package_sha256 = ""
        self.heartbeats = []
        # results
        self.results_header = list(CSV_HEADER)
        self.rows = []              # [{"seq": int, "line": str}]
        self.results_status = 200
        # package
        self.package_version = ""
        self.package_zip = b""
        self.package_sha_override = None
        self._server = None
        self._thread = None

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self):
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _send(self, status, obj, ctype="application/json"):
                data = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                body = self._body() if method == "POST" else b""
                u = urlparse(self.path)
                rec = Recorded(method, self.path, dict(self.headers.items()), body)
                with hub.lock:
                    hub.requests.append(rec)
                if self.headers.get("Authorization") != "Bearer " + hub.token:
                    return self._send(401, {"error": "bad token"})
                route = (method, u.path)
                if route == ("POST", "/api/ingest"):
                    return hub._ingest(self, body)
                if route == ("POST", "/api/agent/heartbeat"):
                    return hub._heartbeat(self, rec)
                if route == ("GET", "/api/agent/results"):
                    return hub._results(self, parse_qs(u.query))
                if route == ("GET", "/api/agent/package"):
                    sha = hub.package_sha_override or hashlib.sha256(hub.package_zip).hexdigest()
                    return self._send(200, {"version": hub.package_version, "sha256": sha})
                if route == ("GET", "/api/agent/package.zip"):
                    return self._send(200, hub.package_zip, "application/zip")
                return self._send(404, {"error": "no route"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def by_path(self, path):
        with self.lock:
            return [r for r in self.requests if urlparse(r.path).path == path]

    # ── routes ───────────────────────────────────────────────────────────
    def _ingest(self, h, body):
        sha = hashlib.sha256(body).hexdigest()
        with self.lock:
            scripted = self.ingest_script.pop(0) if self.ingest_script else None
        if scripted is not None:
            status, obj = scripted
            if obj is None:
                obj = self._default_body(status, sha)
            if isinstance(obj, bytes):
                return h._send(status, obj, "text/html")
            if status in (200, 201, 202):
                with self.lock:
                    self.received.setdefault(sha, body)
            return h._send(status, obj)
        if h.headers.get("X-GC-SHA256") != sha:
            return h._send(400, {"error": "sha256 mismatch"})
        with self.lock:
            if sha in self.received:
                return h._send(200, {"sample_id": 1, "sha256": sha, "duplicate": True})
            self.received[sha] = body
            sid = self.next_sample_id
            self.next_sample_id += 1
        return h._send(201, {"sample_id": sid, "sha256": sha, "status": "queued"})

    def _default_body(self, status, sha):
        if status == 201:
            return {"sample_id": 7, "sha256": sha, "status": "queued"}
        if status == 200:
            return {"sample_id": 7, "sha256": sha, "duplicate": True}
        if status == 202:
            return {"sha256": sha, "conflict_id": 3}
        return {"error": "scripted %d" % status}

    def _heartbeat(self, h, rec):
        with self.lock:
            self.heartbeats.append(rec.json())
            cmd = self.commands.pop(0) if self.commands else None
        if self.heartbeat_status != 200:
            return h._send(self.heartbeat_status, {"error": "scripted"})
        return h._send(200, {"command": cmd,
                             "agent_package_sha256": self.agent_package_sha256,
                             "server_time": "2026-09-28T12:00:00"})

    def _results(self, h, q):
        if self.results_status != 200:
            return h._send(self.results_status, {"error": "scripted"})
        after = int(q.get("after", ["0"])[0])
        limit = int(q.get("limit", ["500"])[0])
        with self.lock:
            rows = [r for r in self.rows if r["seq"] > after]
        page = rows[:limit]
        return h._send(200, {"header": self.results_header, "rows": page,
                             "more": len(rows) > limit})
