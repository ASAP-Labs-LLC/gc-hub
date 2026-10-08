"""v7: every range the operator set prints on every report path.

The request carries what the Compare view sends after the operator edited
ranges in Adjust: eight ranges, out of order on purpose, among them a
40-character label, a single carbon, a reversed pair and one beyond the run
(not evaluable: it can't be drawn). ``tests/report_harness.py`` sends it
through ``/api/analysis``, the direct export, the queue/ZIP export and the
QBench upload, and records each report's content, its trend chart's bands
and the PDF text. Each PDF must list every range in the operator's order
(the footer's Ranges line), say the one beyond the run was not evaluated,
and draw exactly the windows ``/api/analysis`` returned (the windows the UI
draws).
"""
from __future__ import annotations

import re

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

FORTY = "A forty character range label, exactly!"
RANGES = [
    {"label": "Gas", "c_start": 5, "c_end": 11, "color": "#3fb95044"},
    {"label": FORTY, "c_start": 12, "c_end": 16, "color": "#d2992244"},
    {"label": "Heavy tail beyond the run", "c_start": 60, "c_end": 80, "color": "#3498db44"},
    {"label": "Oil", "c_start": 20, "c_end": 44, "color": "#8e44ad44"},
    {"label": "Single", "c_start": 14, "c_end": 14},
    {"label": "Reversed", "c_start": 18, "c_end": 15},
    {"label": "Kero", "c_start": 9, "c_end": 16},
    {"label": "Eighth range", "c_start": 30, "c_end": 34},
]
REQUEST = {"standard_name": "Base", "x_max_min": 6.5, "ranges": RANGES, "conclusion": "",
           "doc_name": "GC Analysis", "overlay_standards": []}
PATHS = ("direct", "zip", "qbench")


def footer_name(r):
    lo, hi = sorted((r["c_start"], r["c_end"]))
    return f"{r['label']} C{lo}–C{hi}"


@pytest.fixture(scope="module")
def run():
    pytest.importorskip("flask")
    pytest.importorskip("netCDF4")
    pytest.importorskip("pypdf")
    pytest.importorskip("xhtml2pdf")
    import numpy as np

    import cdf_fixtures as fx
    import hub_boot

    tmp = Path(tempfile.mkdtemp(prefix="gc-v7-ranges-"))
    try:
        hub = hub_boot.build_hub(tmp)
        t = fx._axis()
        y = (fx.gaussian(t, 0.30, 50000, 0.01) + fx.gaussian(t, 3.2, 1200, 0.9)
             + fx.gaussian(t, 4.6, 600, 0.6) + 40 + 5 * t)
        y = y - fx.gaussian(t, 2.0, 6000, 0.015)
        fx.write_cdf(hub.standards / "Base.CDF", t, np.clip(y, 0, None), "Base",
                     hub_boot.LIVE_SINCE)
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
        yield json.loads(out.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_every_path_reports_every_range_in_order(run):
    assert {k: v["status"] for k, v in run["responses"].items()} == \
        {"analysis": 200, "direct": 200, "zip": 200, "qbench": 200}
    want = [r["label"] for r in RANGES]
    for rec in run["records"]:
        assert [r["label"] for r in rec["content"]["ranges"]] == want, rec["path"]
        assert [w["label"] for w in rec["content"]["windows"]] == want, rec["path"]


def test_every_pdf_lists_every_range_in_the_footer_in_order(run):
    """Every range, in order. A range the charts can only label by its number
    is listed with that number first ("(5) Single C14–C14", v7 report charts)."""
    names = [footer_name(r) for r in RANGES]
    expected = re.compile("Ranges " + ", ".join(
        rf"(?:\({i}\) )?{re.escape(n)}" for i, n in enumerate(names, start=1)))
    for path in PATHS:
        text = run["pdf_text"][path]
        assert expected.search(text), (path, text[-900:])


def test_a_range_beyond_the_run_is_listed_as_not_evaluated(run):
    windows = run["responses"]["analysis"]["json"]["windows"]
    beyond = [w for w in windows if w["label"] == "Heavy tail beyond the run"]
    assert beyond and beyond[0]["evaluable"] is False
    line = "Heavy tail beyond the run (C60–C80): not evaluated"
    assert line in run["responses"]["analysis"]["json"]["text"]
    for path in PATHS:
        assert line in run["pdf_text"][path], path


def test_the_report_bands_are_the_windows_the_ui_draws(run):
    """The UI draws a band per evaluable window of /api/analysis's answer;
    every PDF's trend chart draws exactly those."""
    windows = run["responses"]["analysis"]["json"]["windows"]
    drawn = [[w["t0"], w["t1"]] for w in windows if w["evaluable"]]
    assert len(drawn) == len(RANGES) - 1
    assert len(run["bands"]) == len(PATHS)
    for bands in run["bands"]:
        assert [pytest.approx(b) for b in bands] == drawn
