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
# both GET and POST under one path). Updated for phase 2 T4 (samples by
# sample_id; scan/rebuild/refresh removed); tests/test_route_fates.py holds
# the full fate of every route.
EXPECTED_ROUTES = {
    "/",
    "/calibration",
    "/api/analysis",
    "/api/best-fit",
    "/api/calibration",
    "/api/calibration/active",
    "/api/comparison-standard",
    "/api/comparison-standard/<name>",
    "/api/comparison-standard/rename",
    "/api/comparison-standards",
    "/api/export-analysis-report",
    "/api/export-analysis-reports-zip",
    "/api/export-comparison",
    "/api/export-lims",
    "/api/export-pdf",
    "/api/files",
    "/api/notifications",
    "/api/notifications/<notif_id>/dismiss",
    "/api/notifications/dismiss-all",
    "/api/qbench-api-credentials",
    "/api/qbench-cancel",
    "/api/qbench-credentials",
    "/api/qbench-skip-item",
    "/api/qbench-update-credentials",
    "/api/qbench-upload",
    "/api/qbench-upload-status",
    "/api/qbench-upload/stream",
    "/api/reprocess",
    "/api/reprocess/preview",
    "/api/reprocess/status",
    "/api/restart",
    "/api/samples/<int:sample_id>/distillation-curve",
    "/api/samples/<int:sample_id>/metadata",
    "/api/samples/<int:sample_id>/trace",
    "/api/save-analysis-defaults",
    "/api/server-status",
    "/api/settings",
    "/api/table",
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

    def test_sample_routes_take_a_sample_id(self) -> None:
        # Phase 2 T4: samples are addressed by id, never by file path.
        methods = _route_methods()
        for leaf in ("metadata", "trace", "distillation-curve"):
            self.assertIn("GET", methods.get(f"/api/samples/<int:sample_id>/{leaf}", set()))
        self.assertFalse([p for p in methods if "<path:" in p])


def _app_run_call() -> ast.Call:
    """The ``app.run(...)`` call in app.py's __main__ block."""
    tree = ast.parse((WEBAPP_DIR / "app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "app"
        ):
            return node
    raise AssertionError("No app.run(...) call found in app.py")


class PortBindingTests(unittest.TestCase):
    """The port must be resolvable per instance, never hardcoded.

    AST-only: importing app.py starts the hub and auto-restart threads.
    """

    def test_app_run_has_no_hardcoded_port(self) -> None:
        call = _app_run_call()
        port_kw = next((kw for kw in call.keywords if kw.arg == "port"), None)
        self.assertIsNotNone(port_kw, "app.run must pass an explicit port=")
        self.assertNotIsInstance(
            port_kw.value, ast.Constant,
            "app.run port= must not be a literal — it has to vary per instance",
        )

    def test_app_py_imports_instance(self) -> None:
        tree = ast.parse((WEBAPP_DIR / "app.py").read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertIn("instance", imported)

    def test_instance_is_imported_before_settings(self) -> None:
        # settings.CONFIG_PATH is computed from GC_PORT at import time, so the
        # port must be published to the environment first.
        source = (WEBAPP_DIR / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        instance_line = settings_line = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "instance" and instance_line is None:
                        instance_line = node.lineno
                    if alias.name == "settings" and settings_line is None:
                        settings_line = node.lineno
        self.assertIsNotNone(instance_line)
        self.assertIsNotNone(settings_line)
        self.assertLess(instance_line, settings_line)


class DeployedModeFallbackTests(unittest.TestCase):
    """Relative-path fallbacks must resolve under GC_DATA_DIR, not cwd.

    In deployed mode ``cwd`` is the updater's immutable release folder, so a
    bare relative literal like ``conf.get("distill_output", "distill_results.csv")``
    would resolve inside it. These ``conf.get(key, ...)`` fallbacks are
    defensive (``conf`` usually comes from ``settings_mod.load_settings()``,
    which always sets every ``DEFAULTS`` key). All of them must route
    through ``paths.py`` rather than a hardcoded literal.

    AST-only: importing app.py starts the hub's background threads.
    """

    # Every module with a ``conf.get(<state key>, ...)`` fallback.
    STATE_PATH_MODULES = ("app.py", "distill.py")

    # Settings keys whose value is a filesystem location that must live
    # under GC_DATA_DIR when deployed (see paths.py).
    STATE_PATH_KEYS = {
        "distill_output",
        "processed_cdf_dir",
        "export_folder",
        "comparison_defaults_dir",
        "watch_dir",
    }

    @staticmethod
    def _tree(filename: str) -> ast.Module:
        return ast.parse((WEBAPP_DIR / filename).read_text(encoding="utf-8"))

    def _conf_get_calls(self, tree: ast.Module):
        """Yield (key_node, default_node) for every ``conf.get(<literal key>, <default>)``
        or ``conf[<literal key>]``-with-fallback call, regardless of the
        surrounding variable's exact spelling, as long as it plausibly holds
        a settings dict (i.e. the attribute access is ``.get`` with exactly
        one key + one default argument, or a two-arg ``.get`` on a subscript
        target). This deliberately excludes calls on obviously-unrelated
        dicts (e.g. the directory-snapshot ``cache.get("watch_dir", ...)``
        in app.py) by requiring the receiver be named ``conf``.
        """
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "conf"
                    and len(node.args) == 2):
                continue
            key_node = node.args[0]
            if not (isinstance(key_node, ast.Constant)
                    and key_node.value in self.STATE_PATH_KEYS):
                continue
            yield node, key_node, node.args[1]

    def test_state_path_modules_import_paths(self) -> None:
        for filename in self.STATE_PATH_MODULES:
            tree = self._tree(filename)
            imported = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            self.assertIn("paths", imported, f"{filename} does not import paths")

    def test_state_path_fallbacks_are_not_bare_literals(self) -> None:
        checked = 0
        for filename in self.STATE_PATH_MODULES:
            tree = self._tree(filename)
            for node, key_node, default_node in self._conf_get_calls(tree):
                checked += 1
                self.assertNotIsInstance(
                    default_node, ast.Constant,
                    f'conf.get({key_node.value!r}, ...) at {filename}:{node.lineno} uses '
                    "a bare literal fallback — relative paths resolve inside the "
                    "release folder when GC_DATA_DIR is set; route it through paths.py",
                )
        self.assertGreater(checked, 0, "no conf.get(...) calls found for tracked state keys")


if __name__ == "__main__":
    unittest.main()
