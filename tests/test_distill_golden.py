"""Golden tests: distill's results for the synthetic fixtures are pinned to
the rows v1.0.0 wrote (tests/golden/d2887_rows.json, produced by
tests/golden/make_golden.py on the code before the phase 2 refactor).

Any change to a pinned value is a change in results, which is a MAJOR
release (see RELEASING.md), never a refactor.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = TESTS_DIR.parent
for _p in (WEBAPP_DIR, TESTS_DIR, TESTS_DIR / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import distill  # noqa: E402
import make_golden  # noqa: E402
import settings  # noqa: E402

GOLDEN = json.loads((TESTS_DIR / "golden" / "d2887_rows.json").read_text(encoding="utf-8"))

# The correction values the golden rows were produced with, written out
# here as well so an edit to make_golden's inputs cannot slip past.
PINNED_D86_CORRECTIONS = {
    "IBP - D86": -12.08, "5% - D86": 0.0, "10% - D86": -5.25, "20% - D86": 0.0,
    "30% - D86": 0.0, "50% - D86": -4.06, "70% - D86": 0.0, "80% - D86": 0.0,
    "90% - D86": -3.46, "95% - D86": 0.0, "FBP - D86": -5.57,
}


def _clear_distill_caches() -> None:
    distill._SETTINGS_CACHE = None
    distill._SETTINGS_MTIME = None
    with distill._CAL_LOCK:
        distill._CAL_CACHE.clear()


class _IsolatedSettings(unittest.TestCase):
    """Restores settings.CONFIG_PATH and empties distill's caches around each test."""

    def setUp(self) -> None:
        self._saved_config = settings.CONFIG_PATH
        _clear_distill_caches()
        self._tmp = tempfile.TemporaryDirectory(prefix="gc-golden-test-")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        settings.CONFIG_PATH = self._saved_config
        _clear_distill_caches()
        self._tmp.cleanup()


class ProcessCdfGoldenTests(_IsolatedSettings):
    def test_golden_inputs_are_pinned(self) -> None:
        self.assertEqual(make_golden.D86_CORRECTIONS, PINNED_D86_CORRECTIONS)
        self.assertEqual(sorted(make_golden.CASES), sorted(GOLDEN))

    def test_process_cdf_rows_equal_golden(self) -> None:
        rows = make_golden.run_all(self.root)
        self.assertEqual(settings.CONFIG_PATH, self._saved_config)
        for name, expected in GOLDEN.items():
            with self.subTest(case=name):
                self.assertEqual(rows[name], expected)


if __name__ == "__main__":
    unittest.main()
