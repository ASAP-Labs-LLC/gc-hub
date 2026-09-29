"""One report content for every path (phase 3).

``app._report_content`` is the only analysis pass behind ``/api/analysis``,
the direct export, the queue/ZIP export and the QBench PDF. The harness
(``tests/report_harness.py``, run in a subprocess against a ``hub_boot``
data folder) calls all four with the same request (non-default trend
parameters and thresholds, custom ranges, a client ``bullets`` string that
must be ignored) and records what each one computed and printed.
"""
from __future__ import annotations

import ast
import json
import os
import re
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

REQUEST = {
    "standard_name": "Base",
    "quantile": 0.25, "window": 251, "sigma": 20.0,
    "thresh_marginal": 120, "thresh_moderate": 450, "thresh_significant": 1800,
    "x_max_min": 6.5,
    "ranges": [{"label": "Spiky <b>", "c_start": 9, "c_end": 11, "color": "#ff0000"},
               {"label": "Oil", "c_start": 20, "c_end": 44, "color": "#a05014"}],
    "conclusion": "",
    "bullets": "INJECTED client bullet",
    "doc_name": "GC <i>Analysis</i>",
    "overlay_standards": [],
}


@pytest.fixture(scope="module")
def harness():
    pytest.importorskip("flask")
    pytest.importorskip("netCDF4")
    pytest.importorskip("pypdf")
    pytest.importorskip("xhtml2pdf")
    import numpy as np

    import cdf_fixtures as fx
    import hub_boot

    tmp = Path(tempfile.mkdtemp(prefix="gc-p3-report-"))
    try:
        hub = hub_boot.build_hub(tmp)
        # the standard: the final sample's signal minus a narrow tall spike at
        # 2.0 min (C10 on the fixture ladder), which only the spike channel sees
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


def test_every_path_answered(harness):
    r = harness["responses"]
    assert r["analysis"]["status"] == 200, r["analysis"]
    assert r["direct"]["status"] == 200 and r["zip"]["status"] == 200
    assert r["qbench"]["status"] == 200, r["qbench"]
    assert set(harness["pdf_text"]) == {"direct", "zip", "qbench"}


def test_report_content_is_identical_across_the_four_paths(harness):
    recs = harness["records"]
    assert sorted(r["path"] for r in recs) == ["analysis", "direct", "qbench", "zip"]
    first = recs[0]["content"]
    for r in recs[1:]:
        assert r["content"] == first, r["path"]


def test_the_request_parameters_are_used_everywhere(harness):
    p = harness["records"][0]["content"]["params_used"]
    assert p == {"quantile": 0.25, "window": 251, "sigma": 20.0, "thresh_marginal": 120.0,
                 "thresh_moderate": 450.0, "thresh_significant": 1800.0, "x_max_min": 6.5,
                 "min_width_min": 0.05, "merge_gap_min": 0.10, "spike_min_width_min": 0.02,
                 "spike_report_threshold": 450.0}
    assert [r["label"] for r in harness["records"][0]["content"]["ranges"]] == \
        ["Spiky <b>", "Oil"]


def test_analysis_returns_the_same_items_text_and_windows(harness):
    body = harness["responses"]["analysis"]["json"]
    content = harness["records"][0]["content"]
    assert body["text"] == content["text"]
    assert body["items"] == content["items"]
    assert body["windows"] == content["windows"]
    assert body["params_used"] == content["params_used"]
    assert body["conclusion"] == content["conclusion_generated"]
    assert body["diff"]["x_range"] == [0, 6.5]
    assert "report" not in body and "segments" not in body
    # the spike at 2.0 min is in the report and marked on the plot
    assert body["text"].startswith("• Spiky <b> (C9–C11): HIGHER than Base")
    assert [round(s["t"], 1) for s in body["spikes"]] == [2.0]


def _flat(s: str) -> str:
    return " ".join(s.replace("•", " ").split())


def test_every_pdf_prints_the_computed_bullets_never_the_client_ones(harness):
    text = harness["records"][0]["content"]["text"]
    for path, pdf in harness["pdf_text"].items():
        for line in text.splitlines():
            assert _flat(line) in pdf, (path, line, pdf[:3000])
        assert "INJECTED" not in pdf, path
        assert "Sample appears to be gasoline. (RB, 2026-09-29)" in pdf, path
        conclusion = harness["records"][0]["content"]["conclusion_generated"]
        assert _flat(conclusion) in pdf, path


def test_pdf_footer_lists_parameters_ranges_and_version(harness):
    for path, pdf in harness["pdf_text"].items():
        assert "window 251" in pdf and "marginal ≥120" in pdf, (path, pdf[-1500:])
        assert "x-max 6.5 min" in pdf and "min width 0.05 min" in pdf, path
        assert "Spiky <b> C9–C11" in pdf and "Oil C20–C44" in pdf, path
        assert re.search(r"GC hub (dev|v\d)", pdf), path


def test_report_html_escapes_every_interpolated_string(harness):
    assert len(harness["htmls"]) == 3
    for html in harness["htmls"]:
        assert "<img src=file" not in html
        assert "&lt;img src=file:///etc/passwd&gt; &amp; more" in html
        assert "<i>Analysis</i>" not in html and "GC &lt;i&gt;Analysis&lt;/i&gt;" in html
        assert "Spiky <b>" not in html and "Spiky &lt;b&gt;" in html


def test_report_log_rows_for_every_export(harness):
    rows = harness["log_rows"]
    assert [r["kind"] for r in rows] == ["download", "zip", "qbench"]
    content = harness["records"][0]["content"]
    for r in rows:
        assert r["bullets_text"] == content["text"]
        assert r["bullets"] == content["items"]
        assert r["params"] == content["params_used"]
        assert r["windows"] == content["windows"]
        assert r["ranges"] == content["ranges"]
        assert r["comment_ids"] == [7, 8]
        assert r["conclusion"] == content["conclusion_generated"]
        assert r["conclusion_edited"] is False
        assert r["db_given"] and r["author_initials"] is None
        assert re.fullmatch(r"[0-9a-f]{64}", r["pdf_sha256"])
        assert r["standard_name"] == "Base" and r["revision"] >= 1
        assert r["app_version"]


def _app_src() -> str:
    return (ROOT / "app.py").read_text(encoding="utf-8")


def test_no_client_bullets_anywhere_in_app():
    src = _app_src()
    assert 'get("bullets"' not in src and "get('bullets'" not in src
    assert '["bullets"]' not in src and "['bullets']" not in src


def test_every_report_path_goes_through_report_content():
    tree = ast.parse(_app_src())
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def calls(name):
        return {c.func.id for c in ast.walk(fns[name])
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "_report_content" in calls("api_analysis")
    assert "_report_content" in calls("_build_report_pdf")
    for route in ("api_export_analysis_report", "api_export_analysis_reports_zip",
                  "api_qbench_upload"):
        assert {"_build_report_pdf", "_report_request"} <= calls(route), route
    assert "resolve_report_ranges" in ast.unparse(fns["_report_content"])
    assert "_run_export_analysis" not in fns
