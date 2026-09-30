"""v3.0.1: the analysis-report ZIP is built in the background.

Through https://gc.asaplabs.net, Cloudflare ends a request after 100 s (HTTP
524), and the ZIP built one PDF per queued sample in the request (seconds
each). Now ``POST /api/export-analysis-reports-zip`` answers 202 ``{job}`` at
once; ``GET /api/export-analysis-reports-zip/<job_id>`` is the job (polled by
the page), and ``GET .../<job_id>/download`` streams the finished ZIP once
(``download_jobs``). Booted for real (``tests/bootapp.py`` over a
``tests/hub_boot.py`` hub with a final sample and the Diesel standard).
"""
from __future__ import annotations

import io
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

try:
    import flask, netCDF4, scipy  # noqa: F401,E401
    HAVE_DEPS = True
except Exception:  # pragma: no cover
    HAVE_DEPS = False

pytestmark = pytest.mark.skipif(not HAVE_DEPS, reason="needs flask, netCDF4, scipy")

from bootapp import booted, cookie_header, get, post  # noqa: E402

UNKNOWN = 999999
ZIP = "/api/export-analysis-reports-zip"


@pytest.fixture(scope="module")
def hub_app():
    import hub_boot
    tmp = Path(tempfile.mkdtemp(prefix="gc-zipjob-"))
    try:
        hub = hub_boot.build_hub(tmp)
        with booted(tmp) as (port, _proc, data, _home):
            yield port, hub, data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _finished(port, job_id, timeout=180):
    deadline = time.time() + timeout
    while True:
        code, body = get(port, f"{ZIP}/{job_id}")
        assert code == 200, body
        if body["job"]["state"] != "running":
            return body["job"]
        assert time.time() < deadline, body
        time.sleep(0.1)


def _fetch(port, path, *, auth=True):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=cookie_header(port) if auth else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_bad_requests_are_answered_at_once(hub_app):
    port, _hub, _data = hub_app
    assert post(port, ZIP, {})[0] == 400
    assert post(port, ZIP, {"items": []})[0] == 400
    assert post(port, ZIP, {"items": "x"})[0] == 400
    assert post(port, ZIP, {"items": [1, 2]})[0] == 400


def test_the_zip_is_built_in_the_background_and_fetched_once(hub_app):
    port, hub, data = hub_app
    items = [{"sample_id": hub.ids["final"], "standard_name": "Diesel", "doc_name": "report"},
             {"sample_id": UNKNOWN, "standard_name": "Diesel"}]
    t0 = time.monotonic()
    code, body = post(port, ZIP, {"items": items})
    assert code == 202, body
    assert time.monotonic() - t0 < 5
    job = body["job"]
    assert job["kind"] == "reports-zip" and job["total"] == 2
    assert job["state"] in ("running", "done")
    assert job["download"] is None or job["state"] == "done"

    done = _finished(port, job["id"])
    assert done["state"] == "done", done
    assert done["result"] == {"written": 1, "skipped": 1}
    assert done["download"] == f"{ZIP}/{job['id']}/download"
    assert done["name"] == "analysis_reports.zip"

    # the download needs the session, like every route
    assert _fetch(port, done["download"], auth=False)[0] == 401
    code, headers, raw = _fetch(port, done["download"])
    assert code == 200, raw[:300]
    assert headers["Content-Type"] == "application/zip"
    assert "analysis_reports.zip" in headers["Content-Disposition"]
    assert "no-store" in headers["Cache-Control"]
    assert len(raw) == done["size"]
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        assert len(names) == 1 and names[0].endswith("_report.pdf"), names
        assert zf.read(names[0]).startswith(b"%PDF")
    # once only, and the file is gone from the server
    assert _fetch(port, done["download"])[0] == 404
    assert get(port, f"{ZIP}/{job['id']}")[1]["job"]["state"] == "fetched"
    tmp = data / "report-zips-tmp"
    assert not tmp.exists() or list(tmp.iterdir()) == []


def test_every_item_skipped_fails_the_job(hub_app):
    port, hub, _data = hub_app
    code, body = post(port, ZIP, {"items": [
        {"sample_id": UNKNOWN, "standard_name": "Diesel"},
        {"sample_id": hub.ids["final"], "standard_name": "NoSuchStd"}]})
    assert code == 202, body
    done = _finished(port, body["job"]["id"])
    assert done["state"] == "failed" and "skipped" in done["error"], done
    assert done["download"] is None
    assert _fetch(port, f"{ZIP}/{body['job']['id']}/download")[0] == 404


def test_unknown_jobs_are_404(hub_app):
    port, _hub, _data = hub_app
    assert get(port, f"{ZIP}/nope")[0] == 404
    assert _fetch(port, f"{ZIP}/nope/download")[0] == 404


# ── the page ────────────────────────────────────────────────────────────────

def _driver():
    webdriver = pytest.importorskip("selenium.webdriver")
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1400,1000",
                "--disable-dev-shm-usage"):
        opts.add_argument(arg)
    try:
        return webdriver.Chrome(options=opts)
    except Exception as exc:  # noqa: BLE001 - no Chrome / driver here
        pytest.skip(f"headless Chrome unavailable: {exc}")


def test_the_page_polls_the_job_then_fetches_the_zip(hub_app):
    """Export to PC with two queued reports: the page starts the job, polls it,
    then follows the one-time link (captured here instead of navigating)."""
    from bootapp import browser_sign_in
    port, hub, _data = hub_app
    drv = _driver()
    try:
        browser_sign_in(drv, port)
        drv.get(f"http://127.0.0.1:{port}/")
        deadline = time.time() + 30
        while drv.execute_script("return typeof exportToPC") != "function":
            assert time.time() < deadline
            time.sleep(0.2)
        drv.execute_script("""
            window.__clicked = [];
            HTMLAnchorElement.prototype.click = function () { window.__clicked.push(this.href); };
            state.analysisQueue = [
              {sample_id: arguments[0], lab_id: 'A', sample_name: 'report', standard_name: 'Diesel'},
              {sample_id: arguments[0], lab_id: 'B', sample_name: 'again', standard_name: 'Diesel'}];
            window.__done = exportToPC();
        """, hub.ids["final"])
        deadline = time.time() + 180
        while not drv.execute_script("return window.__clicked.length"):
            assert time.time() < deadline, drv.execute_script("return document.body.innerText")[-2000:]
            time.sleep(0.2)
        href = drv.execute_script("return window.__clicked[0]")
        assert href.startswith(f"http://127.0.0.1:{port}{ZIP}/") and href.endswith("/download"), href
        path = href[len(f"http://127.0.0.1:{port}"):]
        code, _headers, raw = _fetch(port, path)
        assert code == 200, raw[:300]
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            assert len(zf.namelist()) == 2      # both reports (same sample, two doc names)
    finally:
        drv.quit()
