"""2A2 T6: methods seen per instrument, map/unmap (pipeline.on_method_mapped),
and classifying review_method samples."""
from __future__ import annotations

from a2_helpers import SIMDIS, hub  # noqa: F401

import json

import pytest

pytest.importorskip("flask")

import instrument_admin as ia  # noqa: E402
import store  # noqa: E402


def _mixed(hub):
    hub.gc1()
    hub.gc2()
    ok = hub.submit(hub.cdf(name="A")).sample_id
    gas = hub.submit(hub.cdf(name="G", method_name="C:\\Chem32\\1\\METHODS\\d7096.m ")).sample_id
    none = hub.submit(hub.cdf(name="N", method_name=None)).sample_id
    other_inst = hub.submit(hub.cdf(name="O", method_name="D7096.M"), instrument="gc2").sample_id
    hub.worker().run_until_idle()
    assert store.samples.get(gas, db=hub.db)["status"] == "other_method"
    assert store.samples.get(none, db=hub.db)["status"] == "review_method"
    return ok, gas, none, other_inst


def _due(hub):
    now = store.now_iso()
    return {j["sample_id"] for j in store.jobs.list(state="queued", kind="process", db=hub.db)
            if j["not_before"] is None or j["not_before"] <= now}


def test_methods_seen(hub):
    _mixed(hub)
    v = ia.methods_view("gc1", db=hub.db)
    seen = {r["method_name"]: r for r in v["seen"]}
    assert seen["SIMDISB.M"]["count"] == 1 and seen["SIMDISB.M"]["mapped_to"] == "D2887"
    assert seen["D7096.M"]["mapped_to"] is None and seen["D7096.M"]["count"] == 1
    assert seen[""]["count"] == 1 and seen[""]["mapped_to"] is None
    assert seen["SIMDISTB.M"]["count"] == 0          # mapped but never seen: still listed
    assert v["hub_methods"] == ["D2887"]
    assert v["review_count"] == 1


def test_map_queues_that_names_other_method_samples_only(hub):
    ok, gas, none, other_inst = _mixed(hub)
    out = ia.set_method_mapping("gc1", " d7096.m", "d2887", db=hub.db)
    assert out["queued"] == 1 and out["method_map"]["D7096.M"] == "D2887"
    assert json.loads(store.instruments.get("gc1", db=hub.db)["method_map"])["D7096.M"] == "D2887"
    due = _due(hub)
    assert gas in due and other_inst not in due and none not in due


def test_unmap_keeps_results(hub):
    ok, *_ = _mixed(hub)
    rev = store.samples.get(ok, db=hub.db)["current_revision"]
    assert rev is not None
    out = ia.set_method_mapping("gc1", "SIMDISB.M", None, db=hub.db)
    assert "SIMDISB.M" not in out["method_map"] and out["queued"] == 0
    s = store.samples.get(ok, db=hub.db)
    assert s["status"] == "final" and s["current_revision"] == rev


@pytest.mark.parametrize("name,hub_method", [("", "D2887"), ("   ", "D2887"), (None, "D2887"),
                                             ("X.M", "D7096"), ("X.M", 5), (7, "D2887"),
                                             ("x" * 300, "D2887")])
def test_mapping_refusals(hub, name, hub_method):
    hub.gc1()
    with pytest.raises(ia.AdminError) as e:
        ia.set_method_mapping("gc1", name, hub_method, db=hub.db)
    assert e.value.status == 400


def test_mark_review_as_other_method(hub):
    ok, gas, none, _ = _mixed(hub)
    assert ia.mark_review_other("gc1", db=hub.db) == 1
    assert store.samples.get(none, db=hub.db)["status"] == "other_method"
    assert store.samples.get(ok, db=hub.db)["status"] == "final"
    assert ia.mark_review_other("gc1", db=hub.db) == 0


def test_mark_review_selected_ids_only_this_instruments(hub):
    ok, gas, none, other_inst = _mixed(hub)
    assert ia.mark_review_other("gc1", [ok, other_inst], db=hub.db) == 0
    assert ia.mark_review_other("gc1", [none], db=hub.db) == 1
    with pytest.raises(ia.AdminError):
        ia.mark_review_other("gc1", "x", db=hub.db)
