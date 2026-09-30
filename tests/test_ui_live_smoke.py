"""v3.1 live updates, in a real browser: with the main page open, an agent
sends a CDF through the agent API and its row appears (and becomes final)
without a reload; the Refresh button is gone; the "Live · updated …"
indicator runs; on the classic Instruments page a heartbeat redraws the
agent's status without a reload.

Skipped when selenium or a Chrome/chromedriver can't be started. Plotly is
stubbed before the page loads, in case its CDN is unreachable.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")
webdriver = pytest.importorskip("selenium.webdriver")

import ingest_api  # noqa: E402
from bootapp import browser_sign_in, booted, post, send  # noqa: E402
from hub_boot import SIMDIS, build_hub  # noqa: E402

PLOTLY_STUB = """
if (!window.Plotly) {
  window.Plotly = { react(){}, newPlot(){}, purge(){}, relayout(){}, restyle(){},
                    Plots: { resize(){} }, d3: null, __stub: true };
}
"""


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
    try:
        drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": PLOTLY_STUB})
    except Exception:  # noqa: BLE001
        pass
    return drv


def _wait(pred, timeout=30.0):
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


def _rows(drv):
    """``{sample id: (name, status badge text)}`` of the dashboard list."""
    return {int(k): tuple(v) for k, v in drv.execute_script("""
        const out = {};
        document.querySelectorAll('#dash-file-list li[data-uid]').forEach(li => {
            out[li.dataset.sampleId] = [
                (li.querySelector('.file-item-name') || {}).textContent || '',
                (li.querySelector('.status-badge') || {}).textContent || ''];
        });
        return out;""").items()}


def _no_duplicates(drv) -> bool:
    """Every list holds each sample once (a merge never duplicates a row)."""
    return drv.execute_script("""
        return ['dash-file-list', 'chrom-file-list', 'dcurve-file-list', 'analysis-sample-list']
            .every(id => {
                const uids = Array.from(document.querySelectorAll('#' + id + ' li[data-uid]'))
                    .map(li => li.dataset.uid);
                return uids.length > 0 && new Set(uids).size === uids.length;
            });""")


def _detail(drv) -> str:
    return drv.execute_script("return document.getElementById('detail').textContent;")


def _ingest(port, token, body: bytes, filename: str):
    return send(port, "/api/ingest", body, {
        "Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream",
        "X-GC-SHA256": hashlib.sha256(body).hexdigest(), "X-GC-Mtime": "2026-09-27T09:00:00",
        "X-GC-Filename": urllib.parse.quote(filename)})


def _heartbeat(port, token, **over):
    payload = {"version": "v1.0.0", "state": "idle", "host": "GC1-PC",
               "agent_time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    payload.update(over)
    return send(port, "/api/agent/heartbeat", json.dumps(payload).encode(),
                {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})


def test_an_ingested_cdf_appears_without_a_reload(tmp_path):
    hub = build_hub(tmp_path)
    token = ingest_api.mint_token("gc1", db=hub.db)
    new_cdf = hub._cdf("sample", name="77777", injected=datetime(2026, 9, 27, 9, 0, 0),
                       method_name=SIMDIS).read_bytes()

    with booted(tmp_path) as (port, _proc, _data, _home):
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            drv.get(f"http://127.0.0.1:{port}/classic")
            assert _wait(lambda: len(_rows(drv)) >= 8), _rows(drv)
            # the Refresh button (and its Ctrl+R) is gone
            assert drv.execute_script("return document.getElementById('btn-refresh');") is None
            # the indicator says the page is live
            assert _wait(lambda: drv.execute_script(
                "return document.getElementById('live-indicator').textContent;")
                .startswith("Live · updated")), drv.execute_script(
                "return document.getElementById('live-indicator').textContent;")
            drv.execute_script("window.__sameDocument = true;")

            code, res = _ingest(port, token, new_cdf, "77777.CDF")
            assert code == 201, res
            sid = res["sample_id"]
            assert _wait(lambda: sid in _rows(drv)), _rows(drv)
            assert _rows(drv)[sid][0].startswith("77777")
            # processed by the Worker: the row changes in place (no reload)
            assert _wait(lambda: _rows(drv)[sid][1] != "Queued", timeout=40), _rows(drv)[sid]
            assert drv.execute_script("return window.__sameDocument === true;")
            assert _no_duplicates(drv)

            # a change to one row redraws only that row; the selection survives it
            final, rerun = hub.ids["final"], hub.ids["rerun"]
            drv.execute_script(f"""
                state.selectedFile = state.files.find(f => f.sample_id === {final});
                renderAllFileLists();
                for (const id of [{final}, {rerun}]) {{
                    document.querySelector('#dash-file-list li[data-sample-id="' + id + '"]')
                        .__mark = true;
                }}""")
            code, body = post(port, "/api/reprocess", {"sample_ids": [final]})
            assert code == 200 and body["count"] == 1, body
            assert _wait(lambda: not drv.execute_script(
                f"return !!document.querySelector('#dash-file-list li[data-sample-id=\"{final}\"]')"
                ".__mark;"), timeout=40)
            assert drv.execute_script(          # the other row's element was left alone
                f"return !!document.querySelector('#dash-file-list li[data-sample-id=\"{rerun}\"]')"
                ".__mark;")
            assert drv.execute_script(          # still selected, highlighted once
                "return Array.from(document.querySelectorAll('#dash-file-list li.selected'))"
                ".map(li => li.dataset.sampleId);") == [str(final)]
            assert drv.execute_script("return state.selectedFile.sample_id;") == final
            assert _no_duplicates(drv)
            assert drv.execute_script("return window.__sameDocument === true;")

            # the classic Instruments page: a heartbeat redraws the agent's status
            drv.get(f"http://127.0.0.1:{port}/instruments/classic?instrument=gc1")   # v3.1: the 2A2 page moved
            assert _wait(lambda: "Agent version" in _detail(drv))
            drv.execute_script("window.__sameDocument = true;")
            code, _ = _heartbeat(port, token, version="v8.8.8")
            assert code == 200
            assert _wait(lambda: "v8.8.8" in _detail(drv)), _detail(drv)
            assert _wait(lambda: "ago)" in drv.execute_script(
                "return document.querySelector('[data-live=\"last-seen\"]').textContent;"))
            assert drv.execute_script("return window.__sameDocument === true;")
        finally:
            drv.quit()
