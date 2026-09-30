"""v5.0: selecting many backfill runs on /instruments/<id> (headless
Chrome on a booted hub with 30 backfill runs on GC-2).

* a drag down the checkboxes sets every row it passes to the first row's new
  state, and survives a live update that lands mid-drag;
* a shift-click selects the range from the last clicked row;
* the selection survives live updates, and rows released elsewhere drop out;
* Select all (indeterminate when only some are selected), then Release, in
  chunks of the route's limit with progress (the limit is lowered in the page
  so 30 rows take three calls);
* each row says why it is backfill.

Screenshot of the section with rows selected: ``GC_UI_SHOTS`` when set.
Skipped when selenium or a Chrome/chromedriver can't be started.
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
webdriver = pytest.importorskip("selenium.webdriver")

import store  # noqa: E402
import ui_setup_demo  # noqa: E402
from bootapp import booted, browser_sign_in, post, setup_admin  # noqa: E402
from test_ui_setup_pages_smoke import _driver, _errors, _js, _open  # noqa: E402
from ui_wait import wait_for  # noqa: E402

N = 30
LIVE_SINCE = "2026-09-30 14:30:00"
POST_SPY = """
window.__posts = [];
window.__lists = 0;
const __f = window.fetch;
window.fetch = function (url, opts) {
  const o = opts || {};
  if (o.method === 'POST' && String(url).includes('/backfill/release')) {
    window.__posts.push(JSON.parse(o.body).sample_ids.length);
  }
  if ((!o.method || o.method === 'GET') && String(url).includes('/backfill?')) window.__lists++;
  return __f.apply(this, arguments);
};
"""
PROGRESS_SPY = """
window.__progress = [];
new MutationObserver(() => {
  const p = document.querySelector('[data-testid=bf-progress]');
  const t = p && !p.hidden ? p.textContent : '';
  if (t && window.__progress[window.__progress.length - 1] !== t) window.__progress.push(t);
}).observe(document.getElementById('backfill-body'), {subtree: true, childList: true, characterData: true, attributes: true});
"""


def _backfill_runs(hub) -> list:
    """30 final, unreleased backfill runs on GC-2 (result-only, so no CDFs to
    process), injected 13:00-13:29 on the GC's clock, before live_since."""
    store.instruments.upsert({"id": "gc2", "live_since": LIVE_SINCE}, db=hub.db)
    results = store.get_revision(hub.ids["final"], db=hub.db)["results"]
    ids = []
    for i in range(N):
        sid = store.samples.insert_received("gc2", f"BF{i:03d}", f"2026-09-30 13:{i:02d}:00", "csv",
                                            cdf_sha256=None, cdf_path=None, status="final",
                                            backfill=1, received_at="2026-09-30T18:00:00+00:00",
                                            db=hub.db)
        with store.connection(hub.db) as conn:
            with store.write_txn(conn):
                store.add_revision(conn, sid, results, reason="import", by="test")
        ids.append(sid)
    return ids


def _rows(drv):
    return _js(drv, "return Array.from(document.querySelectorAll('#backfill-body tbody tr[data-id]'))"
                    ".map(r => Number(r.dataset.id));")


def _selected(drv):
    return sorted(_js(drv, "return Array.from(document.querySelectorAll("
                           "'#backfill-body tbody input[data-sel]:checked')).map(b => Number(b.dataset.id));"))


def _count(drv):
    return _js(drv, "return document.querySelector('[data-testid=bf-count]').textContent;")


def _box(drv, sid):
    return drv.find_element("css selector", f'#backfill-body input[data-sel][data-id="{sid}"]')


def _row(drv, sid):
    return drv.find_element("css selector", f'#backfill-body tr[data-id="{sid}"] td:nth-child(2)')


def _admin_change(port, pw, lem):
    """Another admin edits GC-2: a live update the page reloads on."""
    code, body = post(port, "/api/admin/instruments/gc2", {"password": pw, "lem_machine_uid": lem})
    assert code == 200, body


def test_select_many_backfill_runs_and_release_them_in_chunks(tmp_path):
    from selenium.webdriver import ActionChains
    from selenium.webdriver.common.keys import Keys

    hub = ui_setup_demo.build(tmp_path)
    ids = _backfill_runs(hub)
    listed = sorted(ids, reverse=True)           # newest injection first
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": POST_SPY})
            _open(drv, port, "/instruments/gc2", "light", (1440, 1000))
            assert wait_for(drv, lambda: len(_rows(drv)) == N)
            assert _rows(drv) == listed
            assert _count(drv) == "None selected"
            assert _js(drv, "return document.querySelector('[data-testid=bf-release]').disabled;")

            # why each row is backfill, in muted text
            why = _js(drv, f"return document.querySelector('tr[data-id=\"{listed[0]}\"] [data-testid=bf-why]').textContent;")
            assert why == "injected 13:29 · before GC-2 went live (Sep 30 14:30)", why
            drv.execute_script("document.getElementById('backfill').scrollIntoView();")

            # ── drag down the checkboxes: rows 2..6 (0-based), and a live update mid-drag
            chain = ActionChains(drv, duration=50)
            chain.move_to_element(_box(drv, listed[2])).click_and_hold()
            chain.move_to_element(_box(drv, listed[3])).move_to_element(_box(drv, listed[4])).perform()
            assert _selected(drv) == sorted(listed[2:5])
            lists = _js(drv, "return window.__lists;")
            _admin_change(port, pw, "gc2-mid-drag")
            time.sleep(4)                            # the live poll lands while the button is down
            assert _js(drv, "return window.__lists;") == lists       # held back mid-drag
            ActionChains(drv, duration=50).move_to_element(_box(drv, listed[5])) \
                .move_to_element(_box(drv, listed[6])).release().perform()
            assert _selected(drv) == sorted(listed[2:7])
            assert _count(drv) == "5 selected"
            assert _js(drv, "return document.querySelector('[data-testid=bf-release]').textContent;") == "Release 5"
            # the held-back reload ran after the drag, and kept the selection
            assert wait_for(drv, lambda: _js(drv, "return window.__lists;") > lists)
            time.sleep(1.0)
            assert _selected(drv) == sorted(listed[2:7])

            # a drag back over a selected row, starting on it, clears what it passes
            ActionChains(drv, duration=50).move_to_element(_row(drv, listed[6])).click_and_hold() \
                .move_to_element(_row(drv, listed[5])).release().perform()
            assert _selected(drv) == sorted(listed[2:5])

            # ── shift-click: the range from the last clicked row
            _box(drv, listed[10]).click()
            ActionChains(drv).key_down(Keys.SHIFT).click(_box(drv, listed[14])).key_up(Keys.SHIFT).perform()
            assert _selected(drv) == sorted(listed[2:5] + listed[10:15])
            assert _count(drv) == "8 selected"

            # ── keyboard: Space toggles the focused row, Shift+Space selects the range
            _box(drv, listed[20]).send_keys(Keys.SPACE)
            assert listed[20] in _selected(drv)
            _box(drv, listed[22]).send_keys(Keys.SHIFT, Keys.SPACE)
            assert set(listed[20:23]) <= set(_selected(drv))
            _box(drv, listed[22]).send_keys(Keys.SPACE)      # toggles it back off
            assert listed[22] not in _selected(drv)
            expected = sorted(listed[2:5] + listed[10:15] + listed[20:22])
            assert _selected(drv) == expected

            # ── a live update: another admin releases two selected runs; they drop out
            gone = [listed[2], listed[10]]
            code, body = post(port, "/api/admin/instruments/gc2/backfill/release",
                              {"password": pw, "sample_ids": gone})
            assert code == 200 and all(r["ok"] for r in body["results"]), body
            assert wait_for(drv, lambda: len(_rows(drv)) == N - 2, timeout=20)
            keep = [s for s in expected if s not in gone]
            assert wait_for(drv, lambda: _selected(drv) == keep)
            assert _count(drv) == f"{len(keep)} selected"

            if os.environ.get("GC_UI_SHOTS"):
                drv.execute_script("document.getElementById('backfill').scrollIntoView();")
                time.sleep(0.3)
                drv.find_element("id", "backfill").screenshot(
                    str(Path(os.environ["GC_UI_SHOTS"]) / "backfill-select.png"))

            # ── Select all: every row shown; indeterminate when only some
            head = drv.find_element("css selector", "[data-testid=bf-all]")
            _js(drv, "arguments[0].scrollIntoView({block: 'center'});", head)
            assert _js(drv, "return arguments[0].indeterminate;", head)
            head.click()
            left = [s for s in listed if s not in gone]
            assert _selected(drv) == sorted(left)
            assert _js(drv, "return arguments[0].checked && !arguments[0].indeterminate;", head)
            _box(drv, left[0]).click()
            assert _js(drv, "return arguments[0].indeterminate;", head)
            head.click()
            assert _count(drv) == f"{N - 2} selected"

            # ── Release, in chunks with progress (limit lowered to 10 for the test)
            _js(drv, "window.GCBackfill.RELEASE_MAX = 10; window.confirm = () => true;")
            _js(drv, PROGRESS_SPY)
            _js(drv, "document.querySelector('[data-testid=bf-release]').click();")
            assert wait_for(drv, lambda: _js(drv, "return document.getElementById('admin-dialog').open;"))
            drv.find_element("id", "admin-password").send_keys(pw)
            _js(drv, "document.getElementById('admin-ok').click();")
            assert wait_for(drv, lambda: _js(drv, "return window.__posts.length;") == 3, timeout=30)
            assert _js(drv, "return window.__posts;") == [10, 10, 8]
            assert wait_for(drv, lambda: len(_rows(drv)) == 0, timeout=30)
            progress = _js(drv, "return window.__progress;")
            assert "Releasing 0 of 28…" in progress and "Releasing 20 of 28…" in progress, progress
            assert _count(drv) == "None selected"
            assert all(store.samples.get(s, db=hub.db)["released_at"] for s in ids)
            assert _errors(drv) == []
        finally:
            drv.quit()


def test_touch_drag_selects_down_the_checkbox_column(tmp_path):
    from selenium.webdriver.common.actions import interaction
    from selenium.webdriver.common.actions.action_builder import ActionBuilder
    from selenium.webdriver.common.actions.pointer_input import PointerInput

    hub = ui_setup_demo.build(tmp_path)
    listed = sorted(_backfill_runs(hub), reverse=True)
    with booted(tmp_path) as (port, _proc, _data, _home):
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            _open(drv, port, "/instruments/gc2", "dark", (1440, 1000))
            assert wait_for(drv, lambda: len(_rows(drv)) == N)
            drv.execute_script("document.getElementById('backfill').scrollIntoView();")
            finger = PointerInput(interaction.POINTER_TOUCH, "finger")
            act = ActionBuilder(drv, mouse=finger, duration=50)
            act.pointer_action.move_to(_box(drv, listed[1])).pointer_down()
            for i in (2, 3, 4):
                act.pointer_action.move_to(_box(drv, listed[i]))
            act.pointer_action.pointer_up()
            act.perform()
            assert wait_for(drv, lambda: _selected(drv) == sorted(listed[1:5]))
            assert _count(drv) == "4 selected"
            assert _errors(drv) == []
        finally:
            drv.quit()
