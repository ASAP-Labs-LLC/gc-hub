"""Route-surface regression guard for app.py.

Parses app.py with the ``ast`` module (no import, so it never triggers app.py's
import-time Looker watcher / auto-restart threads) and asserts every expected
``@app.route`` is still registered. This locks the API surface so the
consolidation — and future edits — cannot silently drop an endpoint the
single-page frontend depends on.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent

# The distinct route paths the frontend consumes (── /api/settings serves
# both GET and POST under one path). Sourced from the live, feature-complete
# app.py at consolidation time.
EXPECTED_ROUTES = {
    "/",
    "/calibration",
    "/api/analysis",
    "/api/best-fit",
    "/api/browse",
    "/api/calibration",
    "/api/calibration/active",
    "/api/comparison-standard",
    "/api/comparison-standard/<name>",
    "/api/comparison-standard/rename",
    "/api/comparison-standards",
    "/api/distillation-curve",
    "/api/export-analysis-report",
    "/api/export-analysis-reports-zip",
    "/api/export-comparison",
    "/api/export-lims",
    "/api/export-pdf",
    "/api/files",
    "/api/files/refresh",
    "/api/library/reindex-times",
    "/api/metadata/<path:filepath>",
    "/api/notifications",
    "/api/notifications/<notif_id>/dismiss",
    "/api/notifications/dismiss-all",
    "/api/open-folder",
    "/api/qbench-cancel",
    "/api/qbench-credentials",
    "/api/qbench-skip-item",
    "/api/qbench-update-credentials",
    "/api/qbench-upload",
    "/api/qbench-upload-status",
    "/api/qbench-upload/stream",
    "/api/rebuild-db",
    "/api/reprocess",
    "/api/reprocess/preview",
    "/api/reprocess/status",
    "/api/restart",
    "/api/save-analysis-defaults",
    "/api/scan",
    "/api/scan/status",
    "/api/scan/stream",
    "/api/server-status",
    "/api/settings",
    "/api/stop-scan",
    "/api/table",
    "/api/trace",
}


def _route_methods() -> dict[str, set[str]]:
    """Map each registered route path to the set of HTTP methods declared."""
    tree = ast.parse((WEBAPP_DIR / "app.py").read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "route"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                path = dec.args[0].value
                methods = {"GET"}
                for kw in dec.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = {
                            el.value for el in kw.value.elts
                            if isinstance(el, ast.Constant)
                        }
                out.setdefault(path, set()).update(methods)
    return out


def _registered_routes() -> set[str]:
    return set(_route_methods())


class RouteSurfaceTests(unittest.TestCase):
    def test_no_expected_route_was_dropped(self) -> None:
        registered = _registered_routes()
        missing = EXPECTED_ROUTES - registered
        self.assertEqual(
            missing, set(),
            f"app.py is missing expected route(s): {sorted(missing)}",
        )

    def test_qbench_upload_route_present(self) -> None:
        # The endpoint at the heart of the bug we fixed.
        self.assertIn("/api/qbench-upload", _registered_routes())

    def test_calibration_api_supports_get_and_post(self) -> None:
        methods = _route_methods().get("/api/calibration", set())
        self.assertIn("GET", methods, "/api/calibration must serve GET (load peaks)")
        self.assertIn("POST", methods, "/api/calibration must serve POST (save assignments)")

    def test_calibration_page_route_present(self) -> None:
        self.assertIn("/calibration", _route_methods())

    def test_reprocess_preview_is_post(self) -> None:
        self.assertIn("POST", _route_methods().get("/api/reprocess/preview", set()))

    def test_notification_routes_present(self) -> None:
        methods = _route_methods()
        self.assertIn("GET", methods.get("/api/notifications", set()))
        self.assertIn("POST", methods.get("/api/notifications/<notif_id>/dismiss", set()))
        self.assertIn("POST", methods.get("/api/notifications/dismiss-all", set()))

    def test_library_reindex_is_post(self) -> None:
        self.assertIn("POST", _route_methods().get("/api/library/reindex-times", set()))


if __name__ == "__main__":
    unittest.main()
