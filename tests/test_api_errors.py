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
import sqlite3
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


def sqlite_error(message: str, code: int) -> sqlite3.OperationalError:
    """An OperationalError as SQLite raises it: with its result code."""
    exc = sqlite3.OperationalError(message)
    exc.sqlite_errorcode = code
    exc.sqlite_errorname = {5: "SQLITE_BUSY", 6: "SQLITE_LOCKED", 517: "SQLITE_BUSY_SNAPSHOT"}.get(
        code, "SQLITE_ERROR")
    return exc


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

    @app.route("/api/locked", methods=["POST"])
    def locked():
        raise sqlite_error("database is locked", sqlite3.SQLITE_BUSY)

    @app.route("/api/locked-extended", methods=["POST"])
    def locked_extended():
        raise sqlite_error("database is locked", 517)        # SQLITE_BUSY_SNAPSHOT

    @app.route("/api/sql/<int:case>", methods=["POST"])
    def sql_not_busy(case):
        raise [sqlite_error("no such column: busy_flag", sqlite3.SQLITE_ERROR),
               sqlite_error("no such table: unlocked_tbl", sqlite3.SQLITE_ERROR),
               # SQLITE_LOCKED: a conflict on this same connection, a bug
               sqlite_error("database table is locked", sqlite3.SQLITE_LOCKED),
               # no error code at all (raised by hand, not by SQLite)
               sqlite3.OperationalError("database is locked")][case]

    @app.route("/locked-page")
    def locked_page():
        raise sqlite_error("database is locked", sqlite3.SQLITE_BUSY)

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


def test_a_locked_store_is_a_503_try_again_not_send_diagnostics(client, caplog):
    """A write that waited out ``busy_timeout`` behind another writer (a purge's
    one transaction, an import batch) is a busy hub, not a bug: 503 with
    Retry-After and a "try again" message, logged at WARNING without a
    traceback, instead of the generic 500 asking for the diagnostics bundle."""
    with caplog.at_level(logging.INFO):
        r = client.post("/api/locked", data="{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 503
    assert r.is_json
    body = r.get_json()
    assert set(body) == {"error", "status", "ref"} and body["status"] == 503
    assert "try again" in body["error"].lower() and "diagnostics" not in body["error"]
    assert r.headers.get("Retry-After") == str(api_errors.BUSY_RETRY_AFTER)
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR], caplog.text
    assert body["ref"] in caplog.text


def test_an_extended_busy_code_is_also_a_503(client):
    r = client.post("/api/locked-extended", data="{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 503


@pytest.mark.parametrize("case", range(4))
def test_other_sqlite_errors_stay_the_generic_500(client, case):
    """Only SQLITE_BUSY (by its result code, never words in the message) is a
    503: 'no such column: busy_flag', 'no such table: unlocked_tbl',
    SQLITE_LOCKED and a code-less error are bugs, so the generic 500."""
    r = client.post(f"/api/sql/{case}", data="{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 500 and GENERIC.match(r.get_json()["error"])


def test_a_locked_store_on_a_page_keeps_the_html_500(client):
    r = client.get("/locked-page")
    assert r.status_code == 500 and not r.is_json


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


# A JSON body that is not an object ("str", 123, [1]) on an operator route was
# read with ``body.get(...)`` and answered the generic 500 (and an ERROR
# traceback in app.log): a client mistake must be a 400 that says so.
NON_OBJECT_ROUTES = ("/api/reprocess", "/api/reprocess/preview", "/api/analysis",
                     "/api/best-fit", "/api/export-lims", "/api/export-pdf",
                     "/api/export-comparison", "/api/export-analysis-report",
                     "/api/export-analysis-reports-zip", "/api/qbench-skip-item",
                     "/api/restart")


@pytest.mark.parametrize("path", NON_OBJECT_ROUTES)
@pytest.mark.parametrize("raw_body", [b'"str"', b"123", b"[1]", b"true"])
def test_a_non_object_json_body_is_a_400_not_a_500(hub, path, raw_body):
    port, _data = hub
    code, h, raw = _raw(port, "POST", path, raw_body, {"Content-Type": "application/json"})
    assert code == 400, (code, raw[:300])
    assert h["content-type"].startswith("application/json")
    assert json.loads(raw)["error"]


@pytest.mark.parametrize("body", [{"idx": "x"}, {"idx": -1}, {"idx": 1.5}, {"idx": [1]},
                                  {"idx": True}, {"idx": 10 ** 30}])
def test_qbench_skip_item_refuses_a_bad_index(hub, body):
    port, _data = hub
    code, _h, raw = _raw(port, "POST", "/api/qbench-skip-item", json.dumps(body).encode(),
                         {"Content-Type": "application/json"})
    assert code == 400, (code, raw[:300])


@pytest.mark.parametrize("queue", [5, 1.5, True])
def test_qbench_upload_with_a_non_list_queue_is_a_400(hub, queue):
    port, _data = hub
    code, _h, raw = _raw(port, "POST", "/api/qbench-upload", json.dumps({"queue": queue}).encode(),
                         {"Content-Type": "application/json"})
    assert code == 400, (code, raw[:300])


def test_reprocess_with_a_non_list_missing_is_a_400(hub):
    port, _data = hub
    code, _h, raw = _raw(port, "POST", "/api/reprocess",
                         json.dumps({"sample_ids": [1], "missing": 5}).encode(),
                         {"Content-Type": "application/json"})
    assert code == 400, (code, raw[:300])


def test_a_large_sample_ids_list_is_answered_quickly(hub):
    """``_sample_ids`` deduplicated with ``i not in out`` (quadratic): the
    ~130,000 ids a 1 MB body holds kept one request thread busy for tens of
    seconds while holding the GIL, stalling every other request."""
    import time
    port, _data = hub
    body = json.dumps({"sample_ids": list(range(10 ** 5, 10 ** 5 + 120_000))}).encode()
    assert len(body) < 1024 * 1024
    t0 = time.monotonic()
    code, _h, raw = _raw(port, "POST", "/api/export-lims", body,
                         {"Content-Type": "application/json"})
    elapsed = time.monotonic() - t0
    assert code in (400, 404), (code, raw[:300])
    assert elapsed < 4, elapsed


@pytest.mark.parametrize("instrument", [5, ["gc1"], {"a": 1}])
def test_reprocess_preview_with_a_non_string_instrument_is_a_400(hub, instrument):
    port, _data = hub
    code, _h, raw = _raw(port, "POST", "/api/reprocess/preview",
                         json.dumps({"query": "1-3", "instrument": instrument}).encode(),
                         {"Content-Type": "application/json"})
    assert code == 400, (code, raw[:300])


# ── a busy store behind a route's own ``except Exception`` ───────────────────
# Many app.py routes catch every exception and answer ``_error(str(exc), 500)``,
# so a locked store never reached api_errors: a 500 with SQLite's raw "database
# is locked" text. The app is booted with ``store`` patched (in the child, by a
# wrapper around app.py) to raise SQLITE_BUSY from the named call, only inside
# a request to the named path, so start-up is untouched.

_BUSY_WRAPPER = r"""
import os, sqlite3, sys, runpy
import flask, store
targets = dict(t.split("=", 1) for t in os.environ["GC_TEST_BUSY"].split(";"))
def busy_wrap(name, real):
    def wrapper(*a, **k):
        if flask.has_request_context() and targets.get(flask.request.path) == name:
            exc = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
            exc.sqlite_errorname = "SQLITE_BUSY"
            raise exc
        return real(*a, **k)
    return staticmethod(wrapper)
for name in set(targets.values()):
    owner, attr = name.split(".")
    cls = getattr(store, owner)
    setattr(cls, attr, busy_wrap(name, getattr(cls, attr)))
sys.argv = ["app.py", "--no-tray"]
runpy.run_path("app.py", run_name="__main__")
"""

BUSY_ROUTES = [
    # (method, path, JSON body or None, the store call that is busy)
    ("POST", "/api/calibration", {"assignments": []}, "instruments.upsert"),
    ("GET", "/api/settings", None, "instruments.get"),
]


@pytest.fixture(scope="module")
def busy_hub():
    from bootapp import setup_admin, sign_in
    from hub_boot import build_hub
    tmp = Path(tempfile.mkdtemp(prefix="gc-apibusy-"))
    try:
        build_hub(tmp)
        env = {"GC_TEST_BUSY": ";".join(f"{p}={call}" for _m, p, _b, call in BUSY_ROUTES)}
        with booted(tmp, cmd=[sys.executable, "-c", _BUSY_WRAPPER], extra_env=env) as (
                port, _proc, data, _home):
            sign_in(port, data)
            yield port, data, setup_admin(port, data)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.parametrize("method,path,body,call", BUSY_ROUTES)
def test_a_busy_store_inside_a_routes_own_except_is_a_503(busy_hub, method, path, body, call):
    port, _data, pw = busy_hub
    raw_body, headers = b"", {}
    if body is not None:
        raw_body = json.dumps(dict(body, password=pw)).encode()
        headers["Content-Type"] = "application/json"
    code, h, raw = _raw(port, method, path, raw_body, headers)
    assert code == 503, (code, raw[:300])
    assert h.get("retry-after") == str(api_errors.BUSY_RETRY_AFTER)
    out = json.loads(raw)
    assert "locked" not in out["error"].lower() and "try again" in out["error"].lower()
