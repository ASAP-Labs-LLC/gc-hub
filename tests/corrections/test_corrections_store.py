"""StoreProvider: the hub's own per-instrument correction factors.

GC factors are set on the hub's Instruments page and read back through an
injected `read_fn(instrument_id) -> {values, updated_at, updated_by} | None`.
An instrument with nothing saved has NO corrections, which is not the same as
all-zero: its samples wait in `pending_corrections` until someone sets them
("Corrections not set for GC-2"). A partial or out-of-bounds record is a
configuration error, never partly applied.
"""
import math

import pytest

import corrections as C

GC2 = {"id": "gc2", "name": "GC-2"}


def eleven(**over):
    values = {cut: 0.0 for cut in C.D86_CUTS}
    values.update(over)
    return values


def record(values=None, updated_at="2026-09-28T10:00:00+00:00", updated_by="ryan"):
    return {"values": eleven() if values is None else values,
            "updated_at": updated_at, "updated_by": updated_by}


def provider(rec):
    seen = []

    def read_fn(instrument_id):
        seen.append(instrument_id)
        return rec
    p = C.StoreProvider(read_fn)
    p.seen = seen
    return p


def config_error(p, instrument=GC2):
    with pytest.raises(C.CorrectionsUnavailable) as info:
        p.get(instrument)
    assert info.value.kind == "config"
    return info.value


def test_the_saved_values_are_returned_as_the_hubs():
    p = provider(record(eleven(IBP=-12.08, FBP=-5.57)))
    got = p.get(GC2)
    assert p.seen == ["gc2"]
    assert got.source == "hub"
    assert got.fetched_at == "2026-09-28T10:00:00+00:00"
    assert got.updated_by == "ryan"
    assert list(got.values) == C.D86_CUTS
    assert got.values["IBP"] == -12.08 and got.values["5%"] == 0.0


def test_values_are_floats_in_cut_order():
    rec = record({cut: 1 for cut in reversed(C.D86_CUTS)})
    got = provider(rec).get(GC2).values
    assert list(got) == C.D86_CUTS
    assert all(type(v) is float for v in got.values())


def test_the_result_does_not_alias_the_store():
    rec = record()
    got = provider(rec).get(GC2)
    rec["values"]["IBP"] = 9.0
    assert got.values["IBP"] == 0.0


def test_not_set_is_a_config_error_named_for_the_instrument():
    err = config_error(provider(None))
    assert err.reason == "Corrections not set for GC-2"


def test_the_id_is_used_when_there_is_no_name():
    err = config_error(provider(None), {"id": "gc3"})
    assert "gc3" in err.reason


def test_a_partial_set_is_a_config_error():
    values = eleven()
    del values["FBP"]
    err = config_error(provider(record(values)))
    assert "FBP" in err.reason and "GC-2" in err.reason


@pytest.mark.parametrize("bad", [math.nan, 51.0, "x", None])
def test_a_bad_value_is_a_config_error(bad):
    config_error(provider(record(eleven(IBP=bad))))


@pytest.mark.parametrize("rec", [
    {"updated_at": "2026-09-28T10:00:00+00:00"},            # no values
    {"values": [], "updated_at": "2026-09-28T10:00:00+00:00"},
    "not a dict",
])
def test_a_malformed_record_is_a_config_error(rec):
    config_error(provider(rec))


def test_a_missing_updated_by_is_empty():
    rec = record()
    del rec["updated_by"]
    assert provider(rec).get(GC2).updated_by == ""


def test_a_record_without_updated_at_is_a_config_error():
    """The record of which values were in force needs to say since when."""
    rec = record()
    del rec["updated_at"]
    config_error(provider(rec))


def test_no_instrument_id_is_a_config_error():
    config_error(provider(record()), {"name": "GC-2"})


def test_a_failing_read_is_not_turned_into_not_set():
    """A database error is not 'no corrections'; it propagates to the caller."""
    def read_fn(instrument_id):
        raise RuntimeError("database is locked")
    with pytest.raises(RuntimeError):
        C.StoreProvider(read_fn).get(GC2)
