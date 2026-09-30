"""The Analysis tab's best-fit auto-select, in headless Chrome: clicking a
sample picks the best-fit comparison standard, shows it selected in the
standards list and runs the analysis, as a manual pick of that standard
would. (It used to call an undefined ``renderAnalysisStandards``: the
standard was set in state, but the list was not redrawn and the analysis
never ran.)

Skipped when selenium or a Chrome/chromedriver can't be started. Plotly is
stubbed before the page loads; ``/api/analysis`` posts are counted.
"""
from __future__ import annotations

import json
import sys
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
from hub_boot import build_hub  # noqa: E402
from ui_wait import click_when_ready, wait_for  # noqa: E402

PRELOAD = """
if (!window.Plotly) {
  window.Plotly = { react(){}, newPlot(){}, purge(){}, relayout(){}, restyle(){},
                    Plots: { resize(){} }, d3: null, __stub: true };
}
window.__analysis = [];
const __fetch = window.fetch;
window.fetch = function (url, opts) {
  if (String(url).indexOf('/api/analysis') === 0 && opts && opts.method === 'POST') {
    try { window.__analysis.push(JSON.parse(opts.body)); } catch (e) {}
  }
  return __fetch.apply(this, arguments);
};
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
    drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": PRELOAD})
    return drv


def test_clicking_a_sample_selects_the_best_fit_standard_and_runs_the_analysis(tmp_path):
    hub = build_hub(tmp_path)
    settings = hub.data / "settings.json"
    conf = json.loads(settings.read_text(encoding="utf-8"))
    conf["bestfit_enabled"] = "true"
    settings.write_text(json.dumps(conf, indent=1), encoding="utf-8")
    sid = hub.ids["final"]          # the "Diesel" standard is a copy of this run
    with booted(tmp_path) as (port, _proc, _data, _home):
        drv = _driver()
        try:
            browser_sign_in(drv, port)
            drv.get(f"http://127.0.0.1:{port}/classic")
            # the page has finished starting up (lists, standards, settings)
            assert wait_for(drv, lambda: drv.execute_script(
                "return !!(window.DeepLink && DeepLink.started) && state.files.length > 0"
                " && state.comparisonStandards.length > 0"), 30)
            assert click_when_ready(drv, '.tab-btn[data-tab="tab-analysis"]')
            # find + click in one retried step: the lists are rebuilt when the
            # flags/best-fit cache fills in, which can stale a found element
            assert click_when_ready(drv, f'#analysis-sample-list li[data-sample-id="{sid}"]')
            assert wait_for(drv, lambda: drv.execute_script(
                "return state.selectedSample && state.selectedSample.sample_id") == sid)

            # the best-fit standard is picked and shown selected in the list
            assert wait_for(drv, lambda: drv.execute_script(
                "return state.selectedStandard && state.selectedStandard.name") == "Diesel", 30), \
                drv.execute_script("return [state.bestFit, state.selectedStandard]")
            assert wait_for(drv, lambda: drv.execute_script("""
                const li = document.querySelector('#analysis-standards-list li.selected');
                return li ? li.textContent : null;""") == "Diesel")
            # ... and the analysis runs with it, and renders
            assert wait_for(drv, lambda: any(
                a.get("sample_id") == sid and a.get("standard_name") == "Diesel"
                for a in drv.execute_script("return window.__analysis")), 30), \
                drv.execute_script("return window.__analysis")
            assert wait_for(drv, lambda: drv.execute_script(
                "return state.analysisResult && state._renderedAnalysisSampleId") == sid, 30)
            assert drv.execute_script("return state.standardPinned") is False
        finally:
            drv.quit()
