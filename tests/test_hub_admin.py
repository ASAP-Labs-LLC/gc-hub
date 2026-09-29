"""Admin wiring (2A1 T5): the folder-loader job and the export actions,
booted for real (``tests/bootapp.py``) with the admin password set through
first-use setup. The hub's own Worker processes what the loader submits.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

import cdf_fixtures as fx  # noqa: E402
import exports  # noqa: E402
import store  # noqa: E402
from bootapp import get_text, booted, get, post, send, setup_admin, wait_for  # noqa: E402
from hub_boot import SIMDIS, Hub  # noqa: E402


@pytest.fixture(scope="module")
def admin_hub():
    tmp = Path(tempfile.mkdtemp(prefix="gc-t5-admin-"))
    try:
        h = Hub(tmp)
        with booted(tmp) as (port, _proc, data, _home):
            assert wait_for(lambda: h.db.is_file()
                            and store.instruments.get("gc1", db=h.db) is not None, timeout=30)
            store.instruments.upsert({"id": "gc1", "live_since": datetime(2020, 1, 1)}, db=h.db)
            pw = setup_admin(port, data)
            yield port, h, pw
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _job(port, pw):
    return post(port, "/api/admin/jobs/status", {"password": pw})[1]["job"]


def test_admin_routes_need_the_password_and_json(admin_hub):
    port, _h, _pw = admin_hub
    paths = ("/api/admin/load-folder", "/api/admin/jobs/status", "/api/admin/exports",
             "/api/admin/exports/gc1/adopt", "/api/admin/exports/gc1/new-path",
             "/api/admin/exports/gc1/write-fresh")
    for path in paths:
        code, _ = send(port, path, b'{"password": "x"}', {"Content-Type": "text/plain"})
        assert code == 415, path
    # two wrong passwords (more would trip admin_auth's backoff for later tests)
    for path in (paths[0], paths[3]):
        assert post(port, path, {"password": "wrong"})[0] == 403, path


def test_load_folder_job_loads_and_the_hub_processes(admin_hub, tmp_path):
    port, h, pw = admin_hub
    folder = tmp_path / "robocopy" / "2026-09"
    folder.mkdir(parents=True)
    fx.blank_cdf(folder / "b.CDF", injected=datetime(2026, 9, 24, 15, 30, 27), method_name=SIMDIS)
    fx.sample_cdf(folder / "s.CDF", name="LF-1", injected=datetime(2026, 9, 25, 14, 23, 0),
                  method_name=SIMDIS)
    before = {p: p.stat().st_mtime_ns for p in folder.iterdir()}

    assert post(port, "/api/admin/load-folder",
                {"password": pw, "instrument": "gc9", "folder": str(folder)})[0] == 404
    assert post(port, "/api/admin/load-folder",
                {"password": pw, "instrument": "gc1", "folder": str(folder / "nope")})[0] == 400
    assert post(port, "/api/admin/load-folder",
                {"password": pw, "instrument": "gc1", "folder": "relative/x"})[0] == 400
    assert post(port, "/api/admin/load-folder", {"password": pw, "instrument": "gc1",
                                                 "folder": str(folder), "backfill": "yes"})[0] == 400

    code, body = post(port, "/api/admin/load-folder",
                      {"password": pw, "instrument": "gc1", "folder": str(tmp_path / "robocopy")})
    assert code == 202, body
    assert body["job"]["kind"] == "load-folder" and body["job"]["state"] == "running"
    assert wait_for(lambda: _job(port, pw)["state"] != "running", timeout=60)
    job = _job(port, pw)
    assert job["state"] == "done", job
    assert job["summary"]["created"] == 2 and job["counts"] == {"created": 2}
    assert {Path(r["file"]).name for r in job["recent"]} == {"b.CDF", "s.CDF"}
    ids = job["summary"]["sample_ids"]
    assert wait_for(lambda: all(store.samples.get(i, db=h.db)["status"] == "final" for i in ids),
                    timeout=60)
    assert {p: p.stat().st_mtime_ns for p in folder.iterdir()} == before   # read-only

    # a second run is a no-op (resumable: every file is a duplicate)
    code, body = post(port, "/api/admin/load-folder",
                      {"password": pw, "instrument": "gc1", "folder": str(tmp_path / "robocopy")})
    assert code == 202
    assert wait_for(lambda: _job(port, pw)["state"] == "done", timeout=60)
    assert _job(port, pw)["counts"] == {"duplicate": 2}


def test_export_status_adopt_new_path_and_write_fresh(admin_hub, tmp_path):
    port, h, pw = admin_hub
    code, body = post(port, "/api/admin/exports", {"password": pw})
    assert code == 200, body
    gc1 = {s["instrument"]: s for s in body["instruments"]}["gc1"]
    assert Path(gc1["path"]).resolve() == (h.data / "results" / "gc1_results.csv").resolve()
    assert gc1["refused"] is None

    assert post(port, "/api/admin/exports/gc9/adopt", {"password": pw})[0] == 404
    for bad in ("", "relative.csv", str(tmp_path / "x.txt"), str(tmp_path / "no" / "x.csv")):
        assert post(port, "/api/admin/exports/gc1/new-path",
                    {"password": pw, "path": bad})[0] == 400, bad

    # A v1 file without a sidecar: pointed at, it is refused until adopted.
    v1 = tmp_path / "distill_results.csv"
    v1.write_bytes(exports.header_line().encode() + b"")
    with v1.open("ab") as fh:
        fh.write(b"40001,2026-09-01 10:00:00" + b"," * 29 + b"\r\n")
    code, body = post(port, "/api/admin/exports/gc1/new-path", {"password": pw, "path": str(v1)})
    assert code == 200 and body["status"]["path"] == str(v1)

    def refused():
        _, b = post(port, "/api/admin/exports", {"password": pw})
        return {s["instrument"]: s for s in b["instruments"]}["gc1"]["refused"]
    # a final sample makes a pending row; the flush then refuses the v1 file
    cdf = fx.sample_cdf(h.src / "ex.CDF", name="EX-1", injected=datetime(2026, 9, 26, 9, 0, 0),
                        method_name=SIMDIS)
    import pipeline
    sid = pipeline.submit("gc1", cdf, conf=h.conf, data_dir=h.data, db=h.db).sample_id
    assert wait_for(lambda: store.samples.get(sid, db=h.db)["status"] == "final", timeout=60)
    assert wait_for(lambda: refused() == "no-sidecar", timeout=30)

    code, body = post(port, "/api/admin/exports/gc1/adopt", {"password": pw})
    assert code == 200, body
    assert body["sidecar"]["instrument"] == "gc1"
    assert wait_for(lambda: b"EX-1," in v1.read_bytes(), timeout=30)
    assert refused() is None

    # write fresh: a new file with every gated current revision, never an existing one
    fresh = tmp_path / "fresh.csv"
    code, body = post(port, "/api/admin/exports/gc1/write-fresh",
                      {"password": pw, "path": str(fresh)})
    assert code == 200, body
    assert body["rows"] >= 1 and body["status"]["path"] == str(fresh)
    assert fresh.read_bytes().startswith(exports.header_line().encode())
    code, body = post(port, "/api/admin/exports/gc1/write-fresh",
                      {"password": pw, "path": str(v1)})
    assert code == 409 and body["reason"] == "exists"


def test_admin_page_is_served(admin_hub):
    port, _h, _pw = admin_hub
    html = get_text(port, "/admin/hub")
    assert 'id="app-version"' in html and "hub_admin.js" in html
