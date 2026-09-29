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
