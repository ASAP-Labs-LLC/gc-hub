"""The front end parses every fetch answer through ``GCSession.readJson``
(v3.1.0), never ``resp.json()``: a web page where data was expected (a hub
error page, Cloudflare's block or challenge page) then shows a sentence
instead of ``SyntaxError: Unexpected token '<'``. Source-only (no browser).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_JSON = re.compile(r"\.json\(\)")


def _sources():
    yield from sorted((ROOT / "static" / "js").glob("*.js"))     # session.js too
    yield from sorted((ROOT / "templates").glob("*.html"))


def test_no_fetch_answer_is_parsed_with_resp_json():
    offenders = []
    for p in _sources():
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if RAW_JSON.search(line):
                offenders.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not offenders, "use GCSession.readJson(resp):\n" + "\n".join(offenders)


def test_the_pages_that_parse_answers_use_read_json():
    for rel in ("templates/admin_setup.html", "static/js/login.js", "static/js/hub_admin.js",
                "static/js/instruments.js", "static/js/diagnostics.js", "static/js/comments.js",
                "static/js/app.js", "templates/calibration.html"):
        assert "GCSession.readJson(" in (ROOT / rel).read_text(encoding="utf-8"), rel


def test_session_js_check_parses_with_read_json():
    """``checkSession`` (the SSE stream's onerror, ``whoami``) reads
    ``/api/session`` through ``readJson`` too."""
    src = (ROOT / "static" / "js" / "session.js").read_text(encoding="utf-8")
    body = re.search(r"function checkSession\(.*?\n    \}\n", src, re.S).group(0)
    assert "readJson(" in body, body


def test_session_js_is_loaded_first_wherever_read_json_is_used():
    for p in sorted((ROOT / "templates").glob("*.html")):
        text = p.read_text(encoding="utf-8")
        if '{% extends "_layout.html" %}' in text:
            # v3.1 pages: the layout's <head> scripts come before the page's own
            text = (ROOT / "templates" / "_layout.html").read_text(encoding="utf-8") + text
        scripts = re.findall(r"js/([a-z_]+\.js)", text)
        if not scripts or p.name.startswith("_"):
            continue
        assert scripts[0] == "session.js", (p.name, scripts)
