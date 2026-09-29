"""v3.1: headless-Chrome smoke of the new pages (/instruments,
/instruments/<id>, /setup), keyed on ``data-testid``, in the light and dark
themes at 1366x768 (the sidebar is an icon rail) and 1440x900 (full sidebar).
Plus: the setup guide moves on by itself when the agent checks in (the 5 s
fallback poll: this branch has no live.js), and "Add a new GC" asks for the
admin password once and lands on the new instrument's guide.

Skipped when selenium or a Chrome/chromedriver can't be started.
"""
from __future__ import annotations

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
webdriver = pytest.importorskip("selenium.webdriver")

import store  # noqa: E402
import ui_setup_demo  # noqa: E402
from bootapp import booted, browser_sign_in, setup_admin  # noqa: E402

SIZES = [(1366, 768), (1440, 900)]
THEMES = ["light", "dark"]


def _driver():
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--force-device-scale-factor=1"):
        opts.add_argument(arg)
    opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    try:
        return webdriver.Chrome(options=opts)
    except Exception as exc:  # noqa: BLE001 - no Chrome / driver here
        pytest.skip(f"headless Chrome unavailable: {exc}")


def _wait(pred, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = pred()
        except Exception:  # noqa: BLE001 - DOM not ready yet
            v = None
        if v:
            return v
        time.sleep(0.2)
    return pred()


def _js(drv, script, *args):
    return drv.execute_script(script, *args)


def _tid(drv, testid, attr="length"):
    return _js(drv, f"return document.querySelectorAll('[data-testid=\"{testid}\"]').{attr};")


def _errors(drv):
    try:
        logs = drv.get_log("browser")
    except Exception:  # noqa: BLE001 - not every driver exposes logs
        return []
    return [e["message"] for e in logs if e["level"] == "SEVERE" and "favicon.ico" not in e["message"]]


def _open(drv, port, path, theme, size):
    drv.set_window_size(*size)
    drv.get(f"http://127.0.0.1:{port}/static/favicon.svg")
    _js(drv, "localStorage.setItem('gc.theme', arguments[0]); localStorage.removeItem('gc.sidebar');", theme)
    drv.get(f"http://127.0.0.1:{port}{path}")


def _common(drv, theme, width):
    assert _js(drv, "return document.documentElement.dataset.theme;") == theme
    # no sideways scrolling, the version badge, the shell
    assert _js(drv, "return document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1;")
    assert _js(drv, "return !!document.getElementById('app-version');")
    assert _tid(drv, "sidebar") == 1 and _tid(drv, "user-chip") == 1
    sb = _js(drv, "return document.getElementById('sidebar').getBoundingClientRect().width;")
    if width < 1400:
        assert sb <= 72, sb                  # the icon rail
    else:
        assert sb >= 250, sb
    # the user chip shows the name only (no roles)
    assert _js(drv, "return document.getElementById('user-name').textContent.trim();") == "Test Operator"
    # the dark theme really changes the page's background
    bg = _js(drv, "return getComputedStyle(document.body).backgroundColor;")
    assert (bg == "rgb(255, 255, 255)") == (theme == "light"), bg


def test_the_three_pages_in_both_themes_at_both_sizes(tmp_path):
    hub = ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, _data, _home):
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            for theme in THEMES:
                for size in SIZES:
                    ui_setup_demo.touch_agent(hub.db)

                    # ── /instruments
                    _open(drv, port, "/instruments", theme, size)
                    assert _wait(lambda: _tid(drv, "instrument-card") == 2)
                    labels = _js(drv, "return Object.fromEntries(Array.from(document.querySelectorAll("
                                      "'[data-testid=instrument-card]')).map(c => [c.dataset.instrument, "
                                      "c.querySelector('[data-testid=setup-label]').textContent]));")
                    assert labels == {"gc1": "Ready", "gc2": "Step 4 of 8"}, labels
                    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#feed li').length;") > 0)
                    feed = _js(drv, "return document.getElementById('feed').textContent;")
                    assert "Written to results CSV" in feed and "LEM" not in feed
                    assert _wait(lambda: not _js(drv, "return document.getElementById('nav-setup').hidden;"))
                    assert _js(drv, "return document.getElementById('nav-setup-step').textContent;") == "Step 4"
                    assert _tid(drv, "add-gc") == 1
                    live = _js(drv, "return document.querySelector('[data-instrument=gc1] [data-role=agent-pill]').textContent;")
                    assert live == "Live"
                    _common(drv, theme, size[0])

                    # ── /instruments/gc2
                    _open(drv, port, "/instruments/gc2", theme, size)
                    assert _wait(lambda: _tid(drv, "checklist-step") == 8)
                    statuses = _js(drv, "return Array.from(document.querySelectorAll("
                                        "'[data-testid=checklist-step]')).map(t => t.dataset.status);")
                    assert statuses.count("current") == 1 and statuses[3] == "current", statuses
                    for sec in ("agent", "calibration", "corrections", "export", "methods", "backfill",
                                "conflicts"):
                        assert _tid(drv, "section-" + sec) == 1, sec
                    assert _wait(lambda: "Waiting for the first check-in" in _js(
                        drv, "return document.getElementById('agent-body').textContent;"))
                    assert _js(drv, "return document.querySelectorAll('#corrections-body input').length;") == 12
                    _common(drv, theme, size[0])

                    # ── /setup
                    _open(drv, port, "/setup?instrument=gc2", theme, size)
                    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#steps > li').length;") == 8)
                    assert _js(drv, "return document.querySelector('[data-testid=step-checkin]').dataset.status;") == "current"
                    assert _js(drv, "return document.querySelector('[data-testid=guide-step]').textContent;") == "Step 4 of 8"
                    assert _js(drv, "return document.getElementById('picker').value;") == "gc2"
                    _common(drv, theme, size[0])
            assert _errors(drv) == []
        finally:
            drv.quit()


def test_the_guide_moves_on_when_the_agent_checks_in_and_add_a_gc(tmp_path):
    hub = ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            _open(drv, port, "/setup?instrument=gc2", "light", (1440, 900))
            assert _wait(lambda: _js(drv, "return document.querySelector('[data-testid=step-checkin]')"
                                          ".dataset.status;") == "current")
            ui_setup_demo.touch_agent(hub.db, "gc2")               # the agent says hello
            assert _wait(lambda: _js(drv, "return document.querySelector('[data-testid=step-checkin]')"
                                          ".dataset.status;") == "done", timeout=20)
            assert _js(drv, "return document.querySelector('[data-testid=guide-step]').textContent;") == "Step 7 of 8"

            # "Add a new GC": step 1's form, the admin password once, then its guide
            _open(drv, port, "/setup?new=1", "light", (1440, 900))
            assert _wait(lambda: _tid(drv, "new-form") == 1)
            assert _js(drv, "return document.getElementById('picker').value;") == "__new"
            name = drv.find_element("css selector", "[data-testid=new-name]")
            name.send_keys("GC 9")
            assert _js(drv, "return document.querySelector('[data-testid=new-id]').value;") == "gc9"
            _js(drv, "document.querySelector('[data-testid=new-form] button[type=submit]').click();")
            assert _wait(lambda: _js(drv, "return document.getElementById('admin-dialog').open;"))
            drv.find_element("id", "admin-password").send_keys(pw)
            _js(drv, "document.getElementById('admin-ok').click();")
            assert _wait(lambda: "instrument=gc9" in drv.current_url)
            assert _wait(lambda: _js(drv, "return document.querySelectorAll('#steps > li').length;") == 8)
            ev = store.instrument_events.list(instrument_id="gc9", db=hub.db)
            assert [e["kind"] for e in ev] == ["created"] and ev[0]["by"].startswith("Test Operator (")
            # the password is never stored by the page
            stored = _js(drv, "return JSON.stringify(localStorage) + JSON.stringify(sessionStorage);")
            assert pw not in stored
            assert _errors(drv) == []
        finally:
            drv.quit()
