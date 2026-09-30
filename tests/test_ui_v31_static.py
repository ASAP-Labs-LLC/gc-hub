"""v3.1 pages, by source (no browser): the DOM is built with textContent only
(no HTML sinks), templates carry no inline script, session.js is the first
script of the layout, the admin password is never put in storage, and
``--text-subtle`` is never used as a text colour (WCAG AA: decoration only)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ["ui_logic.js", "shell.js", "live_adapter.js", "instrument_actions.js", "instruments_home.js",
      "instrument_detail.js", "setup_guide.js", "hub_admin.js", "admin_page.js", "calibration.js",
      "samples_router.js", "samples_logic.js", "samples_page.js", "compare_view_stub.js"]
TEMPLATES = ["_layout.html", "_shell.html", "instruments_home.html", "instrument_detail.html",
             "setup_guide.html", "instrument_missing.html", "hub_admin.html", "calibration.html",
             "samples.html"]
HTML_SINKS = re.compile(r"\.(innerHTML|outerHTML)\b|insertAdjacentHTML|document\.write|"
                        r"\beval\s*\(|new\s+Function\s*\(")


def _js(name):
    return (ROOT / "static" / "js" / name).read_text(encoding="utf-8")


def test_no_html_sinks_in_the_new_scripts():
    for name in JS:
        m = HTML_SINKS.search(_js(name))
        assert m is None, f"{name}: {m.group(0)}"


def test_no_inline_script_in_the_new_templates():
    for name in TEMPLATES:
        html = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert re.search(r"<script>(?!\s*</script>)", html) is None, name
        assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html) is None, name
        assert not re.search(r"\son[a-z]+=", html), name          # no inline handlers


def test_session_js_is_the_first_script_of_the_layout():
    html = (ROOT / "templates" / "_layout.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script src=\"\{\{ url_for\('static', filename='([^']+)'", html)
    assert scripts[0] == "js/session.js", scripts
    assert 'id="app-version"' in html


def test_the_admin_password_never_reaches_storage():
    for name in JS:
        src = _js(name)
        for call in re.findall(r"(?:localStorage|sessionStorage)[^;\n]*", src):
            assert "pw" not in call.lower() and "password" not in call.lower(), (name, call)
    assert "makeAdminGate" in _js("shell.js")


def test_text_subtle_is_decoration_only():
    for css in (ROOT / "static" / "css").glob("*.css"):
        text = css.read_text(encoding="utf-8")
        for m in re.finditer(r"(?<![-\w])color\s*:\s*var\(--text-subtle\)", text):
            raise AssertionError(f"{css.name}: --text-subtle used as a text colour at {m.start()}")


def test_tokens_have_a_dark_theme_and_the_spec_status_colours():
    tokens = (ROOT / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
    assert ':root[data-theme="dark"]' in tokens
    for colour in ("#15803d", "#b45309", "#b91c1c", "#fecaca", "#bfdbfe"):
        assert colour in tokens, colour
