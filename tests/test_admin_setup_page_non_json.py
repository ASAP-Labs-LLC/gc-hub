"""The open v3.0.0 bug: setting the admin password through ``/admin/setup``
showed ``SyntaxError: Unexpected token '<'`` when the answer was a web page
(a hub error page, or Cloudflare's). The page's own script must parse the
answer with ``GCSession.readJson`` and show the real HTTP status and what
came back, never a parser error.

Runs the page's inline script in node against the real ``session.js``, with
a stub DOM and a stub ``fetch``; skipped without node.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

HARNESS = r"""
const S = require(process.argv[1]);
const script = process.argv[2];
const reply = JSON.parse(process.argv[3]);
function el(id) {
  return { id, value: '', textContent: '', className: '', disabled: false,
           listeners: {}, addEventListener(t, f) { this.listeners[t] = f; },
           querySelectorAll() { return []; } };
}
const els = { setup: el('setup'), msg: el('msg'), pw: el('pw'), pw2: el('pw2'),
              code: el('code'), go: el('go') };
els.pw.value = els.pw2.value = 'p@ss<w>rd"&1234';
els.code.value = 'CODE123';
global.document = { getElementById: (id) => els[id] };
global.window = { GCSession: S };
global.fetch = () => Promise.resolve({
  status: reply.status,
  headers: { get: (n) => reply.headers[n.toLowerCase()] ?? null },
  text: () => Promise.resolve(reply.body),
  json: () => { throw new Error("resp.json() called"); },
});
eval(script);
els.setup.listeners.submit({ preventDefault() {} });
setTimeout(() => {
  process.stdout.write(JSON.stringify({ text: els.msg.textContent, cls: els.msg.className,
                                        disabled: els.go.disabled }));
}, 50);
"""


def _page_script() -> str:
    html = (ROOT / "templates" / "admin_setup.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == 1, "expected the page's one inline script"
    return scripts[0]


def _run(reply: dict) -> dict:
    out = subprocess.run([NODE, "-e", HARNESS, "--", str(ROOT / "static" / "js" / "session.js"),
                          _page_script(), json.dumps(reply)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def test_a_web_page_answer_shows_the_http_status_not_a_syntax_error():
    got = _run({"status": 502, "headers": {"content-type": "text/html"},
                "body": "<!DOCTYPE html><html><title>Bad gateway</title></html>"})
    assert "Unexpected token" not in got["text"] and "SyntaxError" not in got["text"]
    assert "HTTP 502" in got["text"] and "web page" in got["text"], got
    assert got["cls"] == "err" and got["disabled"] is False      # can try again


def test_cloudflares_page_is_named():
    got = _run({"status": 403, "headers": {"content-type": "text/html", "cf-ray": "8c1d-DFW"},
                "body": "<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare"
                        "</title></head></html>"})
    assert "HTTP 403" in got["text"] and "Cloudflare: Attention Required!" in got["text"], got


def test_the_hubs_json_error_is_shown_as_is():
    got = _run({"status": 403, "headers": {"content-type": "application/json", "x-gc-hub": "1"},
                "body": json.dumps({"error": "Wrong or missing setup code."})})
    assert got["text"] == "Wrong or missing setup code." and got["cls"] == "err"


def test_json_without_an_error_field_still_names_the_status():
    got = _run({"status": 500, "headers": {"content-type": "application/json"}, "body": "{}"})
    assert got["text"] == "HTTP 500"


def test_success_says_so():
    got = _run({"status": 201, "headers": {"content-type": "application/json"},
                "body": json.dumps({"ok": True})})
    assert got["cls"] == "ok" and "Admin password set" in got["text"]
