"""v7: the distillation curve on the Samples page's Overview, in a real browser.

Against a hub with real samples (``tests/hub_boot.py``), signed in:

* the curve card draws the Results card's own numbers: a dot per percent
  point for D86 (per the Corrected D86 toggle, 40/60 midpoints included) and
  D2887, each dot named "50%: 285.1 °C, D86 uncorrected";
* the real mouse over a dot shows that point's temperature in the callout
  and marks its Results row and cell; pointing at a Results row lights its
  dot back; leaving clears both;
* the keyboard: the dots are one tab stop, the arrow keys walk them and the
  callout follows; Escape puts it away;
* the toggle redraws the curve with the Results card's new values;
* a result-only (v1-imported) run draws from its table row;
* light and dark at 1440x900 and 1366x768, and a phone (390x844) where a tap
  shows a point: no sideways scroll, nothing under the version badge, WCAG
  AA on every visible text (the callout included), no SVG-less WebGL.

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

from selenium.webdriver.common.action_chains import ActionChains  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402

from bootapp import booted, browser_sign_in  # noqa: E402
from hub_boot import build_hub  # noqa: E402
from test_ui_setup_pages_smoke import CONTRAST_JS, THEMES, _driver, _js  # noqa: E402
from test_ui_shell_admin_smoke import BADGE_OVERLAP_JS  # noqa: E402
from ui_wait import wait_for  # noqa: E402

SHOTS = os.environ.get("GC_UI_SHOTS")
PHONE = (390, 844)

DOT = "[data-testid=curve-dot][data-series=\"{s}\"][data-label=\"{l}\"]"
STATE_JS = r"""
const c = document.querySelector('[data-testid=curve-callout]');
const tr = document.querySelector('[data-testid=results-table] tr.is-linked');
const td = document.querySelector('[data-testid=results-table] td.is-hot');
const act = document.querySelector('[data-testid=curve-dot].is-active');
return {
  shown: !!c && !c.hidden,
  temp: c && !c.hidden ? c.querySelector('[data-testid=curve-callout-temp]').textContent : null,
  text: c && !c.hidden ? c.textContent : null,
  row: tr ? tr.dataset.label : null,
  cell: td ? [td.closest('tr').dataset.label, td.dataset.series, td.textContent] : null,
  dot: act ? [act.dataset.series, act.dataset.label] : null,
};
"""
CELLS_JS = r"""
const out = {};
for (const tr of document.querySelectorAll('[data-testid=results-table] tbody tr')) {
  for (const td of tr.querySelectorAll('td[data-series]')) out[td.dataset.series + '|' + tr.dataset.label] = td.textContent;
}
return out;
"""
DOTS_JS = r"""
const out = {};
for (const d of document.querySelectorAll('[data-testid=curve-dot]')) out[d.dataset.series + '|' + d.dataset.label] = d.getAttribute('aria-label');
return out;
"""


def _shot(drv, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        drv.save_screenshot(str(Path(SHOTS) / name))


def _size(drv, size):
    w, h = size
    if w >= 760:
        drv.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
        drv.set_window_size(w, h)
        inner = _js(drv, "return window.innerWidth;")
        if inner != w:
            drv.set_window_size(w + (w - inner), h)
        return
    drv.set_window_size(max(w, 600), h + 120)
    drv.execute_cdp_cmd("Emulation.setDeviceMetricsOverride",
                        {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": True})


def _open(drv, base, sid, theme="light", size=(1440, 900), corrected=False):
    _size(drv, size)
    drv.get(base + "/static/favicon.svg")
    _js(drv, "localStorage.setItem('gc.theme', arguments[0]); localStorage.removeItem('gc.sidebar');"
             "localStorage.setItem('gc.correctedD86', arguments[1]);", theme, "1" if corrected else "0")
    drv.get(f"{base}/samples/{sid}")
    assert wait_for(drv, lambda: _js(drv, "return !!(window.GCSamples && GCSamples.ready)"))
    assert wait_for(drv, lambda: _js(drv, "return document.querySelectorAll('[data-testid=curve-dot]').length > 0")), \
        _js(drv, "const m = document.querySelector('[data-testid=curve-message]'); return m && m.textContent;")
    _js(drv, "document.querySelector('[data-testid=curve-card]').scrollIntoView({block: 'center'});")
    time.sleep(0.2)


def _state(drv):
    return _js(drv, STATE_JS)


def _dot(drv, series, label):
    return drv.find_element(By.CSS_SELECTOR, DOT.format(s=series, l=label))


def _hover(drv, series, label):
    ActionChains(drv).move_to_element(_dot(drv, series, label)).perform()


def _away(drv):
    ActionChains(drv).move_to_element(drv.find_element(By.ID, "d-lab")).perform()


def _tap(drv, series, label):
    r = _js(drv, "const b = arguments[0].getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2];",
            _dot(drv, series, label))
    pt = {"x": r[0], "y": r[1]}
    drv.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [pt]})
    drv.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def _frame_ok(drv, what):
    assert _js(drv, "return document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1;"), what
    assert _js(drv, BADGE_OVERLAP_JS) == [], what
    assert _js(drv, CONTRAST_JS) == [], what


def _errors(drv, expected=()):
    try:
        logs = drv.get_log("browser")
    except Exception:  # noqa: BLE001
        return []
    return [e["message"] for e in logs if e["level"] == "SEVERE" and "favicon.ico" not in e["message"]
            and not any(x in e["message"] for x in expected)]


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("samples-curve-ui")
    hub = build_hub(tmp)
    with booted(tmp) as (port, _proc, _data, _home):
        drv = _driver()
        try:
            browser_sign_in(drv, port)
            yield drv, f"http://127.0.0.1:{port}", hub
        finally:
            drv.quit()


def test_hovering_a_dot_shows_its_temperature_and_marks_its_results_row(page):
    drv, base, hub = page
    sid = hub.ids["final"]
    for theme in THEMES:
        for size in ((1440, 900), (1366, 768)):
            _open(drv, base, sid, theme, size)
            # plain SVG, never WebGL; a dot for every Results value, named as the cell reads
            assert _js(drv, "return !!document.querySelector('[data-testid=curve-plot] svg') && "
                            "!document.querySelector('[data-testid=curve-card] canvas');")
            cells = _js(drv, CELLS_JS)
            dots = _js(drv, DOTS_JS)
            assert set(dots) == {k for k, v in cells.items() if v != "—"}, (dots, cells)
            for k, label in dots.items():
                series, pct = k.split("|")
                name = "D86 uncorrected" if series == "d86" else "D2887"
                assert label == f"{pct}: {cells[k]} °C, {name}", (k, label, cells[k])
            # the real mouse over the D86 50% dot: its temperature, large, and its row and cell marked
            d50 = cells["d86|50%"]
            _hover(drv, "d86", "50%")
            assert wait_for(drv, lambda: _state(drv)["shown"])
            st = _state(drv)
            assert st["temp"] == f"{d50} °C", st
            assert "50% recovered" in st["text"] and "D86 uncorrected" in st["text"], st
            assert st["row"] == "50%" and st["cell"] == ["50%", "d86", d50] and st["dot"] == ["d86", "50%"], st
            # the callout sits inside the card, beside its dot (not over it)
            assert _js(drv, r"""
                const c = document.querySelector('[data-testid=curve-callout]').getBoundingClientRect();
                const k = document.querySelector('[data-testid=curve-card]').getBoundingClientRect();
                const d = document.querySelector('[data-testid=curve-dot].is-active').getBoundingClientRect();
                const cx = d.left + d.width / 2, cy = d.top + d.height / 2;
                const inside = c.left >= k.left - 1 && c.right <= k.right + 1 && c.top >= k.top - 1 && c.bottom <= k.bottom + 1;
                const clear = cx < c.left || cx > c.right || cy < c.top || cy > c.bottom;
                return inside && clear;""")
            time.sleep(0.25)
            _frame_ok(drv, (theme, size, "callout"))
            _shot(drv, f"curve-hover-{theme}-{size[0]}.png")
            # D2887 at 95%
            _hover(drv, "d2887", "95%")
            assert wait_for(drv, lambda: _state(drv)["dot"] == ["d2887", "95%"])
            st = _state(drv)
            assert st["temp"] == cells["d2887|95%"] + " °C" and st["cell"] == ["95%", "d2887", cells["d2887|95%"]], st
            # the mouse leaves: nothing marked
            _away(drv)
            assert wait_for(drv, lambda: not _state(drv)["shown"])
            assert _state(drv)["row"] is None and _state(drv)["dot"] is None
            # a Results row lights its dot back (the cell's series), and leaving clears it
            row = drv.find_element(By.CSS_SELECTOR, "[data-testid=results-table] tr[data-label='10%'] td[data-series=d2887]")
            ActionChains(drv).move_to_element(row).perform()
            assert wait_for(drv, lambda: _state(drv)["dot"] == ["d2887", "10%"])
            st = _state(drv)
            assert st["shown"] and st["temp"] == cells["d2887|10%"] + " °C" and st["row"] == "10%", st
            label_cell = drv.find_element(By.CSS_SELECTOR, "[data-testid=results-table] tr[data-label='FBP'] td:first-child")
            ActionChains(drv).move_to_element(label_cell).perform()
            assert wait_for(drv, lambda: _state(drv)["dot"] == ["d86", "FBP"])
            _away(drv)
            assert wait_for(drv, lambda: _state(drv)["dot"] is None)
            assert _errors(drv) == [], (theme, size)


def test_the_keyboard_walks_the_dots(page):
    drv, base, hub = page
    _open(drv, base, hub.ids["final"])
    cells = _js(drv, CELLS_JS)
    # one tab stop: the first D86 dot
    assert _js(drv, "return [...document.querySelectorAll('[data-testid=curve-dot]')].filter(d => d.getAttribute('tabindex') === '0')"
                    ".map(d => d.dataset.series + '|' + d.dataset.label);") == ["d86|IBP"]
    _js(drv, "document.querySelector('[data-testid=curve-dot][tabindex=\"0\"]').focus();")
    assert wait_for(drv, lambda: _state(drv)["dot"] == ["d86", "IBP"])
    assert _state(drv)["temp"] == cells["d86|IBP"] + " °C"
    active = drv.switch_to.active_element
    active.send_keys(Keys.ARROW_RIGHT)
    assert wait_for(drv, lambda: _state(drv)["dot"] == ["d86", "5%"])
    assert _js(drv, "return document.activeElement.getAttribute('aria-label');") == f"5%: {cells['d86|5%']} °C, D86 uncorrected"
    drv.switch_to.active_element.send_keys(Keys.END)
    assert wait_for(drv, lambda: _state(drv)["dot"] == ["d86", "FBP"])
    drv.switch_to.active_element.send_keys(Keys.ARROW_DOWN)
    assert wait_for(drv, lambda: _state(drv)["dot"] == ["d2887", "FBP"])
    assert _state(drv)["row"] == "FBP" and _state(drv)["cell"][1] == "d2887"
    # the tab stop follows the point
    assert _js(drv, "return document.activeElement.getAttribute('tabindex');") == "0"
    # a theme change redraws the curve and keeps the point and its focus
    _js(drv, "GCTheme.set('dark');")
    assert wait_for(drv, lambda: _js(drv, "return document.documentElement.dataset.theme;") == "dark")
    time.sleep(0.2)
    assert _state(drv)["dot"] == ["d2887", "FBP"] and _state(drv)["shown"]
    assert _js(drv, "return document.activeElement.dataset.label;") == "FBP"
    _js(drv, "GCTheme.set('light');")
    assert wait_for(drv, lambda: _js(drv, "return document.documentElement.dataset.theme;") == "light")
    time.sleep(0.2)                                   # the redraw (next frame) replaces the dots: send to the new one
    assert _js(drv, "return document.activeElement.dataset.label;") == "FBP"
    drv.switch_to.active_element.send_keys(Keys.ESCAPE)
    assert wait_for(drv, lambda: not _state(drv)["shown"])
    assert _state(drv)["row"] is None


def test_the_toggle_redraws_with_the_results_cards_numbers(page):
    drv, base, hub = page
    _open(drv, base, hub.ids["final"], corrected=False)
    before = _js(drv, DOTS_JS)
    _hover(drv, "d86", "50%")
    assert wait_for(drv, lambda: _state(drv)["shown"])
    drv.find_element(By.CSS_SELECTOR, "[data-testid=corrected-toggle]").click()
    assert wait_for(drv, lambda: _js(drv, DOTS_JS) != before)
    cells = _js(drv, CELLS_JS)
    dots = _js(drv, DOTS_JS)
    assert dots, cells
    for k, label in dots.items():
        series, pct = k.split("|")
        name = "D86 corrected" if series == "d86" else "D2887"
        assert label == f"{pct}: {cells[k]} °C, {name}", (k, label)
    assert "D86 corrected" in _js(drv, "return document.querySelector('[data-testid=curve-legend]').textContent;")
    # back off: as it was
    drv.find_element(By.CSS_SELECTOR, "[data-testid=corrected-toggle]").click()
    assert wait_for(drv, lambda: _js(drv, DOTS_JS) == before)


def test_a_result_only_run_draws_from_its_table_row(page):
    drv, base, hub = page
    rid = hub.ids["resultonly"]
    _open(drv, base, rid)
    assert _js(drv, "return !!document.querySelector('[data-testid=results-from-table]');")
    cells = _js(drv, CELLS_JS)
    dots = _js(drv, DOTS_JS)
    assert set(dots) == {k for k, v in cells.items() if v != "—"}
    _hover(drv, "d86", "50%")
    assert wait_for(drv, lambda: _state(drv)["temp"] == cells["d86|50%"] + " °C")
    assert _state(drv)["row"] == "50%"
    # the curve and trace are 404s for a run with no CDF (said in the chart's line)
    assert _errors(drv, expected=(f"/api/samples/{rid}/distillation-curve", f"/api/samples/{rid}/trace")) == []


def test_a_held_run_has_no_curve_card(page):
    drv, base, hub = page
    _size(drv, (1440, 900))
    drv.get(f"{base}/samples/{hub.ids['held']}")
    assert wait_for(drv, lambda: _js(drv, "return !!(window.GCSamples && GCSamples.ready)"))
    assert wait_for(drv, lambda: _js(drv, "return !!document.querySelector('#results-body .held-note');"))
    assert _js(drv, "return document.querySelector('[data-testid=curve-card]').hidden;")


def test_on_a_phone_a_tap_shows_a_point(page):
    drv, base, hub = page
    cells = None
    for theme in THEMES:
        _open(drv, base, hub.ids["final"], theme, PHONE)
        cells = _js(drv, CELLS_JS)
        # the card fits the one pane
        assert _js(drv, r"""
            const k = document.querySelector('[data-testid=curve-card]').getBoundingClientRect();
            return k.left >= 0 && k.right <= document.documentElement.clientWidth + 1;""")
        _tap(drv, "d86", "50%")
        assert wait_for(drv, lambda: _state(drv)["dot"] == ["d86", "50%"])
        st = _state(drv)
        assert st["temp"] == cells["d86|50%"] + " °C" and st["row"] == "50%", st
        # a tap never focuses the plot itself (no focus ring round the whole chart)
        assert _js(drv, "return document.activeElement.tagName.toLowerCase();") != "svg"
        time.sleep(0.25)
        _frame_ok(drv, (theme, "phone"))
        _shot(drv, f"curve-phone-{theme}.png")
        # the tap stays shown; another tap moves it; a tap elsewhere puts it away
        _tap(drv, "d2887", "90%")
        assert wait_for(drv, lambda: _state(drv)["dot"] == ["d2887", "90%"])
        r = _js(drv, "const b = document.getElementById('d-lab').getBoundingClientRect(); return [b.left + 4, b.top + 4];")
        _js(drv, "document.getElementById('d-lab').scrollIntoView({block: 'center'});")
        r = _js(drv, "const b = document.getElementById('d-lab').getBoundingClientRect(); return [b.left + 4, b.top + b.height / 2];")
        drv.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": r[0], "y": r[1]}]})
        drv.execute_cdp_cmd("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        assert wait_for(drv, lambda: _state(drv)["dot"] is None)
    _size(drv, (1440, 900))
