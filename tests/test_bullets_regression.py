"""Regression: a diesel sample vs its standard gave dozens of bullets.

``tests/fixtures/diesel_pair.npz`` (built by ``make_diesel_pair.py``; see its
docstring: realistic synthetic data on the real calibration CDF's time axis
and ladder, no customer identifiers) is a same-product pair with ordinary
batch-to-batch peak-height and retention differences. The pre-phase-3
per-excursion text (``generate_conclusion``) wrote one bullet per excursion;
the range-driven report never writes more than ranges + 1 lines.
"""
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import analysis_core as ac  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "diesel_pair.npz"
DEFAULT_CONF = {}      # the shipped defaults (settings.DEFAULTS values)


def _load():
    d = np.load(FIXTURE)
    ladder = ([float(x) for x in d["ladder_t"]], [int(x) for x in d["ladder_c"]])
    return (d["t"].astype(float), d["sample"].astype(float), d["standard"].astype(float),
            ladder)


def test_fixture_is_small_and_identifier_free():
    assert FIXTURE.stat().st_size < 250_000
    d = np.load(FIXTURE)
    assert sorted(d.files) == ["ladder_c", "ladder_t", "sample", "standard", "t"]


def test_old_per_excursion_text_gave_dozens_of_bullets():
    t, ys, ystd, (lt, lc) = _load()
    pair = ac.analyze_pair(t, ys, ystd, thresh_marginal=100, thresh_moderate=500,
                           thresh_significant=2000, quantile=0.20, window=301, sigma=34.0,
                           cal_times=lt, cal_carbons=lc)
    _, bullets = ac.generate_conclusion(pair["segments"], ac.resolve_report_ranges(None, {}),
                                        lt, lc, "Diesel")
    assert len(bullets.splitlines()) >= 24


@pytest.mark.parametrize("ranges", [
    None,                                                       # legacy Gas/Oil
    [{"label": "Gas", "c_start": 5, "c_end": 11},
     {"label": "Kero", "c_start": 9, "c_end": 16},
     {"label": "Diesel", "c_start": 10, "c_end": 25},
     {"label": "Oil", "c_start": 20, "c_end": 44}],
    [],                                                         # explicit: no ranges
])
def test_range_driven_report_is_bounded(ranges):
    t, ys, ystd, ladder = _load()
    resolved = ac.resolve_report_ranges(ranges, {})
    if ranges == []:
        assert resolved == []
    out = ac.analyze_report(t, ys, ystd, ranges=resolved, ladder=ladder,
                            params=ac.report_params({}, DEFAULT_CONF), standard_name="Diesel")
    lines = out["text"].splitlines()
    assert 1 <= len(lines) <= max(1, len(resolved) + 1), out["text"]
    assert len(out["items"]) == len(lines)


# ── Phase 3 review: sharp peaks must be real ─────────────────────────────
# Same-product batches differ by retention drift and peak heights; neither
# is a "sharp peak above the standard". Gasoline in diesel is.

sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
import make_diesel_pair as mk  # noqa: E402

CONSISTENT = ("Compared to Diesel, this sample shows no significant deviation in "
              "the defined ranges. The chromatographic profile is consistent with the "
              "reference standard.")


def _report(sample, standard, t=None, ranges=None):
    tt, _, _, ladder = _load()
    t = tt if t is None else t
    return ac.analyze_report(t, sample, standard, ranges=ac.resolve_report_ranges(ranges, {}),
                             ladder=ladder, params=ac.report_params({}, DEFAULT_CONF),
                             standard_name="Diesel")


def _elevated(out):
    return [i["label"] for i in out["items"] if i["kind"] == "range" and i["elevated"]]


def test_the_same_product_pair_reports_no_deviation():
    t, ys, ystd, _ = _load()
    out = _report(ys, ystd, t)
    assert out["text"] == "No deviations above the marginal threshold.", out["text"]
    assert out["conclusion"] == CONSISTENT
    assert out["spikes"] == []


@pytest.mark.parametrize("kw", [dict(shift=0.004), dict(shift=0.008),
                                dict(shift=0.008, seed=21), dict(jitter=0.5, seed=31)])
def test_other_batches_are_clean(kw):
    t = mk.axis()
    seed = kw.pop("seed", 11)
    out = _report(mk.batch(t, seed, **kw), mk.standard(t), t)
    assert out["spikes"] == [], out["text"]
    assert _elevated(out) == [], out["text"]
    if out["text"] == "No deviations above the marginal threshold.":
        assert out["conclusion"] == CONSISTENT
    else:
        # v7: the conclusion says what the bullets say. One batch has a
        # marginal run just outside the ranges (+113, 1.88–1.95 min): the
        # bullets showed it in v6 too, the conclusion now names it as well.
        assert [i["kind"] for i in out["items"]] == ["none", "outside"], out["text"]
        assert out["conclusion"].startswith(
            "Compared to Diesel, this sample shows no significant deviation in the defined "
            "ranges. Outside the defined ranges it is slightly "), out["conclusion"]


@pytest.mark.parametrize("frac", [0.01, 0.02, 0.05])
def test_gasoline_in_diesel_flags_gas_only(frac):
    t = mk.axis()
    out = _report(mk.with_gasoline(t, frac), mk.standard(t), t)
    assert _elevated(out) == ["Gas"], out["text"]
    assert out["conclusion"].startswith(
        "Compared to Diesel, this sample shows slightly elevated intensity in the gas range "
        "(C5–C11): more light-end material than Diesel, consistent with possible light-end "
        "(gasoline-range) contamination."), out["conclusion"]
    assert "sharp peak" in out["conclusion"] and "above the standard" in out["conclusion"]
    assert "lower intensity in the gas range" not in out["conclusion"]
