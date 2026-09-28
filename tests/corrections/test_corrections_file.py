"""FileProvider: the phase-1 JSON file, served only to instrument `gc1` (2A1).

Phase 1 (`distill.load_d86_corrections`) answered `{}` for a missing or broken
file, and `{}` means "no correction" — so a share outage silently reported
uncorrected D86 temperatures. The hub's rule is that a failed read is never an
empty result: every way the file can fail is a raise.

What phase 1 did right is kept: a cut the file does not list is uncorrected
(0.0, now recorded explicitly). V4 held five Agilent offsets, so the file
lists five cuts; raising on the other six would hold every sample.
"""
import json
import os
from datetime import datetime

import pytest

import corrections as C

GC1 = {"id": "gc1", "lem_machine_uid": None, "correction_map": None}
NOW = datetime(2026, 9, 28, 10, 0, 0)


def _write(tmp_path, data, name="EQM_corrections.json"):
    p = tmp_path / name
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return str(p)


def _full_section(offset=0.0):
    return {test: {"correction_value": offset + i}
            for i, test in enumerate(C.DEFAULT_CORRECTION_MAP)}


def _provider(path):
    return C.FileProvider(path, clock=lambda: NOW)


def _raises(provider, instrument, kind):
    with pytest.raises(C.CorrectionsUnavailable) as info:
        provider.get(instrument)
    assert info.value.kind == kind, info.value.reason
    return info.value


def test_all_eleven_cuts_from_the_file(tmp_path):
    path = _write(tmp_path, {"Agilent GC": _full_section()})
    got = _provider(path).get(GC1)
    assert got.source == "file"
    assert got.fetched_at == "2026-09-28T10:00:00"
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
def test_an_os_error_other_than_missing_is_unreachable(tmp_path):
    """The file lives on the share: a permission or network error is the share
    not answering, retried, not a setup mistake."""
    path = _write(tmp_path, {"Agilent GC": _full_section()})
    os.chmod(path, 0)
    try:
        _raises(_provider(path), GC1, "unreachable")
    finally:
        os.chmod(path, 0o644)


def test_custom_map(tmp_path):
    path = _write(tmp_path, {"Agilent GC": {"Initial BP": {"correction_value": -2}}})
    inst = dict(GC1, correction_map=json.dumps({"Initial BP": "IBP"}))
    assert _provider(path).get(inst).values == {"IBP": -2.0}


def test_bad_custom_map_is_config(tmp_path):
    path = _write(tmp_path, {"Agilent GC": _full_section()})
    _raises(_provider(path), dict(GC1, correction_map="{"), "config")


def test_refresh_reads_the_file_again(tmp_path):
    path = _write(tmp_path, {"Agilent GC": _full_section()})
    p = _provider(path)
    assert p.get(GC1).values["IBP"] == 0.0
    _write(tmp_path, {"Agilent GC": _full_section(offset=1.0)})
    assert p.refresh(GC1).values["IBP"] == 1.0


def test_changed_since(tmp_path):
    path = _write(tmp_path, {"Agilent GC": _full_section()})
    p = _provider(path)
    assert p.changed_since("gc1", C.Corrections("file", "x", {})) is False  # nothing read yet
    used = p.get(GC1)
    assert p.changed_since("gc1", used) is False
    _write(tmp_path, {"Agilent GC": _full_section(offset=0.5)})
    p.refresh(GC1)
    assert p.changed_since("gc1", used) is True
