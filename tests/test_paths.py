"""paths.py: every on-disk state location, all under GC_DATA_DIR (hub mode only)."""
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


class NoDataDirTests(unittest.TestCase):
    """v2 has no legacy mode (spec D14): without GC_DATA_DIR every location
    refuses, pointing at DEPLOY.md."""

    def test_no_env_is_none(self):
        self.assertIsNone(paths.data_dir(env={}))
        self.assertIsNone(paths.data_dir(env={"GC_DATA_DIR": "  "}))

    def test_every_location_refuses(self):
        for fn in (paths.require_data_dir, paths.settings_file, paths.default_results_csv,
                   paths.default_processed_dir, paths.default_export_dir, paths.standards_dir,
                   paths.notifications_file, paths.log_file):
            with self.assertRaises(paths.DataDirMissing, msg=fn.__name__) as cm:
                fn(env={"GC_PORT": "5570"})
            self.assertIn("DEPLOY.md", str(cm.exception))

    def test_legacy_helpers_are_gone(self):
        for name in ("default_watch_dir", "dir_cache_file"):
            self.assertFalse(hasattr(paths, name), name)


class DataDirTests(unittest.TestCase):
    def setUp(self):
        self.d = Path("/srv/gcdata").resolve()
        self.env = {"GC_DATA_DIR": "/srv/gcdata", "GC_PORT": "5570"}

    def test_everything_under_data_dir(self):
        e = self.env
        self.assertEqual(paths.data_dir(env=e), self.d)
        self.assertEqual(paths.require_data_dir(env=e), self.d)
        self.assertEqual(paths.settings_file(env=e), self.d / "settings.json")  # port ignored
        self.assertEqual(paths.default_results_csv(env=e), self.d / "distill_results.csv")
        self.assertEqual(paths.default_processed_dir(env=e), self.d / "processed_cdf")
        self.assertEqual(paths.default_export_dir(env=e), self.d / "exports")
        self.assertEqual(paths.notifications_file(env=e), self.d / "notifications.json")
        self.assertEqual(paths.standards_dir(env=e), self.d / "gc_comparison_standards")
        self.assertEqual(paths.log_file(env=e), self.d / "app.log")

    def test_relative_env_value_is_resolved_absolute(self):
        # A relative GC_DATA_DIR is normalized once, here, so nothing
        # downstream resolves against cwd (the release folder).
        rel = "relative/gcdata"
        self.assertTrue(paths.data_dir(env={"GC_DATA_DIR": rel}).is_absolute())
        self.assertEqual(paths.data_dir(env={"GC_DATA_DIR": rel}), Path(rel).resolve())


class SettingsUsesPathsTests(unittest.TestCase):
    """Reloading settings.py under GC_DATA_DIR must not leak into other tests.

    The env patch and the module reload it drives are scoped tightly to the
    ``with`` block; the restoring reload happens in ``tearDown`` — strictly
    after ``mock.patch.dict`` has already put ``os.environ`` back — so it
    always recomputes ``settings`` from the *real* environment, never from a
    still-patched one. ``distill``'s settings cache is cleared on both sides
    since it's keyed on ``settings.CONFIG_PATH``'s mtime, which just moved.
    """

    def setUp(self) -> None:
        import distill
        self._distill = distill
        distill._SETTINGS_CACHE = None
        distill._SETTINGS_MTIME = None

    def tearDown(self) -> None:
        import settings
        os.environ.pop("GC_DATA_DIR", None)
        importlib.reload(settings)
        self._distill._SETTINGS_CACHE = None
        self._distill._SETTINGS_MTIME = None

    def test_settings_module_follows_gc_data_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # paths.data_dir() resolves GC_DATA_DIR, which
            # on macOS normalizes /var -> /private/var — resolve tmp the same
            # way so the comparison isn't a symlink-vs-not false negative.
            resolved = Path(tmp).resolve()
            with mock.patch.dict(os.environ, {"GC_DATA_DIR": tmp}):
                import settings
                s = importlib.reload(settings)
                self.assertEqual(s.CONFIG_PATH, resolved / "settings.json")
                self.assertEqual(s.DEFAULTS["distill_output"], str(resolved / "distill_results.csv"))
                self.assertEqual(s.DEFAULTS["processed_cdf_dir"], str(resolved / "processed_cdf"))
                self.assertEqual(s.DEFAULTS["export_folder"], str(resolved / "exports"))
                self.assertEqual(s.DEFAULTS["comparison_defaults_dir"],
                                 str(resolved / "gc_comparison_standards"))
                self.assertEqual(s.DEFAULTS["watch_dir"], "")

    def test_settings_imports_without_gc_data_dir(self):
        # Tools and tests import settings without a data folder: no config
        # file, empty path defaults, and a save refuses (logged, not raised).
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GC_DATA_DIR", None)
            import settings
            s = importlib.reload(settings)
            self.assertIsNone(s.CONFIG_PATH)
            self.assertEqual(s.DEFAULTS["distill_output"], "")
            self.assertEqual(s.DEFAULTS["comparison_defaults_dir"], "")
            conf = s.load_settings()
            self.assertEqual(conf["calibration_sensitivity"], "50")
            s.save_settings({"x": "1"})                  # must not raise
            # mock.patch.dict has restored os.environ here, but the `settings`
            # module object itself still reflects deployed mode — tearDown
            # reloads it back, outside this context, not this test method.


if __name__ == "__main__":
    unittest.main()
