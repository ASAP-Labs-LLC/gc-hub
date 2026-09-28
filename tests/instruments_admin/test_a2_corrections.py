"""2A2 T5: the corrections editor (D4b/2C) and seeding gc1 from the phase-1 file."""
from __future__ import annotations

from a2_helpers import ELEVEN, cal_entries, hub, set_corrections  # noqa: F401

import json
from unittest import mock

import pytest

pytest.importorskip("flask")

import corrections  # noqa: E402
import instrument_admin as ia  # noqa: E402
import store  # noqa: E402


def _pending(hub):
    """gc2 (calibrated, no corrections) and gc3 likewise, one pending_corrections sample each."""
    hub.gc1()
    for iid in ("gc2", "gc3"):
        store.instruments.upsert({"id": iid, "name": iid.upper(), "method_map": json.dumps(
            {"SIMDISB.M": "D2887"}), "live_since": "2020-01-01 00:00:00",
            "calibration_cdf": str(hub.cal),
            "calibration_assignments": json.dumps(cal_entries(hub))}, db=hub.db)
    s2 = hub.submit(hub.cdf(name="S2"), instrument="gc2").sample_id
    s3 = hub.submit(hub.cdf(name="S3"), instrument="gc3").sample_id
    import instruments
    hub.worker(corrections_provider=instruments.corrections_provider(hub.db)).run_until_idle()
    for sid in (s2, s3):
        assert store.samples.get(sid, db=hub.db)["status"] == "pending_corrections"
    return s2, s3


def _queued(hub):
    return {j["sample_id"]: j for j in store.jobs.list(state="queued", kind="process", db=hub.db)}


def test_save_writes_table_audit_and_queues_only_this_instrument(hub):
    s2, s3 = _pending(hub)
    out = ia.save_corrections("gc2", ELEVEN, "entered from the 2026 check", by="admin@1.2.3.4",
                              db=hub.db)
    assert out == {"changed": 11, "queued": 1}
    rec = store.corrections.read("gc2", db=hub.db)
    assert rec["values"] == ELEVEN and rec["updated_by"] == "admin@1.2.3.4"
    audit = store.corrections.audit("gc2", db=hub.db)
    assert len(audit) == 11 and {a["reason"] for a in audit} == {"entered from the 2026 check"}
    q = _queued(hub)          # both wait on the 5-minute retry; only gc2's is brought forward
    now = store.now_iso()
    assert q[s2]["not_before"] is None or q[s2]["not_before"] <= now
    assert q[s3]["not_before"] > now


def test_second_save_audits_only_changes(hub):
    _pending(hub)
    ia.save_corrections("gc2", ELEVEN, "first", by="a", db=hub.db)
    out = ia.save_corrections("gc2", dict(ELEVEN, IBP=-3.5), "IBP recheck", by="b", db=hub.db)
    assert out["changed"] == 1
    audit = store.corrections.audit("gc2", db=hub.db)
    assert audit[0]["cut"] == "IBP" and audit[0]["old_value"] == ELEVEN["IBP"]
    assert audit[0]["new_value"] == -3.5 and audit[0]["changed_by"] == "b"


@pytest.mark.parametrize("values", [
    {}, {k: v for k, v in ELEVEN.items() if k != "FBP"}, dict(ELEVEN, IBP="1.0"),
    dict(ELEVEN, IBP=float("nan")), dict(ELEVEN, IBP=50.5), dict(ELEVEN, IBP=True),
    dict(ELEVEN, **{"99%": 1.0}), [1, 2], None,
])
def test_invalid_values_write_nothing(hub, values):
    _pending(hub)
    with pytest.raises(ia.AdminError) as e:
        ia.save_corrections("gc2", values, "reason", by="a", db=hub.db)
    assert e.value.status == 400 and e.value.extra.get("errors")
    assert store.corrections.read("gc2", db=hub.db) is None
    assert store.corrections.audit("gc2", db=hub.db) == []


@pytest.mark.parametrize("reason", ["", "   ", None, 5, "x" * 501])
def test_reason_required(hub, reason):
    _pending(hub)
    with pytest.raises(ia.AdminError):
        ia.save_corrections("gc2", ELEVEN, reason, by="a", db=hub.db)
    assert store.corrections.read("gc2", db=hub.db) is None


def test_table_and_queue_are_one_transaction(hub):
    s2, _s3 = _pending(hub)
    with mock.patch.object(store.jobs, "enqueue_for_status", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            ia.save_corrections("gc2", ELEVEN, "r", by="a", db=hub.db)
    assert store.corrections.read("gc2", db=hub.db) is None
    assert store.corrections.audit("gc2", db=hub.db) == []


def test_unknown_instrument(hub):
    with pytest.raises(ia.AdminError) as e:
        ia.save_corrections("gc9", ELEVEN, "r", by="a", db=hub.db)
    assert e.value.status == 404


def test_view(hub):
    _pending(hub)
    v = ia.corrections_view("gc2", hub.conf, db=hub.db)
    assert v["cuts"] == corrections.D86_CUTS and v["max_abs"] == corrections.MAX_ABS_CORRECTION_C
    assert v["values"] is None and v["source"] is None and v["audit"] == []
    ia.save_corrections("gc2", ELEVEN, "r", by="a", db=hub.db)
    v = ia.corrections_view("gc2", hub.conf, db=hub.db)
    assert v["values"] == ELEVEN and v["source"] == "hub" and len(v["audit"]) == 11


def test_view_gc1_shows_the_interim_file(hub):
    hub.gc1()
    v = ia.corrections_view("gc1", hub.conf, db=hub.db)
    assert v["source"] == "file" and v["values"] == corrections.seed_from_file(str(hub.corrections))
    assert v["can_seed"] is True
    v = ia.corrections_view("gc1", dict(hub.conf, correction_factors_json="/nope.json"), db=hub.db)
    assert v["values"] is None and "does not exist" in v["file_error"]


def test_seed_gc1(hub):
    hub.gc1()
    out = ia.seed_gc1(hub.conf, by="admin@x", db=hub.db)
    want = corrections.seed_from_file(str(hub.corrections))
    assert out["values"] == want
    rec = store.corrections.read("gc1", db=hub.db)
    assert rec["values"] == want
    audit = store.corrections.audit("gc1", db=hub.db)
    assert {a["reason"] for a in audit} == {"seeded from correction_factors.json"}
    assert len(audit) == 11
    with pytest.raises(ia.AdminError) as e:              # once seeded, never again
        ia.seed_gc1(hub.conf, by="admin@x", db=hub.db)
    assert e.value.status == 409
    v = ia.corrections_view("gc1", hub.conf, db=hub.db)
    assert v["source"] == "hub" and v["can_seed"] is False


def test_seed_refuses_a_bad_file(hub):
    hub.gc1()
    with pytest.raises(ia.AdminError) as e:
        ia.seed_gc1(dict(hub.conf, correction_factors_json="/nope.json"), by="a", db=hub.db)
    assert e.value.status == 409 and "does not exist" in e.value.message
    assert store.corrections.read("gc1", db=hub.db) is None


def test_seed_refuses_after_manual_entry(hub):
    hub.gc1()
    set_corrections(hub, "gc1")
    with pytest.raises(ia.AdminError):
        ia.seed_gc1(hub.conf, by="a", db=hub.db)
    assert store.corrections.read("gc1", db=hub.db)["values"] == ELEVEN
