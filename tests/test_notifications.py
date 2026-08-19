"""Tests for the persistent system-notification tray store.

System messages (e.g. "these Lab IDs weren't found") must survive a server
restart and stay until the user dismisses them, so the store is file-backed.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import notifications  # noqa: E402


class NotificationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "notifications.json"
        self.store = notifications.NotificationStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_returns_entry_with_fields(self) -> None:
        entry = self.store.add("info", "hello")
        self.assertEqual(entry["level"], "info")
        self.assertEqual(entry["message"], "hello")
        self.assertIn("id", entry)
        self.assertIn("ts", entry)

    def test_list_all_is_newest_first(self) -> None:
        self.store.add("info", "first")
        self.store.add("warning", "second")
        msgs = [n["message"] for n in self.store.list_all()]
        self.assertEqual(msgs, ["second", "first"])

    def test_dismiss_removes_one(self) -> None:
        e1 = self.store.add("info", "keep")
        e2 = self.store.add("info", "drop")
        self.assertTrue(self.store.dismiss(e2["id"]))
        remaining = [n["id"] for n in self.store.list_all()]
        self.assertEqual(remaining, [e1["id"]])

    def test_dismiss_unknown_returns_false(self) -> None:
        self.store.add("info", "keep")
        self.assertFalse(self.store.dismiss("does-not-exist"))
        self.assertEqual(len(self.store.list_all()), 1)

    def test_dismiss_all_clears_and_returns_count(self) -> None:
        self.store.add("info", "a")
        self.store.add("info", "b")
        self.assertEqual(self.store.dismiss_all(), 2)
        self.assertEqual(self.store.list_all(), [])

    def test_survives_restart(self) -> None:
        self.store.add("warning", "persisted")
        reopened = notifications.NotificationStore(self.path)
        msgs = [n["message"] for n in reopened.list_all()]
        self.assertEqual(msgs, ["persisted"])

    def test_corrupt_file_does_not_crash(self) -> None:
        self.path.write_text("{ not json", encoding="utf-8")
        store = notifications.NotificationStore(self.path)
        self.assertEqual(store.list_all(), [])
        entry = store.add("info", "after corruption")
        self.assertEqual(store.list_all()[0]["id"], entry["id"])


if __name__ == "__main__":
    unittest.main()
