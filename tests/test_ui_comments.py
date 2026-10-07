"""Phase 4 UI, in headless Chrome: the conclusion presets admin panel on Hub
admin. Since v6.0.0 comments are part of the conclusion: Compare's "Insert
preset", earlier notes, Annotate and Clear annotations are driven by
``tests/test_ui_compare_smoke.py``.

Plotly's CDN is blocked and replaced by a recording stub (``newPlot``/``react``
keep ``div.layout``, ``relayout`` merges into it, ``div.on`` keeps handlers).
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








@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("p4-ui")
    hub = build_hub(tmp)
    with booted(tmp, sign_in_as="Ryan Brown") as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            yield drv, hub, port, pw
        finally:
            drv.quit()


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
