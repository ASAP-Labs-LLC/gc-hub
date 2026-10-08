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
              spike_min_width_min=0.02, spike_report_threshold=500.0,
              spike_max_fwhm_min=0.20, spike_min_dominance=0.6)


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
            "analysis_merge_gap_min": "0.10", "analysis_spike_report_threshold": "",
            "analysis_spike_max_fwhm_min": "0.20", "analysis_spike_min_dominance": "0.6"}

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
                         spike_report_threshold=400.0,   # empty = the moderate in effect
                         spike_max_fwhm_min=0.20, spike_min_dominance=0.6)

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

    def test_broad_humps_are_not_sharp_peaks(self):
        t, v = zeros()
        gauss(t, v, 2.5, 0.4, 800)            # FWHM 0.94 min
        gauss(t, v, 5.0, 0.3, -900)           # a broad negative hump
        gauss(t, v, 7.0, 0.08, 900)           # FWHM 0.19 min: still sharp
        spikes = ac.find_spikes(t, v, PARAMS)
        assert [round(s["t"], 2) for s in spikes] == [7.0]
        assert spikes[0]["fwhm"] == pytest.approx(0.188, abs=0.003)
        assert ac.find_spikes(t, v, dict(PARAMS, spike_max_fwhm_min=0.1)) == []

    def test_a_peak_must_dominate_the_local_peaks(self):
        """|diff at the apex| over the larger of the sample's and the
        standard's local peak heights (each above its own baseline): a
        height mismatch of a big shared peak is not a sharp peak; a peak the
        standard doesn't have is."""
        t, v = zeros()
        gauss(t, v, 1.0, 0.01, 900)           # a 900 difference on a 5000-high shared peak
        gauss(t, v, 3.0, 0.01, 900)           # a 900 peak the standard lacks
        hs, ht = np.zeros_like(t), np.zeros_like(t)
        gauss(t, hs, 1.0, 0.01, 5900)
        gauss(t, ht, 1.0, 0.01, 5000)
        gauss(t, hs, 3.0, 0.01, 900)
        spikes = ac.find_spikes(t, v, PARAMS, heights=(hs, ht))
        assert [round(s["t"], 2) for s in spikes] == [3.0]
        assert spikes[0]["dominance"] == pytest.approx(1.0, abs=0.01)
        loose = ac.find_spikes(t, v, dict(PARAMS, spike_min_dominance=0.15), heights=(hs, ht))
        assert [round(s["t"], 2) for s in loose] == [1.0, 3.0]


class TestAlignment:
    def test_recovers_a_global_retention_shift(self):
        t = axis()
        std = np.full_like(t, 50.0)
        for c in np.arange(0.5, 8.5, 0.37):
            gauss(t, std, c, 0.008, 5000)
        for shift in (0.004, -0.008, 0.013):
            sample = np.interp(t, t + shift, std)            # every peak later by *shift*
            aligned, lag = ac.align_to_standard(t, sample, std)
            assert lag == pytest.approx(-shift, abs=0.0003)
            m = (t > 0.3) & (t < 8.5)
            assert np.max(np.abs(aligned - std)[m]) < 0.1 * np.max(np.abs(sample - std)[m])

    def test_the_search_is_bounded(self):
        t = axis()
        std = np.full_like(t, 50.0)
        for c in np.arange(0.5, 8.5, 0.37):
            gauss(t, std, c, 0.008, 5000)
        _, lag = ac.align_to_standard(t, np.interp(t, t + 0.05, std), std)
        assert abs(lag) <= 0.02 + 1e-9

    def test_no_shared_structure_means_no_shift(self):
        """A peak only the sample has (nothing to match in the standard)
        must not drag the alignment."""
        t = axis()
        std = np.full_like(t, 50.0)
        gauss(t, std, 4.0, 1.0, 1500)
        sample = std.copy()
        gauss(t, sample, 1.2, 0.02, 3000)
        aligned, lag = ac.align_to_standard(t, sample, std)
        assert lag == 0.0 and np.array_equal(aligned, sample)

    def test_flat_or_short_input_is_left_alone(self):
        t = np.arange(20) * 0.001
        y = np.ones(20)
        aligned, lag = ac.align_to_standard(t, y, y)
        assert lag == 0.0 and np.array_equal(aligned, y)


# ── build_deviation_report + render_bullets: the rules, golden text ────
# v7 wording (docs/superpowers/specs/2026-10-07-v7-conclusions.md): a bullet
# leads with the range, then a plain direction (higher / lower / mixed /
# sharp peaks) "than <std> — <severity>", then where in the standard's
# distribution the range lies (light end / main body / heavy end), then the
# numbers. report_layout.finding_rows splits the head at "(Cx–Cy):" and reads
# the severity tag from the first "— <severity>".

class TestRangeBullets:
    def test_moderate(self):
        t, v = zeros()
        items, text = report(box(t, v, 1.0, 1.6, 620))
        assert text == ("• Gas (C5–C11): higher than Diesel #2 — moderate, light end elevated "
                        "(max +620 at 1.00 min; 20% of range beyond the marginal threshold)")
        (it,) = items
        assert it["kind"] == "range" and it["index"] == 0 and it["direction"] == "higher"
        assert it["verdict"] == "higher" and it["position"] == "light"
        assert it["severity"] == "moderate" and it["broad_severity"] == "moderate"
        assert it["max_diff"] == pytest.approx(620)
        assert it["frac_above"] == pytest.approx(0.2, abs=0.002)
        assert it["spike_only"] is False and it["also"] is None and it["spikes"] == []
        assert it["mixed"] is False

    def test_marginal_and_significant(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 150)
        box(t, v, 6.5, 7.0, -2500)
        _, text = report(v)
        assert text == (
            "• Gas (C5–C11): higher than Diesel #2 — marginal, light end elevated "
            "(max +150 at 1.00 min; 20% of range beyond the marginal threshold)\n"
            "• Oil (C20–C44, evaluated to C28): lower than Diesel #2 — significant, heavy end "
            "reduced (max -2500 at 6.50 min; 25% of range beyond the marginal threshold)")

    def test_both_directions_is_mixed(self):
        """A range with a moderate run in the minority direction is mixed:
        the bullet names both, the severity is the larger of the two."""
        t, v = zeros()
        box(t, v, 0.6, 1.6, 200)             # broad, low: area 200
        box(t, v, 2.0, 2.1, -900)            # narrow, tall: area 90
        items, text = report(v)
        assert text == ("• Gas (C5–C11): mixed, higher and lower than Diesel #2 — moderate, "
                        "light end differs in shape (mostly higher, marginal; lower moderate "
                        "at 2.00–2.10 min; max +200 at 0.60 min; 37% of range beyond the "
                        "marginal threshold)")
        (it,) = items
        assert it["verdict"] == "mixed" and it["mixed"] is True
        assert it["direction"] == "higher" and it["broad_severity"] == "moderate"
        also = it["also"]
        assert also["direction"] == "lower" and also["severity"] == "moderate"

    def test_mixed_needs_a_moderate_opposite_run(self):
        t, v = zeros()
        box(t, v, 0.6, 1.6, 200)
        box(t, v, 2.0, 2.1, -400)            # opposite but only marginal
        items, text = report(v)
        assert items[0]["also"] is None and items[0]["verdict"] == "higher"
        assert "mixed" not in text and "lower" not in text

    def test_mixed_shows_at_most_two_merged_opposite_spans(self):
        t, v = zeros()
        box(t, v, 0.6, 1.6, 300)
        for a in (1.80, 1.87, 2.2, 2.6, 3.0):    # 1.80 and 1.87 merge (gap < 0.10)
            box(t, v, a, a + 0.06, -700)
        _, text = report(v)
        assert ("• Gas (C5–C11): mixed, higher and lower than Diesel #2 — moderate, light end "
                "differs in shape (mostly higher, marginal; lower moderate at 1.80–1.93, "
                "2.20–2.26 min, +2 more; max +300 at 0.60 min; ") in text, text

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
                        "• Outside the defined ranges: higher than Diesel #2 — moderate "
                        "at 3.50–3.60 min (max +800 at 3.50 min)")

    def test_spike_only_range(self):
        t, v = zeros()
        s = np.zeros_like(t)
        gauss(t, s, 0.84, 0.01, 900)
        gauss(t, s, 1.40, 0.01, 950)
        items, text = report(v, s)
        assert text == ("• Gas (C5–C11): sharp peaks above Diesel #2 — moderate "
                        "(2 sharp peaks above the standard at 0.84, 1.40 min; "
                        "no broad deviation above the marginal threshold)")
        (it,) = items
        assert it["spike_only"] is True and it["elevated"] is True
        assert it["verdict"] == "higher" and it["broad_severity"] is None
        assert [(round(x["t"], 2), x["sign"]) for x in it["spikes"]] == [(0.84, 1), (1.40, 1)]

    def test_more_than_three_spikes_name_the_largest(self):
        t, v = zeros()
        box(t, v, 2.5, 2.8, 300)
        s = np.zeros_like(t)
        for c, h in ((0.7, 600), (1.0, 1500), (1.3, 700), (1.6, 1200), (1.9, 800)):
            gauss(t, s, c, 0.01, h)
        _, text = report(v, s)
        assert text == ("• Gas (C5–C11): higher than Diesel #2 — moderate, light end elevated "
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
        assert text == ("• Gas (C5–C11): higher than Diesel #2 — moderate, light end elevated "
                        "(max +950 at 1.40 min; 10% of range beyond the marginal threshold; "
                        "2 sharp peaks above, 1 below the standard at 0.84, 1.20, 1.40 min)")

    def test_spikes_outside_ranges_go_to_the_outside_bullet(self):
        t, v = zeros()
        s = np.zeros_like(t)
        gauss(t, s, 4.5, 0.01, -700)
        _, text = report(v, s)
        assert text == ("No deviations above the marginal threshold within the defined ranges.\n"
                        "• Outside the defined ranges: sharp peaks below Diesel #2 — moderate "
                        "(1 sharp peak below the standard at 4.50 min; "
                        "no broad deviation above the marginal threshold)")

    def test_overlapping_ranges_each_report(self):
        t, v = zeros()
        box(t, v, 2.6, 2.9, 620)
        jet = {"label": "Jet", "c_start": 9, "c_end": 16}         # 2.5–5.0
        _, text = report(v, ranges=(GAS, jet))
        assert text == ("• Gas (C5–C11): higher than Diesel #2 — moderate, light end elevated "
                        "(max +620 at 2.60 min; 10% of range beyond the marginal threshold)\n"
                        "• Jet (C9–C16): higher than Diesel #2 — moderate, main body elevated "
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
        assert text.startswith("• Oil (C20–C44, evaluated to C31): higher")

    def test_report_layout_still_splits_every_bullet(self):
        """The PDF's findings rows: the bold head before the first "):" and
        the severity tag from the first "— <severity>"."""
        import report_layout as rl
        t, v = zeros()
        box(t, v, 0.6, 1.6, 200)
        box(t, v, 2.0, 2.1, -900)
        box(t, v, 6.5, 7.0, -2500)
        box(t, v, 5.0, 5.2, 700)
        s = np.zeros_like(t)
        gauss(t, s, 2.6, 0.01, 900)
        _, text = report(v, s)
        rows = rl.finding_rows(text)
        assert [(r["head"], r["severity"]) for r in rows] == [
            ("Gas (C5–C11):", "moderate"),
            ("Oil (C20–C44, evaluated to C28):", "significant"),
            ("Outside the defined ranges:", "moderate")]


class TestPosition:
    """Light end / main body / heavy end come from the range's carbons
    against the standard's own distribution (C at 10% and 90% of its
    area), never from the label."""

    def test_classified_against_the_standard(self):
        diesel = {"c10": 10.0, "c50": 16.0, "c90": 22.0}
        gasoline = {"c10": 5.0, "c50": 7.0, "c90": 10.0}
        assert ac.range_position(5, 11, diesel) == "light"
        assert ac.range_position(9, 16, diesel) == "middle"
        assert ac.range_position(20, 44, diesel) == "heavy"
        assert ac.range_position(5, 11, gasoline) == "middle"
        assert ac.range_position(12, 20, gasoline) == "heavy"
        # without a profile: a typical middle distillate
        assert ac.range_position(5, 11, None) == "light"
        assert ac.range_position(20, 28, None) == "heavy"

    def test_the_label_does_not_decide(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 620)
        gasoline = {"c10": 5.0, "c50": 7.0, "c90": 10.0}
        items = ac.build_deviation_report(t, v, np.zeros_like(t), ranges=[GAS, OIL],
                                          ladder=LADDER, params=PARAMS, std_profile=gasoline)
        assert items[0]["position"] == "middle"
        assert ac.render_bullets(items, "Gasoline").startswith(
            "• Gas (C5–C11): higher than Gasoline — moderate, main body elevated (")
        assert ac.deviation_conclusion(items, [GAS, OIL], "Gasoline") == (
            "Compared to Gasoline, this sample shows moderately elevated intensity in the gas "
            "range (C5–C11): more material in the main body of the distribution than "
            "Gasoline, consistent with a different blend or product. " + INDICATIVE)

    def test_standard_profile_reads_the_standards_area(self):
        t = axis()
        y = np.full_like(t, 40.0)
        gauss(t, y, 0.2, 0.03, 50000)        # the solvent: ignored
        gauss(t, y, 3.0, 0.6, 2000)          # a hump centred at C10
        prof = ac.standard_profile(t, y, LADDER, PARAMS)
        assert prof["c50"] == pytest.approx(10.0, abs=0.3)
        assert prof["c10"] < prof["c50"] < prof["c90"]
        assert ac.standard_profile(t, y, ([], []), PARAMS) is None
        assert ac.standard_profile(t, np.zeros_like(t), LADDER, PARAMS) is None


class TestOutsideAndSpecialCases:
    def test_outside_merges_spans_and_counts_the_rest(self):
        t, v = zeros()
        for a, b, h in ((3.70, 3.80, 300), (3.85, 3.95, 300), (4.2, 4.3, 300),
                        (4.6, 4.7, 300), (5.0, 5.1, 700), (5.4, 5.5, 300)):
            box(t, v, a, b, h)
        items, text = report(v)
        assert text == ("No deviations above the marginal threshold within the defined ranges.\n"
                        "• Outside the defined ranges: higher than Diesel #2 — moderate "
                        "at 3.70–3.95, 4.20–4.30, 4.60–4.70 min, +2 more (max +700 at 5.00 min)")
        assert items[-1]["kind"] == "outside" and len(items[-1]["spans"]) == 5
        assert items[-1]["position"] is None

    def test_zero_ranges_reads_across_the_run(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 620)
        items, text = report(v, ranges=())
        assert text == ("• Across the run: higher than Diesel #2 — moderate "
                        "at 1.00–1.60 min (max +620 at 1.00 min)")
        assert [i["kind"] for i in items] == ["outside"]

    def test_zero_ranges_no_deviation(self):
        _, text = report(ranges=())
        assert text == "No deviations above the marginal threshold."

    def test_no_calibration(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 620)
        items, text = report(v, ladder=([], []))
        assert text == ("• Calibration unavailable — ranges not evaluated: higher than "
                        "Diesel #2 — moderate at 1.00–1.60 min (max +620 at 1.00 min)")
        assert [i["kind"] for i in items] == ["no-calibration"]
        assert ac.deviation_conclusion(items, [GAS, OIL], STD) == (
            "Compared to Diesel #2, the defined ranges could not be evaluated "
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


INDICATIVE = "These findings are indicative only and do not confirm specific substances."
GAS_UP = ("moderately elevated intensity in the gas range (C5–C11): more light-end material "
          "than Diesel #2, consistent with possible light-end (gasoline-range) contamination")
GAS_DOWN = ("lower intensity in the gas range (C5–C11): the light end is reduced compared "
            "with Diesel #2, consistent with a heavier cut or loss of light components "
            "(weathering/evaporation)")
OIL_UP = ("elevated intensity in the oil range (C20–C44): more heavy-end material than "
          "Diesel #2, consistent with heavier components such as lube/oil-range material")


class TestConclusion:
    """The conclusion always says what the bullets say: the same direction
    per range, ordered by severity, sharp peaks and the outside after."""

    def _c(self, v, s=None, ranges=(GAS, OIL)):
        items, _ = report(v, s, ranges=ranges)
        return ac.deviation_conclusion(items, list(ranges), STD)

    def test_all_within(self):
        t, v = zeros()
        assert self._c(v) == (
            "Compared to Diesel #2, this sample shows no significant deviation "
            "in the defined ranges. The chromatographic profile is consistent with the "
            "reference standard.")

    def test_gas_higher(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 620)
        assert self._c(v) == f"Compared to Diesel #2, this sample shows {GAS_UP}. {INDICATIVE}"

    def test_gas_lower(self):
        """Ryan, 2026-10-07: "The gas conclusion always says higher even if
        the deviation is lower." A lower range was not in the conclusion at all."""
        t, v = zeros()
        box(t, v, 1.0, 1.6, -620)
        assert self._c(v) == (f"Compared to Diesel #2, this sample shows moderately "
                              f"{GAS_DOWN}. {INDICATIVE}")

    def test_gas_mixed(self):
        t, v = zeros()
        box(t, v, 0.6, 1.6, 600)             # broad, moderate: higher by area
        box(t, v, 2.0, 2.3, -900)            # moderate the other way
        assert self._c(v) == (
            "Compared to Diesel #2, this sample shows moderate deviations in both directions "
            "in the gas range (C5–C11): the light end differs in shape from Diesel #2 rather "
            "than only in amount (mostly higher; lower at 2.00–2.30 min), consistent with a "
            f"different product or a blend. {INDICATIVE}")

    def test_oil_higher(self):
        t, v = zeros()
        box(t, v, 6.5, 7.0, 620)
        assert self._c(v) == (f"Compared to Diesel #2, this sample shows moderately "
                              f"{OIL_UP}. {INDICATIVE}")

    def test_two_ranges_in_different_directions(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, -2500)           # gas lower, significant
        box(t, v, 6.5, 7.0, 620)             # oil higher, moderate
        assert self._c(v) == (
            f"Compared to Diesel #2, this sample shows significantly {GAS_DOWN}. "
            f"It also shows moderately {OIL_UP}. "
            "Together, a reduced light end and an elevated heavy end indicate a heavier "
            f"overall distribution than Diesel #2. {INDICATIVE}")

    def test_ordered_by_severity(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 150)             # gas, marginal
        box(t, v, 6.5, 7.0, 2500)            # oil, significant
        c = self._c(v)
        assert c.startswith("Compared to Diesel #2, this sample shows significantly elevated "
                            "intensity in the oil range (C20–C44)")
        assert "It also shows slightly elevated intensity in the gas range (C5–C11)" in c
        assert ("Elevated light and heavy ends together are consistent with a blend of a "
                "lighter and a heavier product (possible mixed contamination).") in c

    def test_spikes_only(self):
        t, v = zeros()
        s = np.zeros_like(t)
        gauss(t, s, 0.84, 0.01, 900)
        gauss(t, s, 1.40, 0.01, 950)
        assert self._c(v, s) == (
            "Compared to Diesel #2, this sample shows no broad deviation in the defined ranges, "
            "but isolated sharp peaks above the standard at 0.84, 1.40 min (in the gas range; up "
            "to +950, moderate), "
            f"which may indicate specific added components. {INDICATIVE}")

    def test_lower_with_sharp_peaks_above(self):
        """The case behind Ryan's report: a range lower overall with sharp
        peaks above the standard. v6 called it "elevated intensity … gas
        range contamination"; the broad finding is lower, the peaks are
        named on their own."""
        t, v = zeros()
        box(t, v, 0.6, 1.6, -300)            # broad: lower, marginal
        s = np.zeros_like(t)
        gauss(t, s, 2.0, 0.01, 900)
        gauss(t, s, 2.4, 0.01, 1200)
        gauss(t, s, 3.0, 0.01, -700)
        items, text = report(v, s)
        assert text == ("• Gas (C5–C11): lower than Diesel #2 — moderate, light end reduced "
                        "(max -700 at 3.00 min; 33% of range beyond the marginal threshold; "
                        "2 sharp peaks above, 1 below the standard at 2.00, 2.40, 3.00 min)")
        assert items[0]["verdict"] == "lower" and items[0]["mixed"] is False
        assert items[0]["broad_severity"] == "marginal"
        assert ac.deviation_conclusion(items, [GAS, OIL], STD) == (
            f"Compared to Diesel #2, this sample shows slightly {GAS_DOWN}. It also shows "
            "isolated sharp peaks above the standard at 2.00, 2.40 min (in the gas range; up to "
            "+1200, moderate), which may indicate specific added components, and an isolated "
            "sharp peak below the standard at 3.00 min (in the gas range; -700, moderate), "
            "which may indicate a specific "
            f"component missing from the sample. {INDICATIVE}")

    def test_outside_only(self):
        t, v = zeros()
        box(t, v, 3.7, 3.9, 700)
        assert self._c(v) == (
            "Compared to Diesel #2, this sample shows no significant deviation in the defined "
            "ranges. Outside the defined ranges it is moderately higher than Diesel #2 at "
            f"3.70–3.90 min. {INDICATIVE}")

    def test_outside_after_the_ranges_and_spikes_outside_located(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 620)
        box(t, v, 3.7, 3.9, -700)
        s = np.zeros_like(t)
        gauss(t, s, 4.5, 0.01, 900)
        assert self._c(v, s) == (
            f"Compared to Diesel #2, this sample shows {GAS_UP}. Outside the defined ranges "
            "it is moderately lower than Diesel #2 at 3.70–3.90 min. It also shows an "
            "isolated sharp peak above the standard at 4.50 min (outside the defined ranges; "
            "+900, moderate), "
            f"which may indicate a specific added component. {INDICATIVE}")

    def test_lower_dominant_with_a_higher_run_is_never_just_elevated(self):
        """v6 set "elevated" for a range whose bullet was LOWER with an
        also-higher clause and wrote "elevated intensity … contamination"."""
        t, v = zeros()
        box(t, v, 0.6, 1.6, -900)            # dominant: lower
        box(t, v, 2.0, 2.1, 150)             # marginal positive: not named
        items, text = report(v)
        assert items[0]["verdict"] == "lower" and "higher" not in text
        c = ac.deviation_conclusion(items, [GAS, OIL], STD)
        assert c.startswith(f"Compared to Diesel #2, this sample shows moderately {GAS_DOWN}.")
        assert "elevated" not in c
        box(t, v, 2.5, 2.6, 900)             # now a moderate one: mixed
        items, text = report(v)
        assert items[0]["verdict"] == "mixed" and "mixed, higher and lower" in text
        c = ac.deviation_conclusion(items, [GAS, OIL], STD)
        assert "moderate deviations in both directions in the gas range" in c
        assert "(mostly lower; higher at 2.50–2.60 min)" in c
        assert "elevated intensity" not in c

    def test_same_span_ranges_are_named_together(self):
        t, v = zeros()
        box(t, v, 1.0, 1.6, 620)
        ranges = [GAS, dict(GAS), dict(GAS, label="Gasoline")]
        items, _ = report(v, ranges=ranges)
        assert ac.deviation_conclusion(items, ranges, STD) == (
            "Compared to Diesel #2, this sample shows moderately elevated intensity in the "
            "gas / gasoline range (C5–C11): more light-end material than Diesel #2, "
            f"consistent with possible light-end (gasoline-range) contamination. {INDICATIVE}")

    def test_label_case_is_kept_unless_a_plain_word(self):
        assert ac.range_name("Gas") == "gas"
        assert ac.range_name("Lube oil") == "lube oil"
        assert ac.range_name("Gas range") == "gas"
        assert ac.range_name("GRO") == "GRO"
        assert ac.range_name("C10-C28 DRO") == "C10-C28 DRO"

    def test_zero_ranges(self):
        t, v = zeros()
        assert self._c(v, ranges=()) == (
            "Compared to Diesel #2, this sample shows no significant deviation across the run. "
            "The chromatographic profile is consistent with the reference standard.")
        box(t, v, 1.0, 1.6, 620)
        assert self._c(v, ranges=()) == (
            "Compared to Diesel #2, this sample is moderately higher across the run, at "
            "1.00–1.60 min. No ranges were defined to attribute the deviation. " + INDICATIVE)

    def test_many_ranges_stay_under_the_conclusion_cap(self):
        t, v = zeros()
        ranges = [{"label": f"Range number {i:02d} long label xx", "c_start": 5 + i,
                   "c_end": 6 + i} for i in range(20)]
        for i in range(20):
            a = ac.ladder_carbon_to_time(5 + i, LADDER)
            box(t, v, a, a + 0.1, (-1) ** i * 2500)
        s = np.zeros_like(t)
        for c in np.arange(0.7, 7.5, 0.5):
            gauss(t, s, c, 0.01, 3000)
        items, _ = report(v, s, ranges=ranges)
        c = ac.deviation_conclusion(items, ranges, STD)
        assert len(c) <= 1500, len(c)
        assert "Further deviations:" in c and c.endswith(INDICATIVE)

    def test_the_conclusion_agrees_with_every_bullet(self):
        """Random reports: every range the conclusion names reads in the
        bullet's own direction."""
        rng = random.Random(77)
        t = axis()
        words = {"higher": "elevated intensity in", "lower": "lower intensity in",
                 "mixed": "deviations in both directions in"}
        short = {"higher": "higher in", "lower": "lower in", "mixed": "higher and lower in"}
        for _ in range(80):
            v = np.zeros_like(t)
            s = np.zeros_like(t)
            for _ in range(rng.randint(0, 12)):
                a = rng.uniform(0, 8.8)
                box(t, v, a, a + rng.uniform(0.05, 0.6), rng.choice([-1, 1]) * rng.uniform(50, 3000))
            for _ in range(rng.randint(0, 6)):
                gauss(t, s, rng.uniform(0, 8.8), 0.01, rng.choice([-1, 1]) * rng.uniform(100, 4000))
            items, text = report(v, s)
            c = ac.deviation_conclusion(items, [GAS, OIL], STD)
            for it in items:
                if it["kind"] != "range" or it["spike_only"]:
                    continue
                name = f"the {ac.range_name(it['label'])} range (C{it['c_start']}–C{it['c_end']})"
                assert (f"{words[it['verdict']]} {name}" in c
                        or f"{short[it['verdict']]} {name}" in c), (text, c)
                for other in set(words) - {it["verdict"]}:
                    assert f"{words[other]} {name}" not in c, (text, c)


def test_analyze_report_does_not_call_broad_humps_sharp_peaks():
    t = axis()
    ys = np.full_like(t, 50.0)
    yst = ys.copy()
    gauss(t, ys, 2.5, 0.4, 800)             # broad hump in the sample
    gauss(t, ys, 1.2, 0.01, 3000)           # and one sharp peak
    gauss(t, yst, 4.2, 0.3, 900)            # a broad region lower in the sample
    out = ac.analyze_report(t, ys, yst, ranges=[GAS, OIL], ladder=LADDER,
                            params=dict(PARAMS, sigma=0.0), standard_name=STD)
    assert [round(s["t"], 2) for s in out["spikes"]] == [1.2]
    assert "1 sharp peak above the standard at 1.20 min" in out["text"]
    assert "below the standard" not in out["text"]


def test_analyze_report_runs_both_channels_and_returns_everything():
    t = axis()
    y_std = 50 + gauss(t, np.zeros_like(t), 4.0, 1.0, 1500)
    y_s = y_std.copy()
    gauss(t, y_s, 1.2, 0.02, 3000)            # a gasoline-like spike in Gas
    out = ac.analyze_report(t, y_s, y_std, ranges=[GAS, OIL], ladder=LADDER,
                            params=dict(PARAMS, sigma=0.0), standard_name=STD)
    assert set(out) >= {"diff", "spike_diff", "trend_sample", "trend_std", "windows",
                        "items", "text", "conclusion", "spikes", "alignment_lag_min"}
    assert out["text"].startswith("• Gas (C5–C11): sharp peaks above Diesel #2 — significant "
                                  "(1 sharp peak above the standard at 1.20 min")
    assert [round(s["t"], 2) for s in out["spikes"]] == [1.2]
    assert [w["label"] for w in out["windows"]] == ["Gas", "Oil"]
    assert "sharp peak above the standard at 1.20 min (in the gas range; +" in out["conclusion"]
