"""v2.0.0 RC, in the booted app: the global request body limit is 1 MiB
(only /api/ingest takes more, 25 MB), /api/qbench-update-credentials answers
a non-object body with a JSON 400 (never a 500), and every page links a
favicon."""
from __future__ import annotations

import hashlib
import http.client
import json
import re
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
from bootapp import cookie_header, booted, send, wait_for  # noqa: E402

MiB = 1024 * 1024
PAGES = ("/", "/calibration", "/instruments", "/admin/hub", "/admin/setup")


def _raw_post(port, path, headers, body=b"", timeout=10):
    """A POST that announces its own Content-Length and may send less: the
    server must answer from the header without reading the body."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.putrequest("POST", path)
        for k, v in dict(cookie_header(port), **headers).items():
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


def _get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("GET", path, headers=cookie_header(port))
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


@pytest.fixture(scope="module")
def app_port(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rc-limits")
    with booted(tmp) as (port, _proc, data, _home):
        db = data / store.DB_FILENAME
        assert wait_for(lambda: db.is_file() and store.instruments.get("gc1", db=db) is not None,
                        timeout=30)
        yield port, data, db


# ── 6: the body limit ───────────────────────────────────────────────────────

def test_a_30_mb_reprocess_preview_is_413_before_the_body_is_read(app_port):
    port, _data, _db = app_port
    code, body = _raw_post(port, "/api/reprocess/preview",
                           {"Content-Type": "application/json",
                            "Content-Length": str(30 * MiB)}, b'{"query": "')
    assert code == 413, (code, body)


def test_a_2_mib_json_body_is_413(app_port):
    port, _data, _db = app_port
    payload = json.dumps({"query": "1" * (2 * MiB), "instrument": "gc1"}).encode()
    code, _body = send(port, "/api/reprocess/preview", payload,
                       {"Content-Type": "application/json"})
    assert code == 413


def test_a_small_reprocess_preview_still_works(app_port):
    port, _data, _db = app_port
    code, body = send(port, "/api/reprocess/preview",
                      json.dumps({"query": "40304", "instrument": "gc1"}).encode(),
                      {"Content-Type": "application/json"})
    assert code == 200, body
    assert body["missing"] == ["40304"]


def test_ingest_still_takes_more_than_the_global_limit(app_port):
    import ingest_api
    port, _data, db = app_port
    token = ingest_api.mint_token("gc1", db=db)
    body = b"CDF\x01" + b"\0" * (3 * MiB)           # over 1 MiB, under 25 MB
    code, out = send(port, "/api/ingest", body, {
        "Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream",
        "X-GC-SHA256": "0" * 64, "X-GC-Mtime": "2026-09-25T14:30:00",
        "X-GC-Filename": "big.CDF"})
    # read in full and checked: a sha mismatch, not a 413
    assert code == 400 and "sha256 mismatch" in out["error"], (code, out)
    assert hashlib.sha256(body).hexdigest() in out["error"]


# ── 5: qbench-update-credentials never 500s ─────────────────────────────────

@pytest.mark.parametrize("raw", [b"[1, 2]", b'"text"', b"null", b"3", b"{not json",
                                 b'{"username": 5, "password": "p"}'])
def test_qbench_update_credentials_rejects_a_bad_body_with_400(app_port, raw):
    port, _data, _db = app_port
    code, body = send(port, "/api/qbench-update-credentials", raw,
                      {"Content-Type": "application/json"})
    assert code == 400, (raw, code, body)
    assert isinstance(body, dict) and body.get("error"), body


def test_qbench_update_credentials_accepts_an_object(app_port):
    port, _data, _db = app_port
    code, body = send(port, "/api/qbench-update-credentials",
                      json.dumps({"username": "u", "password": "p"}).encode(),
                      {"Content-Type": "application/json"})
    assert code == 200 and body == {"status": "ok"}


# ── 10: a favicon on every page ─────────────────────────────────────────────

@pytest.mark.parametrize("page", PAGES)
def test_every_page_links_a_favicon_that_is_served(app_port, page):
    port, _data, _db = app_port
    code, html = _get(port, page)
    assert code == 200, (page, code)
    m = re.search(rb'<link[^>]+rel="icon"[^>]+href="([^"]+)"', html)
    assert m, page
    href = m.group(1).decode().split("?")[0]
    code, icon = _get(port, href)
    assert code == 200 and icon.lstrip().startswith(b"<svg"), (href, code)


def test_every_template_links_the_favicon():
    for t in sorted((ROOT / "templates").glob("*.html")):
        if t.name.startswith("_"):        # a partial (_signed_in.html), not a page
            continue
        text = t.read_text(encoding="utf-8")
        if '{% extends "_layout.html" %}' in text:      # v3.1 pages: the layout links it
            text = (ROOT / "templates" / "_layout.html").read_text(encoding="utf-8")
        assert 'rel="icon"' in text, t.name
