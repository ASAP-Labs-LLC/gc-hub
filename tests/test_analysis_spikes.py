"""Spike-channel deviation detection (analysis_core).

A short tall spike (e.g. gasoline adulteration) is invisible to the rolling
low-quantile trend line, so it must be caught on the raw difference.
"""
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analysis_core  # noqa: E402


def _flat_pair(n=6000, dt_min=0.001):
    """Sample and standard: identical flat baselines. t in minutes."""
    t = np.arange(n) * dt_min
    y = np.full(n, 50.0)
    return t, y.copy(), y.copy()


def _add_gaussian(y, t, center_min, width_min, height):
    y += height * np.exp(-0.5 * ((t - center_min) / width_min) ** 2)


CAL_TIMES = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0]
CAL_CARBONS = [5, 6, 7, 8, 10, 12, 16, 20]

THRESH = dict(marginal=100.0, moderate=500.0, significant=2000.0)


def _detect(t, y_sample, y_std, **kw):
    params = dict(
        thresh_marginal=THRESH["marginal"],
        thresh_moderate=THRESH["moderate"],
        thresh_significant=THRESH["significant"],
        quantile=0.20, window=301, sigma=0.0,
        cal_times=CAL_TIMES, cal_carbons=CAL_CARBONS,
    )
    params.update(kw)
    return analysis_core.detect_all_segments(t, y_sample, y_std, **params)


def test_narrow_tall_spike_is_detected():
    t, ys, ystd = _flat_pair()
    # 3000-count spike, ~0.05 min wide at 2.0 min — the adulteration case
    _add_gaussian(ys, t, 2.0, 0.02, 3000.0)
    segments = _detect(t, ys, ystd)
    assert segments, "short tall spike must produce a segment"
    seg = next(s for s in segments if s["start_time"] <= 2.0 <= s["end_time"])
    assert seg["severity"] == "significant"
    assert seg["direction"] == "positive"
    assert seg.get("source") == "spike"


def test_spike_missed_by_trend_channel_alone():
    """Regression guard: proves the old trend-only path misses the spike."""
    t, ys, ystd = _flat_pair()
    _add_gaussian(ys, t, 2.0, 0.02, 3000.0)
    trend_s = analysis_core.compute_trend_line(t, ys, 0.20, 301, sigma=0)
    trend_x = analysis_core.compute_trend_line(t, ystd, 0.20, 301, sigma=0)
    diff = trend_s - trend_x
    trend_only = analysis_core.detect_deviation_segments(
        diff, t, THRESH["marginal"], THRESH["moderate"], THRESH["significant"],
        CAL_TIMES, CAL_CARBONS,
    )
    assert not any(
        s["start_time"] <= 2.0 <= s["end_time"] and s["severity"] == "significant"
        for s in trend_only
    )


def test_single_point_noise_rejected_by_min_width():
    t, ys, ystd = _flat_pair()
    ys[3000] += 5000.0  # one-sample glitch
    segments = _detect(t, ys, ystd, spike_min_width_min=0.02)
    assert not any(s.get("source") == "spike" for s in segments)


def test_negative_spike_direction():
    t, ys, ystd = _flat_pair()
    _add_gaussian(ystd, t, 3.0, 0.02, 900.0)  # standard higher → sample lower
    segments = _detect(t, ys, ystd)
    seg = next(s for s in segments if s["start_time"] <= 3.0 <= s["end_time"])
    assert seg["direction"] == "negative"
    assert seg["severity"] == "moderate"


def test_spike_severity_uses_same_thresholds():
    t, ys, ystd = _flat_pair()
    _add_gaussian(ys, t, 1.0, 0.02, 250.0)     # marginal
    _add_gaussian(ys, t, 4.0, 0.02, 800.0)     # moderate
    segments = _detect(t, ys, ystd)
    sev_at = {}
    for s in segments:
        mid = (s["start_time"] + s["end_time"]) / 2
        sev_at[round(mid)] = s["severity"]
    assert sev_at.get(1) == "marginal"
    assert sev_at.get(4) == "moderate"


def test_trend_and_spike_segments_merge_overlaps():
    t, ys, ystd = _flat_pair()
    # Broad hump (trend channel) with a tall spike on top (spike channel)
    _add_gaussian(ys, t, 2.5, 0.5, 600.0)
    _add_gaussian(ys, t, 2.5, 0.02, 5000.0)
    segments = _detect(t, ys, ystd)
    covering = [s for s in segments if s["start_time"] <= 2.5 <= s["end_time"]]
    assert len(covering) == 1, "overlapping trend+spike segments must merge"
    assert covering[0]["severity"] == "significant"


def test_carbon_range_present_on_spike_segments():
    t, ys, ystd = _flat_pair()
    _add_gaussian(ys, t, 2.0, 0.02, 3000.0)
    segments = _detect(t, ys, ystd)
    seg = next(s for s in segments if s.get("source") == "spike")
    assert seg["carbon_range"].startswith("C")


def test_gasoline_spike_gives_a_gas_bullet_and_the_gas_conclusion():
    """Phase 3: the adulteration fixture (a short tall spike at 2.0 min, C8,
    that the trend channel erases) must still be reported: a spike-only Gas
    bullet, and the conclusion names the sharp peak in the gas range."""
    t, ys, ystd = _flat_pair()
    _add_gaussian(ys, t, 2.0, 0.02, 3000.0)
    ranges = [{"label": "Gas", "c_start": 5, "c_end": 11, "color": "#f0a500"},
              {"label": "Oil", "c_start": 20, "c_end": 44, "color": "#a05014"}]
    params = analysis_core.report_params({"sigma": 0.0}, {
        "analysis_thresh_marginal": THRESH["marginal"],
        "analysis_thresh_moderate": THRESH["moderate"],
        "analysis_thresh_significant": THRESH["significant"]})
    out = analysis_core.analyze_report(t, ys, ystd, ranges=ranges,
                                       ladder=(CAL_TIMES, CAL_CARBONS), params=params,
                                       standard_name="Diesel #2")
    gas = [i for i in out["items"] if i["kind"] == "range" and i["label"] == "Gas"]
    assert len(gas) == 1 and gas[0]["spike_only"] and gas[0]["direction"] == "higher"
    assert out["text"].splitlines()[0] == (
        "• Gas (C5–C11): 1 sharp peak above Diesel #2 — significant")
    # v7: sharp peaks only are named as such, never as a broad elevation
    # (v8: counted in plain words, no time or size)
    assert out["conclusion"] == (
        "Compared to Diesel #2, this sample matches closely in every range. "
        "It has 1 sharp peak above Diesel #2. This could be something added. "
        "This is a screening result only. It does not prove what is in the sample.")
