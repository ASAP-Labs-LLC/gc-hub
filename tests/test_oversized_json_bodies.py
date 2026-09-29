"""413 (not 500 or 400) when a JSON POST's body is oversized, in the booted
app:

* a Content-Length-declared body over the global 1 MiB cap used to be
  swallowed by api_save_settings's catch-all ``except Exception`` and
  answered 500 instead of the JSON 413 the body-size guard means to give;
* a Transfer-Encoding: chunked body (no Content-Length) silently truncates
  at admin_auth's 64 KiB JSON cap (werkzeug's ``LimitedStream.readall``
  stops the read instead of raising once the max is reached) and used to be
  misread as an empty/malformed body -> a misleading 400, in both
  app._admin_json_body and admin_auth's own generic ``_json_body`` helper.
"""
from __future__ import annotations

import http.client
import json
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")

import store  # noqa: E402
from bootapp import booted, send, wait_for  # noqa: E402

MiB = 1024 * 1024


def _raw_post(port, path, headers, body=b"", timeout=10):
    """A POST that announces its own Content-Length and may send less: the
    server must answer from the header without reading the body."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.putrequest("POST", path)
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders()
        if body:
            c.send(body)
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw or b"null")
        except ValueError:
            return r.status, None
    finally:
        c.close()


def _chunked_post(port, path, headers, total_bytes, timeout=20):
    """A POST with Transfer-Encoding: chunked (no Content-Length header)
    whose body is a JSON object opener padded past *total_bytes*."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        def gen():
            yield b'{"password": "'
            sent = 0
            chunk = b"x" * 65536
            while sent < total_bytes:
                yield chunk
                sent += len(chunk)
            yield b'"}'
        c.request("POST", path, body=gen(), encode_chunked=True, headers=headers)
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw or b"null")
        except ValueError:
            return r.status, None
    finally:
        c.close()


@pytest.fixture(scope="module")
def app_port(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("oversized-json")
    with booted(tmp) as (port, _proc, data, _home):
        db = data / store.DB_FILENAME
        assert wait_for(lambda: db.is_file() and store.instruments.get("gc1", db=db) is not None,
                        timeout=30)
        yield port, data, db


def test_settings_over_1mib_declared_length_is_413_not_500(app_port):
    port, _data, _db = app_port
    code, body = _raw_post(port, "/api/settings",
                           {"Content-Type": "application/json",
                            "Content-Length": str(2 * MiB)}, b'{"x": "')
    assert code == 413, (code, body)
    assert isinstance(body, dict) and body.get("error"), body


def test_settings_small_body_still_works(app_port):
    port, _data, _db = app_port
    code, body = send(port, "/api/settings", b"{}", {"Content-Type": "application/json"})
    assert code == 200, body


def test_save_analysis_defaults_chunked_2mib_is_413_not_400(app_port):
    """app._admin_json_body, used by /api/save-analysis-defaults."""
    port, _data, _db = app_port
    code, body = _chunked_post(port, "/api/save-analysis-defaults",
                               {"Content-Type": "application/json"}, 2 * MiB)
    assert code == 413, (code, body)
    assert isinstance(body, dict) and body.get("error"), body


def test_admin_password_chunked_2mib_is_413_not_400(app_port):
    """admin_auth's own generic ``_json_body`` helper, used by
    /api/admin/password (and /api/admin/setup)."""
    port, _data, _db = app_port
    code, body = _chunked_post(port, "/api/admin/password",
                               {"Content-Type": "application/json"}, 2 * MiB)
    assert code == 413, (code, body)
    assert isinstance(body, dict) and body.get("error"), body
