"""Tests for settings.py — defaults surface and save/load round-trip.

Fully isolated from the real ``~/.gc_viewer_settings.json``: setUp redirects
``settings.CONFIG_PATH`` and the default comparison-standards dir into a temp
directory, so running the suite never reads or clobbers the developer's real
config or creates folders in their home directory.
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


class ConfigPathPerPortTests(unittest.TestCase):
    """CONFIG_PATH is computed at import time from GC_PORT."""

    def setUp(self) -> None:
        self._saved_env = os.environ.get("GC_PORT")

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop("GC_PORT", None)
        else:
            os.environ["GC_PORT"] = self._saved_env
        importlib.reload(settings_mod)

    def test_default_port_keeps_legacy_filename(self) -> None:
        os.environ.pop("GC_PORT", None)
        importlib.reload(settings_mod)
        self.assertEqual(settings_mod.CONFIG_PATH.name, ".gc_viewer_settings.json")

    def test_explicit_default_port_keeps_legacy_filename(self) -> None:
        os.environ["GC_PORT"] = "5560"
        importlib.reload(settings_mod)
        self.assertEqual(settings_mod.CONFIG_PATH.name, ".gc_viewer_settings.json")

    def test_other_port_gets_its_own_file(self) -> None:
        os.environ["GC_PORT"] = "5561"
        importlib.reload(settings_mod)
        self.assertEqual(
            settings_mod.CONFIG_PATH.name, ".gc_viewer_settings-5561.json"
        )


if __name__ == "__main__":
    unittest.main()
