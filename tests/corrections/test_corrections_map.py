"""The shapes every provider shares (contract §2): cuts, the two maps,
`Corrections`, `CorrectionsUnavailable`, reading an instrument's map, the map
fingerprint, and what counts as a change.

The map decides which LEM number lands on which D86 cut. A map that is
silently "fixed up" (a typo'd cut dropped, two names for one cut resolved by
whichever came last) would apply a correction to the wrong temperature with
nothing on screen to say so, so every doubtful map is a `config` error.
"""
import dataclasses
import json

import pytest

import corrections as C

LONG = "ASTM D2887/D86 - Distillation in Petroleum Products, {}"


def test_the_eleven_cuts_in_order():
    assert C.D86_CUTS == ["IBP", "5%", "10%", "20%", "30%", "50%", "70%",
                          "80%", "90%", "95%", "FBP"]


def test_default_lem_map_is_the_five_real_lem_methods():
    """Exactly the method names production LEM reports for Agilent GC 1 and
    GC 2 (read-only, lem_machine_config, 2026-09-28)."""
    assert C.DEFAULT_LEM_CORRECTION_MAP == {
        "ASTM D2887/D86 - Distillation in Petroleum Products, IBP": "IBP",
        "ASTM D2887/D86 - Distillation in Petroleum Products, 10% Recovery": "10%",
        "ASTM D2887/D86 - Distillation in Petroleum Products, 50% Recovery": "50%",
        "ASTM D2887/D86 - Distillation in Petroleum Products, 90% Recovery": "90%",
        "ASTM D2887/D86 - Distillation in Petroleum Products, FBP": "FBP",
    }


def test_phase1_file_map_covers_every_cut_once():
    assert sorted(C.PHASE1_FILE_MAP.values(), key=C.D86_CUTS.index) == C.D86_CUTS
    assert C.PHASE1_FILE_MAP["IBP - D86"] == "IBP"


def test_phase1_file_map_mirrors_distill():
    distill = pytest.importorskip("distill")
    assert C.PHASE1_FILE_MAP == distill._D86_CORRECTION_TEST_MAP


def test_the_old_ambiguous_name_is_gone():
    """One name meant both 'the file's names' and 'LEM's names'; it was wrong
    for LEM. Two names now, so nobody picks the wrong one by accident."""
    assert not hasattr(C, "DEFAULT_CORRECTION_MAP")


def test_corrections_is_frozen_and_stale_reason_defaults_empty():
    c = C.Corrections(source="lem", fetched_at="2026-09-28T10:00:00+00:00",
                      values={"IBP": 1.0})
    assert c.stale_reason == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.source = "cache"


def test_unavailable_carries_reason_and_kind():
    exc = C.CorrectionsUnavailable("LEM did not answer", "unreachable")
    assert exc.reason == "LEM did not answer"
    assert exc.kind == "unreachable"
    assert "LEM did not answer" in str(exc)


def test_unavailable_rejects_an_unknown_kind():
    with pytest.raises(ValueError):
        C.CorrectionsUnavailable("x", "busy")


def test_none_map_is_the_lem_default_and_a_copy():
    m = C.parse_correction_map(None)
    assert m == C.DEFAULT_LEM_CORRECTION_MAP
    m[LONG.format("IBP")] = "FBP"
    assert C.DEFAULT_LEM_CORRECTION_MAP[LONG.format("IBP")] == "IBP"


def test_empty_string_map_is_the_default():
    assert C.parse_correction_map("  ") == C.DEFAULT_LEM_CORRECTION_MAP


def test_custom_map_json_string():
    raw = json.dumps({"IBP - D86": "IBP", "FBP - D86": "FBP"})
    assert C.parse_correction_map(raw) == {"IBP - D86": "IBP", "FBP - D86": "FBP"}


def test_custom_map_names_are_stripped():
    assert C.parse_correction_map('{" IBP - D86 ": "IBP"}') == {"IBP - D86": "IBP"}


@pytest.mark.parametrize("raw", [
    "{not json",
    "[]",
    '"IBP"',
    "{}",
    '{"": "IBP"}',
    '{"IBP - D86": "IBP ", "x": "40%"}',   # 40% is not a D86 cut here
    '{"IBP - D86": 1}',
    '{"IBP - D86": "IBP", "Initial BP": "IBP"}',  # two names, one cut
])
def test_doubtful_maps_are_config_errors(raw):
    with pytest.raises(C.CorrectionsUnavailable) as info:
        C.parse_correction_map(raw)
    assert info.value.kind == "config"


def test_a_map_that_is_not_a_string_or_none_is_a_config_error():
    with pytest.raises(C.CorrectionsUnavailable) as info:
        C.parse_correction_map({"IBP - D86": "IBP"})
    assert info.value.kind == "config"


# ── the map fingerprint ────────────────────────────────────────────────────

def test_map_key_is_stable_and_order_free():
    a = {"IBP - D86": "IBP", "FBP - D86": "FBP"}
    b = {"FBP - D86": "FBP", "IBP - D86": "IBP"}
    assert C.map_key(a) == C.map_key(b)
    assert isinstance(C.map_key(a), str) and len(C.map_key(a)) >= 16


def test_map_key_changes_with_a_name_or_a_cut():
    base = {"IBP - D86": "IBP", "FBP - D86": "FBP"}
    assert C.map_key(base) != C.map_key({"Initial BP": "IBP", "FBP - D86": "FBP"})
    assert C.map_key(base) != C.map_key({"IBP - D86": "FBP", "FBP - D86": "IBP"})


# ── what counts as a change ───────────────────────────────────────────────

def test_a_missing_cut_counts_as_zero():
    """The pipeline leaves an unmapped cut uncorrected, i.e. +0.0."""
    assert C.values_differ({"IBP": 1.0, "5%": 0.0}, {"IBP": 1.0}) is False
    assert C.values_differ({"IBP": 1.0, "5%": 0.5}, {"IBP": 1.0}) is True


def test_file_eleven_vs_lem_five_with_the_same_five_values_is_not_a_change():
    five = {"IBP": -12.08, "10%": -5.25, "50%": -4.06, "90%": -3.46, "FBP": -5.57}
    file_values = {cut: five.get(cut, 0.0) for cut in C.D86_CUTS}
    assert sum(1 for v in file_values.values() if v == 0.0) == 6
    assert C.values_differ(file_values, five) is False
    assert C.values_differ(five, file_values) is False


def test_a_real_difference_is_a_change():
    assert C.values_differ({"IBP": 1.0}, {"IBP": 1.5}) is True


def test_the_module_is_the_repo_file_not_this_test_folder():
    """This folder is also called `corrections` and has no __init__.py on
    purpose; with one it would be imported in place of corrections.py."""
    from pathlib import Path
    assert Path(C.__file__).name == "corrections.py"
    assert Path(C.__file__).parent == Path(__file__).resolve().parent.parent.parent
