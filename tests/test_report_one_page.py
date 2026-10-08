"""v6.0: the analysis report is always exactly one Letter page.

``report_layout`` (pure: no app import) plans the page from the content and
renders it; these tests push it through xhtml2pdf with placeholder chart
images of the planned size, from the normal report to every section at its
worst at once, and read the PDF back. The real path (kaleido charts, every
export route) is ``tests/test_report_content.py`` (normal case) and the
worst case at the end of this file.
"""
from __future__ import annotations

import base64
import io
import json
import re
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import pytest

pytest.importorskip("xhtml2pdf")
pytest.importorskip("reportlab")
pypdf = pytest.importorskip("pypdf")

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import report_layout as rl  # noqa: E402

_DEFAULT_CANDIDATES = rl._font_candidates


def _vera_only():
    return [c for c in _DEFAULT_CANDIDATES() if os.path.basename(c[0]) == "Vera.ttf"]


@pytest.fixture(params=["default", "vera"])
def face(request):
    """Every page-count test runs in the face this machine would pick (Segoe
    UI on ASAPSV1, Arial on a Mac or Windows CI, DejaVu Sans on Linux) and
    in reportlab's own Bitstream Vera, which is always there and the widest
    of them: the plan must fit in each without the shrink."""
    if request.param == "vera":
        if not _vera_only():
            pytest.skip("reportlab's Vera is not installed")
        rl._font_candidates = _vera_only
    rl._FONTS.clear()
    try:
        yield rl.fonts()
    finally:
        rl._font_candidates = _DEFAULT_CANDIDATES
        rl._FONTS.clear()


def largest_fitting_step(plan: "rl.Plan", **content) -> float:
    """The largest type step at which this content's text fits beside the
    usual minimum charts with nothing shortened (what the plan must pick)."""
    for fs in rl.TYPE_STEPS:
        trial = rl.Plan(trend_h=0, diff_h=0, fs=fs)
        h = rl._text_height(trial, title=rl.display_title(content["doc_name"]),
                            subtitle=rl.subtitle_text(content["std_name"]),
                            lab_id=content["lab_id"], std_name=content["std_name"],
                            rows=rl.finding_rows(content["bullets_text"]),
                            conclusion=content["conclusion"],
                            region_lines=content.get("region_lines", []),
                            note_lines=content.get("note_lines", []),
                            footer_lines=content["footer_lines"])
        if rl.BODY_H - h >= sum(rl.CHART_MIN):
            return fs
    return rl.TYPE_STEPS[-1]


def _is_wide_face(face) -> bool:
    name = os.path.basename(str(face.get("file") or "")).lower()
    return name.startswith(("segoe", "arial"))

PARAMS_LINE = (
    "Parameters: baseline quantile 0.25 · window 251 pts · smoothing 20 pts · thresholds "
    "marginal ≥120, moderate ≥450, significant ≥1800 · x-max 6.5 min · min width 0.05 min · "
    "merge gap 0.1 min · spike min width 0.02 min · spike report ≥450 · spike max FWHM 0.2 min "
    "· spike dominance ≥0.6")
SENTENCES = (
    "Compared with the reference standard, the sample shows elevated intensity in the light "
    "end between C9 and C11, with a sharp peak at 2.03 min that is consistent with a lighter "
    "component blended into the product. The heavy end from C20 to C44 matches the standard "
    "within the marginal threshold. These findings are indicative only and do not confirm "
    "specific substances; a confirmatory run is recommended before release. ")


def conclusion_of(n: int) -> str:
    """Realistic words, exactly *n* characters (the hub caps a conclusion at 1,500)."""
    text = (SENTENCES * (n // len(SENTENCES) + 2))[:n]
    return text[:-1] + "." if text.endswith(" ") else text


def _png(w: int, h: int) -> str:
    raw = b"".join(b"\x00" + b"\xee\xf0\xf3" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return base64.b64encode(data).decode("ascii")


def render(*, doc_name="GC Analysis", lab_id="40304", std_name="Base", bullets="",
           conclusion="", regions=(), notes=(), footer=None, logo=False):
    footer = footer or [PARAMS_LINE, "Ranges: Gas C5–C11, Oil C20–C44"]
    plan = rl.plan(doc_name=doc_name, lab_id=lab_id, std_name=std_name, bullets_text=bullets,
                   conclusion=conclusion, region_lines=list(regions), note_lines=list(notes),
                   footer_lines=footer, has_logo=logo)
    page = rl.render_html(
        doc_name=doc_name, lab_id=lab_id, std_name=std_name, date_display="October 7, 2026",
        datetime_str="2026-10-07 12:00", img1_b64=_png(int(plan.width), int(plan.trend_h)),
        img2_b64=_png(int(plan.width), int(plan.diff_h)), logo_b64=_png(120, 40) if logo else "",
        bullets_text=bullets, conclusion=conclusion, region_lines=list(regions),
        note_lines=list(notes),
        footer_lines=footer, app_version="v6.0.0", plan_=plan, sample_label=lab_id,
        spikes_marked=True)
    from xhtml2pdf import pisa
    rl.fonts()
    buf = io.BytesIO()
    assert not pisa.CreatePDF(page, dest=buf).err
    pdf = buf.getvalue()
    reader = pypdf.PdfReader(io.BytesIO(pdf))
    text = " ".join(" ".join((p.extract_text() or "") for p in reader.pages).split())
    if os.environ.get("GC_REPORT_SHOTS"):
        out = Path(os.environ["GC_REPORT_SHOTS"])
        out.mkdir(parents=True, exist_ok=True)
        name = "".join(ch for ch in lab_id[:12] if ch.isalnum())
        (out / f"layout-{name}-{len(conclusion)}-{len(regions)}-{len(notes)}.pdf"
         ).write_bytes(pdf)
    return plan, page, len(reader.pages), text


def pages_without_shrink(page: str) -> int:
    """The page count with keep-in-frame shrink taken out: the plan's
    measurement alone must fit (the shrink is only the safety net)."""
    from xhtml2pdf import pisa
    marker = '<div style="-pdf-keep-in-frame-mode: shrink;">'
    assert marker in page
    buf = io.BytesIO()
    assert not pisa.CreatePDF(page.replace(marker, "<div>"), dest=buf).err
    return len(pypdf.PdfReader(io.BytesIO(buf.getvalue())).pages)


def flat(s: str) -> str:
    return " ".join(s.replace("•", " ").split())


NORMAL_BULLETS = ("• Gas (C5–C11): HIGHER than Base — moderate (max +562 at 2.03 min; 39% of "
                  "range beyond the marginal threshold; 1 sharp peak above the standard at 2.03 min)\n"
                  "• Outside the defined ranges: no deviations above the marginal threshold.")


def worst_bullets(std: str, n: int = 41) -> str:
    return "\n".join(
        f"• Range number {i:02d} with a long label xx (C{5 + i}–C{8 + i}): HIGHER than {std} — "
        "significant at 1.20–1.50, 2.00–2.40 and 3.00–3.10 min; also lower moderate 4.00–4.20 "
        "min (max +5620 at 2.03 min; 39% of range beyond the marginal threshold; 3 sharp peaks "
        "above, 2 below the standard at 2.03, 2.10, 2.50 min)" for i in range(n))


BODY_500 = "Re-injected to confirm the light-end shoulder; result consistent with the first run. "


def worst_regions(n: int = 50) -> list[str]:
    return [(f"Region {i}: " + BODY_500 * 8)[:500] + " (C8–C10, 1.30–2.10 min; ABCD, 2026-09-30)"
            for i in range(n)]


def worst_notes(n: int = 50) -> list[str]:
    return [(f"Note {i}: " + BODY_500 * 8)[:500] + " (ABCD, 2026-09-30)" for i in range(n)]


WORST_STD = "Ultra Low Sulfur Diesel Reference Standard, Lot 2026-09 (B), re-certified"
WORST_LAB = "40304-LONG-LAB-ID-2026-09-30-REINJ-07"


def test_a_normal_report_is_one_page_and_prints_everything(face):
    regions = ["Narrow mark (C10, 1.90–2.10 min; CD, 2026-09-29)"]
    notes = ["Sample appears to be gasoline. (RB, 2026-09-29)"]
    concl = conclusion_of(300)
    plan, page, pages, text = render(bullets=NORMAL_BULLETS, conclusion=concl, regions=regions,
                                     notes=notes)
    assert pages == 1 and pages_without_shrink(page) == 1
    assert plan.shortened == [] and plan.fs == rl.TYPE_STEPS[0]
    for line in NORMAL_BULLETS.splitlines():
        assert flat(line) in text
    assert flat(concl) in text
    for c in regions + notes:
        assert c in text
    # findings, marked regions, the conclusion, then its notes; no Comments section
    assert "Comments" not in text
    assert (text.index("Findings") < text.index("Marked regions") < text.index(regions[0])
            < text.index("Conclusion") < text.index(flat(concl)[:40]) < text.index("Notes")
            < text.index(notes[0]) < text.index("Parameters"))
    assert "window 251 pts" in text and "Gas C5–C11, Oil C20–C44" in text
    assert "GC hub v6.0.0" in text
    # a normal report gets tall charts
    assert plan.trend_h >= 200 and plan.diff_h >= 125


def test_the_kept_conclusion_length_is_the_hubs_cap():
    """A conclusion up to the cap the hub enforces is never shortened."""
    import comments
    assert rl.CONCLUSION_KEEP == comments.CONCLUSION_MAX


def test_a_1500_character_conclusion_prints_whole_at_the_readable_size(face):
    """Whole, at the largest type step that fits (the body size itself in
    the app's faces, Segoe UI and Arial), on one page without the shrink."""
    concl = conclusion_of(rl.CONCLUSION_KEEP)
    notes = ["Sample appears to be gasoline. (RB, 2026-09-29)"]
    plan, page, pages, text = render(bullets=NORMAL_BULLETS, conclusion=concl, notes=notes)
    assert pages == 1 and pages_without_shrink(page) == 1
    assert plan.conclusion_chars is None and plan.shortened == []
    assert plan.fs == largest_fitting_step(
        plan, doc_name="GC Analysis", lab_id="40304", std_name="Base",
        bullets_text=NORMAL_BULLETS, conclusion=concl, note_lines=notes,
        footer_lines=[PARAMS_LINE, "Ranges: Gas C5–C11, Oil C20–C44"])
    if _is_wide_face(face):
        assert plan.fs == rl.TYPE_STEPS[0]
    assert flat(concl) in text


def test_every_section_at_its_worst_is_still_one_page(face):
    """1,500-character conclusion, 41 findings, 50 marked regions and 50
    notes of 500 characters, 40 ranges, a 300-character document name, long lab ID and
    standard name, a logo: one page, the conclusion whole, every parameter
    printed, and what was left out is counted on the page."""
    concl = conclusion_of(rl.CONCLUSION_KEEP)
    ranges = "Ranges: " + ", ".join(f"Range number {i:02d} with a long label xx C{5 + i}–C{8 + i}"
                                    for i in range(40))
    bullets = worst_bullets(WORST_STD)
    plan, page, pages, text = render(
        doc_name="GC Analysis of an exceptionally long customer document name " * 5,
        lab_id=WORST_LAB, std_name=WORST_STD, bullets=bullets, conclusion=concl,
        regions=worst_regions(), notes=worst_notes(), footer=[PARAMS_LINE, ranges], logo=True)
    assert pages == 1 and pages_without_shrink(page) == 1, plan
    assert plan.conclusion_chars is None
    assert flat(concl) in text
    assert flat(bullets.splitlines()[0]) in text
    shown = plan.findings_shown
    assert shown is not None and 1 <= shown < 41
    assert rl.more_findings_text(41 - shown) in text
    assert plan.regions_shown is not None and plan.notes_shown is not None
    assert rl.more_regions_text(50 - plan.regions_shown, plan.regions_shown) in text
    assert rl.more_notes_text(50 - plan.notes_shown, plan.notes_shown) in text
    for part in PARAMS_LINE.split(" · "):
        assert flat(part.replace("Parameters: ", "")) in text, part
    # v7: every range is named, never "and N more" (names may be shortened)
    k = plan.ranges_name_chars
    assert k is not None and k >= rl.RANGES_NAME_MIN
    for i in range(40):
        name = f"Range number {i:02d} with a long label xx"
        assert flat(f"{name[:k].rstrip()}… C{5 + i}–C{8 + i}") in text, i
    assert " more, " not in text and not re.search(r"and \d+ more", text)
    assert "GC hub v6.0.0" in text
    assert plan.shortened and not any("does not fit" in x for x in plan.shortened)


def test_long_unbroken_tokens_wrap_and_stay_on_one_page(face):
    token = "X" * 400
    path = "\\\\ASAPServer\\Labsharedrive\\" + "very_long_folder_name_" * 12 + "result.CDF"
    concl = conclusion_of(1000) + " " + token
    plan, page, pages, text = render(
        doc_name="D" * 300, lab_id="L" * 80, std_name="S" * 120, bullets=NORMAL_BULLETS,
        conclusion=concl, notes=[path + " (RB, 2026-09-29)"] * 3,
        regions=[path + " (C8–C10, 1.30–2.10 min; RB, 2026-09-29)"] * 3)
    assert pages == 1 and pages_without_shrink(page) == 1, plan
    assert not any("does not fit" in x for x in plan.shortened)
    assert text.replace(" ", "").count("X" * 400) == 1      # broken across lines, all there


def test_every_interpolated_string_is_escaped(face):
    _plan, page, pages, _text = render(
        doc_name="<i>Doc</i>", lab_id="<b>40304</b>", std_name="<script>x</script>",
        bullets="• <img src=evil> (C5–C11): HIGHER than <script>x</script> — moderate",
        conclusion="<b>bold</b> & more", notes=["<img src=file:///etc/passwd> (AB, 2026-09-29)"],
        regions=["<img src=file:///etc/shadow> (C8–C10, 1.30–2.10 min; AB, 2026-09-29)"],
        footer=[PARAMS_LINE, "Ranges: Spiky <b> C9–C11"])
    assert pages == 1
    for raw in ("<i>Doc", "<b>40304", "<script>", "<img src=evil", "<img src=file", "<b>bold",
                "Spiky <b>"):
        assert raw not in page, raw
    assert "&lt;img src=file:///etc/passwd&gt;" in page and "&lt;b&gt;bold&lt;/b&gt; &amp; more" in page


def test_the_plan_is_deterministic():
    kw = dict(doc_name="GC Analysis", lab_id=WORST_LAB, std_name=WORST_STD,
              bullets_text=worst_bullets(WORST_STD), conclusion=conclusion_of(1500),
              region_lines=worst_regions(), note_lines=worst_notes(),
              footer_lines=[PARAMS_LINE, "Ranges: none"])
    assert rl.plan(**kw) == rl.plan(**kw)


def test_section_labels_fit_their_column(face):
    """A section's title never wraps (the measurement counts one line)."""
    for label in rl.SECTION_LABELS:
        assert rl.line_width(label, rl.TYPE_STEPS[0], bold=True) <= rl.LABEL_W - 8, label


def test_a_long_list_of_short_rows_is_measured_exactly(face):
    """Many short rows (8 findings, 12 marked regions, 12 notes): what is
    listed fits without the shrink and nothing more would have."""
    bullets = "\n".join(f"• R{i} (C{i}–C{i + 2}): HIGHER than Base — marginal (max +130 at 2.0{i} min)"
                        for i in range(8))
    regions = [f"Region {i} (C8–C10, 1.30–2.10 min; RB, 2026-09-29)" for i in range(12)]
    notes = [f"Note {i} (RB, 2026-09-29)" for i in range(12)]
    plan, page, pages, _text = render(bullets=bullets, conclusion=conclusion_of(200),
                                      regions=regions, notes=notes)
    assert pages == 1 and pages_without_shrink(page) == 1, plan


def test_finding_rows_keep_the_line_whole():
    rows = rl.finding_rows(NORMAL_BULLETS + "\nNo deviations detected.")
    assert [r["text"] for r in rows] == [
        flat(NORMAL_BULLETS.splitlines()[0]),
        "Outside the defined ranges: no deviations above the marginal threshold.",
        "No deviations detected."]
    assert rows[0]["head"] == "Gas (C5–C11):" and rows[0]["severity"] == "moderate"
    assert rows[0]["head"] + " " + rows[0]["rest"] == rows[0]["text"]
    assert rows[2]["head"] == "" and rows[2]["severity"] is None


# ── the real path: kaleido charts, every export route, worst content ─────
WORST_RANGES = 12


def worst_range_label(i: int) -> str:
    """40 characters, the hub's cap on a range label."""
    label = f"Range number {i:02d} with a long label xxxxxx"
    assert len(label) == 40
    return label


@pytest.fixture(scope="module")
def worst_harness():
    pytest.importorskip("flask")
    pytest.importorskip("netCDF4")
    import numpy as np

    import cdf_fixtures as fx
    import hub_boot

    tmp = Path(tempfile.mkdtemp(prefix="gc-v6-report-"))
    try:
        hub = hub_boot.build_hub(tmp)
        t = fx._axis()
        y = (fx.gaussian(t, 0.30, 50000, 0.01) + fx.gaussian(t, 3.2, 1200, 0.9)
             + fx.gaussian(t, 4.6, 600, 0.6) + 40 + 5 * t)
        y = y - fx.gaussian(t, 2.0, 6000, 0.015)
        fx.write_cdf(hub.standards / f"{WORST_STD}.CDF", t, np.clip(y, 0, None), "Base",
                     hub_boot.LIVE_SINCE)
        request = {
            "standard_name": WORST_STD, "quantile": 0.25, "window": 251, "sigma": 20.0,
            "thresh_marginal": 120, "thresh_moderate": 450, "thresh_significant": 1800,
            "x_max_min": 6.5,
            "ranges": [{"label": worst_range_label(i), "c_start": 5 + 3 * i,
                        "c_end": 7 + 3 * i} for i in range(WORST_RANGES)],
            "conclusion": conclusion_of(1500),
            "doc_name": "GC Analysis of an exceptionally long customer document name " * 3,
            "overlay_standards": [],
        }
        comments = [{"id": 100 + i, "text": (f"Note {i}: " + "re-injected to confirm the "
                                             "light-end shoulder; consistent. " * 10)[:500],
                     "initials": "ABCD", "created_at": "2026-09-30T09:00:00",
                     "t0": 1.0 + i * 0.01 if i % 2 else None, "t1": 2.5 if i % 2 else None}
                    for i in range(100)]
        req = tmp / "request.json"
        req.write_text(json.dumps(request), encoding="utf-8")
        cpath = tmp / "comments.json"
        cpath.write_text(json.dumps(comments), encoding="utf-8")
        out = tmp / "out.json"
        env = {k: v for k, v in os.environ.items()
               if k not in ("PORT", "GC_PORT", "QBENCH_CLIENT_ID", "QBENCH_CLIENT_SECRET")}
        env["HOME"] = str(tmp / "home")
        env["GC_HARNESS_COMMENTS"] = str(cpath)
        (tmp / "home").mkdir()
        proc = subprocess.run(
            [sys.executable, str(TESTS / "report_harness.py"), str(hub.data),
             str(hub.ids["final"]), str(req), str(out)],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600)
        assert proc.returncode == 0 and out.exists(), proc.stderr[-4000:] + proc.stdout[-2000:]
        yield dict(json.loads(out.read_text(encoding="utf-8")), request=request)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_worst_case_reports_are_one_page_on_every_path(worst_harness):
    assert worst_harness["pdf_pages"] == {"direct": 1, "zip": 1, "qbench": 1}
    concl = worst_harness["request"]["conclusion"]
    for path, pdf in worst_harness["pdf_text"].items():
        assert flat(concl) in pdf, path
        assert re.search(r"\d+ (more )?notes? (is|are) in the GC hub", pdf), path
        assert "Comments" not in pdf and "Marked regions" in pdf, path
        assert "window 251" in pdf and "spike dominance ≥0.6" in pdf, path


def test_worst_case_charts_label_the_same_bands_on_both(worst_harness):
    """12 ranges: the wide ones fall back to their carbons, the narrow ones
    carry their number, and the two charts agree band for band."""
    def texts(fig):
        return [a["text"] for a in fig["layout"]["annotations"] or []
                if a.get("yref") == "paper" and a.get("y") == 1]
    for rec in worst_harness["figures"]:
        trend, diff = rec["figs"]
        assert texts(trend) == texts(diff)
        assert texts(trend)[:2] == ["C5–C7", "C8–C10"], texts(trend)
        assert len(texts(trend)) == WORST_RANGES          # every band labelled or numbered
        assert any(rl.is_tag(t) for t in texts(trend))
        assert sum(s["type"] == "rect" for s in diff["layout"]["shapes"]) == WORST_RANGES


def test_worst_case_reports_name_every_range(worst_harness):
    """v7 (Ryan: every range added on the comparison reflects on the report):
    on the most crowded page every range is still named in the PDF's text,
    in order with its carbons; a numbered band's number is in its entry."""
    assert worst_harness["pdf_pages"] == {"direct": 1, "zip": 1, "qbench": 1}
    tags = {t for rec in worst_harness["figures"] for t in
            (a["text"] for a in rec["figs"][0]["layout"]["annotations"] or [])
            if rl.is_tag(t)}
    for path, pdf in worst_harness["pdf_text"].items():
        assert re.search(r"and \d+ more", pdf) is None, path
        line = pdf[pdf.rindex("Ranges ") + len("Ranges "):pdf.rindex(" Generated")]
        entries = rl.range_entries(line)
        assert len(entries) == WORST_RANGES, (path, line)
        for i, (name, carbons) in enumerate(entries):
            assert carbons == f"C{5 + 3 * i}–C{7 + 3 * i}", (path, line)
            tag = f"({i + 1}) "
            assert name.startswith(tag) == (str(i + 1) in tags), (path, name)
            name = name.removeprefix(tag)
            label = worst_range_label(i)
            assert name == label or (name.endswith("…") and len(name) - 1 >= rl.RANGES_NAME_MIN
                                     and label.startswith(name[:-1])), (path, name)


# ── v7: range labels and legend swatches (pure) ──────────────────────────
def _win(t0, t1, cs, ce, label="", evaluable=True, index=0):
    return {"t0": t0, "t1": t1, "c_start": cs, "c_end": ce, "label": label,
            "evaluable": evaluable, "index": index}


def test_band_labels_fall_back_from_name_to_carbons_to_the_ranges_number(face):
    plot_w = 500.0                                    # pt for 0–10 min: 50 pt a minute
    wins = [_win(0.0, 4.0, 5, 11, "Gasoline", index=0),        # 200 pt: the full label
            _win(4.2, 5.0, 12, 14, "A very long range name", index=1),   # 40 pt: carbons
            _win(5.2, 5.4, 20, 22, "Narrow", index=2),         # 10 pt: its number
            _win(6.0, 9.0, 30, 40, "Skipped", evaluable=False, index=3)]
    out = rl.band_labels(wins, 0.0, 10.0, plot_w)
    assert [(t, row) for _w, t, _x, row in out] == [
        ("Gasoline C5–C11", 0), ("C12–C14", 0), ("3", 0)]
    assert out[2][2] == pytest.approx(5.3)            # a tag is centred on its band
    assert rl.is_tag("3") and not rl.is_tag("C12–C14")


def test_band_labels_start_clear_of_an_overlapping_bands_edge(face):
    wins = [_win(0.0, 3.0, 5, 11, "Gasoline"), _win(2.0, 8.0, 10, 22, "Diesel", index=1)]
    out = rl.band_labels(wins, 0.0, 10.0, 500.0)
    assert [(t, x) for _w, t, x, _r in out] == [("Gasoline C5–C11", 0.0), ("Diesel C10–C22", 3.0)]


def test_band_labels_never_touch_each_other(face):
    wins = [_win(i * 0.5, i * 0.5 + 0.9, 5 + i, 6 + i, f"R{i}", index=i) for i in range(12)]
    out = rl.band_labels(wins, 0.0, 10.0, 500.0)
    for row in (0, 1):
        spans = []
        for _w, t, x, r in out:
            if r != row:
                continue
            wd = rl.line_width(t, rl.BAND_LABEL_FS)
            spans.append((x * 50 - wd / 2 - 1, x * 50 + wd / 2 + 1) if rl.is_tag(t)
                         else (x * 50, x * 50 + wd + 2 * rl.BAND_LABEL_PAD))
        assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), (row, spans)


def test_narrow_neighbours_stagger_their_numbers_into_a_second_row(face):
    wins = [_win(1.0 + i * 0.12, 1.0 + i * 0.12 + 0.1, 20 + 2 * i, 21 + 2 * i, "Narrow",
                 index=i + 8) for i in range(6)]    # 5 pt bands, 1 pt apart
    out = rl.band_labels(wins, 0.0, 10.0, 500.0)
    assert {t for _w, t, _x, _r in out} <= {str(i + 9) for i in range(6)}
    assert {r for *_a, r in out} == {0, 1} and len(out) >= 4
    assert rl.band_lane_pt(2) > rl.band_lane_pt(1) > rl.band_lane_pt(0) == 0


def test_the_ranges_line_never_leaves_a_range_out():
    line = "Gas, light C5–C11, (3) Oil C20–C44, Single C7"
    assert rl.range_entries(line) == [("Gas, light", "C5–C11"), ("(3) Oil", "C20–C44"),
                                      ("Single", "C7")]
    p = rl.Plan(trend_h=0, diff_h=0, fs=8.5)
    assert rl.ranges_text(line, p) == line
    p.ranges_name_chars = 4
    assert rl.ranges_text(line, p) == "Gas,… C5–C11, (3) Oil C20–C44, Sing… C7"
    p.ranges_on_charts = True
    assert rl.ranges_text(line, p) == "3 ranges, each named on both charts"
    assert rl.ranges_text("none", rl.Plan(trend_h=0, diff_h=0, fs=8.5, ranges_name_chars=3)) == "none"


def _pixels(png: bytes):
    """Decode a swatch (8-bit RGB, filter 0, as swatch_png writes it)."""
    import zlib
    w, h = struct.unpack(">II", png[16:24])
    raw = zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])
    rows = [raw[y * (3 * w + 1) + 1:(y + 1) * (3 * w + 1)] for y in range(h)]
    return [[tuple(r[3 * x:3 * x + 3]) for x in range(w)] for r in rows]


def test_legend_swatches_carry_the_charts_colour_and_texture():
    above, below = _pixels(rl.swatch_png("above")), _pixels(rl.swatch_png("below"))
    hexrgb = rl._hex
    for px, fill, ink in ((above, rl.DEV_ABOVE_FILL, rl.DEV_ABOVE),
                          (below, rl.DEV_BELOW_FILL, rl.DEV_BELOW)):
        colours = {p for row in px for p in row}
        assert colours == {hexrgb(fill), hexrgb(ink)}

    def grey(px):            # the texture as a printer would see it
        return [[sum(p) > 3 * 160 for p in row] for row in px]
    assert grey(above) != grey(below)                # hatch vs dots, not just hue
    sample = {p for row in _pixels(rl.swatch_png("sample")) for p in row}
    assert hexrgb(rl.SAMPLE) in sample


def test_the_chart_legends_fit_their_column(face):
    """The keys stay on one line in every face (the plan counts one)."""
    sp = rl.line_width(" ", 7)
    swatch = 12 + sp
    gap = 3 * sp
    trend = 3 * swatch + sum(rl.line_width(t, 7) for t in ("standard", "sample", "range")) + 2 * gap
    diff = (2 * swatch + rl.line_width("▲▼", 7) + sp
            + sum(rl.line_width(t, 7) for t in ("higher than the standard", "lower",
                                                 "counted spike")) + 2 * gap)
    assert trend < rl.KEY_W and diff < rl.KEY_W, (trend, diff)
