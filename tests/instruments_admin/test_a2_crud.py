"""2A2 T2: instrument create/edit (instrument_admin), in-process."""
from __future__ import annotations

from a2_helpers import hub  # noqa: F401

import json
from datetime import datetime, timedelta

import pytest

pytest.importorskip("flask")

import ingest_api  # noqa: E402
import instrument_admin as ia  # noqa: E402
import instruments  # noqa: E402
import store  # noqa: E402


def test_create_defaults(hub):
    row = ia.create({"id": "gc2", "name": " GC-2 "}, db=hub.db)
    assert row["id"] == "gc2" and row["name"] == "GC-2"
    assert row["method"] == "D2887" and row["enabled"] == 1
    assert row["live_since"] is None                      # everything is backfill until set (D11)
    assert json.loads(row["method_map"]) == instruments.DEFAULT_METHOD_MAP
    assert "token_hash" not in row and row["has_token"] is False


@pytest.mark.parametrize("bad", ["", "GC2", "2gc", "gc 2", "gc/2", "a" * 33, None, 5])
def test_create_refuses_bad_ids(hub, bad):
    with pytest.raises(ia.AdminError) as e:
        ia.create({"id": bad, "name": "x"}, db=hub.db)
    assert e.value.status == 400


def test_create_refuses_duplicates_and_missing_name(hub):
    ia.create({"id": "gc2", "name": "GC-2"}, db=hub.db)
    with pytest.raises(ia.AdminError) as e:
        ia.create({"id": "gc2", "name": "again"}, db=hub.db)
    assert e.value.status == 409
    with pytest.raises(ia.AdminError):
        ia.create({"id": "gc3"}, db=hub.db)
    with pytest.raises(ia.AdminError):
        ia.create({"id": "gc3", "name": "x", "token_hash": "zz"}, db=hub.db)


def test_create_with_fields(hub):
    row = ia.create({"id": "gc3", "name": "GC-3", "enabled": False,
                     "live_since": "2026-10-01T08:00", "lem_machine_uid": "M-3"}, db=hub.db)
    assert row["enabled"] == 0
    assert row["live_since"] == "2026-10-01 08:00:00"
    assert row["lem_machine_uid"] == "M-3"


def test_update_fields(hub):
    hub.gc1()
    row, _w = ia.update("gc1", {"name": "GC-1 (FID)", "enabled": 0, "lem_machine_uid": "uid-1"},
                        db=hub.db)
    assert (row["name"], row["enabled"], row["lem_machine_uid"]) == ("GC-1 (FID)", 0, "uid-1")
    row, _w = ia.update("gc1", {"lem_machine_uid": "", "enabled": True}, db=hub.db)
    assert row["lem_machine_uid"] is None and row["enabled"] == 1


@pytest.mark.parametrize("fields", [
    {"name": ""}, {"name": "x" * 65}, {"name": "a\x00b"}, {"enabled": "yes"},
    {"method": "D7096"}, {"live_since": "2026-10-01T08:00:00+02:00"}, {"live_since": "tomorrow"},
    {"live_since": "2026-10-01Z"}, {"lem_machine_uid": 5}, {"calibration_cdf": "/x"},
    {"token_hash": "a"}, {"export_path": "/x"}, {"id": "gc9"}, {},
])
def test_update_refuses(hub, fields):
    hub.gc1()
    with pytest.raises(ia.AdminError) as e:
        ia.update("gc1", fields, db=hub.db)
    assert e.value.status == 400


def test_update_unknown_instrument(hub):
    with pytest.raises(ia.AdminError) as e:
        ia.update("gc7", {"name": "x"}, db=hub.db)
    assert e.value.status == 404


def test_live_since_set_and_cleared(hub):
    hub.gc1()
    row, warnings = ia.update("gc1", {"live_since": "2026-10-01 07:30"}, db=hub.db)
    assert row["live_since"] == "2026-10-01 07:30:00"
    assert any("from now on" in w for w in warnings)
    assert any("has not reported" in w for w in warnings)     # no agent: skew unknown
    row, _ = ia.update("gc1", {"live_since": ""}, db=hub.db)
    assert row["live_since"] is None
    row, _ = ia.update("gc1", {"live_since": None}, db=hub.db)
    assert row["live_since"] is None


def _heartbeat(hub, skew_seconds):
    agent_time = (datetime.now() + timedelta(seconds=skew_seconds)).strftime("%Y-%m-%dT%H:%M:%S")
    ingest_api.record_heartbeat("gc1", {"agent_time": agent_time, "host": "PC"}, db=hub.db)


def test_live_since_warns_on_clock_skew(hub):
    hub.gc1()
    _heartbeat(hub, 600)
    _row, warnings = ia.update("gc1", {"live_since": "2026-10-01 07:30"}, db=hub.db)
    assert any("ahead" in w and "10 min" in w for w in warnings), warnings
    _heartbeat(hub, -300)
    _row, warnings = ia.update("gc1", {"live_since": "2026-10-01 07:30"}, db=hub.db)
    assert any("behind" in w for w in warnings), warnings


def test_live_since_no_skew_warning_when_close(hub):
    hub.gc1()
    _heartbeat(hub, 5)
    _row, warnings = ia.update("gc1", {"live_since": "2026-10-01 07:30"}, db=hub.db)
    assert not any("clock" in w for w in warnings), warnings
    _row, warnings = ia.update("gc1", {"name": "GC-1"}, db=hub.db)
    assert warnings == []


def test_public_row_never_has_the_token_hash(hub):
    hub.gc1()
    ingest_api.mint_token("gc1", db=hub.db)
    row = ia.public_row(store.instruments.get("gc1", db=hub.db))
    assert "token_hash" not in row and row["has_token"] is True
    assert all("token_hash" not in r for r in ia.list_instruments(db=hub.db))


# ── review I1: clearing live_since or moving it later stops exports ────────

def test_clearing_live_since_warns_that_exports_stop(hub):
    hub.gc1(live_since=datetime(2020, 1, 1))
    _row, warnings = ia.update("gc1", {"live_since": ""}, db=hub.db)
    assert any("stops" in w and "export" in w for w in warnings), warnings


def test_clearing_an_unset_live_since_says_nothing(hub):
    ia.create({"id": "gc2", "name": "GC-2"}, db=hub.db)
    _row, warnings = ia.update("gc2", {"live_since": None}, db=hub.db)
    assert warnings == []


def test_moving_live_since_later_warns(hub):
    hub.gc1(live_since=datetime(2020, 1, 1))
    _row, warnings = ia.update("gc1", {"live_since": "2021-01-01 00:00"}, db=hub.db)
    assert any("later" in w for w in warnings), warnings
    _row, warnings = ia.update("gc1", {"live_since": "2020-06-01 00:00"}, db=hub.db)   # earlier
    assert not any("later" in w for w in warnings), warnings


def test_future_live_since_warns(hub):
    hub.gc1(live_since=datetime(2020, 1, 1))
    future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    _row, warnings = ia.update("gc1", {"live_since": future}, db=hub.db)
    assert any("in the future" in w for w in warnings), warnings


@pytest.mark.parametrize("bad", ["2026-10-01", "2026-10-01 08", "2026-10-01 08:00:00.5",
                                 "2026-10-01  08:00", "20261001 0800", "2026-10-01 08:00x"])
def test_live_since_format_is_strict(hub, bad):
    hub.gc1()
    with pytest.raises(ia.AdminError):
        ia.update("gc1", {"live_since": bad}, db=hub.db)
