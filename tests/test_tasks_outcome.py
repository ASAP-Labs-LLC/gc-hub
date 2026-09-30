"""tasks.py after the v4.0 lane E review: a local ``open_url`` only, one
outcome line per ended task (with its counts, never repeated wording), and
tasks a restart interrupted, noted at start-up."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tasks  # noqa: E402


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def reg(clock):
    return tasks.Registry(clock=clock)


def _one(reg, tid, **kw):
    return [t for t in reg.snapshot(None, **kw) if t["id"] == tid][0]


def test_open_url_must_be_a_local_path(reg):
    for bad in ("//evil.example/x", "https://x", "javascript:alert(1)", "admin", "/\\evil"):
        tid = reg.begin("reprocess", open_url=bad)
        assert _one(reg, tid)["open_url"] is None, bad
    tid = reg.begin("reprocess", open_url="/admin/hub#imports")
    assert _one(reg, tid)["open_url"] == "/admin/hub#imports"


@pytest.mark.parametrize("kind,state,counts,want", [
    ("import-history-dry-run", "done", {"classified": 40}, "GC-1 dry run finished · 40 classified"),
    ("import-history-dry-run", "stopped", {}, "GC-1 dry run stopped"),
    ("import-history", "done", {"imported": 1198}, "GC-1 history imported · 1,198 samples"),
    ("import-history", "stopped", {"done": 400}, "GC-1 history import stopped after 400"),
    ("import-history", "failed", {}, "GC-1 history import failed"),
    ("load-folder", "done", {"created": 12, "duplicate": 3},
     "Loaded 12 CDFs into GC-1 · 3 already there"),
    ("load-folder", "done", {"created": 1}, "Loaded 1 CDF into GC-1"),
    ("load-folder", "failed", {}, "Loading CDFs into GC-1 failed"),
    ("purge", "done", {"samples": 5}, "Purged GC-1 · 5 samples"),
    ("purge", "interrupted", {}, "GC-1 purge interrupted by a restart"),
    ("diagnostics-bundle", "done", {"bytes": 12 * 1024 * 1024}, "Diagnostics ready · 12 MB"),
    ("diagnostics-bundle", "done", {"bytes": 300 * 1024}, "Diagnostics ready · 300 KB"),
    ("diagnostics-bundle", "failed", {}, "Diagnostics bundle failed"),
    ("reports-zip", "done", {"reports": 12}, "Report ZIP ready · 12 reports"),
    ("reports-zip", "failed", {}, "Report ZIP failed"),
    ("qbench-upload", "done", {"ok": 3}, "Uploaded 3 reports to QBench"),
    ("qbench-upload", "failed", {"ok": 2, "failed": 1}, "Uploaded 2 reports to QBench · 1 failed"),
    ("qbench-upload", "stopped", {"ok": 2}, "QBench upload stopped after 2 reports"),
    ("reprocess", "done", {"ok": 1}, "Re-processed 1 sample"),
    ("reprocess", "done", {"ok": 3, "failed": 1}, "Re-processed 3 samples · 1 failed"),
    ("reprocess", "failed", {"ok": 0, "failed": 2}, "Re-process failed · 2 samples"),
    ("something-new", "done", {}, "Something new finished"),
    ("something-new", "interrupted", {}, "Something new interrupted by a restart"),
])
def test_an_ended_task_says_its_outcome_once_with_counts(kind, state, counts, want):
    r = tasks.Registry()
    tid = r.begin(kind, instrument="gc1")
    assert _one(r, tid)["outcome"] is None                   # running: no outcome yet
    r.finish(tid, state, counts=counts)
    assert _one(r, tid, names={"gc1": "GC-1"})["outcome"] == want


def test_outcome_counts_are_numbers_only(reg):
    tid = reg.begin("load-folder", instrument="gc1")
    reg.finish(tid, "done", counts={"created": "C:\\secret", "duplicate": 2, "x": [1]})
    out = _one(reg, tid)["outcome"]
    assert "secret" not in out and out == "Loaded 0 CDFs into gc1 · 2 already there"


def test_a_dead_thread_has_an_interrupted_outcome(reg):
    tid = reg.begin("qbench-upload", total=2, alive=lambda: False)
    assert _one(reg, tid)["outcome"] == "QBench upload stopped without finishing"


def test_an_interrupted_task_can_be_noted_at_start_up(reg, clock):
    tid = reg.note_interrupted("purge", instrument="gc2", by="Ryan C")
    t = _one(reg, tid, names={"gc2": "GC-2"})
    assert t["state"] == "interrupted" and t["outcome"] == "GC-2 purge interrupted by a restart"
    assert t["ended_at"] and t["progress"] is None
    assert reg.snapshot("Ryan C")[0]["mine"] is True
    clock.t += tasks.RETAIN_SECONDS + 1
    assert reg.snapshot(None) == []


def test_start_up_recovery_shows_interrupted_work_as_tasks(monkeypatch, tmp_path):
    """``hub.run_startup_recovery`` (v3.1 purge): a purge it finished and an
    import it marked interrupted show as interrupted tasks for 30 minutes."""
    pytest.importorskip("flask")
    import hub
    reg = tasks.Registry()
    monkeypatch.setattr(tasks, "REGISTRY", reg)
    monkeypatch.setattr(hub, "recover_purges", lambda db, d, n: [
        {"instrument": "gc2", "samples": 5}, {"instrument": "gc3", "nothing_to_do": True}])
    monkeypatch.setattr(hub, "mark_interrupted_imports", lambda db, d, n: [
        {"id": 1, "instrument_id": "gc1", "by": "Ryan C (10.0.0.5)"}])
    hub.forget_startup_recovery(tmp_path)
    try:
        assert hub.run_startup_recovery(tmp_path / "gc.db", tmp_path, None) is not None
    finally:
        hub.forget_startup_recovery(tmp_path)
    out = {t["kind"]: t for t in reg.snapshot("Ryan C", names={"gc1": "GC-1", "gc2": "GC-2"})}
    assert set(out) == {"purge", "import-history"}
    assert out["purge"]["outcome"] == "GC-2 purge interrupted by a restart"
    assert out["purge"]["open_url"] == "/admin/hub#purge-panel"
    assert out["import-history"]["outcome"] == "GC-1 history import interrupted by a restart"
    assert out["import-history"]["by"] == "Ryan C" and out["import-history"]["mine"] is True


def test_the_reprocess_probe_reports_its_counts(tmp_path):
    import sqlite3
    db = tmp_path / "gc.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, state TEXT)")
    conn.executemany("INSERT INTO jobs VALUES (?, ?)", [(1, "done"), (2, "failed"), (3, "queued")])
    conn.commit()
    probe = tasks.jobs_probe([1, 2, 3], db)
    assert probe()["counts"] == {"ok": 1, "failed": 1}
    conn.execute("UPDATE jobs SET state='done' WHERE id=3")
    conn.commit()
    out = probe()
    assert out["state"] == "done" and out["counts"] == {"ok": 2, "failed": 1}
    conn.close()
