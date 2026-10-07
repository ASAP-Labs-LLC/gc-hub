"""v3.1 sendable links in headless Chrome, on the Samples page (v5.0.0), signed
in, against a hub with real samples (``tests/hub_boot.py``): ``/lab/<lab_id>``
opens the lab ID's newest run and lists its other runs; ``/samples/<id>
[/compare|/data]`` open that run in that view (``?standard=`` kept); the
classic page's old links (``/classic/...``, v6.0.0: the classic page is gone)
land on the same run and view; a link to a run beyond the list's first page
still opens it; an unknown lab ID gets the friendly page. "Copy link" is
covered by ``test_ui_samples_smoke``.

Skipped when selenium or a Chrome/chromedriver can't be started. Plotly is
stubbed before the page loads.
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
from ui_wait import wait_for  # noqa: E402
import store  # noqa: E402

PRELOAD = """
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
    drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": PRELOAD})
    return drv


# [sample id the page opened, its view, the address bar, the detail's lab ID]
OPENED = """
const r = window.GCSamples.state.route;
const lab = document.getElementById('d-lab');
return [r.sampleId, r.view, location.pathname + location.search, lab ? lab.textContent : null];
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
    assert wait_for(drv, lambda: drv.execute_script("return !!(window.GCSamples && GCSamples.ready)")), \
        drv.execute_script("return document.body.innerText.slice(0, 500)")


def _opened(drv, sid, view, path, lab, timeout=20):
    """The page opened run ``sid`` in ``view`` at ``path`` (case-insensitive:
    Compare rewrites ``?standard=`` to the standard's own spelling) and its
    detail names ``lab`` (a rerun reads "40304 (2)")."""
    def ok():
        got = drv.execute_script(OPENED)
        return (got[:2] == [sid, view] and got[2].lower() == path.lower()
                and (got[3] or "").split(" (")[0] == lab)
    assert wait_for(drv, ok, timeout), (drv.execute_script(OPENED), [sid, view, path, lab])


@pytest.mark.parametrize("prefix", ["", "/classic"])
def test_a_lab_link_opens_its_newest_run_and_lists_the_other(page, prefix):
    drv, base, hub = page
    _open(drv, f"{base}{prefix}/lab/40304")
    rerun, final = hub.ids["rerun"], hub.ids["final"]
    _opened(drv, rerun, "overview", f"/samples/{rerun}", "40304")
    assert wait_for(drv, lambda: not drv.execute_script("return document.getElementById('other-runs').hidden;"))
    hrefs = drv.execute_script("return [...document.querySelectorAll('#runs-list a')].map(a => a.getAttribute('href'));")
    assert any(h.startswith(f"/samples/{final}") for h in hrefs), hrefs


def test_a_lowercase_encoded_lab_link_works_too(page):
    drv, base, hub = page
    _open(drv, f"{base}/lab/%34%30%32%39%38")          # 40298, percent-encoded
    sid = hub.ids["released"]
    _opened(drv, sid, "overview", f"/samples/{sid}", "40298")


@pytest.mark.parametrize("prefix", ["", "/classic"])
@pytest.mark.parametrize("suffix,view", [("", "overview"), ("/compare?standard=diesel", "compare"),
                                         ("/data", "data")])
def test_a_sample_link_opens_that_run_in_that_view(page, prefix, suffix, view):
    drv, base, hub = page
    sid = hub.ids["final"]
    _open(drv, f"{base}{prefix}/samples/{sid}{suffix}")
    _opened(drv, sid, view, f"/samples/{sid}{suffix}", "40304")
    if view == "compare":
        # the standard picked by the link, under its own name
        assert drv.execute_script("return GCSamples.state.route.standard;") == "Diesel"


def test_an_unknown_lab_id_shows_the_friendly_page(page):
    drv, base, _hub = page
    for url in (f"{base}/lab/99999", f"{base}/classic/lab/99999"):
        drv.get(url)
        box = drv.find_element("css selector", '[data-testid="link-not-found"]')
        assert "No GC result for lab ID 99999 yet" in box.text
    box.find_element("link text", "Search the samples").click()
    # the search link opens the Samples page with its lab-ID filter set
    assert wait_for(drv, lambda: drv.execute_script(
        "const q = document.getElementById('filter-q'); return q && q.value") == "99999")


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
    _opened(drv, sid, "overview", f"/samples/{sid}", "40304", timeout=30)


def test_a_lab_link_beyond_the_loaded_page(crowded):
    drv, base, hub = crowded
    _open(drv, f"{base}/classic/lab/40304")
    sid = hub.ids["rerun"]
    _opened(drv, sid, "overview", f"/samples/{sid}", "40304", timeout=30)
