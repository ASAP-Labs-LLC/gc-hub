"""Integration guard for the reprocess-preview + notification wiring.

Confirms the Flask routes expand a query against the live file cache and that
skipped (missing) Lab IDs land in the persistent notification tray. The pure
expansion logic is unit-tested in test_reprocess_query.py; this pins the wiring.

Imports ``app`` (needs flask); skipped if flask is absent. The watcher is
stopped in setUp so it can't repopulate the file cache mid-test.
"""

from __future__ import annotations

import unittest

try:
    import flask  # noqa: F401
    _HAS_FLASK = True
except Exception:  # pragma: no cover
    _HAS_FLASK = False


@unittest.skipUnless(_HAS_FLASK, "flask not installed")
class ReprocessPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import app as app_module
        cls.app_module = app_module

    def setUp(self) -> None:
        app_module = self.app_module
        app_module._watcher_stop.set()
        # Pin a known library so preview expansion is deterministic.
        with app_module._files_cache_lock:
            app_module._files_cache = [
                {"name": "34562", "path": "a", "mtime": 1.0},
                {"name": "34563", "path": "b", "mtime": 2.0},
                {"name": "34565", "path": "c", "mtime": 3.0},
            ]
        app_module._files_cache_ready.set()
        self.client = app_module.app.test_client()

    def test_range_preview_returns_matched_and_missing(self) -> None:
        resp = self.client.post("/api/reprocess/preview", json={"query": "34562 to 34565"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["matched"], ["34562", "34563", "34565"])
        self.assertEqual(data["missing"], ["34564"])

    def test_oversized_range_is_rejected(self) -> None:
        resp = self.client.post("/api/reprocess/preview", json={"query": "1 to 99999999"})
        data = resp.get_json()
        self.assertIn("error", data)

    def test_missing_ids_create_a_notification(self) -> None:
        store = self.app_module.notifications_mod.get_store()
        store.dismiss_all()
        resp = self.client.post(
            "/api/reprocess",
            json={"samples": [], "missing": ["34564", "34999"]},
        )
        self.assertEqual(resp.status_code, 200)
        notes = self.client.get("/api/notifications").get_json()
        self.assertTrue(any("not found" in n["message"] for n in notes))
        self.assertTrue(any("34564" in n["message"] for n in notes))

    def test_notification_dismiss_roundtrip(self) -> None:
        store = self.app_module.notifications_mod.get_store()
        store.dismiss_all()
        entry = store.add("info", "dismiss me")
        resp = self.client.post(f"/api/notifications/{entry['id']}/dismiss")
        self.assertTrue(resp.get_json()["removed"])
        self.assertEqual(self.client.get("/api/notifications").get_json(), [])


if __name__ == "__main__":
    unittest.main()
