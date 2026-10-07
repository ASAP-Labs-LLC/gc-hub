"""v2.0.0 RC: a headless-Chrome smoke of the Samples page (the classic main
page until v6.0.0) with two instruments that hold the same lab ID. Each row
names its instrument, the instrument chips filter the list (the filter is in
the address, so a reload keeps it), and a review note shows on its row.

Skipped when selenium or a Chrome/chromedriver can't be started. Plotly is
stubbed before the page loads, in case its CDN is unreachable (the real
library replaces the stub when it loads).
"""
from __future__ import annotations

import sys
import time
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

import store  # noqa: E402
from bootapp import browser_sign_in, booted  # noqa: E402
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


def _rows(drv):
    """``[(sample id, lab text, instrument tag, review text)]`` of the list."""
    return drv.execute_script("""
        return [...document.querySelectorAll('[data-testid=sample-row]')].map(r => [
            Number(r.dataset.sampleId),
            (r.querySelector('.lab') || {}).textContent || '',
            (r.querySelector('.l1 .tag') || {}).textContent || '',
            (r.querySelector('[data-testid=row-review]') || {}).textContent || '']);
    """)


def _ready(drv):
    return drv.execute_script("return !!(window.GCSamples && GCSamples.ready);")


def test_two_instruments_same_lab_id_are_distinguishable_and_filterable(tmp_path):
    hub = build_hub(tmp_path)
    store.instruments.upsert({"id": "gc2", "name": "GC-2 FID"}, db=hub.db)
    # 40304 on gc2 too (gc2 has no calibration: it is held, which is fine here)
    twin = hub.submit("twin", hub._cdf("sample", name="40304",
                                       injected=datetime(2026, 9, 26, 9, 0, 0),
                                       shift=0.07, method_name=SIMDIS), instrument="gc2")
    store.samples.update(hub.ids["final"], review_note="an earlier-injected blank arrived late",
                         db=hub.db)
    gc1_name = store.instruments.get("gc1", db=hub.db)["name"] or "gc1"

    with booted(tmp_path) as (port, _proc, _data, _home):
        drv = _driver()
        browser_sign_in(drv, port)
        try:
            drv.get(f"http://127.0.0.1:{port}/samples")
            assert _wait(lambda: _ready(drv) and len(_rows(drv)) >= 8), _rows(drv)
            # the instrument names arrive from /api/instruments
            assert _wait(lambda: any(r[2] == "GC-2 FID" for r in _rows(drv))), _rows(drv)
            rows = _rows(drv)
            twins = {r[2] for r in rows if r[1].startswith("40304")}
            assert {gc1_name, "GC-2 FID"} <= twins, rows
            assert all(r[2] for r in rows), rows                # every row names its GC
            final = [r for r in rows if r[0] == hub.ids["final"]][0]
            assert "blank arrived late" in final[3], final      # the review note shows

            # the chips: All + each instrument
            chips = drv.execute_script("return [...document.querySelectorAll('#inst-chips .chip')]"
                                       ".map(c => c.textContent);")
            assert chips[0] == "All instruments" and "GC-2 FID" in chips, chips

            # filter to gc2: only gc2's samples, the twin among them
            drv.execute_script("document.querySelector('[data-testid=chip-inst-gc2]').click();")
            assert _wait(lambda: "instrument=gc2" in drv.current_url)
            assert _wait(lambda: _rows(drv) and all(r[2] == "GC-2 FID" for r in _rows(drv))), \
                _rows(drv)
            ids = [r[0] for r in _rows(drv)]
            assert twin in ids and hub.ids["final"] not in ids

            # the filter is in the address: a reload keeps it
            drv.refresh()
            assert _wait(lambda: _ready(drv) and drv.execute_script(
                "return document.querySelector('[data-testid=chip-inst-gc2]').getAttribute('aria-pressed');")
                == "true")
            assert _wait(lambda: _rows(drv) and all(r[2] == "GC-2 FID" for r in _rows(drv))), \
                _rows(drv)

            # back to All: both instruments again
            drv.execute_script("document.querySelector('[data-testid=chip-inst-all]').click();")
            assert _wait(lambda: {gc1_name, "GC-2 FID"} <= {r[2] for r in _rows(drv)}), _rows(drv)
        finally:
            drv.quit()
