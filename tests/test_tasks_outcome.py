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


def test_imports_a_restart_cut_short_show_as_interrupted(tmp_path, clock):
    """At start-up, before the hub runs anything: every real import run the
    store says started but never finished was cut short by the restart."""
    import sqlite3
    db = tmp_path / "gc.db"
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE import_runs(id INTEGER PRIMARY KEY, instrument_id TEXT, '
                 'started_at TEXT, finished_at TEXT, "by" TEXT, sources TEXT, counts TEXT, '
                 'stopped TEXT)')
    conn.executemany('INSERT INTO import_runs(instrument_id, started_at, finished_at, "by") '
                     'VALUES (?, ?, ?, ?)',
                     [("gc2", "2026-09-30T10:00:00+00:00", None, "Ryan C (10.0.0.5)"),
                      ("gc1", "2026-09-29T10:00:00+00:00", "2026-09-29T11:00:00+00:00", "x")])
    conn.commit()
    conn.close()
    reg = tasks.Registry(clock=clock)
    assert tasks.note_unfinished_imports(db, registry=reg) == 1
    [t] = reg.snapshot("Ryan C", names={"gc2": "GC-2"})
    assert t["state"] == "interrupted" and t["kind"] == "import-history"
    assert t["outcome"] == "GC-2 history import interrupted by a restart"
    assert t["by"] == "Ryan C" and t["mine"] is True             # the name, never the address
    assert t["open_url"] == "/admin/hub#import-history"
    # no store yet (a first start): nothing, no error
    assert tasks.note_unfinished_imports(tmp_path / "missing.db", registry=reg) == 0


def test_the_hub_start_notes_them_before_it_starts():
    import ast
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_start_hub")
    body = ast.get_source_segment(src, fn)
    assert body.index("tasks.note_unfinished_imports(") < body.index("hub.start_with_retry(")


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
