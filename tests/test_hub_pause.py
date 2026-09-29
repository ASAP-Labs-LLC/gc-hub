"""Pausing the hub's background work (the hub tray's "Pause processing").

``HubRuntime.pause()`` stops the Worker, the exporter and maintenance while
the runtime (and the web app) stays up; ``resume()`` starts them again. The
choice is persisted in ``settings_kv`` (``hub.set_processing_paused``) so a
hub started while it is set comes up paused. Ingest keeps accepting: a CDF
submitted while paused waits as ``received`` and is processed on resume.
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

import distill  # noqa: E402
import hub  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402
from bootapp import wait_for  # noqa: E402
from hub_boot import SIMDIS, Hub  # noqa: E402

import cdf_fixtures as fx  # noqa: E402


def _hub_folder(tmp: Path) -> Hub:
    distill._CAL_CACHE.clear()
    return Hub(tmp)


def _start(h: Hub, **kw):
    kw.setdefault("maintenance", True)
    return hub.start(h.conf, data_dir=h.data, notifier=None, conf_fn=lambda: h.conf,
                     export_interval=3600, maintenance_interval=3600, **kw)


def test_pause_stops_the_background_threads_and_resume_restarts_them(tmp_path):
    h = _hub_folder(tmp_path)
    rt = _start(h)
    try:
        assert rt.worker.is_alive() and rt.exporter_alive() and rt.maintenance.is_alive()
        assert rt.paused is False
        rt.pause()
        assert rt.paused is True
        assert not rt.worker.is_alive()
        assert not rt.exporter_alive()
        assert not rt.maintenance.is_alive()
        assert hub.running() is rt              # the runtime itself stays
        rt.pause()                              # idempotent
        rt.resume()
        assert rt.paused is False
        assert rt.worker.is_alive() and rt.exporter_alive() and rt.maintenance.is_alive()
        rt.resume()                             # idempotent
        assert rt.worker.is_alive()
    finally:
        rt.stop()
    assert hub.running() is None


def test_ingest_queues_while_paused_and_is_processed_on_resume(tmp_path):
    h = _hub_folder(tmp_path)
    rt = _start(h, maintenance=False)
    try:
        store.instruments.upsert({"id": "gc1", "live_since": datetime(2020, 1, 1)}, db=h.db)
        rt.pause()
        cdf = fx.sample_cdf(h.src / "p.CDF", name="PAUSE-1",
                            injected=datetime(2026, 9, 25, 14, 23, 0), method_name=SIMDIS)
        res = pipeline.submit("gc1", cdf, conf=h.conf, data_dir=h.data, db=h.db)
        assert res.outcome == "created"
        time.sleep(3.0)                         # well past the Worker's 2 s poll
        assert store.samples.get(res.sample_id, db=h.db)["status"] == "received"
        rt.resume()
        assert wait_for(lambda: store.samples.get(res.sample_id, db=h.db)["status"] == "final",
                        timeout=60), store.samples.get(res.sample_id, db=h.db)
        out = h.data / "results" / "gc1_results.csv"
        assert wait_for(lambda: out.is_file() and b"PAUSE-1," in out.read_bytes(), timeout=30)
    finally:
        rt.stop()


def test_the_paused_flag_is_persisted_and_a_start_honours_it(tmp_path):
    h = _hub_folder(tmp_path)
    store.migrate(h.db)
    assert hub.processing_paused(h.db) is None
    hub.set_processing_paused(h.db, True, by="127.0.0.1")
    got = hub.processing_paused(h.db)
    assert got["by"] == "127.0.0.1" and got["since"]
    rt = _start(h)
    try:
        assert rt.paused is True
        assert not rt.worker.is_alive() and not rt.exporter_alive()
        assert rt.maintenance is None or not rt.maintenance.is_alive()
        rt.resume()
        assert rt.worker.is_alive() and rt.exporter_alive()
        assert rt.maintenance is not None and rt.maintenance.is_alive()
    finally:
        rt.stop()
    hub.set_processing_paused(h.db, False)
    assert hub.processing_paused(h.db) is None
    rt = _start(h)
    try:
        assert rt.paused is False and rt.worker.is_alive()
    finally:
        rt.stop()


def test_an_explicit_paused_argument_wins_over_the_store(tmp_path):
    h = _hub_folder(tmp_path)
    rt = _start(h, paused=True, maintenance=False)
    try:
        assert rt.paused is True and not rt.worker.is_alive()
    finally:
        rt.stop()
    assert hub.running() is None


def _named(name):
    return [t for t in threading.enumerate() if t.name == name and t.is_alive()]


def test_a_pause_that_times_out_never_leaves_duplicate_threads(tmp_path):
    # Review harness dup_threads.py: an export append and a maintenance pass
    # stuck for 1 s outlive pause(timeout=0.2); resume must not start a second
    # thread beside the old one, and "alive" must be the real thread state.
    h = _hub_folder(tmp_path)
    rt = hub.start(h.conf, data_dir=h.data, notifier=None, conf_fn=lambda: h.conf,
                   export_interval=0.05, maintenance=True, maintenance_interval=0.05)
    try:
        orig_tick, orig_run = rt.exporter.tick, rt.maintenance.run_once

        def slow_tick(*a, **k):
            time.sleep(1.0)
            return orig_tick(*a, **k)

        def slow_run(*a, **k):
            time.sleep(1.0)
            return orig_run(*a, **k)

        rt.exporter.tick = slow_tick
        rt.maintenance.run_once = slow_run
        time.sleep(0.3)
        rt.pause(timeout=0.2)
        assert rt.paused is True
        assert rt.exporter_alive() == bool(_named("gc-hub-exports"))
        assert rt.maintenance.is_alive() == bool(_named("gc-hub-maintenance"))
        rt.resume()
        for _ in range(25):
            assert len(_named("gc-hub-exports")) <= 1
            assert len(_named("gc-hub-maintenance")) <= 1
            time.sleep(0.1)
        assert len(_named("gc-hub-exports")) == 1 and rt.exporter_alive()
        assert len(_named("gc-hub-maintenance")) == 1 and rt.maintenance.is_alive()
        rt.pause(timeout=0.2)
        assert wait_for(lambda: not _named("gc-hub-exports")
                        and not _named("gc-hub-maintenance"), timeout=5)
        assert not rt.exporter_alive() and not rt.maintenance.is_alive()
    finally:
        rt.stop()


def test_background_busy_ignores_due_jobs_while_processing_is_paused(tmp_path):
    # the 3 AM restart must not be held up by work that cannot run
    h = _hub_folder(tmp_path)
    store.migrate(h.db)
    store.jobs.enqueue("process", {"x": 1}, db=h.db)
    assert hub.background_busy(h.db) is True
    hub.set_processing_paused(h.db, True, by="t")
    assert hub.background_busy(h.db) is False
    with store.connection(h.db) as conn:
        conn.execute("UPDATE jobs SET state='running'")
        conn.commit()
    assert hub.background_busy(h.db) is True     # something really running still counts
