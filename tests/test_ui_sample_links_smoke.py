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
from ui_wait import click_when_ready, wait_for  # noqa: E402
import store  # noqa: E402

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
    assert wait_for(drv, lambda: drv.execute_script("return !!(window.DeepLink && DeepLink.applied)")), \
        drv.execute_script("return document.body.innerText.slice(0, 500)")


def test_a_lab_link_selects_its_newest_run_and_lists_the_other(page):
    drv, base, hub = page
    _open(drv, f"{base}/lab/40304")
    assert drv.current_url == f"{base}/lab/40304"
    newest, older = hub.ids["rerun"], hub.ids["final"]
    assert wait_for(drv, lambda: drv.execute_script(SELECTED) == [newest, "tab-dashboard", newest]), \
        drv.execute_script(SELECTED)
    runs = drv.find_element("css selector", '[data-testid="other-runs"]')
    assert "Other runs of 40304:" in runs.text
    links = [a.get_attribute("href") for a in runs.find_elements("css selector", "a")]
    assert links == [f"{base}/samples/{older}"]


def test_a_lowercase_encoded_lab_link_works_too(page):
    drv, base, hub = page
    _open(drv, f"{base}/lab/%34%30%32%39%38")          # 40298, percent-encoded
    sid = hub.ids["released"]
    assert wait_for(drv, lambda: drv.execute_script(SELECTED)[2] == sid)


def test_copy_link_uses_the_hub_address_not_the_page_origin(page):
    drv, base, hub = page
    sid = hub.ids["final"]
    _open(drv, f"{base}/samples/{sid}")
    assert wait_for(drv, lambda: drv.execute_script(SELECTED) == [sid, "tab-dashboard", sid])
    assert click_when_ready(drv, "#btn-copy-link")
    want = f"https://gc.asaplabs.net/samples/{sid}"
    assert wait_for(drv, lambda: drv.execute_script("return window.__clip") == want), \
        drv.execute_script("return [window.__clip, DeepLink.lastCopied]")

    # the sample context menu's Copy link
    drv.execute_script("window.__clip = null;")
    other = hub.ids["backfill"]
    assert click_when_ready(drv, f'#dash-file-list li[data-sample-id="{other}"]', context=True)
    item = drv.find_element("id", "ctx-copy-link")
    assert item.is_displayed() and item.text == "Copy link"
    assert click_when_ready(drv, "#ctx-copy-link")
    assert wait_for(drv, lambda: drv.execute_script("return window.__clip")
                 == f"https://gc.asaplabs.net/samples/{other}")


def test_a_compare_link_opens_analysis_with_the_standard(page):
    drv, base, hub = page
    sid = hub.ids["final"]
    _open(drv, f"{base}/samples/{sid}/compare?standard=diesel")
    assert wait_for(drv, lambda: drv.execute_script(SELECTED) == [sid, "tab-analysis", sid]), \
        drv.execute_script(SELECTED)
    assert drv.execute_script("return state.selectedStandard && state.selectedStandard.name") \
        == "Diesel"


def test_a_data_link_on_a_filtered_out_instrument(page):
    drv, base, hub = page
    # this browser remembers the gc1 filter; the linked run is on gc2
    drv.execute_script("localStorage.setItem('gc-hub.listInstrument', 'gc1')")
    sid = hub.ids["held"]
    _open(drv, f"{base}/samples/{sid}/data")
    assert wait_for(drv, lambda: drv.execute_script(SELECTED) == [sid, "tab-distilldata", sid]), \
        drv.execute_script(SELECTED)
    assert drv.execute_script("return localStorage.getItem('gc-hub.listInstrument')") == "gc1"
    # "All" is really all: clearing the link's search shows both instruments
    assert drv.execute_script("return document.getElementById('instrument-filter').value") == ""
    drv.execute_script("const s = document.getElementById('universal-search');"
                       " s.value = ''; onSearchInput();")
    both = {hub.ids["final"], sid}
    shown = wait_for(drv, lambda: both <= set(drv.execute_script(
        "return [...document.querySelectorAll('#dash-file-list li')]"
        ".map(li => Number(li.dataset.sampleId))")))
    assert shown, drv.execute_script("return state.files.map(f => f.instrument)")
    drv.execute_script("localStorage.removeItem('gc-hub.listInstrument')")


LINKED_ROW = """
const tr = document.querySelector('#distill-table-body tr.linked-row');
if (!tr) return null;
const wrap = document.querySelector('#distill-table-wrap .panel-body').getBoundingClientRect();
const r = tr.getBoundingClientRect();
return [Number(tr.dataset.sampleId), r.top >= wrap.top - 1 && r.bottom <= wrap.bottom + 1,
        document.querySelectorAll('#distill-table-body tr.linked-row').length];
"""


def test_a_data_link_highlights_and_scrolls_to_its_row(page):
    drv, base, hub = page
    sid = hub.ids["released"]
    _open(drv, f"{base}/samples/{sid}/data")
    assert wait_for(drv, lambda: drv.execute_script(LINKED_ROW) == [sid, True, 1]), \
        drv.execute_script(LINKED_ROW)
    # a later /data link moves the highlight
    other = hub.ids["rerun"]
    _open(drv, f"{base}/samples/{other}/data")
    assert wait_for(drv, lambda: drv.execute_script(LINKED_ROW) == [other, True, 1]), \
        drv.execute_script(LINKED_ROW)


def test_an_unknown_lab_id_shows_the_friendly_page(page):
    drv, base, _hub = page
    drv.get(f"{base}/lab/99999")
    box = drv.find_element("css selector", '[data-testid="link-not-found"]')
    assert "No GC result for lab ID 99999 yet" in box.text
    box.find_element("link text", "Search the samples").click()
    assert wait_for(drv, lambda: drv.execute_script(
        "return document.getElementById('universal-search').value") == "99999")


# -- a hub with more samples than the list loads -------------------------------

@pytest.fixture(scope="module")
def crowded(tmp_path_factory):
    """5,100 newer runs whose lab IDs contain "40304" (so a search for it is
    crowded too) push the 40304 runs off the list's first page."""
    tmp = tmp_path_factory.mktemp("links-crowded")
    hub = build_hub(tmp)
    with store.connection(hub.db) as conn:
        with store.write_txn(conn):
            conn.executemany(
                "INSERT INTO samples(instrument_id, lab_id, injection_dt, injection_dt_source, "
                "status, received_at) VALUES ('gc1', ?, ?, 'csv', 'final', '2026-09-28')",
                [(f"40304-F{i:04d}",
                  f"2026-09-{27 + i // 1440} {i // 60 % 24:02d}:{i % 60:02d}:00")
                 for i in range(5100)])
    with booted(tmp) as (port, _proc, _data, _home):
        drv = _driver()
        try:
            browser_sign_in(drv, port)
            yield drv, f"http://127.0.0.1:{port}", hub
        finally:
            drv.quit()


def test_a_sample_link_beyond_the_loaded_page(crowded):
    drv, base, hub = crowded
    sid = hub.ids["final"]
    _open(drv, f"{base}/samples/{sid}")
    assert wait_for(drv, lambda: drv.execute_script(SELECTED) == [sid, "tab-dashboard", sid],
                    30), drv.execute_script(SELECTED)


def test_a_lab_link_beyond_the_loaded_page(crowded):
    drv, base, hub = crowded
    _open(drv, f"{base}/lab/40304")
    sid = hub.ids["rerun"]
    assert wait_for(drv, lambda: drv.execute_script(SELECTED) == [sid, "tab-dashboard", sid],
                    30), drv.execute_script(SELECTED)
