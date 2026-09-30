"""Live updates (v3.1): every publisher the spec lists puts an event on
``live.BUS``. In process, on real stores (never ``import app``).

Publishers: the pipeline (``submit``, the Worker, reprocess, Export to LIMS,
backfill release, conflict Replace), ``ingest_api`` (heartbeat, agent
command, token mint/revoke), ``instrument_admin`` (every change), the
notification store, the exporter and ``hub_control`` (its tests are in
``test_hub_control.py``); the QBench upload record in ``app.py`` is checked by
AST.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "pipeline", TESTS / "exports", TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

from pipeline_helpers import hub  # noqa: E402,F401  (the fixture)

import corrections  # noqa: E402
import live  # noqa: E402
import store  # noqa: E402

ELEVEN = {cut: float(i) / 10 for i, cut in enumerate(corrections.D86_CUTS)}


class Watch:
    """Events published after it was made."""

    def __init__(self):
        self.cursor = live.BUS.cursor()

    def out(self) -> dict:
        return live.BUS.since(self.cursor)

    def samples(self) -> list:
        return self.out()["samples"]

    def instruments(self) -> list:
        return self.out()["instruments"]

    def kinds(self) -> list:
        return self.out()["kinds"]


# ── pipeline ────────────────────────────────────────────────────────────────

def test_submit_publishes_the_new_sample(hub):
    hub.gc1()
    w = Watch()
    res = hub.submit(hub.cdf())
    assert res.outcome == "created"
    assert w.samples() == [res.sample_id]


def test_a_duplicate_submit_publishes_nothing(hub):
    hub.gc1()
    cdf = hub.cdf()
    hub.submit(cdf)
    w = Watch()
    assert hub.submit(cdf).outcome == "duplicate"
    assert w.kinds() == []


def test_a_conflict_publishes_the_instrument(hub):
    from datetime import datetime
    hub.gc1()
    t = datetime(2026, 9, 25, 14, 23, 0)
    hub.submit(hub.cdf(injected=t))
    w = Watch()
    res = hub.submit(hub.cdf(injected=t, shift=0.05))
    assert res.outcome == "conflict"
    assert "gc1" in w.instruments()


def test_the_worker_publishes_each_processed_sample(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    w = Watch()
    hub.worker().run_until_idle()
    assert hub.sample(sid)["status"] == "final"
    assert sid in w.samples()


def test_the_worker_publishes_a_hold(hub):
    hub.gc2()                      # no calibration: held awaiting_calibration
    sid = hub.submit(hub.cdf(), instrument="gc2").sample_id
    w = Watch()
    hub.worker().run_until_idle()
    assert hub.sample(sid)["status"] != "final"
    assert sid in w.samples()


def test_reprocess_export_to_lims_and_release_publish(hub):
    import pipeline
    from datetime import datetime
    hub.gc1(live_since=datetime(2026, 9, 26))           # the sample is backfill
    sid = hub.submit(hub.cdf()).sample_id
    hub.worker().run_until_idle()
    w = Watch()
    pipeline.request_reprocess(sid, by="t", db=hub.db)
    assert w.samples() == [sid]
    w = Watch()
    pipeline.release_backfill(sid, by="t", db=hub.db, data_dir=hub.data)
    assert w.samples() == [sid]
    w = Watch()
    pipeline.export_to_lims(sid, by="t", db=hub.db, data_dir=hub.data)
    assert w.samples() == [sid]


def test_conflict_replace_publishes(hub):
    import pipeline
    from datetime import datetime
    hub.gc1()
    t = datetime(2026, 9, 25, 14, 23, 0)
    sid = hub.submit(hub.cdf(injected=t)).sample_id
    cid = hub.submit(hub.cdf(injected=t, shift=0.05)).conflict_id
    w = Watch()
    pipeline.resolve_conflict_replace(cid, by="t", db=hub.db, data_dir=hub.data)
    assert sid in w.samples() and "gc1" in w.instruments()


def test_a_failing_bus_never_breaks_submit(hub, monkeypatch):
    hub.gc1()
    monkeypatch.setattr(live.BUS, "_lock", None)        # publish would raise inside
    res = hub.submit(hub.cdf())
    assert res.outcome == "created"


# ── ingest_api ──────────────────────────────────────────────────────────────

def test_heartbeat_publishes_the_agent_and_updates_the_live_cache(hub, monkeypatch):
    import hub_control
    import ingest_api
    hub.gc1()
    monkeypatch.setenv("GC_DATA_DIR", str(hub.data))
    hub_control.reset()
    try:
        assert hub_control.refresh_cache()
        w = Watch()
        ingest_api.record_heartbeat("gc1", {"version": "9.9", "host": "PC", "state": "sending"},
                                    db=hub.db)
        assert "agent" in w.kinds()
        a = [x for x in hub_control.live_agents() if x["instrument_id"] == "gc1"][0]
        assert a["version"] == "9.9" and a["status"] == "sending" and a["last_seen"]
    finally:
        hub_control.reset()


def test_agent_command_and_tokens_publish(hub):
    import ingest_api
    hub.gc1()
    w = Watch()
    ingest_api.set_agent_command("gc1", "restart", db=hub.db)
    assert "agent" in w.kinds()
    w = Watch()
    ingest_api.mint_token("gc1", db=hub.db)
    assert w.instruments() == ["gc1"]
    w = Watch()
    ingest_api.revoke_token("gc1", db=hub.db)
    assert w.instruments() == ["gc1"]


# ── instrument_admin ────────────────────────────────────────────────────────

def test_instrument_admin_changes_publish_the_instrument(hub):
    import instrument_admin as ia
    hub.gc1()
    w = Watch()
    ia.create({"id": "gc3", "name": "GC-3"}, db=hub.db)
    assert w.instruments() == ["gc3"]
    w = Watch()
    ia.update("gc3", {"name": "GC three"}, db=hub.db)
    assert w.instruments() == ["gc3"]
    w = Watch()
    ia.save_corrections("gc3", ELEVEN, "initial", by="t", db=hub.db)
    assert w.instruments() == ["gc3"]
    w = Watch()
    ia.set_method_mapping("gc3", "OTHER.M", "D2887", db=hub.db)
    assert w.instruments() == ["gc3"]


def test_seed_calibration_and_classification_publish(hub):
    import instrument_admin as ia
    hub.gc1()
    w = Watch()
    ia.seed_gc1(hub.conf, by="t", db=hub.db)
    assert w.instruments() == ["gc1"]
    sid = hub.submit(hub.cdf(), instrument="gc1").sample_id
    w = Watch()
    ia.set_calibration_cdf("gc1", hub.conf, sample_id=sid, db=hub.db, data_dir=hub.data)
    assert "gc1" in w.instruments()
    w = Watch()
    try:
        ia.save_calibration("gc1", [], None, hub.conf, db=hub.db, data_dir=hub.data)
    except ia.AdminError:
        pass
    assert "gc1" in w.instruments()
    store.samples.set_status(sid, "review_method", db=hub.db)
    w = Watch()
    assert ia.mark_review_other("gc1", db=hub.db) == 1
    assert "gc1" in w.instruments() and sid in w.samples()


def test_keep_existing_publishes(hub):
    import instrument_admin as ia
    from datetime import datetime
    hub.gc1()
    t = datetime(2026, 9, 25, 14, 23, 0)
    hub.submit(hub.cdf(injected=t))
    cid = hub.submit(hub.cdf(injected=t, shift=0.05)).conflict_id
    w = Watch()
    ia.keep_existing(cid, by="t", db=hub.db)
    assert "gc1" in w.instruments()


class FakeExporter:
    def __init__(self, db):
        self.db = db

    def new_path(self, instrument, path):
        pass

    def adopt(self, instrument, by=None):
        return {}

    def status(self, instrument):
        return {}


def test_export_path_and_adopt_publish(hub, tmp_path):
    import instrument_admin as ia
    hub.gc1()
    exp = FakeExporter(hub.db)
    w = Watch()
    ia.set_export_path("gc1", str(tmp_path / "x.csv"), exp)
    assert w.instruments() == ["gc1"]
    w = Watch()
    ia.adopt_export("gc1", exp, by="t")
    assert w.instruments() == ["gc1"]


# ── notifications ───────────────────────────────────────────────────────────

def test_the_notification_store_publishes_and_counts(tmp_path):
    import notifications
    ns = notifications.NotificationStore(tmp_path / "n.json")
    w = Watch()
    e = ns.add("info", "hello")
    assert "notification" in w.kinds()
    assert ns.count() == 1
    w = Watch()
    ns.dismiss(e["id"])
    assert "notification" in w.kinds() and ns.count() == 0
    ns.add("info", "a")
    w = Watch()
    ns.dismiss_all()
    assert "notification" in w.kinds()
    w = Watch()
    ns.dismiss("nope")                       # nothing changed: no event
    assert w.kinds() == []


# ── the exporter ────────────────────────────────────────────────────────────

def test_the_exporter_publishes_each_appended_sample(tmp_path):
    import exports
    from exports_testlib import add_final, make_db
    db = make_db(tmp_path)
    add_final(db)
    add_final(db)
    sids = [r["sample_id"] for r in store.export_rows.rows_after("gc1", 0, db=db)]
    exp = exports.HubExporter(db=db, data_dir=tmp_path / "data", notifier=lambda *a: None,
                              retry_sleep=lambda s: None)
    w = Watch()
    res = exp.flush("gc1")
    assert res.appended == 2 and len(sids) == 2
    assert w.samples() == sorted(sids)
    assert "gc1" in w.instruments()


# ── app.py: the QBench upload record (AST; app is never imported) ───────────

def _func(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def _calls(fn) -> set:
    return {f"{c.func.value.id}.{c.func.attr}" for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and isinstance(c.func.value, ast.Name)}


def test_app_publishes_the_qbench_upload_record():
    tree = ast.parse((TESTS.parent / "app.py").read_text(encoding="utf-8"))
    assert "live.publish" in _calls(_func(tree, "_record_qbench_upload"))
