"""v8.1: a report with no comparison (the sample's chromatogram on its own).

Export report offers "No comparison": the request carries ``no_comparison:
true`` and the hub prints one Letter page with the sample's chromatogram
(the trace Overview draws, carbon numbers from the sample's own ladder),
its marked regions and notes, and a footer; no standard, findings or
conclusion. The layout is ``report_layout.graph_plan`` / ``render_graph_html``
(pure, rendered here through xhtml2pdf in every face, like
``tests/test_report_one_page.py``); the real paths (direct, ZIP, QBench) run
through ``tests/report_harness.py`` in a subprocess.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("xhtml2pdf")
pytest.importorskip("reportlab")
pytest.importorskip("pypdf")

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import report_layout as rl  # noqa: E402
from test_report_one_page import (_png, face, flat, pages_without_shrink,  # noqa: E402,F401
                                  worst_notes, worst_regions, WORST_LAB)

FOOTER = ["Graph: retention time to 6.5 min", "Comparison: none"]


def render_graph(*, doc_name="GC Chromatogram", lab_id="40304", regions=(), notes=(),
                 footer=None, logo=False):
    import io

    import pypdf
    from xhtml2pdf import pisa
    footer = FOOTER if footer is None else footer
    plan = rl.graph_plan(doc_name=doc_name, lab_id=lab_id, region_lines=list(regions),
                         note_lines=list(notes), footer_lines=footer, has_logo=logo)
    page = rl.render_graph_html(
        doc_name=doc_name, lab_id=lab_id, date_display="October 14, 2026",
        datetime_str="2026-10-14 12:00", img_b64=_png(int(plan.width), int(plan.trend_h)),
        logo_b64=_png(120, 40) if logo else "", region_lines=list(regions),
        note_lines=list(notes), footer_lines=footer, app_version="v8.1.0", plan_=plan,
        sample_label=lab_id)
    rl.fonts()
    buf = io.BytesIO()
    assert not pisa.CreatePDF(page, dest=buf).err
    reader = pypdf.PdfReader(io.BytesIO(buf.getvalue()))
    text = " ".join(" ".join((p.extract_text() or "") for p in reader.pages).split())
    return plan, page, len(reader.pages), text


# ── the page ────────────────────────────────────────────────────────────────

def test_a_chromatogram_report_is_one_page_with_the_tallest_chart(face):
    regions = ["Light-end shoulder (C8–C10, 1.30–2.10 min; RB, 2026-10-14)"]
    notes = ["Re-injected to confirm. (AB, 2026-10-14)"]
    plan, page, pages, text = render_graph(regions=regions, notes=notes)
    assert pages == 1 and pages_without_shrink(page) == 1, plan
    assert plan.trend_h == rl.GRAPH_MAX and not plan.shortened
    assert "GC Chromatogram" in text and "40304" in text
    assert rl.graph_subtitle_text() in text
    assert "Marked regions" in text and flat(regions[0]) in text
    assert "Notes" in text and flat(notes[0]) in text
    assert "retention time to 6.5 min" in text and "Comparison none" in flat(text.replace(":", ""))
    assert "GC hub v8.1.0" in text
    for absent in ("Findings", "Conclusion", "compared with", "Difference", "Trend", "standard "):
        assert absent not in text, absent


def test_with_no_comments_there_are_no_sections(face):
    plan, page, pages, text = render_graph()
    assert pages == 1 and pages_without_shrink(page) == 1
    assert "Marked regions" not in text and "Notes" not in text
    assert plan.trend_h == rl.GRAPH_MAX


def test_every_section_at_its_worst_is_still_one_page(face):
    """50 marked regions and 50 notes of 500 characters, a 300-character
    title, a long lab ID and a logo: one page, the chart at least its floor,
    what was left out counted on the page."""
    plan, page, pages, text = render_graph(
        doc_name="GC Chromatogram of an exceptionally long customer document name " * 5,
        lab_id=WORST_LAB, regions=worst_regions(), notes=worst_notes(), logo=True)
    assert pages == 1 and pages_without_shrink(page) == 1, plan
    assert plan.trend_h >= rl.GRAPH_FLOOR
    assert plan.regions_shown is not None and plan.notes_shown is not None
    assert rl.more_regions_text(50 - plan.regions_shown, plan.regions_shown) in text
    assert rl.more_notes_text(50 - plan.notes_shown, plan.notes_shown) in text
    assert plan.shortened and not any("does not fit" in x for x in plan.shortened)


def test_every_interpolated_string_is_escaped(face):
    _plan, page, pages, _text = render_graph(
        doc_name="<i>Doc</i>", lab_id="<b>40304</b>",
        notes=["<img src=file:///etc/passwd> (AB, 2026-10-14)"],
        regions=["<img src=file:///etc/shadow> (C8–C10, 1.30–2.10 min; AB, 2026-10-14)"],
        footer=["Graph: <script>x</script>"])
    assert pages == 1
    for raw in ("<i>Doc", "<b>40304", "<script>", "<img src=file"):
        assert raw not in page, raw
    assert "&lt;img src=file:///etc/passwd&gt;" in page


def test_the_plan_is_deterministic():
    kw = dict(doc_name="GC Chromatogram", lab_id="40304", region_lines=worst_regions(9),
              note_lines=worst_notes(7), footer_lines=FOOTER)
    assert rl.graph_plan(**kw) == rl.graph_plan(**kw)


# ── every report path (direct, ZIP, QBench) ──────────────────────────────────

REQUEST = {
    "no_comparison": True,
    "standard_name": "",
    "x_max_min": 6.5,
    "doc_name": "GC <i>Chromatogram</i>",
    "conclusion": "",
    "bullets": "INJECTED client bullet",
    "overlay_standards": ["Diesel"],
    "ranges": [{"label": "Oil", "c_start": 20, "c_end": 44}],
}


@pytest.fixture(scope="module")
def harness():
    pytest.importorskip("flask")
    pytest.importorskip("netCDF4")
    import hub_boot

    tmp = Path(tempfile.mkdtemp(prefix="gc-nocmp-report-"))
    try:
        hub = hub_boot.build_hub(tmp)
        req = tmp / "request.json"
        req.write_text(json.dumps(REQUEST), encoding="utf-8")
        out = tmp / "out.json"
        env = {k: v for k, v in os.environ.items()
               if k not in ("PORT", "GC_PORT", "QBENCH_CLIENT_ID", "QBENCH_CLIENT_SECRET")}
        env["HOME"] = str(tmp / "home")
        (tmp / "home").mkdir()
        proc = subprocess.run(
            [sys.executable, str(TESTS / "report_harness.py"), str(hub.data),
             str(hub.ids["final"]), str(req), str(out)],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600)
        assert proc.returncode == 0 and out.exists(), proc.stderr[-4000:] + proc.stdout[-2000:]
        yield dict(json.loads(out.read_text(encoding="utf-8")), sample_id=hub.ids["final"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_every_export_path_builds_one_page(harness):
    r = harness["responses"]
    assert r["direct"]["status"] == 200, r
    assert r["zip"]["status"] == 200 and r["zip"]["job_state"] == "done", r
    assert r["qbench"]["status"] == 200, r["qbench"]
    assert harness["pdf_pages"] == {"direct": 1, "zip": 1, "qbench": 1}


def test_compare_still_needs_a_standard(harness):
    """/api/analysis is the Compare view: no standard is still a 400 there."""
    a = harness["responses"]["analysis"]
    assert a["status"] == 400 and "standard" in a["json"]["error"], a


def test_the_pdf_is_the_chromatogram_alone(harness):
    for path, text in harness["pdf_text"].items():
        assert "40304" in text, path
        assert "GC <i>Chromatogram</i>" in text, path       # escaped, printed as typed
        assert rl.graph_subtitle_text() in text, path
        # the harness's comments: one note, two marked regions
        assert "Marked regions" in text and "Narrow mark" in text, path
        assert "Notes" in text and "Sample appears to be gasoline." in text, path
        assert "6.5 min" in text and "GC hub" in text, path
        for absent in ("Findings", "Conclusion", "compared with", "Difference", "INJECTED",
                       "Diesel", "Oil C20"):
            assert absent not in text, (path, absent)


def test_no_comparison_content_has_no_findings(harness):
    recs = [r for r in harness["records"] if r["path"] != "analysis"]
    assert sorted(r["path"] for r in recs) == ["direct", "qbench", "zip"]
    for r in recs:
        c = r["content"]
        assert c["items"] == [] and c["text"] == "" and c["windows"] == [], r["path"]
        assert c["ranges"] == [] and c["conclusion_generated"] == "", r["path"]
        assert c["params_used"]["x_max_min"] == 6.5, r["path"]
    # the comparison charts are never drawn
    assert harness["figures"] == []


def test_report_log_rows_record_no_standard(harness):
    rows = harness["log_rows"]
    assert sorted(r["kind"] for r in rows) == ["download", "qbench", "zip"], rows
    for row in rows:
        assert row["standard_name"] is None, row
        assert row["bullets"] == [] and row["bullets_text"] == "", row
        assert row["conclusion"] == "" and row["conclusion_edited"] is False, row
        assert row["sample_id"] == harness["sample_id"] and row["pdf_sha256"]
