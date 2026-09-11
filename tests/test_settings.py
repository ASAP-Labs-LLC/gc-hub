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


class SeedNewInstanceTests(unittest.TestCase):
    """A fresh non-default-port instance inherits tuning but not paths.

    Inheriting distill_output would give two instances one results CSV and
    two processes appending to it.
    """

    PER_INSTANCE = ("watch_dir", "processed_cdf_dir", "distill_output")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)

        self._saved_env = os.environ.get("GC_PORT")
        self._saved_settings_dir = settings_mod.instance.SETTINGS_DIR
        self._saved_cfg = settings_mod.CONFIG_PATH
        self._saved_comp = settings_mod.DEFAULTS["comparison_defaults_dir"]

        settings_mod.instance.SETTINGS_DIR = tmp
        settings_mod.DEFAULTS["comparison_defaults_dir"] = str(tmp / "stds")

        # The primary instance's config, as it would exist on the server.
        self._primary = settings_mod.instance.settings_path(
            settings_mod.instance.DEFAULT_PORT
        )
        self._primary.write_text(
            json.dumps(
                {
                    "watch_dir": r"C:\GC\watch-A",
                    "processed_cdf_dir": r"C:\GC\processed-A",
                    "distill_output": r"C:\GC\results-A.csv",
                    "analysis_window": "555",
                    "series_colors": "red,blue",
                }
            ),
            encoding="utf-8",
        )

        # Act as the second instance.
        os.environ["GC_PORT"] = "5561"
        settings_mod.CONFIG_PATH = settings_mod.instance.settings_path(5561)

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop("GC_PORT", None)
        else:
            os.environ["GC_PORT"] = self._saved_env
        settings_mod.instance.SETTINGS_DIR = self._saved_settings_dir
        settings_mod.CONFIG_PATH = self._saved_cfg
        settings_mod.DEFAULTS["comparison_defaults_dir"] = self._saved_comp
        self._tmp.cleanup()

    def test_inherits_non_path_settings(self) -> None:
        conf = settings_mod.load_settings()
        self.assertEqual(conf["analysis_window"], "555")
        self.assertEqual(conf["series_colors"], "red,blue")

    def test_does_not_inherit_per_instance_paths(self) -> None:
        conf = settings_mod.load_settings()
        for key in self.PER_INSTANCE:
            self.assertEqual(
                conf[key], settings_mod.DEFAULTS[key],
                f"{key} must not be inherited from the primary instance",
            )

    def test_seed_file_is_written_once(self) -> None:
        settings_mod.load_settings()
        self.assertTrue(settings_mod.CONFIG_PATH.exists())
        on_disk = json.loads(settings_mod.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["analysis_window"], "555")
        for key in self.PER_INSTANCE:
            self.assertNotIn(key, on_disk)

    def test_operator_edits_survive_reload(self) -> None:
        conf = settings_mod.load_settings()
        conf["watch_dir"] = str(Path(self._tmp.name) / "watch-B")
        settings_mod.save_settings(conf)
        reloaded = settings_mod.load_settings()
        self.assertEqual(reloaded["watch_dir"], conf["watch_dir"])

    def test_no_seed_when_primary_absent(self) -> None:
        self._primary.unlink()
        conf = settings_mod.load_settings()
        self.assertEqual(
            conf["analysis_window"], settings_mod.DEFAULTS["analysis_window"]
        )
        self.assertFalse(settings_mod.CONFIG_PATH.exists())

    def test_default_port_never_seeds(self) -> None:
        # Guards the existing contract that load_settings creates no file.
        os.environ.pop("GC_PORT", None)
        settings_mod.CONFIG_PATH = Path(self._tmp.name) / "fresh.json"
        settings_mod.load_settings()
        self.assertFalse(settings_mod.CONFIG_PATH.exists())


if __name__ == "__main__":
    unittest.main()
