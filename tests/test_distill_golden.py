"""Golden tests: distill's results for the synthetic fixtures are pinned to
the rows v1.0.0 wrote (tests/golden/d2887_rows.json, produced by
tests/golden/make_golden.py on the code before the phase 2 refactor).

Any change to a pinned value is a change in results, which is a MAJOR
release (see RELEASING.md), never a refactor.
"""
from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np

TESTS_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = TESTS_DIR.parent
for _p in (WEBAPP_DIR, TESTS_DIR, TESTS_DIR / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cdf_fixtures as fx  # noqa: E402
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


def _csv_strings(row: dict) -> dict:
    """``row``'s values exactly as csv.writer writes them (what process_cdf does)."""
    buf = io.StringIO()
    csv.writer(buf).writerow(list(row.values()))
    buf.seek(0)
    return dict(zip(row.keys(), next(csv.reader(buf))))


class CalibrationAnchorsTests(_IsolatedSettings):
    def setUp(self) -> None:
        super().setUp()
        self.inputs = make_golden.build_inputs(self.root)
        self.cal = self.inputs["cal"]
        self.conf = make_golden.case_conf(self.root, self.inputs, "sample_40304_blank")

    def _assert_matches_build(self, info: dict, conf: dict) -> None:
        cbp = distill.carbon_bp_map()
        rt = np.array([a[0] for a in info["anchors"]])
        bp = np.array([cbp[a[1]] for a in info["anchors"]])
        t = np.linspace(0.2, 7.5, 200)
        np.testing.assert_array_equal(distill.build_calibration_from_anchors(rt, bp)(t),
                                      distill._build_calibration(self.cal, conf)(t))

    def test_assigned_anchors_skip_ignored_peaks(self) -> None:
        info = distill.calibration_anchors(self.cal, self.conf)
        self.assertEqual(info["source"], "assignments")
        expected = [[rt, c] for rt, c in zip(fx.ladder_times(), distill.N_ALKANE_CARBON[:20])]
        self.assertEqual(info["anchors"], expected)
        self._assert_matches_build(info, self.conf)

    def test_auto_detect_fallback(self) -> None:
        conf = dict(self.conf, calibration_assignments="")
        info = distill.calibration_anchors(self.cal, conf)
        self.assertEqual(info["source"], "auto")
        # Auto-detection takes the solvent as the first peak (C5) and stops at
        # the ladder's 20 reference carbons.
        self.assertEqual([c for _, c in info["anchors"]], distill.N_ALKANE_CARBON[:20])
        self.assertAlmostEqual(info["anchors"][0][0], make_golden.SOLVENT_RT, places=3)
        self.assertTrue(all(isinstance(rt, float) for rt, _ in info["anchors"]))
        self._assert_matches_build(info, conf)


class ComputeTests(_IsolatedSettings):
    def setUp(self) -> None:
        super().setUp()
        self.inputs = make_golden.build_inputs(self.root)

    def _loaded_conf(self, name: str) -> dict:
        """The case's settings as process_cdf sees them (file merged over DEFAULTS)."""
        path = self.root / f"{name}.settings.json"
        path.write_text(json.dumps(make_golden.case_conf(self.root, self.inputs, name)),
                        encoding="utf-8")
        settings.CONFIG_PATH = path
        distill._SETTINGS_CACHE = None
        return distill._get_settings()

    def _case(self, name: str):
        sample_key, use_blank, _ = make_golden.CASES[name]
        return (self.inputs["samples"][sample_key], self.inputs["blank"] if use_blank else None)

    def test_row_equals_golden(self) -> None:
        for name, expected in GOLDEN.items():
            with self.subTest(case=name):
                cdf, blank = self._case(name)
                result = distill.compute(cdf, self._loaded_conf(name), blank)
                row = result["row"]
                self.assertEqual(list(row), distill.CSV_HEADER)
                self.assertEqual(row["Source File"], "")
                self.assertEqual(_csv_strings({k: v for k, v in row.items() if k != "Source File"}),
                                 expected)

    def test_result_fields(self) -> None:
        cdf, blank = self._case("sample_40304_blank")
        conf = self._loaded_conf("sample_40304_blank")
        result = distill.compute(cdf, conf, blank)
        self.assertEqual(result["lab_id"], "40304")
        self.assertEqual(result["injection_dt"], datetime(2026, 9, 25, 14, 23, 0))
        self.assertEqual(set(result["d2887"]), {"IBP", "5%", "10%", "20%", "30%", "40%", "50%",
                                                "60%", "70%", "80%", "90%", "95%", "FBP"})
        self.assertEqual(result["calibration"], {
            "cdf": str(self.inputs["cal"]),
            "anchors_source": "assignments",
            "anchors": distill.calibration_anchors(self.inputs["cal"], conf)["anchors"],
        })
        # The uncorrected D86 is the no-corrections golden row; the corrected
        # one is that plus each cut's correction.
        golden_uncorrected = GOLDEN["no_corrections"]
        for cut, col in (("IBP", "D86 IBP"), ("50%", "D86 T50"), ("FBP", "D86 FBP")):
            self.assertEqual(str(result["d86_uncorrected"][cut]), golden_uncorrected[col])
        self.assertAlmostEqual(result["d86"]["IBP"] - result["d86_uncorrected"]["IBP"], -12.08, places=6)
        self.assertEqual(result["d86"]["20%"], result["d86_uncorrected"]["20%"])

    def test_empty_corrections_leave_d86_uncorrected(self) -> None:
        cdf, blank = self._case("sample_40304_blank")
        result = distill.compute(cdf, self._loaded_conf("sample_40304_blank"), blank, corrections={})
        self.assertEqual(result["d86"], result["d86_uncorrected"])
        row = {k: v for k, v in result["row"].items() if k != "Source File"}
        self.assertEqual(_csv_strings(row), GOLDEN["no_corrections"])

    def test_compute_writes_nothing(self) -> None:
        cdf, blank = self._case("sample_40304_blank")
        conf = self._loaded_conf("sample_40304_blank")

        def snapshot():
            return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in self.root.rglob("*")}

        before = snapshot()
        distill.compute(cdf, conf, blank)
        self.assertEqual(snapshot(), before)
        self.assertFalse(Path(conf["distill_output"]).exists())
        self.assertFalse(Path(conf["processed_cdf_dir"]).exists())
        self.assertTrue(cdf.is_file())

    def test_missing_calibration_raises_like_process_cdf(self) -> None:
        cdf, _ = self._case("sample_40304_noblank")
        conf = self._loaded_conf("sample_40304_noblank")
        conf["calibration_cdf"] = str(self.root / "missing.CDF")
        with self.assertRaises(FileNotFoundError) as got:
            distill.compute(cdf, conf)
        path = self.root / "missing.settings.json"
        path.write_text(json.dumps(conf), encoding="utf-8")
        settings.CONFIG_PATH = path
        distill._SETTINGS_CACHE = None
        with self.assertRaises(FileNotFoundError) as today:
            distill.process_cdf(cdf)
        self.assertEqual(str(got.exception), str(today.exception))
        self.assertIn("Calibration CDF not found", str(got.exception))


if __name__ == "__main__":
    unittest.main()
