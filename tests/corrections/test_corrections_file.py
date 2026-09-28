"""FileProvider / seed_from_file: the phase-1 JSON file, read once to SEED the
hub's corrections for instrument `gc1` (the hub owns them afterwards).

Phase 1 (`distill.load_d86_corrections`) answered `{}` for a missing or broken
file, and `{}` means "no correction" — so a share outage silently reported
uncorrected D86 temperatures. The hub's rule is that a failed read is never an
empty result: every way the file can fail is a raise.

What phase 1 did right is kept: a cut the file does not list is uncorrected
(0.0, now recorded explicitly). V4 held five Agilent offsets, so the file
lists five cuts; raising on the other six would hold every sample.

The file's names are fixed (`PHASE1_FILE_MAP`). Every failure is `config`:
seeding is a one-off admin act, and a person has to look at a file that
cannot be read.
"""
import json
import os
from datetime import datetime, timezone

import pytest

import corrections as C

GC1 = {"id": "gc1", "lem_machine_uid": None, "correction_map": None}
MTIME = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc).timestamp()


def _write(tmp_path, data, name="EQM_corrections.json"):
    p = tmp_path / name
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return str(p)


def _full_section(offset=0.0):
    return {test: {"correction_value": offset + i}
            for i, test in enumerate(C.PHASE1_FILE_MAP)}


def _provider(path):
    if os.path.exists(path):
        os.utime(path, (MTIME, MTIME))
    return C.FileProvider(path)


def _raises(provider, instrument, kind):
    with pytest.raises(C.CorrectionsUnavailable) as info:
        provider.get(instrument)
    assert info.value.kind == kind, info.value.reason
    return info.value


def test_all_eleven_cuts_from_the_file(tmp_path):
    path = _write(tmp_path, {"Agilent GC": _full_section()})
    got = _provider(path).get(GC1)
    assert got.source == "file"
    assert got.updated_at == "2026-09-28T10:00:00+00:00"   # the file's mtime, in UTC
    assert list(got.values) == C.D86_CUTS
    assert got.values == {cut: float(i) for i, cut in enumerate(C.D86_CUTS)}


def test_the_v4_five_and_explicit_zero_for_the_rest(tmp_path):
    path = _write(tmp_path, {"Agilent GC": {
        "IBP - D86": {"correction_value": -12.08},
        "10% - D86": {"correction_value": -5.25},
        "50% - D86": {"correction_value": -4.06},
        "90% - D86": {"correction_value": -3.46},
        "FBP - D86": {"correction_value": -5.57},
    }, "PAC Flash 2": {"Flash - D7094": {"correction_value": -3.0}}})
    got = _provider(path).get(GC1).values
    assert set(got) == set(C.D86_CUTS)
    assert got["IBP"] == -12.08 and got["FBP"] == -5.57
    assert got["5%"] == 0.0 and got["95%"] == 0.0


def test_matches_phase1_for_the_listed_cuts(tmp_path):
    distill = pytest.importorskip("distill")
    data = {"Agilent GC": {"IBP - D86": {"correction_value": -1.5},
                           "50% - D86": {"correction_value": "2.25"},
                           "Unrelated": {"correction_value": 9}}}
    path = _write(tmp_path, data)
    phase1 = distill.load_d86_corrections(path)
    got = _provider(path).get(GC1).values
    for cut, value in phase1.items():
        assert got[cut] == value
    assert all(got[c] == 0.0 for c in C.D86_CUTS if c not in phase1)


def test_only_gc1(tmp_path):
    path = _write(tmp_path, {"Agilent GC": _full_section()})
    err = _raises(_provider(path), {"id": "gc2", "lem_machine_uid": None,
                                     "correction_map": None}, "config")
    assert "gc2" in err.reason


def test_missing_file_is_config(tmp_path):
    err = _raises(_provider(str(tmp_path / "nope.json")), GC1, "config")
    assert "nope.json" in err.reason


def test_unparseable_file_is_config(tmp_path):
    _raises(_provider(_write(tmp_path, "{not json")), GC1, "config")


def test_top_level_not_an_object_is_config(tmp_path):
    _raises(_provider(_write(tmp_path, [1, 2])), GC1, "config")


def test_missing_section_is_config(tmp_path):
    err = _raises(_provider(_write(tmp_path, {"PAC Flash 2": {}})), GC1, "config")
    assert "Agilent GC" in err.reason


def test_section_not_an_object_is_config(tmp_path):
    _raises(_provider(_write(tmp_path, {"Agilent GC": []})), GC1, "config")


def test_section_with_no_mapped_test_is_config(tmp_path):
    """An empty section, or one keyed by other names, is a wrong file, not a
    statement that every cut is uncorrected."""
    _raises(_provider(_write(tmp_path, {"Agilent GC": {}})), GC1, "config")
    _raises(_provider(_write(tmp_path, {"Agilent GC": {"IBP": {"correction_value": 1}}},
                             name="b.json")), GC1, "config")


@pytest.mark.parametrize("entry", [
    {"correction_value": "abc"},
    {"correction_value": None},
    {"correction_value": float("nan")},
    {"value": 1.0},
    5.0,
])
def test_an_unreadable_mapped_value_is_config(tmp_path, entry):
    path = _write(tmp_path, json.dumps({"Agilent GC": {"IBP - D86": entry}}))
    err = _raises(_provider(path), GC1, "config")
    assert "IBP - D86" in err.reason


def test_an_unreadable_unmapped_value_is_ignored(tmp_path):
    path = _write(tmp_path, {"Agilent GC": {"IBP - D86": {"correction_value": 1},
                                            "Other": {"correction_value": "x"}}})
    assert _provider(path).get(GC1).values["IBP"] == 1.0


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions and a non-root user")
def test_an_os_error_other_than_missing_is_config_too(tmp_path):
    """Seeding is a one-off admin act: a file that cannot be read needs a
    person, and there is nothing to retry into."""
    path = _write(tmp_path, {"Agilent GC": _full_section()})
    os.chmod(path, 0)
    try:
        err = _raises(_provider(path), GC1, "config")
        assert "could not be read" in err.reason
    finally:
        os.chmod(path, 0o644)


def test_a_value_beyond_the_sanity_bound_is_config(tmp_path):
    path = _write(tmp_path, {"Agilent GC": {"IBP - D86": {"correction_value": 75.0}}})
    err = _raises(_provider(path), GC1, "config")
    assert "IBP" in err.reason


# ── seeding gc1 ───────────────────────────────────────────────────────────

def test_seed_from_file_returns_the_eleven_values(tmp_path):
    path = _write(tmp_path, {"Agilent GC": {
        "IBP - D86": {"correction_value": -12.08},
        "FBP - D86": {"correction_value": -5.57}}})
    got = C.seed_from_file(path)
    assert list(got) == C.D86_CUTS
    assert got["IBP"] == -12.08 and got["FBP"] == -5.57 and got["50%"] == 0.0
    assert C.validate_values(got) == []


def test_seed_from_file_returns_a_plain_dict(tmp_path):
    path = _write(tmp_path, {"Agilent GC": _full_section()})
    got = C.seed_from_file(path)
    got["IBP"] = 99.0
    assert C.seed_from_file(path)["IBP"] == 0.0


@pytest.mark.parametrize("data", ["{not json", {"PAC Flash 2": {}}, {"Agilent GC": {}}])
def test_seed_from_file_keeps_the_strict_rules(tmp_path, data):
    with pytest.raises(C.CorrectionsUnavailable) as info:
        C.seed_from_file(_write(tmp_path, data))
    assert info.value.kind == "config"


def test_seed_from_a_missing_file_is_config(tmp_path):
    with pytest.raises(C.CorrectionsUnavailable):
        C.seed_from_file(str(tmp_path / "gone.json"))


# ── when the values were set ───────────────────────────────────────────────

def test_updated_at_is_the_files_mtime_not_the_read_time(tmp_path):
    """The record says since when these values were in force; the moment the
    hub happened to read them says nothing about that."""
    path = _write(tmp_path, {"Agilent GC": _full_section()})
    then = datetime(2026, 3, 1, 8, 30, 15, tzinfo=timezone.utc).timestamp()
    os.utime(path, (then, then))
    assert C.FileProvider(path).get(GC1).updated_at == "2026-03-01T08:30:15+00:00"


def test_updated_at_is_utc_with_an_offset(tmp_path):
    got = _provider(_write(tmp_path, {"Agilent GC": _full_section()})).get(GC1)
    assert datetime.fromisoformat(got.updated_at).utcoffset().total_seconds() == 0


def test_the_file_provider_holds_no_clock_or_lock():
    import inspect
    assert list(inspect.signature(C.FileProvider).parameters) == ["json_path"]
    assert not hasattr(C.FileProvider("x.json"), "_lock")
