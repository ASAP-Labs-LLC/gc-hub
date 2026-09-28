"""2A2 T1: the Worker's corrections provider is hub-owned (D4b/2C).

``instruments.corrections_provider(db)`` is the Worker's ``(conf) ->
provider`` factory: the store's corrections (``StoreProvider``, reading the
given db) for an instrument that has them; for ``gc1`` without any, the
phase-1 file (the interim source until it is seeded); anything else is
``CorrectionsUnavailable``."""
from __future__ import annotations

from a2_helpers import ELEVEN, cal_entries, hub, set_corrections  # noqa: F401

import json

import pytest

import corrections
import instruments
import pipeline
import store


def test_gc1_falls_back_to_the_file_until_seeded(hub):
    gc1 = hub.gc1()
    got = instruments.corrections_provider(hub.db)(hub.conf).get(gc1)
    assert got.source == "file"
    assert got.values == corrections.seed_from_file(str(hub.corrections))


def test_store_rows_win_for_gc1(hub):
    gc1 = hub.gc1()
    set_corrections(hub, "gc1")
    got = instruments.corrections_provider(hub.db)(hub.conf).get(gc1)
    assert got.source == "hub"
    assert got.values == ELEVEN
    assert got.updated_by == "t"


def test_other_instruments_need_hub_corrections(hub):
    hub.gc1()
    gc2 = hub.gc2()
    factory = instruments.corrections_provider(hub.db)
    with pytest.raises(corrections.CorrectionsUnavailable, match="GC-2"):
        factory(hub.conf).get(gc2)
    set_corrections(hub, "gc2")
    assert factory(hub.conf).get(gc2).source == "hub"


def test_gc1_file_missing_is_unavailable(hub):
    gc1 = hub.gc1()
    conf = dict(hub.conf, correction_factors_json=str(hub.root / "nope.json"))
    with pytest.raises(corrections.CorrectionsUnavailable):
        instruments.corrections_provider(hub.db)(conf).get(gc1)


def test_reads_the_given_db_not_the_default(hub, monkeypatch):
    monkeypatch.delenv("GC_DATA_DIR", raising=False)
    hub.gc1()
    gc2 = hub.gc2()
    set_corrections(hub, "gc2")
    assert instruments.corrections_provider(hub.db)(hub.conf).get(gc2).source == "hub"


def test_startup_wires_the_provider(hub):
    hub.gc1()
    hub.gc2(calibration_cdf=str(hub.cal), calibration_assignments=json.dumps(cal_entries(hub)))
    sid = hub.submit(hub.cdf(), instrument="gc2").sample_id
    worker = instruments.startup(hub.conf, db=hub.db, data_dir=hub.data,
                                 conf_fn=lambda: hub.conf, poll_seconds=3600)
    try:
        worker.stop()
        assert worker.corrections_provider is not None
        worker.run_until_idle()
        assert store.samples.get(sid, db=hub.db)["status"] == "pending_corrections"
        set_corrections(hub, "gc2")
        store.jobs.enqueue_for_status("gc2", "pending_corrections", db=hub.db)
        worker.run_until_idle()
        s = store.samples.get(sid, db=hub.db)
        assert s["status"] == "final", s
        used = json.loads(store.get_revision(sid, db=hub.db)["corrections_used"])
        assert used["source"] == "hub" and used["values"] == ELEVEN
    finally:
        worker.stop()


def test_startup_keeps_an_explicit_provider(hub):
    hub.gc1()
    sentinel = object()
    worker = instruments.startup(hub.conf, db=hub.db, data_dir=hub.data,
                                 corrections_provider=sentinel, poll_seconds=3600)
    try:
        assert worker.corrections_provider is sentinel
    finally:
        worker.stop()
