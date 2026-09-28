"""methods/ registry and instruments.py (2A1 T2)."""
from __future__ import annotations

from pipeline_helpers import hub  # noqa: F401  (the fixture)

import json
import os
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

import distill
import instruments
import methods
import store
from methods import d2887


# ── methods ─────────────────────────────────────────────────────────────────

def test_registry_has_d2887():
    assert methods.get("D2887") is d2887
    assert methods.get(" d2887 ") is d2887
    assert "D2887" in methods.names()


def test_unknown_method_raises():
    with pytest.raises(methods.UnknownMethod):
        methods.get("D7096")
    assert issubclass(methods.UnknownMethod, KeyError)


def test_normalise_method_name():
    assert methods.normalise_method_name(" c:\\CHEM32\\1\\Methods\\simdisb.m ") == "SIMDISB.M"
    assert methods.normalise_method_name(None) == ""


def test_d2887_compute_is_distill_compute_in_hub_mode(tmp_path):
    with mock.patch.object(distill, "compute", return_value={"row": {}}) as compute:
        out = d2887.compute(tmp_path / "x.CDF", {"k": "v"}, blank_path=tmp_path / "b.CDF",
                            corrections={"IBP": -1.0})
    assert out == {"row": {}}
    compute.assert_called_once_with(tmp_path / "x.CDF", {"k": "v"}, tmp_path / "b.CDF",
                                    corrections={"IBP": -1.0}, honour_env=False,
                                    allow_auto=False)


def test_d2887_compute_requires_explicit_corrections(tmp_path):
    with pytest.raises(TypeError):
        d2887.compute(tmp_path / "x.CDF", {}, blank_path=None)
    with pytest.raises(ValueError):
        d2887.compute(tmp_path / "x.CDF", {}, blank_path=None, corrections=None)


def test_names_mapped_to():
    mm = {"SIMDISB.M": "D2887", "SIMDISTB.M": "D2887", "GASOLINE.M": "D7096"}
    assert methods.names_mapped_to(mm, "D2887") == ["SIMDISB.M", "SIMDISTB.M"]
    assert methods.names_mapped_to(mm, "d7096") == ["GASOLINE.M"]


# ── instruments.context / calibration_usable ────────────────────────────────

def _row(hub, **kw):
    entries = make_entries()
    row = {"id": "gc1", "name": "GC-1", "method": "D2887",
           "calibration_cdf": str(hub.cal), "calibration_assignments": json.dumps(entries),
           "calibration_sensitivity": 42.0,
           "method_map": json.dumps(instruments.DEFAULT_METHOD_MAP)}
    row.update(kw)
    return row


def make_entries():
    import make_golden
    return make_golden.calibration_entries()


def test_context_overlays_the_instrument_calibration(hub):
    glob = dict(hub.conf, calibration_cdf="/elsewhere/other.CDF", calibration_assignments="",
                calibration_sensitivity="50")
    ctx = instruments.context(_row(hub), glob)
    assert ctx["calibration_cdf"] == str(hub.cal)
    assert ctx["calibration_sensitivity"] == "42.0"
    assert ctx["instrument_id"] == "gc1"
    # distill's settings shape: {resolved cal path: entries}
    assert distill.parse_assignment_map(ctx["calibration_assignments"]) == {
        distill._cal_key(hub.cal): make_entries()}
    assert ctx["correction_factors_json"] == glob["correction_factors_json"]
    assert glob["calibration_cdf"] == "/elsewhere/other.CDF"   # not mutated
    # the numbers use exactly these anchors
    assert distill.calibration_anchors(hub.cal, ctx, allow_auto=False)["source"] == "assignments"


def test_context_resolves_a_relative_calibration_cdf_against_the_data_dir(hub):
    ctx = instruments.context(_row(hub, calibration_cdf="cdf/gc1/2026/09/CAL_1.CDF"), hub.conf,
                              data_dir=hub.data)
    assert ctx["calibration_cdf"] == str(hub.data / "cdf" / "gc1" / "2026" / "09" / "CAL_1.CDF")


def test_context_ignores_gc_cal_cdf(hub, monkeypatch):
    monkeypatch.setenv("GC_CAL_CDF", "/nope/env.CDF")
    ctx = instruments.context(_row(hub), hub.conf)
    assert ctx["calibration_cdf"] == str(hub.cal)
    assert instruments.calibration_usable(ctx)


def test_context_without_calibration(hub):
    ctx = instruments.context(_row(hub, calibration_cdf=None, calibration_assignments=None), hub.conf)
    assert ctx["calibration_cdf"] == ""
    assert ctx["calibration_assignments"] == ""
    assert not instruments.calibration_usable(ctx)
    assert "no calibration" in instruments.calibration_problem(ctx).lower()


def test_calibration_usable_needs_the_file_and_two_pairs(hub, tmp_path):
    assert instruments.calibration_usable(instruments.context(_row(hub), hub.conf))
    missing = instruments.context(_row(hub, calibration_cdf=str(tmp_path / "gone.CDF")), hub.conf)
    assert not instruments.calibration_usable(missing)
    assert "not found" in instruments.calibration_problem(missing)
    one = [{"rt": 0.5, "carbon": 5}, {"rt": 0.8, "ignore": True}]
    few = instruments.context(_row(hub, calibration_assignments=json.dumps(one)), hub.conf)
    assert not instruments.calibration_usable(few)
    assert "1 usable" in instruments.calibration_problem(few)
    none = instruments.context(_row(hub, calibration_assignments="[]"), hub.conf)
    assert not instruments.calibration_usable(none)
    assert instruments.calibration_problem(instruments.context(_row(hub), hub.conf)) is None


def test_method_map_is_decoded_and_normalised():
    assert instruments.method_map({"method_map": '{" simdisb.m ": "d2887"}'}) == {"SIMDISB.M": "D2887"}
    assert instruments.method_map({"method_map": None}) == {}
    assert instruments.method_map({"method_map": "not json"}) == {}


# ── gc1 bootstrap from settings.json ────────────────────────────────────────

def test_bootstrap_gc1_copies_the_calibration_and_sets_live_since(hub):
    now = datetime(2026, 10, 1, 7, 30, 0)
    row = instruments.bootstrap_gc1(hub.conf, db=hub.db, now=now)
    assert row["id"] == "gc1"
    assert row["name"] == "GC-1"
    assert row["method"] == "D2887"
    assert row["calibration_cdf"] == str(hub.cal)
    assert json.loads(row["calibration_assignments"]) == make_entries()
    assert row["calibration_sensitivity"] == 50.0
    assert json.loads(row["method_map"]) == {"SIMDISB.M": "D2887", "SIMDISTB.M": "D2887"}
    assert row["live_since"] == "2026-10-01 07:30:00"
    assert not store.is_backfill(row["live_since"], "2026-10-01 07:30:00")
    assert store.is_backfill(row["live_since"], "2026-10-01 07:29:59")
    # the context built from the row gives the same calibration as settings.json did
    ctx = instruments.context(row, hub.conf)
    assert instruments.calibration_usable(ctx)
    assert (distill.calibration_anchors(hub.cal, ctx, allow_auto=False)
            == distill.calibration_anchors(hub.cal, hub.conf, allow_auto=False))


def test_bootstrap_gc1_is_once_only(hub):
    first = instruments.bootstrap_gc1(hub.conf, db=hub.db, now=datetime(2026, 10, 1))
    store.instruments.upsert({"id": "gc1", "calibration_sensitivity": 60}, db=hub.db)
    again = instruments.bootstrap_gc1(dict(hub.conf, calibration_cdf=""), db=hub.db,
                                      now=datetime(2027, 1, 1))
    assert again["live_since"] == first["live_since"]
    assert again["calibration_cdf"] == str(hub.cal)
    assert again["calibration_sensitivity"] == 60.0


def test_bootstrap_gc1_without_a_calibration(hub):
    row = instruments.bootstrap_gc1({"calibration_cdf": "", "calibration_assignments": ""},
                                    db=hub.db, now=datetime(2026, 10, 1))
    assert row["calibration_cdf"] is None
    assert row["calibration_assignments"] is None
    assert row["live_since"] == "2026-10-01 00:00:00"


def test_bootstrap_gc1_finds_assignments_saved_under_an_unresolved_key(hub):
    conf = dict(hub.conf, calibration_assignments=json.dumps({str(hub.cal): make_entries()}))
    row = instruments.bootstrap_gc1(conf, db=hub.db, now=datetime(2026, 10, 1))
    assert json.loads(row["calibration_assignments"]) == make_entries()
