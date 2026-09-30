"""Purge instrument data (v3.1) through the real app (``tests/bootapp.py``):
the routes' refusals, the admin job, ingest answering 503 for the purged
instrument while it runs (and 201 after), and the admin page's panel.

The purge is held open by a ``running`` job row for a GC-1 sample (the purge
waits for its instrument's running jobs), which the test then marks done.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden", TESTS / "ingest"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

import purge_helpers as ph  # noqa: E402
from bootapp import booted, get, get_text, post, send, setup_admin, wait_for  # noqa: E402
from ingest_helpers import ingest  # noqa: E402

import ingest_api  # noqa: E402
import store  # noqa: E402
from hub_boot import SIMDIS  # noqa: E402


@pytest.fixture(scope="module")
def purge_hub():
    tmp = Path(tempfile.mkdtemp(prefix="gc-purge-boot-"))
    try:
        h = ph.build_two_gc_hub(tmp)
        h.tokens = {i: ingest_api.mint_token(i, db=h.db) for i in ("gc1", "gc2")}
        with booted(tmp) as (port, _proc, data, _home):
            assert data == h.data
            pw = setup_admin(port, data)
            # the queued reprocess jobs run first, so later snapshots are stable
            assert wait_for(lambda: not store.jobs.list(state="queued", db=h.db)
                            and not store.jobs.list(state="running", db=h.db), timeout=60)
            yield port, h, pw
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _job(port, pw):
    return post(port, "/api/admin/jobs/status", {"password": pw})[1]["job"]


def test_purge_routes_need_the_password_json_and_a_session(purge_hub):
    port, _h, _pw = purge_hub
    for path in ("/api/admin/purge/preview", "/api/admin/purge/start"):
        assert send(port, path, b'{"password": "x"}', {"Content-Type": "text/plain"})[0] == 415
        assert post(port, path, {"password": "wrong", "instrument": "gc1",
                                 "scope": "all"})[0] == 403
        assert post(port, path, {"instrument": "gc1"}, auth=False)[0] == 401


def test_preview_and_its_refusals(purge_hub):
    port, h, pw = purge_hub
    assert post(port, "/api/admin/purge/preview",
                {"password": pw, "instrument": "gc9", "scope": "all"})[0] == 404
    assert post(port, "/api/admin/purge/preview",
                {"password": pw, "instrument": "gc1", "scope": "some"})[0] == 400
    code, body = post(port, "/api/admin/purge/preview",
                      {"password": pw, "instrument": "gc1", "scope": "all"})
    assert code == 200, body
    pv = body["preview"]
    assert pv["ok"] and pv["confirm_text"] == "PURGE GC-1"
    assert pv["samples"] == len(ph.instrument_sample_ids(h.db, "gc1"))
    assert pv["appended"] > 0 and pv["warning"]


def test_start_refuses_a_wrong_confirmation_and_a_bad_path(purge_hub):
    port, h, pw = purge_hub
    before = ph.dump(h.db)
    for confirm in ("PURGE gc1", "purge GC-1", "", None):
        code, body = post(port, "/api/admin/purge/start", {"password": pw, "instrument": "gc1",
                                                           "scope": "all", "confirm_text": confirm})
        assert code == 400, (confirm, body)
    code, _ = post(port, "/api/admin/purge/start", {"password": pw, "instrument": "gc1",
                                                    "scope": "all", "confirm_text": "PURGE GC-1",
                                                    "new_results_path": "relative.csv"})
    assert code == 400
    after = ph.dump(h.db)
    for t in ("samples", "sample_results", "export_rows", "conflicts"):
        assert after[t] == before[t], t


def test_purge_job_ingest_503_then_201_and_one_job_at_a_time(purge_hub):
    port, h, pw = purge_hub
    sid = h.ids["final"]
    job_id = store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid, db=h.db)
    with store.connection(h.db) as conn:            # a gc1 job "running": the purge waits
        conn.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
    gc2_before = {t: [r for r in rows if r.get("instrument_id") == "gc2"]
                  for t, rows in ph.dump(h.db).items() if t in ("samples", "export_rows")}

    code, body = post(port, "/api/admin/purge/start", {"password": pw, "instrument": "gc1",
                                                       "scope": "all",
                                                       "confirm_text": "PURGE GC-1"})
    assert code == 202, body
    assert body["job"]["kind"] == "purge" and body["job"]["state"] == "running"
    assert wait_for(lambda: (_job(port, pw)["progress"] or {}).get("phase")
                    == "waiting for processing", timeout=20)

    # the page shows it without the password (a session only)
    code, st = get(port, "/api/purge/status")
    assert code == 200 and st["job"]["kind"] == "purge" and st["job"]["state"] == "running"
    assert get(port, "/api/purge/status", auth=False)[0] == 401

    # one admin job at a time
    code, body = post(port, "/api/admin/purge/start", {"password": pw, "instrument": "gc2",
                                                       "scope": "all",
                                                       "confirm_text": "PURGE GC-2"})
    assert code == 409, body
    code, _ = post(port, "/api/admin/load-folder", {"password": pw, "instrument": "gc2",
                                                    "folder": str(h.src)})
    assert code == 409

    # ingest for gc1 answers 503 (retry); gc2 carries on
    new1 = h._cdf("sample", name="70001", injected=datetime(2026, 9, 28, 9, 0), shift=0.12,
                  method_name=SIMDIS).read_bytes()
    code, body = ingest(port, h.tokens["gc1"], new1)
    assert code == 503 and "purge in progress" in body["error"] and "retry" in body["error"]
    new2 = h._cdf("sample", name="70002", injected=datetime(2026, 9, 28, 9, 5), shift=0.13,
                  method_name=SIMDIS).read_bytes()
    code, body = ingest(port, h.tokens["gc2"], new2)
    assert code == 201, body

    store.jobs.complete(job_id, db=h.db)               # the job ends: the purge goes ahead
    assert wait_for(lambda: _job(port, pw)["state"] != "running", timeout=60)
    job = _job(port, pw)
    assert job["state"] == "done", job
    summary = job["summary"]
    code, st = get(port, "/api/purge/status")
    assert st["job"]["state"] == "done" and st["journal"]["state"] == "done"
    assert st["journal"]["samples"] == summary["samples"]
    assert summary["samples"] > 0 and Path(summary["backup"]).is_file()
    assert not ph.instrument_sample_ids(h.db, "gc1")
    after = {t: [r for r in rows if r.get("instrument_id") == "gc2"]
             for t, rows in ph.dump(h.db).items() if t in ("samples", "export_rows")}
    added = [r for r in after["samples"] if r not in gc2_before["samples"]]
    assert [r["lab_id"] for r in added] == ["70002"]
    assert all(r in after["samples"] for r in gc2_before["samples"])

    # after the purge, the same file is accepted
    code, body = ingest(port, h.tokens["gc1"], new1)
    assert code == 201, body


def test_the_admin_page_has_the_purge_panel(purge_hub):
    port, _h, _pw = purge_hub
    page = get_text(port, "/admin/hub")
    for needle in ('id="purge-panel"', 'id="purge-inst"', 'id="purge-scope"',
                   'id="btn-purge-preview"', 'id="purge-confirm"', 'id="btn-purge-start"',
                   "js/purge.js", "Restore after a purge"):
        assert needle in page, needle
    js = get_text(port, "/static/js/purge.js")
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
