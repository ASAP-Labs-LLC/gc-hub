"""pipeline.submit (2A1 T2): sha dedupe, cross-instrument, conflicts, storage,
blank decided at receive time, legacy injection time, the process job."""
from __future__ import annotations

from pipeline_helpers import SIMDIS, hub  # noqa: F401  (hub: the fixture)

import hashlib
import os
from datetime import datetime
from pathlib import Path

import pytest

import pipeline
import store


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def test_submit_creates_a_received_sample_and_stores_the_file(hub):
    hub.gc1()
    src = hub.cdf(name="40304", injected=datetime(2026, 9, 25, 14, 23, 0))
    before = (src.read_bytes(), src.stat().st_mtime_ns)
    res = hub.submit(src)
    assert res.outcome == "created"
    assert res.sha256 == _sha(src)
    assert res.status == "received"
    assert res.instrument_id == "gc1"
    s = hub.sample(res.sample_id)
    assert s["instrument_id"] == "gc1"
    assert s["lab_id"] == "40304"
    assert s["injection_dt"] == "2026-09-25 14:23:00"
    assert s["injection_dt_source"] == "cdf"
    assert s["legacy_injection_dt"] == "2026-09-25 14:23:00"
    assert s["time_corrected"] == 0
    assert s["method_name"] == SIMDIS
    assert s["source_name"] == src.name
    assert s["is_blank"] == 0
    assert s["backfill"] == 0
    assert s["status"] == "received"
    assert s["cdf_sha256"] == res.sha256
    assert s["cdf_path"] == f"cdf/gc1/2026/09/40304_{res.sample_id}.CDF"
    stored = hub.data / s["cdf_path"]
    assert stored.read_bytes() == before[0]
    # the source is read-only to the hub
    assert (src.read_bytes(), src.stat().st_mtime_ns) == before
    # nothing left in the incoming area
    assert not any((hub.data / "cdf" / ".incoming").glob("*"))
    # one process job queued for it
    (job,) = store.jobs.list(state="queued", kind="process", db=hub.db)
    assert job["sample_id"] == res.sample_id
    assert job["payload"] == {"sample_id": res.sample_id}


def test_submit_bytes_with_the_senders_mtime(hub):
    hub.gc1()
    src = hub.cdf(injected=datetime(2026, 9, 25, 0, 24, 50))
    res = hub.submit(src.read_bytes(), mtime="2026-09-25T00:30:00", source_name="40304 (2).CDF")
    s = hub.sample(res.sample_id)
    assert s["source_name"] == "40304 (2).CDF"
    # the stamp wins over the mtime; v1's misparse is kept as the legacy string
    assert s["injection_dt"] == "2026-09-25 00:24:50"
    assert s["legacy_injection_dt"] == "2026-09-25 02:45:00"
    assert s["time_corrected"] == 1


def test_submit_without_a_stamp_uses_the_senders_mtime(hub, tmp_path):
    from netCDF4 import Dataset
    hub.gc1()
    p = tmp_path / "nostamp.CDF"
    with Dataset(p, "w", format="NETCDF3_CLASSIC") as ds:
        ds.sample_name = "X1"
        ds.detection_method_name = SIMDIS
        ds.createDimension("point_number", 3)
        ds.createVariable("ordinate_values", "f8", ("point_number",))[:] = [0, 1, 0]
    res = hub.submit(p.read_bytes(), mtime=datetime(2026, 9, 1, 8, 0, 5), source_name="x.CDF")
    s = hub.sample(res.sample_id)
    assert (s["injection_dt"], s["injection_dt_source"]) == ("2026-09-01 08:00:05", "mtime")
    assert s["legacy_injection_dt"] == "2026-09-01 08:00:05"
    # bytes with neither a stamp nor a sender mtime: refused (never the receive time)
    other = p.read_bytes().replace(b"X1", b"X2")
    with pytest.raises(pipeline.SubmitRejected):
        hub.submit(other, source_name="x2.CDF")
    # a path defaults to the file's own mtime
    os.utime(p, (1_790_000_000, 1_790_000_000))
    q = tmp_path / "q.CDF"
    q.write_bytes(p.read_bytes().replace(b"X1", b"X3"))
    os.utime(q, (1_790_000_000, 1_790_000_000))
    s3 = hub.sample(hub.submit(q).sample_id)
    assert s3["injection_dt"] == datetime.fromtimestamp(1_790_000_000).isoformat(sep=" ")


def test_submit_rejects_a_non_cdf(hub):
    hub.gc1()
    with pytest.raises(pipeline.SubmitRejected):
        hub.submit(b"not a netcdf file", mtime=datetime(2026, 9, 1), source_name="x.CDF")
    assert store.samples.count(db=hub.db) == 0
    assert not any((hub.data / "cdf").rglob("*.CDF"))


def test_submit_to_an_unknown_instrument(hub):
    with pytest.raises(pipeline.UnknownInstrument):
        hub.submit(hub.cdf(), instrument="gc9")


def test_duplicate_on_the_same_instrument(hub):
    hub.gc1()
    src = hub.cdf()
    first = hub.submit(src)
    again = hub.submit(src.read_bytes(), mtime=datetime(2026, 9, 25), source_name="copy.CDF")
    assert again.outcome == "duplicate"
    assert again.sample_id == first.sample_id
    assert again.sha256 == first.sha256
    assert again.status == "received"
    assert store.samples.count(db=hub.db) == 1
    assert len(store.jobs.list(kind="process", db=hub.db)) == 1


def test_same_file_on_another_instrument(hub):
    hub.gc1()
    hub.gc2()
    src = hub.cdf()
    first = hub.submit(src)
    other = hub.submit(src, instrument="gc2")
    assert other.outcome == "cross_instrument"
    assert other.sample_id == first.sample_id
    assert other.instrument_id == "gc1"
    assert store.samples.count(db=hub.db) == 1


def test_conflict_on_the_lab_id_and_injection_time(hub):
    hub.gc1()
    a = hub.cdf(name="40304", injected=datetime(2026, 9, 25, 14, 23, 0))
    b = hub.cdf(name="40304", injected=datetime(2026, 9, 25, 14, 23, 0), shift=0.2)
    first = hub.submit(a)
    res = hub.submit(b)
    assert res.outcome == "conflict"
    assert res.sha256 == _sha(b)
    assert res.sample_id == first.sample_id
    (c,) = store.conflicts.list("gc1", db=hub.db)
    assert c["id"] == res.conflict_id
    assert (c["lab_id"], c["injection_dt"], c["existing_sample_id"], c["cdf_sha256"]) == \
        ("40304", "2026-09-25 14:23:00", first.sample_id, _sha(b))
    assert (hub.data / c["cdf_path"]).read_bytes() == b.read_bytes()
    assert c["cdf_path"].startswith("cdf/gc1/conflicts/2026/09/")
    assert store.samples.count(db=hub.db) == 1
    # the same conflicting file again: dedupes on the held conflict
    again = hub.submit(b)
    assert (again.outcome, again.conflict_id) == ("conflict", res.conflict_id)
    assert len(store.conflicts.list("gc1", db=hub.db)) == 1


def test_same_key_on_another_instrument_is_not_a_conflict(hub):
    hub.gc1()
    hub.gc2()
    a = hub.cdf(name="40304")
    b = hub.cdf(name="40304", shift=0.2)
    assert hub.submit(a).outcome == "created"
    assert hub.submit(b, instrument="gc2").outcome == "created"


def test_backfill_before_live_since(hub):
    hub.gc1(live_since=datetime(2026, 9, 25, 14, 23, 1))
    s = hub.sample(hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 14, 23, 0))).sample_id)
    assert s["backfill"] == 1
    s2 = hub.sample(hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 14, 23, 1), shift=0.1)).sample_id)
    assert s2["backfill"] == 0


def test_method_name_is_stored_normalised(hub):
    hub.gc1()
    s = hub.sample(hub.submit(hub.cdf(method_name=" C:\\Chem32\\1\\Methods\\simdistb.m")).sample_id)
    assert s["method_name"] == "SIMDISTB.M"
    s2 = hub.sample(hub.submit(hub.cdf(method_name=None, shift=0.1, name="40999")).sample_id)
    assert s2["method_name"] == ""


@pytest.mark.parametrize("name", ["Blank", "blank", "BLANK", "Blank2", "blank_3", "Blank - 1",
                                  "(blank)", "(Blank)", "[b] blank", "[B] Blank2", " Blank "])
def test_genuine_blank_names(name):
    assert pipeline.is_blank_name(name)


@pytest.mark.parametrize("name", ["", "Blank sample", "40304 blank", "not blank", "blanket",
                                  "B", "[b]", "(blank", "Blank)", "40304"])
def test_not_blank_names(name):
    assert not pipeline.is_blank_name(name)


def test_is_blank_decided_at_submit(hub):
    hub.gc1()
    genuine = hub.sample(hub.submit(hub.cdf("blank", name="(Blank)")).sample_id)
    assert genuine["is_blank"] == 1
    # named blank but carries sample signal: not a genuine blank
    fake = hub.cdf(name="Blank", injected=datetime(2026, 9, 24, 16, 0, 0))
    assert hub.sample(hub.submit(fake).sample_id)["is_blank"] == 0
    # blank-looking signal but a sample name: not a blank
    quiet = hub.cdf("blank", name="40310", injected=datetime(2026, 9, 24, 17, 0, 0))
    assert hub.sample(hub.submit(quiet).sample_id)["is_blank"] == 0


def test_blank_threshold_comes_from_the_conf(hub):
    hub.gc1()
    hub.conf["blank_max_intensity_pa"] = "1e9"
    fake = hub.cdf(name="Blank", injected=datetime(2026, 9, 24, 16, 0, 0))
    assert hub.sample(hub.submit(fake).sample_id)["is_blank"] == 1


def test_stored_file_name_is_safe(hub):
    hub.gc1()
    res = hub.submit(hub.cdf(name='A/B:C*? "x" <y>|z.'))
    s = hub.sample(res.sample_id)
    assert s["lab_id"] == 'A/B:C*? "x" <y>|z.'
    name = Path(s["cdf_path"]).name
    assert name == f"A_B_C__ _x_ _y__z_{res.sample_id}.CDF"
    assert (hub.data / s["cdf_path"]).is_file()


def test_submit_default_data_dir_and_db(hub, monkeypatch):
    hub.gc1()
    monkeypatch.setenv("GC_DATA_DIR", str(hub.data))
    res = pipeline.submit("gc1", hub.cdf(), conf=hub.conf)
    assert res.outcome == "created"
    assert (hub.data / hub.sample(res.sample_id)["cdf_path"]).is_file()
