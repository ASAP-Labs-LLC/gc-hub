"""Seams found by the 2A1 integration critic, owned by the pipeline:
truncated CDFs, identity parity with the history importer, the one-txn
admin actions (Export to LIMS, backfill release, conflict replace), shared
definitions, and the stored file's mtime."""
from __future__ import annotations

from pipeline_helpers import SIMDIS, hub  # noqa: F401  (hub: the fixture)

import json
import os
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

import cdf_fixtures as fx
import distill
import exports
import import_match
import pipeline
import store


def _run(hub, **kw) -> int:
    return hub.worker(**kw).run_until_idle()


def _rows(hub, instrument="gc1"):
    return store.export_rows.rows_after(instrument, 0, db=hub.db)


# ── C1: truncated CDFs are refused ──────────────────────────────────────────

@pytest.mark.parametrize("keep", [0.95, 0.5, 0.2])
def test_a_truncated_sample_is_refused(hub, keep):
    hub.gc1()
    whole = hub.cdf()
    cut = fx.truncated_copy(whole, hub.src / f"cut{keep}.CDF", keep)
    with pytest.raises(pipeline.SubmitRejected) as got:
        hub.submit(cut)
    assert "truncated" in str(got.value) or "not a readable CDF" in str(got.value)
    assert store.samples.count(db=hub.db) == 0
    assert not any((hub.data / "cdf").rglob("*.CDF"))


def test_a_truncated_blank_is_refused(hub):
    hub.gc1()
    blank = hub.cdf("blank")
    cut = fx.truncated_copy(blank, hub.src / "cutblank.CDF", 0.95)
    assert distill.is_plausible_blank(cut)          # netCDF4 zero-fills: it would pass
    with pytest.raises(pipeline.SubmitRejected) as got:
        hub.submit(cut)
    assert "truncated" in str(got.value)


def test_a_cdf_without_intensity_data_is_refused(hub, tmp_path):
    from netCDF4 import Dataset
    hub.gc1()
    p = tmp_path / "empty.CDF"
    with Dataset(p, "w", format="NETCDF3_CLASSIC") as ds:
        ds.sample_name = "X1"
        ds.injection_date_time_stamp = "20260925142300+0000"
        ds.detection_method_name = SIMDIS
        ds.createDimension("point_number", 0)
        ds.createVariable("ordinate_values", "f8", ("point_number",))
    with pytest.raises(pipeline.SubmitRejected) as got:
        hub.submit(p)
    assert "intensity" in str(got.value)
    q = tmp_path / "novar.CDF"
    with Dataset(q, "w", format="NETCDF3_CLASSIC") as ds:
        ds.sample_name = "X2"
        ds.injection_date_time_stamp = "20260925142300+0000"
        ds.createDimension("point_number", 3)
        ds.createVariable("something_else", "f8", ("point_number",))[:] = [1, 2, 3]
    with pytest.raises(pipeline.SubmitRejected):
        hub.submit(q)


def test_a_whole_file_passes_the_check(hub):
    assert pipeline.cdf_problem(hub.cdf()) is None
    assert pipeline.cdf_problem(hub.cal) is None


# ── C2: the pipeline and the history importer agree on identity ─────────────

MTIME = 1_790_000_000.123456      # a file time with microseconds, as v1 read it

CASES = {
    # name: (sample_name, raw_stamp)
    "stamped": ("40304", "20260925142300+0000"),
    "misparsed": ("40305", "20260925002450+0000"),
    "z_stamp": ("40306", "20260925002450Z"),
    "no_stamp": ("40307", ""),
    "slash_date": ("40308", "2026/09/25 00:24:50"),
    "whitespace_name": ("   ", "20260925142300+0000"),
    "empty_name": ("", "20260925142300+0000"),
    "no_name": (None, "20260925142300+0000"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_stored_identity_matches_the_importers(hub, case):
    hub.gc1()
    name, stamp = CASES[case]
    t = fx._axis()
    y = fx.gaussian(t, 0.30, 50000, 0.01) + fx.gaussian(t, 3.2, 1200, 0.9)
    path = fx.write_cdf(hub.src / f"{case}.CDF", t, y, name, datetime(2026, 9, 25),
                        method_name=SIMDIS, raw_stamp=stamp)
    os.utime(path, (MTIME, MTIME))
    meta = import_match.read_cdf_meta(path)
    s = hub.sample(hub.submit(path).sample_id)
    assert s["lab_id"] == import_match.normalise_lab_id(meta.lab_id)
    assert s["legacy_injection_dt"] == meta.legacy_injection_dt
    assert s["method_name"] == meta.method_name
    assert s["injection_dt_source"] == meta.dt_source
    if meta.dt_source == "mtime":
        # documented: the hub keeps whole seconds; the importer (and v1) the microseconds
        assert s["injection_dt"] == meta.injection_dt[:19]
        assert meta.injection_dt.endswith(".123456")
        assert s["legacy_injection_dt"].endswith(".123456")
    else:
        assert s["injection_dt"] == meta.injection_dt
    if case in ("whitespace_name", "empty_name", "no_name"):
        assert s["lab_id"] == case


def test_legacy_mtime_string_is_v1s_exact_string(hub):
    hub.gc1()
    t = fx._axis()
    path = fx.write_cdf(hub.src / "ns.CDF", t, fx.gaussian(t, 3.2, 1200, 0.9), "40309",
                        datetime(2026, 9, 25), method_name=SIMDIS, raw_stamp="")
    os.utime(path, (MTIME, MTIME))
    s = hub.sample(hub.submit(path).sample_id)
    assert s["legacy_injection_dt"] == datetime.fromtimestamp(os.stat(path).st_mtime).isoformat(sep=" ")
    assert s["injection_dt"] == datetime.fromtimestamp(int(MTIME)).isoformat(sep=" ")


def test_one_cdf_name_reader():
    assert distill.cdf_lab_name("  40304 ", "x") == "40304"
    assert distill.cdf_lab_name("   ", "40777") == "40777"
    assert distill.cdf_lab_name("", "") == "Unknown"


# ── I3: admin actions in one transaction ────────────────────────────────────

def _final(hub, **kw):
    sid = hub.submit(hub.cdf(**kw)).sample_id
    _run(hub)
    assert hub.sample(sid)["status"] == "final"
    return sid


def test_export_to_lims_copies_the_current_revision(hub):
    hub.gc1()
    sid = _final(hub)
    first = store.get_revision(sid, db=hub.db)
    with mock.patch.object(distill, "compute", side_effect=AssertionError("no recompute")):
        out = pipeline.export_to_lims(sid, by="ryan", db=hub.db, data_dir=hub.data)
    rev = store.get_revision(sid, db=hub.db)
    assert (out["revision"], rev["revision"], rev["reason"], rev["by"]) == (2, 2, "export-lims", "ryan")
    for col in ("results", "d86_uncorrected", "calibration_used", "blank_used", "corrections_used",
                "best_fit", "fit_score", "flags", "notes"):
        assert rev[col] == first[col], col
    rows = _rows(hub)
    assert [(r["revision"], r["seq"]) for r in rows][-1] == (2, out["seq"])
    assert rows[-1]["line"] == rows[0]["line"]
    assert hub.sample(sid)["current_revision"] == 2


def test_export_to_lims_enforces_the_gate(hub):
    hub.gc1(live_since=datetime(2026, 10, 1))
    sid = _final(hub)                                  # backfill, unreleased
    with pytest.raises(pipeline.NotExportable):
        pipeline.export_to_lims(sid, by="ryan", db=hub.db, data_dir=hub.data)
    assert store.list_revisions(sid, db=hub.db)[-1]["revision"] == 1
    assert _rows(hub) == []
    other = hub.submit(hub.cdf(name="40999", method_name="GASOLINE.M")).sample_id
    _run(hub)
    with pytest.raises(pipeline.NotExportable):
        pipeline.export_to_lims(other, by="ryan", db=hub.db, data_dir=hub.data)


def test_release_backfill_writes_the_export_row(hub):
    hub.gc1(live_since=datetime(2026, 10, 1))
    sid = _final(hub)
    assert _rows(hub) == []
    seq = pipeline.release_backfill(sid, by="ryan", db=hub.db, data_dir=hub.data)
    s = hub.sample(sid)
    assert s["released_by"] == "ryan" and s["released_at"]
    (row,) = _rows(hub)
    assert (row["seq"], row["sample_id"], row["revision"]) == (seq, sid, 1)
    assert row["line"] == exports.format_line(store.get_revision(sid, db=hub.db)["results"],
                                              s["cdf_path"])
    assert store.samples.is_gated(sid, db=hub.db)
    with pytest.raises(pipeline.NotExportable):                # once only
        pipeline.release_backfill(sid, by="ryan", db=hub.db, data_dir=hub.data)
    assert len(_rows(hub)) == 1


def test_release_backfill_refuses_live_and_unfinished_samples(hub):
    hub.gc1()
    live = _final(hub)
    with pytest.raises(pipeline.NotExportable):
        pipeline.release_backfill(live, by="ryan", db=hub.db, data_dir=hub.data)
    store.instruments.upsert({"id": "gc1", "live_since": datetime(2027, 1, 1)}, db=hub.db)
    hub.corrections.unlink()
    pend = hub.submit(hub.cdf(name="40998", shift=0.1)).sample_id
    _run(hub)
    assert hub.sample(pend)["status"] == "pending_corrections"
    with pytest.raises(pipeline.NotExportable):
        pipeline.release_backfill(pend, by="ryan", db=hub.db, data_dir=hub.data)
    assert hub.sample(pend)["released_at"] is None


def test_resolve_conflict_replace(hub):
    hub.gc1()
    sid = _final(hub, name="40304")
    old = hub.sample(sid)
    b = hub.cdf(name="40304", shift=0.3)
    res = hub.submit(b)
    assert res.outcome == "conflict"
    job = pipeline.resolve_conflict_replace(res.conflict_id, by="ryan", conf=hub.conf,
                                            db=hub.db, data_dir=hub.data)
    assert job
    # only a job is queued: the conflict and the sample wait for the recompute
    assert store.conflicts.get(res.conflict_id, db=hub.db)["resolved"] is None
    assert hub.sample(sid) == old
    _run(hub)
    c = store.conflicts.get(res.conflict_id, db=hub.db)
    assert (c["resolved"], c["resolved_by"], c["error"]) == ("replaced", "ryan", None)
    s = hub.sample(sid)
    assert s["cdf_sha256"] == res.sha256
    assert s["cdf_path"] == c["cdf_path"]
    assert (hub.data / s["cdf_path"]).read_bytes() == b.read_bytes()
    assert (hub.data / old["cdf_path"]).is_file()      # raw CDFs are kept forever (D7)
    rev = store.get_revision(sid, db=hub.db)
    assert (rev["revision"], rev["reason"], rev["by"]) == (2, "replace", "ryan")
    assert rev["results"] != store.get_revision(sid, 1, db=hub.db)["results"]
    assert [r["revision"] for r in _rows(hub)] == [1, 2]
    with pytest.raises(ValueError):
        pipeline.resolve_conflict_replace(res.conflict_id, by="ryan", conf=hub.conf,
                                          db=hub.db, data_dir=hub.data)


def test_a_first_revision_after_pending_corrections_is_corrections_released(hub):
    hub.gc1()
    good = hub.corrections.read_text()
    hub.corrections.unlink()
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub)
    assert hub.sample(sid)["status"] == "pending_corrections"
    hub.corrections.write_text(good)
    store.jobs.enqueue_for_status("gc1", "pending_corrections", db=hub.db)
    _run(hub)
    assert store.get_revision(sid, db=hub.db)["reason"] == "corrections-released"
    assert "corrections-released" in store.REVISION_REASONS


# ── M1: one definition each ─────────────────────────────────────────────────

def test_shared_definitions():
    assert import_match._correct_parse is distill.parse_injection_datetime
    assert import_match._method_basename is distill.normalise_method_name
    assert pipeline.normalise_lab_id is import_match.normalise_lab_id
    assert not hasattr(pipeline, "default_format_line")


def test_the_worker_writes_the_exports_line(hub):
    assert hub.worker().format_line is exports.format_line


def test_the_final_write_uses_the_stores_gate(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    with mock.patch.object(store.samples, "is_gated", return_value=False) as gated:
        _run(hub)
    assert gated.called
    assert _rows(hub) == []


# ── M2: the stored file gets the sender's mtime after the move ──────────────

def test_the_final_file_gets_the_mtime_not_the_temp(hub):
    hub.gc1()
    seen = []
    real = os.utime

    def spy(path, *a, **kw):
        seen.append(Path(path))
        return real(path, *a, **kw)

    src = hub.cdf()
    os.utime(src, (1_700_000_000, 1_700_000_000))
    with mock.patch.object(pipeline.os, "utime", spy):
        sid = hub.submit(src).sample_id
    stored = hub.data / hub.sample(sid)["cdf_path"]
    assert seen == [stored]
    assert stored.stat().st_mtime == 1_700_000_000
