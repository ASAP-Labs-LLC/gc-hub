"""Range-driven deviation bullets (phase 3, analysis_core).

One bullet per deviating range, one catch-all for what lies outside every
range, never more than ranges + 1 lines. The rules are in the phase 3+4 spec
(docs/superpowers/specs/2026-09-29-phase3-4-bullets-comments-design.md);
every template's text is pinned here with golden strings.

Synthetic difference arrays on a 0.001 min axis (the current GC's rate) are
fed straight to ``build_deviation_report``: ``trend`` is the trend difference,
``spike`` the (denoised) raw difference the spike channel reads.
"""
import math
import random
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analysis_core as ac  # noqa: E402

STD = "Diesel #2"
LADDER = ([0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0], [5, 6, 7, 8, 10, 12, 16, 20])
GAS = {"label": "Gas", "c_start": 5, "c_end": 11, "color": "#f0a500"}     # 0.5–3.5 min
OIL = {"label": "Oil", "c_start": 20, "c_end": 44, "color": "#a05014"}    # 6.0–12.0, shown to 8.0
PARAMS = dict(quantile=0.20, window=301, sigma=34.0,
              thresh_marginal=100.0, thresh_moderate=500.0, thresh_significant=2000.0,
              x_max_min=8.0, min_width_min=0.05, merge_gap_min=0.10,
              spike_min_width_min=0.02, spike_report_threshold=500.0)


def axis():
    return np.arange(9000) * 0.001          # 0 … 8.999 min


def box(t, v, a, b, h):
    v[(t >= a) & (t <= b)] += h
    return v


def gauss(t, v, c, w, h):
    v += h * np.exp(-0.5 * ((t - c) / w) ** 2)
    return v


def report(trend=None, spike=None, ranges=(GAS, OIL), ladder=LADDER, **over):
    t = axis()
    trend = np.zeros_like(t) if trend is None else trend
    spike = np.zeros_like(t) if spike is None else spike
    params = dict(PARAMS, **over)
    items = ac.build_deviation_report(t, trend, spike, ranges=list(ranges),
                                      ladder=ladder, params=params)
    return items, ac.render_bullets(items, STD)


def zeros():
    t = axis()
    return t, np.zeros_like(t)


# ── range_windows: the one carbon↔time conversion ──────────────────────

class TestRangeWindows:
    def test_interpolates_inside_the_ladder(self):
        (w,) = ac.range_windows([GAS], LADDER, 0.0, 8.0)
        assert w["t0"] == pytest.approx(0.5) and w["t1"] == pytest.approx(3.5)
        assert w["evaluable"] and not w["clipped"]
        assert (w["index"], w["label"], w["c_start"], w["c_end"]) == (0, "Gas", 5, 11)
        assert w["color"] == "#f0a500"

    def test_extrapolates_linearly_from_the_end_pairs(self):
        assert ac.ladder_carbon_to_time(44, LADDER) == pytest.approx(12.0)   # 6 + 24 × 0.25
        assert ac.ladder_carbon_to_time(4, LADDER) == pytest.approx(0.0)     # 0.5 − 0.5
        assert ac.ladder_carbon_to_time(3, LADDER) == pytest.approx(-0.5)
        assert ac.ladder_time_to_carbon(8.0, LADDER) == pytest.approx(28.0)
        assert ac.ladder_time_to_carbon(0.25, LADDER) == pytest.approx(4.5)
        for c in (3, 5, 9, 11, 17, 44):
            assert ac.ladder_time_to_carbon(ac.ladder_carbon_to_time(c, LADDER), LADDER) \
                == pytest.approx(c)

    def test_clipped_to_the_axis(self):
        (w,) = ac.range_windows([OIL], LADDER, 0.0, 8.0)
        assert w["t0"] == pytest.approx(6.0) and w["t1"] == pytest.approx(8.0)
        assert w["clipped"] and w["evaluable"]
        assert (w["c_eval_start"], w["c_eval_end"]) == (20, 28)

    def test_swapped_bounds(self):
        (w,) = ac.range_windows([dict(GAS, c_start=11, c_end=5)], LADDER, 0.0, 8.0)
        assert (w["c_start"], w["c_end"]) == (5, 11)
        assert w["t0"] == pytest.approx(0.5) and w["t1"] == pytest.approx(3.5)

    def test_width_zero_is_not_evaluable(self):
        light = {"label": "Light", "c_start": 3, "c_end": 4}      # −0.5 … 0.0 → [0, 0]
        heavy = {"label": "Heavy", "c_start": 50, "c_end": 60}    # past the axis
        for w in ac.range_windows([light, heavy], LADDER, 0.0, 8.0):
            assert not w["evaluable"], w
            assert w["t0"] is None and w["t1"] is None

    def test_single_carbon_range_spans_half_a_carbon_each_side(self):
        (w,) = ac.range_windows([{"label": "C7", "c_start": 7, "c_end": 7}], LADDER, 0.0, 8.0)
        assert w["evaluable"] and not w["clipped"]
        assert w["t0"] == pytest.approx(1.25) and w["t1"] == pytest.approx(1.75)
        assert (w["c_start"], w["c_end"]) == (7, 7)

    def test_duplicate_labels_keep_their_index(self):
        ws = ac.range_windows([GAS, dict(GAS, c_end=8)], LADDER, 0.0, 8.0)
        assert [w["index"] for w in ws] == [0, 1]
        assert [w["label"] for w in ws] == ["Gas", "Gas"]

    def test_no_usable_ladder_gives_no_windows(self):
        assert ac.range_windows([GAS], ([], []), 0.0, 8.0) == []
        assert ac.range_windows([GAS], ([0.5], [5]), 0.0, 8.0) == []

    def test_mismatched_ladder_is_still_rejected(self):
        with pytest.raises(ValueError):
            ac.range_windows([GAS], ([0.5, 1.0, 1.5], [5, 6]), 0.0, 8.0)


# ── report_params and spikes ───────────────────────────────────────────

class TestReportParams:
    CONF = {"analysis_quantile": "0.20", "analysis_window": "301", "analysis_sigma": "34.0",
            "analysis_thresh_marginal": "100", "analysis_thresh_moderate": "500",
            "analysis_thresh_significant": "2000", "analysis_x_max_min": "7.0",
            "analysis_spike_min_width_min": "0.02", "analysis_min_width_min": "0.05",
            "analysis_merge_gap_min": "0.10", "analysis_spike_report_threshold": ""}

    def test_operator_params_from_the_body_admin_ones_from_settings(self):
        p = ac.report_params({"quantile": 0.3, "window": 201.0, "sigma": 10,
                              "thresh_marginal": 50, "thresh_moderate": 400,
                              "thresh_significant": 900, "x_max_min": 6.5,
                              "min_width_min": 9, "merge_gap_min": 9,
                              "spike_report_threshold": 9, "spike_min_width_min": 9},
                             self.CONF)
        assert p == dict(quantile=0.3, window=201, sigma=10.0, thresh_marginal=50.0,
                         thresh_moderate=400.0, thresh_significant=900.0, x_max_min=6.5,
                         min_width_min=0.05, merge_gap_min=0.10, spike_min_width_min=0.02,
                         spike_report_threshold=400.0)   # empty = the moderate in effect

    def test_settings_fill_the_gaps(self):
        p = ac.report_params({}, dict(self.CONF, analysis_spike_report_threshold="750"))
        assert p["window"] == 301 and p["x_max_min"] == 7.0
        assert p["spike_report_threshold"] == 750.0

    def test_garbage_is_a_value_error(self):
        with pytest.raises(ValueError):
            ac.report_params({"window": "abc"}, self.CONF)


class TestFindSpikes:
    def test_signed_spikes_over_the_report_threshold(self):
        t, v = zeros()
        gauss(t, v, 1.0, 0.01, 900)
        gauss(t, v, 2.0, 0.01, -1500)
        gauss(t, v, 3.0, 0.01, 300)          # below the report threshold
        v[4000] += 5000                      # one-point glitch: too narrow
        gauss(t, v, 8.5, 0.01, 900)          # past x_max
        spikes = ac.find_spikes(t, v, PARAMS)
        assert [(round(s["t"], 3), s["sign"]) for s in spikes] == [(1.0, 1), (2.0, -1)]
        assert spikes[1]["value"] == pytest.approx(-1500, rel=1e-3)


# ── build_deviation_report + render_bullets: the rules, golden text ────

class TestRangeBullets:
    def test_moderate(self):
        t, v = zeros()
        items, text = report(box(t, v, 1.0, 1.6, 620))
        assert text == ("• Gas (C5–C11): HIGHER than Diesel #2 — moderate "
                        "(max +620 at 1.00 min; 20% of range beyond the marginal threshold)")
        (it,) = items
        assert it["kind"] == "range" and it["index"] == 0 and it["direction"] == "higher"
        assert it["severity"] == "moderate" and it["max_diff"] == pytest.approx(620)
        assert it["frac_above"] == pytest.approx(0.2, abs=0.002)
        assert it["spike_only"] is False and it["also"] is None and it["spikes"] == []

    def test_marginal_and_significant(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 150)
        box(t, v, 6.5, 7.0, -2500)
        _, text = report(v)
        assert text == (
            "• Gas (C5–C11): HIGHER than Diesel #2 — marginal "
            "(max +150 at 1.00 min; 20% of range beyond the marginal threshold)\n"
            "• Oil (C20–C44, evaluated to C28): LOWER than Diesel #2 — significant "
            "(max -2500 at 6.50 min; 25% of range beyond the marginal threshold)")

    def test_direction_by_area_with_an_also_clause(self):
        t, v = zeros()
        box(t, v, 0.6, 1.6, 200)             # broad, low: area 200
        box(t, v, 2.0, 2.1, -900)            # narrow, tall: area 90
        items, text = report(v)
        assert text == ("• Gas (C5–C11): HIGHER than Diesel #2 — marginal; "
                        "also lower moderate 2.00–2.10 min "
                        "(max +200 at 0.60 min; 37% of range beyond the marginal threshold)")
        also = items[0]["also"]
        assert also["direction"] == "lower" and also["severity"] == "moderate"

    def test_also_clause_needs_moderate(self):
        t, v = zeros()
        box(t, v, 0.6, 1.6, 200)
        box(t, v, 2.0, 2.1, -400)            # opposite but only marginal
        items, text = report(v)
        assert items[0]["also"] is None and "also" not in text

    def test_also_clause_shows_at_most_two_merged_spans(self):
        t, v = zeros()
        box(t, v, 0.6, 1.6, 300)
        for a in (1.80, 1.87, 2.2, 2.6, 3.0):    # 1.80 and 1.87 merge (gap < 0.10)
            box(t, v, a, a + 0.06, -700)
        _, text = report(v)
        assert ("• Gas (C5–C11): HIGHER than Diesel #2 — marginal; also lower moderate "
                "1.80–1.93, 2.20–2.26 min, +2 more (max +300 at 0.60 min; ") in text, text

    def test_min_width_rejects_a_narrow_trend_run(self):
        t, v = zeros()
        box(t, v, 1.0, 1.03, 800)
        items, text = report(v)
        assert text == "No deviations above the marginal threshold."
        assert [i["kind"] for i in items] == ["none"]

    def test_min_width_applies_after_clipping_at_a_window_edge(self):
        t, v = zeros()
        box(t, v, 3.47, 3.60, 800)           # 0.03 min inside Gas, 0.10 outside
        _, text = report(v)
        assert text == ("No deviations above the marginal threshold within the defined ranges.\n"
                        "• Outside the defined ranges: HIGHER than Diesel #2 — moderate "
                        "at 3.50–3.60 min (max +800 at 3.50 min)")

    def test_spike_only_range(self):
        t, v = zeros()
        s = np.zeros_like(t)
        gauss(t, s, 0.84, 0.01, 900)
        gauss(t, s, 1.40, 0.01, 950)
        items, text = report(v, s)
        assert text == ("• Gas (C5–C11): HIGHER than Diesel #2 — moderate, sharp peaks only "
                        "(2 sharp peaks above the standard at 0.84, 1.40 min; "
                        "no broad deviation above the marginal threshold)")
        (it,) = items
        assert it["spike_only"] is True and it["elevated"] is True
        assert [(round(x["t"], 2), x["sign"]) for x in it["spikes"]] == [(0.84, 1), (1.40, 1)]

    def test_more_than_three_spikes_name_the_largest(self):
        t, v = zeros()
        box(t, v, 2.5, 2.8, 300)
        s = np.zeros_like(t)
        for c, h in ((0.7, 600), (1.0, 1500), (1.3, 700), (1.6, 1200), (1.9, 800)):
            gauss(t, s, c, 0.01, h)
        _, text = report(v, s)
        assert text == ("• Gas (C5–C11): HIGHER than Diesel #2 — moderate "
                        "(max +1500 at 1.00 min; 10% of range beyond the marginal threshold; "
                        "5 sharp peaks above the standard incl. 1.00, 1.60, 1.90 min)")

    def test_spikes_below_are_signed(self):
        t, v = zeros()
        s = np.zeros_like(t)
        gauss(t, s, 0.84, 0.01, 900)
        gauss(t, s, 1.40, 0.01, 950)
        gauss(t, s, 1.20, 0.01, -3000)
        box(t, v, 2.5, 2.8, 300)
        _, text = report(v, s)
        assert text == ("• Gas (C5–C11): HIGHER than Diesel #2 — moderate "
                        "(max +950 at 1.40 min; 10% of range beyond the marginal threshold; "
                        "2 sharp peaks above, 1 below the standard at 0.84, 1.20, 1.40 min)")

    def test_spikes_outside_ranges_go_to_the_outside_bullet(self):
        t, v = zeros()
        s = np.zeros_like(t)
        gauss(t, s, 4.5, 0.01, -700)
        _, text = report(v, s)
        assert text == ("No deviations above the marginal threshold within the defined ranges.\n"
                        "• Outside the defined ranges: LOWER than Diesel #2 — moderate, "
                        "sharp peaks only (1 sharp peak below the standard at 4.50 min; "
                        "no broad deviation above the marginal threshold)")

    def test_overlapping_ranges_each_report(self):
        t, v = zeros()
        box(t, v, 2.6, 2.9, 620)
        jet = {"label": "Jet", "c_start": 9, "c_end": 16}         # 2.5–5.0
        _, text = report(v, ranges=(GAS, jet))
        assert text == ("• Gas (C5–C11): HIGHER than Diesel #2 — moderate "
                        "(max +620 at 2.60 min; 10% of range beyond the marginal threshold)\n"
                        "• Jet (C9–C16): HIGHER than Diesel #2 — moderate "
                        "(max +620 at 2.60 min; 12% of range beyond the marginal threshold)")

    def test_duplicate_labels_each_get_a_bullet(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 620)
        items, _ = report(v, ranges=(GAS, dict(GAS, c_end=8)))
        assert [(i["kind"], i["index"], i["label"]) for i in items] == \
            [("range", 0, "Gas"), ("range", 1, "Gas")]

    def test_not_evaluated_range(self):
        t, v = zeros()
        light = {"label": "Light", "c_start": 3, "c_end": 4}
        items, text = report(v, ranges=(light, GAS))
        assert text == ("• Light (C3–C4): not evaluated — outside the evaluated window "
                        "(calibration, run end or x-axis limit)\n"
                        "No deviations above the marginal threshold.")
        assert items[0]["kind"] == "not-evaluated" and items[0]["index"] == 0

    def test_evaluation_stops_at_x_max(self):
        t, v = zeros()
        box(t, v, 8.2, 8.5, 900)
        s = np.zeros_like(t)
        gauss(t, s, 8.6, 0.01, 900)
        _, text = report(v, s)
        assert text == "No deviations above the marginal threshold."
        _, text = report(v, s, x_max_min=8.9)
        assert text.startswith("• Oil (C20–C44, evaluated to C31): HIGHER")


class TestOutsideAndSpecialCases:
    def test_outside_merges_spans_and_counts_the_rest(self):
        t, v = zeros()
        for a, b, h in ((3.70, 3.80, 300), (3.85, 3.95, 300), (4.2, 4.3, 300),
                        (4.6, 4.7, 300), (5.0, 5.1, 700), (5.4, 5.5, 300)):
            box(t, v, a, b, h)
        items, text = report(v)
        assert text == ("No deviations above the marginal threshold within the defined ranges.\n"
                        "• Outside the defined ranges: HIGHER than Diesel #2 — moderate "
                        "at 3.70–3.95, 4.20–4.30, 4.60–4.70 min, +2 more (max +700 at 5.00 min)")
        assert items[-1]["kind"] == "outside" and len(items[-1]["spans"]) == 5

    def test_zero_ranges_reads_across_the_run(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 620)
        items, text = report(v, ranges=())
        assert text == ("• Across the run: HIGHER than Diesel #2 — moderate "
                        "at 1.00–1.60 min (max +620 at 1.00 min)")
        assert [i["kind"] for i in items] == ["outside"]

    def test_zero_ranges_no_deviation(self):
        _, text = report(ranges=())
        assert text == "No deviations above the marginal threshold."

    def test_no_calibration(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 620)
        items, text = report(v, ladder=([], []))
        assert text == ("• Calibration unavailable — ranges not evaluated: HIGHER than "
                        "Diesel #2 — moderate at 1.00–1.60 min (max +620 at 1.00 min)")
        assert [i["kind"] for i in items] == ["no-calibration"]
        assert ac.deviation_conclusion(items, [GAS, OIL], STD) == (
            "Conclusion: Compared to Diesel #2, the defined ranges could not be evaluated "
            "because no usable calibration was available for this sample.")

    def test_no_calibration_no_deviation(self):
        _, text = report(ladder=([0.5], [5]))
        assert text == ("• Calibration unavailable — ranges not evaluated: "
                        "no deviations above the marginal threshold.")

    def test_bound_never_exceeds_ranges_plus_one(self):
        rng = random.Random(1234)
        t = axis()
        pool = [GAS, OIL, {"label": "Jet", "c_start": 9, "c_end": 16},
                {"label": "Light", "c_start": 3, "c_end": 4},
                {"label": "Mid", "c_start": 12, "c_end": 20}]
        for _ in range(60):
            v = np.zeros_like(t)
            s = np.zeros_like(t)
            for _ in range(rng.randint(0, 25)):
                a = rng.uniform(0, 8.8)
                box(t, v, a, a + rng.uniform(0.001, 0.4), rng.choice([-1, 1]) * rng.uniform(50, 3000))
            for _ in range(rng.randint(0, 15)):
                gauss(t, s, rng.uniform(0, 8.8), 0.01, rng.choice([-1, 1]) * rng.uniform(100, 4000))
            ranges = rng.sample(pool, rng.randint(0, len(pool)))
            ladder = rng.choice([LADDER, LADDER, ([], [])])
            items, text = report(v, s, ranges=ranges, ladder=ladder)
            lines = text.splitlines()
            assert len(lines) <= max(1, len(ranges) + 1), (ranges, text)
            assert len(items) == len(lines)


class TestConclusion:
    def _c(self, v, s=None, ranges=(GAS, OIL)):
        items, _ = report(v, s, ranges=ranges)
        return ac.deviation_conclusion(items, list(ranges), STD)

    def test_none(self):
        t, v = zeros()
        assert self._c(v) == (
            "Conclusion: Compared to Diesel #2, this sample shows no significant deviation "
            "in the defined ranges. The chromatographic profile is consistent with the "
            "reference standard.")

    def test_one_range(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 620)
        assert self._c(v) == (
            "Conclusion: Compared to Diesel #2, this sample shows elevated intensity in the "
            "gas range (C5–C11), consistent with possible gas range contamination. "
            "These findings are indicative only and do not confirm specific substances.")

    def test_several_ranges(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 620)
        box(t, v, 6.5, 7.0, 300)
        assert self._c(v) == (
            "Conclusion: Compared to Diesel #2, this sample shows elevated intensity in "
            "the gas range (C5–C11) and the oil range (C20–C44). This pattern is consistent "
            "with mixed contamination. These findings are indicative only and do not "
            "confirm specific substances.")

    def test_lower_only_is_not_elevated(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, -620)
        assert "no significant deviation" in self._c(v)

    def test_elevated_only_when_the_bullet_reports_something_higher(self):
        t, v = zeros()
        box(t, v, 0.6, 1.6, -900)            # dominant: lower
        box(t, v, 2.0, 2.1, 150)             # a marginal positive run the bullet doesn't name
        items, text = report(v)
        assert "LOWER" in text and "higher" not in text.lower().replace("lower", "")
        assert items[0]["elevated"] is False
        assert "no significant deviation" in ac.deviation_conclusion(items, [GAS, OIL], STD)
        box(t, v, 2.5, 2.6, 900)             # now a moderate one: the also-clause names it
        items, text = report(v)
        assert "also higher moderate" in text and items[0]["elevated"] is True
        assert "elevated intensity in the gas range" in \
            ac.deviation_conclusion(items, [GAS, OIL], STD)

    def test_mixed_lower_overall_with_sharp_peaks_above(self):
        t, v = zeros()
        box(t, v, 0.6, 1.6, -300)            # broad: lower
        s = np.zeros_like(t)
        gauss(t, s, 2.0, 0.01, 900)
        gauss(t, s, 2.4, 0.01, 1200)
        gauss(t, s, 3.0, 0.01, -700)
        items, text = report(v, s)
        assert text == ("• Gas (C5–C11): mixed: LOWER than Diesel #2 overall, with 2 sharp "
                        "peaks above the standard at 2.00, 2.40 min — moderate "
                        "(max -700 at 3.00 min; 33% of range beyond the marginal threshold; "
                        "1 sharp peak below the standard at 3.00 min)")
        assert items[0]["mixed"] is True and items[0]["elevated"] is True
        assert "elevated intensity in the gas range" in \
            ac.deviation_conclusion(items, [GAS, OIL], STD)

    def test_duplicate_ranges_are_named_once(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 620)
        ranges = [GAS, dict(GAS), dict(GAS, label="Gasoline")]
        items, _ = report(v, ranges=ranges)
        assert ac.deviation_conclusion(items, ranges, STD) == (
            "Conclusion: Compared to Diesel #2, this sample shows elevated intensity in "
            "the gas range (C5–C11) and the gasoline range (C5–C11). This pattern is "
            "consistent with mixed contamination. These findings are indicative only and "
            "do not confirm specific substances.")

    def test_zero_ranges(self):
        t, v = zeros()
        assert "consistent with the reference standard" in self._c(v, ranges=())
        box(t, v, 1.0, 1.6, 620)
        assert self._c(v, ranges=()) == (
            "Conclusion: Compared to Diesel #2, this sample deviates from the reference "
            "standard; no ranges were defined to attribute the deviation. These findings "
            "are indicative only and do not confirm specific substances.")


def test_analyze_report_runs_both_channels_and_returns_everything():
    t = axis()
    y_std = 50 + gauss(t, np.zeros_like(t), 4.0, 1.0, 1500)
    y_s = y_std.copy()
    gauss(t, y_s, 1.2, 0.02, 3000)            # a gasoline-like spike in Gas
    out = ac.analyze_report(t, y_s, y_std, ranges=[GAS, OIL], ladder=LADDER,
                            params=dict(PARAMS, sigma=0.0), standard_name=STD)
    assert set(out) >= {"diff", "spike_diff", "trend_sample", "trend_std", "windows",
                        "items", "text", "conclusion", "spikes"}
    assert out["text"].startswith("• Gas (C5–C11): HIGHER than Diesel #2 — significant, "
                                  "sharp peaks only (1 sharp peak above the standard at 1.20 min")
    assert [round(s["t"], 2) for s in out["spikes"]] == [1.2]
    assert [w["label"] for w in out["windows"]] == ["Gas", "Oil"]
    assert "gas range" in out["conclusion"]
