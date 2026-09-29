"""The QBench upload builds its PDF with the report export's analysis
(2A1 T4 carry-over, done in T5): both detection channels (trend + spike) and
the saved range overlays, exactly as ``/api/export-analysis-report``.

The upload itself (Selenium, a live QBench) is never run in a test, so the
wiring is pinned by AST (the upload goes through ``_build_report_pdf`` and
no longer computes a trend-only difference) and the shared analysis is
checked end to end through the report route: a narrow spike that only the
spike channel sees, inside a saved overlay, shows in the PDF's text.
"""
from __future__ import annotations

import ast
import io
import json
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"app.{name} not found")


def _called(fn: ast.AST) -> set:
    return {n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def test_qbench_upload_builds_its_pdf_with_the_export_analysis():
    calls = _called(_function("api_qbench_upload"))
    # phase 3: one report builder for every export (_report_content inside)
    assert {"_build_report_pdf", "_report_request"} <= calls
    # the old trend-only path is gone
    assert not calls & {"compute_trend_line", "detect_deviation_segments"}


def test_the_shared_analysis_reports_a_spike_in_a_saved_overlay():
    pytest.importorskip("flask")
    pytest.importorskip("netCDF4")
    pypdf = pytest.importorskip("pypdf")
    import numpy as np

    import cdf_fixtures as fx
    import hub_boot
    from bootapp import cookie_header, booted, wait_for

    tmp = Path(tempfile.mkdtemp(prefix="gc-t5-qb-"))
    try:
        hub = hub_boot.build_hub(tmp)
        # the standard: the final sample's own signal minus a narrow tall spike
        # at 2.0 min (C10 on the fixture ladder), so sample - standard is
        # exactly that spike, which the rolling low-quantile trend erases.
        t = fx._axis()
        y = (fx.gaussian(t, 0.30, 50000, 0.01) + fx.gaussian(t, 3.2, 1200, 0.9)
             + fx.gaussian(t, 4.6, 600, 0.6) + 40 + 5 * t)
        y = y - fx.gaussian(t, 2.0, 6000, 0.015)
        fx.write_cdf(hub.standards / "Base.CDF", t, np.clip(y, 0, None), "Base",
                     hub_boot.LIVE_SINCE)
        conf = json.loads((hub.data / "settings.json").read_text())
        conf["analysis_range_overlays"] = json.dumps(
            [{"label": "Spiky", "c_start": 9, "c_end": 11, "color": "#ff0000"}])
        (hub.data / "settings.json").write_text(json.dumps(conf))
        with booted(tmp) as (port, _proc, _data, _home):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/export-analysis-report",
                data=json.dumps({"sample_id": hub.ids["final"], "standard_name": "Base"}).encode(),
                method="POST", headers={"Content-Type": "application/json",
                                        **cookie_header(port)})
            with urllib.request.urlopen(req, timeout=120) as r:
                pdf = r.read()
        text = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(pdf)).pages)
        flat = " ".join(text.split())
        assert "Spiky (C9–C11): HIGHER than Base" in flat, flat[:2000]
        assert "spiky range" in flat.lower(), flat[:2000]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
