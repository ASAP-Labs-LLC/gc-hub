"""v5.0 lane R in a real browser: /results, /settings, /help and the
notifications panel, keyed on ``data-testid``.

* every page, light and dark, at 1366x768 (icon rail) and 1440x900 (full
  sidebar): the shell, no sideways scrolling, nothing under the version
  badge, WCAG AA on every visible text (the open notifications panel too);
* Results: the grouped D2887 / D86 headers are /api/table's columns in their
  order (never swapped), Key points by default and All points remembered,
  filters in the address bar (a reload keeps them, Back restores), each row
  links to /samples/<id>/data, Corrected D86 changes D86 cells only, the
  40/60 tooltips, ticked rows overlay their curves, the CSV;
* Settings: flag rules save without a password; an admin section asks once
  in the shell's masked dialog (never window.prompt), then not again; a bad
  value is refused next to its field; paths are collapsed; a standard is
  renamed;
* the notifications panel updates live, dismisses one and all.

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
for _p in (TESTS.parent, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")
pytest.importorskip("selenium.webdriver")

import ui_setup_demo  # noqa: E402
from bootapp import booted, browser_sign_in, get, post, setup_admin  # noqa: E402
from test_ui_setup_pages_smoke import SIZES, THEMES, _driver, _errors, _js, _open, _wait  # noqa: E402
from test_ui_shell_admin_smoke import _frame_checks  # noqa: E402

SHOTS = os.environ.get("GC_UI_SHOTS")
KEY = ["IBP", "T10", "T50", "T90", "FBP"]


def _shot(drv, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        drv.save_screenshot(str(Path(SHOTS) / name))


def _rows(drv):
    return _js(drv, "return document.querySelectorAll('#tbody tr').length;")


def _head_cols(drv):
    return _js(drv, "return Array.from(document.querySelectorAll('#thead th[data-col]')).map(t => t.dataset.col);")


def _open_results(drv, port, path="/results", theme="light", size=(1440, 900)):
    _open(drv, port, path, theme, size)
    assert _wait(lambda: _js(drv, "return !!window.GCResultsPage && window.GCResultsPage.state.loadedOnce;"))


def test_pages_in_both_themes_at_both_sizes(tmp_path):
    ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, _data, _home):
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            _, table = get(port, "/api/table")
            columns = table["columns"]
            for theme in THEMES:
                for size in SIZES:
                    suffix = "" if theme == "light" else "-dark"
                    # ── /results
                    _open_results(drv, port, "/results", theme, size)
                    assert _rows(drv) > 0
                    groups = _js(drv, "return Array.from(document.querySelectorAll('#thead th.grp'))"
                                      ".map(t => [t.dataset.group, t.textContent, t.colSpan]);")
                    assert groups == [["d2887", "D2887 (°C)", 5], ["d86", "D86 (°C)", 5]]
                    # the headers are the table's columns, in its order: each group's cuts
                    expect = [c for c in columns if c.split(" ")[0] in ("2887", "D86")
                              and c.split(" ")[1] in KEY]
                    assert _head_cols(drv) == expect
                    assert _js(drv, "return document.querySelector('.nav-item[data-nav=results]')"
                                    ".getAttribute('aria-current');") == "page"
                    time.sleep(0.3)
                    _frame_checks(drv, theme, size[0], "results")
                    if size == (1440, 900):
                        _shot(drv, f"results{suffix}.png")
                    # ── /settings
                    _open(drv, port, "/settings", theme, size)
                    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#rules .rule').length;") >= 1)
                    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#fields-bestfit [data-key]')"
                                                  ".length;") == 4)
                    time.sleep(0.5)
                    _frame_checks(drv, theme, size[0], "settings")
                    if size == (1440, 900):
                        _shot(drv, f"settings{suffix}.png")
                    # ── /help
                    _open(drv, port, "/help", theme, size)
                    assert _wait(lambda: _js(drv, "return !!document.querySelector('[data-testid=help-page] h2');"))
                    _frame_checks(drv, theme, size[0], "help")
                    if size == (1440, 900):
                        _shot(drv, f"help{suffix}.png")
                    # ── the notifications panel, open
                    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#bell-list li').length;") >= 1)
                    _js(drv, "document.getElementById('bell').click();")
                    assert _wait(lambda: not _js(drv, "return document.getElementById('bell-panel').hidden;"))
                    time.sleep(0.3)
                    _frame_checks(drv, theme, size[0], "notifications")
                    if size == (1440, 900):
                        _shot(drv, f"notifications{suffix}.png")
            assert _errors(drv) == []
        finally:
            drv.quit()


def test_results_table_filters_overlay_and_csv(tmp_path):
    ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, _data, _home):
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            _open_results(drv, port)
            _js(drv, "localStorage.removeItem('gc.results.points'); localStorage.setItem('gc.correctedD86', '1');")
            _open_results(drv, port)
            assert len(_head_cols(drv)) == 10                                    # key points
            # All points: 13 per method, remembered across a reload
            _js(drv, "document.querySelector('[data-points=all]').click();")
            assert _wait(lambda: len(_head_cols(drv)) == 26)
            _open_results(drv, port)
            assert len(_head_cols(drv)) == 26
            assert _js(drv, "return document.querySelector('[data-points=all]').getAttribute('aria-checked');") == "true"
            # the 40/60 cells say what they are
            t40 = _js(drv, "const i = Array.from(document.querySelectorAll('#thead th[data-col]'))"
                           ".findIndex(t => t.dataset.col === 'D86 T40');"
                           "const tr = document.querySelector('#tbody tr:not(.no-result)');"
                           "return tr.querySelectorAll('td')[5 + i].title;")
            assert t40.startswith("Midpoint of the uncorrected 30/50 values")

            # Corrected D86 off: D86 cells change, D2887 cells never do
            def cells():
                return _js(drv, "const tr = document.querySelector('#tbody tr[data-sample-id]:not(.no-result)');"
                                "return Array.from(tr.querySelectorAll('td.num')).map(t => t.textContent);")
            before = cells()
            _js(drv, "document.getElementById('corrected').click();")
            assert _wait(lambda: cells() != before)
            after = cells()
            assert after[:13] == before[:13] and after[13:] != before[13:]
            assert _js(drv, "return localStorage.getItem('gc.correctedD86');") == "0"

            # rows link to their data view
            href = _js(drv, "return document.querySelector('#tbody a.lab').getAttribute('href');")
            assert href.startswith("/samples/") and href.endswith("/data")

            # filters live in the address bar; a reload keeps them; Back restores
            all_rows = _rows(drv)
            _js(drv, "document.querySelector('#f-inst [data-inst=gc2]').click();")
            assert _wait(lambda: "instrument=gc2" in drv.current_url)
            assert _wait(lambda: _rows(drv) < all_rows)
            gcs = _js(drv, "return Array.from(document.querySelectorAll('#tbody .c-gc')).map(t => t.textContent);")
            assert gcs and set(gcs) == {"GC-2"}
            _open_results(drv, port, "/results?instrument=gc2&status=held")
            assert _js(drv, "return document.getElementById('f-status').value;") == "held"
            assert _js(drv, "return document.querySelector('#f-inst [data-inst=gc2]').getAttribute('aria-pressed');") == "true"
            _js(drv, "document.querySelector('#f-inst [data-inst=\"\"]').click();")
            assert _wait(lambda: "instrument=" not in drv.current_url)
            drv.back()
            assert _wait(lambda: "instrument=gc2" in drv.current_url)
            assert _wait(lambda: _js(drv, "return document.querySelector('#f-inst [data-inst=gc2]')"
                                          ".getAttribute('aria-pressed');") == "true")
            _open_results(drv, port)

            # tick two runs: their curves on one chart
            boxes = drv.find_elements("css selector", "#tbody input[type=checkbox]:not([disabled])")
            boxes[0].click()
            boxes[1].click()
            assert _wait(lambda: _js(drv, "return document.querySelectorAll('#picked li').length;") == 2)
            assert not _js(drv, "return document.getElementById('overlay').hidden;")
            assert _wait(lambda: _js(drv, "const c = document.getElementById('chart');"
                                          "return (c.data && c.data.length) || (window.Plotly ? 0 : -1);") in (2, -1))
            # the chart follows the theme
            if _js(drv, "return !!window.Plotly;"):
                ink = _js(drv, "return document.getElementById('chart').data[0].line.color;")
                _js(drv, "localStorage.setItem('gc.theme', 'dark');"
                         "if (window.GCTheme) GCTheme.set('dark'); else document.documentElement.setAttribute('data-theme', 'dark');")
                assert _wait(lambda: _js(drv, "return document.getElementById('chart').data[0].line.color;") != ink)
                _js(drv, "localStorage.setItem('gc.theme', 'light');"
                         "if (window.GCTheme) GCTheme.set('light'); else document.documentElement.setAttribute('data-theme', 'light');")
            _js(drv, "document.getElementById('overlay-clear').click();")
            assert _wait(lambda: _js(drv, "return document.getElementById('overlay').hidden;"))

            # the CSV: every column, the rows shown
            _js(drv, "window.__csv = null; const o = URL.createObjectURL;"
                     "URL.createObjectURL = (b) => { b.text().then(t => { window.__csv = t; }); return o(b); };"
                     "HTMLAnchorElement.prototype.click = function () {};")
            _js(drv, "document.getElementById('csv').click();")
            assert _wait(lambda: _js(drv, "return window.__csv;"))
            lines = _js(drv, "return window.__csv;").lstrip("﻿").strip().split("\r\n")
            assert lines[0].startswith("Lab ID,GC,InjectionDateTime,Status,2887 IBP,")
            assert len(lines) == _rows(drv) + 1
            assert _errors(drv) == []
        finally:
            drv.quit()


def test_settings_save_with_one_unlock_never_prompt(tmp_path):
    ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            _open(drv, port, "/settings", "light", (1440, 900))
            assert _wait(lambda: _js(drv, "return document.querySelectorAll('#fields-bestfit [data-key]').length;") == 4)
            _js(drv, "window.__prompts = 0; window.prompt = () => { window.__prompts++; return null; };")
            assert _js(drv, "return document.getElementById('paths').open;") is False
            paths = _js(drv, "return document.getElementById('path-list').textContent;")
            assert "Report PDFs" in paths and "Watch" not in paths

            # flag rules: anyone, no password
            _js(drv, "const t = document.querySelector('#rules .rule .r-thr'); t.value = '7600';")
            drv.find_element("id", "save-flags").click()
            assert _wait(lambda: drv.find_element("id", "flags-msg").text.startswith("Saved"))
            _, conf = get(port, "/api/settings")
            assert json.loads(conf["sample_flag_rules"])[0]["threshold"] == 7600
            assert not _js(drv, "return document.getElementById('admin-dialog').open;")

            # a bad value is refused next to its field, nothing sent
            _js(drv, "document.getElementById('set-bestfit_threshold').value = '3';")
            drv.find_element("css selector", "[data-save=bestfit]").click()
            assert _wait(lambda: drv.find_element("id", "set-bestfit_threshold-err").text == "Between 0 and 1.")
            assert not _js(drv, "return document.getElementById('admin-dialog').open;")

            # an admin section: the shell's masked dialog, once
            _js(drv, "document.getElementById('set-bestfit_threshold').value = '0.91';")
            drv.find_element("css selector", "[data-save=bestfit]").click()
            assert _wait(lambda: _js(drv, "return document.getElementById('admin-dialog').open;"))
            drv.find_element("id", "admin-password").send_keys(pw)
            _js(drv, "document.getElementById('admin-ok').click();")
            assert _wait(lambda: drv.find_element("id", "bestfit-msg").text == "Saved.", timeout=30)
            assert get(port, "/api/settings")[1]["bestfit_threshold"] == "0.91"
            assert _wait(lambda: _js(drv, "return !document.getElementById('unlock-chip').hidden;"))
            time.sleep(1.1)
            _js(drv, "document.getElementById('set-analysis_min_width_min').value = '0.06';")
            drv.find_element("css selector", "[data-save=findings]").click()
            assert _wait(lambda: drv.find_element("id", "findings-msg").text == "Saved.", timeout=30)
            assert not _js(drv, "return document.getElementById('admin-dialog').open;")
            assert get(port, "/api/settings")[1]["analysis_min_width_min"] == "0.06"

            # a standard: rename, with the same unlock
            assert _wait(lambda: _js(drv, "return document.querySelectorAll('[data-testid=standard-row]').length;") >= 1)
            name = _js(drv, "return document.querySelector('[data-testid=standard-row] b').textContent;")
            _js(drv, "Array.from(document.querySelectorAll('[data-testid=standard-row] button'))"
                     ".find(b => b.textContent === 'Rename').click();")
            field = drv.find_element("css selector", "#std-rows tr.inline input")
            field.clear()
            field.send_keys(name + " B")
            _js(drv, "Array.from(document.querySelectorAll('#std-rows tr.inline button'))"
                     ".find(b => b.textContent === 'Rename').click();")
            assert _wait(lambda: name + " B" in _js(drv, "return document.getElementById('std-rows').textContent;"), timeout=30)
            assert not _js(drv, "return document.getElementById('admin-dialog').open;")
            assert _js(drv, "return window.__prompts;") == 0
            assert _errors(drv) == []
        finally:
            drv.quit()


def test_the_notifications_panel_is_live(tmp_path):
    ui_setup_demo.build(tmp_path)
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            _open(drv, port, "/help", "light", (1440, 900))
            assert _wait(lambda: _js(drv, "return document.querySelectorAll('#bell-list li').length;") >= 1)
            n0 = _js(drv, "return document.querySelectorAll('#bell-list li').length;")
            _js(drv, "document.getElementById('bell').click();")
            assert _js(drv, "return document.getElementById('bell-title').textContent;") == f"Notifications · {n0}"
            # a new one arrives through GCLive, no reload
            code, _ = post(port, "/api/admin/hub/pause-processing", {"password": pw})
            assert code == 202
            assert _wait(lambda: "paused" in _js(drv, "return document.getElementById('bell-list').textContent;"), timeout=30)
            paused = _js(drv, "return Array.from(document.querySelectorAll('#bell-list li'))"
                              ".find(li => li.textContent.includes('paused'));")
            assert paused.find_element("css selector", ".note-go").get_attribute("href").endswith("/admin/hub#status")
            assert paused.find_element("css selector", ".note-level").text == "Warning"
            _shot(drv, "notifications-live.png")
            # dismiss one, then all
            count = _js(drv, "return document.querySelectorAll('#bell-list li').length;")
            paused.find_element("css selector", ".note-dismiss").click()
            assert _wait(lambda: _js(drv, "return document.querySelectorAll('#bell-list li').length;") == count - 1)
            assert _wait(lambda: len(get(port, "/api/notifications")[1]) == count - 1)
            assert not _js(drv, "return document.getElementById('bell-panel').hidden;")    # stays open
            drv.find_element("id", "bell-clear").click()
            assert _wait(lambda: not _js(drv, "return document.getElementById('bell-empty').hidden;"))
            assert _js(drv, "return document.getElementById('bell-count').hidden;") is True
            assert _wait(lambda: get(port, "/api/notifications")[1] == [])
            post(port, "/api/admin/hub/resume-processing", {"password": pw})
            assert _errors(drv) == []
        finally:
            drv.quit()
