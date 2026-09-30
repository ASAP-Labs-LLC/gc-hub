"""v4.0 lane E in a real browser, on the classic main page and Hub admin:

* the "running now" indicator follows a history dry run started elsewhere:
  it appears with its progress, then its outcome, without a reload;
* the GC strip changes on a heartbeat, without a reload;
* the sample list: each row's injection time, day headings, "No injection
  time" last, the Newest run / Lab ID switch (remembered), full lab IDs;
* Hub admin: unlock once and everything loads; Load CDFs from a folder can
  start (its instrument list is filled) and renders in its own card; the way
  home is visible.

Skipped when selenium or a Chrome/chromedriver can't be started. Plotly is
stubbed before the page loads.
"""
from __future__ import annotations

import os
import sys
import threading
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

import ingest_api  # noqa: E402
import store  # noqa: E402
from bootapp import browser_sign_in, booted, post, setup_admin  # noqa: E402
from hub_boot import build_hub  # noqa: E402
from import_history_testlib import SIMDIS, sample  # noqa: E402
from test_ui_live_smoke import _driver, _heartbeat, _wait  # noqa: E402


def _js(drv, script):
    return drv.execute_script(script)


def _indicator(drv) -> str:
    return _js(drv, "const b = document.getElementById('running-now');"
                    "return b.hidden ? '' : b.textContent;")


class Stall:
    """A FIFO named like a CDF: the dry run blocks reading it until
    ``release()`` feeds it (a deterministic "still running")."""

    def __init__(self, folder: Path):
        self.path = folder / "zz_STALL.CDF"
        os.mkfifo(self.path)
        self._stop = threading.Event()

    def release(self, timeout=20.0):
        deadline = time.time() + timeout

        def feed():
            while not self._stop.is_set() and time.time() < deadline:
                try:
                    fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
                except OSError:
                    time.sleep(0.02)
                    continue
                try:
                    os.write(fd, b"not a netCDF file" * 8)
                except OSError:
                    pass
                os.close(fd)
                time.sleep(0.05)

        threading.Thread(target=feed, daemon=True).start()

    def close(self):
        self._stop.set()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO to hold the job")
def test_running_now_follows_a_dry_run_then_shows_its_outcome(tmp_path):
    hub = build_hub(tmp_path)
    processed = tmp_path / "v1" / "processed"
    sample(processed, "PG-1", datetime(2026, 9, 19, 10, 0, 0), method=SIMDIS)
    stall = Stall(processed)
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            drv.get(f"http://127.0.0.1:{port}/classic")
            assert _wait(lambda: _js(drv, "return document.querySelectorAll("
                                          "'#dash-file-list li[data-uid]').length") > 0)
            assert _indicator(drv) == ""                  # nothing running: no pill
            assert _js(drv, "return !!document.getElementById('status-scan-info');") is False
            _js(drv, "window.__sameDocument = true;")

            code, body = post(port, "/api/admin/import-history/dry-run",
                              {"password": pw, "instrument": "gc1",
                               "processed_dir": str(processed)})
            assert code == 202, body
            # running, with its progress in words, on a page that didn't start it
            assert _wait(lambda: "history dry run" in _indicator(drv)
                         and "Scanning the folder" in _indicator(drv)), _indicator(drv)
            assert _js(drv, "return document.getElementById('running-now').dataset.state;") \
                == "running"
            drv.find_element("id", "running-now").click()
            pop = lambda: _js(drv, "return document.getElementById('running-now-popover').textContent;")  # noqa: E731
            assert _wait(lambda: "Scanning the folder" in pop()), pop()
            assert "Test Operator" in pop()               # who started it
            assert "Open" in pop() and "Dismiss" not in pop()
            assert str(processed) not in pop()            # never its folder
            assert pop().startswith("Running now")

            stall.release()
            # the outcome, still without a reload: one line with its count
            assert _wait(lambda: "dry run finished" in _indicator(drv), timeout=60), _indicator(drv)
            assert "classified" in _indicator(drv)
            assert _js(drv, "return document.getElementById('running-now').dataset.state;") == "done"
            assert _wait(lambda: pop().startswith("Recent work")), pop()
            assert "dry run finished" in pop() and "Scanning the folder" not in pop()
            assert _js(drv, "return window.__sameDocument === true;")

            # a second ended task, then Dismiss one: the list stays open (review #5)
            code, body = post(port, "/api/reprocess", {"sample_ids": [hub.ids["final"]]})
            assert code == 200, body
            assert _wait(lambda: "Re-processed 1 sample" in pop(), timeout=60), pop()
            items = lambda: _js(drv, "return document.querySelectorAll("  # noqa: E731
                                     "'#running-now-popover .rn-task').length;")
            assert items() == 2
            drv.find_element("css selector",
                             "#running-now-popover li[data-task-id^='import-history-dry-run'] "
                             ".rn-dismiss").click()
            assert _wait(lambda: items() == 1)
            assert _js(drv, "return document.getElementById('running-now-popover').hidden;") is False
            assert "Re-processed 1 sample" in pop()

            # dismissed in this browser: the pill goes, and stays gone after a reload
            drv.find_element("css selector", "#running-now-popover .rn-dismiss").click()
            assert _wait(lambda: _indicator(drv) == "")
            drv.refresh()
            assert _wait(lambda: _js(drv, "return document.querySelectorAll("
                                          "'#dash-file-list li[data-uid]').length") > 0)
            time.sleep(4)                                  # a poll or two
            assert _indicator(drv) == ""
        finally:
            stall.close()
            drv.quit()


def test_the_gc_strip_follows_heartbeats_and_the_list_order(tmp_path):
    hub = build_hub(tmp_path)
    token = ingest_api.mint_token("gc1", db=hub.db)
    # a run whose CDF had no injection time (the hub used the file's time)
    nt = store.samples.insert_received("gc1", "NO-TIME-1", "2026-09-26 10:00:00", "mtime",
                                       cdf_sha256=None, cdf_path=None, status="final",
                                       db=hub.db)
    with booted(tmp_path) as (port, _proc, _data, _home):
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            drv.get(f"http://127.0.0.1:{port}/classic")
            strip = lambda: _js(drv, "const b = document.getElementById('gc-strip');"  # noqa: E731
                                     "return b.hidden ? '' : b.textContent;")
            assert _wait(lambda: strip() == "\u25cb0 of 2 GCs live"), strip()
            _js(drv, "window.__sameDocument = true;")

            code, _ = _heartbeat(port, token)
            assert code == 200
            assert _wait(lambda: strip() == "\u25cb1 of 2 GCs live"), strip()
            assert _js(drv, "return window.__sameDocument === true;")

            # click: each GC with its status and a link to its page
            drv.find_element("id", "gc-strip").click()
            rows_ = lambda: _js(drv, """return Array.from(document.querySelectorAll(
                '#gc-strip-popover .gc-row')).map(li => [li.dataset.instrument,
                li.textContent, li.querySelector('a').getAttribute('href')]);""")
            assert _wait(lambda: len(rows_()) == 2), rows_()
            by_id = {r[0]: r for r in rows_()}
            assert "Live" in by_id["gc1"][1] and by_id["gc1"][2] == "/instruments/gc1"
            assert "Never checked in" in by_id["gc2"][1] and "GC-2" in by_id["gc2"][1]

            # review blocker: with no new answer, the age keeps going (it used to
            # freeze at "Not seen for 1 min"): stop polling, move the clock on 3 min
            _js(drv, "GCLive.stop(); const t0 = Date.now(); Date.now = () => t0 + 180000;")
            assert _wait(lambda: strip() == "\u25cb0 of 2 GCs live", timeout=25), strip()
            drv.find_element("id", "gc-strip").click()
            drv.find_element("id", "gc-strip").click()
            assert _wait(lambda: "Not seen for 3 min" in {r[0]: r for r in rows_()}["gc1"][1]), rows_()
            drv.refresh()

            # the toolbar fits: nothing pushed off-screen at 1366 or 1440 px (review #6)
            for width in (1366, 1440):
                drv.set_window_size(width, 900)
                assert _wait(lambda: _js(drv, "return !document.getElementById('gc-strip').hidden;"))
                over = _js(drv, """const t = document.getElementById('toolbar');
                    return Array.from(t.children).filter(c => c.offsetParent !== null
                        && c.getBoundingClientRect().right > t.clientWidth + 1).map(c => c.id);""")
                assert over == [], (width, over)
            drv.set_window_size(1600, 1000)

            # ── the sample list ──
            rows = lambda: _js(drv, """return Array.from(document.querySelectorAll(
                '#dash-file-list li')).map(li => li.classList.contains('list-day')
                ? ['day', li.textContent] : [li.dataset.sampleId,
                   (li.querySelector('.file-item-name') || {}).textContent || '',
                   (li.querySelector('.file-item-time') || {}).textContent || '']);""")
            assert _wait(lambda: any(r[0] == str(nt) for r in rows())), rows()
            r = rows()
            days = [x[1] for x in r if x[0] == "day"]
            assert days[-1] == "No injection time", days
            assert r[-1][0] == str(nt) and r[-1][2] == "file 2026-09-26 10:00"
            final = [x for x in r if x[0] == str(hub.ids["final"])][0]
            assert final[2] == "14:23"          # under its day heading: the time only (review #2)
            assert _js(drv, f"return document.querySelector('#dash-file-list li[data-sample-id="
                            f"\"{hub.ids['final']}\"] .file-item-time').title;") == \
                "Injected 2026-09-25 14:23:00"
            # newest first: injection times descend within each day
            day, per_day = None, {}
            for x in r:
                if x[0] == "day":
                    day = x[1]
                elif not x[2].startswith("file"):
                    per_day.setdefault(day, []).append(x[2])
            for d, times in per_day.items():
                assert times == sorted(times, reverse=True), (d, times)

            # full lab IDs: the name is never cut (no ellipsis, nothing hidden)
            cut = _js(drv, """return Array.from(document.querySelectorAll(
                '#dash-file-list .file-item-name')).filter(e => e.scrollWidth > e.clientWidth + 1)
                .map(e => e.textContent);""")
            assert cut == [], cut

            # Lab ID: natural numeric order, no headings, remembered per browser
            drv.find_element("css selector", "#sample-sort button[data-sort='lab']").click()
            names = lambda: [x[1] for x in rows() if x[0] != "day"]  # noqa: E731
            assert _wait(lambda: not any(x[0] == "day" for x in rows()))
            n = names()
            lab = [x.split(" (")[0] for x in n]
            # no headings in Lab ID order: each row says its date again
            assert final_time_in_lab_mode(drv, hub.ids["final"]) == "2026-09-25 14:23"
            assert lab.index("39999") < lab.index("40298") < lab.index("40304") < lab.index("50001"), n
            drv.refresh()
            assert _wait(lambda: len(names()) > 3 and not any(x[0] == "day" for x in rows()))
            assert _js(drv, "return document.querySelector(\"#sample-sort button[data-sort='lab']\")"
                            ".getAttribute('aria-pressed');") == "true"
        finally:
            drv.quit()


def final_time_in_lab_mode(drv, sid):
    return _js(drv, f"return document.querySelector('#dash-file-list li[data-sample-id="
                    f"\"{sid}\"] .file-item-time').textContent;")


def test_hub_admin_unlocks_once_and_starts_a_folder_load(tmp_path):
    build_hub(tmp_path)
    folder = tmp_path / "to-load"
    sample(folder, "LF-1", datetime(2026, 9, 21, 9, 0, 0), method=SIMDIS)
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            drv.get(f"http://127.0.0.1:{port}/admin/hub")
            # v4.0 lane E2: the page is in the shell; the sidebar's mark leads home
            home = drv.find_element("css selector", "#sidebar .sb-mark")
            assert home.is_displayed() and home.get_attribute("href").endswith("/")
            # the instrument selects are filled without the password (the old bug)
            assert _wait(lambda: _js(drv, "return document.querySelectorAll('#lf-inst option')"
                                          ".length;") >= 2)
            assert _js(drv, "return document.getElementById('btn-lf-stop').disabled;") is True
            assert _js(drv, "return document.getElementById('btn-ih-stop').disabled;") is True
            # no per-card Load buttons, no Refresh
            for gone in ("btn-refresh", "btn-sessions", "btn-presets-load", "btn-ih-last"):
                assert _js(drv, f"return document.getElementById('{gone}');") is None, gone

            # a wrong password never shows "Unlocked" (review #8)
            drv.find_element("id", "pw").send_keys("not-the-password")
            drv.find_element("id", "btn-unlock").click()
            assert _wait(lambda: drv.find_element("id", "unlock-msg").text not in ("", "Checking…"),
                         timeout=30)
            assert _js(drv, "return document.getElementById('unlocked').hidden;") is True
            drv.find_element("id", "pw").clear()
            time.sleep(1.2)                               # the hub's one-attempt-at-a-time rule

            # unlock once: sessions, presets and results files load by themselves
            drv.find_element("id", "pw").send_keys(pw)
            drv.find_element("id", "btn-unlock").click()
            assert _wait(lambda: _js(drv, "return !document.getElementById('unlocked').hidden;"))
            assert _wait(lambda: _js(drv, "return document.querySelectorAll('#sessions-rows tr')"
                                          ".length > 0 && document.querySelectorAll('#presets li')"
                                          ".length > 0 && document.querySelectorAll("
                                          "'#exports-rows tr').length > 0;"), timeout=30)
            assert _js(drv, "return document.getElementById('pw').value;") == ""

            # Load CDFs from a folder starts, and renders in its own card only
            _js(drv, "document.getElementById('lf-inst').value = 'gc1';")
            drv.find_element("id", "lf-folder").send_keys(str(folder))
            drv.find_element("id", "btn-load").click()
            lf = lambda: drv.find_element("id", "lf-job").text  # noqa: E731
            assert _wait(lambda: "Folder load finished at " in lf(), timeout=60), \
                lf() + " | " + drv.find_element("id", "lf-msg").text
            assert "Created" in lf() and "{" not in lf()           # counts, never JSON
            assert drv.find_element("id", "ih-job").text == ""     # not in the import card
            # "Started" was stale once it finished: cleared (review #7)
            assert _wait(lambda: drv.find_element("id", "lf-msg").text == "")
            assert _js(drv, "return document.getElementById('btn-lf-stop').disabled;") is True

            # a wrong start gets its message next to the button, not at the top
            drv.find_element("id", "lf-folder").clear()
            drv.find_element("id", "lf-folder").send_keys("relative/path")
            drv.find_element("id", "btn-load").click()
            assert _wait(lambda: "absolute path" in drv.find_element("id", "lf-msg").text)

            # New path is an inline field, never window.prompt (review #7)
            _js(drv, "window.prompt = () => { throw new Error('prompt used'); };")
            drv.find_element("css selector", "#exports-rows tr:first-child td.actions button").click()
            assert _wait(lambda: _js(drv, "return !!document.querySelector("
                                          "'#exports-rows tr.path-edit input.path-input');"))
            drv.find_element("css selector", "#exports-rows tr.path-edit input").send_keys("relative.csv")
            drv.find_element("css selector", "#exports-rows tr.path-edit button.primary").click()
            assert _wait(lambda: "absolute" in drv.find_element(
                "css selector", "#exports-rows tr.path-edit .msg").text)

            # Lock forgets the password
            drv.find_element("id", "btn-lock").click()
            assert _wait(lambda: _js(drv, "return !document.getElementById('unlock-form').hidden;"))
        finally:
            drv.quit()


def test_the_shell_sidebar_shows_running_work_and_live_gcs(tmp_path):
    """The v3.1 shell pages (here /instruments) carry the same running-now row
    and "N of M GCs live" in the sidebar footer, and the paused banner."""
    hub = build_hub(tmp_path)
    token = ingest_api.mint_token("gc1", db=hub.db)
    with booted(tmp_path) as (port, _proc, _data, _home):
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            drv.get(f"http://127.0.0.1:{port}/instruments")
            gcs = lambda: _js(drv, "const a = document.getElementById('gc-summary');"  # noqa: E731
                                   "return a.hidden ? '' : a.textContent;")
            assert _wait(lambda: gcs().endswith("0 of 2 GCs live")), gcs()
            code, _ = _heartbeat(port, token)
            assert code == 200
            assert _wait(lambda: gcs().endswith("1 of 2 GCs live")), gcs()

            code, body = post(port, "/api/reprocess", {"sample_ids": [hub.ids["final"]]})
            assert code == 200, body
            assert _wait(lambda: "Re-process" in _indicator(drv), timeout=60), _indicator(drv)
            drv.find_element("id", "running-now").click()
            assert _wait(lambda: "Re-processed 1 sample" in _js(
                drv, "return document.getElementById('running-now-popover').textContent;"),
                timeout=60)
            assert _js(drv, "return document.getElementById('paused-banner').hidden;") is True
        finally:
            drv.quit()
