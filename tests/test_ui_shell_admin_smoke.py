"""v4.0 lane E2 in a real browser: /admin/hub and /calibration inside the shell.

* both pages, light and dark, at 1366x768 (icon rail) and 1440x900 (full
  sidebar): the sidebar and its mark home, no sideways scrolling, the version
  badge overlaps nothing (at the top and scrolled to the bottom), WCAG AA on
  every visible text (the unlocked admin page too);
* Hub admin: the sub-nav lists the sections in Ryan's order and follows the
  scroll; a task's Open link (#purge) lands on its section;
* a sample admin flow: unlock once at the top, then a dry run shows its
  progress in its own section and in the sidebar's running-now row;
* Calibration: Save asks for the admin password once in the shell's dialog
  (no password box on the page), saves, and a second Save needs no password.

Skipped when selenium or a Chrome/chromedriver can't be started.
Screenshots go to ``GC_UI_SHOTS`` when it is set.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden", TESTS / "import_history"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")
pytest.importorskip("selenium.webdriver")

import store  # noqa: E402
import ui_setup_demo  # noqa: E402
from bootapp import booted, browser_sign_in, setup_admin  # noqa: E402
from test_ui_setup_pages_smoke import CONTRAST_JS, SIZES, THEMES, _driver, _errors, _js, _open, _wait  # noqa: E402

ORDER = ["status", "import-history", "load-folder", "purge-panel", "exports", "diagnostics",
         "presets-panel", "sessions", "server", "hub-address"]
SHOTS = os.environ.get("GC_UI_SHOTS")

# Every visible leaf (text, control, image) whose box meets the version badge's.
BADGE_OVERLAP_JS = r"""
const b = document.getElementById('app-version');
if (!b) return ['no badge'];
const br = b.getBoundingClientRect();
if (!br.width) return ['badge not shown'];
const out = [];
for (const el of document.body.querySelectorAll('*')) {
  if (el === b || el.contains(b) || b.contains(el)) continue;
  const cs = getComputedStyle(el);
  if (cs.visibility === 'hidden' || cs.display === 'none' || el.closest('[hidden]')) continue;
  const leaf = ['INPUT','SELECT','BUTTON','TEXTAREA','IMG','SVG','CANVAS','A'].includes(el.tagName.toUpperCase())
    || Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
  if (!leaf) continue;
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) continue;
  if (r.left < br.right && r.right > br.left && r.top < br.bottom && r.bottom > br.top)
    out.push(el.tagName + '#' + el.id + '.' + el.className + ' ' + (el.textContent || '').trim().slice(0, 30));
}
return out;
"""


def _frame_checks(drv, theme, width, where):
    assert _js(drv, "return document.documentElement.dataset.theme;") == theme
    assert _js(drv, "return document.documentElement.scrollWidth <= "
                    "document.documentElement.clientWidth + 1;"), (where, theme, width)
    # the shell: sidebar, its mark home, the running-now footer slot
    assert _js(drv, "return document.querySelectorAll('#sidebar').length;") == 1
    assert _js(drv, "return document.querySelector('#sidebar .sb-mark').getAttribute('href');") == "/"
    assert _js(drv, "return !!document.getElementById('running-now') && "
                    "!!document.getElementById('gc-summary');")
    sb = _js(drv, "return document.getElementById('sidebar').getBoundingClientRect().width;")
    assert (sb <= 72) if width < 1400 else (sb >= 250), (where, sb)
    bg = _js(drv, "return getComputedStyle(document.body).backgroundColor;")
    assert (bg == "rgb(255, 255, 255)") == (theme == "light"), bg
    for pos in ("top", "bottom"):
        _js(drv, "window.scrollTo(0, 0);" if pos == "top"
            else "window.scrollTo(0, document.documentElement.scrollHeight);")
        time.sleep(0.15)
        assert _js(drv, BADGE_OVERLAP_JS) == [], (where, theme, width, pos)
    _js(drv, "window.scrollTo(0, 0);")
    assert _js(drv, CONTRAST_JS) == [], (where, theme, width)


def _shot(drv, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        drv.save_screenshot(str(Path(SHOTS) / name))


def test_admin_and_calibration_in_the_shell_both_themes_both_sizes(tmp_path):
    ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            for theme in THEMES:
                for size in SIZES:
                    # ── /admin/hub, locked
                    _open(drv, port, "/admin/hub", theme, size)
                    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#lf-inst option')"
                                                  ".length;") >= 2)
                    assert _js(drv, "return document.querySelector('.nav-item[data-nav=admin]')"
                                    ".getAttribute('aria-current');") == "page"
                    links = _js(drv, "return Array.from(document.querySelectorAll('.adm-nav a'))"
                                     ".map(a => a.getAttribute('href').slice(1));")
                    assert links == ORDER
                    secs = _js(drv, "return Array.from(document.querySelectorAll('section.adm-sec'))"
                                    ".map(s => s.id);")
                    assert secs == ORDER
                    assert _js(drv, "return document.querySelector('.adm-nav a.active')"
                                    ".getAttribute('href');") == "#status"
                    assert _wait(lambda: "Version" in _js(
                        drv, "return document.getElementById('server-facts').textContent;"))
                    # the one unlock is at the top; the shell's own chip is not shown twice
                    assert _js(drv, "return document.getElementById('unlock-chip');") is None
                    assert drv.find_element("id", "pw").is_displayed()
                    _frame_checks(drv, theme, size[0], "admin")
                    if size == (1440, 900):
                        _shot(drv, f"admin-shell{'' if theme == 'light' else '-dark'}.png")

                    # ── /calibration
                    _open(drv, port, "/calibration?instrument=gc2", theme, size)
                    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#rows tr select')"
                                                  ".length;") > 3)
                    assert _js(drv, "return document.querySelector('.nav-item[data-nav=instruments]')"
                                    ".getAttribute('aria-current');") == "page"
                    assert _js(drv, "return document.querySelectorAll('input[type=password]').length;") == 1
                    assert _js(drv, "return document.getElementById('admin-password').closest('dialog') "
                                    "!== null;")         # only the shell's dialog field
                    time.sleep(0.5)
                    _frame_checks(drv, theme, size[0], "calibration")
                    if size == (1440, 900):
                        _shot(drv, f"calibration-shell{'' if theme == 'light' else '-dark'}.png")

            # a task's Open link lands on its section, and the sub-nav follows
            _open(drv, port, "/admin/hub#purge", "light", (1440, 900))
            assert _wait(lambda: _js(drv, "return document.querySelector('.adm-nav a.active')"
                                          ".getAttribute('href');") == "#purge-panel")
            top = _js(drv, "return document.getElementById('purge-panel').getBoundingClientRect().top;")
            assert 0 <= top < 140, top
            _js(drv, "document.querySelector('.adm-nav a[href=\"#hub-address\"]').click();")
            assert _wait(lambda: _js(drv, "return document.querySelector('.adm-nav a.active')"
                                          ".getAttribute('href');") == "#hub-address")

            # unlocked: everything loaded, still AA and no overlap, in both themes
            for theme in THEMES:
                _open(drv, port, "/admin/hub", theme, (1440, 900))
                assert _wait(lambda: _js(drv, "return !!document.getElementById('pw');"))
                if _js(drv, "return document.getElementById('unlocked').hidden;"):
                    drv.find_element("id", "pw").send_keys(pw)
                    drv.find_element("id", "btn-unlock").click()
                assert _wait(lambda: _js(drv, "return !document.getElementById('unlocked').hidden;"))
                assert _wait(lambda: _js(drv, "return document.querySelectorAll('#sessions-rows tr').length "
                                              "> 0 && document.querySelectorAll('#presets li').length > 0 && "
                                              "document.querySelectorAll('#exports-rows tr').length > 0;"),
                             timeout=30)
                time.sleep(0.5)
                _frame_checks(drv, theme, 1440, "admin unlocked")
                if theme == "light":
                    _shot(drv, "admin-shell-unlocked.png")
                time.sleep(1.1)                       # one password check at a time
            assert _errors(drv) == []
        finally:
            drv.quit()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO to hold the job")
def test_unlock_then_a_dry_run_shows_its_progress(tmp_path):
    from hub_boot import build_hub
    from import_history_testlib import SIMDIS, sample
    from test_ui_status_smoke import Stall

    build_hub(tmp_path)
    processed = tmp_path / "v1" / "processed"
    sample(processed, "PG-1", datetime(2026, 9, 19, 10, 0, 0), method=SIMDIS)
    stall = Stall(processed)
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            _open(drv, port, "/admin/hub#import-history", "light", (1440, 900))
            assert _wait(lambda: _js(drv, "return document.querySelectorAll('#ih-inst option').length;") >= 1)
            drv.find_element("id", "pw").send_keys(pw)
            drv.find_element("id", "btn-unlock").click()
            assert _wait(lambda: _js(drv, "return !document.getElementById('unlocked').hidden;"))
            assert _wait(lambda: drv.find_element("id", "ih-last").text != "", timeout=30)
            # the shell's gate is the same unlock: its adminPost needs no second password
            assert _js(drv, "return window.GCAdminUnlock.page.isUnlocked();") is True

            _js(drv, "document.getElementById('ih-inst').value = 'gc1';")
            drv.find_element("id", "ih-processed").clear()
            drv.find_element("id", "ih-processed").send_keys(str(processed))
            drv.find_element("id", "btn-ih-dryrun").click()
            ih = lambda: drv.find_element("id", "ih-job").text  # noqa: E731
            # running: its progress in its own section, Stop enabled, and the sidebar row
            assert _wait(lambda: "Scanning the folder" in ih() or "of" in ih(), timeout=30), ih()
            assert _wait(lambda: _js(drv, "return !document.getElementById('btn-ih-stop').disabled;"))
            assert _wait(lambda: "dry run" in _js(
                drv, "const b = document.getElementById('running-now');"
                     "return b.hidden ? '' : b.textContent;").lower(), timeout=30)
            assert drv.find_element("id", "lf-job").text == ""
            _shot(drv, "admin-shell-dryrun.png")

            stall.release()
            assert _wait(lambda: "Dry run finished at " in ih(), timeout=60), ih()
            assert "{" not in ih() and "CDF files" in ih()
            assert _wait(lambda: _js(drv, "return document.getElementById('btn-ih-stop').disabled;"))
            assert _errors(drv) == []
        finally:
            stall.close()
            drv.quit()


def test_calibration_save_uses_the_one_unlock(tmp_path):
    hub = ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            _open(drv, port, "/calibration?instrument=gc2", "light", (1440, 900))
            assert _wait(lambda: _js(drv, "return document.querySelectorAll('#rows tr select').length;") > 3)
            saved = lambda: len([e for e in store.instrument_events.list(instrument_id="gc2", db=hub.db)  # noqa: E731
                                 if e["kind"] == "calibration_saved"])
            before = saved()
            # Save the loaded assignments: the shell's dialog asks once
            drv.find_element("id", "btn-save").click()
            assert _wait(lambda: _js(drv, "return document.getElementById('admin-dialog').open;"))
            drv.find_element("id", "admin-password").send_keys(pw)
            _js(drv, "document.getElementById('admin-ok').click();")
            status = lambda: drv.find_element("id", "status").text  # noqa: E731
            assert _wait(lambda: status().startswith("Saved"), timeout=30), status()
            assert _js(drv, "return window.GCAdminUnlock.page.isUnlocked();") is True
            # the top bar's chip shows the unlock; the second Save asks nothing
            assert _wait(lambda: _js(drv, "return !document.getElementById('unlock-chip').hidden;"))
            _js(drv, "document.getElementById('status').textContent = '';")
            drv.find_element("id", "btn-save").click()
            assert _wait(lambda: status().startswith("Saved"), timeout=30), status()
            assert _js(drv, "return document.getElementById('admin-dialog').open;") is False
            assert _wait(lambda: saved() == before + 2), (before, saved())
            # the password never reaches storage
            stored = _js(drv, "return JSON.stringify(localStorage) + JSON.stringify(sessionStorage);")
            assert pw not in stored
            assert _errors(drv) == []
        finally:
            drv.quit()
