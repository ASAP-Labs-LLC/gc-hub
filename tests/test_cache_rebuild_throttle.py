"""Unit test for the throttled file-cache rebuild helper.

Pure logic — monkeypatches app's clock and the real rebuild so no time
passes and no CSV/CDF I/O happens. Imports app (flask-gated); the watcher is
stopped in setUp so it cannot rebuild the cache mid-test.
"""
from __future__ import annotations

import unittest

try:
    import flask  # noqa: F401
    _HAS_FLASK = True
except Exception:  # pragma: no cover
    _HAS_FLASK = False


@unittest.skipUnless(_HAS_FLASK, "flask not installed")
class CacheRebuildThrottleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app = app_module

    def setUp(self):
        self.app._watcher_stop.set()
        self.calls = []
        self._orig_rebuild = self.app._rebuild_files_cache
        self._orig_clock = self.app._monotonic
        self.app._rebuild_files_cache = lambda: self.calls.append("rebuild")
        self._now = [1000.0]
        self.app._monotonic = lambda: self._now[0]
        self.app._last_cache_rebuild = 0.0

    def tearDown(self):
        self.app._rebuild_files_cache = self._orig_rebuild
        self.app._monotonic = self._orig_clock
        self.app._watcher_stop.clear()

    def test_first_call_rebuilds(self):
        self.app._maybe_rebuild_files_cache()
        self.assertEqual(self.calls, ["rebuild"])

    def test_second_call_within_window_skips(self):
        self.app._maybe_rebuild_files_cache()
        self._now[0] += 1.0  # < CACHE_REBUILD_MIN_INTERVAL
        self.app._maybe_rebuild_files_cache()
        self.assertEqual(self.calls, ["rebuild"])

    def test_call_after_window_rebuilds(self):
        self.app._maybe_rebuild_files_cache()
        self._now[0] += self.app.CACHE_REBUILD_MIN_INTERVAL + 0.1
        self.app._maybe_rebuild_files_cache()
        self.assertEqual(self.calls, ["rebuild", "rebuild"])

    def test_force_bypasses_throttle(self):
        self.app._maybe_rebuild_files_cache()
        self._now[0] += 0.1
        self.app._maybe_rebuild_files_cache(force=True)
        self.assertEqual(self.calls, ["rebuild", "rebuild"])


if __name__ == "__main__":
    unittest.main()
