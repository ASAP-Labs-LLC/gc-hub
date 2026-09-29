"""Carbon labels per instrument (phase 3+4 integration).

A sample is labelled with the ladder of the revision it was computed with
(``calibration_used`` anchors, via ``app._revision_ladder``), served with its
trace. A gc2 sample must never be labelled with gc1's calibration (what
``/api/calibration`` returns), and nothing invents a carbon number.

The hub from ``hub_boot`` is given a gc2 sample with two revisions whose
anchors differ from gc1's (and from each other). Covers the trace route
(current and ``?revision=``), then, in headless Chrome with a recording
Plotly stub: the dashboard chromatogram, the Chromatogram tab overlay and the
annotation modal's description. Browser tests skip without selenium/Chrome.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

from bootapp import browser_sign_in, booted, get  # noqa: E402
from hub_boot import build_hub  # noqa: E402

PLOTLY_STUB = r"""
(function () {
  function el(d) { return typeof d === 'string' ? document.getElementById(d) : d; }
  function plot(d, data, layout) {
    d = el(d); if (!d) return Promise.resolve();
    d.data = data || [];
    d.layout = JSON.parse(JSON.stringify(layout || {}));
    if (!d.on) {
      d.__handlers = {};
      d.on = function (ev, fn) { (d.__handlers[ev] = d.__handlers[ev] || []).push(fn); };
    }
    return Promise.resolve(d);
  }
  window.Plotly = {
    __recording: true,
    newPlot: plot, react: plot,
    relayout: function (d, upd) {
      d = el(d); d.layout = d.layout || {};
      for (const k of Object.keys(upd || {})) d.layout[k] = JSON.parse(JSON.stringify(upd[k]));
      return Promise.resolve(d);
    },
    restyle: function () { return Promise.resolve(); },
    purge: function () {}, addTraces: function () {}, deleteTraces: function () {},
    Plots: { resize: function () {} },
  };
  Object.defineProperty(window, 'Plotly', { value: window.Plotly, writable: false });
})();
"""


def _gc2_revisions(hub):
    """Give the held gc2 sample two revisions with their own anchors: rev 1
    gc1's times with carbons + 20, rev 2 (current) + 30. Returns (gc1, r1, r2)
    as ``(times, carbons)``."""
    import store
    final = store.get_revision(hub.ids["final"], db=hub.db)
    cal = json.loads(final["calibration_used"])
    gc1 = ([float(a[0]) for a in cal["anchors"]], [int(a[1]) for a in cal["anchors"]])
    assert len(gc1[0]) >= 3
    sid = hub.ids["held"]
    out = []
    for bump in (20, 30):
        anchors = [[t, c + bump] for t, c in zip(*gc1)]
        with store.connection(hub.db) as conn:
            with store.write_txn(conn):
                store.add_revision(conn, sid, json.loads(final["results"])
                                   if isinstance(final["results"], str) else final["results"],
                                   reason="processed", by="test",
                                   calibration_used={"cdf": "gc2-cal.CDF", "anchors": anchors})
        out.append((gc1[0], [c + bump for c in gc1[1]]))
    store.samples.update(sid, status="final", error=None, db=hub.db)
    return gc1, out[0], out[1]


@pytest.fixture(scope="module")
def hub_app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ladder")
    hub = build_hub(tmp)
    ladders = _gc2_revisions(hub)
    with booted(tmp) as (port, _proc, _data, _home):
        yield port, hub, ladders


# ── the trace route serves the revision's ladder ────────────────────────────

def test_trace_carries_each_samples_own_ladder(hub_app):
    port, hub, (gc1, r1, r2) = hub_app
    code, body = get(port, f"/api/samples/{hub.ids['final']}/trace")
    assert code == 200, body
    assert (body["cal_times"], body["cal_carbons"]) == (gc1[0], gc1[1])

    code, body = get(port, f"/api/samples/{hub.ids['held']}/trace")
    assert code == 200, body
    assert (body["cal_times"], body["cal_carbons"]) == (r2[0], r2[1])
    assert body["cal_carbons"] != gc1[1]

    code, body = get(port, f"/api/samples/{hub.ids['held']}/trace?revision=1")
    assert code == 200, body
    assert body["cal_carbons"] == r1[1]


def test_calibration_route_is_gc1s_and_differs(hub_app):
    """The premise: /api/calibration would have labelled gc2 with gc1's ladder."""
    port, _hub, (gc1, _r1, r2) = hub_app
    code, body = get(port, "/api/calibration")
    assert code == 200
    assert body.get("carbon_numbers") != r2[1]


def test_app_js_has_no_gc1_only_carbon_fallback():
    src = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "i + 5" not in src
    assert "state.calibration" not in src
    assert "'/api/calibration'" not in src


# ── headless Chrome ─────────────────────────────────────────────────────────

def _driver():
    webdriver = pytest.importorskip("selenium.webdriver")
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1600,1000",
                "--disable-dev-shm-usage"):
        opts.add_argument(arg)
    try:
        drv = webdriver.Chrome(options=opts)
    except Exception as exc:  # noqa: BLE001 - no Chrome / driver here
        pytest.skip(f"headless Chrome unavailable: {exc}")
    drv.execute_cdp_cmd("Network.enable", {})
    drv.execute_cdp_cmd("Network.setBlockedURLs", {"urls": ["*cdn.plot.ly*"]})
    drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": PLOTLY_STUB})
    return drv


def _wait(pred, timeout=20.0):
    deadline = time.time() + timeout
    v = None
    while time.time() < deadline:
        try:
            v = pred()
        except Exception:  # noqa: BLE001 - DOM not ready yet
            v = None
        if v:
            return v
        time.sleep(0.2)
    return v


@pytest.fixture(scope="module")
def page(hub_app):
    port, hub, ladders = hub_app
    drv = _driver()
    browser_sign_in(drv, port)
    try:
        drv.get(f"http://127.0.0.1:{port}/")
        assert _wait(lambda: drv.execute_script("return state.files.length") >= 5)
        yield drv, hub, ladders
    finally:
        drv.quit()


def _labels(drv, div_id):
    return drv.execute_script(
        "const d = document.getElementById(arguments[0]);"
        "return ((d && d.layout && d.layout.annotations) || [])"
        ".filter(a => /^C\\d+$/.test(a.text)).map(a => a.text);", div_id)


def _file(drv, sid):
    return drv.execute_script("return state.files.find(f => f.sample_id === arguments[0])", sid)


def test_dashboard_labels_each_sample_with_its_own_ladder(page):
    drv, hub, (gc1, _r1, r2) = page
    for key, ladder in (("held", r2), ("final", gc1), ("held", r2)):
        drv.execute_script("window.__done = false; loadDashboardData(state.files.find("
                           "f => f.sample_id === arguments[0])).then(() => window.__done = true);",
                           hub.ids[key])
        assert _wait(lambda: drv.execute_script("return window.__done"))
        want = [f"C{c}" for c in ladder[1]]
        assert _wait(lambda: _labels(drv, "dash-chrom-plot") == want), \
            (key, _labels(drv, "dash-chrom-plot"), want)


def test_chromatogram_overlay_uses_the_first_traces_ladder_and_says_so(page):
    drv, hub, (gc1, _r1, r2) = page
    drv.execute_script("state.traces.length = 0;")
    for key in ("held", "final"):
        drv.execute_script("window.__done = false; addChromatogramTrace(state.files.find("
                           "f => f.sample_id === arguments[0])).then(() => window.__done = true);",
                           hub.ids[key])
        assert _wait(lambda: drv.execute_script("return window.__done"))
    assert _labels(drv, "chrom-plot") == [f"C{c}" for c in r2[1]]
    title = drv.execute_script("return document.getElementById('chrom-plot').layout.title.text")
    assert "50001" in title and "carbon" in title.lower(), title

    # only gc1 visible: gc1's ladder, and no note
    drv.execute_script("state.traces[0].visible = false; renderChromatogramChart();")
    assert _labels(drv, "chrom-plot") == [f"C{c}" for c in gc1[1]]
    title = drv.execute_script("return document.getElementById('chrom-plot').layout.title.text")
    assert title == "Chromatogram Overlay"
    drv.execute_script("state.traces.length = 0; renderChromatogramChart();")


def test_annotation_modal_describes_the_span_with_the_samples_ladder(page):
    drv, hub, (gc1, _r1, r2) = page
    sid = hub.ids["held"]
    drv.execute_script("""
        const f = state.files.find(x => x.sample_id === arguments[0]);
        state.selectedFile = f; state.selectedSample = f;
        state.selectedStandard = state.comparisonStandards[0];
        updateAnalysisOverlay();
        window.__done = false; runAnalysis().then(() => window.__done = true);
    """, sid)
    assert _wait(lambda: drv.execute_script("return window.__done"), timeout=60)
    assert drv.execute_script("return state._renderedAnalysisSampleId") == sid
    assert drv.execute_script("return state.analysisResult.cal_carbons") == r2[1]
    t0, t1 = gc1[0][0], gc1[0][2]
    drv.execute_script(
        "setupAnnotationHandler(); if (!annotationMode) toggleAnnotationMode();"
        "const d = document.getElementById('analysis-trend-plot');"
        "(d.__handlers.plotly_selected || []).forEach(f => f({range: {x: [arguments[0], "
        "arguments[1]]}}));", t0, t1)
    desc = drv.execute_script("return document.getElementById('annotation-region-desc').textContent")
    want = f"C{r2[1][0]}–C{r2[1][2]}"
    assert want in desc, desc
    assert not re.search(rf"\bC{gc1[1][0]}\b", desc), desc
    drv.execute_script("document.getElementById('btn-annotation-cancel').click();")
