"""Admin wiring for the 2D history import (T5): dry run, the real import as an
admin job (backfill only, never exported), stop/resume, refusal while another
admin job runs, and admin gating. Booted for real (``tests/bootapp.py``), the
same way ``tests/test_hub_admin.py`` wires the T6 folder loader.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden", TESTS / "import_history"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

import store  # noqa: E402
from bootapp import booted, post, send, setup_admin, wait_for  # noqa: E402
from hub_boot import Hub  # noqa: E402
from import_history_testlib import (  # noqa: E402
    ALIASES, SIMDIS, one, row, sample, samples, src, table_counts, write_csv)


@pytest.fixture(scope="module")
def admin_hub():
    tmp = Path(tempfile.mkdtemp(prefix="gc-t5-import-"))
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


def _poll_until(predicate, timeout=10.0, interval=0.005) -> bool:
    """Like ``wait_for``, but polls tightly (for catching a fast job mid-run)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _bulk_rows(n: int, *, start=datetime(2026, 1, 1, 0, 0, 1), prefix="BULK") -> list:
    out = []
    for i in range(n):
        dt = (start + timedelta(seconds=i)).isoformat(sep=" ")
        lab = f"{prefix}{i:04d}"
        out.append(row(lab, dt, source=src(f"{lab}.CDF")))
    return out


# ── admin gating ─────────────────────────────────────────────────────────────

def test_import_history_routes_need_the_password_and_json(admin_hub):
    port, _h, _pw = admin_hub
    paths = ("/api/admin/import-history/dry-run", "/api/admin/import-history/start",
             "/api/admin/import-history/last-run")
    for path in paths:
        code, _ = send(port, path, b'{"password": "x"}', {"Content-Type": "text/plain"})
        assert code == 415, path
    for path in paths:
        assert post(port, path, {"password": "wrong"})[0] == 403, path
    assert post(port, "/api/admin/jobs/stop", {"password": "wrong"})[0] == 403


# ── dry run ──────────────────────────────────────────────────────────────────

def test_dry_run_classifies_without_writing(admin_hub, tmp_path):
    port, h, pw = admin_hub
    processed = tmp_path / "dry" / "processed_cdfs2"
    a = sample(processed, "DR-1", datetime(2026, 9, 17, 14, 50, 18), method=SIMDIS)
    rows = [row("DR-1", "2026-09-17 14:50:18", source=src(a.name)),
            row("RO-1", "2026-06-29 09:48:42", source=src("RO-1.CDF"))]
    csv_path = write_csv(tmp_path / "dry" / "distill_results.csv", rows)

    assert post(port, "/api/admin/import-history/dry-run",
               {"password": pw, "instrument": "gc9", "processed_dir": str(processed),
                "results_csv": str(csv_path), "aliases": ALIASES})[0] == 404
    assert post(port, "/api/admin/import-history/dry-run",
               {"password": pw, "instrument": "gc1", "processed_dir": str(processed / "nope"),
                "results_csv": str(csv_path)})[0] == 400
    assert post(port, "/api/admin/import-history/dry-run",
               {"password": pw, "instrument": "gc1", "processed_dir": "relative/x"})[0] == 400
    assert post(port, "/api/admin/import-history/dry-run",
               {"password": pw, "instrument": "gc1", "processed_dir": str(processed),
                "results_csv": "relative.csv"})[0] == 400
    assert post(port, "/api/admin/import-history/dry-run",
               {"password": pw, "instrument": "gc1", "processed_dir": str(processed),
                "results_csv": str(tmp_path / "dry" / "nope.csv")})[0] == 400
    assert post(port, "/api/admin/import-history/dry-run",
               {"password": pw, "instrument": "gc1", "processed_dir": str(processed),
                "results_csv": str(csv_path), "aliases": "not-a-list"})[0] == 400
    assert post(port, "/api/admin/import-history/dry-run",
               {"password": pw, "instrument": "gc1", "processed_dir": str(processed),
                "results_csv": str(csv_path), "aliases": ALIASES, "batch_size": 0})[0] == 400

    code, body = post(port, "/api/admin/import-history/dry-run",
                      {"password": pw, "instrument": "gc1", "processed_dir": str(processed),
                       "results_csv": str(csv_path), "aliases": ALIASES})
    assert code == 200, body
    summary = body["summary"]
    assert summary["dry_run"] is True
    assert summary["counts"]["attached"] == 1
    assert summary["counts"]["result_only"] == 1
    assert samples(h, "gc1") == []   # a dry run writes nothing


# ── the real import ──────────────────────────────────────────────────────────

def test_real_import_creates_backfill_final_and_result_only_no_export_rows(admin_hub, tmp_path):
    port, h, pw = admin_hub
    processed = tmp_path / "real" / "processed_cdfs2"
    a = sample(processed, "RI-1", datetime(2026, 9, 18, 15, 33, 41), method=SIMDIS)
    rows = [row("RI-1", "2026-09-18 15:33:41", source=src(a.name)),
            row("RI-2", "2026-06-29 09:48:42", source=src("RI-2.CDF"))]
    csv_path = write_csv(tmp_path / "real" / "distill_results.csv", rows)
    body_base = {"password": pw, "instrument": "gc1", "processed_dir": str(processed),
                "results_csv": str(csv_path), "aliases": ALIASES}

    assert post(port, "/api/admin/import-history/start", body_base)[0] == 400   # missing confirm
    assert post(port, "/api/admin/import-history/start",
               dict(body_base, confirm=False))[0] == 400

    before = dict(table_counts(h))
    code, body = post(port, "/api/admin/import-history/start", dict(body_base, confirm=True))
    assert code == 202, body
    assert body["job"]["kind"] == "import-history" and body["job"]["state"] == "running"
    assert wait_for(lambda: _job(port, pw)["state"] != "running", timeout=30)
    job = _job(port, pw)
    assert job["state"] == "done", job
    assert job["summary"]["counts"]["attached"] == 1
    assert job["summary"]["counts"]["result_only"] == 1

    attached = one(h, "RI-1")
    assert attached["status"] == "final" and attached["backfill"] == 1
    assert attached["cdf_sha256"] is not None and attached["legacy_unverified"] == 0

    result_only = one(h, "RI-2")
    assert result_only["status"] == "final" and result_only["backfill"] == 1
    assert result_only["cdf_sha256"] is None and result_only["legacy_unverified"] == 1

    after = table_counts(h)
    assert after["export_rows"] == before["export_rows"]   # D11: backfill is never exported
    assert after["jobs"] == before["jobs"]                 # never queued for recompute

    # a second run over the same inputs writes nothing new (resumable)
    code, body = post(port, "/api/admin/import-history/start", dict(body_base, confirm=True))
    assert code == 202, body
    assert wait_for(lambda: _job(port, pw)["state"] == "done", timeout=30)
    job = _job(port, pw)
    assert job["summary"]["counts"]["already_imported"] == 2
    assert job["summary"]["counts"].get("imported", 0) == 0


# ── last run ─────────────────────────────────────────────────────────────────

def test_last_run_defaults_the_form_and_warns_on_a_different_csv(admin_hub, tmp_path):
    port, _h, pw = admin_hub
    assert post(port, "/api/admin/import-history/last-run",
               {"password": pw, "instrument": "gc9"})[0] == 404

    code, body = post(port, "/api/admin/import-history/last-run",
                      {"password": pw, "instrument": "gc1"})
    assert code == 200, body
    last = body["last_run"]
    assert last is not None
    assert last["aliases"] == ALIASES
    assert last["results_csv"] and Path(last["results_csv"]).is_file()
    assert last["processed_dir"] and Path(last["processed_dir"]).is_dir()
    # the run records the admin who started it, like other admin actions
    assert last["by"] == "admin@127.0.0.1", last

    # a different CSV path than last time is not refused, only warned about
    other_csv = Path(last["results_csv"]).with_name("other.csv")
    shutil.copy2(last["results_csv"], other_csv)
    code, body = post(port, "/api/admin/import-history/dry-run",
                      {"password": pw, "instrument": "gc1",
                       "processed_dir": last["processed_dir"], "results_csv": str(other_csv),
                       "aliases": ALIASES})
    assert code == 200, body
    assert any("different CSV" in w for w in body["summary"]["warnings"])


# ── stop / resume / refusal while another job runs ───────────────────────────

def test_stop_between_batches_resumes_and_refuses_a_concurrent_job(admin_hub, tmp_path):
    port, h, pw = admin_hub
    processed = tmp_path / "bulk" / "processed_cdfs2"
    processed.mkdir(parents=True)
    n = 8000
    csv_path = write_csv(tmp_path / "bulk" / "distill_results.csv", _bulk_rows(n))
    body = {"password": pw, "instrument": "gc1", "processed_dir": str(processed),
            "results_csv": str(csv_path), "aliases": ALIASES, "batch_size": 1, "confirm": True}

    code, resp = post(port, "/api/admin/import-history/start", body)
    assert code == 202, resp

    # refused while running: the same kind, and a different admin job kind
    assert post(port, "/api/admin/import-history/start", body)[0] == 409
    empty = tmp_path / "empty"
    empty.mkdir()
    assert post(port, "/api/admin/load-folder",
               {"password": pw, "instrument": "gc1", "folder": str(empty)})[0] == 409

    # let a good number of single-row batches actually commit before asking it
    # to stop, so the stop is a genuine mid-run one (some samples in, most not)
    # rather than a race against the very first or very last progress event
    _poll_until(lambda: (_job(port, pw)["progress"] or {}).get("done", 0) >= 200
               or _job(port, pw)["state"] != "running", timeout=60)
    code, resp = post(port, "/api/admin/jobs/stop", {"password": pw})
    stopped_early = code == 200
    if not stopped_early:
        assert code == 409, resp   # the run finished before the stop request landed

    assert wait_for(lambda: _job(port, pw)["state"] != "running", timeout=120)
    job = _job(port, pw)
    bulk_count = sum(1 for s in samples(h, "gc1") if s["lab_id"].startswith("BULK"))
    if stopped_early:
        assert job["state"] == "stopped", job
        assert job["summary"] is not None and job["summary"].get("stopped")
        assert 0 < bulk_count < n, bulk_count   # some batches committed, not all
    else:
        assert job["state"] == "done", job
        assert bulk_count == n

    # resume: same params pick up wherever the run left off
    code, resp = post(port, "/api/admin/import-history/start",
                      {"password": pw, "instrument": "gc1", "processed_dir": str(processed),
                       "results_csv": str(csv_path), "aliases": ALIASES, "confirm": True})
    assert code == 202, resp
    assert wait_for(lambda: _job(port, pw)["state"] != "running", timeout=120)
    job = _job(port, pw)
    assert job["state"] == "done", job

    bulk_samples = [s for s in samples(h, "gc1") if s["lab_id"].startswith("BULK")]
    assert len(bulk_samples) == n
    assert all(s["status"] == "final" and s["backfill"] == 1 and s["cdf_sha256"] is None
              for s in bulk_samples)


def test_stop_refuses_when_no_job_is_running(admin_hub):
    port, _h, pw = admin_hub
    assert wait_for(lambda: _job(port, pw)["state"] != "running", timeout=30)
    assert post(port, "/api/admin/jobs/stop", {"password": pw})[0] == 409


def test_admin_page_shows_the_history_import_section(admin_hub):
    import urllib.request
    port, _h, _pw = admin_hub
    html = urllib.request.urlopen(f"http://127.0.0.1:{port}/admin/hub", timeout=5).read().decode()
    assert 'id="ih-inst"' in html and 'id="btn-ih-start"' in html and 'id="btn-ih-stop"' in html
