"""v4.0 lane E: ``hub_control.busy_reasons`` (what holds off a Stop or a
restart) and ``tasks.REGISTRY`` (what the running-now indicator shows) agree:
every running task of a kind that a restart would cut short is a busy reason,
and every admin job busy_reasons names is a running task. In process."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")

import hub_admin  # noqa: E402
import hub_control  # noqa: E402
import tasks  # noqa: E402


@pytest.fixture
def clean(monkeypatch, tmp_path):
    monkeypatch.setenv("GC_DATA_DIR", str(tmp_path))
    hub_control.reset()
    reg = tasks.Registry()
    monkeypatch.setattr(tasks, "REGISTRY", reg)
    monkeypatch.setattr(hub_admin, "JOBS", hub_admin.AdminJobs())
    try:
        yield reg
    finally:
        hub_control.reset()


def _wait(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


def test_the_blocking_kinds_are_what_a_restart_would_cut_short():
    assert tasks.BLOCKING_KINDS == {"load-folder", "import-history", "import-history-dry-run",
                                    "purge", "diagnostics-bundle", "reports-zip",
                                    "qbench-upload"}
    assert "reprocess" not in tasks.BLOCKING_KINDS      # durable jobs: they survive a restart


def test_an_admin_job_is_both_busy_and_a_running_task(clean):
    gate = threading.Event()
    hub_admin.JOBS.start("load-folder", lambda progress: gate.wait(10) and {},
                         {"instrument": "gc1"})
    try:
        assert _wait(lambda: hub_control.busy_reasons() != [])
        assert any("load-folder" in b for b in hub_control.busy_reasons())
        assert [t["kind"] for t in clean.snapshot(None) if t["state"] == "running"] == ["load-folder"]
        # one reason for it, not two
        assert len([b for b in hub_control.busy_reasons() if "load" in b.lower()]) == 1
    finally:
        gate.set()
    assert _wait(lambda: hub_control.busy_reasons() == [])
    assert clean.snapshot(None)[0]["state"] == "done"


def test_a_running_task_no_other_check_saw_is_still_busy(clean):
    tid = clean.begin("qbench-upload", total=3)
    assert hub_control.busy_reasons() == ["QBench upload · 3 reports is running"]
    clean.finish(tid, "done")
    assert hub_control.busy_reasons() == []


def test_a_task_its_own_check_reports_is_named_once(clean):
    hub_control.configure(busy_extra=lambda: ["a QBench upload is running (2 item(s) queued)"])
    clean.begin("qbench-upload", total=2)
    assert hub_control.busy_reasons() == ["a QBench upload is running (2 item(s) queued)"]


def test_work_that_survives_a_restart_is_not_busy(clean):
    clean.begin("reprocess", total=5)
    assert hub_control.busy_reasons() == []
