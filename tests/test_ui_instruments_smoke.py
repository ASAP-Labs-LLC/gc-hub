"""v2.0.0 RC: a headless-Chrome smoke of the main page with two instruments
that hold the same lab ID. Each row names its instrument, the toolbar's
instrument select filters the list (and is remembered by the browser), a
review note shows as a badge, and the Re-process modal starts on the list's
instrument.

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
    """``[(name, instrument badge text, status badge text)]`` of the dashboard list."""
    return drv.execute_script("""
        return Array.from(document.querySelectorAll('#dash-file-list li[data-uid]')).map(li => [
            (li.querySelector('.file-item-name') || {}).textContent || '',
            (li.querySelector('.instrument-badge') || {}).textContent || '',
            (li.querySelector('.status-badge') || {}).textContent || '']);
    """)


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
            drv.get(f"http://127.0.0.1:{port}/classic")
            assert _wait(lambda: len(_rows(drv)) >= 8), _rows(drv)
            # the instrument names arrive from /api/instruments
            assert _wait(lambda: any(r[1] == "GC-2 FID" for r in _rows(drv))), _rows(drv)
            rows = _rows(drv)
            twins = {r[1] for r in rows if r[0].startswith("40304")}
            assert {gc1_name, "GC-2 FID"} <= twins, rows
            assert all(r[1] for r in rows), rows                # every row has a badge
            assert any(r[2] == "Review" for r in rows), rows      # the review note shows

            # the select lists All + each instrument
            opts = drv.execute_script(
                "return Array.from(document.querySelectorAll('#instrument-filter option'))"
                ".map(o => [o.value, o.textContent]);")
            assert opts[0] == ["", "All instruments"] and ["gc2", "GC-2 FID"] in opts, opts

            # filter to gc2: only gc2's samples, the twin among them
            drv.execute_script("const s = document.getElementById('instrument-filter');"
                               "s.value = 'gc2'; s.dispatchEvent(new Event('change'));")
            assert _wait(lambda: _rows(drv) and all(r[1] == "GC-2 FID" for r in _rows(drv))), \
                _rows(drv)
            assert any(r[0].startswith("40304") for r in _rows(drv))
            ids = drv.execute_script("return Array.from(document.querySelectorAll("
                                     "'#dash-file-list li[data-uid]')).map(li => li.dataset.sampleId);")
            assert str(twin) in ids and str(hub.ids["final"]) not in ids

            # the Re-process modal resolves lab IDs on the list's instrument
            drv.execute_script("openReprocessModal();")
            assert drv.execute_script(
                "return document.getElementById('reprocess-instrument').value;") == "gc2"

            # remembered by this browser
            drv.get(f"http://127.0.0.1:{port}/classic")
            assert _wait(lambda: drv.execute_script(
                "return document.getElementById('instrument-filter').value;") == "gc2")
            assert _wait(lambda: _rows(drv) and all(r[1] == "GC-2 FID" for r in _rows(drv))), \
                _rows(drv)

            # back to All: both instruments again
            drv.execute_script("const s = document.getElementById('instrument-filter');"
                               "s.value = ''; s.dispatchEvent(new Event('change'));")
            assert _wait(lambda: {gc1_name, "GC-2 FID"} <= {r[1] for r in _rows(drv)}), _rows(drv)
            # with more than one instrument and no filter, the modal makes you choose
            drv.execute_script("openReprocessModal();")
            assert drv.execute_script(
                "return document.getElementById('reprocess-instrument').value;") == ""
        finally:
            drv.quit()
