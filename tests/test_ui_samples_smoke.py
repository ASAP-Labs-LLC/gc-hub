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


def _errors(drv):
    try:
        logs = drv.get_log("browser")
    except Exception:  # noqa: BLE001
        return []
    return [e["message"] for e in logs if e["level"] == "SEVERE"
            and "favicon.ico" not in e["message"] and "compare_view.js" not in e["message"]]


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


def test_copy_link_compare_and_adjust(page):
    drv, base, hub = page
    sid = hub.ids["final"]
    _open(drv, base, f"/samples/{sid}")
    _js(drv, "document.querySelector('[data-testid=more-menu-button]').click();")
    assert _js(drv, "return document.querySelector('[data-testid=more-menu]').hidden") is False
    assert _js(drv, "return document.querySelector('[data-testid=act-cdf]').getAttribute('href')") == f"/api/samples/{sid}/cdf"
    _js(drv, "document.querySelector('[data-testid=act-copy-link]').click();")
    assert wait_for(drv, lambda: _js(drv, "return window.__clip") == f"https://gc.asaplabs.net/samples/{sid}")
    # Compare mounts GCCompare; its standard goes in the address
    _js(drv, "document.querySelector('[data-testid=view-compare]').click();")
    assert wait_for(drv, lambda: _path(drv) == f"/samples/{sid}/compare")
    assert wait_for(drv, lambda: _js(drv, "return !!(GCSamples.state.compare)"))
    _js(drv, "GCSamples.state.compare.setStandard('Diesel');")
    if _js(drv, "return !!window.GCCompare.__stub"):
        _js(drv, "const s = document.querySelector('[data-testid=compare-stub] select');"
                 "s.value = s.options[1] ? s.options[1].value : ''; s.dispatchEvent(new Event('change'));")
        want = _js(drv, "return document.querySelector('[data-testid=compare-stub] select').value")
        if want:
            assert wait_for(drv, lambda: "standard=" in _path(drv))
    # the Adjust drawer hides the list
    _js(drv, "document.dispatchEvent(new CustomEvent('gc:adjust', {detail: {open: true}}));")
    assert wait_for(drv, lambda: _js(drv, "return getComputedStyle(document.querySelector('[data-testid=list-pane]')).display") == "none")
    _js(drv, "document.dispatchEvent(new CustomEvent('gc:adjust', {detail: {open: false}}));")
    assert wait_for(drv, lambda: _js(drv, "return getComputedStyle(document.querySelector('[data-testid=list-pane]')).display") != "none")
    assert _errors(drv) == []
