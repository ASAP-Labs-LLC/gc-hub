"""v5.0.0 lane S: the Samples page's routes and its small read-only server
additions, booted against a hub with real samples (``tests/hub_boot.py``).

* ``/``, ``/samples`` and ``/samples/<id>[/compare|/data]`` render the new
  Samples page; ``/classic`` the old page; ``/?open=settings|help`` (the old
  user-menu links) go to ``/classic?open=…``; ``/lab/<id>`` lands on
  ``/samples/<its newest run>``;
* ``GET /api/files/ids`` answers the ids ``/api/files`` would list for the
  same filters (the "Select all N matching this filter" source), capped;
* ``/api/files`` ``notsent=1`` = final results not uploaded to QBench, and
  its rows carry ``qbench_uploaded_at``;
* ``GET /api/samples/<id>/cdf`` downloads the stored CDF.
"""
from __future__ import annotations

import http.client
import shutil
import sys
import tempfile
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


def test_the_classic_page_moved_to_classic(hub_app):
    port, _hub, _ = hub_app
    html = get_text(port, "/classic")
    assert 'id="dash-file-list"' in html and "js/app.js" in html
    assert 'data-testid="samples-page"' not in html


@pytest.mark.parametrize("what", ["settings", "help"])
def test_the_old_open_links_go_to_the_classic_modals(hub_app, what):
    port, _hub, _ = hub_app
    code, headers, _ = _raw(port, f"/?open={what}")
    assert code == 302 and headers["Location"].endswith(f"/classic?open={what}")


def test_a_lab_link_lands_on_its_newest_run(hub_app):
    port, hub, _ = hub_app
    code, headers, _ = _raw(port, "/lab/40304")
    assert code == 302 and headers["Location"].endswith(f"/samples/{hub.ids['rerun']}")
    code, _h, body = _raw(port, "/lab/nope-404")
    assert code == 404 and b"No GC result" in body


def test_classic_links_still_open_the_classic_page(hub_app):
    port, hub, _ = hub_app
    for path in (f"/classic/samples/{hub.ids['final']}/data", "/classic/lab/40304"):
        html = get_text(port, path)
        assert 'id="dash-file-list"' in html, path


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
