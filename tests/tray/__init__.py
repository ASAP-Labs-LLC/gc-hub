"""The hub tray suite (tray/). A package so that its conftest registers as
``tray.conftest`` instead of replacing the top-level ``conftest`` module
(see tests/agent/__init__.py). The path setup lives here too, so ``unittest
discover`` can import these modules as well as pytest."""
import sys
from pathlib import Path

_TRAY = Path(__file__).resolve().parents[2] / "tray"
if str(_TRAY) not in sys.path:
    sys.path.insert(0, str(_TRAY))
