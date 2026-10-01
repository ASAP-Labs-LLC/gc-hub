"""v5.0.0 lane S in a real browser: the Samples page, keyed on data-testid.

Against a hub with real samples (``tests/hub_boot.py``), signed in:

* light and dark, at 1366x768 (icon rail) and 1440x900 (full sidebar): the
  list, the header, Overview; no sideways scroll, nothing under the version
  badge, WCAG AA on every visible text;
* the address bar is the link: the views push /samples/<id>[/compare|/data],
  Back and Forward walk them, filters and search go in the query and survive
  a reload, the old /?sample= link is rewritten, /lab/<id> lands on its run;
* held rows name their reason and link to the fix; the Data view's numbers,
  calibration and history;
* keyboard: j/k and the arrows step through the list, Ctrl K focuses the
  filter; multi-select with the bulk bar, "Select all N matching this filter"
  (from GET /api/files/ids) with a confirmation naming the count and the
  filter, run as a task through the existing endpoints;
* Copy link copies the hub address; Compare mounts GCCompare (lane C's view,
  or the stub) and its standard goes in ?standard=; gc:adjust hides the list.

Skipped when selenium or a Chrome/chromedriver can't be started. Screenshots
go to ``GC_UI_SHOTS`` when it is set.
"""
from __future__ import annotations

import os
import sys
import time
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
from test_ui_setup_pages_smoke import CONTRAST_JS, SIZES, THEMES, _driver, _js  # noqa: E402
from test_ui_shell_admin_smoke import BADGE_OVERLAP_JS  # noqa: E402
from ui_wait import wait_for  # noqa: E402

SHOTS = os.environ.get("GC_UI_SHOTS")
PRELOAD = """
window.__clip = null;
try {
  Object.defineProperty(navigator, 'clipboard', { configurable: true,
    value: { writeText: async (t) => { window.__clip = t; } } });
} catch (e) {}
"""


def _shot(drv, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        drv.save_screenshot(str(Path(SHOTS) / name))


def _tid(drv, testid, attr="length"):
    return _js(drv, f"return document.querySelectorAll('[data-testid=\"{testid}\"]').{attr};")


def _open(drv, base, path, theme="light", size=(1440, 900)):
    drv.set_window_size(*size)
    drv.get(base + "/static/favicon.svg")
    _js(drv, "localStorage.setItem('gc.theme', arguments[0]); localStorage.removeItem('gc.sidebar');"
             "localStorage.removeItem('gc-sample-sort');", theme)
    drv.get(base + path)
    assert wait_for(drv, lambda: _js(drv, "return !!(window.GCSamples && GCSamples.ready)")), \
        _js(drv, "return document.body.innerText.slice(0, 400)")


def _path(drv):
    return _js(drv, "return location.pathname + location.search;")


def _rows(drv):
    return _js(drv, "return [...document.querySelectorAll('[data-testid=sample-row]')]"
                    ".map(r => Number(r.dataset.sampleId));")


def _errors(drv, expected=()):
    """SEVERE console lines, less the favicon and the given expected answers
    (a result-only run's trace and curve are 404s: it has no CDF)."""
    try:
        logs = drv.get_log("browser")
    except Exception:  # noqa: BLE001
        return []
    return [e["message"] for e in logs if e["level"] == "SEVERE" and "favicon.ico" not in e["message"]
            and not any(x in e["message"] for x in expected)]


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("samples-ui")
    hub = build_hub(tmp)
    with booted(tmp) as (port, _proc, _data, _home):
        drv = _driver()
        drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": PRELOAD})
        try:
            browser_sign_in(drv, port)
            yield drv, f"http://127.0.0.1:{port}", hub
        finally:
            drv.quit()


def test_the_page_in_both_themes_at_both_sizes(page):
    drv, base, hub = page
    sid = hub.ids["final"]
    for theme in THEMES:
        for size in SIZES:
            _open(drv, base, f"/samples/{sid}", theme, size)
            assert _js(drv, "return document.documentElement.dataset.theme;") == theme
            assert wait_for(drv, lambda: len(_rows(drv)) == len(hub.ids)), _rows(drv)
            assert wait_for(drv, lambda: _tid(drv, "detail-lab", "length") == 1
                            and _js(drv, "return document.querySelector('[data-testid=detail-lab]').textContent") == "40304")
            assert "Final" in _js(drv, "return document.querySelector('[data-testid=detail-status]').textContent")
            assert _js(drv, "return document.querySelector('[data-testid=view-overview]').getAttribute('aria-selected')") == "true"
            assert wait_for(drv, lambda: _tid(drv, "results-table") == 1)
            assert _js(drv, "return document.querySelectorAll('[data-testid=results-table] tbody tr').length") == 13
            # the open row is marked, the sidebar marks Samples
            assert _js(drv, "return document.querySelector('.srow.active').dataset.sampleId") == str(sid)
            assert _js(drv, "return document.querySelector('.nav-item[data-nav=samples]').getAttribute('aria-current')") == "page"
            # the frame: no sideways scroll, the rail below 1400px, the badge clear, AA
            assert _js(drv, "return document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1;")
            sb = _js(drv, "return document.getElementById('sidebar').getBoundingClientRect().width;")
            assert (sb <= 72) if size[0] < 1400 else (sb >= 250), sb
            time.sleep(0.3)
            assert _js(drv, BADGE_OVERLAP_JS) == [], (theme, size)
            assert _js(drv, CONTRAST_JS) == [], (theme, size)
            assert _errors(drv) == [], (theme, size)
            if size == (1440, 900):
                _shot(drv, f"samples-overview-{theme}.png")


def test_views_are_urls_and_back_forward_walk_them(page):
    drv, base, hub = page
    sid = hub.ids["final"]
    _open(drv, base, f"/samples/{sid}")
    _js(drv, "document.querySelector('[data-testid=view-data]').click();")
    assert wait_for(drv, lambda: _path(drv) == f"/samples/{sid}/data")
    assert wait_for(drv, lambda: _js(drv, "return document.querySelectorAll('[data-testid=data-table] tbody tr').length") == 13)
    assert "Revision 1" in _js(drv, "return document.querySelector('[data-testid=history]').textContent")
    assert "Anchors" in _js(drv, "return document.querySelector('[data-testid=calibration-used]').textContent")
    assert _tid(drv, "copy-table") == 1 and _js(drv, "return document.querySelector('[data-testid=copy-table]').hidden") is False
    _shot(drv, "samples-data-light.png")
    drv.back()
    assert wait_for(drv, lambda: _path(drv) == f"/samples/{sid}")
    assert wait_for(drv, lambda: not _js(drv, "return document.querySelector('[data-testid=panel-overview]').hidden"))
    drv.forward()
    assert wait_for(drv, lambda: not _js(drv, "return document.querySelector('[data-testid=panel-data]').hidden"))
    # a reload restores the view
    drv.refresh()
    assert wait_for(drv, lambda: _js(drv, "return !!(window.GCSamples && GCSamples.ready)"))
    assert wait_for(drv, lambda: not _js(drv, "return document.querySelector('[data-testid=panel-data]').hidden"))


def test_filters_search_and_sort_live_in_the_query(page):
    drv, base, hub = page
    _open(drv, base, "/samples")
    _js(drv, "document.querySelector('[data-testid=chip-status-held]').click();")
    assert wait_for(drv, lambda: "status=held" in _path(drv))
    held = {hub.ids["other"], hub.ids["held"]}
    assert wait_for(drv, lambda: set(_rows(drv)) == held), _rows(drv)
    # a held row names its reason and links to its fix
    row = f"[data-testid=sample-row][data-sample-id=\"{hub.ids['held']}\"]"
    assert "waiting for calibration" in _js(drv, f"return document.querySelector('{row}').textContent")
    assert _js(drv, f"return document.querySelector('{row} [data-testid=row-fix]').getAttribute('href')") \
        == "/calibration?instrument=gc2"
    assert _js(drv, f"return document.querySelector('[data-testid=sample-row][data-sample-id=\"{hub.ids['other']}\"] "
                    "[data-testid=row-fix]').getAttribute('href')") == "/instruments/gc1#methods"
    drv.refresh()
    assert wait_for(drv, lambda: _js(drv, "return !!(window.GCSamples && GCSamples.ready)"))
    assert wait_for(drv, lambda: set(_rows(drv)) == held)
    assert _js(drv, "return document.querySelector('[data-testid=chip-status-held]').getAttribute('aria-pressed')") == "true"
    # search goes in the query too (typed: replaceState)
    _js(drv, "document.querySelector('[data-testid=chip-status-held]').click();")
    box = drv.find_element("css selector", "[data-testid=filter-q]")
    box.send_keys("4030")
    assert wait_for(drv, lambda: "q=4030" in _path(drv))
    assert wait_for(drv, lambda: set(_rows(drv)) == {hub.ids["final"], hub.ids["rerun"]}), _rows(drv)
    # sort by lab ID
    _js(drv, "document.querySelector('#sort-switch [data-sort=lab]').click();")
    assert wait_for(drv, lambda: "sort=lab" in _path(drv))
    # the old link form is rewritten
    _open(drv, base, f"/?sample={hub.ids['final']}&tab=data")
    assert wait_for(drv, lambda: _path(drv) == f"/samples/{hub.ids['final']}/data")
    # a lab link lands on its newest run, which lists the other
    _open(drv, base, "/lab/40304")
    assert wait_for(drv, lambda: _path(drv) == f"/samples/{hub.ids['rerun']}")
    assert wait_for(drv, lambda: _tid(drv, "other-runs") == 1 and not _js(
        drv, "return document.querySelector('[data-testid=other-runs]').hidden"))
    assert f"/samples/{hub.ids['final']}" in _js(drv, "return document.querySelector('#runs-list a').getAttribute('href')")


def test_keyboard_steps_and_ctrl_k(page):
    drv, base, hub = page
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.keys import Keys
    _open(drv, base, "/samples")
    order = wait_for(drv, lambda: _rows(drv) if len(_rows(drv)) == len(hub.ids) else None)
    _js(drv, "document.activeElement && document.activeElement.blur(); document.getElementById('rows').focus();")
    ActionChains(drv).send_keys("j").perform()
    assert wait_for(drv, lambda: _path(drv) == f"/samples/{order[0]}")
    ActionChains(drv).send_keys("j").perform()
    assert wait_for(drv, lambda: _path(drv) == f"/samples/{order[1]}")
    ActionChains(drv).send_keys(Keys.ARROW_UP).perform()
    assert wait_for(drv, lambda: _path(drv) == f"/samples/{order[0]}")
    ActionChains(drv).key_down(Keys.CONTROL).send_keys("k").key_up(Keys.CONTROL).perform()
    assert wait_for(drv, lambda: _js(drv, "return document.activeElement.id") == "filter-q")


def test_multi_select_bulk_bar_and_select_all_matching(page):
    drv, base, hub = page
    _open(drv, base, "/samples")
    order = wait_for(drv, lambda: _rows(drv) if len(_rows(drv)) == len(hub.ids) else None)
    assert _js(drv, "return document.querySelector('[data-testid=bulk-bar]').hidden") is True

    def lead(i, **mods):
        _js(drv, "const r = document.querySelectorAll('[data-testid=sample-row] .lead')[arguments[0]];"
                 "r.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0, shiftKey: arguments[1]}));"
                 "document.dispatchEvent(new MouseEvent('mouseup'));", i, bool(mods.get("shift")))
    lead(0)
    lead(2, shift=True)                                  # shift-click: the range
    assert wait_for(drv, lambda: _js(drv, "return document.querySelector('[data-testid=bulk-text]').textContent") == "3 selected")
    assert _js(drv, "return document.querySelector('[data-testid=bulk-bar]').hidden") is False
    assert _js(drv, "return document.querySelectorAll('.srow.checked').length") == 3
    # select every row shown; with more on the server, the bar offers them all
    _js(drv, "document.querySelector('[data-testid=select-shown]').click();")
    assert wait_for(drv, lambda: _js(drv, "return GCSamples.state.sel.ids.size") == len(order))
    # pretend more match than are loaded, then untick and tick "select all shown" again
    _js(drv, "GCSamples.state.total = 1234;"
             "document.querySelector('[data-testid=select-shown]').click();"
             "document.querySelector('[data-testid=select-shown]').click();")
    offer = wait_for(drv, lambda: _js(drv, "const b = document.querySelector('[data-testid=bulk-select-all-matching]');"
                                         "return !b.hidden && b.textContent"))
    assert offer == "Select all 1,234 matching this filter", offer
    _js(drv, "document.querySelector('[data-testid=bulk-select-all-matching]').click();")
    assert wait_for(drv, lambda: _js(drv, "return document.querySelector('[data-testid=bulk-text]').textContent")
                    == "All 1,234 matching this filter are selected")
    _shot(drv, "samples-bulk-light.png")
    # a bulk action on the filter asks first, naming the count the server has for it and the filter
    _js(drv, "document.querySelector('[data-testid=bulk-reprocess]').click();")
    assert wait_for(drv, lambda: _js(drv, "return document.getElementById('bulk-confirm').open"))
    n = len(hub.ids)
    assert _js(drv, "return document.getElementById('bc-title').textContent") == f"Re-process {n} samples?"
    assert "All samples" in _js(drv, "return document.getElementById('bc-body').textContent")
    _js(drv, "document.querySelector('[data-testid=bulk-confirm-ok]').click();")
    toast = wait_for(drv, lambda: (lambda t: t if t.startswith("Re-process:") else None)(
        _js(drv, "return document.getElementById('toast').textContent")))
    assert toast and f"of {n} done" in toast and "refused" in toast, toast       # the result-only import is refused
    assert wait_for(drv, lambda: _js(drv, "return document.querySelector('[data-testid=bulk-bar]').hidden") is True)
    # Clear selection
    lead(1)
    _js(drv, "document.querySelector('[data-testid=bulk-clear]').click();")
    assert wait_for(drv, lambda: _js(drv, "return GCSamples.state.sel.ids.size") == 0)


def test_a_result_only_run_shows_its_imported_numbers(page):
    """A v1-imported run has no CDF, so no curve: Overview and Data show its /api/table numbers."""
    drv, base, hub = page
    sid = hub.ids["resultonly"]
    _open(drv, base, f"/samples/{sid}")
    assert wait_for(drv, lambda: _tid(drv, "results-table") == 1 and _tid(drv, "results-from-table") == 1)
    cells = _js(drv, "return [...document.querySelectorAll('[data-testid=results-table] tbody tr')]"
                     ".map(tr => [...tr.children].map(td => td.textContent));")
    assert cells[0][0] == "IBP" and cells[0][1] != "—" and cells[0][2] != "—", cells
    _js(drv, "document.querySelector('[data-testid=view-data]').click();")
    assert wait_for(drv, lambda: _js(drv, "return document.querySelectorAll('[data-testid=data-table] tbody tr').length") == 13)
    rep = _js(drv, "return document.querySelector('[data-testid=data-table] tbody tr td.rep').textContent")
    assert rep != "—", rep
    assert _errors(drv, expected=(f"/api/samples/{sid}/trace", f"/api/samples/{sid}/distillation-curve")) == []


def test_copy_link_compare_and_adjust(page):
    drv, base, hub = page
    sid = hub.ids["final"]
    _open(drv, base, f"/samples/{sid}")
    assert wait_for(drv, lambda: _js(drv, "return document.querySelector('[data-testid=act-cdf]').getAttribute('href')")
                    == f"/api/samples/{sid}/cdf")
    _js(drv, "document.querySelector('[data-testid=more-menu-button]').click();")
    assert _js(drv, "return document.querySelector('[data-testid=more-menu]').hidden") is False
    assert _js(drv, "return document.querySelector('[data-testid=act-cdf]').getAttribute('href')") == f"/api/samples/{sid}/cdf"
    _js(drv, "document.querySelector('[data-testid=act-copy-link]').click();")
    assert wait_for(drv, lambda: _js(drv, "return window.__clip") == f"https://gc.asaplabs.net/samples/{sid}")
    # Compare mounts lane C's GCCompare; the standard it settles on goes in the address
    _js(drv, "document.querySelector('[data-testid=view-compare]').click();")
    assert wait_for(drv, lambda: _path(drv).startswith(f"/samples/{sid}/compare"))
    assert wait_for(drv, lambda: _js(drv, "return !!(GCSamples.state.compare)"))
    assert _js(drv, "return !window.GCCompare.__stub && typeof GCCompare.openExportSheet") == "function"
    assert wait_for(drv, lambda: "standard=" in _path(drv)), _path(drv)
    assert wait_for(drv, lambda: _tid(drv, "compare-standard") == 1)
    # a reload with ?standard= opens Compare on that standard
    drv.get(f"{base}/samples/{sid}/compare?standard=Diesel")
    assert wait_for(drv, lambda: _js(drv, "return !!(window.GCSamples && GCSamples.ready && GCSamples.state.compare)"))
    assert wait_for(drv, lambda: _js(drv, "return GCSamples.state.compare.standard()") == "Diesel")
    assert _path(drv) == f"/samples/{sid}/compare?standard=Diesel"
    # the header's Add to queue carries what Compare shows: a threshold set in Adjust
    _js(drv, "try { GCReportQueue.clear(); } catch (e) {}")
    _js(drv, "document.querySelector('[data-testid=compare-adjust-toggle]').click();")
    box = wait_for(drv, lambda: drv.find_element("css selector", "[data-testid=adjust-significant]"))
    box.clear()
    box.send_keys("2345")
    assert wait_for(drv, lambda: _js(drv, "return GCSamples.state.compare.params().thresh_significant") in (2345, "2345"))
    _js(drv, "document.querySelector('[data-testid=add-to-queue]').click();")
    item = wait_for(drv, lambda: _js(drv, "const i = GCReportQueue.items(); return i.length && i[i.length - 1];"))
    assert item and item["sample_id"] == sid and item["standard_name"] == "Diesel", item
    assert float(item["params"]["thresh_significant"]) == 2345, item
    assert item.get("sample_name") != "40304"
    # Export report opens lane C's sheet
    _js(drv, "document.querySelector('[data-testid=export-report]').click();")
    assert wait_for(drv, lambda: _js(drv, "const d = document.querySelector('[data-testid=export-sheet]'); return !!(d && d.open)"))
    _js(drv, "document.querySelector('[data-testid=export-sheet]').close();")
    # the Adjust drawer hides the list
    _js(drv, "document.dispatchEvent(new CustomEvent('gc:adjust', {detail: {open: true}}));")
    assert wait_for(drv, lambda: _js(drv, "return getComputedStyle(document.querySelector('[data-testid=list-pane]')).display") == "none")
    _js(drv, "document.dispatchEvent(new CustomEvent('gc:adjust', {detail: {open: false}}));")
    assert wait_for(drv, lambda: _js(drv, "return getComputedStyle(document.querySelector('[data-testid=list-pane]')).display") != "none")
    assert _errors(drv) == []


def test_the_detail_clears_the_badge_with_overlay_scrollbars(page):
    """Overlay scrollbars (Chrome on a Mac trackpad; ``--hide-scrollbars``
    here) take no width, and a release tag (``v5.0.0``) is wider than this
    checkout's ``dev``: the detail's right gutter must clear the version badge
    on its own, not thanks to a 15 px classic scrollbar (v5.0.0 put the
    Overview's results table under the badge at 1366 and 1440)."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    _drv0, base, hub = page
    opts = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--force-device-scale-factor=1", "--hide-scrollbars"):
        opts.add_argument(arg)
    try:
        drv = webdriver.Chrome(options=opts)
    except Exception as exc:  # noqa: BLE001 - no Chrome / driver here
        pytest.skip(f"headless Chrome unavailable: {exc}")
    try:
        browser_sign_in(drv, int(base.rsplit(":", 1)[1]))
        sid = hub.ids["final"]
        for size in SIZES:
            for view, ready in (("", "results-table"), ("/data", "data-table")):
                _open(drv, base, f"/samples/{sid}{view}", "light", size)
                assert wait_for(drv, lambda: _tid(drv, ready) == 1)
                # today's tag, then a wider one: the gutter follows the badge's real width
                for tag in ("v5.0.0", "v10.10.10"):
                    _js(drv, "document.getElementById('app-version').textContent = arguments[0];", tag)
                    time.sleep(0.2)
                    if tag == "v5.0.0":       # no visible change at today's tag
                        assert _js(drv, "return getComputedStyle(document.getElementById('detail')).paddingRight") == "48px"
                    for top in (True, False):
                        _js(drv, "const d = document.getElementById('detail');"
                                 "d.scrollTop = arguments[0] ? 0 : d.scrollHeight;", top)
                        time.sleep(0.15)
                        assert _js(drv, BADGE_OVERLAP_JS) == [], (size, view, tag, top)
    finally:
        drv.quit()


def test_a_live_update_in_flight_never_adds_rows_of_the_old_filter(page):
    """A live update fetches its changed rows with the filter of the moment;
    if the operator changes the filter before that answer arrives, its rows
    must not be merged into the new filter's list (a re-processed Final run
    showed up under Held)."""
    from bootapp import post
    drv, base, hub = page
    port = int(base.rsplit(":", 1)[1])
    _open(drv, base, "/samples")
    assert wait_for(drv, lambda: len(_rows(drv)) == len(hub.ids))
    _js(drv, """
        window.__held = 0;
        const realFetch = window.fetch;
        window.fetch = function (url, opts) {
            const u = String(url);
            if (u.startsWith('/api/files?') && /[?&]ids=/.test(u) && !/status=/.test(u)) {
                window.__held++;
                return new Promise(r => setTimeout(r, 2500)).then(() => realFetch.call(window, url, opts));
            }
            return realFetch.apply(this, arguments);
        };""")
    rid = hub.ids["rerun"]
    assert post(port, "/api/reprocess", {"sample_ids": [rid]})[0] == 200
    assert wait_for(drv, lambda: _js(drv, "return window.__held") >= 1, timeout=20)
    _js(drv, "document.querySelector('[data-testid=chip-status-held]').click();")
    assert wait_for(drv, lambda: _path(drv) == "/samples?status=held")
    time.sleep(3.5)                                   # the held-back live answer lands
    statuses = _js(drv, "return [...document.querySelectorAll('[data-testid=sample-row]')].map(r => r.dataset.status);")
    assert statuses and set(statuses) == {"held"}, (statuses, _rows(drv))
    assert rid not in _rows(drv)


def test_compare_mounts_once_when_asked_twice_while_the_standards_load(page):
    """Compare waits for the standards before it mounts. Asked twice in that
    wait (the Compare tab clicked twice, or a live update for the open run),
    it mounted twice: the first view's Adjust drawer stayed in the page
    (duplicate ids) and every change was analysed twice."""
    drv, base, hub = page
    _open(drv, base, f"/samples/{hub.ids['final']}")
    assert wait_for(drv, lambda: _tid(drv, "results-table") == 1)
    _js(drv, """
        window.__analysis = 0;
        const realFetch = window.fetch;
        window.fetch = function (url, opts) {
            const u = String(url);
            if (u.startsWith('/api/analysis')) window.__analysis++;
            if (u.startsWith('/api/comparison-standards'))
                return new Promise(r => setTimeout(r, 1500)).then(() => realFetch.call(window, url, opts));
            return realFetch.apply(this, arguments);
        };
        GCSamples.state.standardsReady = null;           // as on a first visit: the standards still to load
        document.querySelector('[data-testid=view-compare]').click();""")
    time.sleep(0.2)
    _js(drv, "document.querySelector('[data-testid=view-compare]').click();")
    assert wait_for(drv, lambda: _js(drv, "return !!GCSamples.state.compare") and _tid(drv, "compare-standard") == 1)
    time.sleep(2.5)
    assert _tid(drv, "compare-drawer") == 1
    assert _js(drv, "return window.__analysis") == 1
    assert _errors(drv) == []


def test_the_queue_upload_cannot_start_twice_while_its_request_is_out(page):
    """The report queue's Upload is disabled while its POST is out; a re-render
    in that window (an item removed, the upload status answering) must not
    turn it back on, or a second click queues the same reports for QBench
    again (markSent only runs when the first answer arrives)."""
    drv, base, hub = page
    _open(drv, base, f"/samples/{hub.ids['final']}")
    _js(drv, """
        window.__uploads = 0;
        const realFetch = window.fetch;
        window.fetch = function (url, opts) {
            if (String(url) === '/api/qbench-upload') { window.__uploads++; return new Promise(() => {}); }
            return realFetch.apply(this, arguments);
        };
        GCReportQueue.clear();
        GCReportQueue.addMany([
            {sample_id: arguments[0], lab_id: '40304', sample_name: 'GC Analysis', instrument: 'gc1', standard_name: 'Diesel'},
            {sample_id: arguments[1], lab_id: '40304', sample_name: 'GC Analysis', instrument: 'gc1', standard_name: 'Diesel'},
        ], {quiet: true});
        GCReportQueue.openSheet();""", hub.ids["final"], hub.ids["rerun"])
    up = "document.querySelector('[data-testid=rq-upload]')"
    _js(drv, f"{up}.click();")                       # shows the sign-in
    assert wait_for(drv, lambda: _js(drv, "return !document.querySelector('[data-testid=rq-signin]').hidden"))
    _js(drv, f"{up}.click();")                       # Start upload: the POST stays out
    assert wait_for(drv, lambda: _js(drv, "return window.__uploads") == 1)
    assert _js(drv, f"return {up}.disabled") is True
    _js(drv, "GCReportQueue.remove(arguments[0]);", f"s{hub.ids['rerun']}")
    assert _js(drv, f"return {up}.disabled") is True
    _js(drv, f"{up}.click();")
    time.sleep(0.3)
    assert _js(drv, "return window.__uploads") == 1
    _js(drv, "GCReportQueue.clear(); document.getElementById('report-queue-sheet').close();")


HUNG_UPLOAD_JS = """
    // the start hangs; the upload status answers from __statusSeq (the last one
    // repeats; null = the status call fails). __statusEarly answers before the start.
    window.__uploads = 0;
    window.__statusSeq = arguments[1];
    window.__statusEarly = arguments[2] || {active: false, items: []};
    const json = (o) => Promise.resolve(new Response(JSON.stringify(o),
        {status: 200, headers: {'Content-Type': 'application/json'}}));
    const realFetch = window.fetch;
    window.fetch = function (url, opts) {
        const u = String(url);
        if (u === '/api/qbench-upload') { window.__uploads++; return new Promise(() => {}); }
        if (u.startsWith('/api/qbench-upload-status')) {
            if (!window.__uploads) return json(window.__statusEarly);
            const a = window.__statusSeq.length > 1 ? window.__statusSeq.shift() : window.__statusSeq[0];
            return a === null ? Promise.reject(new TypeError('Failed to fetch')) : json(a);
        }
        return realFetch.apply(this, arguments);
    };
    window.EventSource = function () { this.close = () => {}; };   // no stream in this test
    GCReportQueue.timeouts.start = 1000;
    GCReportQueue.timeouts.watch = 3000;
    GCReportQueue.timeouts.watchEvery = 300;
    GCReportQueue.clear();
    GCReportQueue.add({sample_id: arguments[0], lab_id: '40304', sample_name: 'GC Analysis',
                       instrument: 'gc1', standard_name: 'Diesel'}, {quiet: true});
    GCReportQueue.openSheet();"""
RUNNING_40304 = {"active": True, "items": [{"idx": 0, "lab_id": "40304", "status": "uploading", "msg": "Uploading"}]}
UP = "document.querySelector('[data-testid=rq-upload]')"


def _hung_queue(drv, base, hub, status_seq, early=None):
    _open(drv, base, f"/samples/{hub.ids['final']}")
    _js(drv, HUNG_UPLOAD_JS, hub.ids["final"], status_seq, early)


def _start_hung_upload(drv, base, hub, status_seq):
    _hung_queue(drv, base, hub, status_seq)
    _js(drv, f"{UP}.click();")
    assert wait_for(drv, lambda: _js(drv, "return !document.querySelector('[data-testid=rq-signin]').hidden"))
    _js(drv, f"{UP}.click();")
    assert wait_for(drv, lambda: _js(drv, "return window.__uploads") == 1)


def _rq_msg(drv):
    return _js(drv, "return document.querySelector('[data-testid=rq-msg]').textContent")


def _sent(drv):
    return _js(drv, "return GCReportQueue.items()[0].sent_at || null") is not None


def _rq_done(drv):
    _js(drv, "GCReportQueue.timeouts.start = 60000; GCReportQueue.timeouts.watch = 60000;"
             "GCReportQueue.timeouts.watchEvery = 5000; GCReportQueue.clear();"
             "document.getElementById('report-queue-sheet').close();")


def test_a_hung_upload_start_that_did_start_is_attached_not_retried(page):
    """The start request can outlive the browser's 60 s (the hub reading a
    saved QBench login from an unreachable share) and still start the upload.
    On the timeout the queue asks the upload status first: when the upload
    is running with these lab IDs, the items are sent (marked so) and the
    queue follows the progress; it never says "Not uploaded", which would
    invite a retry that uploads the same reports twice."""
    drv, base, hub = page
    _start_hung_upload(drv, base, hub, [RUNNING_40304])
    assert wait_for(drv, lambda: _sent(drv), timeout=10), _rq_msg(drv)
    assert "Not uploaded" not in _rq_msg(drv)
    _rq_done(drv)


def test_a_hung_upload_start_that_starts_late_is_found_while_the_sheet_watches(page):
    """The hub registers the upload only after the timeout's first status
    check: the sheet keeps checking (Upload off meanwhile, saying so), finds
    it, marks the report sent, and never offers "Add 1 to the upload"; one
    POST in all."""
    drv, base, hub = page
    _start_hung_upload(drv, base, hub, [{"active": False, "items": []}, {"active": False, "items": []},
                                        RUNNING_40304])
    assert wait_for(drv, lambda: "Checking whether the hub started it" in _rq_msg(drv), timeout=10), _rq_msg(drv)
    assert _js(drv, f"return {UP}.disabled") is True
    assert wait_for(drv, lambda: _sent(drv), timeout=10), _rq_msg(drv)
    _js(drv, f"if (!{UP}.hidden && !{UP}.disabled) {UP}.click();")
    time.sleep(0.5)
    assert "Add 1" not in _js(drv, f"return {UP}.hidden ? '' : {UP}.textContent")
    assert "Not uploaded" not in _rq_msg(drv)
    assert _js(drv, "return window.__uploads") == 1
    _rq_done(drv)


def test_a_hung_upload_start_with_no_status_says_it_may_still_start(page):
    """No answer from the start, and the status never shows the upload: after
    the watch the queue says so (never "Not uploaded"), nothing is marked
    sent, and Upload is usable again."""
    drv, base, hub = page
    _start_hung_upload(drv, base, hub, [None])
    assert wait_for(drv, lambda: "Checking whether the hub started it" in _rq_msg(drv), timeout=10), _rq_msg(drv)
    assert _js(drv, f"return {UP}.disabled") is True
    assert wait_for(drv, lambda: "may still start" in _rq_msg(drv), timeout=15), _rq_msg(drv)
    assert "Not uploaded" not in _rq_msg(drv)
    assert not _sent(drv)
    assert _js(drv, f"return {UP}.disabled") is False
    _rq_done(drv)


def test_opening_the_queue_during_a_running_upload_marks_its_reports_sent(page):
    """The sheet's own status check (on open) finds an upload already running
    with a queued report's lab ID: that report counts as sent, so the sheet
    never offers to add it to the upload a second time."""
    drv, base, hub = page
    _hung_queue(drv, base, hub, [RUNNING_40304], early=RUNNING_40304)
    assert wait_for(drv, lambda: _sent(drv), timeout=10), _js(drv, f"return {UP}.textContent")
    assert "Add 1" not in _js(drv, f"return {UP}.hidden ? '' : {UP}.textContent")
    assert _js(drv, "return window.__uploads") == 0
    _rq_done(drv)


def test_back_from_a_missing_sample_shows_the_pick_a_sample_placeholder(page):
    """A run that is gone (purged while its row was on screen, or an old
    link) shows its error in the empty detail; going Back to the list must
    bring back "Pick a sample", not keep the old error on screen."""
    drv, base, _hub = page
    _open(drv, base, "/samples")
    assert wait_for(drv, lambda: "Pick a sample" in _js(drv, "return document.querySelector('[data-testid=detail-empty]').textContent"))
    _js(drv, "GCSamples.openSample(987654, 'push');")
    assert wait_for(drv, lambda: _path(drv) == "/samples/987654")
    assert wait_for(drv, lambda: "not found" in _js(drv, "return document.querySelector('[data-testid=detail-empty]').textContent"))
    drv.back()
    assert wait_for(drv, lambda: _path(drv) == "/samples")
    assert wait_for(drv, lambda: "Pick a sample" in _js(drv, "return document.querySelector('[data-testid=detail-empty]').textContent")), \
        _js(drv, "return document.querySelector('[data-testid=detail-empty]').textContent")
    assert _js(drv, "return document.querySelector('[data-testid=detail-empty]').hidden") is False
    assert _errors(drv, expected=("/api/samples/987654/metadata",)) == []
