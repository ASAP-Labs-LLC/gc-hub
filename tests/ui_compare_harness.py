"""A test-only page for the Compare view (v5.0.0 lane C). Not a test module.

``/samples/<id>/compare`` is lane S's page, so lane C's Selenium tests mount
``GCCompare`` on a harness instead, and the hub gains no test route:

* ``build(root)`` builds a hub data folder with real samples (``hub_boot``)
  plus a realistic diesel sample with 5% gasoline and two comparison
  standards (``tests/fixtures/make_diesel_pair.py``);
* ``Harness(app_port)`` renders ``tests/fixtures/compare_harness.html`` (it
  extends the real ``_layout.html``, so the shell, the top bar's Report
  queue button and every script are the shipped ones) with jinja2, and
  serves it from a small proxy on its own port: ``/__harness/...`` from
  ``tests/fixtures``, everything else forwarded to the booted app (the
  session cookie is per host, so it applies; ``Origin`` is dropped because
  the proxy's port is not the app's). Nothing here ships.
"""
from __future__ import annotations

import http.client
import json
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
FIXTURES = TESTS / "fixtures"
for _p in (ROOT, TESTS, TESTS / "golden", FIXTURES):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

STD_ULSD = "Diesel ULSD Std"
STD_RED = "Diesel #2 Red Dye Std"


def build(root: Path):
    """The hub with ``hub.ids["diesel"]``: lab ID 40329, final, gc1."""
    import cdf_fixtures as fx
    import make_diesel_pair as dp
    from hub_boot import SIMDIS, build_hub

    hub = build_hub(root)
    t = dp.axis()
    fx.write_cdf(hub.standards / f"{STD_ULSD}.CDF", t, dp.standard(t), STD_ULSD, datetime(2026, 9, 1, 13, 30))
    fx.write_cdf(hub.standards / f"{STD_RED}.CDF", t, dp.batch(t, 77, shift=0.004, hump=1.06),
                 STD_RED, datetime(2026, 9, 1, 13, 40))
    cdf = fx.write_cdf(hub.src / "40329.CDF", t, dp.with_gasoline(t, 0.05), "40329",
                       datetime(2026, 9, 26, 14, 33), method_name=SIMDIS)
    hub.submit("diesel", cdf)
    hub.worker().run_until_idle()
    conf = json.loads((hub.data / "settings.json").read_text(encoding="utf-8"))
    conf["bestfit_enabled"] = "true"
    (hub.data / "settings.json").write_text(json.dumps(conf, indent=1), encoding="utf-8")
    return hub


def render_page() -> str:
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader([str(FIXTURES), str(ROOT / "templates")]),
                             autoescape=True)
    env.globals["url_for"] = lambda _ep, filename: "/static/" + filename
    return env.get_template("compare_harness.html").render(
        app_version="test", web_user={"name": "Test Operator"}, nav="samples")


class Harness:
    """The proxy: ``url(path)`` is on this server; ``stop()`` shuts it."""

    DROP = {"host", "origin", "referer", "connection", "keep-alive", "accept-encoding"}

    def __init__(self, app_port: int):
        page = render_page().encode("utf-8")
        app = app_port

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_a):
                pass

            def _local(self):
                path = self.path.split("?", 1)[0]
                if path == "/__harness/compare":
                    body, ctype = page, "text/html; charset=utf-8"
                elif path == "/__harness/blank":        # a page to set localStorage on
                    body, ctype = b"<!doctype html><title>blank</title>", "text/html; charset=utf-8"
                else:
                    name = path[len("/__harness/"):]
                    f = (FIXTURES / name).resolve()
                    if FIXTURES not in f.parents or not f.is_file():
                        self.send_error(404)
                        return
                    body = f.read_bytes()
                    ctype = "text/javascript" if f.suffix == ".js" else "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _forward(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                headers = {k: v for k, v in self.headers.items() if k.lower() not in Harness.DROP}
                conn = http.client.HTTPConnection("127.0.0.1", app, timeout=300)
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(k, v)
                self.end_headers()
                try:
                    while True:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    conn.close()

            def do_GET(self):
                if self.path.startswith("/__harness/"):
                    self._local()
                else:
                    self._forward()

            def do_POST(self):
                self._forward()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
