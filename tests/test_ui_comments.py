"""Phase 4 UI, in headless Chrome: the Analysis tab's Comments section, the
annotation → comment persistence, Clear Annotations and the presets admin
panel.

Plotly's CDN is blocked and replaced by a recording stub (``newPlot``/``react``
keep ``div.layout``, ``relayout`` merges into it, ``div.on`` keeps handlers), so
the test can read the shapes and labels the page draws and fire a box-select.
Skipped when selenium or Chrome can't be started.
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
from bootapp import browser_sign_in, booted, setup_admin  # noqa: E402
from hub_boot import build_hub  # noqa: E402

PLOTLY_STUB = r"""
(function () {
  function el(d) { return typeof d === 'string' ? document.getElementById(d) : d; }
  function plot(d, data, layout) {
    d = el(d); if (!d) return Promise.resolve();
    d.data = data || [];
    d.layout = JSON.parse(JSON.stringify(layout || {}));
    if (!d.on) {
      d.__handlers = {};
      d.on = function (ev, fn) { (d.__handlers[ev] = d.__handlers[ev] || []).push(fn); };
    }
    return Promise.resolve(d);
  }
  window.Plotly = {
    __recording: true,
    newPlot: plot, react: plot,
    relayout: function (d, upd) {
      d = el(d); d.layout = d.layout || {};
      for (const k of Object.keys(upd || {})) {
        d.layout[k] = JSON.parse(JSON.stringify(upd[k]));
      }
      return Promise.resolve(d);
    },
    restyle: function () { return Promise.resolve(); },
    purge: function () {}, addTraces: function () {}, deleteTraces: function () {},
    Plots: { resize: function () {} },
  };
  Object.defineProperty(window, 'Plotly', { value: window.Plotly, writable: false });
})();
"""
EVIL = '<img src=x onerror="window.__pwned=1">'


def _driver():
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1600,1000",
                "--disable-dev-shm-usage"):
        opts.add_argument(arg)
    try:
        drv = webdriver.Chrome(options=opts)
    except Exception as exc:  # noqa: BLE001 - no Chrome / driver here
        pytest.skip(f"headless Chrome unavailable: {exc}")
    drv.execute_cdp_cmd("Network.enable", {})
    drv.execute_cdp_cmd("Network.setBlockedURLs", {"urls": ["*cdn.plot.ly*"]})
    drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": PLOTLY_STUB})
    return drv


def _wait(pred, timeout=20.0):
    deadline = time.time() + timeout
    v = None
    while time.time() < deadline:
        try:
            v = pred()
        except Exception:  # noqa: BLE001 - DOM not ready yet
            v = None
        if v:
            return v
        time.sleep(0.2)
    return v


def _js(drv, script, *args):
    return drv.execute_script(script, *args)


def _comments(db, sid, deleted=False):
    return store.sample_comments.list(sid, include_deleted=deleted, db=db)


def _list_texts(drv):
    return _js(drv, "return Array.from(document.querySelectorAll('#comment-list li "
                    ".comment-text')).map(e => e.textContent);")


def _select(drv, sid):
    """Make ``sid`` the Analysis tab's sample, as a click in its list does."""
    _js(drv, """
        const f = state.files.find(x => x.sample_id === arguments[0]);
        state.selectedFile = f; state.selectedSample = f;
        updateAnalysisOverlay();
    """, sid)


def _trend(drv):
    return _js(drv, """
        const d = document.getElementById('analysis-trend-plot');
        return {shapes: (d.layout.shapes || []), annotations: (d.layout.annotations || [])};
    """)


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("p4-ui")
    hub = build_hub(tmp)
    with booted(tmp, sign_in_as="Ryan Brown") as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            drv.get(f"http://127.0.0.1:{port}/")
            assert _wait(lambda: _js(drv, "return state.files.length") >= 5)
            assert _js(drv, "return window.Plotly.__recording === true")
            yield drv, hub, port, pw
        finally:
            drv.quit()


def test_comments_are_by_the_signed_in_name(page):
    """Sign-in (D6 rev 2): no initials box; the page says who comments are
    saved as, and the server records the session's name."""
    drv, hub, _port, _pw = page
    assert _js(drv, "return document.getElementById('comment-initials')") is None
    assert _wait(lambda: _js(drv, "return document.getElementById('comment-author')"
                                  ".textContent") == "Commenting as Ryan Brown")
    assert "Ryan Brown" in _js(drv, "return document.getElementById('signed-in').textContent")
    assert _js(drv, "return 'INITIALS_KEY' in Comments || 'requireInitials' in Comments") is False


def test_free_text_is_rendered_as_text(page):
    drv, hub, _port, _pw = page
    sid = hub.ids["final"]
    _select(drv, sid)
    _js(drv, "document.getElementById('comment-free-text').value = arguments[0];"
             "document.getElementById('btn-comment-add').click();", EVIL)
    assert _wait(lambda: EVIL in (_list_texts(drv) or []))
    assert [c["text"] for c in _comments(hub.db, sid)] == [EVIL]
    assert _js(drv, "return document.querySelectorAll('#comment-list img').length") == 0
    assert _js(drv, "return window.__pwned || 0") == 0
    meta = _js(drv, "return document.querySelector('#comment-list li .comment-meta')"
                    ".textContent")
    assert "Ryan Brown" in meta           # the list shows the account name


def test_preset_chip_adds_the_preset_text(page):
    drv, hub, _port, _pw = page
    sid = hub.ids["rerun"]
    _select(drv, sid)
    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#comment-presets "
                                  "button').length") >= 4)
    _js(drv, "document.querySelectorAll('#comment-presets button')[1].click();")
    assert _wait(lambda: _comments(hub.db, sid))
    c = _comments(hub.db, sid)[0]
    assert c["text"] == "Sample appears to be gasoline." and c["source"] == "preset"
    assert c["author_initials"] == "RB" and c["author_name"] == "Ryan Brown"
    assert _wait(lambda: _list_texts(drv) == ["Sample appears to be gasoline."])


def test_delete_asks_for_confirmation(page):
    drv, hub, _port, _pw = page
    sid = hub.ids["rerun"]
    _select(drv, sid)
    assert _wait(lambda: _list_texts(drv) == ["Sample appears to be gasoline."])
    _js(drv, "window.__confirms = []; window.confirm = m => { __confirms.push(m); return false; };"
             "document.querySelector('#comment-list li .comment-delete').click();")
    time.sleep(0.5)
    assert len(_comments(hub.db, sid)) == 1
    assert len(_js(drv, "return window.__confirms")) == 1
    _js(drv, "window.confirm = m => true;"
             "document.querySelector('#comment-list li .comment-delete').click();")
    assert _wait(lambda: _comments(hub.db, sid) == [])
    gone = _comments(hub.db, sid, deleted=True)[0]
    assert gone["deleted_by_initials"] == "RB"
    assert _wait(lambda: _list_texts(drv) == [])


def _annotate(drv, t0, t1, text):
    """Box-select on the trend plot in annotation mode, then save the modal."""
    _js(drv, "setupAnnotationHandler();"
             "if (!annotationMode) toggleAnnotationMode();"
             "const d = document.getElementById('analysis-trend-plot');"
             "(d.__handlers.plotly_selected || []).forEach(f => f({range: {x: [arguments[0], "
             "arguments[1]]}}));", t0, t1)
    _js(drv, "document.getElementById('annotation-comment-input').value = arguments[0];"
             "document.getElementById('btn-annotation-save').click();", text)


def test_annotation_save_posts_one_comment_and_shapes_come_from_get(page):
    drv, hub, _port, _pw = page
    sid = hub.ids["released"]
    _select(drv, sid)
    _annotate(drv, 1.1, 1.4, "<b>odd</b> hump")
    assert _wait(lambda: len(_comments(hub.db, sid)) == 1)
    time.sleep(0.5)
    rows = _comments(hub.db, sid)
    assert len(rows) == 1                                     # exactly one POST
    assert rows[0]["source"] == "annotation" and (rows[0]["t0"], rows[0]["t1"]) == (1.1, 1.4)
    assert rows[0]["text"] == "<b>odd</b> hump"
    assert _js(drv, "return typeof annotationData") == "undefined"
    report = _js(drv, "return document.getElementById('analysis-report-text').textContent")
    assert "odd" not in report                                # never written into the bullets

    # a second one with no text gets the default label from the server
    _annotate(drv, 2.0, 2.5, "")
    assert _wait(lambda: len(_comments(hub.db, sid)) == 2)
    assert _comments(hub.db, sid)[1]["text"].startswith("Marked region C")

    # another sample, then back: the shapes are redrawn from GET
    _select(drv, hub.ids["final"])
    assert _wait(lambda: not [s for s in _trend(drv)["shapes"]
                              if s.get("fillcolor") == _js(drv, "return Comments.ANNOT_FILL")])
    _select(drv, sid)

    def drawn():
        tr = _trend(drv)
        fill = _js(drv, "return Comments.ANNOT_FILL")
        return sorted((s["x0"], s["x1"]) for s in tr["shapes"] if s.get("fillcolor") == fill)
    assert _wait(lambda: drawn() == [(1.1, 1.4), (2.0, 2.5)]), drawn()
    labels = [a["text"] for a in _trend(drv)["annotations"]
              if a.get("bgcolor") == _js(drv, "return Comments.ANNOT_LABEL_BG")]
    assert "&lt;b&gt;odd&lt;/b&gt; hump" in labels
    assert not any("<b>" in t for t in labels)
    assert _js(drv, "return document.getElementById('annotation-count').textContent") \
        .startswith("2 ")


def test_clear_annotations_confirms_the_count_and_soft_deletes(page):
    drv, hub, _port, _pw = page
    sid = hub.ids["released"]
    _select(drv, sid)
    _js(drv, "document.getElementById('comment-free-text').value = 'keep me';"
             "document.getElementById('btn-comment-add').click();")
    assert _wait(lambda: len(_comments(hub.db, sid)) == 3)
    _js(drv, "window.__confirms = []; window.confirm = m => { __confirms.push(m); return false; };"
             "document.getElementById('btn-clear-annotations').click();")
    time.sleep(0.5)
    assert len(_comments(hub.db, sid)) == 3                  # cancelled: nothing deleted
    assert "2 annotation comments" in _js(drv, "return window.__confirms[0]")
    _js(drv, "window.confirm = m => true;"
             "document.getElementById('btn-clear-annotations').click();")
    assert _wait(lambda: [c["text"] for c in _comments(hub.db, sid)] == ["keep me"])
    deleted = [c for c in _comments(hub.db, sid, deleted=True) if c["deleted_at"]]
    assert len(deleted) == 2 and all(c["deleted_by_initials"] == "RB" for c in deleted)
    fill = _js(drv, "return Comments.ANNOT_FILL")
    assert _wait(lambda: not [s for s in _trend(drv)["shapes"] if s.get("fillcolor") == fill])


def test_queued_items_carry_no_annotations(page):
    drv, hub, _port, _pw = page
    _select(drv, hub.ids["final"])
    _js(drv, "state.analysisQueue.length = 0; openAnalysisExportModal(); confirmAddToQueue();")
    item = _js(drv, "return state.analysisQueue[0]")
    assert item and "annotations" not in item
    _js(drv, "state._editingQueueIdx = 0; addToAnalysisQueue();")
    assert "annotations" not in _js(drv, "return state.analysisQueue[0]")
    _js(drv, "state.analysisQueue.length = 0; renderAnalysisQueue();")


def test_presets_admin_panel(page):
    drv, hub, port, pw = page
    drv.get(f"http://127.0.0.1:{port}/admin/hub")
    # v4.0 lane E: unlock once, the presets load by themselves
    _js(drv, "document.getElementById('pw').value = arguments[0];"
             "document.getElementById('btn-unlock').click();", pw)
    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#presets li').length") >= 4)
    _js(drv, "document.getElementById('preset-new-text').value = arguments[0];"
             "document.getElementById('btn-preset-add').click();", EVIL)
    assert _wait(lambda: EVIL in [p["text"] for p in store.comment_presets.list(db=hub.db)])
    assert _wait(lambda: EVIL in _js(drv, "return Array.from(document.querySelectorAll("
                                          "'#presets li input.preset-text')).map(i => i.value)"))
    assert _js(drv, "return document.querySelectorAll('#presets img').length") == 0
    assert _js(drv, "return window.__pwned || 0") == 0
    # move the new (last) preset up, then deactivate it
    last = store.comment_presets.list(db=hub.db)[-1]
    _js(drv, "document.querySelector(`#presets li[data-id='${arguments[0]}'] .preset-up`)"
             ".click();", last["id"])
    assert _wait(lambda: store.comment_presets.list(db=hub.db)[-2]["id"] == last["id"])
    _js(drv, "document.querySelector(`#presets li[data-id='${arguments[0]}'] .preset-toggle`)"
             ".click();", last["id"])
    assert _wait(lambda: last["id"] not in [p["id"] for p in store.comment_presets.list(db=hub.db)])
    # edit the first preset's text
    first = store.comment_presets.list(db=hub.db)[0]
    _js(drv, "const li = document.querySelector(`#presets li[data-id='${arguments[0]}']`);"
             "li.querySelector('input.preset-text').value = 'Edited by admin';"
             "li.querySelector('.preset-save').click();", first["id"])
    assert _wait(lambda: store.comment_presets.get(first["id"], db=hub.db)["text"]
                 == "Edited by admin")


# ── wrong-sample races (critic C1, I2) ──────────────────────────────────────

FETCH_DELAY = r"""
if (!window.__delayInstalled) {
  window.__delayInstalled = true;
  window.__delay = {};          // url substring -> ms
  window.__fakeAnalysis = null; // (body) -> {ms, result}
  const orig = window.fetch.bind(window);
  window.fetch = async function (url, opts) {
    const u = String(url);
    if (window.__fakeAnalysis && u.includes('/api/analysis')) {
      const f = window.__fakeAnalysis(JSON.parse(opts.body));
      await new Promise(r => setTimeout(r, f.ms));
      return new Response(JSON.stringify(f.result),
                          {status: 200, headers: {'Content-Type': 'application/json'}});
    }
    for (const [k, ms] of Object.entries(window.__delay)) {
      if (u.includes(k)) await new Promise(r => setTimeout(r, ms));
    }
    return orig(url, opts);
  };
}
"""


def _home(drv, port):
    if not drv.current_url.rstrip("/").endswith(str(port)):
        drv.get(f"http://127.0.0.1:{port}/")
    assert _wait(lambda: _js(drv, "return state.files.length") >= 5)


def _annot(db, sid, t0, t1, text):
    return store.sample_comments.add(sid, text=text, source="annotation", t0=t0, t1=t1,
                                     author_initials="ZZ", author_ip=None, revision=None, db=db)


def _drawn(drv):
    fill = _js(drv, "return Comments.ANNOT_FILL")
    return sorted((s["x0"], s["x1"]) for s in _trend(drv)["shapes"] if s.get("fillcolor") == fill)


def test_clear_annotations_never_touches_the_previous_sample(page):
    drv, hub, port, _pw = page
    _home(drv, port)
    a, b = hub.ids["backfill"], hub.ids["slashed"]
    ca = _annot(hub.db, a, 1.0, 1.2, "on A")
    cb = _annot(hub.db, b, 3.0, 3.2, "on B")
    _js(drv, FETCH_DELAY)
    _select(drv, a)
    assert _wait(lambda: _drawn(drv) == [(1.0, 1.2)]), _drawn(drv)

    _js(drv, "window.__delay[arguments[0]] = 1500;", f"/api/samples/{b}/comments")
    _select(drv, b)
    # while B's comments are in flight, A's spans are not shown as B's
    _js(drv, "redrawAnnotations();")
    time.sleep(0.2)
    assert _drawn(drv) == []
    _js(drv, "window.__confirms = []; window.confirm = m => { __confirms.push(m); return true; };"
             "document.getElementById('btn-clear-annotations').click();")
    assert _wait(lambda: store.sample_comments.get(cb, db=hub.db)["deleted_at"]), "B not cleared"
    time.sleep(0.5)
    assert store.sample_comments.get(ca, db=hub.db)["deleted_at"] is None   # A untouched
    msg = _js(drv, "return window.__confirms[0]")
    assert "1 annotation comment" in msg and "AB/../12" in msg, msg
    _js(drv, "window.__delay = {};")


def test_annotation_is_refused_when_the_sample_changed(page):
    drv, hub, port, _pw = page
    _home(drv, port)
    a, b = hub.ids["backfill"], hub.ids["other"]
    before = {s: len(_comments(hub.db, s)) for s in (a, b)}
    _select(drv, a)
    _js(drv, "setupAnnotationHandler();"
             "if (!annotationMode) toggleAnnotationMode();"
             "const d = document.getElementById('analysis-trend-plot');"
             "(d.__handlers.plotly_selected || []).forEach(f => f({range: {x: [0.5, 0.7]}}));")
    _select(drv, b)                                   # the operator moved on
    _js(drv, "document.getElementById('annotation-comment-input').value = 'meant for A';"
             "document.getElementById('btn-annotation-save').click();")
    time.sleep(0.8)
    assert {s: len(_comments(hub.db, s)) for s in (a, b)} == before
    assert _js(drv, "return document.getElementById('modal-annotation')"
                    ".classList.contains('open')")
    # back on A, the same modal saves to A
    _select(drv, a)
    _js(drv, "document.getElementById('btn-annotation-save').click();")
    assert _wait(lambda: len(_comments(hub.db, a)) == before[a] + 1)
    assert _comments(hub.db, a)[-1]["text"] == "meant for A"
    assert len(_comments(hub.db, b)) == before[b]


def test_a_slow_earlier_analysis_does_not_render_over_a_newer_one(page):
    drv, hub, port, _pw = page
    _home(drv, port)
    a, b = hub.ids["backfill"], hub.ids["final"]
    _js(drv, FETCH_DELAY)
    _js(drv, """
        window.__fakeAnalysis = body => ({
          ms: body.sample_id === arguments[0] ? 1500 : 100,
          result: {trend: {sample_x: [0, 1], sample_y: [0, 1]}, diff: {x: [0, 1], y: [0, 0]},
                   report: 'r', conclusion: 'from ' + body.sample_id}});
        state.selectedStandard = state.comparisonStandards[0];
    """, a)
    _select(drv, a)
    _js(drv, "runAnalysis();")
    time.sleep(0.2)
    _select(drv, b)
    _js(drv, "runAnalysis();")
    time.sleep(2.5)
    assert _js(drv, "return document.getElementById('analysis-conclusion').value") == f"from {b}"
    assert _js(drv, "return state._renderedAnalysisSampleId") == b
    _js(drv, "window.__fakeAnalysis = null;")


def test_admin_reorder_keeps_unsaved_edits(page):
    drv, hub, port, pw = page
    drv.get(f"http://127.0.0.1:{port}/admin/hub")
    # v4.0 lane E: unlock once, the presets load by themselves
    _js(drv, "document.getElementById('pw').value = arguments[0];"
             "document.getElementById('btn-unlock').click();", pw)
    assert _wait(lambda: _js(drv, "return document.querySelectorAll('#presets li').length") >= 3)
    ids = [p["id"] for p in store.comment_presets.list(include_inactive=True, db=hub.db)]
    _js(drv, "document.querySelector(`#presets li[data-id='${arguments[0]}'] input.preset-text`)"
             ".value = 'unsaved edit';", ids[0])
    _js(drv, "document.querySelector(`#presets li[data-id='${arguments[0]}'] .preset-down`)"
             ".click();", ids[2])
    assert _wait(lambda: [p["id"] for p in store.comment_presets.list(include_inactive=True,
                                                                      db=hub.db)][3] == ids[2])
    assert _wait(lambda: _js(drv, "return document.querySelector(`#presets li[data-id='"
                                  "${arguments[0]}'] input.preset-text`).value", ids[0])
                 == "unsaved edit")
    assert store.comment_presets.get(ids[0], db=hub.db)["text"] != "unsaved edit"
    drv.get(f"http://127.0.0.1:{port}/")
    assert _wait(lambda: _js(drv, "return state.files.length") >= 5)
