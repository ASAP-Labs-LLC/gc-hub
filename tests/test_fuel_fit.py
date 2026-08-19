"""Fuel-type best-fit classification (fuel_fit module).

Chromatographic ground rules encoded here:
- amplitude/loading varies run to run → traces are area-normalized, so a
  scaled sample must classify identically ("up/down" leniency);
- retention times are stable on a fixed method → only a small global time
  shift is tolerated ("side to side" strictness);
- a sample matching no single standard but reconstructable as a non-negative
  blend of two → named mix; otherwise plain "Mix".
"""
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fuel_fit  # noqa: E402


def _t(n=4000, dt=0.002):
    return np.arange(n) * dt  # 0 .. 8 min


def _hump(t, center, width, height=1000.0):
    return height * np.exp(-0.5 * ((t - center) / width) ** 2)


def _gasoline(t):
    # light fuel: early narrow envelope
    return _hump(t, 1.2, 0.4) + _hump(t, 1.8, 0.3, 600)


def _diesel(t):
    # heavier fuel: later, broader envelope
    return _hump(t, 4.0, 0.9) + _hump(t, 5.2, 0.6, 700)


def _jet(t):
    # in-between envelope
    return _hump(t, 2.8, 0.5) + _hump(t, 3.3, 0.4, 800)


@pytest.fixture()
def standards():
    t = _t()
    return [
        {"name": "Gasoline", "t": t, "y": _gasoline(t)},
        {"name": "Diesel", "t": t, "y": _diesel(t)},
        {"name": "Jet", "t": t, "y": _jet(t)},
    ]


CFG = {"threshold": 0.93, "shift_tolerance_min": 0.05, "mix_min_frac": 0.10,
       "x_max_min": 8.0}


class TestSingleFuel:
    def test_exact_match(self, standards):
        t = _t()
        res = fuel_fit.classify(t, _diesel(t), standards, **CFG)
        assert res["label"] == "Diesel"
        assert res["best_standard"] == "Diesel"
        assert res["score"] > 0.99

    def test_amplitude_scaling_is_forgiven(self, standards):
        t = _t()
        res = fuel_fit.classify(t, 0.05 * _gasoline(t), standards, **CFG)
        assert res["label"] == "Gasoline"
        assert res["score"] > 0.99

    def test_baseline_offset_and_noise_tolerated(self, standards):
        t = _t()
        rng = np.random.default_rng(42)
        y = 3.0 * _jet(t) + 40.0 + rng.normal(0, 8.0, len(t))
        res = fuel_fit.classify(t, y, standards, **CFG)
        assert res["label"] == "Jet"

    def test_small_time_shift_tolerated(self, standards):
        t = _t()
        y = np.interp(t - 0.03, t, _diesel(t))  # +0.03 min retention drift
        res = fuel_fit.classify(t, y, standards, **CFG)
        assert res["label"] == "Diesel"
        assert res["score"] > 0.98

    def test_large_time_shift_not_forgiven(self, standards):
        t = _t()
        y = np.interp(t - 0.8, t, _diesel(t))  # way beyond retention drift
        res = fuel_fit.classify(t, y, standards, **CFG)
        # must NOT confidently claim Diesel purely via shift search
        assert not (res["label"] == "Diesel" and res["score"] > 0.99)

    def test_ranking_sorted_and_complete(self, standards):
        t = _t()
        res = fuel_fit.classify(t, _diesel(t), standards, **CFG)
        names = [r["name"] for r in res["ranking"]]
        assert set(names) == {"Gasoline", "Diesel", "Jet"}
        scores = [r["score"] for r in res["ranking"]]
        assert scores == sorted(scores, reverse=True)
        assert names[0] == "Diesel"


class TestMixtures:
    def test_two_component_blend_named(self, standards):
        t = _t()
        y = 0.8 * fuel_fit.normalize_trace(t, _diesel(t), 8.0) \
          + 0.2 * fuel_fit.normalize_trace(t, _gasoline(t), 8.0)
        res = fuel_fit.classify(t, y, standards, **CFG)
        assert res["label"].startswith("Mix:")
        assert "Diesel" in res["label"] and "Gasoline" in res["label"]
        assert res["mix"] is not None
        fracs = dict(res["mix"]["components"])
        assert fracs["Diesel"] > fracs["Gasoline"] > 0.05

    def test_unmatchable_sample_is_plain_mix(self, standards):
        t = _t()
        y = _hump(t, 6.8, 0.15, 5000.0)  # far past every standard envelope
        res = fuel_fit.classify(t, y, standards, **CFG)
        assert res["label"] == "Mix"

    def test_trace_minor_component_below_min_frac_is_single_fuel(self, standards):
        t = _t()
        y = 0.98 * fuel_fit.normalize_trace(t, _diesel(t), 8.0) \
          + 0.02 * fuel_fit.normalize_trace(t, _gasoline(t), 8.0)
        res = fuel_fit.classify(t, y, standards, **CFG)
        assert res["label"] == "Diesel"


class TestEdges:
    def test_no_standards_returns_empty(self):
        t = _t()
        res = fuel_fit.classify(t, _diesel(t), [], **CFG)
        assert res["label"] == ""
        assert res["ranking"] == []

    def test_flat_zero_sample_never_crashes(self, standards):
        t = _t()
        res = fuel_fit.classify(t, np.zeros(len(t)), standards, **CFG)
        assert res["label"] in ("", "Mix")


def test_csv_columns_present():
    import distill
    assert "Best Fit" in distill.CSV_HEADER
    assert "Fit Score" in distill.CSV_HEADER
    # Source File stays last-but-N ordering intact: Best Fit before Source File
    assert distill.CSV_HEADER.index("Best Fit") < distill.CSV_HEADER.index("Source File")
