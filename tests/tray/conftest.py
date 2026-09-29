"""The hub tray's suite. ``tray/`` is imported from there (it never imports
hub modules); stdlib + pytest only, so it runs on the Windows CI leg
without the hub's pinned requirements."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TRAY_DIR = REPO / "tray"
if str(TRAY_DIR) not in sys.path:
    sys.path.insert(0, str(TRAY_DIR))
