"""The runners that feed ``tasks.REGISTRY`` (v4.0 lane E), in process:
``hub_admin.AdminJobs`` (admin jobs and the diagnostics runner) and
``download_jobs.DownloadJobs`` (report ZIPs). The QBench upload thread, the
reprocess batch and ``/api/live``'s ``tasks`` live in ``app.py`` and are
checked by AST here and end to end in ``test_tasks_boot.py``."""
from __future__ import annotations

import ast
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("flask")

import download_jobs  # noqa: E402
import hub_admin  # noqa: E402
import tasks  # noqa: E402


def _wait(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


# ── AdminJobs ───────────────────────────────────────────────────────────────

def test_an_admin_job_is_a_task_with_progress_and_no_parameters():
    reg = tasks.Registry()
    runner = hub_admin.AdminJobs(tasks=reg)
    release = threading.Event()

    def fn(progress):
        progress({"phase": "match", "done": 500, "total": 12000,
                  "file": r"\\ASAPServer\secret\x.CDF"})
        assert release.wait(10)
        return {"counts": {"new": 3, "cdfs_read": 40}, "processed_dir": r"\\ASAPServer\secret"}

    job = runner.start("import-history-dry-run", fn,
                       {"instrument": "gc2", "processed_dir": r"\\ASAPServer\secret",
                        "by": "Ryan C (10.0.0.5)"})
    assert _wait(lambda: (reg.snapshot(None)[0]["progress"] or {}).get("done") == 500)
    [t] = reg.snapshot(None, names={"gc2": "GC-2"})
    assert t["kind"] == "import-history-dry-run" and t["state"] == "running"
    assert t["title"] == "GC-2 history dry run" and t["instrument"] == "gc2"
    assert t["progress"] == {"done": 500, "total": 12000,
                             "text": "Reading CDFs and matching them to the CSV"}
    assert t["open_url"] == "/admin/hub#import-history"
    release.set()
    assert _wait(lambda: reg.snapshot(None)[0]["state"] == "done")
    feed = repr(reg.snapshot(None))
    assert "ASAPServer" not in feed and "10.0.0.5" not in feed and "counts" not in feed
    # one outcome line, with its count (v4.0 lane E review)
    assert reg.snapshot(None, names={"gc2": "GC-2"})[0]["outcome"] == \
        "GC-2 dry run finished · 40 classified"
    assert runner.current()["id"] == job["id"]


def test_failed_and_stopped_jobs_end_their_task_without_the_error_text():
    reg = tasks.Registry()
    runner = hub_admin.AdminJobs(tasks=reg)

    def boom(progress):
        raise FileNotFoundError(r"C:\secret\folder")

    runner.start("load-folder", boom, {"instrument": "gc1"})
    assert _wait(lambda: reg.snapshot(None)[0]["state"] == "failed")
    assert "secret" not in repr(reg.snapshot(None))
    assert reg.snapshot(None)[0]["open_url"] == "/admin/hub#load-folder"

    gate = threading.Event()

    def slow(progress):
        progress({"phase": "commit", "done": 400, "total": 900})
        gate.wait(10)
        progress({"phase": "commit", "done": 500, "total": 900})    # raises: stopped
        return {}

    runner.start("import-history", slow, {"instrument": "gc1"})
    assert _wait(lambda: (reg.snapshot(None)[0]["progress"] or {}).get("done") == 400)
    runner.request_stop()
    gate.set()
    assert _wait(lambda: reg.snapshot(None)[0]["state"] == "stopped")
    assert reg.snapshot(None)[0]["outcome"] == "gc1 history import stopped after 400"


def test_the_diagnostics_runner_opens_the_diagnostics_panel():
    assert hub_admin.DIAG_JOBS._open_url("diagnostics-bundle") == "/admin/hub#diagnostics"
    assert hub_admin.JOBS._open_url("purge") == "/admin/hub#purge-panel"
    assert hub_admin.JOBS._open_url("load-folder") == "/admin/hub#load-folder"
    assert hub_admin.JOBS._open_url("import-history") == "/admin/hub#import-history"


# ── DownloadJobs ────────────────────────────────────────────────────────────

def test_a_report_zip_is_a_task_whose_download_goes_to_its_owner_once(tmp_path):
    reg = tasks.Registry()
    jobs = download_jobs.DownloadJobs(
        lambda: tmp_path / "zips", tasks=reg,
        download_url=lambda jid: f"/api/export-analysis-reports-zip/{jid}/download")
    release = threading.Event()

    def fn(progress, out_path):
        progress(done=1, total=2)
        assert release.wait(10)
        out_path.write_bytes(b"zip")
        return {"written": 2, "skipped": 0}

    job = jobs.start("reports-zip", fn, name="analysis_reports.zip", owner="Ryan C", total=2)
    assert _wait(lambda: (reg.snapshot("Ryan C")[0]["progress"] or {}).get("done") == 1)
    [t] = reg.snapshot("Jo")
    assert t["title"] == "Report ZIP · 2 reports" and t["by"] == "Ryan C"
    assert job["id"] not in repr(reg.snapshot("Jo"))          # the id is the link
    release.set()
    assert _wait(lambda: reg.snapshot("Ryan C")[0]["state"] == "done")
    assert reg.snapshot("Ryan C")[0]["outcome"] == "Report ZIP ready · 2 reports"
    assert reg.snapshot("Ryan C")[0]["download_url"] == \
        f"/api/export-analysis-reports-zip/{job['id']}/download"
    assert reg.snapshot("Jo")[0]["download_url"] is None
    claimed = jobs.claim(job["id"], "Ryan C")
    assert claimed is not None
    assert reg.snapshot("Ryan C")[0]["download_url"] is None   # fetched: no more link
    jobs.finished_streaming(claimed)


def test_a_failed_zip_is_a_failed_task(tmp_path):
    reg = tasks.Registry()
    jobs = download_jobs.DownloadJobs(lambda: tmp_path / "zips", tasks=reg)

    def fn(progress, out_path):
        raise download_jobs.NothingToDownload("All 3 report(s) were skipped")

    jobs.start("reports-zip", fn, name="a.zip", owner="Ryan C", total=3)
    assert _wait(lambda: reg.snapshot("Ryan C")[0]["state"] == "failed")
    assert reg.snapshot("Ryan C")[0]["download_url"] is None


def test_download_jobs_without_a_registry_feed_nothing(tmp_path):
    jobs = download_jobs.DownloadJobs(lambda: tmp_path / "zips")
    before = len(tasks.REGISTRY.snapshot(None))
    jobs.start("reports-zip", lambda p, o: o.write_bytes(b"x") or {}, name="a.zip",
               owner="x", total=1)
    time.sleep(0.1)
    assert len(tasks.REGISTRY.snapshot(None)) == before


# ── app.py (AST: never imported) ────────────────────────────────────────────

APP = (ROOT / "app.py").read_text(encoding="utf-8")


def _func(name):
    tree = ast.parse(APP)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(APP, node)
    raise AssertionError(name)


def test_api_live_carries_the_viewers_tasks():
    src = _func("api_live")
    assert "tasks=" in src and "_live_tasks" in src
    src = _func("_live_tasks")
    assert "tasks.REGISTRY.snapshot(" in src and "web_auth.current_name()" in src


def test_the_report_zips_the_qbench_upload_and_reprocess_feed_the_registry():
    assert "tasks=tasks.REGISTRY" in APP                      # REPORT_ZIPS
    assert 'tasks.REGISTRY.begin("reprocess"' in _func("api_reprocess")
    assert "tasks.jobs_probe(" in _func("api_reprocess")
    up = _func("api_qbench_upload")
    assert "tasks.REGISTRY.begin(" in up and '"qbench-upload"' in up
    assert "_upload_task_progress(" in up and "_upload_task_end()" in up
    assert "tasks.REGISTRY.finish(" in _func("_upload_task_end")
    assert "_upload_task_progress()" in _func("api_qbench_skip_item")


def test_admin_job_outcomes_carry_their_counts():
    cases = [
        ("load-folder", {"created": 12, "duplicate": 3, "folder": "C:\\x"},
         "Loaded 12 CDFs into gc1 · 3 already there"),
        ("import-history", {"counts": {"imported": 1198}}, "gc1 history imported · 1,198 samples"),
        ("diagnostics-bundle", {"size": 12 * 1024 * 1024, "download": "/api/x/secret"},
         "Diagnostics ready · 12 MB"),
        ("purge", {"counts": {"samples": 5}}, "Purged gc1 · 5 samples"),
    ]
    for kind, summary, want in cases:
        reg = tasks.Registry()
        runner = hub_admin.AdminJobs(tasks=reg)
        runner.start(kind, lambda progress, s=summary: s, {"instrument": "gc1"})
        assert _wait(lambda: reg.snapshot(None)[0]["state"] == "done")
        t = reg.snapshot(None)[0]
        assert t["outcome"] == want, (kind, t)
        assert "secret" not in repr(t)


def test_the_qbench_upload_ends_with_its_counts():
    src = _func("_upload_task_end")
    assert "counts=" in src
