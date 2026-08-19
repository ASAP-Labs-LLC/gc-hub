"""Tests for reprocess query parsing and resolution.

Users type single Lab IDs, comma/newline lists, and integer ranges
("34562 to 34569", "34562-34569", "34562:34569"). The backend expands those
against the live library so the modal can preview matched + missing samples
before anything is reprocessed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import reprocess_query as rq  # noqa: E402


class ParseQueryTests(unittest.TestCase):
    def test_single_value(self) -> None:
        self.assertEqual(rq.parse_reprocess_query("34562"),
                         [{"kind": "single", "value": "34562"}])

    def test_comma_separated_singles(self) -> None:
        out = rq.parse_reprocess_query("34562, 34563")
        self.assertEqual(out, [{"kind": "single", "value": "34562"},
                               {"kind": "single", "value": "34563"}])

    def test_newline_separated_singles(self) -> None:
        out = rq.parse_reprocess_query("34562\n34563")
        self.assertEqual([t["value"] for t in out], ["34562", "34563"])

    def test_range_with_to_keyword(self) -> None:
        self.assertEqual(rq.parse_reprocess_query("34562 to 34569"),
                         [{"kind": "range", "start": 34562, "end": 34569}])

    def test_range_with_dash(self) -> None:
        self.assertEqual(rq.parse_reprocess_query("34562-34569"),
                         [{"kind": "range", "start": 34562, "end": 34569}])

    def test_range_with_colon(self) -> None:
        self.assertEqual(rq.parse_reprocess_query("34562:34569"),
                         [{"kind": "range", "start": 34562, "end": 34569}])

    def test_reversed_range_is_normalised(self) -> None:
        self.assertEqual(rq.parse_reprocess_query("34569 to 34562"),
                         [{"kind": "range", "start": 34562, "end": 34569}])

    def test_mixed_range_and_single(self) -> None:
        out = rq.parse_reprocess_query("34562 to 34564\n34570")
        self.assertEqual(out, [{"kind": "range", "start": 34562, "end": 34564},
                               {"kind": "single", "value": "34570"}])

    def test_empty_returns_empty_list(self) -> None:
        self.assertEqual(rq.parse_reprocess_query("   \n  "), [])

    def test_oversized_range_raises(self) -> None:
        with self.assertRaises(ValueError):
            rq.parse_reprocess_query("1 to 99999999")


class ResolveQueryTests(unittest.TestCase):
    LIBRARY = ["34562", "34563", "34565", "34570"]

    def test_single_match(self) -> None:
        out = rq.resolve_query([{"kind": "single", "value": "34562"}], self.LIBRARY)
        self.assertEqual(out["matched"], ["34562"])
        self.assertEqual(out["missing"], [])

    def test_single_missing(self) -> None:
        out = rq.resolve_query([{"kind": "single", "value": "99999"}], self.LIBRARY)
        self.assertEqual(out["matched"], [])
        self.assertEqual(out["missing"], ["99999"])

    def test_range_splits_matched_and_missing(self) -> None:
        out = rq.resolve_query([{"kind": "range", "start": 34562, "end": 34566}],
                               self.LIBRARY)
        self.assertEqual(out["matched"], ["34562", "34563", "34565"])
        self.assertEqual(out["missing"], ["34564", "34566"])

    def test_zero_padded_library_matches_integer_query(self) -> None:
        out = rq.resolve_query([{"kind": "range", "start": 1, "end": 2}],
                               ["0001", "0002"])
        self.assertEqual(out["matched"], ["0001", "0002"])
        self.assertEqual(out["missing"], [])

    def test_results_are_deduped_preserving_order(self) -> None:
        out = rq.resolve_query(
            [{"kind": "single", "value": "34562"},
             {"kind": "range", "start": 34562, "end": 34563}],
            self.LIBRARY)
        self.assertEqual(out["matched"], ["34562", "34563"])


if __name__ == "__main__":
    unittest.main()
