"""The hub's one start-up call (2A1 T5, integration carry-over I4).

``hub.start(app_conf)`` owns everything that runs in the background: store
migrate, the gc1 bootstrap, the pipeline Worker (notifier = the notification
store, corrections from the hub store first, else the phase-1 file for gc1),
``HubExporter(notifier).start()``, the nightly backup and ``jobs.prune_done``.
``app._init_app`` calls it; the boot test below proves a freshly booted hub
turns a submitted CDF into a final sample and an appended export row with no
worker run by the test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

import corrections  # noqa: E402
import distill  # noqa: E402
import exports  # noqa: E402
import hub  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402
from bootapp import ROOT, booted, free_port, get, wait_for  # noqa: E402
from hub_boot import SIMDIS, Hub  # noqa: E402

import cdf_fixtures as fx  # noqa: E402


def _hub_folder(tmp: Path) -> Hub:
    distill._CAL_CACHE.clear()
    return Hub(tmp)                       # tmp/data with settings.json, no store yet


def _stop_all(rt):
    rt.stop()
    assert not rt.worker.is_alive()


# ── in process ────────────────────────────────────────────────────────────

def test_start_migrates_bootstraps_and_runs_everything(tmp_path):
    h = _hub_folder(tmp_path)
    notes = []
    rt = hub.start(h.conf, data_dir=h.data, notifier=lambda lvl, msg: notes.append((lvl, msg)),
                   conf_fn=lambda: h.conf, maintenance=False)
    try:
        assert h.db.is_file()
        assert store.instruments.get("gc1", db=h.db) is not None
        assert rt.worker.is_alive() and rt.exporter_alive()
        assert hub.running() is rt
    finally:
        _stop_all(rt)
    assert hub.running() is None
    # a second start in the same process works after a clean stop
    rt2 = hub.start(h.conf, data_dir=h.data, notifier=None, conf_fn=lambda: h.conf,
                    maintenance=False)
    _stop_all(rt2)


def test_start_warns_about_an_instrument_with_a_reserved_id(tmp_path):
    """v3.1: an instrument created before ids like "classic" were reserved
    can't be opened at /instruments/<id>; the hub says so at start."""
    h = _hub_folder(tmp_path)
    store.migrate(h.db)
    store.instruments.upsert({"id": "classic", "name": "Old GC"}, db=h.db)
    notes = []
    rt = hub.start(h.conf, data_dir=h.data, notifier=lambda lvl, msg: notes.append((lvl, msg)),
                   conf_fn=lambda: h.conf, maintenance=False)
    _stop_all(rt)
    warn = [m for lvl, m in notes if lvl == "warning" and "reserved" in m]
    assert len(warn) == 1 and "Old GC" in warn[0] and "classic" in warn[0]
    assert "/instruments/classic?instrument=classic" in warn[0]


def test_final_sample_is_flushed_without_waiting_for_the_export_interval(tmp_path):
    h = _hub_folder(tmp_path)
    rt = hub.start(h.conf, data_dir=h.data, notifier=None, conf_fn=lambda: h.conf,
                   export_interval=3600, maintenance=False)
    try:
        store.instruments.upsert({"id": "gc1", "live_since": datetime(2020, 1, 1)}, db=h.db)
        cdf = fx.sample_cdf(h.src / "s.CDF", name="40304",
                            injected=datetime(2026, 9, 25, 14, 23, 0), method_name=SIMDIS)
        res = pipeline.submit("gc1", cdf, conf=h.conf, data_dir=h.data, db=h.db)
        rt.worker.wake()
        out = h.data / "results" / "gc1_results.csv"
        assert wait_for(lambda: store.samples.get(res.sample_id, db=h.db)["status"] == "final",
                        timeout=30), store.samples.get(res.sample_id, db=h.db)
        assert wait_for(lambda: out.is_file() and b"40304," in out.read_bytes(), timeout=15)
        assert out.read_bytes().startswith(exports.header_line().encode())
    finally:
        _stop_all(rt)


def test_corrections_are_hub_owned(tmp_path):
    h = _hub_folder(tmp_path)
    store.migrate(h.db)
    import instruments
    instruments.bootstrap_gc1(h.conf, db=h.db)
    provider = hub.corrections_provider(h.db)
    # nothing saved in the hub: never the file, never zeros
    with pytest.raises(corrections.CorrectionsUnavailable, match="Corrections not set"):
        provider.get({"id": "gc1", "name": "GC-1"})
    values = {k: v + 1.0 for k, v in
              corrections.seed_from_file(h.conf["correction_factors_json"]).items()}
    with store.connection(h.db) as conn, store.write_txn(conn):
        store.corrections.set_all(conn, "gc1", values, by="test", reason="t")
    got = provider.get({"id": "gc1", "name": "GC-1"})
    assert got.source == "hub" and got.values == values


def test_worker_default_corrections_are_the_hubs_never_the_file(tmp_path):
    # I4: a Worker built without a provider must not read the phase-1 file,
    # or hub-edited corrections could be silently ignored.
    h = _hub_folder(tmp_path)
    store.migrate(h.db)
    import instruments
    instruments.bootstrap_gc1(h.conf, db=h.db, now=datetime(2020, 1, 1))
    cdf = fx.sample_cdf(h.src / "d.CDF", name="40304",
                        injected=datetime(2026, 9, 25, 14, 23, 0), method_name=SIMDIS)
    sid = pipeline.submit("gc1", cdf, conf=h.conf, data_dir=h.data, db=h.db).sample_id
    w = pipeline.Worker(db=h.db, data_dir=h.data, conf_fn=lambda: h.conf)
    w.run_until_idle()
    assert store.samples.get(sid, db=h.db)["status"] == "pending_corrections"
    values = corrections.seed_from_file(h.conf["correction_factors_json"])
    with store.connection(h.db) as conn, store.write_txn(conn):
        store.corrections.set_all(conn, "gc1", values, by="test", reason="t")
    pipeline.requeue_on_start(db=h.db)
    w.run_until_idle()
    assert store.samples.get(sid, db=h.db)["status"] == "final"
    used = json.loads(store.get_revision(sid, db=h.db)["corrections_used"])
    assert used["source"] == "hub"


def test_first_start_seeds_gc1_corrections_from_the_phase1_file(tmp_path):
    h = _hub_folder(tmp_path)
    notes = []
    rt = hub.start(h.conf, data_dir=h.data, notifier=lambda lvl, msg: notes.append((lvl, msg)),
                   conf_fn=lambda: h.conf, maintenance=False)
    rt.stop()
    rec = store.corrections.read("gc1", db=h.db)
    assert rec is not None and rec["updated_by"] == "startup"
    assert rec["values"] == corrections.seed_from_file(h.conf["correction_factors_json"])
    audit = store.corrections.audit("gc1", db=h.db)
    assert audit and all(a["reason"] == "seeded from correction_factors.json" for a in audit)
    assert [lvl for lvl, _ in notes] == ["info"]
    # a later start never re-seeds over the hub's values (even from a changed file)
    doc = json.loads(Path(h.conf["correction_factors_json"]).read_text())
    for entry in doc["Agilent GC"].values():
        entry["correction_value"] = 9.0
    Path(h.conf["correction_factors_json"]).write_text(json.dumps(doc))
    rt = hub.start(h.conf, data_dir=h.data, notifier=None, conf_fn=lambda: h.conf,
                   maintenance=False)
    rt.stop()
    assert store.corrections.read("gc1", db=h.db)["values"] == rec["values"]


@pytest.mark.parametrize("path", ["missing.json", ""])
def test_unusable_phase1_file_leaves_gc1_pending_and_says_so(tmp_path, path):
    h = _hub_folder(tmp_path)
    conf = dict(h.conf, correction_factors_json=str(tmp_path / path) if path else "")
    notes = []
    rt = hub.start(conf, data_dir=h.data, notifier=lambda lvl, msg: notes.append((lvl, msg)),
                   conf_fn=lambda: conf, maintenance=False)
    try:
        assert store.corrections.read("gc1", db=h.db) is None
        assert [lvl for lvl, _ in notes] == ["error"] and "GC-1" in notes[0][1]
        store.instruments.upsert({"id": "gc1", "live_since": datetime(2020, 1, 1)}, db=h.db)
        cdf = fx.sample_cdf(h.src / "p.CDF", name="40304",
                            injected=datetime(2026, 9, 25, 14, 23, 0), method_name=SIMDIS)
        res = pipeline.submit("gc1", cdf, conf=conf, data_dir=h.data, db=h.db)
        rt.worker.wake()
        assert wait_for(lambda: store.samples.get(res.sample_id, db=h.db)["status"]
                        == "pending_corrections", timeout=30)
        assert store.samples.get(res.sample_id, db=h.db)["current_revision"] is None
    finally:
        rt.stop()


def test_maintenance_backs_up_once_a_day_and_prunes_done_jobs(tmp_path):
    h = _hub_folder(tmp_path)
    store.migrate(h.db)
    notes = []
    m = hub.Maintenance(h.db, h.data, notifier=lambda lvl, msg: notes.append((lvl, msg)))
    day = datetime(2026, 9, 28, 1, 30)
    early = datetime(2026, 9, 28, 0, 30)
    assert m.run_once(now=early)["backup"] is None           # before BACKUP_HOUR
    assert m.run_once(now=day)["backup"] == h.data / "backups" / "gc-2026-09-28.db"
    assert (h.data / "backups" / "gc-2026-09-28.db").is_file()
    assert m.run_once(now=day + timedelta(hours=1))["backup"] is None   # once a day
    # a restart doesn't back up twice: the file for the day is the record
    assert hub.Maintenance(h.db, h.data).run_once(now=day + timedelta(hours=2))["backup"] is None

    # prune: a done job finished long ago goes, a recent one stays
    with store.connection(h.db) as conn, store.write_txn(conn):
        for days in (40, 1):
            conn.execute("INSERT INTO jobs(kind, state, payload, attempts, created_at, finished_at) "
                         "VALUES ('process', 'done', '{}', 0, ?, ?)",
                         (store.now_iso(), store._ts((day - timedelta(days=days)).astimezone())))
    assert m.run_once(now=day + timedelta(days=1, hours=1))["pruned"] == 1
    assert len(store.jobs.list(state="done", db=h.db)) == 1
    assert notes == []


def test_maintenance_prunes_sessions_that_ended_over_30_days_ago(tmp_path):
    from datetime import timezone
    h = _hub_folder(tmp_path)
    store.migrate(h.db)
    now = datetime.now(timezone.utc)
    iso = lambda d: d.isoformat(timespec="microseconds")  # noqa: E731
    store.web_sessions.add("old", name="A", method="password", ip=None, user_agent=None,
                           expires_at=iso(now - timedelta(days=31)), db=h.db)
    store.web_sessions.add("live", name="B", method="password", ip=None, user_agent=None,
                           expires_at=iso(now + timedelta(days=1)), db=h.db)
    res = hub.Maintenance(h.db, h.data).run_once(now=datetime.now())
    assert res["sessions_pruned"] == 1
    assert [s["name"] for s in store.web_sessions.list_active(db=h.db)] == ["B"]


def test_a_failed_backup_notifies_once_and_retries_later(tmp_path, monkeypatch):
    h = _hub_folder(tmp_path)
    store.migrate(h.db)
    notes = []
    m = hub.Maintenance(h.db, h.data, notifier=lambda lvl, msg: notes.append((lvl, msg)))

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(store, "backup_nightly", boom)
    day = datetime(2026, 9, 28, 2, 0)
    assert m.run_once(now=day)["backup"] is None
    assert m.run_once(now=day + timedelta(minutes=5))["backup"] is None
    assert [lvl for lvl, _ in notes] == ["error"] and "disk full" in notes[0][1]
    monkeypatch.undo()
    assert m.run_once(now=day + timedelta(hours=2))["backup"] is not None


# ── the real app ──────────────────────────────────────────────────────────

def test_booted_hub_processes_and_exports_with_no_worker_in_the_test(tmp_path):
    h = _hub_folder(tmp_path)
    with booted(tmp_path) as (port, _proc, data, _home):
        assert data == h.data
        assert wait_for(lambda: h.db.is_file()
                        and store.instruments.get("gc1", db=h.db) is not None, timeout=30)
        store.instruments.upsert({"id": "gc1", "live_since": datetime(2020, 1, 1)}, db=h.db)
        cdf = fx.sample_cdf(h.src / "boot.CDF", name="BOOT-1",
                            injected=datetime(2026, 9, 25, 14, 23, 0), method_name=SIMDIS)
        res = pipeline.submit("gc1", cdf, conf=h.conf, data_dir=h.data, db=h.db)
        assert res.outcome == "created"
        assert wait_for(lambda: store.samples.get(res.sample_id, db=h.db)["status"] == "final",
                        timeout=60), store.samples.get(res.sample_id, db=h.db)
        out = h.data / "results" / "gc1_results.csv"
        assert wait_for(lambda: out.is_file() and b"BOOT-1," in out.read_bytes(), timeout=30), \
            (tmp_path / "boot.log").read_text(errors="replace")[-3000:]
        ledger = store.export_rows.rows_after("gc1", 0, db=h.db)
        assert out.read_bytes() == (exports.header_line() + ledger[0]["line"]).encode()
        code, body = get(port, "/api/files")
        assert code == 200 and body["samples"][0]["status"] == "final"


def test_app_refuses_to_start_without_gc_data_dir(tmp_path):
    env = dict(os.environ, PORT=str(free_port()), HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    env.pop("GC_DATA_DIR", None)
    try:
        r = subprocess.run([sys.executable, "app.py", "--no-tray"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise AssertionError("app.py started without GC_DATA_DIR")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "GC_DATA_DIR" in out and "DEPLOY.md" in out
    assert list(tmp_path.iterdir()) == []       # nothing written to HOME either
