"""Sticky-stop behaviour: a stopped backlog must not auto-resume, but
genuinely new files still get processed. Pure-helper + route tests.

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

    def test_api_scan_clears_suppression_and_halt(self):
        with self.app._suppressed_lock:
            self.app._suppressed_paths.update({"z.cdf"})
        self.app._scan_halt.set()
        client = self.app.app.test_client()
        resp = client.post("/api/scan")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.app._scan_halt.is_set())
        with self.app._suppressed_lock:
            self.assertEqual(self.app._suppressed_paths, set())


if __name__ == "__main__":
    unittest.main()
