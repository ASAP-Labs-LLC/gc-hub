"""Tests for calibration peak-assignment logic in distill.py.

Covers the manual peak→carbon assignment feature: the interpolation builder
factored out of ``_build_calibration``, the settings-string parsing, anchor
selection, and the fallback-vs-assigned branch of ``_build_calibration``.

numpy-only — no CDF files needed (CDF reads / peak detection are stubbed).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import distill  # noqa: E402


class BuildCalibrationFromAnchorsTests(unittest.TestCase):
    """The interpolation builder: rt[] + bp[] -> callable rt->bp."""

    def test_passes_through_anchor_points(self) -> None:
        rt = np.array([1.0, 2.0, 3.0, 4.0])
        bp = np.array([36.0, 69.0, 98.0, 126.0])
        cal = distill.build_calibration_from_anchors(rt, bp)
        np.testing.assert_allclose(cal(rt), bp, atol=1e-6)

    def test_monotonic_input_gives_increasing_output(self) -> None:
        rt = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        bp = np.array([36.0, 69.0, 98.0, 126.0, 151.0])
        cal = distill.build_calibration_from_anchors(rt, bp)
        probe = np.linspace(1.0, 5.0, 50)
        out = cal(probe)
        self.assertTrue(np.all(np.diff(out) > 0))

    def test_two_anchors_build_without_error_and_pass_through(self) -> None:
        # Regression: a user assigning only 2 carbons must not crash the
        # calibration (cubic needs >=4 pts; the builder must drop to linear).
        rt = np.array([1.0, 3.0])
        bp = np.array([36.0, 98.0])
        cal = distill.build_calibration_from_anchors(rt, bp)
        np.testing.assert_allclose(cal(rt), bp, atol=1e-6)
        # linear midpoint
        self.assertAlmostEqual(float(cal(np.array([2.0]))[0]), 67.0, delta=0.5)

    def test_three_anchors_build_without_error_and_pass_through(self) -> None:
        rt = np.array([1.0, 2.0, 3.0])
        bp = np.array([36.0, 69.0, 98.0])
        cal = distill.build_calibration_from_anchors(rt, bp)
        np.testing.assert_allclose(cal(rt), bp, atol=1e-6)

    def test_below_first_anchor_clamps_to_first_anchor_bp(self) -> None:
        # Before the first anchor the curve is clamped to that anchor's BP.
        # Linear extrapolation (the pre-2026-09-18 behaviour) put the start of
        # a real run at -155 °C; a cubic can dive further still.
        rt = np.array([2.0, 3.0, 4.0, 5.0])
        bp = np.array([69.0, 98.0, 126.0, 151.0])
        cal = distill.build_calibration_from_anchors(rt, bp)
        vals = cal(np.array([0.0, 1.0, 1.99]))
        np.testing.assert_allclose(vals, [69.0, 69.0, 69.0], atol=1e-6)


class PeakDetectionSensitivityTests(unittest.TestCase):
    """Sensitivity slider: higher value -> detect more (smaller) peaks."""

    @staticmethod
    def _signal():
        # Four well-separated Gaussian peaks of decreasing height (100,40,8,2)
        # on a max of 100, so detection thresholds decide how many are found.
        t = np.linspace(0, 30, 3000)
        centers = [5.0, 12.0, 19.0, 26.0]
        heights = [100.0, 40.0, 8.0, 2.0]
        y = np.zeros_like(t)
        for c, h in zip(centers, heights):
            y += h * np.exp(-((t - c) / 0.15) ** 2)
        return t, y

    def test_higher_sensitivity_finds_at_least_as_many_peaks(self) -> None:
        t, y = self._signal()
        low = distill._detect_nalkane_peaks(t, y, sensitivity=20)
        high = distill._detect_nalkane_peaks(t, y, sensitivity=90)
        self.assertGreater(len(high), len(low))

    def test_higher_sensitivity_is_a_superset(self) -> None:
        t, y = self._signal()
        low = distill._detect_nalkane_peaks(t, y, sensitivity=20)
        high = distill._detect_nalkane_peaks(t, y, sensitivity=90)
        lo = {round(v, 1) for v in low}
        hi = {round(v, 1) for v in high}
        self.assertTrue(lo.issubset(hi), f"{lo} not subset of {hi}")

    def test_default_sensitivity_matches_legacy_fixed_thresholds(self) -> None:
        # sensitivity=50 must reproduce the original behavior exactly (no loss).
        t, y = self._signal()
        default = distill._detect_nalkane_peaks(t, y)
        fifty = distill._detect_nalkane_peaks(t, y, sensitivity=50)
        np.testing.assert_array_equal(default, fifty)


class CarbonBpMapTests(unittest.TestCase):
    def test_maps_each_carbon_to_its_boiling_point(self) -> None:
        m = distill.carbon_bp_map()
        self.assertEqual(m[5], distill.N_ALKANE_BP[0])
        self.assertEqual(m[6], distill.N_ALKANE_BP[1])
        # last entry
        self.assertEqual(
            m[distill.N_ALKANE_CARBON[-1]], distill.N_ALKANE_BP[-1]
        )

    def test_covers_every_reference_carbon(self) -> None:
        m = distill.carbon_bp_map()
        self.assertEqual(set(m), set(distill.N_ALKANE_CARBON))


class ParseAssignmentMapTests(unittest.TestCase):
    def test_parses_valid_json_object(self) -> None:
        raw = '{"/x/cal.CDF": [{"rt": 1.0, "carbon": 5}]}'
        out = distill.parse_assignment_map(raw)
        self.assertEqual(out["/x/cal.CDF"], [{"rt": 1.0, "carbon": 5}])

    def test_empty_string_returns_empty_dict(self) -> None:
        self.assertEqual(distill.parse_assignment_map(""), {})

    def test_garbage_returns_empty_dict(self) -> None:
        self.assertEqual(distill.parse_assignment_map("not json{"), {})

    def test_non_object_json_returns_empty_dict(self) -> None:
        self.assertEqual(distill.parse_assignment_map("[1, 2, 3]"), {})


class AnchorsForTests(unittest.TestCase):
    def _key(self, p: str) -> str:
        return str(Path(p).resolve())

    def test_returns_sorted_anchors_for_assigned_carbons(self) -> None:
        path = "/data/cal.CDF"
        amap = {
            self._key(path): [
                {"rt": 3.0, "carbon": 7},
                {"rt": 1.0, "carbon": 5},
                {"rt": 2.0, "carbon": 6},
            ]
        }
        rt, bp = distill.anchors_for(amap, path)
        np.testing.assert_allclose(rt, [1.0, 2.0, 3.0])
        np.testing.assert_allclose(bp, [36.0, 69.0, 98.0])

    def test_ignored_and_unassigned_entries_are_excluded(self) -> None:
        path = "/data/cal.CDF"
        amap = {
            self._key(path): [
                {"rt": 1.0, "carbon": 5},
                {"rt": 1.5, "ignore": True},
                {"rt": 1.8},  # unassigned
                {"rt": 2.0, "carbon": 6},
            ]
        }
        rt, bp = distill.anchors_for(amap, path)
        np.testing.assert_allclose(rt, [1.0, 2.0])
        np.testing.assert_allclose(bp, [36.0, 69.0])

    def test_fewer_than_two_anchors_returns_none(self) -> None:
        path = "/data/cal.CDF"
        amap = {self._key(path): [{"rt": 1.0, "carbon": 5}]}
        self.assertIsNone(distill.anchors_for(amap, path))

    def test_unknown_path_returns_none(self) -> None:
        self.assertIsNone(distill.anchors_for({}, "/data/cal.CDF"))

    def test_duplicate_carbons_are_deduped_keeping_first_by_time(self) -> None:
        # Behaviour change from the pre-hardening version (which kept
        # duplicates): anchors_for now shares distill._assignment_pairs with
        # calibration_ladder, which de-duplicates by carbon number. This only
        # affects hand-edited settings with a repeated carbon assignment.
        path = "/data/cal.CDF"
        amap = {
            self._key(path): [
                {"rt": 1.0, "carbon": 5},
                {"rt": 1.5, "carbon": 5},  # duplicate C5, later time - dropped
                {"rt": 2.0, "carbon": 6},
            ]
        }
        rt, bp = distill.anchors_for(amap, path)
        np.testing.assert_allclose(rt, [1.0, 2.0])
        np.testing.assert_allclose(bp, [36.0, 69.0])


class AssignmentPairsTests(unittest.TestCase):
    """distill._assignment_pairs: the single (rt, carbon) source shared by
    anchors_for, calibration_ladder, and the /api/calibration overlay."""

    def _key(self, p: str) -> str:
        return str(Path(p).resolve())

    def test_returns_sorted_pairs(self) -> None:
        path = "/data/cal.CDF"
        amap = {self._key(path): [
            {"rt": 3.0, "carbon": 7}, {"rt": 1.0, "carbon": 5}, {"rt": 2.0, "carbon": 6},
        ]}
        self.assertEqual(distill._assignment_pairs(amap, path), [(1.0, 5), (2.0, 6), (3.0, 7)])

    def test_skips_ignored_and_unassigned(self) -> None:
        path = "/data/cal.CDF"
        amap = {self._key(path): [
            {"rt": 1.0, "carbon": 5}, {"rt": 1.5, "ignore": True}, {"rt": 1.8},
        ]}
        self.assertEqual(distill._assignment_pairs(amap, path), [(1.0, 5)])

    def test_drops_carbon_unknown_to_reference_ladder(self) -> None:
        path = "/data/cal.CDF"
        amap = {self._key(path): [{"rt": 1.0, "carbon": 999}, {"rt": 2.0, "carbon": 6}]}
        self.assertEqual(distill._assignment_pairs(amap, path), [(2.0, 6)])

    def test_dedupes_carbons_keeping_first_by_time(self) -> None:
        path = "/data/cal.CDF"
        amap = {self._key(path): [
            {"rt": 1.0, "carbon": 5}, {"rt": 1.5, "carbon": 5}, {"rt": 2.0, "carbon": 6},
        ]}
        self.assertEqual(distill._assignment_pairs(amap, path), [(1.0, 5), (2.0, 6)])

    def test_malformed_rt_or_carbon_is_skipped_not_raised(self) -> None:
        path = "/data/cal.CDF"
        amap = {self._key(path): [
            {"rt": "not-a-number", "carbon": 5},
            {"rt": 1.0, "carbon": "also-not-a-number"},
            {"rt": 2.0, "carbon": 6},
        ]}
        self.assertEqual(distill._assignment_pairs(amap, path), [(2.0, 6)])

    def test_falls_back_to_str_key_lookup_when_canonical_key_is_absent(self) -> None:
        path = "/data/cal.CDF"
        amap = {str(path): [{"rt": 1.0, "carbon": 5}]}  # no resolved-path key at all
        self.assertEqual(distill._assignment_pairs(amap, path), [(1.0, 5)])

    def test_present_but_empty_entries_does_not_fall_through_to_str_key(self) -> None:
        # `is None`, not `or`: an *empty* list at the canonical (resolved)
        # key means the operator cleared all assignments for this CDF — it
        # must not fall through to a stray str(path) entry as if nothing
        # were saved. A relative path is used so the resolved key and the
        # raw str(path) key are genuinely distinct dict entries.
        path = "data/cal.CDF"
        amap = {
            self._key(path): [],
            str(path): [{"rt": 1.0, "carbon": 5}],
        }
        self.assertNotEqual(self._key(path), str(path))  # sanity: two real keys
        self.assertEqual(distill._assignment_pairs(amap, path), [])

    def test_unknown_path_returns_empty_list(self) -> None:
        self.assertEqual(distill._assignment_pairs({}, "/data/cal.CDF"), [])


class ValidateAssignmentsTests(unittest.TestCase):
    """distill.validate_assignments: pure function backing the 400 the
    /api/calibration POST route returns for a non-increasing ladder."""

    def test_strictly_increasing_carbons_has_no_errors(self) -> None:
        entries = [{"rt": 1.0, "carbon": 5}, {"rt": 2.0, "carbon": 6}, {"rt": 3.0, "carbon": 7}]
        self.assertEqual(distill.validate_assignments(entries), [])

    def test_out_of_order_pair_is_named_in_the_error(self) -> None:
        entries = [{"rt": 1.0, "carbon": 5}, {"rt": 2.0, "carbon": 10}, {"rt": 3.0, "carbon": 7}]
        errors = distill.validate_assignments(entries)
        self.assertEqual(len(errors), 1)
        self.assertIn("C7", errors[0])
        self.assertIn("C10", errors[0])

    def test_ignored_and_unassigned_entries_do_not_affect_ordering(self) -> None:
        entries = [
            {"rt": 1.0, "carbon": 5},
            {"rt": 1.2, "ignore": True},
            {"rt": 1.4},
            {"rt": 2.0, "carbon": 6},
        ]
        self.assertEqual(distill.validate_assignments(entries), [])

    def test_repeated_carbon_is_an_error(self) -> None:
        entries = [{"rt": 1.0, "carbon": 5}, {"rt": 2.0, "carbon": 5}]
        errors = distill.validate_assignments(entries)
        self.assertEqual(len(errors), 1)

    def test_empty_list_has_no_errors(self) -> None:
        self.assertEqual(distill.validate_assignments([]), [])


class ActiveCalibrationPathTests(unittest.TestCase):
    """distill.active_calibration_path: the one place GC_CAL_CDF is resolved,
    shared by the distillation math and calibration_ladder() so labels and
    numbers always agree on which file is 'the' calibration."""

    def test_uses_settings_when_no_env_override(self) -> None:
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GC_CAL_CDF", None)
            path = distill.active_calibration_path({"calibration_cdf": "/data/cal.CDF"})
        self.assertEqual(path, Path("/data/cal.CDF"))

    def test_env_override_wins_over_settings(self) -> None:
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"GC_CAL_CDF": "/env/cal.CDF"}):
            path = distill.active_calibration_path({"calibration_cdf": "/data/cal.CDF"})
        self.assertEqual(path, Path("/env/cal.CDF"))

    def test_returns_none_when_nothing_configured(self) -> None:
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GC_CAL_CDF", None)
            self.assertIsNone(distill.active_calibration_path({"calibration_cdf": ""}))
            self.assertIsNone(distill.active_calibration_path({}))


class CalibrationCacheInvalidationTests(unittest.TestCase):
    """The cached calibration must refresh when assignments change.

    Regression: the cache keyed only on the CDF file's mtime, so saving new
    assignments (which never touches the CDF) returned a stale calibration —
    re-processed samples kept the old D2887 numbers.
    """

    def test_changing_assignments_changes_the_calibration(self) -> None:
        import json, os, tempfile
        from unittest import mock

        # A real file is needed only so .stat() succeeds; with assignments
        # present the builder never actually reads it, so an empty file is fine.
        fd, p = tempfile.mkstemp(suffix=".CDF")
        os.close(fd)
        key = distill._cal_key(p)

        def conf_with(carbons):
            entries = [
                {"rt": float(i + 1), "carbon": c} for i, c in enumerate(carbons)
            ]
            return {"calibration_assignments": json.dumps({key: entries})}

        try:
            with distill._CAL_LOCK:
                distill._CAL_CACHE.clear()
            with mock.patch.object(
                distill, "_get_settings", return_value=conf_with([5, 6, 7, 8])
            ):
                f1 = distill._calibration_function(Path(p))
                v1 = float(f1(np.array([1.0]))[0])      # rt 1.0 -> C5 -> 36 °C
            # Change assignments WITHOUT clearing the cache manually.
            with mock.patch.object(
                distill, "_get_settings", return_value=conf_with([20, 24, 28, 32])
            ):
                f2 = distill._calibration_function(Path(p))
                v2 = float(f2(np.array([1.0]))[0])      # rt 1.0 -> C20 -> 344 °C
        finally:
            os.unlink(p)

        self.assertAlmostEqual(v1, 36.0, delta=1.0)
        self.assertNotAlmostEqual(
            v1, v2, delta=1.0,
            msg="calibration cache did not refresh after assignments changed",
        )


class UpsertAssignmentsTests(unittest.TestCase):
    """The string->string round-trip the POST /api/calibration route uses."""

    def test_writes_assignments_under_resolved_path_key(self) -> None:
        path = "/data/cal.CDF"
        assigns = [{"rt": 1.0, "carbon": 5}, {"rt": 2.0, "carbon": 6}]
        raw = distill.upsert_assignments("", path, assigns)
        amap = distill.parse_assignment_map(raw)
        self.assertEqual(amap[distill._cal_key(path)], assigns)

    def test_round_trips_through_anchors_for(self) -> None:
        path = "/data/cal.CDF"
        assigns = [
            {"rt": 1.0, "carbon": 5}, {"rt": 2.0, "carbon": 6},
            {"rt": 3.0, "carbon": 7}, {"rt": 4.0, "carbon": 8},
        ]
        raw = distill.upsert_assignments("", path, assigns)
        rt, bp = distill.anchors_for(distill.parse_assignment_map(raw), path)
        np.testing.assert_allclose(rt, [1.0, 2.0, 3.0, 4.0])
        np.testing.assert_allclose(bp, [36.0, 69.0, 98.0, 126.0])

    def test_replaces_only_the_target_cdf_leaving_others(self) -> None:
        existing = distill.upsert_assignments(
            "", "/data/other.CDF", [{"rt": 5.0, "carbon": 9}]
        )
        updated = distill.upsert_assignments(
            existing, "/data/cal.CDF", [{"rt": 1.0, "carbon": 5}]
        )
        amap = distill.parse_assignment_map(updated)
        self.assertIn(distill._cal_key("/data/other.CDF"), amap)
        self.assertIn(distill._cal_key("/data/cal.CDF"), amap)

    def test_reassigning_same_cdf_overwrites_previous(self) -> None:
        first = distill.upsert_assignments(
            "", "/data/cal.CDF", [{"rt": 1.0, "carbon": 5}]
        )
        second = distill.upsert_assignments(
            first, "/data/cal.CDF", [{"rt": 9.0, "carbon": 20}]
        )
        amap = distill.parse_assignment_map(second)
        self.assertEqual(
            amap[distill._cal_key("/data/cal.CDF")], [{"rt": 9.0, "carbon": 20}]
        )


class BuildCalibrationBranchTests(unittest.TestCase):
    """`_build_calibration` chooses saved assignments over auto-detection."""

    def test_uses_saved_assignments_when_present(self) -> None:
        import json
        from unittest import mock

        path = "/data/cal.CDF"
        key = str(Path(path).resolve())
        raw = json.dumps({key: [
            {"rt": 1.0, "carbon": 5},   # -> 36 °C
            {"rt": 2.0, "carbon": 6},   # -> 69 °C
            {"rt": 3.0, "carbon": 7},   # -> 98 °C
            {"rt": 4.0, "carbon": 8},   # -> 126 °C (cubic needs >= 4 anchors)
            {"rt": 9.0, "ignore": True},
        ]})
        with mock.patch.object(
            distill, "_get_settings",
            return_value={"calibration_assignments": raw},
        ), mock.patch.object(
            distill, "_read_cdf",
            side_effect=AssertionError("must not read CDF when assignments exist"),
        ):
            cal = distill._build_calibration(Path(path))
        np.testing.assert_allclose(
            cal(np.array([1.0, 2.0, 3.0, 4.0])),
            [36.0, 69.0, 98.0, 126.0], atol=1e-6,
        )

    def test_two_carbon_assignment_does_not_crash(self) -> None:
        # End-to-end regression for the reported dashboard outage: 2 saved
        # carbons must yield a usable calibration, not raise.
        import json
        from unittest import mock
        path = "/data/cal.CDF"
        raw = json.dumps({str(Path(path).resolve()): [
            {"rt": 1.0, "carbon": 5}, {"rt": 3.0, "carbon": 7},
        ]})
        with mock.patch.object(
            distill, "_get_settings",
            return_value={"calibration_assignments": raw},
        ):
            cal = distill._build_calibration(Path(path))
        np.testing.assert_allclose(cal(np.array([1.0, 3.0])), [36.0, 98.0], atol=1e-6)

    def test_falls_back_to_autodetect_when_anchor_build_fails(self) -> None:
        # Defense in depth: any builder failure must fall back, never propagate
        # (a bad calibration must not take down every sample on the dashboard).
        import json
        from unittest import mock
        path = "/data/cal.CDF"
        raw = json.dumps({str(Path(path).resolve()): [
            {"rt": 1.0, "carbon": 5}, {"rt": 2.0, "carbon": 6},
            {"rt": 3.0, "carbon": 7}, {"rt": 4.0, "carbon": 8},
        ]})
        t = np.linspace(0, 10, 100)
        detected = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        real_builder = distill.build_calibration_from_anchors
        calls: list = []

        def builder(rt, bp):
            calls.append(len(rt))
            if len(calls) == 1:           # the assignment-based build — fail it
                raise ValueError("boom")
            return real_builder(rt, bp)   # the auto-detect fallback — let it work

        with mock.patch.object(
            distill, "_get_settings",
            return_value={"calibration_assignments": raw},
        ), mock.patch.object(
            distill, "build_calibration_from_anchors", side_effect=builder,
        ), mock.patch.object(
            distill, "_read_cdf", return_value=(t, np.zeros(100)),
        ), mock.patch.object(
            distill, "_detect_nalkane_peaks", return_value=detected,
        ):
            # Must not raise — falls back to auto-detected sequential calibration.
            cal = distill._build_calibration(Path(path))
        self.assertTrue(callable(cal))

    def test_falls_back_to_sequential_when_no_assignments(self) -> None:
        from unittest import mock

        t = np.linspace(0, 10, 100)
        y = np.zeros(100)
        # 5 peaks -> C5..C9 sequentially (cubic spline needs >= 4 anchors)
        detected = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        with mock.patch.object(
            distill, "_get_settings",
            return_value={"calibration_assignments": ""},
        ), mock.patch.object(
            distill, "_read_cdf", return_value=(t, y),
        ), mock.patch.object(
            distill, "_detect_nalkane_peaks", return_value=detected,
        ):
            cal = distill._build_calibration(Path("/data/cal.CDF"))
        # Sequential zip: detected peaks map to the first N reference BPs.
        np.testing.assert_allclose(
            cal(detected), distill.N_ALKANE_BP[:5], atol=1e-6
        )


if __name__ == "__main__":
    unittest.main()
