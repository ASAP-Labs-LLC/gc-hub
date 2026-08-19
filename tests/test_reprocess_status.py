"""Reprocess progress-reporting regression guard.

The bug: the browser reprocess toast polled ``/api/scan/status`` while
``_do_reprocess`` never wrote to ``_scan_status`` — so the first poll saw the
watcher's idle/0 state and the toast turned green showing "0 processed, 0
errors" no matter what reprocess actually did. And ``_scan_status`` can't be
shared with the watcher, which rewrites it (resetting ``processed=0``) every
``WATCHER_POLL_SECONDS``.

This test pins the fix: reprocess has its own status, decoupled from the
watcher, exposed at ``/api/reprocess/status`` and reflecting the real result.

Imports ``app`` (needs flask); skipped automatically if flask is absent. The
background watcher is stopped in setUp so it can't race the assertions.
"""

from __future__ import annotations

import unittest

try:
    import flask  # noqa: F401
    _HAS_FLASK = True
except Exception:  # pragma: no cover - dep-absent path
    _HAS_FLASK = False


@unittest.skipUnless(_HAS_FLASK, "flask not installed")
class ReprocessStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import app as app_module
        cls.app_module = app_module

    def setUp(self) -> None:
        app_module = self.app_module
        # Stop the 24/7 watcher so it can't clobber status during the test.
        app_module._watcher_stop.set()

        class _FakeLooker:
            """Returns a deterministic reprocess result without touching disk."""

            def reprocess_samples(self, samples, stop_event=None):
                return {s: {"status": "ok", "path": f"{s}.CDF"} for s in samples}

        self._orig_get_looker = app_module._get_looker
        self._orig_rebuild = app_module._rebuild_files_cache
        app_module._get_looker = lambda: _FakeLooker()
        app_module._rebuild_files_cache = lambda: []  # avoid disk side effects
        self.client = app_module.app.test_client()

    def tearDown(self) -> None:
        self.app_module._get_looker = self._orig_get_looker
        self.app_module._rebuild_files_cache = self._orig_rebuild

    def _run_reprocess(self, samples):
        resp = self.client.post("/api/reprocess", json={"samples": samples})
        self.assertEqual(resp.status_code, 200)
        # Drain the background task worker so the reprocess actually completes.
        self.app_module._task_queue.join()

    def test_reprocess_status_endpoint_exists(self):
        resp = self.client.get("/api/reprocess/status")
        self.assertEqual(
            resp.status_code, 200,
            "reprocess needs its own status endpoint, decoupled from the watcher",
        )

    def test_completed_reprocess_reports_real_counts(self):
        self._run_reprocess(["2025-0001", "2025-0002"])
        st = self.client.get("/api/reprocess/status").get_json()
        self.assertEqual(st["phase"], "done")
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["processed"], 2)
        self.assertEqual(st["errors"], 0)

    def test_reprocess_status_is_not_the_watcher_scan_status(self):
        # The whole point: reprocess must not depend on _scan_status, which the
        # watcher rewrites (processed=0) every poll.
        self.app_module._scan_status.update(phase="idle", processed=0, total=0)
        self._run_reprocess(["2025-0001"])
        st = self.client.get("/api/reprocess/status").get_json()
        self.assertEqual(st["processed"], 1)
        self.assertEqual(st["phase"], "done")


if __name__ == "__main__":
    unittest.main()
