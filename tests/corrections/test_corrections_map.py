"""The shapes every provider shares: cuts, the phase-1 file map, `Corrections`,
`CorrectionsUnavailable`, what counts as a change, and the editor's checks.

GC correction factors live in the hub, per instrument (Ryan, 2026-09-28): LEM
holds none for the GCs. A correction is added to a reported D86 temperature,
so a doubtful value is refused, never repaired.
"""
import dataclasses
import math

import pytest

import corrections as C


def test_the_eleven_cuts_in_order():
    assert C.D86_CUTS == ["IBP", "5%", "10%", "20%", "30%", "50%", "70%",
                          "80%", "90%", "95%", "FBP"]


def test_phase1_file_map_covers_every_cut_once():
    assert sorted(C.PHASE1_FILE_MAP.values(), key=C.D86_CUTS.index) == C.D86_CUTS


def test_phase1_file_map_mirrors_distill():
    distill = pytest.importorskip("distill")
    assert C.PHASE1_FILE_MAP == distill._D86_CORRECTION_TEST_MAP


def test_nothing_lem_specific_is_left():
    for name in ("LemProvider", "MemoryCacheStore", "requests_http_get",
                 "DEFAULT_LEM_CORRECTION_MAP", "DEFAULT_CORRECTION_MAP",
                 "parse_correction_map", "map_key"):
        assert not hasattr(C, name), name


def test_corrections_is_frozen():
    c = C.Corrections(source="hub", fetched_at="2026-09-28T10:00:00+00:00",
                      values={"IBP": 1.0})
    assert c.updated_by == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.source = "file"


def test_unavailable_is_config_only():
    exc = C.CorrectionsUnavailable("Corrections not set for GC-2")
    assert exc.kind == "config" and exc.reason == "Corrections not set for GC-2"
    assert "GC-2" in str(exc)
    with pytest.raises(ValueError):
        C.CorrectionsUnavailable("x", "unreachable")


# ── the editor's checks ───────────────────────────────────────────────────

def eleven(**over):
    values = {cut: 0.0 for cut in C.D86_CUTS}
    values.update(over)
    return values


def test_the_sanity_bound_is_a_named_constant():
    assert C.MAX_ABS_CORRECTION_C == 50.0


def test_eleven_finite_values_are_valid():
    assert C.validate_values(eleven(IBP=-12.08, FBP=-5.57)) == []


def test_the_bound_itself_is_allowed():
    assert C.validate_values(eleven(IBP=50.0, FBP=-50)) == []


@pytest.mark.parametrize("value", [50.01, -50.5, 1e6])
def test_beyond_the_bound_is_refused(value):
    errors = C.validate_values(eleven(IBP=value))
    assert len(errors) == 1 and "IBP" in errors[0]


def test_a_missing_cut_is_named():
    values = eleven()
    del values["95%"]
    del values["FBP"]
    errors = C.validate_values(values)
    assert any("95%" in e and "FBP" in e for e in errors)


def test_an_unknown_cut_is_named():
    errors = C.validate_values(eleven(**{"40%": 1.0}))
    assert any("40%" in e for e in errors)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, None, "abc", "1.5", True, [1]])
def test_a_non_number_is_refused(value):
    errors = C.validate_values(eleven(IBP=value))
    assert errors and "IBP" in errors[0]


def test_ints_are_numbers():
    assert C.validate_values(eleven(IBP=-3)) == []


@pytest.mark.parametrize("values", [None, [], "IBP", 5])
def test_not_a_mapping_is_refused(values):
    assert C.validate_values(values)


def test_every_problem_is_listed_at_once():
    values = eleven(IBP=math.nan, FBP=99.0)
    del values["5%"]
    assert len(C.validate_values(values)) == 3


# ── what counts as a change ───────────────────────────────────────────────

def test_a_missing_cut_counts_as_zero():
    """The pipeline leaves a cut it has no value for uncorrected, i.e. +0.0."""
    assert C.values_differ({"IBP": 1.0, "5%": 0.0}, {"IBP": 1.0}) is False
    assert C.values_differ({"IBP": 1.0, "5%": 0.5}, {"IBP": 1.0}) is True


def test_legacy_five_vs_hub_eleven_with_the_same_values_is_not_a_change():
    five = {"IBP": -12.08, "10%": -5.25, "50%": -4.06, "90%": -3.46, "FBP": -5.57}
    hub = {cut: five.get(cut, 0.0) for cut in C.D86_CUTS}
    assert C.values_differ(hub, five) is False
    assert C.values_differ(five, hub) is False


def test_a_real_difference_is_a_change():
    assert C.values_differ({"IBP": 1.0}, {"IBP": 1.5}) is True


def test_the_module_is_the_repo_file_not_this_test_folder():
    """This folder is also called `corrections` and has no __init__.py on
    purpose; with one it would be imported in place of corrections.py."""
    from pathlib import Path
    assert Path(C.__file__).name == "corrections.py"
    assert Path(C.__file__).parent == Path(__file__).resolve().parent.parent.parent
