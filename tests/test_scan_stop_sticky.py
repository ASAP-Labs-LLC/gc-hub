"""Sticky-stop behaviour: a stopped backlog must not auto-resume, but
genuinely new files still get processed. Pure-helper tests of the legacy
watcher (removed with it in T5; the /api/scan route is gone since T4).

Imports app (flask-gated); the watcher is stopped in setUp.
"""
from __future__ import annotations

import unittest

try:
    import flask  # noqa: F401
    _HAS_FLASK = True
except Exception:  # pragma: no cover
    _HAS_FLASK = False


@unittest.skipUnless(_HAS_FLASK, "flask not installed")
class ScanStopStickyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app = app_module

    def setUp(self):
        self.app._watcher_stop.set()
        with self.app._suppressed_lock:
            self.app._suppressed_paths.clear()
        self.app._scan_halt.clear()

    def tearDown(self):
        with self.app._suppressed_lock:
            self.app._suppressed_paths.clear()
        self.app._scan_halt.clear()
        self.app._watcher_stop.clear()

    def test_filter_excludes_seen_and_suppressed(self):
        seen = {"a.cdf"}
        with self.app._suppressed_lock:
            self.app._suppressed_paths.update({"b.cdf"})
        out = self.app._filter_candidates(["a.cdf", "b.cdf", "c.cdf"], seen)
        self.assertEqual(out, ["c.cdf"])  # a seen, b suppressed, c is new

    def test_suppress_backlog_records_unprocessed(self):
        self.app._suppress_backlog(["x.cdf", "y.cdf"])
        with self.app._suppressed_lock:
            self.assertEqual(self.app._suppressed_paths, {"x.cdf", "y.cdf"})


if __name__ == "__main__":
    unittest.main()
