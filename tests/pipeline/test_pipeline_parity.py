"""Parity (2A1 T2): the pipeline's results equal 2A0's golden rows (v1.0.0's
``process_cdf`` output) for the same conf and corrections. The numbers may
change only through the injection-time fix, which these fixtures don't hit.
"""
from __future__ import annotations

from pipeline_helpers import SIMDIS, hub  # noqa: F401  (hub: the fixture)

import csv
import io
import json
import shutil
from pathlib import Path

import pytest

import corrections
import distill
import exports
import make_golden
import pipeline
import store

GOLDEN = json.loads(make_golden.GOLDEN_JSON.read_text(encoding="utf-8"))
BESTFIT = json.loads(make_golden.BESTFIT_JSON.read_text(encoding="utf-8"))
SNAPSHOT = json.loads(make_golden.SNAPSHOT_JSON.read_text(encoding="utf-8"))


class NoCorrections:
    """What v1 did with ``correction_factors_json`` = '' (every cut 0.0)."""

    def get(self, instrument):
        return corrections.Corrections(source="hub", updated_at="2026-09-28T00:00:00+00:00",
                                       values={c: 0.0 for c in corrections.D86_CUTS})


def _with_method(src: Path, dest: Path) -> Path:
    """A copy of a fixture/snapshot CDF carrying a D2887 method name (numbers untouched)."""
    from netCDF4 import Dataset
    shutil.copyfile(src, dest)
    with Dataset(dest, "a") as ds:
        if "detection_method_name" not in ds.ncattrs():
            ds.detection_method_name = SIMDIS
    return dest


def _row_strings(line: str) -> dict:
    values = next(csv.reader(io.StringIO(line)))
    row = dict(zip(distill.CSV_HEADER, values))
    row.pop("Source File")
    return row


def _run_case(hub, name, *, provider=None):
    inputs = make_golden.build_inputs(hub.root / f"golden-{name}")
    conf = make_golden.case_conf(hub.root / f"golden-{name}", inputs, name)
    conf["correction_factors_json"] = str(hub.corrections)      # same golden values
    hub.conf.clear()
    hub.conf.update(conf)
    row = hub.gc1()
    store.instruments.upsert({"id": "gc1", "calibration_cdf": conf["calibration_cdf"],
                              "calibration_assignments": _entries(conf)}, db=hub.db)
    sample_src, blank_src = make_golden.case_paths(inputs, name)
    if blank_src is not None:
        res = hub.submit(_with_method(blank_src, hub.src / f"{name}-blank.CDF"))
        assert hub.sample(res.sample_id)["is_blank"] == 1
    sid = hub.submit(_with_method(sample_src, hub.src / f"{name}-sample.CDF")).sample_id
    hub.worker(corrections_provider=provider).run_until_idle()
    s = hub.sample(sid)
    assert s["status"] == "final", s["error"]
    (export,) = [r for r in store.export_rows.rows_after("gc1", 0, db=hub.db) if r["sample_id"] == sid]
    rev = store.get_revision(sid, db=hub.db)
    return s, rev, export


def _entries(conf):
    amap = distill.parse_assignment_map(conf["calibration_assignments"])
    (entries,) = amap.values() if amap else ([],)
    return json.dumps(entries) if entries else None


@pytest.mark.parametrize("name", ["sample_40304_blank", "sample_40304_noblank", "sample_40305_blank"])
def test_results_equal_the_golden_rows(hub, name):
    s, rev, export = _run_case(hub, name)
    assert _row_strings(export["line"]) == GOLDEN[name]
    results = json.loads(rev["results"])
    assert _row_strings(exports.format_line(results, "")) == GOLDEN[name]
    assert results["Source File"] == s["cdf_path"]
    assert (rev["blank_used"] is not None) == (make_golden.CASES[name][1] is not None)


def test_no_corrections_equals_the_golden_row(hub):
    _s, _rev, export = _run_case(hub, "no_corrections", provider=NoCorrections())
    assert _row_strings(export["line"]) == GOLDEN["no_corrections"]


@pytest.mark.parametrize("name", sorted(make_golden.BESTFIT_CASES))
def test_bestfit_rows_equal_the_golden_rows(hub, name):
    _s, _rev, export = _run_case(hub, name)
    assert _row_strings(export["line"]) == BESTFIT[name]


@pytest.mark.skipif(not make_golden.snapshot_available(),
                    reason=f"no share snapshot at {make_golden.SNAPSHOT_DIR}")
def test_snapshot_blank_row_is_computed_with_assignments_only(hub):
    """The snapshot golden rows were auto-detected, which the hub refuses
    (allow_auto=False): the hub holds them awaiting calibration instead."""
    inputs = make_golden.build_inputs(hub.root / "snap")
    conf = make_golden.case_conf(hub.root / "snap", inputs, "snapshot_std_blank")
    hub.conf.update(conf)
    hub.gc1()
    store.instruments.upsert({"id": "gc1", "calibration_cdf": conf["calibration_cdf"],
                              "calibration_assignments": None}, db=hub.db)
    sid = hub.submit(_with_method(inputs["samples"]["snapshot_std"], hub.src / "std.CDF")).sample_id
    hub.worker().run_until_idle()
    assert hub.sample(sid)["status"] == "awaiting_calibration"
    assert SNAPSHOT  # the pinned rows exist; 2A0's golden test covers their numbers
