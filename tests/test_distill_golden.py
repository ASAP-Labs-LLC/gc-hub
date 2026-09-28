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

import numpy as np

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


class ConfExplicitCalibrationTests(_IsolatedSettings):
    """Calibration functions use the conf they are given, not the global settings."""

    def setUp(self) -> None:
        super().setUp()
        import cdf_fixtures as fx
        self.cal = fx.calibration_cdf(self.root / "cal.CDF")
        self.sample = fx.sample_cdf(self.root / "40304.CDF")
        ladder = fx.ladder_times()
        carbons = distill.N_ALKANE_CARBON
        base = {"calibration_cdf": str(self.cal)}
        self.conf_a = dict(base, calibration_assignments=distill.upsert_assignments(
            "", self.cal, [{"rt": rt, "carbon": c} for rt, c in zip(ladder, carbons[:20])]))
        # The same peaks, every one assigned one carbon higher.
        self.conf_b = dict(base, calibration_assignments=distill.upsert_assignments(
            "", self.cal, [{"rt": rt, "carbon": c} for rt, c in zip(ladder[:19], carbons[1:20])]))
        self.t = np.linspace(0.5, 6.0, 50)

    def _write_settings(self, conf: dict) -> None:
        path = self.root / f"settings-{len(list(self.root.glob('settings-*')))}.json"
        path.write_text(json.dumps(dict(conf, comparison_defaults_dir=str(self.root / "std"))),
                        encoding="utf-8")
        settings.CONFIG_PATH = path
        distill._SETTINGS_CACHE = None

    def test_two_confs_on_one_path_get_their_own_calibration(self) -> None:
        for first, second in (("a", "b"), ("b", "a")):
            with self.subTest(order=first + second):
                _clear_distill_caches()
                confs = {"a": self.conf_a, "b": self.conf_b}
                v1 = distill._calibration_function(self.cal, confs[first])(self.t)
                v2 = distill._calibration_function(self.cal, confs[second])(self.t)
                self.assertFalse(np.allclose(v1, v2))
                # Both stay cached: going back returns the first conf's function.
                again = distill._calibration_function(self.cal, confs[first])(self.t)
                np.testing.assert_array_equal(again, v1)
                self.assertEqual(len(distill._CAL_CACHE), 2)

    def test_assignment_signature_reads_the_given_conf(self) -> None:
        self.assertNotEqual(distill._assignment_signature(self.cal, self.conf_a),
                            distill._assignment_signature(self.cal, self.conf_b))

    def test_distillation_curve_uses_the_passed_conf(self) -> None:
        self._write_settings(self.conf_a)
        pct_a, bp_a = distill.distillation_curve_from_cdf(self.sample)
        self._write_settings(self.conf_b)
        pct_b, bp_b = distill.distillation_curve_from_cdf(self.sample)
        self.assertFalse(np.allclose(bp_a, bp_b))

        # settings.CONFIG_PATH still points at conf_b's calibration.
        pct, bp = distill.distillation_curve_from_cdf(self.sample, blank_path=None, conf=self.conf_a)
        np.testing.assert_array_equal(pct, pct_a)
        np.testing.assert_array_equal(bp, bp_a)


if __name__ == "__main__":
    unittest.main()
