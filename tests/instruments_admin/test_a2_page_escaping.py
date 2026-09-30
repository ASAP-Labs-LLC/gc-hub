"""2A2 T10 / 2B1 review M5: the Instruments page renders agent-supplied strings
(host, state, last_file, last_error, version) and instrument names as text,
never as HTML. The page script builds its DOM with textContent only, and the
calibration page escapes what it interpolates."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE_JS = ROOT / "static" / "js" / "instruments.js"
LOGIC_JS = ROOT / "static" / "js" / "instruments_logic.js"
TEMPLATE = ROOT / "templates" / "instruments.html"
CAL = ROOT / "templates" / "calibration.html"
CAL_JS = ROOT / "static" / "js" / "calibration.js"   # v4.0 lane E2: the page code moved here

HTML_SINKS = re.compile(r"\.(innerHTML|outerHTML)\b|insertAdjacentHTML|document\.write|"
                        r"\beval\s*\(|new\s+Function\s*\(")


def test_page_script_has_no_html_sinks():
    for p in (PAGE_JS, LOGIC_JS):
        src = p.read_text(encoding="utf-8")
        assert not HTML_SINKS.search(src), f"{p.name}: {HTML_SINKS.search(src).group(0)}"


def test_page_script_sets_text_with_textcontent():
    src = PAGE_JS.read_text(encoding="utf-8")
    assert "textContent" in src
    assert "createElement" in src


def test_template_has_no_inline_script_and_loads_both_files():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "instruments_logic.js" in html and "instruments.js" in html
    assert "instruments.css" in html
    assert re.search(r"<script>(?!\s*</script>)", html) is None      # no inline code
    assert 'id="app-version"' in html                                # the version badge


def test_review_minors_in_the_page():
    """Stale answers for another instrument are dropped; the hub-URL box is
    found by id; the methods card offers every hub method."""
    src = PAGE_JS.read_text(encoding="utf-8")
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "querySelectorAll('details.add')[" not in src
    # (rev 2: the installer never needs the hub URL first, so the page no longer
    # opens that box itself; it stays findable by id)
    assert 'id="hub-url-box"' in html
    for fn in ("loadBackfill", "loadConflicts", "select"):
        body = src.split(f"async function {fn}(", 1)[1].split("\n    }\n", 1)[0]
        assert "stale(" in body, fn
    methods = src.split("function methodsCard(", 1)[1].split("\n    }\n", 1)[0]
    assert "STATE.hubMethods.map" in methods


def test_calibration_page_escapes_interpolated_errors():
    src = CAL.read_text(encoding="utf-8") + CAL_JS.read_text(encoding="utf-8")
    assert "${e.message}" not in src
    assert "instrument" in src and "/api/instruments/" in src
