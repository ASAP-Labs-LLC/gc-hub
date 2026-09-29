"""Tests for settings.py — defaults surface and save/load round-trip.

Fully isolated: setUp redirects ``settings.CONFIG_PATH`` and the default
comparison-standards dir into a temp directory, so running the suite never
reads or clobbers a real config or creates folders outside it.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import settings as settings_mod  # noqa: E402

# Keys the rest of the app relies on. The early_signal_* trio is a consolidation
# gain (present in the live folder, absent in the old copy) — lock it in.
REQUIRED_KEYS = (
    "watch_dir",
    "processed_cdf_dir",
    "distill_output",
    "calibration_cdf",
    "calibration_assignments",
    "calibration_sensitivity",
    "export_folder",
    "comparison_defaults_dir",
    "analysis_quantile",
    "analysis_window",
    "analysis_sigma",
    "analysis_thresh_marginal",
    "analysis_thresh_moderate",
    "analysis_thresh_significant",
    "early_signal_enabled",
    "early_signal_time_min",
    "early_signal_intensity_threshold",
)


class DefaultsTests(unittest.TestCase):
    def test_all_required_keys_present(self) -> None:
        missing = [k for k in REQUIRED_KEYS if k not in settings_mod.DEFAULTS]
        self.assertEqual(missing, [], f"settings.DEFAULTS missing keys: {missing}")

    def test_calibration_assignments_defaults_empty(self) -> None:
        self.assertEqual(settings_mod.DEFAULTS["calibration_assignments"], "")

    def test_calibration_sensitivity_defaults_to_fifty(self) -> None:
        # 50 == the legacy fixed-threshold behavior.
        self.assertEqual(settings_mod.DEFAULTS["calibration_sensitivity"], "50")

    def test_early_signal_defaults_preserved(self) -> None:
        self.assertEqual(settings_mod.DEFAULTS["early_signal_enabled"], "true")
        self.assertEqual(settings_mod.DEFAULTS["early_signal_time_min"], "0.5")
        self.assertEqual(
            settings_mod.DEFAULTS["early_signal_intensity_threshold"], "7500"
        )


class RoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        # Redirect config + comparison dir into the temp sandbox.
        self._saved_cfg = settings_mod.CONFIG_PATH
        self._saved_comp = settings_mod.DEFAULTS["comparison_defaults_dir"]
        settings_mod.CONFIG_PATH = tmp / "settings.json"
        settings_mod.DEFAULTS["comparison_defaults_dir"] = str(tmp / "stds")

    def tearDown(self) -> None:
        settings_mod.CONFIG_PATH = self._saved_cfg
        settings_mod.DEFAULTS["comparison_defaults_dir"] = self._saved_comp
        self._tmp.cleanup()

    def test_load_returns_defaults_when_no_file(self) -> None:
        conf = settings_mod.load_settings()
        self.assertEqual(conf["analysis_window"], "301")
        self.assertFalse(
            settings_mod.CONFIG_PATH.exists(),
            "load_settings must not create the config file as a side effect.",
        )

    def test_save_then_load_roundtrip(self) -> None:
        conf = settings_mod.load_settings()
        conf["watch_dir"] = str(Path(self._tmp.name) / "watch")
        conf["analysis_window"] = "555"
        settings_mod.save_settings(conf)
        self.assertTrue(settings_mod.CONFIG_PATH.exists())

        reloaded = settings_mod.load_settings()
        self.assertEqual(reloaded["watch_dir"], conf["watch_dir"])
        self.assertEqual(reloaded["analysis_window"], "555")


    def test_save_keeps_indent_2_formatting(self) -> None:
        settings_mod.save_settings({"a": "1", "b": "2"})
        self.assertEqual(
            settings_mod.CONFIG_PATH.read_text(encoding="utf-8"),
            json.dumps({"a": "1", "b": "2"}, indent=2),
        )

    def test_failed_save_leaves_old_file_and_no_temp(self) -> None:
        """A crash half-way through writing must not truncate settings.json
        (the operator's calibration assignments live there) and must not
        leave a temp file behind. The failure is logged, not raised."""
        settings_mod.save_settings({"watch_dir": "old", "analysis_window": "301"})
        before = settings_mod.CONFIG_PATH.read_text(encoding="utf-8")

        def half_then_fail(obj, fh, **kw):
            fh.write('{"watch_dir": "ne')
            fh.flush()
            raise OSError("disk full")

        from unittest import mock
        with mock.patch.object(settings_mod.json, "dump", side_effect=half_then_fail):
            with self.assertLogs("settings", level="ERROR") as logs:
                settings_mod.save_settings({"watch_dir": "new"})
        self.assertTrue(any("Settings save failed" in m for m in logs.output))

        self.assertEqual(settings_mod.CONFIG_PATH.read_text(encoding="utf-8"), before)
        self.assertEqual(
            sorted(p.name for p in settings_mod.CONFIG_PATH.parent.iterdir()
                   if p.is_file()),
            ["settings.json"],
        )


    def test_save_retries_a_briefly_locked_target(self) -> None:
        from unittest import mock
        real_replace = os.replace
        calls = []

        def locked_once(src, dst):
            calls.append(dst)
            if len(calls) == 1:
                raise PermissionError(13, "sharing violation")
            return real_replace(src, dst)

        with mock.patch.object(settings_mod, "SAVE_REPLACE_BACKOFF_SECONDS", 0), \
                mock.patch.object(settings_mod.os, "replace", side_effect=locked_once):
            settings_mod.save_settings({"watch_dir": "retried"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            json.loads(settings_mod.CONFIG_PATH.read_text(encoding="utf-8")),
            {"watch_dir": "retried"},
        )

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_save_keeps_the_existing_file_mode(self) -> None:
        settings_mod.save_settings({"a": "1"})
        os.chmod(settings_mod.CONFIG_PATH, 0o644)
        settings_mod.save_settings({"a": "2"})
        self.assertEqual(settings_mod.CONFIG_PATH.stat().st_mode & 0o777, 0o644)

class NoPerInstanceSettingsTests(unittest.TestCase):
    """v2: one settings file per data folder; v1's per-port files and the
    seeding of a second instance are gone (spec D14)."""

    def test_legacy_helpers_are_gone(self):
        self.assertFalse(hasattr(settings_mod, "PER_INSTANCE_KEYS"))
        self.assertFalse(hasattr(settings_mod, "_seed_new_instance"))
        self.assertFalse(hasattr(settings_mod, "instance"))


class DeviationBulletSettingsTests(unittest.TestCase):
    """Phase 3: the deviation-bullet settings are defaults, admin-gated, and
    "Set as Default" (/api/save-analysis-defaults) saves them."""

    NEW = {
        "analysis_min_width_min": "0.05",
        "analysis_merge_gap_min": "0.10",
        "analysis_spike_report_threshold": "",   # empty = the moderate threshold
        "analysis_spike_max_fwhm_min": "0.20",
        "analysis_spike_min_dominance": "0.6",
    }

    def test_defaults(self) -> None:
        for key, value in self.NEW.items():
            self.assertEqual(settings_mod.DEFAULTS.get(key), value, key)

    def test_admin_gated(self) -> None:
        for key in self.NEW:
            self.assertIn(key, settings_mod.ADMIN_KEYS)
            self.assertNotIn(key, settings_mod.OPERATOR_KEYS)

    def test_save_analysis_defaults_saves_them(self) -> None:
        import ast
        src = (WEBAPP_DIR / "app.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "api_save_analysis_defaults")
        names = {c.value for c in ast.walk(fn)
                 if isinstance(c, ast.Constant) and isinstance(c.value, str)}
        for key in ("min_width_min", "merge_gap_min", "spike_report_threshold",
                    "spike_min_width_min", "spike_max_fwhm_min", "spike_min_dominance"):
            self.assertIn(key, names)


if __name__ == "__main__":
    unittest.main()
