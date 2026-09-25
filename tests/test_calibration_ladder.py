"""calibration_ladder: (time, carbon) pairs for Analysis / reports.

Regression for the 2026-09-16 crash: auto-detect found 24 peaks on the new
350 °C calibration (20 alkanes + CS2 + 3 impurities) and they were paired by
position with the fixed 20-entry N_ALKANE_CARBON list -> np.interp raised
'fp and xp are not of the same length' and every Analysis/Export 500'd.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import analysis_core  # noqa: E402
import distill  # noqa: E402


def _assignments(cal_path, entries):
    return json.dumps({distill._cal_key(cal_path): entries})


class CalibrationLadderTests(unittest.TestCase):
    def setUp(self):
        self.cal = Path("/tmp/fake_cal.CDF")
        # 24 peaks: CS2 solvent, C5..C24, and 3 impurities, as in 002F0301.CDF
        self.entries = [{"rt": 0.30, "ignore": True}]  # CS2
        self.entries += [{"rt": 0.50 + 0.25 * i, "carbon": 5 + i} for i in range(20)]
        self.entries += [{"rt": 1.13, "ignore": True},
                         {"rt": 2.61, "ignore": True},
                         {"rt": 4.07, "ignore": True}]

    def test_uses_saved_assignments_and_drops_ignored_peaks(self):
        conf = {"calibration_cdf": str(self.cal),
                "calibration_assignments": _assignments(self.cal, self.entries)}
        times, carbons = distill.calibration_ladder(conf)
        self.assertEqual(len(times), len(carbons))
        self.assertEqual(carbons, list(range(5, 25)))
        self.assertAlmostEqual(times[0], 0.50)
        self.assertEqual(times, sorted(times))

    def test_ladder_feeds_segment_carbon_range_without_error(self):
        conf = {"calibration_cdf": str(self.cal),
                "calibration_assignments": _assignments(self.cal, self.entries)}
        times, carbons = distill.calibration_ladder(conf)
        # 0.50 min is C5 exactly, 0.75 min is C6: the solvent is NOT C5
        self.assertEqual(analysis_core.segment_carbon_range(0.50, 0.75, times, carbons), "C5-C6")

    def test_falls_back_to_auto_detect_truncated_to_known_carbons(self):
        conf = {"calibration_cdf": str(self.cal), "calibration_assignments": ""}
        peaks = [0.1 * i for i in range(1, 25)]  # 24 detected peaks
        with mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(distill, "calibration_peak_times", return_value=peaks):
            times, carbons = distill.calibration_ladder(conf)
        self.assertEqual(len(times), len(carbons))
        self.assertEqual(len(carbons), len(distill.N_ALKANE_CARBON))

    def test_no_calibration_configured_returns_empty(self):
        self.assertEqual(distill.calibration_ladder({"calibration_cdf": ""}), ([], []))

    def test_unreadable_calibration_returns_empty_not_raise(self):
        conf = {"calibration_cdf": str(self.cal), "calibration_assignments": ""}
        with mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(distill, "calibration_peak_times", side_effect=OSError("bad")):
            self.assertEqual(distill.calibration_ladder(conf), ([], []))


class AnalysisCoreBoundaryTests(unittest.TestCase):
    def test_mismatched_ladder_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "calibration ladder"):
            analysis_core.segment_carbon_range(1.0, 2.0, [0.5, 1.0, 1.5], [5, 6])
        with self.assertRaisesRegex(ValueError, "calibration ladder"):
            analysis_core.carbon_to_time(6.0, [0.5, 1.0, 1.5], [5, 6])


class AppUsesLadderTests(unittest.TestCase):
    """AST/source guard: the positional pairing must not come back."""
    def test_app_has_no_positional_n_alkane_pairing(self):
        src = Path(__file__).resolve().parent.parent.joinpath("app.py").read_text(encoding="utf-8")
        self.assertNotIn("cal_carbons: list[int] = list(distill.N_ALKANE_CARBON)", src)
        self.assertGreaterEqual(src.count("distill.calibration_ladder("), 4)


if __name__ == "__main__":
    unittest.main()
