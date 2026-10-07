"""v6.0.0 lane L3 in a real browser: the Samples page's chart and checkboxes.

Regressions for Ryan's v5 bugs, against a hub with real samples
(``tests/hub_boot.py``), signed in:

* **the chart always draws or says why** (bug 4): opening a second sample
  after a first one has been drawn used to wipe Plotly's DOM with the
  "Loading…" line and then ``Plotly.react`` the same div, which still held
  the old plot's state, so nothing was drawn; a live update for the open
  sample did the same. A result-only run (no CDF) says so in one line, with
  a Retry;
* **the checkbox shows the selection** (bug 5): a click on a row's checkbox
  toggled the selection on mousedown and then the browser's own click
  toggled the box back, so the box showed the opposite of the selection;
* **stacking** (bug 8): add selected samples and a comparison standard to
  the chart, Overlay vs Stacked, a legend naming each trace, remove one,
  clear all, kept across a reload.

Skipped when selenium or a Chrome/chromedriver can't be started.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")
pytest.importorskip("selenium.webdriver")

from bootapp import booted, browser_sign_in  # noqa: E402
from hub_boot import build_hub  # noqa: E402
from test_ui_setup_pages_smoke import _driver, _js  # noqa: E402
from ui_wait import wait_for  # noqa: E402

# Is the chart really on screen: Plotly's DOM inside #chrom, attached, with a
# size, and a drawn line (scattergl draws on a canvas; scatter as a path).
CHART_DRAWN_JS = """
const el = document.getElementById('chrom');
if (!el) return 'no #chrom';
const svg = el.querySelector('.plot-container svg.main-svg');
if (!svg || !svg.isConnected) return 'no plot (data=' + !!el.data + ')';
const r = svg.getBoundingClientRect();
if (r.width < 200 || r.height < 200) return 'plot ' + r.width + 'x' + r.height;
if (!el.data || !el.data.length) return 'no data';
if (!el.querySelector('.scatterlayer .trace path.js-line')) return 'no line drawn';
const msg = document.getElementById('chrom-msg');
if (msg && !msg.hidden) return 'message shown: ' + msg.textContent;
return 'ok';
"""


def _open(drv, base, path, size=(1440, 900)):
    drv.set_window_size(*size)
    drv.get(base + path)
    assert wait_for(drv, lambda: _js(drv, "return !!(window.GCSamples && GCSamples.ready)")), \
        _js(drv, "return document.body.innerText.slice(0, 400)")


def _row_click(drv, sid):
    _js(drv, "document.querySelector('.srow[data-sample-id=\"' + arguments[0] + '\"] .lab').click();", sid)


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("samples-v6-ui")
    hub = build_hub(tmp)
    with booted(tmp) as (port, _proc, _data, _home):
        drv = _driver()
        try:
            browser_sign_in(drv, port)
            yield drv, f"http://127.0.0.1:{port}", hub
        finally:
            drv.quit()


# ── bug 4: the chart ─────────────────────────────────────────────────────────

def test_the_chart_draws_for_every_sample_opened_in_turn(page):
    drv, base, hub = page
    _open(drv, base, f"/samples/{hub.ids['final']}")
    assert wait_for(drv, lambda: _js(drv, CHART_DRAWN_JS) == "ok"), _js(drv, CHART_DRAWN_JS)
    # a second (uncached) sample, then back to the first (cached), then a third
    for key in ("rerun", "final", "backfill", "released"):
        _row_click(drv, hub.ids[key])
        assert wait_for(drv, lambda: _js(drv, "return GCSamples.state.route.sampleId") == hub.ids[key])
        assert wait_for(drv, lambda: _js(drv, CHART_DRAWN_JS) == "ok"), (key, _js(drv, CHART_DRAWN_JS))
        name = _js(drv, "return document.getElementById('chrom').data[0].name")
        # the chart is this sample's (its display name: "40304 (2)" for the rerun)
        assert name.startswith(hub.sample(key)["lab_id"]), (key, name)
        assert name == _js(drv, "return GCSamples.state.row.display_name"), (key, name)


def test_a_live_update_for_the_open_sample_keeps_the_chart(page):
    drv, base, hub = page
    sid = hub.ids["final"]
    _open(drv, base, f"/samples/{sid}")
    assert wait_for(drv, lambda: _js(drv, CHART_DRAWN_JS) == "ok"), _js(drv, CHART_DRAWN_JS)
    # what GCLive delivers when the open sample changes (a re-process, a revision)
    _js(drv, "GCSamples.onLive({samples: [arguments[0]]});", sid)
    assert wait_for(drv, lambda: _js(drv, CHART_DRAWN_JS) == "ok"), _js(drv, CHART_DRAWN_JS)
    _js(drv, "GCSamples.onLive({samples: [arguments[0]]});", sid)
    _row_click(drv, hub.ids["rerun"])
    assert wait_for(drv, lambda: _js(drv, CHART_DRAWN_JS) == "ok"), _js(drv, CHART_DRAWN_JS)


def test_a_result_only_run_says_why_in_one_line_with_retry(page):
    drv, base, hub = page
    _open(drv, base, f"/samples/{hub.ids['final']}")
    assert wait_for(drv, lambda: _js(drv, CHART_DRAWN_JS) == "ok")
    _row_click(drv, hub.ids["resultonly"])
    msg = wait_for(drv, lambda: _js(drv, "const m = document.getElementById('chrom-msg');"
                                         "return m && !m.hidden && !/Loading/.test(m.textContent) ? m.textContent : null;"))
    assert msg and "no chromatogram" in msg.lower() and "v1" in msg, msg
    assert _js(drv, "return !!document.querySelector('#chrom-msg [data-testid=chart-retry]')")
    # no stale plot of the sample before is left under the message
    assert _js(drv, "return !document.querySelector('#chrom .plot-container')")
    # and the next sample still draws
    _row_click(drv, hub.ids["final"])
    assert wait_for(drv, lambda: _js(drv, CHART_DRAWN_JS) == "ok"), _js(drv, CHART_DRAWN_JS)
    # no stray "null" in the Results card (v5 printed one under the D86 switch)
    assert wait_for(drv, lambda: _js(drv, "return !!document.querySelector('[data-testid=results-table]')"))
    assert "null" not in _js(drv, "return document.getElementById('results-body').textContent")


def test_a_failed_trace_offers_retry_and_retry_draws(page):
    drv, base, hub = page
    sid = hub.ids["rerun"]
    _open(drv, base, "/samples")
    # the first trace request for this sample fails as a network error would
    _js(drv, """
        const real = window.fetch;
        let failed = false;
        window.fetch = function (url, opts) {
            if (!failed && String(url).includes('/api/samples/' + arguments_sid + '/trace')) {
                failed = true;
                return Promise.reject(new TypeError('Failed to fetch'));
            }
            return real.apply(this, arguments);
        };
    """.replace("arguments_sid", str(sid)))
    _row_click(drv, sid)
    assert wait_for(drv, lambda: _js(drv, "return !!document.querySelector('#chrom-msg:not([hidden]) [data-testid=chart-retry]')")), \
        _js(drv, "return document.getElementById('chrom-msg').innerText")
    assert "did not answer" in _js(drv, "return document.getElementById('chrom-msg').innerText")
    _js(drv, "document.querySelector('#chrom-msg [data-testid=chart-retry]').click();")
    assert wait_for(drv, lambda: _js(drv, CHART_DRAWN_JS) == "ok"), _js(drv, CHART_DRAWN_JS)


# ── bug 5: the checkbox ──────────────────────────────────────────────────────

BOXES_JS = """
return [...document.querySelectorAll('#rows .srow')].map(r => {
    const id = Number(r.dataset.sampleId);
    const box = r.querySelector('input[type=checkbox]');
    const sel = GCSamples.state.sel;
    return {id, box: box.checked, row: r.classList.contains('checked'), model: sel.all || sel.ids.has(id)};
});
"""


def _mismatches(drv):
    return [b for b in _js(drv, BOXES_JS) if not (b["box"] == b["row"] == b["model"])]


def _click_box(drv, sid):
    from selenium.webdriver import ActionChains
    row = drv.find_element("css selector", f'.srow[data-sample-id="{sid}"]')
    ActionChains(drv).move_to_element(row).perform()
    box = row.find_element("css selector", ".lead")
    ActionChains(drv).move_to_element(box).click().perform()


def test_clicking_a_checkbox_shows_exactly_the_selection(page):
    drv, base, hub = page
    _open(drv, base, "/samples")
    assert wait_for(drv, lambda: len(_js(drv, BOXES_JS)) == len(hub.ids))
    a, b = hub.ids["final"], hub.ids["rerun"]
    _click_box(drv, a)
    assert wait_for(drv, lambda: _js(drv, "return GCSamples.state.sel.ids.has(arguments[0])", a))
    assert _mismatches(drv) == []
    _click_box(drv, b)
    assert _mismatches(drv) == []
    assert sorted(_js(drv, "return [...GCSamples.state.sel.ids]")) == sorted([a, b])
    _click_box(drv, a)                    # off again
    assert _mismatches(drv) == []
    assert _js(drv, "return [...GCSamples.state.sel.ids]") == [b]
    # a live re-render keeps it
    _js(drv, "GCSamples.onLive({samples: [arguments[0]]});", a)
    assert wait_for(drv, lambda: _mismatches(drv) == []), _mismatches(drv)
    assert _js(drv, "return [...GCSamples.state.sel.ids]") == [b]
    _js(drv, "document.getElementById('bulk-clear').click();")
    assert _mismatches(drv) == []


# ── bug 8: stacking chromatograms ────────────────────────────────────────────

TRACES_JS = """
const el = document.getElementById('chrom');
return (el.data || []).map(d => ({name: d.name, dash: d.line.dash, color: d.line.color,
    lo: Math.min(...d.y.filter(v => v !== null)), hi: Math.max(...d.y.filter(v => v !== null))}));
"""
LEGEND_JS = """
return [...document.querySelectorAll('#chrom-legend [data-testid=trace-item]')].map(li =>
    li.querySelector('.tl-name').textContent);
"""


def test_overlay_selected_samples_and_a_standard_stacked_and_back(page):
    drv, base, hub = page
    final, rerun, backfill = hub.ids["final"], hub.ids["rerun"], hub.ids["backfill"]
    _open(drv, base, f"/samples/{final}")
    _js(drv, "try { sessionStorage.removeItem('gc.samples.overlay'); } catch (e) {}")
    _open(drv, base, f"/samples/{final}")
    assert wait_for(drv, lambda: _js(drv, CHART_DRAWN_JS) == "ok"), _js(drv, CHART_DRAWN_JS)
    assert _js(drv, "return document.getElementById('chrom-mode').hidden")      # one trace: no mode switch
    # tick two rows, "Overlay on chart" from the bulk bar
    _click_box(drv, rerun)
    _click_box(drv, backfill)
    _js(drv, "document.querySelector('[data-testid=bulk-overlay]').click();")
    assert wait_for(drv, lambda: len(_js(drv, TRACES_JS)) == 3), _js(drv, TRACES_JS)
    assert wait_for(drv, lambda: _js(drv, CHART_DRAWN_JS) == "ok"), _js(drv, CHART_DRAWN_JS)
    assert _js(drv, LEGEND_JS) == ["40304", "40304 (2)", "40299"]
    tr = _js(drv, TRACES_JS)
    assert len({(t["color"], t["dash"]) for t in tr}) == 3, tr               # told apart
    assert tr[0]["dash"] == "solid"
    assert not _js(drv, "return document.getElementById('chrom-mode').hidden")
    # a comparison standard from Add trace
    _js(drv, "document.getElementById('btn-add-trace').click();")
    assert wait_for(drv, lambda: _js(drv, "return !document.getElementById('add-trace-pop').hidden"
                                          " && !!document.querySelector('[data-testid=overlay-standard][data-name=Diesel]')"))
    assert "2 ticked" in _js(drv, "return document.querySelector('[data-testid=overlay-selected]').textContent")
    _js(drv, "document.querySelector('[data-testid=overlay-standard][data-name=Diesel]').click();")
    assert wait_for(drv, lambda: len(_js(drv, TRACES_JS)) == 4), _js(drv, TRACES_JS)
    assert _js(drv, "return document.getElementById('add-trace-pop').hidden")
    assert _js(drv, LEGEND_JS)[-1] == "Diesel"
    assert "standard" in _js(drv, "return document.querySelector('#chrom-legend li[data-key=\"std:Diesel\"]').textContent")
    # Stacked: each trace above the one before; Overlay: back on one axis
    _js(drv, "document.querySelector('#chrom-mode [data-mode=stacked]').click();")
    assert wait_for(drv, lambda: _js(drv, "return GCSamples.state.overlay.mode") == "stacked")
    st = _js(drv, TRACES_JS)
    assert all(st[i + 1]["lo"] > st[i]["lo"] for i in range(3)), st
    assert st[0]["lo"] == 0
    assert _js(drv, "return document.getElementById('chrom').layout.yaxis.showticklabels") is False
    assert _js(drv, CHART_DRAWN_JS) == "ok"
    _js(drv, "document.querySelector('#chrom-mode [data-mode=overlay]').click();")
    assert wait_for(drv, lambda: _js(drv, TRACES_JS)[1]["lo"] == tr[1]["lo"])
    # carbon marks are the open sample's
    assert "40304" in _js(drv, "return document.getElementById('chrom-note').textContent")
    # remove one
    _js(drv, "document.querySelector('#chrom-legend li[data-key=\"s:%d\"] [data-testid=trace-remove]').click();" % backfill)
    assert wait_for(drv, lambda: _js(drv, LEGEND_JS) == ["40304", "40304 (2)", "Diesel"]), _js(drv, LEGEND_JS)
    # kept across a reload (sessionStorage), with the mode
    _js(drv, "document.querySelector('#chrom-mode [data-mode=stacked]').click();")
    _open(drv, base, f"/samples/{final}")
    assert wait_for(drv, lambda: len(_js(drv, TRACES_JS)) == 3), _js(drv, TRACES_JS)
    assert _js(drv, "return GCSamples.state.overlay.mode") == "stacked"
    assert _js(drv, CHART_DRAWN_JS) == "ok"
    # another sample keeps the set (Diesel next to each sample); the open one is never drawn twice
    _row_click(drv, rerun)
    assert wait_for(drv, lambda: _js(drv, LEGEND_JS) == ["40304 (2)", "Diesel"]), _js(drv, LEGEND_JS)
    # add by lab ID
    _js(drv, "document.getElementById('btn-add-trace').click();")
    assert wait_for(drv, lambda: _js(drv, "return !!document.querySelector('[data-testid=overlay-lab]')"))
    drv.find_element("css selector", "[data-testid=overlay-lab]").send_keys("40298\n")
    assert wait_for(drv, lambda: "40298" in _js(drv, LEGEND_JS)), _js(drv, LEGEND_JS)
    _js(drv, "document.getElementById('btn-add-trace').click();")
    assert wait_for(drv, lambda: _js(drv, "return !!document.querySelector('[data-testid=overlay-lab]')"))
    drv.find_element("css selector", "[data-testid=overlay-lab]").send_keys("nope-77\n")
    assert wait_for(drv, lambda: "No GC run has lab ID nope-77" in _js(
        drv, "return document.querySelector('[data-testid=overlay-lab-out]').textContent"))
    _js(drv, "document.getElementById('btn-add-trace').click();")
    # clear all
    _js(drv, "document.querySelector('[data-testid=trace-clear]').click();")
    assert wait_for(drv, lambda: len(_js(drv, TRACES_JS)) == 1)
    assert _js(drv, "return document.getElementById('chrom-legend').hidden && document.getElementById('chrom-mode').hidden")
    assert _js(drv, CHART_DRAWN_JS) == "ok"


def test_an_overlay_that_does_not_load_says_so_in_the_legend(page):
    drv, base, hub = page
    _open(drv, base, f"/samples/{hub.ids['final']}")
    assert wait_for(drv, lambda: _js(drv, CHART_DRAWN_JS) == "ok")
    _js(drv, "GCSamples.addOverlay([{kind: 'standard', name: 'Gone'}, {kind: 'sample', id: arguments[0], label: '39999'}]);",
        hub.ids["resultonly"])
    li = wait_for(drv, lambda: _js(drv, "const t = document.querySelector('#chrom-legend').innerText;"
                                        "return /not found/.test(t) && /without its CDF/.test(t) ? t : null;"))
    assert li, _js(drv, "return document.querySelector('#chrom-legend').innerText")
    assert _js(drv, CHART_DRAWN_JS) == "ok"                       # the open sample still drawn
    _js(drv, "document.querySelector('[data-testid=trace-clear]').click();")
    assert wait_for(drv, lambda: _js(drv, "return document.getElementById('chrom-legend').hidden"))


# ── deferred: the backfill reason on the row, the action first ──────────────

def test_a_backfill_row_says_why_and_its_action_comes_first(page):
    drv, base, hub = page
    _open(drv, base, "/samples")
    bf = hub.ids["backfill"]
    sel = f'.srow[data-sample-id="{bf}"]'
    text = wait_for(drv, lambda: _js(drv, "const r = document.querySelector(arguments[0] + ' [data-testid=row-reason]');"
                                          "return r && /went live/.test(r.textContent) ? r.textContent : null;", sel))
    assert text and text.startswith("Backfill · not released · injected Sep 20 13:30 · before "), text
    assert "(Sep 22 00:00)" in text, text
    # the action is the line's first item, so a long reason or review note never hides it
    assert _js(drv, "return document.querySelector(arguments[0] + ' .l2').firstElementChild.dataset.testid", sel) == "row-fix"
    assert _js(drv, "return document.querySelector(arguments[0] + ' [data-testid=row-reason]').title", sel) == text
    # the header says it too, in full
    _row_click(drv, bf)
    assert wait_for(drv, lambda: "went live" in _js(drv, "return document.getElementById('d-reason').textContent"))
