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

    def test_relative_env_value_is_resolved_absolute(self):
        # The updater's launcher config or a developer's shell could set a
        # relative GC_DATA_DIR; every consumer downstream (settings.py,
        # notifications.py, app.py) compares/joins these paths, so a relative
        # one must be normalized once, here, rather than leaking cwd-relative
        # behaviour back in through the side door.
        rel = "relative/gcdata"
        self.assertTrue(paths.data_dir(env={"GC_DATA_DIR": rel}).is_absolute())
        self.assertEqual(paths.data_dir(env={"GC_DATA_DIR": rel}), Path(rel).resolve())


class DefaultWatchDirTests(unittest.TestCase):
    def test_legacy_is_cwd(self):
        self.assertEqual(paths.default_watch_dir(env={}), str(Path.cwd()))

    def test_deployed_is_empty(self):
        self.assertEqual(
            paths.default_watch_dir(env={"GC_DATA_DIR": "/srv/gcdata"}), ""
        )


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
            # paths.data_dir() resolves GC_DATA_DIR (paths.py item 5), which
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
            # mock.patch.dict has restored os.environ here, but the `settings`
            # module object itself still reflects deployed mode — tearDown
            # reloads it back, outside this context, not this test method.


if __name__ == "__main__":
    unittest.main()
