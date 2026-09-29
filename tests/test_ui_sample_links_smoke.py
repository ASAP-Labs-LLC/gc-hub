"""v3.1 sendable links: a headless-Chrome smoke of ``/lab/<lab_id>`` and
``/samples/<id>[/compare|/data]`` on the classic page, signed in, against a
hub with real samples (``tests/hub_boot.py``): the linked sample is selected
on the right tab, a lab ID's other runs are listed, and "Copy link" copies
the hub address (https://gc.asaplabs.net/samples/<id>) although the page was
opened over a local address.

Skipped when selenium or a Chrome/chromedriver can't be started. Plotly is
stubbed before the page loads, and the clipboard is replaced by a recorder.
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

from bootapp import browser_sign_in, booted  # noqa: E402
from hub_boot import build_hub  # noqa: E402

PRELOAD = """
if (!window.Plotly) {
  window.Plotly = { react(){}, newPlot(){}, purge(){}, relayout(){}, restyle(){},
                    Plots: { resize(){} }, d3: null, __stub: true };
}
window.__clip = null;
try {
  Object.defineProperty(navigator, 'clipboard', { configurable: true,
    value: { writeText: async (t) => { window.__clip = t; } } });
} catch (e) {}
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
    drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": PRELOAD})
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


SELECTED = """
const li = document.querySelector('#dash-file-list li.selected');
const tab = document.querySelector('.tab-btn.active');
return [li ? Number(li.dataset.sampleId) : null, tab ? tab.dataset.tab : null,
        state.selectedFile ? state.selectedFile.sample_id : null];
"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("links-ui")
    hub = build_hub(tmp)
    with booted(tmp) as (port, _proc, _data, _home):
        drv = _driver()
        try:
            browser_sign_in(drv, port)
            yield drv, f"http://127.0.0.1:{port}", hub
        finally:
            drv.quit()


def _open(drv, url):
    drv.get(url)
    assert _wait(lambda: drv.execute_script("return !!(window.DeepLink && DeepLink.applied)")), \
        drv.execute_script("return document.body.innerText.slice(0, 500)")


def test_a_lab_link_selects_its_newest_run_and_lists_the_other(page):
    drv, base, hub = page
    _open(drv, f"{base}/lab/40304")
    assert drv.current_url == f"{base}/lab/40304"
    newest, older = hub.ids["rerun"], hub.ids["final"]
    assert _wait(lambda: drv.execute_script(SELECTED) == [newest, "tab-dashboard", newest]), \
        drv.execute_script(SELECTED)
    runs = drv.find_element("css selector", '[data-testid="other-runs"]')
    assert "Other runs of 40304:" in runs.text
    links = [a.get_attribute("href") for a in runs.find_elements("css selector", "a")]
    assert links == [f"{base}/samples/{older}"]


def test_a_lowercase_encoded_lab_link_works_too(page):
    drv, base, hub = page
    _open(drv, f"{base}/lab/%34%30%32%39%38")          # 40298, percent-encoded
    sid = hub.ids["released"]
    assert _wait(lambda: drv.execute_script(SELECTED)[2] == sid)


def test_copy_link_uses_the_hub_address_not_the_page_origin(page):
    drv, base, hub = page
    sid = hub.ids["final"]
    _open(drv, f"{base}/samples/{sid}")
    assert _wait(lambda: drv.execute_script(SELECTED) == [sid, "tab-dashboard", sid])
    drv.find_element("id", "btn-copy-link").click()
    want = f"https://gc.asaplabs.net/samples/{sid}"
    assert _wait(lambda: drv.execute_script("return window.__clip") == want), \
        drv.execute_script("return [window.__clip, DeepLink.lastCopied]")

    # the sample context menu's Copy link
    drv.execute_script("window.__clip = null;")
    other = hub.ids["backfill"]
    li = drv.find_element("css selector", f'#dash-file-list li[data-sample-id="{other}"]')
    webdriver.ActionChains(drv).context_click(li).perform()
    item = drv.find_element("id", "ctx-copy-link")
    assert item.is_displayed() and item.text == "Copy link"
    item.click()
    assert _wait(lambda: drv.execute_script("return window.__clip")
                 == f"https://gc.asaplabs.net/samples/{other}")


def test_a_compare_link_opens_analysis_with_the_standard(page):
    drv, base, hub = page
    sid = hub.ids["final"]
    _open(drv, f"{base}/samples/{sid}/compare?standard=diesel")
    assert _wait(lambda: drv.execute_script(SELECTED) == [sid, "tab-analysis", sid]), \
        drv.execute_script(SELECTED)
    assert drv.execute_script("return state.selectedStandard && state.selectedStandard.name") \
        == "Diesel"


def test_a_data_link_on_a_filtered_out_instrument(page):
    drv, base, hub = page
    # this browser remembers the gc1 filter; the linked run is on gc2
    drv.execute_script("localStorage.setItem('gc-hub.listInstrument', 'gc1')")
    sid = hub.ids["held"]
    _open(drv, f"{base}/samples/{sid}/data")
    assert _wait(lambda: drv.execute_script(SELECTED) == [sid, "tab-distilldata", sid]), \
        drv.execute_script(SELECTED)
    assert drv.execute_script("return localStorage.getItem('gc-hub.listInstrument')") == "gc1"
    drv.execute_script("localStorage.removeItem('gc-hub.listInstrument')")


def test_an_unknown_lab_id_shows_the_friendly_page(page):
    drv, base, _hub = page
    drv.get(f"{base}/lab/99999")
    box = drv.find_element("css selector", '[data-testid="link-not-found"]')
    assert "No GC result for lab ID 99999 yet" in box.text
    box.find_element("link text", "Search the samples").click()
    assert _wait(lambda: drv.execute_script(
        "return document.getElementById('universal-search').value") == "99999")
