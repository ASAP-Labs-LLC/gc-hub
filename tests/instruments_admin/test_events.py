"""v3.1: the operations the setup guide cares about write an
``instrument_events`` row (schema v4) with the caller's ``by``, in the same
transaction as the change where there is one; a refused change writes none."""
from __future__ import annotations

from a2_helpers import ELEVEN, cal_entries, hub  # noqa: F401

import json

import pytest

pytest.importorskip("flask")

import exports  # noqa: E402
import ingest_api  # noqa: E402
import instrument_admin as ia  # noqa: E402
import store  # noqa: E402

BY = "Ryan C (10.0.0.5)"


def _events(hub, inst="gc2"):
    return [(e["kind"], e["by"], e["detail"]) for e in
            reversed(store.instrument_events.list(instrument_id=inst, db=hub.db))]


def test_create_records_created(hub):
    ia.create({"id": "gc2", "name": "GC-2"}, db=hub.db, by=BY)
    assert _events(hub) == [("created", BY, {"name": "GC-2"})]


def test_create_with_live_since_records_it_too(hub):
    ia.create({"id": "gc2", "name": "GC-2", "live_since": "2026-10-01 08:00"}, db=hub.db, by=BY)
    assert _events(hub) == [("created", BY, {"name": "GC-2"}),
                            ("live_since", BY, {"live_since": "2026-10-01 08:00:00", "old": None})]


def test_keeping_the_hub_only_results_file_is_an_explicit_recorded_choice(hub):
    hub.gc2()
    exp = exports.HubExporter(hub.db, data_dir=hub.data)
    out = ia.keep_hub_only_export("gc2", exp, by=BY)
    assert out["configured"] is False
    ev = _events(hub)
    assert [k for k, _b, _d in ev] == ["export_hub_only"] and ev[0][1] == BY
    assert ev[0][2]["path"].endswith("gc2_results.csv")


def test_the_hub_only_choice_is_refused_when_a_results_file_is_set(hub, tmp_path):
    hub.gc2()
    exp = exports.HubExporter(hub.db, data_dir=hub.data)
    ia.set_export_path("gc2", str(tmp_path / "gc2.csv"), exp, by=BY)
    with pytest.raises(ia.AdminError) as e:
        ia.keep_hub_only_export("gc2", exp, by=BY)
    assert e.value.status == 409
    assert [k for k, _b, _d in _events(hub)] == ["export_path"]


def test_the_export_path_event_is_in_the_same_transaction(hub, tmp_path, monkeypatch):
    """If recording the event fails, the path is not changed either."""
    hub.gc2()
    exp = exports.HubExporter(hub.db, data_dir=hub.data)
    real = store.instrument_events.add

    def boom(db, iid, kind, **kw):
        if kind == "export_path":
            raise RuntimeError("event write failed")
        return real(db, iid, kind, **kw)

    monkeypatch.setattr(store.instrument_events, "add", staticmethod(boom))
    with pytest.raises(RuntimeError):
        ia.set_export_path("gc2", str(tmp_path / "gc2.csv"), exp, by=BY)
    assert not store.instruments.get("gc2", db=hub.db)["export_path"]


def test_a_refused_create_records_nothing(hub):
    ia.create({"id": "gc2", "name": "GC-2"}, db=hub.db, by=BY)
    with pytest.raises(ia.AdminError):
        ia.create({"id": "gc2", "name": "again"}, db=hub.db, by=BY)
    assert [k for k, _b, _d in _events(hub)] == ["created"]


def test_live_since_set_and_cleared_are_recorded_other_edits_are_not(hub):
    hub.gc2(live_since=None)
    ia.update("gc2", {"name": "GC-2 FID"}, db=hub.db, by=BY)
    assert _events(hub) == []
    ia.update("gc2", {"live_since": "2026-10-01 08:00"}, db=hub.db, by=BY)
    ia.update("gc2", {"live_since": "2026-10-01 08:00"}, db=hub.db, by=BY)   # unchanged
    ia.update("gc2", {"live_since": ""}, db=hub.db, by=BY)
    assert _events(hub) == [
        ("live_since", BY, {"live_since": "2026-10-01 08:00:00", "old": None}),
        ("live_since", BY, {"live_since": None, "old": "2026-10-01 08:00:00"})]


def test_export_path_and_adopt_are_recorded(hub, tmp_path):
    hub.gc2()
    exp = exports.HubExporter(hub.db, data_dir=hub.data)
    target = tmp_path / "gc2.csv"
    ia.set_export_path("gc2", str(target), exp, by=BY)
    target.write_text("a,b\n", encoding="utf-8")
    try:
        ia.adopt_export("gc2", exp, by=BY)
    except ia.AdminError:
        pass
    kinds = [k for k, _b, _d in _events(hub)]
    assert kinds[0] == "export_path" and _events(hub)[0][2] == {"path": str(target)}
    with pytest.raises(ia.AdminError):
        ia.set_export_path("gc2", "relative.csv", exp, by=BY)
    assert [k for k, _b, _d in _events(hub)].count("export_path") == 1


def test_calibration_cdf_and_save_are_recorded(hub):
    hub.gc1()
    hub.gc2()
    cal = hub.submit(hub.cal, instrument="gc2").sample_id
    ia.set_calibration_cdf("gc2", hub.conf, sample_id=cal, db=hub.db, data_dir=hub.data, by=BY)
    ia.save_calibration("gc2", cal_entries(hub), 50, hub.conf, db=hub.db, data_dir=hub.data, by=BY)
    ev = _events(hub)
    assert [k for k, _b, _d in ev] == ["calibration_cdf", "calibration_saved"]
    assert ev[0][2]["sample_id"] == cal
    assert ev[1][2]["assigned"] >= 2 and ev[1][1] == BY


def test_corrections_saved_is_recorded_with_the_count(hub):
    hub.gc2()
    ia.save_corrections("gc2", ELEVEN, "EQM study", by=BY, db=hub.db)
    assert _events(hub) == [("corrections_saved", BY, {"changed": 11, "reason": "EQM study"})]
    with pytest.raises(ia.AdminError):
        ia.save_corrections("gc2", {"IBP": 1}, "x", by=BY, db=hub.db)
    assert len(_events(hub)) == 1


def test_method_mapped_and_unmapped_are_recorded(hub):
    hub.gc2()
    ia.set_method_mapping("gc2", "new.m", "D2887", db=hub.db, by=BY)
    ia.set_method_mapping("gc2", "NEW.M", None, db=hub.db, by=BY)
    assert _events(hub) == [
        ("method_mapped", BY, {"method": "NEW.M", "hub_method": "D2887", "queued": 0}),
        ("method_mapped", BY, {"method": "NEW.M", "hub_method": None, "queued": 0})]


def test_token_minted_and_revoked_are_recorded(hub):
    hub.gc2()
    ingest_api.mint_token("gc2", db=hub.db, by=BY)
    ingest_api.revoke_token("gc2", db=hub.db, by=BY)
    assert [(k, b) for k, b, _d in _events(hub)] == [("installer", BY), ("token_revoked", BY)]
    with pytest.raises(LookupError):
        ingest_api.revoke_token("nope", db=hub.db, by=BY)
    assert store.instrument_events.list(instrument_id="nope", db=hub.db) == []


def test_events_never_hold_a_token(hub):
    hub.gc2()
    token = ingest_api.mint_token("gc2", db=hub.db, by=BY)
    with store.connection(hub.db) as conn:
        dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM instrument_events")])
    assert token not in dump and ingest_api.token_hash(token) not in dump
