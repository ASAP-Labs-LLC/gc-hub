"""Characterization tests for distill.py's core numeric routines.

These lock the science the whole pipeline depends on — cumulative-area →
percent, boiling-point interpolation, the ASTM D86 X4 polynomial conversion,
and the thread-safe CSV upsert/dedup — so consolidation and future edits can't
silently change a result. They use only numpy (no CDF files needed).
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import distill  # noqa: E402


class CumulativePercentTests(unittest.TestCase):
    def test_flat_signal_gives_linear_percent(self) -> None:
        t = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        y = np.array([1.0, 1.0, 1.0, 1.0, 1.0])
        out = distill._cumulative_percent(t, y)
        np.testing.assert_allclose(out, [0, 25, 50, 75, 100])

    def test_negative_intensities_are_clipped_to_zero(self) -> None:
        t = np.array([0.0, 1.0, 2.0])
        y = np.array([-5.0, 1.0, 1.0])  # first sample negative → treated as 0
        out = distill._cumulative_percent(t, y)
        self.assertEqual(out[0], 0.0)
        self.assertEqual(out[-1], 100.0)
        self.assertTrue(np.all(np.diff(out) >= 0))  # monotonic non-decreasing

    def test_zero_area_raises(self) -> None:
        t = np.array([0.0, 1.0, 2.0])
        y = np.zeros(3)
        with self.assertRaises(ValueError):
            distill._cumulative_percent(t, y)


class InterpBPTests(unittest.TestCase):
    def test_linear_interpolation_at_targets(self) -> None:
        pct = np.array([0.0, 50.0, 100.0])
        bp = np.array([100.0, 200.0, 300.0])
        out = distill._interp_bp(pct, bp, [25.0, 50.0, 75.0])
        np.testing.assert_allclose(out, [150.0, 200.0, 250.0])

    def test_handles_unsorted_percentiles(self) -> None:
        pct = np.array([100.0, 0.0, 50.0])
        bp = np.array([300.0, 100.0, 200.0])
        out = distill._interp_bp(pct, bp, [50.0])
        np.testing.assert_allclose(out, [200.0])


class ConvertToD86Tests(unittest.TestCase):
    def test_polynomial_matches_x4_coefficients(self) -> None:
        # Feed a constant temperature to every referenced D2887 percentile, so
        # each cut reduces to a0 + (a1+a2+a3)*T — letting us verify the real
        # coefficient wiring without hard-coding 11 expected numbers.
        referenced: set[str] = set()
        for trio in distill._CONVERSION_REL.values():
            referenced.update(trio)
        T = 123.4
        d2887 = {k: T for k in referenced}

        out = distill._convert_to_d86(d2887)

        self.assertEqual(set(out), set(distill._CONVERSION_REL))
        for cut, (a0, a1, a2, a3) in distill._CONVERSION_COEFF.items():
            if cut not in distill._CONVERSION_REL:
                continue
            expected = distill._round2(a0 + (a1 + a2 + a3) * T)
            self.assertEqual(out[cut], expected, f"D86 cut {cut} mismatch")

    def test_missing_percentile_is_skipped_not_fatal(self) -> None:
        # An incomplete D2887 dict must not raise — affected cuts are dropped.
        out = distill._convert_to_d86({"IBP": 50.0})
        self.assertIsInstance(out, dict)


class Round2Tests(unittest.TestCase):
    def test_rounds_to_two_decimals(self) -> None:
        self.assertEqual(distill._round2(3.14159), 3.14)
        self.assertEqual(distill._round2(2.0), 2.0)


class ParseInjectionDatetimeTests(unittest.TestCase):
    """The library is ordered by injection time. Agilent/Thermo ANDI .CDF
    files store that timestamp in several non-ISO formats; all must parse so
    the order doesn't silently collapse to file-modified time."""

    EXPECTED = datetime(2025, 2, 24, 13, 45, 0)

    def test_iso_space_separator(self) -> None:
        self.assertEqual(distill.parse_injection_datetime("2025-02-24 13:45:00"), self.EXPECTED)

    def test_iso_t_separator(self) -> None:
        self.assertEqual(distill.parse_injection_datetime("2025-02-24T13:45:00"), self.EXPECTED)

    def test_andi_compact_with_timezone_offset(self) -> None:
        # ANDI/AIA injection_date_time_stamp: YYYYMMDDHHMMSS±ZZZZ. Offset is
        # dropped — all runs from one instrument share a zone, so naive
        # wall-clock keeps ordering correct and matches the rest of distill.
        self.assertEqual(distill.parse_injection_datetime("20250224134500-0500"), self.EXPECTED)

    def test_andi_compact_no_timezone(self) -> None:
        self.assertEqual(distill.parse_injection_datetime("20250224134500"), self.EXPECTED)

    def test_day_month_name_year(self) -> None:
        self.assertEqual(distill.parse_injection_datetime("24-Feb-2025 13:45:00"), self.EXPECTED)

    def test_us_slash_format(self) -> None:
        self.assertEqual(distill.parse_injection_datetime("02/24/2025 13:45:00"), self.EXPECTED)

    def test_surrounding_whitespace_tolerated(self) -> None:
        self.assertEqual(distill.parse_injection_datetime("  20250224134500-0500 "), self.EXPECTED)

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(distill.parse_injection_datetime(""))

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(distill.parse_injection_datetime("not a date"))


class ProcessedCdfFilenameTests(unittest.TestCase):
    """Processed CDFs must carry the run *time*, not just the date, so two
    same-day re-runs of a sample don't collide and overwrite each other."""

    def test_includes_date_and_time(self) -> None:
        name = distill.processed_cdf_filename("AF25", datetime(2026, 6, 15, 9, 30, 0), ".CDF")
        self.assertEqual(name, "AF25_06152026_093000.CDF")

    def test_same_day_different_time_distinct(self) -> None:
        a = distill.processed_cdf_filename("AF25", datetime(2026, 6, 15, 9, 0, 0), ".CDF")
        b = distill.processed_cdf_filename("AF25", datetime(2026, 6, 15, 14, 0, 0), ".CDF")
        self.assertNotEqual(a, b)

    def test_suffix_is_uppercased(self) -> None:
        name = distill.processed_cdf_filename("AF25", datetime(2026, 6, 15, 9, 0, 0), ".cdf")
        self.assertTrue(name.endswith(".CDF"))

    def test_illegal_characters_sanitised(self) -> None:
        name = distill.processed_cdf_filename("A/F:25", datetime(2026, 6, 15, 9, 0, 0), ".CDF")
        for bad in '/:*?"<>|':
            self.assertNotIn(bad, name)


class UpsertCsvRowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.csv_path = Path(self._tmp.name) / "distill_results.csv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _row(self, lab_id: str, inj_dt: str, marker: float) -> list:
        # CSV_HEADER is 29 wide: Lab ID, InjectionDateTime, then 27 numbers.
        row = [lab_id, inj_dt] + [marker] * (len(distill.CSV_HEADER) - 2)
        return row

    def _read_rows(self) -> list[dict]:
        with self.csv_path.open("r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def test_creates_file_with_header_and_row(self) -> None:
        distill._upsert_csv_row(self.csv_path, self._row("L1", "2024-01-01 10:00", 1))
        rows = self._read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Lab ID"], "L1")

    def test_same_key_replaces_instead_of_duplicating(self) -> None:
        distill._upsert_csv_row(self.csv_path, self._row("L1", "2024-01-01 10:00", 1))
        distill._upsert_csv_row(self.csv_path, self._row("L1", "2024-01-01 10:00", 99))
        rows = self._read_rows()
        self.assertEqual(len(rows), 1, "Same (Lab ID, InjectionDateTime) must not duplicate")
        self.assertEqual(float(rows[0]["2887 IBP"]), 99.0, "Row should be updated in place")

    def test_different_key_appends(self) -> None:
        distill._upsert_csv_row(self.csv_path, self._row("L1", "2024-01-01 10:00", 1))
        distill._upsert_csv_row(self.csv_path, self._row("L2", "2024-01-02 10:00", 2))
        self.assertEqual(len(self._read_rows()), 2)


class AppendCsvRowTests(unittest.TestCase):
    """``_append_csv_row`` always adds a new line — even for an identical
    (Lab ID, InjectionDateTime). This backs the user decision that each reprocess
    / Export-to-LIMS produces a distinct row in the results CSV."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.csv_path = Path(self._tmp.name) / "distill_results.csv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _row(self, lab_id: str, inj_dt: str, marker: float) -> list:
        return [lab_id, inj_dt] + [marker] * (len(distill.CSV_HEADER) - 2)

    def _read_rows(self) -> list[dict]:
        with self.csv_path.open("r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def test_creates_file_with_header_and_row(self) -> None:
        distill._append_csv_row(self.csv_path, self._row("L1", "2024-01-01 10:00", 1))
        rows = self._read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Lab ID"], "L1")

    def test_same_key_appends_a_second_row(self) -> None:
        distill._append_csv_row(self.csv_path, self._row("L1", "2024-01-01 10:00", 1))
        distill._append_csv_row(self.csv_path, self._row("L1", "2024-01-01 10:00", 99))
        rows = self._read_rows()
        self.assertEqual(len(rows), 2, "append-always must add a second row")
        self.assertEqual(float(rows[0]["2887 IBP"]), 1.0)
        self.assertEqual(float(rows[1]["2887 IBP"]), 99.0)


if __name__ == "__main__":
    unittest.main()
