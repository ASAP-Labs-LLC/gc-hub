"""v5.0: System / Light / Dark on the shell pages (headless Chrome, with
``prefers-color-scheme`` emulated through CDP ``Emulation.setEmulatedMedia``).

* Nobody has chosen: System, which follows the OS, and switches live when
  the OS does (no reload), and the theme is on <html> before <body> exists
  (so the first paint is already right).
* Picking Light or Dark stops following the OS; picking System again follows.
* The choice is kept in localStorage (``gc.theme``).
* Calibration's Plotly chart re-themes when the mode changes.

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
webdriver = pytest.importorskip("selenium.webdriver")

import ui_setup_demo  # noqa: E402
from bootapp import booted, browser_sign_in  # noqa: E402
from test_ui_setup_pages_smoke import _contrast, _driver, _errors, _js  # noqa: E402
from ui_wait import wait_for  # noqa: E402

AT_BODY = """
window.__themeAtBody = 'no body yet';
new MutationObserver((_m, o) => {
  if (document.body) { window.__themeAtBody = document.documentElement.getAttribute('data-theme'); o.disconnect(); }
}).observe(document, {childList: true, subtree: true});
"""


def _os(drv, scheme):
    drv.execute_cdp_cmd("Emulation.setEmulatedMedia",
                        {"features": [{"name": "prefers-color-scheme", "value": scheme}]})


def _theme(drv):
    return _js(drv, "return document.documentElement.dataset.theme;")


def _bg(drv):
    return _js(drv, "return getComputedStyle(document.body).backgroundColor;")


def _pick(drv, choice):
    _js(drv, "document.getElementById('user-chip').click();")
    _js(drv, f"document.querySelector('[data-theme-choice={choice}]').click();")
    _js(drv, "document.body.click();")


def _checked(drv):
    _js(drv, "document.getElementById('user-chip').click();")
    out = _js(drv, "return Array.from(document.querySelectorAll('[data-theme-choice]'))"
                   ".filter(b => b.getAttribute('aria-checked') === 'true').map(b => b.dataset.themeChoice);")
    _js(drv, "document.body.click();")
    return out


def test_system_follows_the_os_until_light_or_dark_is_picked(tmp_path):
    ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, _data, _home):
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            drv.set_window_size(1440, 900)
            drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": AT_BODY})
            _js(drv, "localStorage.removeItem('gc.theme');")          # nobody has chosen
            _os(drv, "dark")
            drv.get(f"http://127.0.0.1:{port}/instruments")
            assert wait_for(drv, lambda: _js(drv, "return document.readyState;") == "complete")
            assert _theme(drv) == "dark"
            assert _js(drv, "return window.__themeAtBody;") == "dark"   # before the first paint
            assert _bg(drv) != "rgb(255, 255, 255)"
            # the menu: System, Light, Dark, with System checked
            labels = _js(drv, "return Array.from(document.querySelectorAll('[data-theme-choice]'))"
                              ".map(b => b.textContent.trim());")
            assert labels == ["System", "Light", "Dark"], labels
            assert _checked(drv) == ["system"]

            # the OS switches: the page follows at once, without a reload
            _js(drv, "window.__same = true;")
            _os(drv, "light")
            assert wait_for(drv, lambda: _theme(drv) == "light", timeout=5)
            assert _bg(drv) == "rgb(255, 255, 255)"
            _os(drv, "dark")
            assert wait_for(drv, lambda: _theme(drv) == "dark", timeout=5)
            assert _js(drv, "return window.__same === true;")
            assert _contrast(drv) == []

            # Light stops following the OS
            _pick(drv, "light")
            assert _theme(drv) == "light"
            assert _js(drv, "return localStorage.getItem('gc.theme');") == "light"
            _os(drv, "light")
            _os(drv, "dark")
            assert _theme(drv) == "light"
            assert _checked(drv) == ["light"]

            # Dark too, across a page load
            _pick(drv, "dark")
            _os(drv, "light")
            drv.get(f"http://127.0.0.1:{port}/instruments/gc2")
            assert wait_for(drv, lambda: _js(drv, "return document.readyState;") == "complete")
            assert _theme(drv) == "dark" and _js(drv, "return window.__themeAtBody;") == "dark"
            assert _contrast(drv) == []

            # System follows again
            _pick(drv, "system")
            assert _theme(drv) == "light"
            _os(drv, "dark")
            assert wait_for(drv, lambda: _theme(drv) == "dark", timeout=5)
            assert _js(drv, "return localStorage.getItem('gc.theme');") == "system"

            # Hub admin and the setup guide follow the same choice
            for path in ("/admin/hub", "/setup?instrument=gc2"):
                drv.get(f"http://127.0.0.1:{port}{path}")
                assert wait_for(drv, lambda: _js(drv, "return document.readyState;") == "complete")
                assert _theme(drv) == "dark", path
                _os(drv, "light")
                assert wait_for(drv, lambda: _theme(drv) == "light", timeout=5), path
                _os(drv, "dark")
            assert _errors(drv) == []
        finally:
            drv.quit()


def test_calibration_chart_rethemes_when_the_mode_changes(tmp_path):
    ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, _data, _home):
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            drv.set_window_size(1440, 900)
            _js(drv, "localStorage.removeItem('gc.theme');")
            _os(drv, "light")
            drv.get(f"http://127.0.0.1:{port}/calibration?instrument=gc2")
            paper = "return (document.getElementById('chart')._fullLayout || {}).paper_bgcolor;"
            assert wait_for(drv, lambda: _js(drv, paper), timeout=30)
            light = _js(drv, paper)
            assert light in ("#fff", "#ffffff", "rgb(255, 255, 255)"), light
            _os(drv, "dark")                                    # System: follows the OS
            assert wait_for(drv, lambda: _js(drv, paper) not in (light,), timeout=5)
            assert _js(drv, paper) in ("#141414", "rgb(20, 20, 20)"), _js(drv, paper)
            _pick(drv, "light")                                 # a pick re-themes it too
            assert wait_for(drv, lambda: _js(drv, paper) == light, timeout=5)
            assert _errors(drv) == []
        finally:
            drv.quit()
