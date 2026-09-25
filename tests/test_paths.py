"""paths.py: every on-disk state location, legacy vs deployed (GC_DATA_DIR)."""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import paths  # noqa: E402


class LegacyModeTests(unittest.TestCase):
    def test_no_env_is_legacy(self):
        self.assertIsNone(paths.data_dir(env={}))

    def test_legacy_settings_path_is_home_file(self):
        self.assertEqual(paths.settings_file(env={}),
                          Path.home() / ".gc_viewer_settings.json")

    def test_legacy_settings_path_is_per_port(self):
        self.assertEqual(paths.settings_file(env={"GC_PORT": "5570"}),
                          Path.home() / ".gc_viewer_settings-5570.json")

    def test_legacy_defaults_are_cwd_relative(self):
        self.assertEqual(paths.default_results_csv(env={}), Path.cwd() / "distill_results.csv")
        self.assertEqual(paths.default_processed_dir(env={}), Path.cwd() / "processed_cdf")
        self.assertEqual(paths.default_export_dir(env={}), Path.cwd() / "exports")

    def test_legacy_home_files(self):
        self.assertEqual(paths.dir_cache_file(env={}), Path.home() / ".gc_viewer_dircache.json")
        self.assertEqual(paths.notifications_file(env={}), Path.home() / ".gc_viewer_notifications.json")
        self.assertEqual(paths.standards_dir(env={}), Path.home() / "gc_comparison_standards")
        self.assertIsNone(paths.log_file(env={}))


class DeployedModeTests(unittest.TestCase):
    def setUp(self):
        self.d = Path("/srv/gcdata")
        self.env = {"GC_DATA_DIR": str(self.d), "GC_PORT": "5570"}

    def test_everything_under_data_dir(self):
        e = self.env
        self.assertEqual(paths.data_dir(env=e), self.d)
        self.assertEqual(paths.settings_file(env=e), self.d / "settings.json")  # port ignored
        self.assertEqual(paths.default_results_csv(env=e), self.d / "distill_results.csv")
        self.assertEqual(paths.default_processed_dir(env=e), self.d / "processed_cdf")
        self.assertEqual(paths.default_export_dir(env=e), self.d / "exports")
        self.assertEqual(paths.dir_cache_file(env=e), self.d / "dircache.json")
        self.assertEqual(paths.notifications_file(env=e), self.d / "notifications.json")
        self.assertEqual(paths.standards_dir(env=e), self.d / "gc_comparison_standards")
        self.assertEqual(paths.log_file(env=e), self.d / "app.log")

    def test_blank_env_value_is_legacy(self):
        self.assertIsNone(paths.data_dir(env={"GC_DATA_DIR": "  "}))


class SettingsUsesPathsTests(unittest.TestCase):
    def test_settings_module_follows_gc_data_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"GC_DATA_DIR": tmp}):
            import settings
            s = importlib.reload(settings)
            try:
                self.assertEqual(s.CONFIG_PATH, Path(tmp) / "settings.json")
                self.assertEqual(s.DEFAULTS["distill_output"], str(Path(tmp) / "distill_results.csv"))
                self.assertEqual(s.DEFAULTS["processed_cdf_dir"], str(Path(tmp) / "processed_cdf"))
                self.assertEqual(s.DEFAULTS["export_folder"], str(Path(tmp) / "exports"))
                self.assertEqual(s.DEFAULTS["comparison_defaults_dir"],
                                 str(Path(tmp) / "gc_comparison_standards"))
            finally:
                os.environ.pop("GC_DATA_DIR", None)
                importlib.reload(settings)


if __name__ == "__main__":
    unittest.main()
