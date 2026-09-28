"""The shapes every provider shares (contract §2): cuts, the default map,
`Corrections`, `CorrectionsUnavailable`, and reading an instrument's map.

The map decides which LEM number lands on which D86 cut. A map that is
silently "fixed up" (a typo'd cut dropped, two names for one cut resolved by
whichever came last) would apply a correction to the wrong temperature with
nothing on screen to say so, so every doubtful map is a `config` error.
"""
import dataclasses
import json

import pytest

import corrections as C


def test_the_eleven_cuts_in_order():
    assert C.D86_CUTS == ["IBP", "5%", "10%", "20%", "30%", "50%", "70%",
                          "80%", "90%", "95%", "FBP"]


def test_default_map_covers_every_cut_once():
    assert sorted(C.DEFAULT_CORRECTION_MAP.values(), key=C.D86_CUTS.index) == C.D86_CUTS
    assert C.DEFAULT_CORRECTION_MAP["IBP - D86"] == "IBP"
    assert C.DEFAULT_CORRECTION_MAP["FBP - D86"] == "FBP"


def test_default_map_mirrors_phase1_distill():
    """The contract says it mirrors distill._D86_CORRECTION_TEST_MAP; if one
    changes without the other, file corrections and LEM corrections diverge."""
    distill = pytest.importorskip("distill")
    assert C.DEFAULT_CORRECTION_MAP == distill._D86_CORRECTION_TEST_MAP


def test_corrections_is_frozen():
    c = C.Corrections(source="lem", fetched_at="2026-09-28T10:00:00", values={"IBP": 1.0})
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.source = "cache"


def test_unavailable_carries_reason_and_kind():
    exc = C.CorrectionsUnavailable("LEM did not answer", "unreachable")
    assert exc.reason == "LEM did not answer"
    assert exc.kind == "unreachable"
    assert "LEM did not answer" in str(exc)
    assert isinstance(exc, Exception)


def test_unavailable_rejects_an_unknown_kind():
    with pytest.raises(ValueError):
        C.CorrectionsUnavailable("x", "busy")


def test_none_map_is_the_default_and_a_copy():
    m = C.parse_correction_map(None)
    assert m == C.DEFAULT_CORRECTION_MAP
    m["IBP - D86"] = "FBP"
    assert C.DEFAULT_CORRECTION_MAP["IBP - D86"] == "IBP"


def test_empty_string_map_is_the_default():
    assert C.parse_correction_map("  ") == C.DEFAULT_CORRECTION_MAP


def test_custom_map_json_string():
    raw = json.dumps({"ASTM D2887/D86 - Distillation in Petroleum Products, IBP": "IBP",
                      "ASTM D2887/D86 - Distillation in Petroleum Products, FBP": "FBP"})
    assert C.parse_correction_map(raw) == {
        "ASTM D2887/D86 - Distillation in Petroleum Products, IBP": "IBP",
        "ASTM D2887/D86 - Distillation in Petroleum Products, FBP": "FBP"}


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


def test_the_module_is_the_repo_file_not_this_test_folder():
    """This folder is also called `corrections` and has no __init__.py on
    purpose; with one it would be imported in place of corrections.py."""
    from pathlib import Path
    assert Path(C.__file__).name == "corrections.py"
    assert Path(C.__file__).parent == Path(__file__).resolve().parent.parent.parent
