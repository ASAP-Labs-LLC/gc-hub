"""v6.0.0: the report queue sheet, when a QBench upload fails (Selenium).

Ryan: "I can't retry an upload if it fails, I have to clear the queue and
resubmit." On the compare harness (``tests/ui_compare_harness.py``: the real
``_layout.html`` and ``report_queue.js``), with the hub's upload answers and
stream scripted in the page:

* the saved sign-in can't be read → the sign-in says exactly why and the
  cursor is in the password box;
* a failed report shows the hub's reason on its row, is not marked sent, and
  has Retry; "Retry failed (N)" sends every failed one; nothing is cleared;
* a retry the hub refuses for want of a password opens the sign-in with the
  hub's words, and Start sends just that report.

Skipped when selenium or a Chrome/chromedriver can't be started.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")
pytest.importorskip("jinja2")
pytest.importorskip("selenium.webdriver")

import ui_compare_harness as harness  # noqa: E402
from bootapp import booted, browser_sign_in, setup_admin  # noqa: E402
from test_ui_compare_smoke import SPY, frame, js, open_page, wait  # noqa: E402
from test_ui_setup_pages_smoke import _driver  # noqa: E402

NO_FILE = ("Can't read the saved QBench sign-in file \\\\ASAPServer\\Labsharedrive\\ASAP Lab Results"
           "\\qbenchlogin.txt: The network path was not found (Windows error 53)")
API_REASON = ("QBench API: QBench client_secret is not configured. Enter it in the app under "
              "Settings > QBench API")

# The hub's upload routes, scripted: /api/qbench-upload answers from
# window.__answers (first in, first out; 'started' when empty), every POST is
# recorded, and __emit plays a stream message.
FAKE = r"""
window.__posted = [];
window.__answers = [];
window.__streams = [];
window.EventSource = function (url) { this.url = url; window.__es = this; window.__streams.push(this);
  this.close = () => { this.closed = true; }; };
const __f3 = window.fetch;
const reply = (b, s) => Promise.resolve(new Response(JSON.stringify(b),
  {status: s || 200, headers: {'Content-Type': 'application/json'}}));
window.fetch = function (url, opts) {
  const u = String(url);
  if (['/api/qbench-upload', '/api/qbench-update-credentials', '/api/qbench-skip-item', '/api/qbench-cancel'].includes(u)) {
    window.__posted.push([u, JSON.parse((opts && opts.body) || '{}')]);
    if (u === '/api/qbench-upload') {
      const a = window.__answers.shift();
      return a ? reply(a.body, a.status) : reply({status: 'started', count: 1});
    }
    return reply({status: 'ok'});
  }
  if (u === '/api/qbench-credentials') return reply({username: 'lab.user', has_password: false, problem: window.__problem});
  if (u === '/api/qbench-upload-status') return reply({active: false, items: [], skipped: [], overall: null});
  return __f3.apply(this, arguments);
};
window.__emit = (d) => window.__es.onmessage({data: JSON.stringify(d)});
"""


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("v6-retry")
    hub = harness.build(tmp)
    with booted(tmp) as (port, _proc, data, _home):
        setup_admin(port, data)
        hx = harness.Harness(port)
        drv = _driver()
        drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": SPY})
        try:
            browser_sign_in(drv, port)
            yield {"drv": drv, "hx": hx, "hub": hub, "port": port, "sid": hub.ids["diesel"]}
        finally:
            drv.quit()
            hx.stop()


def uploads(drv):
    return js(drv, "return window.__posted.filter(p => p[0] === '/api/qbench-upload').map(p => p[1])")


def test_a_failed_upload_says_why_and_is_retried_without_clearing(env):
    drv = open_page(env)
    js(drv, FAKE)
    js(drv, "window.__problem = arguments[0]", NO_FILE)
    final = env["hub"].ids["final"]
    js(drv, "document.getElementById('h-queue').click()")          # 40329 (the mounted sample)
    js(drv, "GCCompare.addToQueue({sample: {sample_id: arguments[0], lab_id: '40304'},"
            " standards: window.__h.standards, settings: window.__h.settings})", final)
    js(drv, "document.getElementById('report-queue-btn').click()")
    js(drv, "document.querySelector('[data-testid=rq-upload]').click()")

    # the saved sign-in can't be read: said exactly, the password box focused
    assert wait(lambda: js(drv, "return document.querySelector('[data-testid=rq-signin-note]').textContent")
                == NO_FILE + " Enter the QBench password to upload.")
    assert js(drv, "return document.activeElement.id") == "rq-qb-pass"
    frame(drv, "sign-in with the reason")
    js(drv, "document.getElementById('rq-qb-pass').value = 'pw'; document.querySelector('[data-testid=rq-upload]').click()")
    assert wait(lambda: len(uploads(drv)) == 1)
    assert [q["sample_id"] for q in uploads(drv)[0]["queue"]] == [env["sid"], final]

    # the run: one uploaded, one failed with the hub's reason
    js(drv, "__emit({t: 'snapshot', active: true, items: [], overall: null})")
    js(drv, "__emit({t: 'item', idx: 0, total: 2, sample_id: arguments[0], lab_id: '40329', status: 'ok', msg: 'Uploaded'});"
            "__emit({t: 'item', idx: 1, total: 2, sample_id: arguments[1], lab_id: '40304', status: 'failed', msg: arguments[2]});"
            "__emit({t: 'overall', status: 'partial', ok: 1, fail: 1, total: 2, msg: '1 OK, 1 failed'});",
       env["sid"], final, API_REASON)
    assert wait(lambda: js(drv, "return document.getElementById('rq-overall').textContent") == "1 uploaded, 1 failed.")
    rows = js(drv, "return [...document.querySelectorAll('[data-testid=rq-item]')].map(li => li.dataset.state)")
    assert rows == ["sent", "failed"], rows
    assert js(drv, "return document.querySelector('[data-testid=rq-reason]').textContent") == API_REASON
    assert js(drv, "return GCReportQueue.items().map(i => !!i.sent_at)") == [True, False]
    assert js(drv, "return document.querySelectorAll('[data-testid=rq-retry]').length") == 1
    assert js(drv, "return document.querySelectorAll('[data-testid=rq-up-retry]').length") == 1
    btn = "document.querySelector('[data-testid=rq-retry-failed]')"
    assert js(drv, f"return !{btn}.hidden && {btn}.textContent") == "Retry failed (1)"
    assert "Retry failed (1) sends it again" in js(drv, "return document.querySelector('[data-testid=rq-msg]').textContent")
    assert js(drv, "return document.querySelector('[data-testid=rq-up-item][data-status=failed] .rq-detail').textContent") \
        == API_REASON
    frame(drv, "a failed report")

    # Retry: just that report, the queue kept; it goes through this time
    js(drv, "document.querySelector('[data-testid=rq-retry]').click()")
    assert wait(lambda: len(uploads(drv)) == 2)
    assert [q["sample_id"] for q in uploads(drv)[1]["queue"]] == [final]
    assert js(drv, "return GCReportQueue.count()") == 2
    assert wait(lambda: js(drv, "return window.__streams.length") == 2)
    js(drv, "__emit({t: 'snapshot', active: true, items: [{idx: 0, sample_id: arguments[0], lab_id: '40304', status: 'waiting', msg: 'Waiting'}], overall: null});"
            "__emit({t: 'item', idx: 0, total: 1, sample_id: arguments[0], lab_id: '40304', status: 'ok', msg: 'Uploaded'});"
            "__emit({t: 'overall', status: 'done', ok: 1, fail: 0, total: 1, msg: ''});", final)
    assert wait(lambda: js(drv, "return GCReportQueue.items().every(i => !!i.sent_at)"))
    assert js(drv, f"return {btn}.hidden") is True
    assert js(drv, "return document.querySelectorAll('[data-testid=rq-retry]').length") == 0
    js(drv, "GCReportQueue.clear(); document.querySelector('[data-testid=report-queue-sheet]').close()")


def test_retry_failed_and_a_retry_that_needs_the_password(env):
    drv = open_page(env)
    js(drv, FAKE)
    js(drv, "window.__problem = null")
    final, rerun = env["hub"].ids["final"], env["hub"].ids["rerun"]
    for sid in (final, rerun):
        js(drv, "GCCompare.addToQueue({sample: {sample_id: arguments[0], lab_id: '40304'},"
                " standards: window.__h.standards, settings: window.__h.settings})", sid)
    js(drv, "document.getElementById('report-queue-btn').click()")
    js(drv, "document.querySelector('[data-testid=rq-upload]').click()")
    assert wait(lambda: not js(drv, "return document.querySelector('[data-testid=rq-signin]').hidden"))
    assert js(drv, "return document.querySelector('[data-testid=rq-signin-note]').textContent") == \
        "Leave the password empty to use the saved QBench sign-in."
    js(drv, "document.querySelector('[data-testid=rq-upload]').click()")
    assert wait(lambda: len(uploads(drv)) == 1)
    # both fail; then the stream is lost and the run ends while the page is away
    js(drv, "__emit({t: 'item', idx: 0, total: 2, sample_id: arguments[0], lab_id: '40304', status: 'failed', msg: 'Chrome could not start: session not created'});",
       final)
    js(drv, "window.__statusAnswer = {active: false, overall: {t: 'overall', status: 'allfailed', ok: 0, fail: 2, total: 2},"
            " items: [{idx: 0, sample_id: arguments[0], lab_id: '40304', status: 'failed', msg: 'Chrome could not start: session not created'},"
            "         {idx: 1, sample_id: arguments[1], lab_id: '40304', status: 'uploading', msg: 'Step 3/10'}]};"
            "const __f4 = window.fetch; window.fetch = function (u, o) { if (String(u) === '/api/qbench-upload-status')"
            " return Promise.resolve(new Response(JSON.stringify(window.__statusAnswer), {status: 200,"
            " headers: {'Content-Type': 'application/json'}})); return __f4.apply(this, arguments); };"
            "window.__es.onerror();", final, rerun)
    assert wait(lambda: js(drv, "return document.getElementById('rq-overall').textContent") == "The upload failed (2).", 15)
    assert js(drv, "return GCReportQueue.items().map(i => i.upload_error && i.upload_error.msg)") == [
        "Chrome could not start: session not created",
        "Not uploaded: the upload ended before this report was sent."]
    btn = "document.querySelector('[data-testid=rq-retry-failed]')"
    assert js(drv, f"return {btn}.textContent") == "Retry failed (2)"

    # the hub now wants the password: the sign-in says so, Start sends those two
    js(drv, "window.__answers.push({status: 400, body: {need_password: true,"
            " error: 'The saved QBench sign-in file X is empty. Enter the QBench password to upload.'}})")
    js(drv, f"{btn}.click()")
    assert wait(lambda: not js(drv, "return document.querySelector('[data-testid=rq-signin]').hidden"))
    assert wait(lambda: js(drv, "return document.querySelector('[data-testid=rq-signin-note]').textContent")
                == "The saved QBench sign-in file X is empty. Enter the QBench password to upload.")
    assert js(drv, "return document.querySelector('[data-testid=rq-upload]').textContent") == "Start upload (2)"
    assert js(drv, "return GCReportQueue.items().every(i => !i.sent_at)")      # still not sent
    js(drv, "document.getElementById('rq-qb-pass').value = 'pw'; document.querySelector('[data-testid=rq-upload]').click()")
    assert wait(lambda: len(uploads(drv)) == 3)
    last = uploads(drv)[2]
    assert [q["sample_id"] for q in last["queue"]] == [final, rerun] and last["password"] == "pw"
    assert js(drv, "return GCReportQueue.items().every(i => !!i.sent_at && !i.upload_error)")
    js(drv, "GCReportQueue.clear(); document.querySelector('[data-testid=report-queue-sheet]').close()")


def test_a_silent_stream_never_leaves_the_sheet_waiting(env):
    """A stream that says nothing (buffered by a proxy, or dropped unnoticed):
    the sheet asks the hub, learns the run ended and how, and settles."""
    drv = open_page(env)
    js(drv, FAKE)
    js(drv, "window.__problem = null; GCReportQueue.timeouts.quietStream = 300; GCReportQueue.timeouts.watchEvery = 200")
    final = env["hub"].ids["final"]
    js(drv, "GCCompare.addToQueue({sample: {sample_id: arguments[0], lab_id: '40304'},"
            " standards: window.__h.standards, settings: window.__h.settings})", final)
    js(drv, "document.getElementById('report-queue-btn').click()")
    js(drv, "document.querySelector('[data-testid=rq-upload]').click()")
    assert wait(lambda: not js(drv, "return document.querySelector('[data-testid=rq-signin]').hidden"))
    js(drv, "const __sid = arguments[0]; const __f5 = window.fetch; window.fetch = function (u, o) { if (String(u) === '/api/qbench-upload-status')"
            " return Promise.resolve(new Response(JSON.stringify({active: false, skipped: [],"
            " overall: {t: 'overall', status: 'allfailed', ok: 0, fail: 1, total: 1},"
            " items: [{idx: 0, sample_id: __sid, lab_id: '40304', status: 'failed', msg: 'QBench has no sample with lab ID 40304.'}]}),"
            " {status: 200, headers: {'Content-Type': 'application/json'}})); return __f5.apply(this, arguments); };", final)
    js(drv, "document.querySelector('[data-testid=rq-upload]').click()")
    assert wait(lambda: js(drv, "return document.getElementById('rq-overall').textContent") == "The upload failed (1).", 15)
    assert js(drv, "return window.__es.closed") is True
    assert js(drv, "return GCReportQueue.items().map(i => !!i.sent_at)") == [False]
    assert js(drv, "return document.querySelector('[data-testid=rq-reason]').textContent") == \
        "QBench has no sample with lab ID 40304."
    assert js(drv, "return document.querySelector('[data-testid=rq-retry-failed]').textContent") == "Retry failed (1)"
    js(drv, "GCReportQueue.timeouts.quietStream = 20000; GCReportQueue.timeouts.watchEvery = 5000;"
            "GCReportQueue.clear(); document.querySelector('[data-testid=report-queue-sheet]').close()")
