"""v5.1.0: every operator path that writes an export row hands the pipeline a
notifier, so the "Lab ID LEM will misread" warning reaches the notifications
panel (Export to LIMS in app.py, backfill release in instruments_api.py).
AST only: app.py is never imported in a test."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _calls(path: Path, func: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == func:
                yield node


def _has_notifier(call) -> bool:
    return any(k.arg == "notifier" for k in call.keywords)


def test_export_to_lims_route_passes_a_notifier():
    calls = list(_calls(ROOT / "app.py", "export_to_lims"))
    assert calls and all(_has_notifier(c) for c in calls)


def test_backfill_release_route_passes_a_notifier():
    calls = [c for c in _calls(ROOT / "instruments_api.py", "release")
             if isinstance(c.func, ast.Attribute) and getattr(c.func.value, "id", "") == "ia"]
    assert calls and all(_has_notifier(c) for c in calls)


def test_instrument_admin_release_hands_it_to_the_pipeline():
    calls = list(_calls(ROOT / "instrument_admin.py", "release_backfill"))
    assert calls and all(_has_notifier(c) for c in calls)
