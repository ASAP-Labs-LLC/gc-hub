"""Folder loader hardening after review (2A1 T6, M1-M7): the sort key uses the
time ``submit`` will store, ``--process`` is locked and refuses to race a
running hub, partial loads are reported, ``jobs`` is a real package, the CLI
bootstraps like the hub's start-up, and the identity pre-pass reports
progress."""
from __future__ import annotations

from loader_testlib import SIMDIS, hub  # noqa: F401

import importlib.util
import json
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import cdf_fixtures as fx
import pipeline
import store
from jobs import load_folder as load_folder_mod
from jobs.load_folder import load_folder

REPO = Path(__file__).resolve().parent.parent.parent
CLI = REPO / "tools" / "load_folder.py"
DAY = datetime(2026, 9, 25)


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


def _load(hub, folder, **kw):
    kw.setdefault("backfill", False)
    return load_folder("gc1", folder, db=hub.db, data_dir=hub.data, conf=hub.conf, **kw)


def _stampless_blank(path, mtime_ts):
    t = fx._axis()
    y = fx.gaussian(t, 0.30, 50000, 0.01) + 40 + 5 * t
    p = fx.write_cdf(path, t, y, "Blank", at(0, 0), method_name=SIMDIS, raw_stamp="")
    os.utime(p, (mtime_ts, mtime_ts))
    return p


def _submit_order(hub, folder):
    order = []
    worker = hub.worker()

    def progress(e):
        if e["phase"] == "submit":
            order.append(Path(e["file"]).name)
            worker.run_until_idle()

    summary = _load(hub, folder, progress=progress)
    return order, summary


# ── M1: the sort key is the time submit stores ──────────────────────────────

def test_a_stampless_blank_whose_mtime_rounds_down_to_a_samples_time_goes_first(hub, tmp_path):
    """The critic's probe: the blank's file time is 14:23:00.6, stored as 14:23:00;
    the sample is stamped 14:23:00. The blank must go first (blank first at a
    tie), so the sample gets it and no late-blank note."""
    hub.gc1()
    src = tmp_path / "robocopy"
    src.mkdir()
    _stampless_blank(src / "z_blank.CDF", at(14, 23).timestamp() + 0.6)
    fx.sample_cdf(src / "a_sample.CDF", name="40404", injected=at(14, 23), method_name=SIMDIS)

    order, summary = _submit_order(hub, src)

    assert order == ["z_blank.CDF", "a_sample.CDF"]
    assert summary["late_blank_review_notes"] == 0
    b = store.samples.find_by_key("gc1", "Blank", "2026-09-25 14:23:00", db=hub.db)
    s = store.samples.find_by_key("gc1", "40404", "2026-09-25 14:23:00", db=hub.db)
    assert b["injection_dt_source"] == "mtime" and b["is_blank"] == 1
    assert store.get_revision(s["id"], db=hub.db)["blank_used"] == b["id"]


def test_a_stampless_sample_ties_on_its_stored_time(hub, tmp_path):
    """A stamp-less sample at 14:23:00.9 (stored 14:23:00), a stamped sample at
    14:23:00 and a stamped blank at 14:23:00: all tie on the stored time, so the
    blank goes first and the samples follow in path order (the unrounded file
    time would have put the stamp-less one last)."""
    hub.gc1()
    src = tmp_path / "robocopy"
    src.mkdir()
    t = fx._axis()
    y = fx.gaussian(t, 0.30, 50000, 0.01) + fx.gaussian(t, 3.2, 1200, 0.9) + 40 + 5 * t
    s = fx.write_cdf(src / "a_sample.CDF", t, y, "40405", at(0, 0), method_name=SIMDIS, raw_stamp="")
    ts = at(14, 23).timestamp() + 0.9
    os.utime(s, (ts, ts))
    fx.sample_cdf(src / "b_sample.CDF", name="40406", injected=at(14, 23), method_name=SIMDIS)
    fx.blank_cdf(src / "c_blank.CDF", injected=at(14, 23), method_name=SIMDIS)

    order, summary = _submit_order(hub, src)

    assert order == ["c_blank.CDF", "a_sample.CDF", "b_sample.CDF"]
    assert summary["late_blank_review_notes"] == 0
    stored = store.samples.find_by_key("gc1", "40405", "2026-09-25 14:23:00", db=hub.db)
    assert stored["injection_dt_source"] == "mtime"


def test_the_order_key_uses_the_truncated_mtime(tmp_path):
    p = _stampless_blank(tmp_path / "b.CDF", at(14, 23).timestamp() + 0.6)
    key = load_folder_mod._order_key(p, tmp_path)
    assert key[0] == at(14, 23)


# ── M7: progress during the identity pre-pass ───────────────────────────────

def test_the_identity_pre_pass_reports_progress(hub, tmp_path):
    hub.gc1()
    src = tmp_path / "robocopy"
    src.mkdir()
    fx.sample_cdf(src / "a.CDF", name="40401", injected=at(14, 23), method_name=SIMDIS)
    fx.sample_cdf(src / "b.CDF", name="40402", injected=at(15, 23), method_name=SIMDIS)
    events = []
    _load(hub, src, progress=events.append)
    phases = [e["phase"] for e in events]
    assert phases.index("scan") < phases.index("identify") < phases.index("submit")
    ident = [e for e in events if e["phase"] == "identify"]
    assert [(e["done"], e["total"]) for e in ident] == [(1, 2), (2, 2)]
    assert all(Path(e["file"]).name in {"a.CDF", "b.CDF"} for e in ident)


# ── M3: a load that stops partway still reports what it did ─────────────────

def test_a_load_that_stops_partway_carries_its_summary(hub, tmp_path):
    hub.gc1()
    src = tmp_path / "robocopy"
    src.mkdir()
    fx.sample_cdf(src / "a.CDF", name="40401", injected=at(14, 23), method_name=SIMDIS)
    fx.sample_cdf(src / "b.CDF", name="40402", injected=at(15, 23), method_name=SIMDIS)

    def progress(e):
        if e["phase"] == "submit" and e["done"] == 1:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt) as got:
        _load(hub, src, progress=progress)
    partial = got.value.load_summary
    assert partial["created"] == 1 and partial["files"] == 2
    assert partial["stopped"] and "KeyboardInterrupt" in partial["stopped"]


def _cli_module():
    spec = importlib.util.spec_from_file_location("load_folder_cli", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_prints_the_partial_summary_when_the_load_stops(hub, tmp_path, monkeypatch, capsys):
    hub.gc1()
    src = tmp_path / "robocopy"
    src.mkdir()
    fx.sample_cdf(src / "a.CDF", name="40401", injected=at(14, 23), method_name=SIMDIS)
    fx.sample_cdf(src / "b.CDF", name="40402", injected=at(15, 23), method_name=SIMDIS)
    real = pipeline.submit
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(pipeline, "submit", flaky)
    code = _cli_module().main(["--data-dir", str(hub.data), "--json", "gc1", str(src)])
    out = capsys.readouterr()
    assert code == 3
    summary = json.loads(out.out)
    assert summary["created"] == 1 and "database is locked" in summary["stopped"]
    assert "stopped" in out.err


# ── M6: the CLI bootstraps like the hub's start-up ──────────────────────────

def _cli(*args, env=None):
    e = dict(os.environ)
    e.pop("GC_DATA_DIR", None)
    e.update(env or {})
    return subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True,
                          text=True, env=e, timeout=120)


def test_cli_on_a_fresh_data_folder_creates_the_store_and_gc1(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    src = tmp_path / "robocopy"
    src.mkdir()
    fx.sample_cdf(src / "a.CDF", name="40401", injected=at(14, 23), method_name=SIMDIS)
    res = _cli("--data-dir", data, "--json", "gc1", src)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["created"] == 1
    inst = store.instruments.get("gc1", db=data / "gc.db")
    assert inst is not None and inst["live_since"]
    assert store.jobs.list(state="queued", db=data / "gc.db")      # no worker ran


# ── M2: --process is locked and never races a running hub ───────────────────

def test_process_refuses_when_the_hub_port_answers(hub, tmp_path):
    hub.gc1()
    src = tmp_path / "robocopy"
    src.mkdir()
    fx.sample_cdf(src / "a.CDF", name="40401", method_name=SIMDIS)
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        res = _cli("--data-dir", hub.data, "--process", "--hub-port", port, "gc1", src)
    assert res.returncode == 2
    assert "running" in res.stderr
    assert store.samples.count(db=hub.db) == 0                    # refused before loading


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_process_refuses_while_another_loader_holds_the_lock(hub, tmp_path):
    hub.gc1()
    src = tmp_path / "robocopy"
    src.mkdir()
    fx.sample_cdf(src / "a.CDF", name="40401", method_name=SIMDIS)
    lock = hub.data / load_folder_mod.PROCESS_LOCK
    lock.write_text("12345")
    res = _cli("--data-dir", hub.data, "--process", "--hub-port", _free_port(), "gc1", src)
    assert res.returncode == 2
    assert str(lock.name) in res.stderr
    assert lock.read_text() == "12345"                             # not ours to remove


def test_process_requeues_under_the_lock_and_runs_the_queue(hub, tmp_path):
    hub.gc1()
    first = hub.submit(hub.cdf(name="40400"))
    (job,) = store.jobs.list(state="queued", db=hub.db)
    store.jobs.claim_next(db=hub.db)                                 # left 'running' by a crash
    src = tmp_path / "robocopy"
    src.mkdir()
    fx.sample_cdf(src / "a.CDF", name="40401", injected=at(15, 23), method_name=SIMDIS)
    settings_json = hub.data / "settings.json"
    settings_json.write_text(json.dumps(hub.conf), encoding="utf-8")
    res = _cli("--data-dir", hub.data, "--process", "--hub-port", _free_port(), "gc1", src)
    assert res.returncode == 0, res.stderr + res.stdout
    assert not (hub.data / load_folder_mod.PROCESS_LOCK).exists()
    assert hub.sample(first.sample_id)["status"] == "final"         # the stale job was requeued
    s = store.samples.find_by_key("gc1", "40401", "2026-09-25 15:23:00", db=hub.db)
    assert s["status"] == "final"


# ── M5: jobs is a regular package ───────────────────────────────────────────

def test_jobs_is_the_repo_package():
    import jobs
    assert Path(jobs.__file__).resolve() == (REPO / "jobs" / "__init__.py").resolve()
    assert Path(load_folder_mod.__file__).resolve().parent == (REPO / "jobs").resolve()
