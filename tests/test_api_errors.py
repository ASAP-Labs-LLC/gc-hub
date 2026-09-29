"""api_errors.py: every failure on an ``/api/`` path answers JSON
``{error, status, ref}`` (v3.1.0), so the front end never has to parse an
HTML error page ("Unexpected token '<'").

In process on a bare Flask app wired with ``api_errors.install`` (a route
that raises, a 405, a werkzeug 400, the existing JSON handlers kept), then on
the real app booted in a subprocess (never ``import app``).
"""
from __future__ import annotations

import http.client
import json
import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")

from flask import Flask, jsonify, request  # noqa: E402

import admin_auth  # noqa: E402
import api_errors  # noqa: E402

GENERIC = re.compile(r"^Something went wrong on the hub \(ref ([A-Za-z0-9]+)\)\. Send the "
                     r"diagnostics bundle or app\.log to have it looked at\.$")


def make_app():
    app = Flask("api_errors_test")
    app.register_blueprint(admin_auth.bp)     # its JSON 413 handler must survive

    @app.errorhandler(404)
    def _not_found(exc):                     # app.py's own: a JSON 404 for the API
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not found"}), 404
        return exc

    class HubUnavailable(RuntimeError):
        pass

    @app.errorhandler(HubUnavailable)
    def _hub_unavailable(exc):
        return jsonify({"error": str(exc)}), 503

    api_errors.install(app)

    @app.route("/api/boom", methods=["GET", "POST"])
    def boom():
        raise RuntimeError("internal detail: C:\\ASAPApps\\gc\\data\\gc.db is locked")

    @app.route("/boom")
    def page_boom():
        raise RuntimeError("page internals")

    @app.route("/api/only-get")
    def only_get():
        return jsonify({"ok": True})

    @app.route("/api/parse", methods=["POST"])
    def parse():
        body = request.get_json(force=True)   # malformed JSON → werkzeug's BadRequest
        return jsonify(body)

    @app.route("/api/hub-down")
    def hub_down():
        raise HubUnavailable("The hub store has not been created yet")

    @app.route("/api/big", methods=["POST"])
    def big():
        admin_auth.limit_json_body(10)
        request.get_data()
        return jsonify({"ok": True})

    @app.route("/api/things/<name>/only-get")
    def thing(name):
        return jsonify({"name": name})

    @app.route("/api/things/<name>/boom", methods=["POST"])
    def thing_boom(name):
        raise RuntimeError("boom")

    @app.route("/api/upstream/<int:code>")
    def upstream(code):
        from werkzeug.exceptions import abort
        abort(code)

    return app


@pytest.fixture()
def client():
    return make_app().test_client()


def test_a_route_that_raises_answers_json_500_with_a_ref_and_no_internals(client, caplog):
    with caplog.at_level(logging.INFO):
        r = client.post("/api/boom", data=json.dumps({"password": "hunter2-secret"}),
                        headers={"Content-Type": "application/json"},
                        environ_base={"REMOTE_ADDR": "10.0.0.25"})
    assert r.status_code == 500
    assert r.is_json, r.data[:200]
    body = r.get_json()
    assert set(body) == {"error", "status", "ref"}
    assert body["status"] == 500
    m = GENERIC.match(body["error"])
    assert m, body["error"]
    assert m.group(1) == body["ref"] and 4 <= len(body["ref"]) <= 12
    assert "gc.db" not in r.get_data(as_text=True) and "RuntimeError" not in r.get_data(as_text=True)
    # logged at ERROR with the ref, method, path and client address, plus the traceback
    errs = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert errs, caplog.text
    line = errs[-1].getMessage()
    assert body["ref"] in line and "POST" in line and "/api/boom" in line and "10.0.0.25" in line
    assert errs[-1].exc_info and errs[-1].exc_info[0] is RuntimeError
    # never the request body
    assert "hunter2-secret" not in caplog.text


def test_refs_differ_between_failures(client):
    a = client.get("/api/boom").get_json()["ref"]
    b = client.get("/api/boom").get_json()["ref"]
    assert a != b


def test_a_page_that_raises_keeps_the_html_500(client):
    r = client.get("/boom")
    assert r.status_code == 500
    assert not r.is_json
    assert b"<html" in r.data.lower() or b"<!doctype" in r.data.lower()


def test_405_on_an_api_path_is_json_and_keeps_allow(client):
    r = client.post("/api/only-get")
    assert r.status_code == 405
    assert r.is_json
    body = r.get_json()
    assert body["status"] == 405 and body["error"] and body["ref"]
    assert "GET" in r.headers.get("Allow", "")


def test_werkzeug_400_on_an_api_path_is_json(client):
    r = client.post("/api/parse", data=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.is_json
    body = r.get_json()
    assert body["status"] == 400 and body["error"] and body["ref"]


def test_existing_json_handlers_keep_their_shape(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.get_json() == {"error": "Not found"}
    r = client.get("/api/hub-down")
    assert r.status_code == 503 and r.get_json() == {"error": "The hub store has not been created yet"}
    r = client.post("/api/big", data=b"x" * 100, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.get_json() == {"error": "The request body is too large."}


def test_page_errors_keep_the_html_pages(client):
    r = client.get("/nope")
    assert r.status_code == 404 and not r.is_json
    r = client.post("/boom")          # 405 on a page
    assert r.status_code == 405 and not r.is_json


FORGED = "2026-01-01 00:00:00 [ERROR] forged: x"
FORGED_PATH = "%0A2026-01-01%2000:00:00%20[ERROR]%20forged:%20x"


def _no_forged_line(caplog):
    for rec in caplog.records:
        msg = rec.getMessage()
        assert "\n" not in msg and "\r" not in msg, msg
    assert not any(line.startswith(FORGED) for line in caplog.text.splitlines()), caplog.text


def test_a_percent_encoded_newline_in_the_path_cannot_forge_a_log_line(client, caplog):
    """``request.path`` is percent-decoded: ``%0A`` in a segment is a real
    newline. Logged raw, it wrote a whole fake line into app.log."""
    with caplog.at_level(logging.INFO):
        r = client.post(f"/api/things/{FORGED_PATH}/only-get")          # 405, logged at INFO
        assert r.status_code == 405
        r = client.post(f"/api/things/{FORGED_PATH}/boom")              # 500, logged at ERROR
        assert r.status_code == 500
    _no_forged_line(caplog)
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "api_errors"]
    assert len(lines) == 2
    assert all("\\x0a2026-01-01 00:00:00 [ERROR] forged: x" in line for line in lines), lines


@pytest.mark.parametrize("code", [502, 503, 504])
def test_a_5xx_http_exception_keeps_its_own_status(client, caplog, code):
    """A deliberate 502/503/504 is not an unhandled error: it keeps its status
    (the front end and the updater can tell "busy" from "broken")."""
    with caplog.at_level(logging.INFO):
        r = client.get(f"/api/upstream/{code}")
    assert r.status_code == code
    body = r.get_json()
    assert body["status"] == code and body["ref"] and body["error"]
    assert any(body["ref"] in rec.getMessage() and str(code) in rec.getMessage()
               for rec in caplog.records if rec.name == "api_errors"), caplog.text


def test_a_plain_500_http_exception_is_still_the_generic_answer(client):
    r = client.get("/api/upstream/500")
    assert r.status_code == 500
    assert GENERIC.match(r.get_json()["error"])


def test_every_response_says_it_came_from_the_hub(client):
    """``X-GC-Hub`` tells the front end a page is the hub's own, not
    Cloudflare's (through the tunnel every response carries cf-ray)."""
    assert client.get("/api/only-get").headers.get("X-GC-Hub") == "1"
    assert client.get("/nope").headers.get("X-GC-Hub") == "1"
    assert client.get("/api/boom").headers.get("X-GC-Hub") == "1"


# ── the real app ────────────────────────────────────────────────────────────

from bootapp import booted, cookie_header  # noqa: E402


@pytest.fixture(scope="module")
def hub():
    tmp = Path(tempfile.mkdtemp(prefix="gc-apierr-"))
    try:
        with booted(tmp) as (port, _proc, data, _home):
            yield port, data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _raw(port, method, path, body=b"", headers=None):
    h = dict(cookie_header(port))
    h.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        c.request(method, path, body=body or None, headers=h)
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


def test_the_real_app_answers_json_for_a_405_on_an_api_path(hub):
    port, _data = hub
    code, h, raw = _raw(port, "POST", "/api/session", b"{}", {"Content-Type": "application/json"})
    assert code == 405
    assert h["content-type"].startswith("application/json"), raw[:200]
    body = json.loads(raw)
    assert body["status"] == 405 and body["ref"]


def test_the_real_app_answers_json_for_a_werkzeug_400(hub):
    port, _data = hub
    code, h, raw = _raw(port, "POST", "/api/analysis", b"{not json",
                        {"Content-Type": "application/json"})
    assert code == 400
    assert h["content-type"].startswith("application/json"), raw[:200]
    body = json.loads(raw)
    assert body["status"] == 400 and body["ref"]


def test_the_real_app_keeps_its_json_404_and_html_pages(hub):
    port, _data = hub
    code, h, raw = _raw(port, "GET", "/api/definitely-not-a-route")
    assert code == 404 and json.loads(raw) == {"error": "Not found"}
    code, h, raw = _raw(port, "GET", "/definitely-not-a-page")
    assert code == 404 and not h["content-type"].startswith("application/json")
    assert h.get("x-gc-hub") == "1"


def test_the_real_app_log_has_no_forged_line_from_a_percent_encoded_path(hub):
    """The critic's proof, on the real app: ``%0A`` in a path segment wrote a
    whole fake ``[ERROR]`` line into app.log."""
    port, data = hub
    code, _h, _body = _raw(port, "POST", f"/api/instruments/{FORGED_PATH}/corrections",
                           b"{}", {"Content-Type": "application/json"})
    assert code == 405
    text = (data / "app.log").read_text(encoding="utf-8", errors="replace")
    assert "\\x0a2026-01-01 00:00:00 [ERROR] forged: x" in text      # logged, escaped
    assert not any(line.startswith(FORGED) for line in text.splitlines()), text[-2000:]
