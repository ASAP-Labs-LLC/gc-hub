"""calibration_ladder: (time, carbon) pairs for Analysis / reports.

Regression for the 2026-09-16 crash: auto-detect found 24 peaks on the new
350 °C calibration (20 alkanes + CS2 + 3 impurities) and they were paired by
position with the fixed 20-entry N_ALKANE_CARBON list -> np.interp raised
'fp and xp are not of the same length' and every Analysis/Export 500'd.

Also covers the follow-up hardening after code review: an empty/short ladder
must not reach np.interp either (generate_conclusion), a saved ladder whose
carbons are not increasing with retention time must not silently mislabel
carbon ranges, and GC_CAL_CDF must be the single source of truth for "the"
calibration file everywhere (labels and math alike).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import analysis_core  # noqa: E402
import distill  # noqa: E402


def _assignments(cal_path, entries):
    return json.dumps({distill._cal_key(cal_path): entries})


def _real_cdf() -> Path:
    """A real (empty) file so Path.is_file() checks pass without mocking
    Path itself — mocking Path.is_file globally is fragile (it affects every
    Path in the process, per test_calibration.py's existing convention)."""
    fd, p = tempfile.mkstemp(suffix=".CDF")
    os.close(fd)
    return Path(p)


class CalibrationLadderTests(unittest.TestCase):
    def setUp(self):
        self.cal = Path("/tmp/fake_cal.CDF")
        # 24 peaks: CS2 solvent, the 20 reference n-alkanes (non-consecutive
        # carbon numbers, matching the real N_ALKANE_CARBON table — a saved
        # assignment can never carry a carbon outside it, since
        # api_calibration_save validates against the same list), and 3
        # impurities, as in 002F0301.CDF.
        self.entries = [{"rt": 0.30, "ignore": True}]  # CS2
        self.entries += [{"rt": 0.50 + 0.25 * i, "carbon": c}
                          for i, c in enumerate(distill.N_ALKANE_CARBON)]
        self.entries += [{"rt": 1.13, "ignore": True},
                         {"rt": 2.61, "ignore": True},
                         {"rt": 4.07, "ignore": True}]

    def test_uses_saved_assignments_and_drops_ignored_peaks(self):
        conf = {"calibration_cdf": str(self.cal),
                "calibration_assignments": _assignments(self.cal, self.entries)}
        times, carbons = distill.calibration_ladder(conf)
        self.assertEqual(len(times), len(carbons))
        self.assertEqual(carbons, list(distill.N_ALKANE_CARBON))
        self.assertAlmostEqual(times[0], 0.50)
        self.assertEqual(times, sorted(times))

    def test_ladder_feeds_segment_carbon_range_without_error(self):
        conf = {"calibration_cdf": str(self.cal),
                "calibration_assignments": _assignments(self.cal, self.entries)}
        times, carbons = distill.calibration_ladder(conf)
        # 0.50 min is C5 exactly, 0.75 min is C6: the solvent is NOT C5
        self.assertEqual(analysis_core.segment_carbon_range(0.50, 0.75, times, carbons), "C5-C6")

    def test_falls_back_to_auto_detect_truncated_to_known_carbons(self):
        cal = _real_cdf()
        try:
            conf = {"calibration_cdf": str(cal), "calibration_assignments": ""}
            peaks = [0.1 * i for i in range(1, 25)]  # 24 detected peaks
            with mock.patch.object(distill, "calibration_peak_times", return_value=peaks):
                times, carbons = distill.calibration_ladder(conf)
        finally:
            os.unlink(cal)
        self.assertEqual(len(times), len(carbons))
        self.assertEqual(len(carbons), len(distill.N_ALKANE_CARBON))

    def test_no_calibration_configured_returns_empty(self):
        self.assertEqual(distill.calibration_ladder({"calibration_cdf": ""}), ([], []))

    def test_unreadable_calibration_returns_empty_not_raise(self):
        cal = _real_cdf()
        try:
            conf = {"calibration_cdf": str(cal), "calibration_assignments": ""}
            with mock.patch.object(distill, "calibration_peak_times", side_effect=OSError("bad")):
                self.assertEqual(distill.calibration_ladder(conf), ([], []))
        finally:
            os.unlink(cal)


class CalibrationLadderMonotonicityTests(unittest.TestCase):
    """Saved assignments must yield strictly-increasing carbons after sorting
    by retention time, or np.interp(carbon, cn, ct) is silently wrong."""

    def setUp(self):
        self.cal = Path("/tmp/fake_cal_monotonic.CDF")

    def test_out_of_order_assignment_is_dropped_with_warning(self):
        entries = [
            {"rt": 0.50, "carbon": 5},
            {"rt": 0.75, "carbon": 10},
            {"rt": 1.00, "carbon": 7},   # <= last kept (10) -> dropped
            {"rt": 1.25, "carbon": 8},   # <= last kept (10) -> dropped
        ]
        conf = {"calibration_cdf": str(self.cal),
                "calibration_assignments": _assignments(self.cal, entries)}
        with self.assertLogs(distill.LOGGER, level="WARNING") as cm:
            times, carbons = distill.calibration_ladder(conf)
        self.assertEqual(times, [0.50, 0.75])
        self.assertEqual(carbons, [5, 10])
        self.assertTrue(any("C7" in m for m in cm.output), cm.output)

    def test_too_few_survive_monotonicity_falls_back(self):
        # Only one pair survives the filter -> not a usable ladder -> falls
        # through to auto-detect, which returns empty for an unreadable file.
        entries = [{"rt": 0.50, "carbon": 10}, {"rt": 1.00, "carbon": 5}]
        conf = {"calibration_cdf": str(self.cal),
                "calibration_assignments": _assignments(self.cal, entries)}
        with self.assertLogs(distill.LOGGER, level="WARNING"):
            result = distill.calibration_ladder(conf)
        self.assertEqual(result, ([], []))

    def test_repeated_carbon_is_deduped_not_treated_as_out_of_order(self):
        # An exact carbon repeat is de-duplicated upstream by
        # distill._assignment_pairs (keeping the first occurrence) before the
        # monotonicity filter ever runs, so this is a silent cleanup, not a
        # warned-about ordering violation.
        entries = [
            {"rt": 0.50, "carbon": 5},
            {"rt": 0.75, "carbon": 5},  # duplicate carbon - deduped, no warning
            {"rt": 1.00, "carbon": 6},
        ]
        conf = {"calibration_cdf": str(self.cal),
                "calibration_assignments": _assignments(self.cal, entries)}
        times, carbons = distill.calibration_ladder(conf)
        self.assertEqual(carbons, [5, 6])
        self.assertEqual(times, [0.50, 1.00])


class CalibrationLadderFallbackWarningTests(unittest.TestCase):
    """Auto-detect fallback must warn when the detected peak count doesn't
    match the reference ladder — labels may then be off by a shift."""

    def test_warns_when_peak_count_mismatches_reference_ladder(self):
        cal = _real_cdf()
        try:
            conf = {"calibration_cdf": str(cal), "calibration_assignments": ""}
            peaks = [0.1 * i for i in range(1, 25)]  # 24 != 20 reference alkanes
            with mock.patch.object(distill, "calibration_peak_times", return_value=peaks):
                with self.assertLogs(distill.LOGGER, level="WARNING") as cm:
                    times, carbons = distill.calibration_ladder(conf)
        finally:
            os.unlink(cal)
        self.assertEqual(len(carbons), len(distill.N_ALKANE_CARBON))
        self.assertTrue(
            any("Calibration page" in m or "mislabel" in m.lower() for m in cm.output),
            cm.output,
        )

    def test_no_warning_when_peak_count_matches_reference_ladder(self):
        cal = _real_cdf()
        try:
            conf = {"calibration_cdf": str(cal), "calibration_assignments": ""}
            n = len(distill.N_ALKANE_CARBON)
            peaks = [0.1 * i for i in range(1, n + 1)]
            with mock.patch.object(distill, "calibration_peak_times", return_value=peaks):
                with self.assertNoLogs(distill.LOGGER, level="WARNING"):
                    times, carbons = distill.calibration_ladder(conf)
        finally:
            os.unlink(cal)
        self.assertEqual(len(carbons), n)


class CalibrationLadderNeverShortTests(unittest.TestCase):
    """calibration_ladder must never hand out a 0<n<2-point ladder from
    EITHER branch — a 1-point 'ladder' still crashes every consumer that
    needs to interpolate against it (analyze_pair/generate_conclusion), just
    with a smaller reproduction case than the 24-vs-20 mismatch."""

    def test_single_auto_detected_peak_returns_empty_not_one_point(self):
        cal = _real_cdf()
        try:
            conf = {"calibration_cdf": str(cal), "calibration_assignments": ""}
            with mock.patch.object(distill, "calibration_peak_times", return_value=[0.5]):
                with self.assertLogs(distill.LOGGER, level="WARNING") as cm:
                    result = distill.calibration_ladder(conf)
        finally:
            os.unlink(cal)
        self.assertEqual(result, ([], []))
        self.assertTrue(any("at least 2" in m for m in cm.output), cm.output)

    def test_zero_auto_detected_peaks_still_returns_empty(self):
        cal = _real_cdf()
        try:
            conf = {"calibration_cdf": str(cal), "calibration_assignments": ""}
            with mock.patch.object(distill, "calibration_peak_times", return_value=[]):
                result = distill.calibration_ladder(conf)
        finally:
            os.unlink(cal)
        self.assertEqual(result, ([], []))


class CalibrationLadderEnvOverrideTests(unittest.TestCase):
    """GC_CAL_CDF must win over the saved setting for calibration_ladder too,
    the same way it already does for the distillation math."""

    def test_env_override_selects_the_assignments_used(self):
        env_cal = Path("/tmp/env_cal.CDF")
        settings_cal = Path("/tmp/settings_cal.CDF")
        entries = [{"rt": 1.0, "carbon": 5}, {"rt": 2.0, "carbon": 6}]
        conf = {
            "calibration_cdf": str(settings_cal),
            "calibration_assignments": _assignments(env_cal, entries),
        }
        with mock.patch.dict(os.environ, {"GC_CAL_CDF": str(env_cal)}):
            times, carbons = distill.calibration_ladder(conf)
        self.assertEqual((times, carbons), ([1.0, 2.0], [5, 6]))

    def test_no_env_override_uses_settings_path(self):
        settings_cal = Path("/tmp/settings_cal2.CDF")
        entries = [{"rt": 1.0, "carbon": 5}, {"rt": 2.0, "carbon": 6}]
        conf = {
            "calibration_cdf": str(settings_cal),
            "calibration_assignments": _assignments(settings_cal, entries),
        }
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GC_CAL_CDF", None)
            times, carbons = distill.calibration_ladder(conf)
        self.assertEqual((times, carbons), ([1.0, 2.0], [5, 6]))


class AnalysisCoreBoundaryTests(unittest.TestCase):
    def test_mismatched_ladder_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "calibration ladder"):
            analysis_core.segment_carbon_range(1.0, 2.0, [0.5, 1.0, 1.5], [5, 6])
        with self.assertRaisesRegex(ValueError, "calibration ladder"):
            analysis_core.carbon_to_time(6.0, [0.5, 1.0, 1.5], [5, 6])

    def test_fewer_than_two_points_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "need at least 2"):
            analysis_core.carbon_to_time(6.0, [0.5], [5])
        with self.assertRaisesRegex(ValueError, "need at least 2"):
            analysis_core.segment_carbon_range(1.0, 2.0, [], [])

    def test_non_increasing_carbons_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "not increasing"):
            analysis_core.carbon_to_time(6.0, [0.5, 1.0, 1.5], [5, 8, 6])
        with self.assertRaisesRegex(ValueError, "not increasing"):
            analysis_core.segment_carbon_range(0.5, 1.5, [0.5, 1.0, 1.5], [5, 5, 6])


class AnalyzePairShortLadderGuardTests(unittest.TestCase):
    """analyze_pair's calibration guard must require >=2 points, not just
    truthiness — a 1-point ladder is still too short for segment_carbon_range
    (analysis_core._ladder now raises for < 2 points), and calibration_ladder
    can no longer produce one, but analyze_pair is a public function that
    must not crash if handed one directly."""

    def test_single_point_ladder_is_skipped_not_crashed(self):
        n = 1000
        t = np.linspace(0, 10, n)
        y_sample = np.zeros(n)
        y_std = np.zeros(n)
        y_sample[400:450] = 500.0  # a real, wide, above-threshold deviation
        result = analysis_core.analyze_pair(
            t, y_sample, y_std,
            thresh_marginal=50.0, thresh_moderate=200.0, thresh_significant=400.0,
            cal_times=[0.5], cal_carbons=[5],
        )
        self.assertEqual(result["segments"], [])


class GenerateConclusionEmptyLadderTests(unittest.TestCase):
    """Regression: generate_conclusion called carbon_to_time for every range
    even when cal_times == [] -> np.interp('array of sample points is
    empty'). Reached from app.py's api_analysis and _run_export_analysis
    whenever no calibration is configured."""

    def test_empty_ladder_produces_a_conclusion_without_raising(self):
        ranges = [{"label": "Gas", "c_start": 5, "c_end": 11},
                  {"label": "Oil", "c_start": 20, "c_end": 44}]
        conclusion, bullets = analysis_core.generate_conclusion(
            [], ranges, [], [], standard_name="Std"
        )
        self.assertIn("Conclusion:", conclusion)
        self.assertIn("No deviations", bullets)

    def test_short_ladder_also_skips_the_range_overlap_block(self):
        ranges = [{"label": "Gas", "c_start": 5, "c_end": 11}]
        conclusion, _ = analysis_core.generate_conclusion(
            [], ranges, [0.5], [5], standard_name="Std"
        )
        self.assertIn("Conclusion:", conclusion)


class AppUsesLadderTests(unittest.TestCase):
    """AST/source guard: the positional pairing must not come back."""

    @staticmethod
    def _app_src() -> str:
        return Path(__file__).resolve().parent.parent.joinpath("app.py").read_text(encoding="utf-8")

    def test_app_has_no_positional_n_alkane_pairing(self):
        src = self._app_src()
        self.assertNotIn("cal_carbons: list[int] = list(distill.N_ALKANE_CARBON)", src)
        self.assertGreaterEqual(src.count("distill.calibration_ladder("), 4)

    def test_app_has_no_leftover_length_slicing(self):
        src = self._app_src()
        self.assertNotIn("cal_carbons[:len(cal_times)]", src)
        self.assertNotIn("cal_carbons[: len(cal_times)]", src)

    def test_calibration_save_route_validates_assignment_ordering(self):
        src = self._app_src()
        self.assertIn("distill.validate_assignments(", src)


if __name__ == "__main__":
    unittest.main()
