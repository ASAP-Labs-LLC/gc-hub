"""v5.0.0 lane C in a real browser: the Compare view, the Adjust drawer, the
report queue sheet and the Export report sheet, mounted on the test-only
harness (``tests/ui_compare_harness.py``: the shipped ``_layout.html`` and
scripts, served through a proxy to a booted hub with real samples; the hub
has no test route).

* light and dark, at 1366x768 and 1440x900 (and the right column at 1680):
  no sideways scroll, WCAG AA on every visible text, the charts about
  240/150 px at 1366x768, the drawer beside the page (``gc:adjust``), the
  top bar's "Report queue N" and its sheet;
* the standard: the best fit by default, a pick remembered per sample,
  ``setStandard`` and ``onUrlChange``;
* Findings are the server's lines; Adjust validates before anything is sent
  and recomputes on the hub; Save as default through the admin unlock;
* the Conclusion is read-only until Edit; Add to queue names the standard;
  Export report and Download all build real PDFs/ZIPs;
* the QBench upload's sheet over a scripted stream (progress, skip,
  re-entered password, stop), Annotate, comments, re-theme, unmount.

Skipped when selenium or a Chrome/chromedriver can't be started.
Screenshots go to ``GC_UI_SHOTS`` when it is set.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden", TESTS / "fixtures"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")
pytest.importorskip("jinja2")
pytest.importorskip("selenium.webdriver")

import ui_compare_harness as harness  # noqa: E402
from bootapp import TEST_ADMIN_PASSWORD, booted, browser_sign_in, setup_admin  # noqa: E402
from test_ui_setup_pages_smoke import CONTRAST_JS, SIZES, THEMES, _driver  # noqa: E402

SHOTS = os.environ.get("GC_UI_SHOTS")

SPY = r"""
window.__calls = [];
const __f = window.fetch;
window.fetch = async function (url, opts) {
  const o = opts || {};
  const rec = { url: String(url), method: o.method || 'GET', body: o.body || null };
  window.__calls.push(rec);
  const r = await __f.apply(this, arguments);
  if (rec.url.indexOf('/api/analysis') === 0) {
    try { rec.json = JSON.parse(await r.clone().text()); } catch (e) { rec.json = null; }
  }
  return r;
};
"""


def js(drv, script, *args):
    return drv.execute_script(script, *args)


def wait(pred, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = pred()
        except Exception:  # noqa: BLE001 - still rendering
            v = None
        if v:
            return v
        time.sleep(0.15)
    return pred()


def shot(drv, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        drv.save_screenshot(str(Path(SHOTS) / name))


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("lane-c")
    hub = harness.build(tmp)
    with booted(tmp) as (port, _proc, data, _home):
        setup_admin(port, data)
        hx = harness.Harness(port)
        drv = _driver()
        drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": SPY})
        try:
            browser_sign_in(drv, port)
            yield {"drv": drv, "hx": hx, "hub": hub, "port": port, "sid": hub.ids["diesel"]}
        finally:
            drv.quit()
            hx.stop()


def open_page(e, theme="light", size=(1440, 900), clear=True, query=""):
    drv, hx = e["drv"], e["hx"]
    drv.execute_cdp_cmd("Emulation.setDeviceMetricsOverride",
                        {"width": size[0], "height": size[1], "deviceScaleFactor": 1, "mobile": False})
    drv.get(hx.url("/__harness/blank"))
    js(drv, "localStorage.setItem('gc.theme', arguments[0]); localStorage.removeItem('gc.sidebar');", theme)
    if clear:
        js(drv, "localStorage.removeItem('gc.compare.picks'); sessionStorage.clear();")
    drv.get(hx.url(f"/__harness/compare?sample={e['sid']}{query}"))
    assert wait(lambda: js(drv, "return document.querySelectorAll('[data-testid=compare-finding]').length"), 60), \
        js(drv, "return window.__h && window.__h.error")
    assert wait(lambda: js(drv, "return !document.querySelector('.cmp').classList.contains('is-loading')"))
    wait(lambda: js(drv, "return !!(document.querySelector('[data-testid=compare-trend]')._fullLayout)"), 10)
    return drv


def analysis_calls(drv):
    return js(drv, "return window.__calls.filter(c => c.url.indexOf('/api/analysis') === 0)"
                   ".map(c => ({body: JSON.parse(c.body), json: c.json || null}))")


def settled(drv, n_before, timeout=30):
    """Wait for a new /api/analysis answer after ``n_before`` calls and for the view to settle."""
    assert wait(lambda: len(analysis_calls(drv)) > n_before and analysis_calls(drv)[-1]["json"], timeout)
    wait(lambda: js(drv, "return !document.querySelector('.cmp').classList.contains('is-loading')"))
    return analysis_calls(drv)[-1]


def frame(drv, where):
    assert js(drv, "return document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1;"), where
    # the toast (checked on its own: ink on --ink-fg) may be mid-fade here
    js(drv, "const t = document.getElementById('toast'); if (t) { t.textContent = ''; t.className = 'toast'; }")
    bad = js(drv, CONTRAST_JS)
    assert bad == [], (where, bad)


# ── the frame: both themes, both sizes, the drawer and the sheet ────────────

def test_compare_in_both_themes_at_both_sizes(env):
    for theme in THEMES:
        for size in SIZES:
            where = f"{theme} {size[0]}x{size[1]}"
            drv = open_page(env, theme, size)
            assert js(drv, "return document.documentElement.dataset.theme") == theme
            frame(drv, where)
            trend = js(drv, "return document.querySelector('.cmp-plot-trend').getBoundingClientRect().height")
            diff = js(drv, "return document.querySelector('.cmp-plot-diff').getBoundingClientRect().height")
            if size == (1366, 768):
                assert abs(trend - 240) <= 12 and abs(diff - 150) <= 12, (trend, diff)
            else:
                assert trend >= 240 and diff >= 150, (trend, diff)
            # below 1600 the findings come after the charts (no right column)
            assert js(drv, "return document.querySelector('.cmp-side').getBoundingClientRect().top >= "
                           "document.querySelector('.cmp-main').getBoundingClientRect().bottom - 1")
            # the chart template follows the theme's tokens
            ink = js(drv, "return getComputedStyle(document.documentElement).getPropertyValue('--chart-ink').trim()")
            line = js(drv, "const t = document.querySelector('[data-testid=compare-trend]').data;"
                           "return t[1].line.color;")
            assert line == ink, (line, ink)
            shot(drv, f"compare-{size[0]}x{size[1]}-{theme}.png")

            # Adjust: beside the page, announced with gc:adjust
            js(drv, "document.querySelector('[data-testid=compare-adjust-toggle]').click()")
            assert wait(lambda: js(drv, "return !document.querySelector('[data-testid=compare-drawer]').hidden"))
            assert js(drv, "return window.__h.adjust.slice(-1)[0]") == {"open": True}
            time.sleep(0.4)
            assert js(drv, "return document.querySelector('.cmp').getBoundingClientRect().right <= "
                           "document.querySelector('[data-testid=compare-drawer]').getBoundingClientRect().left + 1")
            frame(drv, where + " adjust")
            shot(drv, f"compare-adjust-{size[0]}x{size[1]}-{theme}.png")
            js(drv, "document.querySelector('[data-testid=adjust-close]').click()")
            assert js(drv, "return window.__h.adjust.slice(-1)[0]") == {"open": False}
            assert not js(drv, "return document.documentElement.classList.contains('cmp-adjust-open')")

            # the report queue: hidden while empty, "Report queue 1" after Add to queue
            assert js(drv, "return document.getElementById('report-queue-btn').hidden")
            js(drv, "document.getElementById('h-queue').click()")
            assert wait(lambda: not js(drv, "return document.getElementById('report-queue-btn').hidden"))
            assert js(drv, "return document.getElementById('report-queue-btn').getAttribute('aria-label')") \
                == "Report queue 1"
            assert js(drv, "return document.querySelector('#report-queue-btn .rq-count').textContent") == "1"
            js(drv, "document.getElementById('report-queue-btn').click()")
            assert wait(lambda: js(drv, "return document.querySelector('[data-testid=report-queue-sheet]').open"))
            time.sleep(0.3)
            frame(drv, where + " queue")
            shot(drv, f"report-queue-{size[0]}x{size[1]}-{theme}.png")
            js(drv, "document.querySelector('[data-testid=report-queue-sheet]').close()")


def test_the_right_column_is_sticky_from_1600(env):
    drv = open_page(env, "light", (1680, 1000))
    frame(drv, "1680")
    side = js(drv, "const s = document.querySelector('.cmp-side').getBoundingClientRect(),"
                   " m = document.querySelector('.cmp-main').getBoundingClientRect();"
                   "return [s.left >= m.right, getComputedStyle(document.querySelector('.cmp-side')).position];")
    assert side == [True, "sticky"], side
    shot(drv, "compare-1680x1000-light.png")


# ── the standard ─────────────────────────────────────────────────────────────

def test_the_standard_defaults_to_the_best_fit_and_a_pick_is_remembered(env):
    drv = open_page(env)
    best = js(drv, "return fetch('/api/best-fit', {method: 'POST', headers: {'Content-Type': 'application/json'},"
                   " body: JSON.stringify({sample_id: arguments[0]})}).then(r => GCSession.readJson(r))"
                   ".then(r => r.body.best_standard)", env["sid"])
    assert best
    urls = js(drv, "return window.__h.urls")
    assert urls[0] == {"standard": best, "source": "best"}, urls
    assert js(drv, "return document.querySelector('[data-testid=compare-standard]').value") == best
    assert analysis_calls(drv)[-1]["body"]["standard_name"] == best
    assert best in js(drv, "return document.querySelector('[data-testid=compare-bestfit]').textContent")

    other = next(s for s in (harness.STD_ULSD, harness.STD_RED) if s != best)
    n = len(analysis_calls(drv))
    js(drv, "const s = document.querySelector('[data-testid=compare-standard]'); s.value = arguments[0];"
            "s.dispatchEvent(new Event('change'));", other)
    call = settled(drv, n)
    assert call["body"]["standard_name"] == other
    assert js(drv, "return window.__h.urls.slice(-1)[0]") == {"standard": other, "source": "pick"}
    assert json.loads(js(drv, "return localStorage.getItem('gc.compare.picks')")) == [[str(env["sid"]), other]]

    # the next visit opens with the pick, before any best fit
    drv = open_page(env, clear=False)
    assert js(drv, "return document.querySelector('[data-testid=compare-standard]').value") == other
    assert js(drv, "return window.__h.urls[0]") == {"standard": other, "source": "remembered"}
    assert all(c["body"]["standard_name"] == other for c in analysis_calls(drv))

    # setStandard (lane S: ?standard= in the address bar)
    n = len(analysis_calls(drv))
    assert js(drv, "return window.__h.handle.setStandard(arguments[0])", best) is True
    assert settled(drv, n)["body"]["standard_name"] == best
    assert js(drv, "return window.__h.handle.setStandard('No such standard')") is False
    assert js(drv, "return window.__h.handle.standard()") == best

    # the harness's ?standard= wins over the remembered pick
    drv = open_page(env, clear=False, query="&standard=" + best.replace(" ", "%20").replace("#", "%23"))
    wait(lambda: js(drv, "return document.querySelector('[data-testid=compare-standard]').value") == best)
    assert js(drv, "return document.querySelector('[data-testid=compare-standard]').value") == best


# ── findings, adjust ─────────────────────────────────────────────────────────

def test_findings_are_the_servers_lines(env):
    drv = open_page(env)
    res = analysis_calls(drv)[-1]["json"]
    rows = js(drv, "return Array.from(document.querySelectorAll('[data-testid=compare-finding]')).map(li => {"
                   " const h = li.querySelector('.cmp-find-head'); const p = li.querySelector('.cmp-find-text');"
                   " return (h ? h.textContent + ': ' : '') + p.textContent; })")
    lines = [ln.lstrip("• ").strip() for ln in res["text"].split("\n")]
    assert rows == lines, (rows, lines)
    assert len(rows) == len(res["items"])
    # the conclusion is the server's, read-only
    assert js(drv, "return document.querySelector('[data-testid=compare-conclusion-text]').textContent") == res["conclusion"]
    assert js(drv, "return document.querySelector('[data-testid=compare-conclusion-input]').offsetParent") is None
    # the range bands come from the answer's windows
    bands = js(drv, "return document.querySelector('[data-testid=compare-trend]').layout.shapes"
                    ".filter(s => s.type === 'rect' && s.layer === 'below').map(s => [s.x0, s.x1])")
    assert bands == [[w["t0"], w["t1"]] for w in res["windows"] if w["evaluable"]]


def test_adjust_validates_then_recomputes_and_saves_the_default(env):
    drv = open_page(env)
    js(drv, "document.querySelector('[data-testid=compare-adjust-toggle]').click()")
    set_input = ("const i = document.querySelector(arguments[0]); i.value = arguments[1];"
                 "i.dispatchEvent(new Event('input', {bubbles: true}));")
    n = len(analysis_calls(drv))
    js(drv, set_input, "[data-testid=adjust-marginal]", "-5")
    time.sleep(1.2)
    assert len(analysis_calls(drv)) == n                       # nothing invalid is sent
    err = js(drv, "return document.querySelector('[data-testid=adjust-marginal]').closest('.cmp-num')"
                  ".querySelector('.cmp-err').textContent")
    assert err == "A number, 0 or more."
    assert js(drv, "return document.querySelector('[data-testid=adjust-marginal]').getAttribute('aria-invalid')") == "true"
    assert js(drv, "return document.querySelector('[data-testid=adjust-save-default]').disabled") is True
    js(drv, set_input, "[data-testid=adjust-marginal]", "150")
    call = settled(drv, n)
    assert call["body"]["thresh_marginal"] == 150
    assert call["json"]["params_used"]["thresh_marginal"] == 150
    # a slider: the real value the classic tab would send
    n = len(analysis_calls(drv))
    js(drv, set_input, "[data-testid=adjust-smoothing]", "0.5")
    assert settled(drv, n)["body"]["sigma"] == 117.0
    # ranges: add one, make it invalid, remove it
    n = len(analysis_calls(drv))
    js(drv, "document.querySelector('[data-testid=adjust-add-range]').click()")
    call = settled(drv, n)
    assert [r["label"] for r in call["body"]["ranges"]][-1] == "Range 3"
    n = len(analysis_calls(drv))
    js(drv, "const c = document.querySelectorAll('[data-testid=adjust-range]')[2].querySelectorAll('input')[2];"
            "c.value = '2'; c.dispatchEvent(new Event('input', {bubbles: true}));")
    time.sleep(1.0)
    assert len(analysis_calls(drv)) == n
    assert "The end must be at or after the start." in js(
        drv, "return document.querySelectorAll('[data-testid=adjust-range]')[2].textContent")
    js(drv, "document.querySelectorAll('[data-testid=adjust-remove-range]')[2].click()")
    call = settled(drv, n)
    assert len(call["body"]["ranges"]) == 2

    # Save as default: the shell's admin dialog, once
    js(drv, "document.querySelector('[data-testid=adjust-save-default]').click()")
    assert wait(lambda: js(drv, "return document.getElementById('admin-dialog').open"))
    js(drv, "document.getElementById('admin-password').value = arguments[0];"
            "document.getElementById('admin-form').requestSubmit();", TEST_ADMIN_PASSWORD)
    assert wait(lambda: "Saved as the default" in js(drv, "return document.querySelector('[data-testid=compare-drawer]').textContent"))
    saved = js(drv, "return fetch('/api/settings').then(r => GCSession.readJson(r)).then(r => r.body)")
    assert float(saved["analysis_thresh_marginal"]) == 150
    assert float(saved["analysis_sigma"]) == 117.0
    assert [r["label"] for r in json.loads(saved["analysis_range_overlays"])] == ["Gas", "Oil"]
    # put the defaults back for the other tests
    js(drv, "return fetch('/api/save-analysis-defaults', {method: 'POST', headers: {'Content-Type': 'application/json'},"
            " body: JSON.stringify({password: arguments[0], params: {thresh_marginal: 100, sigma: 34}})})",
       TEST_ADMIN_PASSWORD)
    js(drv, "document.querySelector('[data-testid=adjust-close]').click()")


# ── conclusion, queue, export ────────────────────────────────────────────────

def test_conclusion_edit_add_to_queue_export_and_download(env):
    drv = open_page(env)
    std = js(drv, "return window.__h.handle.standard()")
    js(drv, "document.querySelector('[data-testid=compare-conclusion-edit]').click()")
    assert js(drv, "return document.querySelector('[data-testid=compare-conclusion-input]').offsetParent") is not None
    js(drv, "document.querySelector('[data-testid=compare-conclusion-input]').value = 'Operator wording.';"
            "document.querySelector('[data-testid=compare-conclusion-save]').click();")
    assert js(drv, "return document.querySelector('[data-testid=compare-conclusion-text]').textContent") == "Operator wording."

    js(drv, "document.getElementById('h-queue').click()")
    toast = wait(lambda: js(drv, "return document.getElementById('toast').textContent"))
    assert toast == f"Added 40329 to the report queue, compared with {std}", toast
    items = json.loads(js(drv, "return sessionStorage.getItem('gc.reportQueue')"))
    assert len(items) == 1 and items[0]["conclusion"] == "Operator wording." and items[0]["standard_name"] == std
    assert items[0]["params"]["thresh_marginal"] == 100 and "bullets" not in items[0]
    # the same sample again replaces its entry
    js(drv, "document.getElementById('h-queue').click()")
    assert wait(lambda: js(drv, "return document.getElementById('toast').textContent").startswith("Updated 40329"))

    # Export report: the current parameters, the edited conclusion, a real PDF
    js(drv, "document.getElementById('h-export').click()")
    assert wait(lambda: js(drv, "return document.querySelector('[data-testid=export-sheet]').open"))
    frame(drv, "export sheet")
    shot(drv, "export-sheet-1440x900-light.png")
    assert "Thresholds 100 / 500 / 2000" in js(drv, "return document.querySelector('[data-testid=export-params]').textContent")
    assert js(drv, "return document.querySelector('[data-testid=export-conclusion]').value") == "Operator wording."
    n = js(drv, "return window.__calls.length")
    js(drv, "document.querySelector('[data-testid=export-download]').click()")
    assert wait(lambda: js(drv, "return document.getElementById('toast').textContent").endswith("downloaded"), 90)
    body = json.loads(js(drv, "return window.__calls.slice(arguments[0]).find(c => c.url === '/api/export-analysis-report').body", n))
    assert body["conclusion"] == "Operator wording." and body["standard_name"] == std and "bullets" not in body
    assert body["thresh_marginal"] == 100 and body["ranges"][0]["label"] == "Gas"

    # a second sample, without a mounted view (bulk-style), then Download all: a ZIP job
    js(drv, "GCCompare.addToQueue({sample: {sample_id: arguments[0], lab_id: '40304'},"
            " standards: window.__h.standards, settings: window.__h.settings})", env["hub"].ids["final"])
    js(drv, "document.getElementById('report-queue-btn').click()")
    assert wait(lambda: js(drv, "return document.querySelectorAll('[data-testid=rq-item]').length") == 2)
    js(drv, "document.querySelector('[data-testid=rq-download]').click()")
    msg = wait(lambda: (lambda m: m if m.startswith("Downloaded") or m.startswith("Download failed") else None)(
        js(drv, "return document.querySelector('[data-testid=rq-msg]').textContent")), 120)
    assert msg == "Downloaded 2 reports as ZIP", msg
    # Remove one, then Clear (confirmed)
    js(drv, "document.querySelector('[data-testid=rq-remove]').click()")
    assert wait(lambda: js(drv, "return document.querySelectorAll('[data-testid=rq-item]').length") == 1)
    js(drv, "window.confirm = () => true; document.querySelector('[data-testid=rq-clear]').click()")
    assert wait(lambda: js(drv, "return GCReportQueue.count()") == 0)
    assert js(drv, "return !document.querySelector('[data-testid=rq-empty]').hidden")
    js(drv, "document.querySelector('[data-testid=report-queue-sheet]').close()")
    assert wait(lambda: js(drv, "return document.getElementById('report-queue-btn').hidden"))


def pdf_text(drv, body):
    """The text of the PDF the hub builds for this export body (fetched in the
    page, so with its session), whitespace collapsed."""
    import base64
    import io

    pypdf = pytest.importorskip("pypdf")
    b64 = drv.execute_async_script("""
        const done = arguments[arguments.length - 1];
        fetch('/api/export-analysis-report', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                               body: JSON.stringify(arguments[0])})
          .then(r => r.ok ? r.arrayBuffer() : Promise.reject(new Error('HTTP ' + r.status)))
          .then(buf => { let s = ''; const b = new Uint8Array(buf);
                         for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
                         done(btoa(s)); })
          .catch(e => done('ERR ' + e.message));""", body)
    assert not b64.startswith("ERR"), b64
    reader = pypdf.PdfReader(io.BytesIO(base64.b64decode(b64)))
    return " ".join(" ".join((p.extract_text() or "") for p in reader.pages).split())


def set_range(drv, i, label=None, c_start=None, c_end=None):
    """Type into the i-th range card of the open Adjust drawer."""
    for k, value in ((0, label), (1, c_start), (2, c_end)):
        if value is None:
            continue
        js(drv, "const c = document.querySelectorAll('[data-testid=adjust-range]')[arguments[0]]"
                ".querySelectorAll('input')[arguments[1]]; c.value = arguments[2];"
                "c.dispatchEvent(new Event('input', {bubbles: true}));", i, k, str(value))


def test_ranges_added_in_adjust_print_on_the_report(env):
    """v7: every range on screen, in order and with its label, is on the
    report: the export sheet names them, the request carries them, the PDF
    lists them (one beyond the run as "not checked, outside this run"), and the report's
    bands are the windows the view drew."""
    drv = open_page(env)
    drv.set_script_timeout(120)
    js(drv, "document.querySelector('[data-testid=compare-adjust-toggle]').click()")
    n = len(analysis_calls(drv))
    js(drv, "document.querySelector('[data-testid=adjust-add-range]').click()")
    set_range(drv, 2, "Jet fuel cut", 12, 18)
    js(drv, "document.querySelector('[data-testid=adjust-add-range]').click()")
    set_range(drv, 3, "Heavy tail beyond the run", 60, 80)
    call = settled(drv, n)
    wait(lambda: analysis_calls(drv)[-1]["body"]["ranges"][-1]["c_end"] == 80)
    call = analysis_calls(drv)[-1]
    labels = ["Gas", "Oil", "Jet fuel cut", "Heavy tail beyond the run"]
    assert [r["label"] for r in call["body"]["ranges"]] == labels
    js(drv, "document.querySelector('[data-testid=adjust-close]').click()")
    drawn = js(drv, "return document.querySelector('[data-testid=compare-trend]').layout.shapes"
                    ".filter(s => s.layer === 'below').map(s => [s.x0, s.x1])")
    windows = call["json"]["windows"]
    assert drawn == [[w["t0"], w["t1"]] for w in windows if w["evaluable"]]
    assert [w["evaluable"] for w in windows][-1] is False

    # Export report: the sheet names every range, the request carries them
    js(drv, "document.getElementById('h-export').click()")
    assert wait(lambda: js(drv, "return document.querySelector('[data-testid=export-sheet]').open"))
    line = js(drv, "return document.querySelector('[data-testid=export-params]').textContent")
    assert "Jet fuel cut C12–C18" in line and "Heavy tail beyond the run C60–C80" in line, line
    n = js(drv, "return window.__calls.length")
    js(drv, "document.querySelector('[data-testid=export-download]').click()")
    assert wait(lambda: js(drv, "return document.getElementById('toast').textContent").endswith("downloaded"), 90)
    body = json.loads(js(drv, "return window.__calls.slice(arguments[0]).find(c => c.url === '/api/export-analysis-report').body", n))
    assert [r["label"] for r in body["ranges"]] == labels
    assert [(r["c_start"], r["c_end"]) for r in body["ranges"]][2:] == [(12, 18), (60, 80)]
    # the report computes exactly the windows the view drew
    again = js(drv, "return fetch('/api/analysis', {method: 'POST', headers: {'Content-Type': 'application/json'},"
                    " body: JSON.stringify(arguments[0])}).then(r => GCSession.readJson(r)).then(r => r.body.windows)", body)
    assert again == windows
    text = pdf_text(drv, body)
    assert "Jet fuel cut C12–C18" in text and "Heavy tail beyond the run C60–C80" in text, text[-900:]
    assert "Heavy tail beyond the run (C60–C80): not checked, outside this run" in text, text
    assert text.index("Gas C5") < text.index("Oil C") < text.index("Jet fuel cut C12") \
        < text.index("Heavy tail beyond the run C60"), text[-900:]


def test_adjusted_ranges_survive_the_view_and_reach_every_queue_path(env):
    """v7: the ranges on screen reach the report even when the Compare view
    is no longer mounted (the Samples page unmounts it on Overview/Data or
    another sample), through a bulk add, and a queue item says which ranges
    it prints and offers an update when the open view has changed since."""
    drv = open_page(env)
    sid = env["sid"]
    js(drv, "document.querySelector('[data-testid=compare-adjust-toggle]').click()")
    n = len(analysis_calls(drv))
    js(drv, "document.querySelector('[data-testid=adjust-add-range]').click()")
    set_range(drv, 2, "Jet fuel cut", 12, 18)
    settled(drv, n)
    wait(lambda: analysis_calls(drv)[-1]["body"]["ranges"][-1]["label"] == "Jet fuel cut")
    js(drv, "document.querySelector('[data-testid=adjust-close]').click()")
    labels = ["Gas", "Oil", "Jet fuel cut"]

    # Add to queue: the sheet names the ranges the item will print
    js(drv, "document.getElementById('h-queue').click()")
    wait(lambda: js(drv, "return GCReportQueue.count()") == 1)
    js(drv, "GCReportQueue.openSheet()")
    line = js(drv, "return document.querySelector('[data-testid=rq-ranges]').textContent")
    assert "Gas C5–C11" in line and "Jet fuel cut C12–C18" in line, line
    assert js(drv, "return document.querySelector('[data-testid=rq-update]')") is None
    js(drv, "document.querySelector('[data-testid=report-queue-sheet]').close()")

    # a range removed after the item was added: the item keeps what it was
    # added with, and the sheet offers to update it from the open view
    js(drv, "document.querySelector('[data-testid=compare-adjust-toggle]').click()")
    n = len(analysis_calls(drv))
    js(drv, "document.querySelectorAll('[data-testid=adjust-remove-range]')[0].click()")
    settled(drv, n)
    js(drv, "document.querySelector('[data-testid=adjust-close]').click()")
    item = json.loads(js(drv, "return sessionStorage.getItem('gc.reportQueue')"))[0]
    assert [r["label"] for r in item["ranges"]] == labels
    js(drv, "GCReportQueue.openSheet()")
    assert wait(lambda: js(drv, "return document.querySelector('[data-testid=rq-update]')"))
    js(drv, "document.querySelector('[data-testid=rq-update]').click()")
    wait(lambda: [r["label"] for r in json.loads(js(drv, "return sessionStorage.getItem('gc.reportQueue')"))[0]["ranges"]]
         == ["Oil", "Jet fuel cut"])
    assert js(drv, "return document.querySelector('[data-testid=rq-update]')") is None
    line = js(drv, "return document.querySelector('[data-testid=rq-ranges]').textContent")
    assert "Gas" not in line and "Jet fuel cut C12–C18" in line, line
    js(drv, "document.querySelector('[data-testid=report-queue-sheet]').close()")

    # the view goes (Overview/Data on the Samples page): Export report and Add
    # to queue still use this sample's ranges, not the saved defaults
    js(drv, "window.__h.handle.unmount()")
    js(drv, "GCCompare.openExportSheet({sample: window.__h.sample, standards: window.__h.standards,"
            " settings: window.__h.settings})")
    assert wait(lambda: js(drv, "return document.querySelector('[data-testid=export-sheet]').open"))
    line = js(drv, "return document.querySelector('[data-testid=export-params]').textContent")
    assert "Oil C20–C44, Jet fuel cut C12–C18" in line and "Gas" not in line, line
    js(drv, "document.querySelector('[data-testid=export-sheet]').close()")
    # a bulk add (the Samples list) captures the same, replacing the item
    js(drv, "GCReportQueue.addMany([GCCompare.reportItem({sample: window.__h.sample,"
            " standards: window.__h.standards, settings: window.__h.settings})], {quiet: true})")
    item = json.loads(js(drv, "return sessionStorage.getItem('gc.reportQueue')"))[0]
    assert [r["label"] for r in item["ranges"]] == ["Oil", "Jet fuel cut"], item
    # another sample with no adjustments: the saved defaults, captured now
    other = js(drv, "return GCCompare.reportItem({sample: {sample_id: arguments[0], lab_id: '40304'},"
                    " standards: window.__h.standards, settings: window.__h.settings})", env["hub"].ids["final"])
    assert [r["label"] for r in other["ranges"]] == ["Gas", "Oil"]

    # mounted again: the view shows the same adjustments
    n = len(analysis_calls(drv))
    js(drv, "window.__h.mount(arguments[0])", sid)
    call = settled(drv, n, 60)
    assert [r["label"] for r in call["body"]["ranges"]] == ["Oil", "Jet fuel cut"]
    # Reset returns to the saved defaults and forgets them
    js(drv, "document.querySelector('[data-testid=compare-adjust-toggle]').click()")
    n = len(analysis_calls(drv))
    js(drv, "document.querySelector('[data-testid=adjust-reset]').click()")
    call = settled(drv, n)
    assert [r["label"] for r in call["body"]["ranges"]] == ["Gas", "Oil"]
    js(drv, "document.querySelector('[data-testid=adjust-close]').click()")
    js(drv, "window.__h.handle.unmount()")
    again = js(drv, "return GCCompare.reportItem({sample: window.__h.sample, standards: window.__h.standards,"
                    " settings: window.__h.settings})")
    assert [r["label"] for r in again["ranges"]] == ["Gas", "Oil"]
    js(drv, "GCReportQueue.clear()")


FAKE_UPLOAD = r"""
window.__posted = [];
window.EventSource = function (url) { this.url = url; window.__es = this; this.close = () => { this.closed = true; }; };
const __f2 = window.fetch;
const ok = (b) => Promise.resolve(new Response(JSON.stringify(b), {status: 200, headers: {'Content-Type': 'application/json'}}));
window.fetch = function (url, opts) {
  const u = String(url);
  if (['/api/qbench-upload', '/api/qbench-update-credentials', '/api/qbench-skip-item', '/api/qbench-cancel'].includes(u)) {
    window.__posted.push([u, JSON.parse((opts && opts.body) || '{}')]);
    return ok(u === '/api/qbench-upload' ? {status: 'started'} : {status: 'ok'});
  }
  if (u === '/api/qbench-credentials') return ok({username: 'lab.user', has_password: true});
  return __f2.apply(this, arguments);
};
window.__emit = (d) => window.__es.onmessage({data: JSON.stringify(d)});
"""


def test_upload_to_qbench_over_the_stream(env):
    drv = open_page(env)
    js(drv, FAKE_UPLOAD)
    js(drv, "document.getElementById('h-queue').click()")
    js(drv, "GCCompare.addToQueue({sample: {sample_id: arguments[0], lab_id: '40304'},"
            " standards: window.__h.standards, settings: window.__h.settings})", env["hub"].ids["final"])
    js(drv, "document.getElementById('report-queue-btn').click()")
    js(drv, "document.querySelector('[data-testid=rq-upload]').click()")
    assert wait(lambda: js(drv, "return !document.querySelector('[data-testid=rq-signin]').hidden"))
    assert wait(lambda: js(drv, "return document.getElementById('rq-qb-user').value") == "lab.user")
    assert js(drv, "return document.querySelector('[data-testid=rq-upload]').textContent") == "Start upload (2)"
    js(drv, "document.getElementById('rq-qb-pass').value = 'pw'; document.querySelector('[data-testid=rq-upload]').click()")
    assert wait(lambda: js(drv, "return window.__es && window.__es.url") == "/api/qbench-upload/stream")
    sent = js(drv, "return window.__posted[0]")
    assert sent[0] == "/api/qbench-upload" and sent[1]["username"] == "lab.user" and sent[1]["password"] == "pw"
    assert [q["sample_id"] for q in sent[1]["queue"]] == [env["sid"], env["hub"].ids["final"]]
    assert all("bullets" not in q and "params" not in q for q in sent[1]["queue"])
    assert js(drv, "return document.getElementById('rq-qb-pass').value") == ""

    js(drv, "__emit({t: 'item', idx: 0, total: 2, lab_id: '40329', status: 'uploading', msg: 'Uploading', step: 1, steps: 3})")
    assert wait(lambda: js(drv, "return document.querySelectorAll('[data-testid=rq-up-item]').length") == 2)
    assert "Uploading 0/2" in js(drv, "return document.getElementById('report-queue-btn').textContent")
    # QBench refuses the sign-in mid-run: the password again, then it carries on
    js(drv, "__emit({t: 'overall', status: 'credentials_needed', msg: 'Login failed'})")
    assert wait(lambda: js(drv, "return !document.querySelector('[data-testid=rq-reauth]').hidden"))
    frame(drv, "reauth")
    js(drv, "document.getElementById('rq-reauth-pass').value = 'pw2'; document.querySelector('[data-testid=rq-reauth-submit]').click()")
    assert wait(lambda: js(drv, "return window.__posted.some(p => p[0] === '/api/qbench-update-credentials')"))
    assert js(drv, "return window.__posted.find(p => p[0] === '/api/qbench-update-credentials')[1]") == \
        {"username": "lab.user", "password": "pw2"}
    js(drv, "__emit({t: 'overall', status: 'credentials_updated', msg: ''})")
    assert wait(lambda: js(drv, "return document.querySelector('[data-testid=rq-reauth]').hidden"))
    # skip the second, finish the first
    js(drv, "document.querySelectorAll('[data-testid=rq-skip]')[1].click()")
    assert wait(lambda: js(drv, "return window.__posted.some(p => p[0] === '/api/qbench-skip-item')"))
    assert js(drv, "return window.__posted.find(p => p[0] === '/api/qbench-skip-item')[1]") == {"idx": 1}
    js(drv, "__emit({t: 'item', idx: 1, total: 2, lab_id: '40304', status: 'skipped', msg: 'Skipped by user'});"
            "__emit({t: 'item', idx: 0, total: 2, lab_id: '40329', status: 'ok', msg: 'Uploaded'});")
    assert wait(lambda: js(drv, "return document.querySelector('.rq-progress-block .progress span').style.width") == "100%")
    frame(drv, "upload progress")
    shot(drv, "report-queue-upload-1440x900-light.png")
    # Stop asks first
    js(drv, "window.confirm = () => true; document.querySelector('[data-testid=rq-stop]').click()")
    assert wait(lambda: js(drv, "return window.__posted.some(p => p[0] === '/api/qbench-cancel')"))
    js(drv, "__emit({t: 'overall', status: 'partial', ok: 1, fail: 0, total: 2, msg: ''})")
    assert wait(lambda: js(drv, "return document.getElementById('rq-overall').textContent") == "1 uploaded, 0 failed.")
    assert js(drv, "return window.__es.closed") is True
    assert js(drv, "return document.querySelector('[data-testid=rq-stop]').hidden") is True
    # the uploaded one is sent and not offered again; v6.0.0: the skipped one
    # was not uploaded, so it is back in the queue to send (never marked sent)
    sent = js(drv, "return GCReportQueue.items().map(i => [i.lab_id, !!i.sent_at])")
    assert sorted(sent) == [["40304", False], ["40329", True]], sent
    assert js(drv, "return document.querySelector('[data-testid=rq-upload]').disabled") is False
    assert js(drv, "return document.querySelector('[data-testid=rq-upload]').textContent") == "Upload 1 to QBench"
    assert js(drv, "return document.querySelector('[data-testid=rq-upload]').hidden") is False
    js(drv, "GCReportQueue.clear(); document.querySelector('[data-testid=report-queue-sheet]').close()")


# ── v6: conclusion presets and earlier notes ────────────────────────────────

def test_conclusion_presets_insert_into_the_conclusion_and_earlier_notes_show_under_it(env):
    drv = open_page(env)
    sid = env["sid"]
    generated = js(drv, "return document.querySelector('[data-testid=compare-conclusion-text]').textContent")
    assert generated
    # Add preset (v6.0.1): opens the editor with the presets tray under it, never
    # a menu over the text; a chip appends its text and the tray stays
    btn = "document.querySelector('[data-testid=compare-preset-menu]')"
    tray = "document.querySelector('[data-testid=compare-presets]')"
    assert wait(lambda: js(drv, f"return !{btn}.hidden && !{btn}.disabled"))
    assert js(drv, f"return {tray}.hidden")
    js(drv, f"{btn}.click()")
    assert js(drv, f"return !{tray}.hidden")
    assert js(drv, f"return {btn}.hidden")                       # the editor is open: no header button
    assert js(drv, "return document.activeElement.dataset.testid") == "compare-preset"
    presets = js(drv, "return Array.from(document.querySelectorAll('[data-testid=compare-preset]'))"
                      ".map(b => b.textContent)")
    assert "Re-run requested." in presets, presets
    frame(drv, "preset tray")
    shot(drv, "v6-preset-tray.png")
    js(drv, "Array.from(document.querySelectorAll('[data-testid=compare-preset]'))"
            ".find(b => b.textContent === 'Re-run requested.').click()")
    assert js(drv, f"return !{tray}.hidden")
    area = "document.querySelector('[data-testid=compare-conclusion-input]')"
    assert js(drv, f"return {area}.offsetParent") is not None
    assert js(drv, f"return {area}.value") == generated + " Re-run requested."
    assert js(drv, f"return document.activeElement === {area} && {area}.selectionStart === {area}.value.length")
    # the chip now shows it is in the conclusion; a click on it adds nothing again
    rerun = ("Array.from(document.querySelectorAll('[data-testid=compare-preset]'))"
             ".find(b => b.textContent === 'Re-run requested.')")
    assert js(drv, f"return {rerun}.classList.contains('is-in')")
    js(drv, f"{rerun}.click()")
    assert js(drv, f"return {area}.value") == generated + " Re-run requested."
    # Undo takes the insert back, and the chip is a plain one again
    js(drv, "document.querySelector('[data-testid=compare-preset-undo]').click()")
    assert js(drv, f"return {area}.value") == generated
    assert not js(drv, f"return {rerun}.classList.contains('is-in')")
    js(drv, f"{rerun}.click()")
    assert js(drv, f"return {area}.value") == generated + " Re-run requested."
    # a second preset while editing goes in at the cursor
    js(drv, f"{area}.setSelectionRange(0, 0)")
    js(drv, f"{btn}.click()")
    js(drv, "Array.from(document.querySelectorAll('[data-testid=compare-preset]'))"
            ".find(b => b.textContent === 'Sample appears to be gasoline.').click()")
    assert js(drv, f"return {area}.value") == \
        "Sample appears to be gasoline. " + generated + " Re-run requested."
    js(drv, "document.querySelector('[data-testid=compare-conclusion-save]').click()")
    final = "Sample appears to be gasoline. " + generated + " Re-run requested."
    assert js(drv, "return document.querySelector('[data-testid=compare-conclusion-text]').textContent") == final
    # the edited conclusion is what the report gets
    assert js(drv, "return window.__h.handle.queueItem().conclusion") == final

    # an earlier note (a comment from before v6): listed read-only under the conclusion
    assert js(drv, "return document.querySelector('[data-testid=compare-notes]').hidden")
    code = js(drv, "return fetch('/api/samples/' + arguments[0] + '/comments', {method: 'POST',"
                   " headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: 'Old <b>note</b>'})})"
                   ".then(r => r.status)", sid)
    assert code == 201
    js(drv, "Comments.load(arguments[0])", sid)
    assert wait(lambda: js(drv, "return !document.querySelector('[data-testid=compare-notes]').hidden"))
    assert js(drv, "return Array.from(document.querySelectorAll('[data-testid=compare-note] .cmp-note-text'))"
                   ".map(p => p.textContent)") == ["Old <b>note</b>"]
    assert js(drv, "return document.querySelectorAll('[data-testid=compare-notes] b').length") == 0
    frame(drv, "notes")
    shot(drv, "v6-notes.png")
    # Add to conclusion: into the editor, then the analyst saves (or cancels)
    js(drv, "document.querySelector('[data-testid=compare-note-insert]').click()")
    assert js(drv, f"return {area}.value") == final + " Old <b>note</b>"
    js(drv, "Array.from(document.querySelectorAll('.cmp-concl-form button')).find(b => b.textContent === 'Cancel').click()")
    assert js(drv, "return document.querySelector('[data-testid=compare-conclusion-text]').textContent") == final
    # Remove: confirmed, soft-deleted, gone from the list
    js(drv, "window.confirm = (m) => { window.__asked = m; return true; };"
            "document.querySelector('[data-testid=compare-note-remove]').click()")
    assert wait(lambda: js(drv, "return document.querySelector('[data-testid=compare-notes]').hidden"))
    assert "no longer prints on reports" in js(drv, "return window.__asked")
    left = js(drv, "return fetch('/api/samples/' + arguments[0] + '/comments').then(r => GCSession.readJson(r))"
                   ".then(r => r.body.comments.map(c => c.text))", sid)
    assert "Old <b>note</b>" not in left


def test_conclusion_limit_counter_paste_and_refused_preset(env):
    """v6: 1,500 characters. A live "N / 1,500" counter (warning colour from
    1,350), maxlength stops typing, a paste is cut to fit with a one-line
    notice, and a preset that would pass the limit is refused, saying why."""
    drv = open_page(env)
    js(drv, "document.querySelector('[data-testid=compare-conclusion-edit]').click()")
    area = "document.querySelector('[data-testid=compare-conclusion-input]')"
    count = "document.querySelector('[data-testid=compare-conclusion-count]')"
    assert js(drv, f"return {area}.maxLength") == 1500
    n = js(drv, f"return {area}.value.length")
    assert js(drv, f"return {count}.textContent") == f"{n:,} / 1,500"
    # typing updates the counter; near the limit it takes the warning colour
    normal = js(drv, f"return getComputedStyle({count}).color")
    js(drv, f"{area}.value = 'x'.repeat(1400); {area}.dispatchEvent(new Event('input'))")
    assert js(drv, f"return {count}.textContent") == "1,400 / 1,500"
    assert js(drv, f"return {count}.classList.contains('is-near')")
    warn = js(drv, "return getComputedStyle(document.documentElement).getPropertyValue('--warn-text').trim()")
    assert warn and js(drv, f"return getComputedStyle({count}).color") != normal
    frame(drv, "counter near the limit")
    # a paste past the limit is cut to fit, with the notice
    js(drv, f"{area}.focus(); {area}.setSelectionRange(1400, 1400);"
            "const dt = new DataTransfer(); dt.setData('text/plain', 'y'.repeat(300));"
            f"{area}.dispatchEvent(new ClipboardEvent('paste', {{clipboardData: dt, bubbles: true, cancelable: true}}))")
    assert js(drv, f"return {area}.value.length") == 1500
    assert js(drv, f"return {area}.value.endsWith('y'.repeat(100))")
    assert js(drv, f"return {count}.textContent") == "1,500 / 1,500"
    cut = "document.querySelector('[data-testid=compare-conclusion-cut]')"
    assert js(drv, f"return !{cut}.hidden && {cut}.textContent") == \
        "The pasted text was cut to fit the 1,500-character limit."
    frame(drv, "paste cut")
    # a preset that does not fit is marked in the tray, and refused, the text unchanged
    chip = "document.querySelector('[data-testid=compare-preset]')"
    assert wait(lambda: js(drv, "return !document.querySelector('[data-testid=compare-presets]').hidden"))
    assert js(drv, f"return {chip}.classList.contains('is-full') && {chip}.getAttribute('aria-disabled')") == "true"
    js(drv, f"{chip}.click()")
    assert js(drv, f"return {area}.value.length") == 1500
    msg = wait(lambda: js(drv, "return document.getElementById('toast').textContent"))
    assert "longer than 1,500 characters" in msg, msg
    # the next keystroke clears the notice
    js(drv, f"{area}.value = 'Short.'; {area}.dispatchEvent(new Event('input'))")
    assert js(drv, f"return {cut}.hidden") and js(drv, f"return {count}.textContent") == "6 / 1,500"
    js(drv, "Array.from(document.querySelectorAll('.cmp-concl-form button')).find(b => b.textContent === 'Cancel').click()")


# ── annotate, comments, theme, unmount ───────────────────────────────────────

def test_annotate_comments_retheme_and_unmount(env):
    drv = open_page(env)
    sid = env["sid"]
    # v6: no separate Comments section and no comment composer
    assert js(drv, "return document.querySelector('[data-testid=compare-comments]')") is None
    assert js(drv, "return document.getElementById('comment-free-text')") is None

    # Annotate: drag-select on the trend -> a small form -> an annotation comment
    js(drv, "document.querySelector('[data-testid=compare-annotate]').click()")
    assert js(drv, "return document.querySelector('[data-testid=compare-trend]')._fullLayout.dragmode") == "select"
    js(drv, "document.querySelector('[data-testid=compare-trend]').emit('plotly_selected', {range: {x: [2.4, 3.1]}})")
    assert wait(lambda: js(drv, "return !document.querySelector('[data-testid=compare-annotation-pop]').hidden"))
    assert js(drv, "return document.querySelector('[data-testid=compare-trend]')._fullLayout.dragmode") == "zoom"
    region = js(drv, "return document.querySelector('.cmp-pop-region').textContent")
    assert region.startswith("Region C") and region.endswith("2.40–3.10 min"), region
    frame(drv, "annotate")
    js(drv, "document.querySelector('[data-testid=compare-annotation-text]').value = 'Hump <x>';"
            "document.querySelector('[data-testid=compare-annotation-save]').click()")
    assert wait(lambda: js(drv, "return document.querySelector('[data-testid=compare-annotation-pop]').hidden"))
    shapes = wait(lambda: js(drv, "return document.querySelector('[data-testid=compare-trend]').layout.shapes"
                                  ".filter(s => s.layer === 'above' && s.x0 === 2.4).length"))
    assert shapes == 1
    labels = js(drv, "return document.querySelector('[data-testid=compare-trend]').layout.annotations.map(a => a.text)")
    assert "Hump &lt;x&gt;" in labels, labels
    # Clear annotations: in the Annotate menu, with the count, confirmed
    js(drv, "document.querySelector('[data-testid=compare-annotate-menu]').click()")
    assert js(drv, "return document.querySelector('[data-testid=compare-clear-annotations]').textContent") == "Clear 1 marked region…"
    # the menu lists the marked region (escaped: textContent)
    region_items = js(drv, "return Array.from(document.querySelectorAll('[data-testid=compare-region-remove]'))"
                           ".map(b => b.textContent)")
    assert region_items == ["Hump <x> · 2.40–3.10 min×"], region_items
    js(drv, "window.confirm = () => true; document.querySelector('[data-testid=compare-clear-annotations]').click()")
    assert wait(lambda: js(drv, "return document.querySelector('[data-testid=compare-trend]').layout.shapes"
                                "  .filter(s => s.layer === 'above').length") == 0)
    comments = js(drv, "return fetch('/api/samples/' + arguments[0] + '/comments').then(r => GCSession.readJson(r))"
                       ".then(r => r.body.comments.map(c => c.source))", sid)
    assert "annotation" not in comments

    # re-theme without a reload (gc:theme, or the attribute when GCTheme is absent)
    light_ink = js(drv, "return document.querySelector('[data-testid=compare-trend]').data[1].line.color")
    js(drv, "document.documentElement.setAttribute('data-theme', 'dark');"
            "document.dispatchEvent(new CustomEvent('gc:theme', {detail: {mode: 'dark'}}));")
    assert wait(lambda: js(drv, "return document.querySelector('[data-testid=compare-trend]').data[1].line.color") != light_ink)
    time.sleep(0.5)                          # the buttons' background transition
    frame(drv, "rethemed")

    # unmount: the drawer and the view go, the page hears the drawer close
    js(drv, "document.querySelector('[data-testid=compare-adjust-toggle]').click()")
    js(drv, "window.__h.handle.unmount()")
    assert js(drv, "return document.querySelectorAll('[data-testid=compare-drawer]').length") == 0
    assert js(drv, "return document.getElementById('compare-root').children.length") == 0
    assert js(drv, "return window.__h.adjust.slice(-1)[0]") == {"open": False}
    assert not js(drv, "return document.documentElement.classList.contains('cmp-adjust-open')")
    errors = [e["message"] for e in drv.get_log("browser")
              if e["level"] == "SEVERE" and "favicon.ico" not in e["message"]]
    assert errors == [], errors
