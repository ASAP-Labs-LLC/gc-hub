"""The Distillation Data table's header comes from /api/table's ``columns``
(v3.1.0), never a hard-coded list: the old template put D86 IBP…FBP before
2887 IBP…FBP while the rows follow ``distill.CSV_HEADER`` (2887 first), so
every D86 header sat over a D2887 value. The alignment itself is tested in
``tests/js/distill_view.test.js``; this pins the wiring. Source only.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _index() -> str:
    return (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


def test_the_distill_table_head_is_not_hard_coded():
    m = re.search(r'<table class="data-table" id="distill-table">(.*?)</table>', _index(), re.S)
    assert m, "distill-table not found"
    thead = re.search(r"<thead[^>]*>(.*?)</thead>", m.group(1), re.S).group(1)
    assert "<th" not in thead, "the header is built from /api/table's columns"


def test_app_js_builds_the_head_from_the_columns():
    src = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "DistillView.tableHeader(columns)" in src


def test_the_dashboard_d86_table_shows_what_is_stored_at_40_and_60():
    """The Dashboard's D86 table used to print "—" at 40% and 60% whatever was
    stored; the values now come from ``DistillView.dashboardD86`` (tested in
    node), with a tooltip when there is none."""
    src = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "DistillView.dashboardD86(" in src
    assert "(label === '40%' || label === '60%') ? '\\u2014'" not in src


def test_the_dashboard_d86_cells_get_the_notes_as_tooltips():
    """``dashboardD86``'s notes (why a cell is empty; that 40%/60% are
    uncorrected midpoints) reach the cells' tooltips, not only for "—"."""
    src = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "d86Notes = view.notes" in src
    assert "populateDashboardTables(d2887, d86, dcDiv, d86Notes)" in src
    assert "d86Notes[label]" in src


def test_convert_to_d86_rounds_like_python_and_fills_40_60():
    src = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    body = src[src.index("function convertToD86(d2887) {"):]
    body = body[:body.index("\n}\n")]
    assert "DistillView.pyRound2(" in body and "DistillView.x4Midpoints(d86)" in body


def test_distill_view_is_loaded_before_app_js():
    scripts = re.findall(r"js/([a-z_]+\.js)", _index())
    assert "distill_view.js" in scripts
    assert scripts.index("distill_view.js") < scripts.index("app.js")
