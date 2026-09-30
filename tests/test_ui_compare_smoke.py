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
    # both were sent: marked, and not offered again
    assert js(drv, "return GCReportQueue.items().every(i => !!i.sent_at)")
    assert js(drv, "return document.querySelector('[data-testid=rq-upload]').disabled") is True
    assert js(drv, "return document.querySelector('[data-testid=rq-upload]').hidden") is False
    js(drv, "GCReportQueue.clear(); document.querySelector('[data-testid=report-queue-sheet]').close()")


# ── annotate, comments, theme, unmount ───────────────────────────────────────

def test_annotate_comments_retheme_and_unmount(env):
    drv = open_page(env)
    sid = env["sid"]
    # a preset chip adds a comment
    assert wait(lambda: js(drv, "return document.querySelectorAll('#comment-presets .comment-chip').length") > 0)
    before = js(drv, "return document.querySelectorAll('#comment-list .comment-item').length")
    js(drv, "document.querySelector('#comment-presets .comment-chip').click()")
    assert wait(lambda: js(drv, "return document.querySelectorAll('#comment-list .comment-item').length") == before + 1)
    # the composer
    js(drv, "document.getElementById('comment-free-text').value = 'Free <b>text</b>';"
            "document.getElementById('btn-comment-add').click()")
    assert wait(lambda: "Free <b>text</b>" in js(drv, "return document.getElementById('comment-list').textContent"))
    assert js(drv, "return document.querySelectorAll('#comment-list b').length") == 0

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
    assert js(drv, "return document.querySelector('[data-testid=compare-clear-annotations]').textContent") == "Clear 1 annotation…"
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
