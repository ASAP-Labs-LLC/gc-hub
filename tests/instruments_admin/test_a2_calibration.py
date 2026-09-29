"""2A2 T4: per-instrument calibration: the CDF picked from the instrument's
received samples (or an absolute path), its sensitivity and assignments,
"Calibration usable", and queueing only that instrument's
awaiting_calibration samples."""
from __future__ import annotations

from a2_helpers import cal_entries, hub  # noqa: F401

import json
from datetime import datetime

import pytest

pytest.importorskip("flask")

import instrument_admin as ia  # noqa: E402
import store  # noqa: E402


def _queued_ids(hub):
    return sorted(j["sample_id"] for j in store.jobs.list(state="queued", kind="process", db=hub.db))


def _uncalibrated(hub):
    """gc2 and gc3 with no calibration; one sample each, left awaiting_calibration."""
    hub.gc1()
    hub.gc2()
    store.instruments.upsert({"id": "gc3", "name": "GC-3", "method_map": json.dumps(
        {"SIMDISB.M": "D2887"}), "live_since": datetime(2020, 1, 1)}, db=hub.db)
    s2 = hub.submit(hub.cdf(name="S2"), instrument="gc2").sample_id
    s3 = hub.submit(hub.cdf(name="S3"), instrument="gc3").sample_id
    cal2 = hub.submit(hub.cal, instrument="gc2").sample_id
    hub.worker().run_until_idle()
    for sid in (s2, s3, cal2):
        assert store.samples.get(sid, db=hub.db)["status"] == "awaiting_calibration"
    return s2, s3, cal2


def test_status_unusable_without_a_cdf(hub):
    hub.gc1()
    hub.gc2()
    st = ia.calibration_status("gc2", hub.conf, db=hub.db, data_dir=hub.data)
    assert st["usable"] is False and "No calibration CDF" in st["problem"]
    st = ia.calibration_status("gc1", hub.conf, db=hub.db, data_dir=hub.data)
    assert st["usable"] is True and st["problem"] is None
    assert st["assigned"] >= 2 and st["calibration_cdf"] == str(hub.cal)


def test_candidates_are_this_instruments_samples(hub):
    s2, s3, cal2 = _uncalibrated(hub)
    got = ia.calibration_candidates("gc2", db=hub.db)
    assert sorted(c["sample_id"] for c in got) == sorted([s2, cal2])
    assert {"lab_id", "injection_dt", "status", "method_name"} <= set(got[0])
    assert "cdf_path" not in got[0]
    got = ia.calibration_candidates("gc2", q="S2", db=hub.db)
    assert [c["sample_id"] for c in got] == [s2]


def test_pick_from_a_sample_clears_assignments(hub):
    _s2, _s3, cal2 = _uncalibrated(hub)
    store.instruments.upsert({"id": "gc2", "calibration_assignments": json.dumps(cal_entries(hub))},
                             db=hub.db)
    st = ia.set_calibration_cdf("gc2", hub.conf, sample_id=cal2, db=hub.db, data_dir=hub.data)
    row = store.instruments.get("gc2", db=hub.db)
    assert row["calibration_cdf"] == store.samples.get(cal2, db=hub.db)["cdf_path"]
    assert not row["calibration_cdf"].startswith("/")          # data-relative
    assert row["calibration_assignments"] is None
    assert st["usable"] is False and "0 usable" in st["problem"]


def test_pick_by_absolute_path(hub):
    _uncalibrated(hub)
    ia.set_calibration_cdf("gc2", hub.conf, path=str(hub.cal), db=hub.db, data_dir=hub.data)
    assert store.instruments.get("gc2", db=hub.db)["calibration_cdf"] == str(hub.cal)


@pytest.mark.parametrize("kw", [{}, {"path": "rel/x.CDF"}, {"path": "/no/such/file.CDF"},
                                {"path": 5}, {"sample_id": "x"}, {"sample_id": True},
                                {"sample_id": 1, "path": "/x"}])
def test_pick_refusals(hub, kw):
    _uncalibrated(hub)
    with pytest.raises(ia.AdminError) as e:
        ia.set_calibration_cdf("gc2", hub.conf, db=hub.db, data_dir=hub.data, **kw)
    assert e.value.status == 400


@pytest.mark.parametrize("content", [b"not a cdf at all", b""])
def test_pick_refuses_a_file_that_is_not_a_cdf_and_keeps_assignments(hub, content):
    """Review minor: the file is parsed as a CDF before it is accepted."""
    hub.gc1()
    bad = hub.root / "bogus.CDF"
    bad.write_bytes(content)
    before = store.instruments.get("gc1", db=hub.db)
    with pytest.raises(ia.AdminError) as e:
        ia.set_calibration_cdf("gc1", hub.conf, path=str(bad), db=hub.db, data_dir=hub.data)
    assert e.value.status == 400 and "CDF" in e.value.message
    after = store.instruments.get("gc1", db=hub.db)
    assert after["calibration_cdf"] == before["calibration_cdf"]
    assert after["calibration_assignments"] == before["calibration_assignments"]


def test_pick_refuses_a_truncated_cdf(hub):
    import cdf_fixtures as fx
    hub.gc1()
    cut = fx.truncated_copy(hub.cal, hub.root / "cut.CDF")
    with pytest.raises(ia.AdminError):
        ia.set_calibration_cdf("gc1", hub.conf, path=str(cut), db=hub.db, data_dir=hub.data)


def test_pick_another_instruments_sample_is_refused(hub):
    _s2, s3, _cal2 = _uncalibrated(hub)
    with pytest.raises(ia.AdminError) as e:
        ia.set_calibration_cdf("gc2", hub.conf, sample_id=s3, db=hub.db, data_dir=hub.data)
    assert e.value.status == 400 and "GC-3" in e.value.message
    with pytest.raises(ia.AdminError) as e:
        ia.set_calibration_cdf("gc2", hub.conf, sample_id=99999, db=hub.db, data_dir=hub.data)
    assert e.value.status == 404


def test_save_assignments_queues_only_this_instrument(hub):
    s2, s3, cal2 = _uncalibrated(hub)
    ia.set_calibration_cdf("gc2", hub.conf, path=str(hub.cal), db=hub.db, data_dir=hub.data)
    before = _queued_ids(hub)
    out = ia.save_calibration("gc2", cal_entries(hub) + [{"rt": 0.01}], 62, hub.conf,
                              db=hub.db, data_dir=hub.data)
    assert out["usable"] is True and out["anchors"] >= 2 and out["saved"] == len(cal_entries(hub))
    assert out["queued"] == 2
    queued = set(_queued_ids(hub)) - set(before)
    assert queued == {s2, cal2} and s3 not in queued
    row = store.instruments.get("gc2", db=hub.db)
    assert json.loads(row["calibration_assignments"]) == [
        {"rt": float(e["rt"]), **({"ignore": True} if e.get("ignore") else {"carbon": int(e["carbon"])})}
        for e in cal_entries(hub)]
    assert row["calibration_sensitivity"] == 62.0
    hub.worker().run_until_idle()
    assert store.samples.get(s2, db=hub.db)["status"] != "awaiting_calibration"
    assert store.samples.get(s3, db=hub.db)["status"] == "awaiting_calibration"


@pytest.mark.parametrize("assignments", [
    "x", [5], [{"carbon": 12}], [{"rt": "a", "carbon": 12}], [{"rt": 1.0, "carbon": 999}],
    [{"rt": 1.0, "carbon": 14}, {"rt": 2.0, "carbon": 12}],
])
def test_save_refusals(hub, assignments):
    _uncalibrated(hub)
    ia.set_calibration_cdf("gc2", hub.conf, path=str(hub.cal), db=hub.db, data_dir=hub.data)
    with pytest.raises(ia.AdminError) as e:
        ia.save_calibration("gc2", assignments, 50, hub.conf, db=hub.db, data_dir=hub.data)
    assert e.value.status == 400
    assert store.instruments.get("gc2", db=hub.db)["calibration_assignments"] is None


@pytest.mark.parametrize("sens", [-1, 101, "x", float("nan"), True])
def test_save_refuses_bad_sensitivity(hub, sens):
    hub.gc1()
    with pytest.raises(ia.AdminError):
        ia.save_calibration("gc1", cal_entries(hub), sens, hub.conf, db=hub.db, data_dir=hub.data)


def test_save_needs_a_cdf(hub):
    hub.gc1()
    hub.gc2()
    with pytest.raises(ia.AdminError) as e:
        ia.save_calibration("gc2", cal_entries(hub), 50, hub.conf, db=hub.db, data_dir=hub.data)
    assert e.value.status == 409


def test_view_has_peaks_and_saved_assignments(hub):
    hub.gc1()
    view = ia.calibration_view("gc1", hub.conf, db=hub.db, data_dir=hub.data)
    assert view["instrument"] == "gc1" and view["cdf_name"] == hub.cal.name
    assert view["peaks"] and view["compounds"] and view["trace"]["x"]
    assert len(view["assignments"]) == len(cal_entries(hub))
    assert view["usable"] is True
    assert view["sensitivity"] == 50.0
    view = ia.calibration_view("gc1", hub.conf, sensitivity=80, db=hub.db, data_dir=hub.data)
    assert view["sensitivity"] == 80.0


def test_view_without_cdf_is_409(hub):
    hub.gc1()
    hub.gc2()
    with pytest.raises(ia.AdminError) as e:
        ia.calibration_view("gc2", hub.conf, db=hub.db, data_dir=hub.data)
    assert e.value.status == 409
