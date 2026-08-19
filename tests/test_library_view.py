"""Tests for sample-library duplicate labelling.

Re-runs of the same sample name (e.g. two 'AF25', incl. same-day) are distinct
samples keyed on (Lab ID, InjectionDateTime). The library must show all of
them, each with a stable unique id and a run-order counter suffix for display.
The raw 'name' stays untouched (CSV / QBench use it); only 'display_name' gets
the suffix.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import library_view  # noqa: E402


def _entry(name, mtime, inj_dt):
    return {"name": name, "mtime": mtime, "inj_dt": inj_dt, "path": f"{name}.CDF"}


class AssignDuplicateLabelsTests(unittest.TestCase):
    def test_unique_name_keeps_plain_label(self) -> None:
        out = library_view.assign_duplicate_labels([_entry("AF25", 1.0, "2026-06-15 09:00:00")])
        self.assertEqual(out[0]["display_name"], "AF25")
        self.assertEqual(out[0]["name"], "AF25")  # raw name untouched

    def test_repeats_get_counter_in_run_order(self) -> None:
        entries = [
            _entry("AF25", 30.0, "2026-06-15 11:00:00"),  # newest (3rd run)
            _entry("AF25", 10.0, "2026-06-15 09:00:00"),  # oldest (1st run)
            _entry("AF25", 20.0, "2026-06-15 10:00:00"),  # 2nd run
        ]
        out = library_view.assign_duplicate_labels(entries)
        by_mtime = {e["mtime"]: e["display_name"] for e in out}
        self.assertEqual(by_mtime[10.0], "AF25")        # first run, no suffix
        self.assertEqual(by_mtime[20.0], "AF25 (2)")
        self.assertEqual(by_mtime[30.0], "AF25 (3)")

    def test_distinct_names_are_independent(self) -> None:
        entries = [
            _entry("AF25", 10.0, "2026-06-15 09:00:00"),
            _entry("BF30", 20.0, "2026-06-15 10:00:00"),
        ]
        out = library_view.assign_duplicate_labels(entries)
        labels = {e["name"]: e["display_name"] for e in out}
        self.assertEqual(labels["AF25"], "AF25")
        self.assertEqual(labels["BF30"], "BF30")

    def test_every_entry_gets_a_unique_uid(self) -> None:
        entries = [
            _entry("AF25", 10.0, "2026-06-15 09:00:00"),
            _entry("AF25", 20.0, "2026-06-15 10:00:00"),
        ]
        out = library_view.assign_duplicate_labels(entries)
        uids = [e["uid"] for e in out]
        self.assertEqual(len(uids), len(set(uids)))
        self.assertTrue(all(uids))

    def test_input_order_is_preserved(self) -> None:
        entries = [
            _entry("AF25", 30.0, "2026-06-15 11:00:00"),
            _entry("AF25", 10.0, "2026-06-15 09:00:00"),
        ]
        out = library_view.assign_duplicate_labels(entries)
        self.assertEqual([e["mtime"] for e in out], [30.0, 10.0])

    def test_collision_on_same_name_and_time_still_unique(self) -> None:
        # Defensive: identical (name, inj_dt) must not produce duplicate uids.
        entries = [
            _entry("AF25", 10.0, "2026-06-15 09:00:00"),
            _entry("AF25", 10.0, "2026-06-15 09:00:00"),
        ]
        out = library_view.assign_duplicate_labels(entries)
        self.assertNotEqual(out[0]["uid"], out[1]["uid"])


if __name__ == "__main__":
    unittest.main()
