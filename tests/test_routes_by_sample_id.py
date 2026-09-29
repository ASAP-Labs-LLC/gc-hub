"""Boot tests for the T4 routes: samples addressed by ``sample_id``.

app.py is launched in a subprocess (``tests/bootapp.py``) against a hub data
folder built by ``tests/hub_boot.py`` (``instruments.startup``, real
``pipeline.submit`` + Worker runs), so every route reads a real store.
Covers: every migrated route by ``sample_id``, 404 for unknown ids, the
export/QBench gate refusals (backfill, non-final), the removed routes (JSON
404) and 503 with no store.
"""
from __future__ import annotations

import json
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

from bootapp import get_text, booted, get, post, send, wait_for  # noqa: E402

UNKNOWN = 999999


@pytest.fixture(scope="module")
def hub_app():
    import hub_boot
    import store
    tmp = Path(tempfile.mkdtemp(prefix="gc-t4-"))
    try:
        hub = hub_boot.build_hub(tmp)
        with booted(tmp) as (port, proc, data, home):
            assert data == hub.data
            yield port, hub, store
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── read routes ─────────────────────────────────────────────────────────────

def test_files_is_a_store_query(hub_app):
    port, hub, store = hub_app
    code, body = get(port, "/api/files")
    assert code == 200, body
    assert body["total"] == len(hub.ids)
    assert "gc1" in body["instruments"] and "gc2" in body["instruments"]
    by_id = {s["sample_id"]: s for s in body["samples"]}
    assert set(by_id) == set(hub.ids.values())
    final = by_id[hub.ids["final"]]
    for key in ("sample_id", "instrument", "lab_id", "injection_dt", "status", "flags", "best_fit",
                "backfill", "time_corrected", "method_name", "display_name", "error"):
        assert key in final, key
    assert final["lab_id"] == "40304" and final["status"] == "final" and final["instrument"] == "gc1"
    assert final["method_name"] == "SIMDISB.M"
    assert by_id[hub.ids["backfill"]]["backfill"] == 1
    held = by_id[hub.ids["held"]]
    assert held["status"] == "awaiting_calibration" and "calibration" in held["error"].lower()
    assert by_id[hub.ids["other"]]["status"] == "other_method"
    # re-runs of one lab ID get a run-order label, the first run the bare name
    assert final["display_name"] == "40304"
    assert by_id[hub.ids["rerun"]]["display_name"] == "40304 (2)"
    # newest injection first
    dts = [s["injection_dt"] for s in body["samples"]]
    assert dts == sorted(dts, reverse=True)


def test_files_filters_and_paging(hub_app):
    port, hub, _ = hub_app
    _, body = get(port, "/api/files?status=final")
    assert {s["status"] for s in body["samples"]} == {"final"}
    _, body = get(port, "/api/files?q=40304")
    assert {s["sample_id"] for s in body["samples"]} == {hub.ids["final"], hub.ids["rerun"]}
    _, body = get(port, "/api/files?instrument=gc2")
    assert [s["sample_id"] for s in body["samples"]] == [hub.ids["held"]]
    _, body = get(port, "/api/files?backfill=1")
    assert sorted(s["sample_id"] for s in body["samples"]) == sorted([hub.ids["backfill"], hub.ids["released"]])
    _, body = get(port, "/api/files?method=D7096.M")
    assert [s["sample_id"] for s in body["samples"]] == [hub.ids["other"]]
    _, body = get(port, "/api/files?date_from=2026-09-25&date_to=2026-09-25")
    assert hub.ids["backfill"] not in {s["sample_id"] for s in body["samples"]}
    code, body = get(port, "/api/files?limit=2&offset=1")
    assert code == 200 and len(body["samples"]) == 2 and body["total"] == len(hub.ids)
    assert (body["limit"], body["offset"]) == (2, 1)
    assert get(port, "/api/files?limit=x")[0] == 400


def test_files_never_reads_a_cdf(hub_app):
    port, hub, _ = hub_app
    cdf = hub.data / "cdf"
    moved = hub.data / "cdf-away"
    cdf.rename(moved)
    try:
        code, body = get(port, "/api/files")
    finally:
        moved.rename(cdf)
    assert code == 200, body
    assert len(body["samples"]) == len(hub.ids)


def test_flags_and_best_fit_are_cached_in_the_store(hub_app):
    port, hub, store = hub_app
    from bootapp import wait_for
    get(port, "/api/files")
    assert wait_for(lambda: get(port, "/api/files")[1]["cache_pending"] == 0, timeout=30)
    for sid in hub.ids.values():
        row = store.sample_cache.get(sid, db=hub.db)
        assert row is not None and row["rules_fingerprint"], sid
        assert isinstance(json.loads(row["flags"]), list)


def test_sample_metadata(hub_app):
    port, hub, store = hub_app
    sid = hub.ids["final"]
    code, body = get(port, f"/api/samples/{sid}/metadata")
    assert code == 200, body
    s = hub.sample("final")
    assert body["sample_id"] == sid and body["lab_id"] == "40304" and body["sample_name"] == "40304"
    assert body["injection_datetime"] == s["injection_dt"] == "2026-09-25 14:23:00"
    assert body["status"] == "final" and body["instrument"] == "gc1"
    assert body["current_revision"] == 1
    assert [r["revision"] for r in body["revisions"]] == [1]
    assert body["revisions"][0]["reason"] == "processed"
    assert get(port, f"/api/samples/{UNKNOWN}/metadata")[0] == 404


def test_sample_trace(hub_app):
    port, hub, _ = hub_app
    code, body = get(port, f"/api/samples/{hub.ids['final']}/trace")
    assert code == 200, body
    assert len(body["x"]) == len(body["y"]) > 1000
    assert body["name"] == "40304" and body["sample_id"] == hub.ids["final"]
    assert get(port, f"/api/samples/{UNKNOWN}/trace")[0] == 404


def test_distillation_curve_is_the_revisions(hub_app):
    port, hub, store = hub_app
    sid = hub.ids["final"]
    rev = store.get_revision(sid, db=hub.db)
    results = json.loads(rev["results"])
    code, body = get(port, f"/api/samples/{sid}/distillation-curve")
    assert code == 200, body
    assert body["revision"] == 1
    assert body["blank_used"] == hub.ids["blank"] == rev["blank_used"]
    assert body["calibration"]["anchors"] == json.loads(rev["calibration_used"])["anchors"]
    assert body["d2887"]["2887 T50"] == float(results["2887 T50"])
    assert body["d86"]["D86 T50"] == float(results["D86 T50"])
    unc = json.loads(rev["d86_uncorrected"])
    assert body["d86_uncorrected"]["D86 T50"] == float(unc["50%"])
    assert len(body["percent"]) == len(body["temperature"]) > 100
    # The curve at 50% recovered is the stored T50 (same calibration, same blank).
    import numpy as np
    pct, temp = np.array(body["percent"]), np.array(body["temperature"])
    assert abs(float(np.interp(50.0, pct, temp)) - float(results["2887 T50"])) < 0.05
    assert get(port, f"/api/samples/{UNKNOWN}/distillation-curve")[0] == 404
    code, body = get(port, f"/api/samples/{hub.ids['held']}/distillation-curve")
    assert code == 409 and "calibration" in body["error"].lower()


def test_distillation_curve_uses_the_revisions_own_blank_file(hub_app):
    # The blank sample's CDF can be swapped later (a conflict Replace); the
    # curve must still subtract the file the revision was computed with.
    port, hub, store = hub_app
    import numpy as np
    sid, blank_id = hub.ids["final"], hub.ids["blank"]
    results = json.loads(store.get_revision(sid, db=hub.db)["results"])
    original = hub.sample("blank")["cdf_path"]
    store.samples.update(blank_id, cdf_path=hub.sample("other")["cdf_path"], db=hub.db)
    try:
        code, body = get(port, f"/api/samples/{sid}/distillation-curve")
    finally:
        store.samples.update(blank_id, cdf_path=original, db=hub.db)
    assert code == 200, body
    pct, temp = np.array(body["percent"]), np.array(body["temperature"])
    assert abs(float(np.interp(50.0, pct, temp)) - float(results["2887 T50"])) < 0.05


def test_table_is_current_revisions(hub_app):
    port, hub, store = hub_app
    import distill
    code, body = get(port, "/api/table")
    assert code == 200, body
    assert body["columns"] == distill.CSV_HEADER
    with_results = [hub.ids[k] for k in ("blank", "final", "rerun", "backfill", "released", "slashed", "resultonly")]
    assert sorted(body["sample_ids"]) == sorted(with_results)
    row = body["rows"][body["sample_ids"].index(hub.ids["final"])]
    results = json.loads(store.get_revision(hub.ids["final"], db=hub.db)["results"])
    assert row[0] == "40304" and row[1] == "2026-09-25 14:23:00"
    assert row[distill.CSV_HEADER.index("2887 T50")] == str(results["2887 T50"])
    assert row[-1] == hub.sample("final")["cdf_path"]


def test_table_shows_the_samples_corrected_injection_time(tmp_path):
    # An imported (v1) revision keeps v1's cells verbatim as the record: its
    # InjectionDateTime may be the misparsed time (spec, Injection time) and
    # its numbers are strings. The table shows the sample's corrected
    # injection_dt and every other cell as stored.
    import distill
    import hub_boot
    import instruments
    import store
    h = hub_boot.Hub(tmp_path)
    store.migrate(h.db)
    instruments.bootstrap_gc1(h.conf, db=h.db)
    cells = {c: "" for c in distill.CSV_HEADER}
    cells.update({"Lab ID": "40305", "InjectionDateTime": "2026-09-25 02:45:00",
                  "2887 T50": "301.20", "D86 T50": "288.0", "Fit Score": "0.9500"})
    sid = store.samples.insert_received("gc1", "40305", "2026-09-25 00:24:50", "cdf",
                                        cdf_sha256=None, cdf_path=None, status="final",
                                        legacy_injection_dt="2026-09-25 02:45:00",
                                        time_corrected=1, db=h.db)
    with store.connection(h.db) as conn, store.write_txn(conn):
        store.add_revision(conn, sid, json.dumps(cells), reason="import", by="test")
    with booted(tmp_path) as (port, _proc, _data, _home):
        code, body = get(port, "/api/table")
        assert code == 200, body
        row = body["rows"][body["sample_ids"].index(sid)]
        col = distill.CSV_HEADER.index
        assert row[col("InjectionDateTime")] == "2026-09-25 00:24:50"
        assert row[col("Lab ID")] == "40305"
        assert row[col("2887 T50")] == "301.20" and row[col("Fit Score")] == "0.9500"
        # the stored record is untouched
        rev = json.loads(store.get_revision(sid, db=h.db)["results"])
        assert rev["InjectionDateTime"] == "2026-09-25 02:45:00"


# ── removed routes ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("POST", "/api/scan"), ("GET", "/api/scan/status"), ("GET", "/api/scan/stream"),
    ("POST", "/api/stop-scan"), ("POST", "/api/rebuild-db"),
    ("POST", "/api/library/reindex-times"), ("POST", "/api/files/refresh"),
    ("GET", "/api/trace?path=x"), ("GET", "/api/distillation-curve?path=x"),
    ("GET", "/api/metadata/some/file.CDF"),
])
def test_removed_routes_answer_json_404(hub_app, method, path):
    port, _hub, _ = hub_app
    code, body = (get(port, path) if method == "GET" else post(port, path, {}))
    assert code == 404
    assert body == {"error": "Not found"}


# ── actions ─────────────────────────────────────────────────────────────────

def test_export_lims_enforces_the_gate(hub_app):
    port, hub, store = hub_app
    assert post(port, "/api/export-lims", {})[0] == 400
    assert post(port, "/api/export-lims", {"sample_ids": [UNKNOWN]})[0] == 404

    code, body = post(port, "/api/export-lims", {"sample_ids": [hub.ids["backfill"]]})
    assert code == 409 and body["exported"] == []
    assert body["refused"][0]["sample_id"] == hub.ids["backfill"]
    code, body = post(port, "/api/export-lims", {"sample_ids": [hub.ids["other"]]})
    assert code == 409 and "other_method" in body["refused"][0]["error"]
    assert store.get_revision(hub.ids["backfill"], db=hub.db)["revision"] == 1

    before = len(store.export_rows.rows_after("gc1", 0, db=hub.db))
    code, body = post(port, "/api/export-lims",
                      {"sample_ids": [hub.ids["rerun"], hub.ids["backfill"]]})
    assert code == 200, body
    assert [e["sample_id"] for e in body["exported"]] == [hub.ids["rerun"]]
    assert [r["sample_id"] for r in body["refused"]] == [hub.ids["backfill"]]
    rev = store.get_revision(hub.ids["rerun"], db=hub.db)
    assert (rev["revision"], rev["reason"]) == (2, "export-lims")
    rows = store.export_rows.rows_after("gc1", 0, db=hub.db)
    assert len(rows) == before + 1 and rows[-1]["sample_id"] == hub.ids["rerun"]


def test_qbench_upload_refuses_ungated_and_unknown_samples(hub_app):
    port, hub, store = hub_app
    item = {"standard_name": "Diesel"}
    assert post(port, "/api/qbench-upload", {"queue": []})[0] == 400
    assert post(port, "/api/qbench-upload",
                {"queue": [dict(item, sample_id=UNKNOWN)]})[0] == 404
    for key in ("backfill", "other", "held"):
        code, body = post(port, "/api/qbench-upload",
                          {"queue": [dict(item, sample_id=hub.ids["final"]),
                                     dict(item, sample_id=hub.ids[key])]})
        assert code == 409, (key, body)
        assert [r["sample_id"] for r in body["refused"]] == [hub.ids[key]]
    assert get(port, "/api/qbench-upload-status")[1]["items"] == []
    assert hub.sample("final")["qbench_revision"] is None


def test_reprocess_by_sample_ids(hub_app):
    port, hub, store = hub_app
    assert post(port, "/api/reprocess", {"sample_ids": [UNKNOWN]})[0] == 404
    assert post(port, "/api/reprocess", {})[0] == 400
    code, body = post(port, "/api/reprocess", {"sample_ids": [hub.ids["final"]]})
    assert code == 200, body
    assert body["status"] == "queued" and body["count"] == 1
    assert body["sample_ids"] == [hub.ids["final"]]
    job = store.jobs.get(body["job_ids"][0], db=hub.db)
    assert job["payload"]["sample_id"] == hub.ids["final"]
    assert job["payload"]["reason"] == "reprocess"
    # the app's own Worker (hub.start) runs it; no worker in the test
    status = f"/api/reprocess/status?sample_ids={hub.ids['final']}"
    assert get(port, status)[1]["phase"] in ("processing", "done")
    assert wait_for(lambda: get(port, status)[1]["phase"] == "done", timeout=30)
    code, st = get(port, status)
    assert st["phase"] == "done" and st["processed"] == 1 and st["errors"] == 0
    assert st["samples"][0]["current_revision"] == 2
    assert get(port, "/api/reprocess/status")[1]["phase"] == "idle"
    assert get(port, f"/api/reprocess/status?sample_ids={UNKNOWN}")[0] == 404


def test_reprocess_lab_id_selection_needs_an_instrument(hub_app):
    port, hub, _ = hub_app
    code, body = post(port, "/api/reprocess/preview", {"query": "40299 to 40304"})
    assert code == 400 and "instrument" in body["error"]
    code, body = post(port, "/api/reprocess", {"query": "40304"})
    assert code == 400 and "instrument" in body["error"]
    code, body = post(port, "/api/reprocess/preview",
                      {"query": "40299 to 40301, 40304", "instrument": "gc1"})
    assert code == 200, body
    assert body["matched"] == ["40299", "40304"]
    assert body["missing"] == ["40300", "40301"]
    # the latest injection of each lab ID
    assert body["sample_ids"] == [hub.ids["backfill"], hub.ids["rerun"]]
    assert post(port, "/api/reprocess/preview", {"query": "1", "instrument": "nope"})[0] == 404


def test_reprocess_missing_ids_reach_the_notification_tray(hub_app):
    # (moved from the retired in-process test_reprocess_preview.py)
    port, hub, _ = hub_app
    code, body = post(port, "/api/reprocess/preview", {"query": "1 to 99999999", "instrument": "gc1"})
    assert code == 400 and "too large" in body["error"]
    post(port, "/api/notifications/dismiss-all", {})
    code, body = post(port, "/api/reprocess", {"sample_ids": [], "missing": ["34564", "34999"]})
    assert code == 200 and body["status"] == "no-match"
    notes = get(port, "/api/notifications")[1]
    assert any("not found" in n["message"] and "34564" in n["message"] for n in notes)
    note = notes[0]
    code, body = post(port, f"/api/notifications/{note['id']}/dismiss", {})
    assert code == 200 and body["removed"]


def test_analysis_and_best_fit_by_sample_id(hub_app):
    port, hub, _ = hub_app
    sid = hub.ids["final"]
    code, body = post(port, "/api/analysis", {"sample_id": sid, "standard_name": "Diesel"})
    assert code == 200, body
    assert len(body["trend"]["sample_x"]) > 1000
    # the ladder is the revision's calibration anchors
    assert body["cal_times"][:2] == [0.5, 0.8] and body["cal_carbons"][:2] == [5, 6]
    assert post(port, "/api/analysis", {"sample_id": UNKNOWN, "standard_name": "Diesel"})[0] == 404
    assert post(port, "/api/analysis", {"standard_name": "Diesel"})[0] == 400
    assert post(port, "/api/analysis", {"sample_id": "abc", "standard_name": "Diesel"})[0] == 400
    assert post(port, "/api/export-lims", {"sample_ids": ["abc"]})[0] == 400

    code, body = post(port, "/api/best-fit", {"sample_id": sid})
    assert code == 200, body
    assert body["recorded"]["revision"] == hub.sample("final")["current_revision"]
    assert post(port, "/api/best-fit", {"sample_id": UNKNOWN})[0] == 404


def test_report_routes_take_sample_ids(hub_app):
    port, hub, _ = hub_app
    for path, body in (("/api/export-analysis-report", {"sample_id": UNKNOWN, "standard_name": "Diesel"}),
                       ("/api/export-pdf", {"sample_id": UNKNOWN}),
                       ("/api/export-comparison", {"sample_ids": [UNKNOWN]})):
        assert post(port, path, body)[0] == 404, path
    for path in ("/api/export-analysis-report", "/api/export-pdf", "/api/export-comparison"):
        assert post(port, path, {"sample_path": "/x.CDF", "path": "/x.CDF",
                                 "sample_paths": ["/x.CDF"]})[0] == 400, path


def test_comparison_standard_from_a_sample(hub_app):
    port, hub, _ = hub_app
    pw = _admin(port, hub)
    code, body = post(port, "/api/comparison-standard",
                      {"sample_id": hub.ids["rerun"], "name": "Rerun", "password": pw})
    assert code == 200, body
    assert (hub.standards / "Rerun.CDF").is_file()
    assert post(port, "/api/comparison-standard",
                {"sample_id": UNKNOWN, "name": "X", "password": pw})[0] == 404


def test_calibration_reads_and_writes_the_gc1_row(hub_app):
    port, hub, store = hub_app
    code, body = get(port, "/api/calibration")
    assert code == 200, body
    assert body["cdf_path"] == str(hub.cal)
    row = store.instruments.get("gc1", db=hub.db)
    assert body["assignments"] == json.loads(row["calibration_assignments"])
    code, active = get(port, "/api/calibration/active")
    assert code == 200 and active["mode"] == "manual" and active["calibration_cdf"] == str(hub.cal)

    entries = body["assignments"]
    # admin-gated (2A1 T5): no password, no change
    code, refused = post(port, "/api/calibration", {"assignments": [], "sensitivity": 40})
    assert code == 403, refused
    assert json.loads(store.instruments.get("gc1", db=hub.db)["calibration_assignments"]) == entries
    code, _ = send(port, "/api/calibration", json.dumps({"assignments": entries}).encode(),
                   {"Content-Type": "text/plain"})
    assert code == 415
    pw = _admin(port, hub)
    code, saved = post(port, "/api/calibration",
                       {"assignments": entries, "sensitivity": 40, "password": pw})
    assert code == 200, saved
    assert saved["ok"] and saved["queued"] == 0
    row = store.instruments.get("gc1", db=hub.db)
    assert json.loads(row["calibration_assignments"]) == entries
    assert row["calibration_sensitivity"] == 40
    bad = [{"rt": 1.0, "carbon": 9}, {"rt": 2.0, "carbon": 7}]
    assert post(port, "/api/calibration", {"assignments": bad, "password": pw})[0] == 400
    assert json.loads(store.instruments.get("gc1", db=hub.db)["calibration_assignments"]) == entries


def test_calibration_save_queues_awaiting_calibration(hub_app):
    port, hub, store = hub_app
    # gc1 has nothing held; hold one sample the way the worker would.
    sid = hub.ids["rerun"]
    store.samples.set_status(sid, "awaiting_calibration", error="test hold", db=hub.db)
    try:
        _, body = get(port, "/api/calibration")
        code, saved = post(port, "/api/calibration",
                           {"assignments": body["assignments"], "password": _admin(port, hub)})
        assert code == 200 and saved["queued"] == 1
        assert any(j["sample_id"] == sid for j in store.jobs.list(db=hub.db))
        # the app's Worker processes it back to final
        assert wait_for(lambda: hub.sample("rerun")["status"] == "final", timeout=30)
    finally:
        hub.worker().run_until_idle()


def test_settings_save_needs_json_and_leaves_instrument_config_alone(hub_app):
    port, hub, store = hub_app
    before = store.instruments.get("gc1", db=hub.db)
    # text/plain with no Origin (not a browser): refused before anything is read
    code, _ = send(port, "/api/settings", json.dumps({"calibration_cdf": "/etc/hosts"}).encode(),
                   {"Content-Type": "text/plain"})
    assert code == 415
    _, conf = get(port, "/api/settings")
    for key, value in (("calibration_cdf", "/etc/hosts"),
                       ("correction_factors_json", "/tmp/other.json"),
                       ("calibration_sensitivity", "10"),
                       ("calibration_assignments", "{}")):
        code, body = post(port, "/api/settings", dict(conf, **{key: value}))
        assert code == 400 and key in body["error"], (key, body)
    after = store.instruments.get("gc1", db=hub.db)
    assert (after["calibration_cdf"], after["calibration_assignments"]) == \
        (before["calibration_cdf"], before["calibration_assignments"])
    on_disk = json.loads((hub.data / "settings.json").read_text())
    assert on_disk["correction_factors_json"] == hub.conf["correction_factors_json"]
    # echoing them back unchanged is fine, and other settings save
    code, saved = post(port, "/api/settings", dict(conf, series_colors="#123456"))
    assert code == 200, saved
    on_disk = json.loads((hub.data / "settings.json").read_text())
    assert on_disk["series_colors"] == "#123456"
    assert on_disk["correction_factors_json"] == hub.conf["correction_factors_json"]
    assert on_disk["calibration_assignments"] == hub.conf["calibration_assignments"]
    assert store.instruments.get("gc1", db=hub.db)["calibration_cdf"] == before["calibration_cdf"]


_ADMIN_PW = {}


def _admin(port, hub):
    """Set the booted hub's admin password once (first-use setup) and return it."""
    from bootapp import setup_admin
    if port not in _ADMIN_PW:
        _ADMIN_PW[port] = setup_admin(port, hub.data)
    return _ADMIN_PW[port]


# ── review fixes ────────────────────────────────────────────────────────────

HUGE = 2 ** 70


def _is_json_error(code, body, expected):
    return code == expected and isinstance(body, dict) and "error" in body


@pytest.mark.parametrize("path", [
    f"/api/samples/{HUGE}/metadata", f"/api/samples/{HUGE}/trace",
    f"/api/samples/{HUGE}/distillation-curve", f"/api/samples/{2 ** 63}/metadata",
    "/api/samples/0/metadata",
])
def test_out_of_range_path_ids_are_json_404(hub_app, path):
    port, _hub, _ = hub_app
    code, body = get(port, path)
    assert _is_json_error(code, body, 404), (code, body)


@pytest.mark.parametrize("path,body", [
    ("/api/export-lims", {"sample_ids": [HUGE]}),
    ("/api/export-lims", {"sample_ids": [0]}),
    ("/api/export-lims", {"sample_ids": [-3]}),
    ("/api/reprocess", {"sample_ids": [HUGE]}),
    ("/api/qbench-upload", {"queue": [{"sample_id": HUGE, "standard_name": "Diesel"}]}),
    ("/api/qbench-upload", {"queue": [{"sample_id": -1, "standard_name": "Diesel"}]}),
    ("/api/analysis", {"sample_id": HUGE, "standard_name": "Diesel"}),
    ("/api/best-fit", {"sample_id": 2 ** 63}),
    ("/api/export-analysis-report", {"sample_id": HUGE, "standard_name": "Diesel"}),
])
def test_out_of_range_body_ids_are_json_400(hub_app, path, body):
    port, _hub, _ = hub_app
    code, resp = post(port, path, body)
    assert _is_json_error(code, resp, 400), (code, resp)


def test_out_of_range_status_ids_are_json_400(hub_app):
    port, _hub, _ = hub_app
    code, body = get(port, f"/api/reprocess/status?sample_ids={HUGE}")
    assert _is_json_error(code, body, 400), (code, body)


def test_qbench_refuses_a_result_only_sample(hub_app):
    port, hub, _ = hub_app
    code, body = post(port, "/api/qbench-upload",
                      {"queue": [{"sample_id": hub.ids["resultonly"], "standard_name": "Diesel"}]})
    assert code == 409, body
    assert body["refused"][0]["sample_id"] == hub.ids["resultonly"]
    assert "CDF" in body["refused"][0]["error"]


def test_a_released_backfill_sample_passes_the_gate(hub_app):
    port, hub, store = hub_app
    sid = hub.ids["released"]
    # QBench: only the unreleased backfill is refused
    code, body = post(port, "/api/qbench-upload",
                      {"queue": [{"sample_id": sid, "standard_name": "Diesel"},
                                 {"sample_id": hub.ids["backfill"], "standard_name": "Diesel"}]})
    assert code == 409 and [r["sample_id"] for r in body["refused"]] == [hub.ids["backfill"]]
    # LIMS: exported
    code, body = post(port, "/api/export-lims", {"sample_ids": [sid]})
    assert code == 200, body
    assert [e["sample_id"] for e in body["exported"]] == [sid]


@pytest.mark.parametrize("status,error", [("pending_corrections", "Corrections not set"),
                                          ("error", "boom")])
def test_held_and_failed_samples_are_refused(hub_app, status, error):
    port, hub, store = hub_app
    sid = hub.ids["rerun"]
    store.samples.set_status(sid, status, error=error, db=hub.db)
    try:
        code, body = post(port, "/api/export-lims", {"sample_ids": [sid]})
        assert code == 409 and status in body["refused"][0]["error"], body
        code, body = post(port, "/api/qbench-upload",
                          {"queue": [{"sample_id": sid, "standard_name": "Diesel"}]})
        assert code == 409 and status in body["refused"][0]["error"], body
        assert error in body["refused"][0]["error"]
    finally:
        store.samples.set_status(sid, "final", db=hub.db)


def test_reports_zip_is_409_when_every_item_is_skipped(hub_app):
    port, hub, _ = hub_app
    code, body = post(port, "/api/export-analysis-reports-zip",
                      {"items": [{"sample_id": UNKNOWN, "standard_name": "Diesel"},
                                 {"sample_id": hub.ids["final"], "standard_name": "NoSuchStd"}]})
    assert _is_json_error(code, body, 409), (code, body)
    assert "skipped" in body["error"]


@pytest.mark.parametrize("name", ["../evil", "a/b", "..", "x\\y", ""])
def test_comparison_standard_names_cannot_escape_the_folder(hub_app, name):
    port, hub, _ = hub_app
    pw = _admin(port, hub)
    code, body = post(port, "/api/comparison-standard",
                      {"sample_id": hub.ids["final"], "name": name, "password": pw})
    assert _is_json_error(code, body, 400), (code, body)
    assert not (hub.data / "evil.CDF").exists()
    for path, payload in (("/api/comparison-standard/rename",
                           {"old_name": "Diesel", "new_name": name, "password": pw}),):
        code, body = post(port, path, payload)
        assert _is_json_error(code, body, 400), (path, code, body)
    assert (hub.standards / "Diesel.CDF").is_file()


def test_export_comparison_sanitises_the_lab_id_in_file_names(hub_app):
    port, hub, _ = hub_app
    code, body = post(port, "/api/export-comparison", {"sample_ids": [hub.ids["slashed"]]})
    assert code == 200, body
    export_dir = hub.data / "exports"
    for f in body["files"]:
        p = Path(f).resolve()
        assert p.parent == export_dir.resolve(), f
        assert "/" not in p.name and ".." not in p.name.replace("_comparison", "")


def test_an_unreadable_cdf_is_not_reread_on_every_list_request(hub_app):
    port, hub, store = hub_app
    from bootapp import wait_for
    sid = hub.ids["slashed"]
    cdf = hub.data / hub.sample("slashed")["cdf_path"]
    log = hub.data / "app.log"
    needle = f"sample_cache: sample {sid}:"
    with store.connection(hub.db) as conn:
        conn.execute("DELETE FROM sample_cache WHERE sample_id=?", (sid,))
    cdf.rename(cdf.with_suffix(".away"))
    try:
        get(port, "/api/files")
        assert wait_for(lambda: needle in log.read_text(encoding="utf-8", errors="replace"), timeout=20)
        for _ in range(3):
            get(port, "/api/files")
        wait_for(lambda: False, timeout=1.5)
        assert log.read_text(encoding="utf-8", errors="replace").count(needle) == 1
    finally:
        cdf.with_suffix(".away").rename(cdf)


def test_server_search_with_filters(hub_app):
    # The UI searches the server when the box is non-empty and the store
    # holds more than the page it loaded: q + instrument + status + paging.
    port, hub, _ = hub_app
    code, body = get(port, "/api/files?q=4030&instrument=gc1&status=final&limit=1")
    assert code == 200
    assert body["total"] == 2            # both 40304 injections (gc1, final)
    assert len(body["samples"]) == 1
    code, body = get(port, "/api/files?q=4030&instrument=gc2")
    assert body["total"] == 0


def test_index_has_no_scan_controls_and_loads_the_sample_helpers(hub_app):
    import urllib.request
    port, _hub, _ = hub_app
    html = get_text(port, "/")
    for gone in ('id="btn-scan"', 'id="btn-stop"', 'id="btn-rebuild-db"', 'id="btn-reindex-times"',
                 'id="modal-log"'):
        assert gone not in html, gone
    assert html.index("js/samples.js") < html.index("js/app.js")
    js = get_text(port, "/static/js/app.js")
    for gone in ("/api/scan", "/api/rebuild-db", "/api/library/reindex-times", "/api/files/refresh",
                 "/api/trace?", "/api/distillation-curve?", "sample_path", "pdf_path"):
        assert gone not in js, gone


# ── no store ────────────────────────────────────────────────────────────────

def test_an_empty_hub_answers_cleanly():
    # The app's start-up (hub.start, 2A1 T5) creates the store and gc1 on a
    # background thread: until then store routes answer 503, then a fresh
    # data folder has an empty store: lists are empty, ids are 404, and gc1
    # exists with no calibration.
    with tempfile.TemporaryDirectory() as t:
        with booted(Path(t)) as (port, proc, data, home):
            assert wait_for(lambda: get(port, "/api/calibration/active")[0] == 200, timeout=30)
            code, body = get(port, "/api/files")
            assert code == 200 and body["samples"] == [] and body["total"] == 0
            assert get(port, "/api/table")[1]["rows"] == []
            assert get(port, "/api/samples/1/metadata")[0] == 404
            code, body = get(port, "/api/calibration/active")
            assert code == 200 and body["mode"] == "unusable", body
