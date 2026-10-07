"""v5.0.0 lane S: the Samples page's routes and its small read-only server
additions, booted against a hub with real samples (``tests/hub_boot.py``).

* ``/``, ``/samples`` and ``/samples/<id>[/compare|/data]`` render the new
  Samples page; ``/classic…`` (v6.0.0: the old page is gone) redirect
  there; ``/?open=settings|help`` (the old
  user-menu links) go to ``/settings`` and ``/help``; ``/lab/<id>`` lands on
  ``/samples/<its newest run>``;
* ``GET /api/files/ids`` answers the ids ``/api/files`` would list for the
  same filters (the "Select all N matching this filter" source), capped;
* ``/api/files`` ``notsent=1`` = final results not uploaded to QBench, and
  its rows carry ``qbench_uploaded_at``;
* ``GET /api/samples/<id>/cdf`` downloads the stored CDF.
"""
from __future__ import annotations

import http.client
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

try:
    import flask, netCDF4, scipy  # noqa: F401,E401
    HAVE_DEPS = True
except Exception:  # pragma: no cover
    HAVE_DEPS = False

pytestmark = pytest.mark.skipif(not HAVE_DEPS, reason="needs flask, netCDF4, scipy")

from bootapp import booted, cookie_header, get, get_text  # noqa: E402


@pytest.fixture(scope="module")
def hub_app():
    import hub_boot
    import store
    tmp = Path(tempfile.mkdtemp(prefix="gc-samples-"))
    try:
        hub = hub_boot.build_hub(tmp)
        with booted(tmp) as (port, _proc, _data, _home):
            yield port, hub, store
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _raw(port, path):
    """(status, headers, body bytes) without following a redirect."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("GET", path, headers=cookie_header(port))
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


# ── pages ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/samples", "/samples?status=held&q=40", "/samples/{final}",
                                  "/samples/{final}/compare?standard=Diesel", "/samples/{final}/data"])
def test_the_samples_page_is_served_at_its_urls(hub_app, path):
    port, hub, _ = hub_app
    html = get_text(port, path.format(final=hub.ids["final"]))
    assert 'data-testid="samples-page"' in html
    assert 'id="app-version"' in html and 'id="sidebar"' in html
    assert "js/samples_router.js" in html and "js/samples_page.js" in html
    assert 'data-nav="samples"' in html


def test_the_classic_page_is_gone_and_its_address_opens_the_samples_page(hub_app):
    """v6.0.0: ``/classic`` (with its query) redirects to ``/``."""
    port, _hub, _ = hub_app
    code, headers, _ = _raw(port, "/classic")
    assert code == 302 and headers["Location"].endswith("/") and "?" not in headers["Location"]
    code, headers, _ = _raw(port, "/classic?q=40304")
    assert code == 302 and headers["Location"].endswith("/?q=40304")
    html = get_text(port, "/classic")
    assert 'data-testid="samples-page"' in html and "js/app.js" not in html


@pytest.mark.parametrize("what", ["settings", "help"])
def test_the_old_open_links_go_to_the_settings_and_help_pages(hub_app, what):
    port, _hub, _ = hub_app
    code, headers, _ = _raw(port, f"/?open={what}")
    assert code == 302 and headers["Location"].endswith(f"/{what}")


def test_a_lab_link_lands_on_its_newest_run(hub_app):
    port, hub, _ = hub_app
    code, headers, _ = _raw(port, "/lab/40304")
    assert code == 302 and headers["Location"].endswith(f"/samples/{hub.ids['rerun']}")
    code, _h, body = _raw(port, "/lab/nope-404")
    assert code == 404 and b"No GC result" in body


def test_old_classic_links_open_the_same_run_on_the_samples_page(hub_app):
    port, hub, _ = hub_app
    final = hub.ids["final"]
    code, headers, _ = _raw(port, f"/classic/samples/{final}/data")
    assert code == 302 and headers["Location"].endswith(f"/samples/{final}/data")
    code, headers, _ = _raw(port, f"/classic/samples/{final}/compare?standard=Diesel")
    assert code == 302 and headers["Location"].endswith(f"/samples/{final}/compare?standard=Diesel")
    code, headers, _ = _raw(port, "/classic/lab/40304")
    assert code == 302 and headers["Location"].endswith("/lab/40304")
    for path in (f"/classic/samples/{final}/data", "/classic/lab/40304"):
        html = get_text(port, path)       # urllib follows both hops
        assert 'data-testid="samples-page"' in html, path


def test_the_classic_only_routes_are_gone(hub_app):
    """v6.0.0: Comparison Export, the chromatogram PDF and the classic
    reprocess preview/progress went with the classic page (JSON 404)."""
    from bootapp import send
    port, hub, _ = hub_app
    for path, body in (("/api/export-comparison", {"sample_ids": [hub.ids["final"]]}),
                       ("/api/export-pdf", {"sample_id": hub.ids["final"]}),
                       ("/api/reprocess/preview", {"query": "40304", "instrument": "gc1"})):
        status, answer = send(port, path, json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"})
        assert status == 404 and answer["error"], (path, status, answer)
    assert get(port, f"/api/reprocess/status?sample_ids={hub.ids['final']}")[0] == 404
    for rel in ("templates/index.html", "static/js/app.js", "templates/instruments.html",
                "static/js/instruments.js"):
        assert not (ROOT / rel).exists(), rel


# ── /api/files/ids ─────────────────────────────────────────────────────────

def test_files_ids_match_the_list_for_the_same_filters(hub_app):
    port, hub, _ = hub_app
    for query in ("", "?instrument=gc1", "?status=awaiting_calibration,other_method", "?q=4030",
                  "?instrument=gc2&status=final"):
        code, listed = get(port, "/api/files" + query + ("&" if query else "?") + "limit=5000")
        assert code == 200, listed
        code, ids = get(port, "/api/files/ids" + query)
        assert code == 200, ids
        assert ids["ids"] == [s["sample_id"] for s in listed["samples"]], query
        assert ids["total"] == listed["total"] and ids["capped"] is False


def test_files_ids_are_read_only_and_refuse_a_bad_date(hub_app):
    port, _hub, _ = hub_app
    code, body = get(port, "/api/files/ids?date_from=not-a-date")
    assert code == 400 and "error" in body


def test_not_sent_to_qbench_is_final_and_never_uploaded(hub_app):
    port, hub, store = hub_app
    sent = hub.ids["final"]
    store.samples.update(sent, qbench_uploaded_at=store.now_iso(), qbench_revision=1, db=hub.db)
    code, body = get(port, "/api/files?notsent=1&limit=5000")
    assert code == 200
    ids = {s["sample_id"] for s in body["samples"]}
    assert sent not in ids
    assert all(s["status"] == "final" and s["qbench_uploaded_at"] is None for s in body["samples"])
    assert hub.ids["rerun"] in ids and hub.ids["held"] not in ids
    code, only = get(port, "/api/files/ids?notsent=1")
    assert only["ids"] == [s["sample_id"] for s in body["samples"]]
    code, one = get(port, f"/api/files?ids={sent}")
    assert one["samples"][0]["qbench_uploaded_at"]


# ── /api/samples/<id>/cdf ──────────────────────────────────────────────────

def test_the_cdf_downloads(hub_app):
    port, hub, _ = hub_app
    code, headers, body = _raw(port, f"/api/samples/{hub.ids['final']}/cdf")
    assert code == 200 and len(body) > 1000
    assert body[:3] == b"CDF" or body[:4] == b"\x89HDF"
    assert "attachment" in headers["Content-Disposition"] and "40304" in headers["Content-Disposition"]
    assert get(port, f"/api/samples/{hub.ids['resultonly']}/cdf")[0] == 404
    assert get(port, "/api/samples/999999/cdf")[0] == 404


# ── /api/comparison-standards/<name>/trace (v6.0.0) ──────────────────────

def test_a_standards_trace_is_shaped_like_a_samples(hub_app):
    """The Samples page stacks a comparison standard under the open sample:
    its trace comes in the shape of ``/api/samples/<id>/trace``."""
    import urllib.parse
    port, hub, _ = hub_app
    code, sample = get(port, f"/api/samples/{hub.ids['final']}/trace", timeout=10)
    assert code == 200
    code, std = get(port, "/api/comparison-standards/Diesel/trace", timeout=10)
    assert code == 200, std
    assert set(sample) <= set(std) | {"sample_id"}, (set(sample), set(std))
    assert std["name"] == "Diesel" and std["standard"] is True and std["sample_id"] is None
    assert len(std["x"]) == len(std["y"]) > 100
    assert std["cal_times"] == [] and std["cal_carbons"] == []
    # the standard is a copy of the final sample's CDF (hub_boot): the same numbers
    assert std["x"] == sample["x"] and std["y"] == sample["y"]
    # a name with spaces and '#' (Diesel #2) arrives decoded once
    shutil.copy2(hub.standards / "Diesel.CDF", hub.standards / "Diesel #2.CDF")
    code, d2 = get(port, "/api/comparison-standards/" + urllib.parse.quote("Diesel #2", safe="") + "/trace", timeout=10)
    assert code == 200 and d2["name"] == "Diesel #2"


def test_a_standards_trace_refuses_unknown_and_bad_names_and_needs_a_session(hub_app):
    port, _hub, _ = hub_app
    code, body = get(port, "/api/comparison-standards/R99/trace")
    assert code == 404 and "not found" in body["error"]
    for bad in (".hidden", "a%3Ab", "..%5Cgc"):
        code, body = get(port, f"/api/comparison-standards/{bad}/trace")
        assert code in (400, 404) and "error" in body, (bad, code, body)
    code, body = get(port, "/api/comparison-standards/.hidden/trace")
    assert code == 400
    code, body = get(port, "/api/comparison-standards/Diesel/trace", auth=False)
    assert code == 401


# ── a JSON body that is not an object ──────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/reprocess", "/api/export-lims", "/api/analysis",
                                  "/api/best-fit", "/api/export-analysis-report", "/api/export-analysis-reports-zip",
                                  "/api/qbench-skip-item", "/api/qbench-upload"])
@pytest.mark.parametrize("raw", [b"[1]", b'"x"', b"5", b"true"])
def test_a_json_body_that_is_not_an_object_is_a_400_not_a_500(hub_app, path, raw):
    """The bulk bar's and Compare's POST routes read ``body.get``: a JSON
    array, string, number or boolean is the caller's mistake (400 with a
    JSON error), not an unhandled error (500)."""
    from bootapp import send
    port, _hub, _store = hub_app
    status, body = send(port, path, raw, headers={"Content-Type": "application/json"})
    assert status == 400, (path, raw, status, body)
    assert "JSON object" in body["error"], body


def test_a_skip_with_a_non_integer_index_is_a_400(hub_app):
    from bootapp import send
    port, _hub, _store = hub_app
    for idx in ("abc", 1.5, True, [1]):
        status, body = send(port, "/api/qbench-skip-item", json.dumps({"idx": idx}).encode(),
                            headers={"Content-Type": "application/json"})
        assert status == 400 and "integer" in body["error"], (idx, status, body)
