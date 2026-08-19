"""Persistent system-notification tray.

Holds system messages (e.g. "these Lab IDs weren't found during reprocess")
that must survive a server restart and remain until the user dismisses them.
Backed by a single JSON file, guarded by a lock so the Flask request threads
and background task worker can write concurrently.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger(__name__)

# Default store lives next to the user settings file (home dir), not in the
# data folder, so it is per-machine and independent of watch/processed paths.
DEFAULT_PATH = Path.home() / ".gc_viewer_notifications.json"

_VALID_LEVELS = {"info", "success", "warning", "error"}


class NotificationStore:
    """File-backed list of notifications, newest first."""

    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._items: list[dict] = self._load()

    # ── persistence ──────────────────────────────────────────────────
    def _load(self) -> list[dict]:
        if not self._path.is_file():
            return []
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
            LOGGER.warning("Notification file %s is not a list; ignoring", self._path)
        except Exception as exc:  # corrupt/unreadable — start clean, don't crash
            LOGGER.warning("Could not read notifications (%s); starting empty", exc)
        return []

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._items, fh, indent=2)
            tmp.replace(self._path)
        except Exception as exc:
            LOGGER.warning("Could not write notifications (%s)", exc)

    # ── public API ───────────────────────────────────────────────────
    def add(self, level: str, message: str) -> dict:
        """Append a notification (stored newest-first) and persist it."""
        entry = {
            "id": uuid.uuid4().hex,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "level": level if level in _VALID_LEVELS else "info",
            "message": str(message),
        }
        with self._lock:
            self._items.insert(0, entry)
            self._save()
        return entry

    def list_all(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._items]

    def dismiss(self, notif_id: str) -> bool:
        """Remove one notification by id. Returns True if it existed."""
        with self._lock:
            before = len(self._items)
            self._items = [n for n in self._items if n.get("id") != notif_id]
            removed = len(self._items) != before
            if removed:
                self._save()
            return removed

    def dismiss_all(self) -> int:
        """Clear all notifications. Returns how many were removed."""
        with self._lock:
            count = len(self._items)
            if count:
                self._items = []
                self._save()
            return count


# Module-level default instance used by the app.
_default_store: Optional[NotificationStore] = None
_default_lock = threading.Lock()


def get_store() -> NotificationStore:
    """Return the process-wide default store (lazily created)."""
    global _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = NotificationStore(DEFAULT_PATH)
        return _default_store
