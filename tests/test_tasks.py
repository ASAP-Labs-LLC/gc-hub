"""tasks.py (v4.0 lane E): the in-memory registry of running work behind the
"running now" indicator and ``GET /api/live``'s ``tasks``. Stdlib only,
tested in-process with an injected clock; no app import."""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest import mock

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


PUBLIC = {"id", "kind", "title", "instrument", "state", "progress", "by", "started_at",
          "ended_at", "open_url", "download_url", "mine"}


def test_begin_update_finish_and_the_public_shape(reg, clock):
    tid = reg.begin("import-history", by="Ryan C", instrument="gc2", total=12000,
                    open_url="/admin/hub#imports")
    reg.update(tid, done=3000, text="Reading CDFs and matching them to the CSV")
    [t] = reg.snapshot("Ryan C", names={"gc2": "GC-2"})
    assert set(t) == PUBLIC
    assert t["kind"] == "import-history"
    assert t["title"] == "Importing GC-2 history"
    assert t["instrument"] == "gc2"
    assert t["state"] == "running"
    assert t["progress"] == {"done": 3000, "total": 12000,
                             "text": "Reading CDFs and matching them to the CSV"}
    assert t["by"] == "Ryan C" and t["mine"] is True
    assert t["open_url"] == "/admin/hub#imports"
    assert t["ended_at"] is None and t["started_at"].endswith("+00:00")
    clock.t += 60
    reg.finish(tid, "done")
    [t] = reg.snapshot("Somebody else")
    assert t["state"] == "done" and t["ended_at"] is not None
    assert t["mine"] is False
    assert t["title"] == "Importing gc2 history"     # no name known: the id


def test_titles_per_kind(reg):
    names = {"gc1": "GC-1"}
    cases = [
        (dict(kind="load-folder", instrument="gc1"), "Loading CDFs into GC-1"),
        (dict(kind="import-history-dry-run", instrument="gc1"), "GC-1 history dry run"),
        (dict(kind="purge", instrument="gc1"), "Purging GC-1 data"),
        (dict(kind="diagnostics-bundle"), "Diagnostics bundle"),
        (dict(kind="reports-zip", total=1), "Report ZIP · 1 report"),
        (dict(kind="reports-zip", total=12), "Report ZIP · 12 reports"),
        (dict(kind="qbench-upload", total=3), "QBench upload · 3 reports"),
        (dict(kind="reprocess", total=1), "Re-process · 1 sample"),
        (dict(kind="reprocess", total=40), "Re-process · 40 samples"),
        (dict(kind="something-new"), "Something new"),
    ]
    for kw, want in cases:
        r = tasks.Registry()
        r.begin(kw.pop("kind"), **kw)
        assert r.snapshot(None, names=names)[0]["title"] == want


def test_finished_tasks_stay_30_minutes_then_go(reg, clock):
    a = reg.begin("diagnostics-bundle", by="A")
    b = reg.begin("reports-zip", by="B", owner="B", total=2)
    reg.finish(a, "failed")
    clock.t += tasks.RETAIN_SECONDS - 1
    assert {t["id"] for t in reg.snapshot(None)} == {a, b}
    clock.t += 2
    # the failed one is gone; the running one stays however old it is
    assert [t["id"] for t in reg.snapshot(None)] == [b]


def test_running_first_then_most_recently_ended(reg, clock):
    a = reg.begin("reprocess", total=1)
    clock.t += 1
    b = reg.begin("reprocess", total=2)
    clock.t += 1
    c = reg.begin("reprocess", total=3)
    reg.finish(a, "done")
    clock.t += 1
    reg.finish(b, "stopped")
    assert [t["id"] for t in reg.snapshot(None)] == [c, b, a]


def test_unknown_state_or_task_never_raises(reg):
    tid = reg.begin("reprocess", total=1)
    reg.finish(tid, "exploded")            # not a state: failed
    assert reg.snapshot(None)[0]["state"] == "failed"
    reg.update("nope", done=1)
    reg.finish("nope", "done")
    reg.set_download("nope", None)
    assert len(reg.snapshot(None)) == 1


def test_no_paths_tokens_or_summaries_leak(reg):
    """Only whitelisted fields go out; text a feed gives is capped, never a
    dict (a summary), and progress numbers must be numbers."""
    tid = reg.begin("load-folder", by="R", instrument="gc1", total=5)
    reg.update(tid, done="7", text={"summary": "x"})
    [t] = reg.snapshot("R")
    assert t["progress"] == {"done": None, "total": 5, "text": None}
    reg.update(tid, text="x" * 500)
    assert len(reg.snapshot("R")[0]["progress"]["text"]) <= tasks.TEXT_MAX


def test_download_only_for_the_owner_and_only_until_it_expires(reg, clock):
    tid = reg.begin("reports-zip", by="Ryan C", owner="Ryan C", total=2)
    reg.finish(tid, "done", download="/api/export-analysis-reports-zip/SECRET/download",
               download_until=clock.t + 600)
    mine = reg.snapshot("Ryan C")[0]
    theirs = reg.snapshot("Jo")[0]
    assert mine["download_url"] == "/api/export-analysis-reports-zip/SECRET/download"
    assert theirs["download_url"] is None
    assert "SECRET" not in repr(theirs)
    assert "SECRET" not in tid
    clock.t += 601
    assert reg.snapshot("Ryan C")[0]["download_url"] is None
    tid2 = reg.begin("reports-zip", by="Ryan C", owner="Ryan C", total=1)
    reg.finish(tid2, "done", download="/x", download_until=clock.t + 600)
    reg.set_download(tid2, None)          # fetched: gone
    assert all(t["download_url"] is None for t in reg.snapshot("Ryan C"))


def test_a_dead_thread_makes_a_running_task_interrupted(reg):
    alive = {"v": True}
    tid = reg.begin("qbench-upload", total=3, alive=lambda: alive["v"])
    assert reg.snapshot(None)[0]["state"] == "running"
    alive["v"] = False
    [t] = reg.snapshot(None)
    assert t["state"] == "interrupted" and t["ended_at"] is not None
    reg.finish(tid, "done")               # too late: it stays interrupted
    assert reg.snapshot(None)[0]["state"] == "interrupted"


def test_the_feed_is_thread_safe(reg):
    ids = []

    def worker():
        for _ in range(200):
            t = reg.begin("reprocess", total=1)
            reg.update(t, done=1)
            reg.finish(t, "done")
            ids.append(t)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(set(ids)) == 800
    assert len(reg.snapshot(None)) <= tasks.MAX_KEPT


def test_watch_runs_the_probe_until_it_reports_an_end(reg):
    seen = []

    def probe():
        seen.append(1)
        if len(seen) < 3:
            return {"done": len(seen), "total": 3}
        return {"done": 3, "total": 3, "state": "done", "text": "3 re-processed"}

    tid = reg.begin("reprocess", total=3)
    reg.watch(tid, probe, start=False)
    reg.run_watches()
    assert reg.snapshot(None)[0]["progress"]["done"] == 1
    reg.run_watches()
    reg.run_watches()
    [t] = reg.snapshot(None)
    assert t["state"] == "done" and t["progress"]["text"] == "3 re-processed"
    reg.run_watches()
    assert len(seen) == 3                  # no longer probed


def test_a_broken_probe_fails_the_task_after_a_few_tries(reg):
    def boom():
        raise sqlite3.OperationalError("locked")

    tid = reg.begin("reprocess", total=1)
    reg.watch(tid, boom, start=False)
    for _ in range(tasks.WATCH_MAX_ERRORS - 1):
        reg.run_watches()
    assert reg.snapshot(None)[0]["state"] == "running"
    reg.run_watches()
    assert reg.snapshot(None)[0]["state"] == "failed"


def test_reprocess_probe_counts_job_states(tmp_path):
    db = tmp_path / "gc.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, state TEXT)")
    conn.executemany("INSERT INTO jobs VALUES (?, ?)",
                     [(1, "done"), (2, "running"), (3, "queued"), (4, "failed")])
    conn.commit()
    probe = tasks.jobs_probe([1, 2, 3, 4, 99], db)
    assert probe() == {"done": 2, "total": 4, "text": "2 of 4 re-processed · 1 failed"}
    conn.execute("UPDATE jobs SET state='superseded' WHERE id IN (2, 3)")
    conn.commit()
    out = probe()
    assert out["state"] == "done" and out["done"] == 4
    assert out["text"] == "4 of 4 re-processed · 1 failed"
    conn.execute("UPDATE jobs SET state='failed'")
    conn.commit()
    assert probe()["state"] == "failed"
    conn.close()


def test_snapshot_never_opens_sqlite(reg):
    reg.begin("reprocess", total=1)

    def no_sqlite(*a, **k):
        raise AssertionError("SQLite on the /api/live path")

    with mock.patch.object(sqlite3, "connect", side_effect=no_sqlite):
        t0 = time.perf_counter()
        for _ in range(200):
            reg.snapshot("x")
        assert time.perf_counter() - t0 < 0.5


def test_module_registry_and_helpers():
    assert isinstance(tasks.REGISTRY, tasks.Registry)
    assert tasks.phase_text({"phase": "match", "done": 5, "total": 9}) == \
        "Reading CDFs and matching them to the CSV"
    assert tasks.phase_text({"phase": "scan"}) == "Scanning the folder"
    assert tasks.phase_text({"phase": "import", "file": "/secret/path.CDF"}) == \
        "Classifying samples"
    assert tasks.phase_text({}) is None
    assert tasks.phase_text({"phase": "C:\\x\\y"}) is None     # unknown phases never echo
