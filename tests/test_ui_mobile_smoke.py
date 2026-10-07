"""v6.0 lane L5 in a real browser: the hub on phones and tablets.

Against a hub with real samples (``tests/hub_boot.py``), signed in, with the
viewport emulated over CDP (``Emulation.setDeviceMetricsOverride``):

* at 390x844 (a phone), light and dark, on every shell page: no sideways
  scroll, the menu button and the top bar's status shown, the sidebar off
  screen, the version badge passes taps through and covers nothing at the
  page's end, WCAG AA on every visible text;
* the drawer: the menu button opens the sidebar (aria-expanded, focus moves
  into it, the page behind is inert, Tab stays inside), Escape closes it and
  gives focus back, so do the overlay and the close button, and a nav link
  goes where it says; the top bar's bell opens the notifications panel;
* Samples, one pane at a time: the list, a row opens the sample (the list
  hides, "Samples" back shows), the views keep it, back returns to the list
  with its filters, the browser's Back returns to the sample; a sample's link
  opened directly shows the sample;
* at 768x1024 (a tablet) the icon rail stays and nothing scrolls sideways;
  at 1366x768 none of the phone controls show and Samples has both panes.

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
from test_ui_setup_pages_smoke import CONTRAST_JS, THEMES, _driver, _js  # noqa: E402
from ui_wait import click_when_ready, wait_for  # noqa: E402

SHOTS = os.environ.get("GC_UI_SHOTS")
PHONE = (390, 844)
TABLET = (768, 1024)
DESKTOP = (1366, 768)

# The version badge never takes a tap: whatever is under its middle gets it.
BADGE_TAPS_THROUGH_JS = r"""
const b = document.getElementById('app-version');
if (!b) return 'no badge';
const r = b.getBoundingClientRect();
if (!r.width) return 'badge not shown';
const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
return hit === b || b.contains(hit) ? 'the badge takes taps' : '';
"""

# Every visible leaf (text, control, image) whose box, clipped by its
# scrolling ancestors, meets the version badge's (the shell smoke's check,
# plus the clipping, and less what a closed <details> hides: Chrome still
# lays that out).
BADGE_OVERLAP_JS = r"""
const b = document.getElementById('app-version');
if (!b) return ['no badge'];
const br = b.getBoundingClientRect();
if (!br.width) return ['badge not shown'];
const out = [];
for (const el of document.body.querySelectorAll('*')) {
  if (el === b || el.contains(b) || b.contains(el)) continue;
  const cs = getComputedStyle(el);
  if (cs.visibility === 'hidden' || cs.display === 'none' || el.closest('[hidden]')
      || el.closest('details:not([open]) > :not(summary)')) continue;
  const leaf = ['INPUT','SELECT','BUTTON','TEXTAREA','IMG','SVG','CANVAS','A'].includes(el.tagName.toUpperCase())
    || Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
  if (!leaf) continue;
  let r = el.getBoundingClientRect();
  let box = { l: r.left, r: r.right, t: r.top, b: r.bottom };
  for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
    if (getComputedStyle(p).overflow === 'visible') continue;
    const q = p.getBoundingClientRect();
    box = { l: Math.max(box.l, q.left), r: Math.min(box.r, q.right), t: Math.max(box.t, q.top), b: Math.min(box.b, q.bottom) };
  }
  if (box.r <= box.l || box.b <= box.t) continue;
  if (box.l < br.right && box.r > br.left && box.t < br.bottom && box.b > br.top)
    out.push(el.tagName + '#' + el.id + '.' + el.className + ' ' + (el.textContent || '').trim().slice(0, 30));
}
return out;
"""

SHOWN_JS = r"""
const el = document.querySelector(arguments[0]);
if (!el) return false;
const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'
  && r.right > 0 && r.left < document.documentElement.clientWidth;
"""


def _shot(drv, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        drv.save_screenshot(str(Path(SHOTS) / name))


def _size(drv, size):
    w, h = size
    if size == DESKTOP:
        drv.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
        drv.set_window_size(w, h)
        # the window's inner width must be the size asked for
        inner = _js(drv, "return window.innerWidth;")
        if inner != w:
            drv.set_window_size(w + (w - inner), h)
        return
    drv.set_window_size(max(w, 600), h + 120)      # headless Chrome keeps windows at least 500 wide
    drv.execute_cdp_cmd("Emulation.setDeviceMetricsOverride",
                        {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": True})


def _open(drv, base, path, size=PHONE, theme="light"):
    _size(drv, size)
    drv.get(base + "/static/favicon.svg")
    _js(drv, "localStorage.setItem('gc.theme', arguments[0]); localStorage.removeItem('gc.sidebar');", theme)
    drv.get(base + path)
    assert wait_for(drv, lambda: _js(drv, "return document.readyState === 'complete' && !!window.GCMobile;"))


def _shown(drv, css):
    return _js(drv, SHOWN_JS, css)


def _no_sideways_scroll(drv):
    return _js(drv, "return document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1;")


def _path(drv):
    return _js(drv, "return location.pathname + location.search;")


def _active_id(drv):
    return _js(drv, "return document.activeElement && document.activeElement.id;")


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("mobile-ui")
    hub = build_hub(tmp)
    with booted(tmp) as (port, _proc, _data, _home):
        drv = _driver()
        try:
            browser_sign_in(drv, port)
            yield drv, f"http://127.0.0.1:{port}", hub
        finally:
            drv.quit()


def _pages(hub):
    sid = hub.ids["final"]
    return {
        "samples": "/", "sample": f"/samples/{sid}", "data": f"/samples/{sid}/data",
        "compare": f"/samples/{sid}/compare", "results": "/results", "settings": "/settings",
        "instruments": "/instruments", "instrument": "/instruments/gc1", "admin": "/admin/hub",
        "help": "/help", "calibration": "/calibration", "setup": "/setup?instrument=gc2",
    }


def test_every_page_fits_a_phone_in_both_themes(page):
    drv, base, hub = page
    for theme in THEMES:
        for name, path in _pages(hub).items():
            where = (name, theme)
            _open(drv, base, path, PHONE, theme)
            time.sleep(0.6)                                   # lists and charts drawn
            assert _js(drv, "return document.documentElement.dataset.theme;") == theme, where
            assert wait_for(drv, lambda: _no_sideways_scroll(drv)), (where, _js(
                drv, "return [document.documentElement.scrollWidth, document.documentElement.clientWidth];"))
            # the phone's frame: a way to the menu and the global status, the sidebar shut
            assert _shown(drv, "#nav-open"), where
            assert _shown(drv, "#tb-live") and _shown(drv, "#tb-bell"), where
            assert not _shown(drv, "#sidebar"), where
            assert _js(drv, "return document.getElementById('nav-open').getAttribute('aria-expanded');") == "false"
            # the live state is mirrored from the sidebar footer
            assert wait_for(drv, lambda: _js(
                drv, "return document.getElementById('tb-live-dot').dataset.state === "
                     "document.getElementById('live-dot').dataset.state;")), where
            # the badge: taps go through it anywhere; at the page's end nothing is under it
            assert _js(drv, BADGE_TAPS_THROUGH_JS) == "", where
            _js(drv, "window.scrollTo(0, document.documentElement.scrollHeight);")
            time.sleep(0.15)
            if _js(drv, "return document.documentElement.scrollHeight > window.innerHeight + 4;"):
                assert _js(drv, BADGE_OVERLAP_JS) == [], where
            _js(drv, "window.scrollTo(0, 0);")
            assert _js(drv, CONTRAST_JS) == [], where
            _shot(drv, f"mobile-{name}-390-{theme}.png")


def test_the_login_page_fits_a_phone(page):
    drv, base, _hub = page
    _size(drv, PHONE)
    drv.get(base + "/static/favicon.svg")
    cookies = drv.get_cookies()
    drv.delete_all_cookies()
    try:
        drv.get(base + "/login")
        assert wait_for(drv, lambda: _shown(drv, "#password-go"))
        assert _no_sideways_scroll(drv)
        for css in ("#card", "#username", "#password", "#password-go"):
            h = _js(drv, "return document.querySelector(arguments[0]).getBoundingClientRect().height;", css)
            assert h >= 44, (css, h)
        assert _js(drv, "return parseFloat(getComputedStyle(document.getElementById('username')).fontSize);") >= 16
        _shot(drv, "mobile-login-390.png")
    finally:
        drv.get(base + "/static/favicon.svg")
        for c in cookies:
            drv.add_cookie({"name": c["name"], "value": c["value"], "path": "/"})


def test_the_drawer_opens_traps_focus_and_closes(page):
    drv, base, _hub = page
    _open(drv, base, "/results", PHONE)
    assert click_when_ready(drv, "#nav-open")
    assert wait_for(drv, lambda: _js(drv, "return document.documentElement.classList.contains('nav-open');"))
    assert wait_for(drv, lambda: _js(drv, "return document.getElementById('sidebar').getBoundingClientRect().left;") == 0)
    assert _shown(drv, "#sidebar") and _shown(drv, "#sb-scrim")
    assert _js(drv, "return document.getElementById('nav-open').getAttribute('aria-expanded');") == "true"
    assert _js(drv, "return document.getElementById('sidebar').getAttribute('aria-modal');") == "true"
    assert _js(drv, "return document.querySelector('.main-col').inert;") is True
    assert _active_id(drv) == "sb-close"
    # the full sidebar, not the icon rail: labels and the nav
    assert _shown(drv, "#sidebar .nav-item[data-nav='results'] .sb-label")
    assert _js(drv, "return document.getElementById('sidebar').getBoundingClientRect().width;") >= 250
    _shot(drv, "mobile-drawer-open-390.png")
    # Tab stays inside: Shift+Tab from the first control lands on the last one in the drawer
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.keys import Keys
    ActionChains(drv).key_down(Keys.SHIFT).send_keys(Keys.TAB).key_up(Keys.SHIFT).perform()
    assert _js(drv, "return document.getElementById('sidebar').contains(document.activeElement);")
    assert _active_id(drv) != "sb-close"
    ActionChains(drv).send_keys(Keys.TAB).perform()
    assert _active_id(drv) == "sb-close"
    # Escape closes it and gives focus back to the menu button
    ActionChains(drv).send_keys(Keys.ESCAPE).perform()
    assert wait_for(drv, lambda: not _js(drv, "return document.documentElement.classList.contains('nav-open');"))
    assert _js(drv, "return document.getElementById('nav-open').getAttribute('aria-expanded');") == "false"
    assert _js(drv, "return document.querySelector('.main-col').inert;") is False
    assert _active_id(drv) == "nav-open"
    assert wait_for(drv, lambda: not _shown(drv, "#sidebar"))
    # the overlay closes it
    assert click_when_ready(drv, "#nav-open")
    assert wait_for(drv, lambda: _shown(drv, "#sb-scrim"))
    _js(drv, "document.getElementById('sb-scrim').dispatchEvent(new MouseEvent('click', {bubbles: true}));")
    assert wait_for(drv, lambda: not _js(drv, "return document.documentElement.classList.contains('nav-open');"))
    # the close button closes it
    assert click_when_ready(drv, "#nav-open")
    assert click_when_ready(drv, "#sb-close")
    assert wait_for(drv, lambda: not _js(drv, "return document.documentElement.classList.contains('nav-open');"))
    # the status dot opens it too (the global status lives in its footer)
    assert click_when_ready(drv, "#tb-live")
    assert wait_for(drv, lambda: _js(drv, "return document.documentElement.classList.contains('nav-open');"))
    assert wait_for(drv, lambda: _shown(drv, "#live-text"))   # once the drawer has slid in
    # a nav link goes where it says
    assert click_when_ready(drv, "#sidebar .nav-item[data-nav='instruments']")
    assert wait_for(drv, lambda: _path(drv) == "/instruments")
    assert wait_for(drv, lambda: _js(drv, "return !!window.GCMobile && !GCMobile.isOpen();"))
    # the top bar's bell opens the notifications panel as a sheet on screen; Escape shuts it
    assert click_when_ready(drv, "#tb-bell")
    assert wait_for(drv, lambda: _shown(drv, "#bell-panel"))
    box = _js(drv, "const r = document.getElementById('bell-panel').getBoundingClientRect();"
                   "return [r.left, r.right, r.bottom, document.documentElement.clientWidth, window.innerHeight];")
    assert box[0] >= 0 and box[1] <= box[3] and box[2] <= box[4], box
    assert _js(drv, "return document.getElementById('tb-bell').getAttribute('aria-expanded');") == "true"
    _shot(drv, "mobile-notifications-390.png")
    ActionChains(drv).send_keys(Keys.ESCAPE).perform()
    assert wait_for(drv, lambda: not _shown(drv, "#bell-panel"))
    # wider than a phone the drawer can't be open
    _size(drv, TABLET)
    assert wait_for(drv, lambda: not _shown(drv, "#nav-open"))
    assert _js(drv, "return document.getElementById('sidebar').getBoundingClientRect().width;") <= 72


def test_samples_list_then_detail_then_back(page):
    drv, base, hub = page
    sid = hub.ids["final"]
    _open(drv, base, "/?status=final", PHONE)
    assert wait_for(drv, lambda: _js(drv, "return !!(window.GCSamples && GCSamples.ready)"))
    assert wait_for(drv, lambda: _shown(drv, "[data-testid=sample-row]"))
    assert not _shown(drv, "#detail")
    assert not _shown(drv, "#detail-back")
    assert _shown(drv, ".topbar .tb-title")
    _shot(drv, "mobile-samples-list-390.png")
    # a row opens the sample in the one pane
    y = _js(drv, "window.scrollTo(0, 120); return window.scrollY;")
    assert click_when_ready(drv, f"[data-testid=sample-row][data-sample-id='{sid}']")
    assert wait_for(drv, lambda: _path(drv).startswith(f"/samples/{sid}"))
    assert wait_for(drv, lambda: _shown(drv, "#detail-body") and not _shown(drv, "#list-pane"))
    assert _js(drv, "return document.documentElement.classList.contains('s-detail-open');")
    assert _shown(drv, "#detail-back") and not _shown(drv, ".topbar .tb-title")
    assert _js(drv, "return window.scrollY;") == 0
    assert _no_sideways_scroll(drv)
    _shot(drv, "mobile-samples-detail-390.png")
    # the views keep the one pane
    assert click_when_ready(drv, "[data-testid=view-data]")
    assert wait_for(drv, lambda: _path(drv).startswith(f"/samples/{sid}/data"))
    assert wait_for(drv, lambda: _shown(drv, "[data-testid=data-table]"))
    assert _no_sideways_scroll(drv)
    # the data table scrolls inside its card with its first column held
    assert _js(drv, "const c = document.querySelector('#data-table td'); return c && getComputedStyle(c).position;") == "sticky"
    # back: the list again, its filter kept
    assert click_when_ready(drv, "#detail-back")
    assert wait_for(drv, lambda: _path(drv) == "/samples?status=final")
    assert wait_for(drv, lambda: _shown(drv, "#list-pane") and not _shown(drv, "#detail"))
    assert not _shown(drv, "#detail-back")
    assert wait_for(drv, lambda: _js(drv, "return window.scrollY;") == y)      # the list's place kept
    # the browser's Back returns to the sample
    drv.back()
    assert wait_for(drv, lambda: _path(drv).startswith(f"/samples/{sid}/data"))
    assert wait_for(drv, lambda: _shown(drv, "#detail-body") and not _shown(drv, "#list-pane"))
    # a sample's link opened directly: the sample first, back to the list
    _open(drv, base, f"/samples/{sid}/compare", PHONE)
    assert wait_for(drv, lambda: _shown(drv, "#detail-body") and not _shown(drv, "#list-pane"))
    assert click_when_ready(drv, "#detail-back")
    assert wait_for(drv, lambda: _path(drv) == "/samples")
    assert wait_for(drv, lambda: _shown(drv, "[data-testid=sample-row]"))


def test_tablet_keeps_the_rail_and_desktop_is_unchanged(page):
    drv, base, hub = page
    for name, path in _pages(hub).items():
        _open(drv, base, path, TABLET)
        time.sleep(0.4)
        assert wait_for(drv, lambda: _no_sideways_scroll(drv)), name
        assert not _shown(drv, "#nav-open"), name
        assert not _shown(drv, "#tb-status"), name
        assert _js(drv, "return document.getElementById('sidebar').getBoundingClientRect().width;") <= 72, name
        _shot(drv, f"mobile-{name}-768-light.png")
    # desktop: none of the phone controls, Samples with both panes side by side
    sid = hub.ids["final"]
    _open(drv, base, f"/samples/{sid}", DESKTOP)
    assert wait_for(drv, lambda: _js(drv, "return !!(window.GCSamples && GCSamples.ready)"))
    for css in ("#nav-open", "#tb-status", "#detail-back", "#sb-close", "#sb-scrim"):
        assert not _shown(drv, css), css
    assert wait_for(drv, lambda: _shown(drv, "#list-pane") and _shown(drv, "#detail-body"))
    assert _js(drv, "return getComputedStyle(document.querySelector('.topbar')).height;") == "56px"
    assert _no_sideways_scroll(drv)
