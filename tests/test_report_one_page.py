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
           conclusion="", comments=(), footer=None, logo=False):
    footer = footer or [PARAMS_LINE, "Ranges: Gas C5–C11, Oil C20–C44"]
    plan = rl.plan(doc_name=doc_name, lab_id=lab_id, std_name=std_name, bullets_text=bullets,
                   conclusion=conclusion, comment_lines=list(comments), footer_lines=footer,
                   has_logo=logo)
    page = rl.render_html(
        doc_name=doc_name, lab_id=lab_id, std_name=std_name, date_display="October 7, 2026",
        datetime_str="2026-10-07 12:00", img1_b64=_png(int(plan.width), int(plan.trend_h)),
        img2_b64=_png(int(plan.width), int(plan.diff_h)), logo_b64=_png(120, 40) if logo else "",
        bullets_text=bullets, conclusion=conclusion, comment_lines=list(comments),
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
        (out / f"layout-{name}-{len(conclusion)}-{len(comments)}.pdf").write_bytes(pdf)
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


def worst_comments(n: int = 100) -> list[str]:
    body = "Re-injected to confirm the light-end shoulder; result consistent with the first run. "
    return [(f"Note {i}: " + body * 8)[:500] + f" (C8–C10, 1.30–2.10 min; ABCD, 2026-09-30)"
            for i in range(n)]


WORST_STD = "Ultra Low Sulfur Diesel Reference Standard, Lot 2026-09 (B), re-certified"
WORST_LAB = "40304-LONG-LAB-ID-2026-09-30-REINJ-07"


def test_a_normal_report_is_one_page_and_prints_everything():
    comments = ["Sample appears to be gasoline. (RB, 2026-09-29)",
                "Narrow mark (C10, 1.90–2.10 min; CD, 2026-09-29)"]
    concl = conclusion_of(300)
    plan, page, pages, text = render(bullets=NORMAL_BULLETS, conclusion=concl, comments=comments)
    assert pages == 1 and pages_without_shrink(page) == 1
    assert plan.notes == [] and plan.fs == rl.TYPE_STEPS[0]
    for line in NORMAL_BULLETS.splitlines():
        assert flat(line) in text
    assert flat(concl) in text
    for c in comments:
        assert c in text
    assert "window 251 pts" in text and "Gas C5–C11, Oil C20–C44" in text
    assert "GC hub v6.0.0" in text
    # a normal report gets tall charts
    assert plan.trend_h >= 220 and plan.diff_h >= 140


def test_a_1500_character_conclusion_prints_whole_at_the_readable_size():
    concl = conclusion_of(rl.CONCLUSION_KEEP)
    plan, page, pages, text = render(bullets=NORMAL_BULLETS, conclusion=concl,
                                     comments=["Sample appears to be gasoline. (RB, 2026-09-29)"])
    assert pages == 1 and pages_without_shrink(page) == 1
    assert plan.conclusion_chars is None and plan.fs == rl.TYPE_STEPS[0]
    assert flat(concl) in text


def test_every_section_at_its_worst_is_still_one_page():
    """1,500-character conclusion, 41 findings, 100 comments of 500
    characters, 40 ranges, a 300-character document name, long lab ID and
    standard name, a logo: one page, the conclusion whole, every parameter
    printed, and what was left out is counted on the page."""
    concl = conclusion_of(rl.CONCLUSION_KEEP)
    ranges = "Ranges: " + ", ".join(f"Range number {i:02d} with a long label xx C{5 + i}–C{8 + i}"
                                    for i in range(40))
    bullets = worst_bullets(WORST_STD)
    comments = worst_comments()
    plan, page, pages, text = render(
        doc_name="GC Analysis of an exceptionally long customer document name " * 5,
        lab_id=WORST_LAB, std_name=WORST_STD, bullets=bullets, conclusion=concl,
        comments=comments, footer=[PARAMS_LINE, ranges], logo=True)
    assert pages == 1 and pages_without_shrink(page) == 1, plan
    assert plan.conclusion_chars is None
    assert flat(concl) in text
    assert flat(bullets.splitlines()[0]) in text
    shown = plan.findings_shown
    assert shown is not None and 1 <= shown < 41
    assert rl.more_findings_text(41 - shown) in text
    assert rl.more_comments_text(100 - (plan.comments_shown or 0), plan.comments_shown) in text
    for part in PARAMS_LINE.split(" · "):
        assert flat(part.replace("Parameters: ", "")) in text, part
    assert "Range number 00 with a long label xx C5–C8" in text and "more" in text
    assert "GC hub v6.0.0" in text
    assert plan.notes


def test_long_unbroken_tokens_wrap_and_stay_on_one_page():
    token = "X" * 400
    path = "\\\\ASAPServer\\Labsharedrive\\" + "very_long_folder_name_" * 12 + "result.CDF"
    concl = conclusion_of(1000) + " " + token
    plan, page, pages, text = render(
        doc_name="D" * 300, lab_id="L" * 80, std_name="S" * 120, bullets=NORMAL_BULLETS,
        conclusion=concl, comments=[path + " (RB, 2026-09-29)"] * 3)
    assert pages == 1 and pages_without_shrink(page) == 1, plan
    assert text.replace(" ", "").count("X" * 400) == 1      # broken across lines, all there


def test_every_interpolated_string_is_escaped():
    _plan, page, pages, _text = render(
        doc_name="<i>Doc</i>", lab_id="<b>40304</b>", std_name="<script>x</script>",
        bullets="• <img src=evil> (C5–C11): HIGHER than <script>x</script> — moderate",
        conclusion="<b>bold</b> & more", comments=["<img src=file:///etc/passwd> (AB, 2026-09-29)"],
        footer=[PARAMS_LINE, "Ranges: Spiky <b> C9–C11"])
    assert pages == 1
    for raw in ("<i>Doc", "<b>40304", "<script>", "<img src=evil", "<img src=file", "<b>bold", "Spiky <b>"):
        assert raw not in page, raw
    assert "&lt;img src=file:///etc/passwd&gt;" in page and "&lt;b&gt;bold&lt;/b&gt; &amp; more" in page


def test_the_plan_is_deterministic():
    kw = dict(doc_name="GC Analysis", lab_id=WORST_LAB, std_name=WORST_STD,
              bullets_text=worst_bullets(WORST_STD), conclusion=conclusion_of(1500),
              comment_lines=worst_comments(), footer_lines=[PARAMS_LINE, "Ranges: none"])
    assert rl.plan(**kw) == rl.plan(**kw)


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
            "ranges": [{"label": f"Range number {i:02d} with a long label xx", "c_start": 5 + 3 * i,
                        "c_end": 7 + 3 * i} for i in range(13)],
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
        assert re.search(r"\d+ (more )?comments on this sample are in the GC hub", pdf), path
        assert "window 251" in pdf and "spike dominance ≥0.6" in pdf, path
