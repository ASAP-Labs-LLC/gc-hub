"""v3.1 live updates, in a real browser: with the Samples page open, an agent
sends a CDF through the agent API and its row appears (and leaves Queued)
without a reload, each run once; a change to one run redraws that run and
keeps the open one open; on the instrument's page a heartbeat redraws the
agent's status without a reload. (Until v6.0.0 this drove the classic page
and the classic Instruments page.)

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
    """``{sample id: (lab text, status group)}`` of the Samples page's list."""
    return {int(k): tuple(v) for k, v in drv.execute_script("""
        const out = {};
        document.querySelectorAll('[data-testid=sample-row]').forEach(r => {
            out[r.dataset.sampleId] = [(r.querySelector('.lab') || {}).textContent || '',
                                       r.dataset.status || ''];
        });
        return out;""").items()}


def _no_duplicates(drv) -> bool:
    """The list holds each run once (a live merge never duplicates a row)."""
    return drv.execute_script("""
        const ids = [...document.querySelectorAll('[data-testid=sample-row]')].map(r => r.dataset.sampleId);
        return ids.length > 0 && new Set(ids).size === ids.length;""")


def _agent(drv) -> str:
    return drv.execute_script("const s = document.getElementById('agent');"
                              "return s ? s.textContent : '';")


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
            drv.get(f"http://127.0.0.1:{port}/samples")
            assert _wait(lambda: drv.execute_script("return !!(window.GCSamples && GCSamples.ready);"))
            assert _wait(lambda: len(_rows(drv)) >= 8), _rows(drv)
            # no Refresh button: the page follows /api/live
            assert drv.execute_script("return document.getElementById('btn-refresh');") is None
            drv.execute_script("window.__sameDocument = true;")

            code, res = _ingest(port, token, new_cdf, "77777.CDF")
            assert code == 201, res
            sid = res["sample_id"]
            assert _wait(lambda: sid in _rows(drv)), _rows(drv)
            assert _rows(drv)[sid][0].startswith("77777")
            # processed by the Worker: the row changes in place (no reload)
            assert _wait(lambda: _rows(drv)[sid][1] not in ("", "processing"), timeout=40), _rows(drv)[sid]
            assert drv.execute_script("return window.__sameDocument === true;")
            assert _no_duplicates(drv)

            # a change to one run: the open run stays open, each run listed once
            final = hub.ids["final"]
            drv.get(f"http://127.0.0.1:{port}/samples/{final}")
            assert _wait(lambda: drv.execute_script("return !!(window.GCSamples && GCSamples.ready);"))
            assert _wait(lambda: final in _rows(drv))
            drv.execute_script("window.__sameDocument = true;")
            import store
            rev = store.samples.get(final, db=hub.db)["current_revision"]
            code, body = post(port, "/api/reprocess", {"sample_ids": [final]})
            assert code == 200 and body["count"] == 1, body
            assert _wait(lambda: store.samples.get(final, db=hub.db)["current_revision"] != rev, timeout=40)
            time.sleep(4)                                   # the live poll that reports it
            assert _rows(drv).get(final, ("", ""))[1] == "final", _rows(drv)
            assert drv.execute_script("return location.pathname;") == f"/samples/{final}"
            assert _no_duplicates(drv)
            assert drv.execute_script("return window.__sameDocument === true;")

            # the instrument's page: a heartbeat redraws the agent's status
            drv.get(f"http://127.0.0.1:{port}/instruments/gc1")
            assert _wait(lambda: "Agent version" in _agent(drv)), _agent(drv)
            drv.execute_script("window.__sameDocument = true;")
            code, _ = _heartbeat(port, token, version="v8.8.8")
            assert code == 200
            assert _wait(lambda: "v8.8.8" in _agent(drv)), _agent(drv)
            assert drv.execute_script("return window.__sameDocument === true;")
        finally:
            drv.quit()
