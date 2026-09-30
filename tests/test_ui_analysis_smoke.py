"""Phase 3: a headless-Chrome smoke of the Analysis tab.

The range boxes come from ``/api/analysis``'s ``windows`` (not from
``/api/calibration``), the difference plot shows the ±threshold lines and
the counted spikes, the deviation report and the export modal's bullets are
read-only server text, and a queued item captures the parameters and never
carries bullets.

Plotly calls are recorded (a thin wrapper over the real library, or over a
stub when its CDN is unreachable). Skipped when selenium or a
Chrome/chromedriver can't be started.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")
webdriver = pytest.importorskip("selenium.webdriver")

from bootapp import browser_sign_in, booted  # noqa: E402
from hub_boot import LIVE_SINCE, build_hub  # noqa: E402

RECORDER = """
(function () {
  window.__plots = {};
  function wrap(P) {
    if (!P || P.__recorded) return P;
    for (const fn of ['react', 'newPlot']) {
      const orig = P[fn];
      P[fn] = function (div, traces, layout, config) {
        const id = (typeof div === 'string') ? div : (div && div.id);
        window.__plots[id] = { traces: traces, layout: layout };
        try { return orig ? orig.apply(this, arguments) : undefined; } catch (e) { return undefined; }
      };
    }
    P.__recorded = true;
    return P;
  }
  let current = wrap(window.Plotly || { react(){}, newPlot(){}, purge(){}, relayout(){},
                                         restyle(){}, Plots: { resize(){} }, __stub: true });
  Object.defineProperty(window, 'Plotly', {
    configurable: true,
    get() { return current; },
    set(v) { current = wrap(v); },
  });
})();
"""


def _driver():
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1600,1000",
                "--disable-dev-shm-usage"):
        opts.add_argument(arg)
    try:
        drv = webdriver.Chrome(options=opts)
    except Exception as exc:  # noqa: BLE001 - no Chrome / driver here
        pytest.skip(f"headless Chrome unavailable: {exc}")
    drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": RECORDER})
    return drv


def _wait(pred, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = pred()
        except Exception:  # noqa: BLE001 - DOM not ready yet
            v = None
        if v:
            return v
        time.sleep(0.2)
    return pred()


@pytest.fixture(scope="module")
def page():
    import numpy as np

    import cdf_fixtures as fx
    tmp = Path(tempfile.mkdtemp(prefix="gc-p3-ui-"))
    drv = None
    try:
        hub = build_hub(tmp)
        t = fx._axis()
        y = (fx.gaussian(t, 0.30, 50000, 0.01) + fx.gaussian(t, 3.2, 1200, 0.9)
             + fx.gaussian(t, 4.6, 600, 0.6) + 40 + 5 * t)
        y = y - fx.gaussian(t, 2.0, 6000, 0.015)
        fx.write_cdf(hub.standards / "Base.CDF", t, np.clip(y, 0, None), "Base", LIVE_SINCE)
        with booted(tmp) as (port, _proc, _data, _home):
            drv = _driver()
            browser_sign_in(drv, port)
            drv.get(f"http://127.0.0.1:{port}/classic")
            _wait(lambda: drv.execute_script(
                "return state.files && state.files.length > 0 && "
                "state.comparisonStandards.some(s => s.name === 'Base')"))
            sid = hub.ids["final"]
            drv.set_script_timeout(120)
            result = drv.execute_async_script("""
                const done = arguments[arguments.length - 1];
                // a deliberately wrong gc1 calibration: boxes must not use it
                state.calibration = { peak_times: [0.1, 0.2], carbon_numbers: [1, 2],
                                      boiling_points: [] };
                state.selectedSample = state.files.find(f => f.sample_id === %d);
                state.selectedStandard = state.comparisonStandards.find(s => s.name === 'Base');
                state.rangeOverlays = [
                  { id: 1, label: 'Spiky', c_start: 9, c_end: 11, color: '#ff000044' },
                  { id: 2, label: 'Oil', c_start: 20, c_end: 44, color: '#a0501444' }];
                populateAnalysisParamInputs();
                document.getElementById('param-thresh-marginal').value = '120';
                runAnalysis().then(() => setTimeout(() => done(state.analysisResult), 300));
            """ % sid)
            yield drv, result
    finally:
        if drv is not None:
            drv.quit()
        shutil.rmtree(tmp, ignore_errors=True)


def test_range_boxes_come_from_the_analysis_windows(page):
    drv, result = page
    assert result and result["windows"], result
    for plot in ("analysis-trend-plot", "analysis-diff-plot"):
        shapes = drv.execute_script(f"return window.__plots['{plot}'].layout.shapes")
        rects = [s for s in shapes if s.get("type") == "rect"]
        want = [(w["t0"], w["t1"]) for w in result["windows"] if w["evaluable"]]
        assert [(r["x0"], r["x1"]) for r in rects] == want, (plot, rects)
    # the trend plot's calibration lines are the revision's ladder, not
    # /api/calibration's
    shapes = drv.execute_script("return window.__plots['analysis-trend-plot'].layout.shapes")
    lines = [s["x0"] for s in shapes if s.get("type") == "line" and s.get("yref") == "paper"]
    assert 0.1 not in lines and set(result["cal_times"]) <= set(lines), lines


def test_difference_plot_has_threshold_lines_and_spike_markers(page):
    drv, result = page
    layout = drv.execute_script("return window.__plots['analysis-diff-plot'].layout")
    levels = sorted({round(s["y0"], 6) for s in layout["shapes"]
                     if s.get("type") == "line" and s.get("yref") == "y"
                     and s["line"].get("dash") == "dot"})
    p = result["params_used"]
    assert p["thresh_marginal"] == 120
    want = sorted(sg * p[k] for k in ("thresh_marginal", "thresh_moderate",
                                      "thresh_significant") for sg in (1, -1))
    assert levels == want
    traces = drv.execute_script("return window.__plots['analysis-diff-plot'].traces")
    markers = [tr for tr in traces if tr.get("mode") == "markers"]
    assert len(markers) == 1 and markers[0]["x"] == [s["t"] for s in result["spikes"]]
    assert result["spikes"], result["text"]


def test_deviation_report_is_the_server_text_and_read_only(page):
    drv, result = page
    info = drv.execute_script("""
        const el = document.getElementById('analysis-report-text');
        return { tag: el.tagName, editable: el.isContentEditable, text: el.textContent };""")
    assert info["tag"] == "PRE" and not info["editable"]
    assert info["text"].endswith(result["text"]), info["text"]
    assert result["text"].startswith("• Spiky (C9–C11): HIGHER than Base")


def test_export_modal_preview_is_read_only_and_queue_captures_params(page):
    drv, result = page
    info = drv.execute_script("""
        openAnalysisExportModal();
        const b = document.getElementById('export-bullets');
        const out = { readOnly: b.readOnly, value: b.value };
        confirmAddToQueue();
        out.item = JSON.parse(JSON.stringify(state.analysisQueue[state.analysisQueue.length - 1]));
        out.payload = buildReportItemPayload(out.item, state.rangeOverlays, state.analysisParams);
        return out;""")
    assert info["readOnly"] is True and info["value"] == result["text"]
    item = info["item"]
    assert "bullets" not in item
    assert item["params"]["thresh_marginal"] == 120
    assert [r["label"] for r in item["ranges"]] == ["Spiky", "Oil"]
    assert "bullets" not in info["payload"] and info["payload"]["thresh_marginal"] == 120


def test_saved_empty_overlays_load_as_no_ranges(page):
    """Phase 3 review (I6): a saved analysis_range_overlays of "[]" means no
    ranges in the UI too, as on the server; nothing saved → Gas/Oil."""
    drv, _ = page
    drv.set_script_timeout(30)
    got = drv.execute_async_script("""
        const done = arguments[arguments.length - 1];
        const realGet = apiGet;
        const run = async (overlays) => {
            apiGet = async (url) => {
                const r = await realGet(url);
                return url === '/api/settings' ? Object.assign({}, r,
                    { analysis_range_overlays: overlays }) : r;
            };
            try { await loadSettings(); } finally { apiGet = realGet; }
            return state.rangeOverlays.map(r => r.label);
        };
        (async () => done({ empty: await run('[]'), unset: await run('') }))();
    """)
    assert got == {"empty": [], "unset": ["Gas", "Oil"]}
