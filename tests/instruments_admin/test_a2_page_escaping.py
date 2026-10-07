"""2A2 T10 / 2B1 review M5: the Instruments pages render agent-supplied strings
(host, state, last_file, last_error, version) and instrument names as text,
never as HTML: no HTML sink in their scripts, and the calibration page
escapes what it interpolates. (v6.0.0: the 2A2 page itself is gone.)"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static" / "js"
# v6.0.0: the 2A2 page (instruments.js/.html) is gone; these are the pages
# that show agent strings and instrument names now
PAGE_JS = [JS / n for n in ("instruments_home.js", "instrument_detail.js", "setup_guide.js",
                            "instrument_actions.js", "instruments_logic.js")]
CAL = ROOT / "templates" / "calibration.html"
CAL_JS = ROOT / "static" / "js" / "calibration.js"   # v4.0 lane E2: the page code moved here

HTML_SINKS = re.compile(r"\.(innerHTML|outerHTML)\b|insertAdjacentHTML|document\.write|"
                        r"\beval\s*\(|new\s+Function\s*\(")


def test_page_scripts_have_no_html_sinks():
    for p in PAGE_JS:
        src = p.read_text(encoding="utf-8")
        assert not HTML_SINKS.search(src), f"{p.name}: {HTML_SINKS.search(src).group(0)}"


def test_calibration_page_escapes_interpolated_errors():
    src = CAL.read_text(encoding="utf-8") + CAL_JS.read_text(encoding="utf-8")
    assert "${e.message}" not in src
    assert "instrument" in src and "/api/instruments/" in src
