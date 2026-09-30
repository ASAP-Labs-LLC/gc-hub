"""Start-up recovery never touches work that is live in this process (v3.1
re-review B1), and admin jobs wait for it.

``hub.start`` runs on a retrying background thread while Flask already
serves, and ``_restart_hub`` calls it again after a failed respawn. So:

* the recovery (``purge.recover``, ``import_history.mark_interrupted``) runs
  once per process and data folder, and ``hub.startup_recovered(data)`` says
  when it has; until then every ``AdminJobs`` start is refused 503 "the hub is
  still starting";
* even called again, it skips a purge journal or an import run that a job in
  this process owns: the critic's case, a live purge held after its backup
  while ``hub.recover_purges`` runs, keeps its backup and finishes ``done``;
  a live import keeps its run and its staged file;
* while a restart is claimed (Restart & install waiting for the updater, the
  3 AM restart's last second) no admin job of any kind starts (409).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("netCDF4")
pytest.importorskip("flask")

import purge_helpers as ph  # noqa: E402

import hub  # noqa: E402
import hub_admin  # noqa: E402
import pipeline  # noqa: E402
import purge  # noqa: E402
import store  # noqa: E402


@pytest.fixture(scope="module")
def template():
    root = Path(tempfile.mkdtemp(prefix="gc-live-rec-tpl-"))
    try:
        yield ph.build_two_gc_hub(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def h(template, tmp_path):
    return ph.HubCopy(template, tmp_path)


def _until(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_recovery_during_a_live_purge_leaves_it_alone(h, monkeypatch):
    sid = h.ids["final"]
    job = store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid, db=h.db)
    orig = purge._backup

    def backup(db, dest):
        out = orig(db, dest)
        with store.connection(h.db) as c:            # hold the purge after its backup
            c.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))
        return out
    monkeypatch.setattr(purge, "_backup", backup)
    res = {}
    t = threading.Thread(target=lambda: res.update(s=h.run(poll_seconds=0.05, wait_seconds=60)))
    t.start()
    assert _until(lambda: list((h.data / "backups").glob("pre-purge-*")))
    [bk] = list((h.data / "backups").glob("pre-purge-*"))
    notes = []
    out = hub.recover_purges(h.db, h.data, lambda lvl, m: notes.append(m))   # what hub.start calls
    assert out == [] and notes == []
    assert bk.is_file()
    store.jobs.complete(job, db=h.db)
    t.join(60)
    assert res["s"]["state"] == "done" and Path(res["s"]["backup"]).is_file()
    [j] = [json.loads(m.read_text()) for m in (h.data / "purged").glob("*/manifest.json")]
    assert j["state"] == "done"


def test_recovery_during_a_live_import_leaves_it_alone(h, monkeypatch):
    run_id = store.import_runs.start("gc1", by="x", sources={}, db=h.db)
    staged = h.data / "cdf" / pipeline.INCOMING_DIR / "import-live.CDF"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"staged")
    from jobs import import_history
    with import_history.running(run_id):             # what import_history does while it runs
        notes = []
        assert hub.mark_interrupted_imports(h.db, h.data, lambda l, m: notes.append(m)) == []
        assert notes == [] and staged.is_file()
        assert store.import_runs.list("gc1", db=h.db)[0]["finished_at"] is None
    # once it is no longer running here, the same run counts as interrupted
    assert [r["id"] for r in hub.mark_interrupted_imports(h.db, h.data, None)] == [run_id]
    assert not staged.exists()


def test_a_running_admin_job_protects_its_instrument_too(h, monkeypatch):
    """Belt and braces: a purge job in AdminJobs protects that instrument's
    journals even before purge.run registered one."""
    journal_dir = h.data / "purged" / "gc1-x"
    journal_dir.mkdir(parents=True)
    (journal_dir / "manifest.json").write_text(json.dumps({
        "state": "pending", "instrument": "gc1", "sample_ids": [h.ids["final"]],
        "backup": str(h.data / "backups" / "nope.db")}))

    class Jobs:
        def current(self):
            return {"state": "running", "kind": "purge", "params": {"instrument": "gc1"}}
    monkeypatch.setattr(hub_admin, "JOBS", Jobs())
    assert hub.recover_purges(h.db, h.data, None) == []
    assert json.loads((journal_dir / "manifest.json").read_text())["state"] == "pending"


def test_recovery_runs_once_per_process_and_data_folder(h):
    hub.forget_startup_recovery(h.data)
    assert not hub.startup_recovered(h.data)
    first = hub.run_startup_recovery(h.db, h.data, None)
    assert first is not None and hub.startup_recovered(h.data)
    # a pending journal appearing afterwards (a purge started since) is never
    # touched by a later hub.start in this process
    journal_dir = h.data / "purged" / "gc1-later"
    journal_dir.mkdir(parents=True)
    (journal_dir / "manifest.json").write_text(json.dumps({
        "state": "pending", "instrument": "gc1", "sample_ids": [h.ids["final"]],
        "backup": str(h.data / "backups" / "later.db")}))
    assert hub.run_startup_recovery(h.db, h.data, None) is None
    assert json.loads((journal_dir / "manifest.json").read_text())["state"] == "pending"
    hub.forget_startup_recovery(h.data)


def test_admin_jobs_wait_for_the_start_up_recovery_and_a_claimed_restart(h, monkeypatch):
    monkeypatch.setenv("GC_DATA_DIR", str(h.data))
    jobs = hub_admin.AdminJobs()
    jobs.refuse = hub_admin.job_refusal
    hub.forget_startup_recovery(h.data)
    for kind in ("load-folder", "import-history", "import-history-dry-run", "purge"):
        with pytest.raises(hub_admin.JobRefused) as exc:
            jobs.start(kind, lambda progress: {}, {})
        assert exc.value.status == 503 and "still starting" in str(exc.value)
    hub.run_startup_recovery(h.db, h.data, None)
    claimed = {"v": True}
    monkeypatch.setattr(hub_admin, "_restart_claimed", lambda: claimed["v"])
    with pytest.raises(hub_admin.JobRefused) as exc:
        jobs.start("purge", lambda progress: {}, {})
    assert exc.value.status == 409 and "restart" in str(exc.value)
    claimed["v"] = False
    job = jobs.start("load-folder", lambda progress: {"ok": True}, {})
    assert job["kind"] == "load-folder"
    hub.forget_startup_recovery(h.data)
