"""Every route the app registers has a fate in the T4 plan, and vice versa.

The route-fate table in ``docs/superpowers/plans/2026-09-28-phase2-T4-routes.md``
(between the ``route-fates`` markers) is the single list: this test derives
the registered ``(path, methods)`` by AST (never ``import app``) and asserts
it equals every row whose fate is not ``removed``, and that no ``removed``
path is registered. So no route can be forgotten, added without a fate, or
come back after removal.

"Registered" covers ``@app.route`` and ``app.add_url_rule`` in app.py, and
every Blueprint app.py registers (``app.register_blueprint(mod.bp)``):
``@bp.route`` / ``bp.add_url_rule`` in that module, with module-level string
constants (e.g. ``SETUP_PATH``) resolved.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "docs" / "superpowers" / "plans" / "2026-09-28-phase2-T4-routes.md"
FATES = {"migrated", "unchanged", "removed", "new"}


def _constants(tree: ast.Module) -> dict:
    """Module-level ``NAME = "string"`` assignments."""
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            out[node.targets[0].id] = node.value.value
    return out


def _path_of(expr, consts: dict):
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name) and expr.id in consts:
        return consts[expr.id]
    raise AssertionError(f"route path is not a literal or a module constant: {ast.dump(expr)}")


def _methods(call: ast.Call) -> set:
    for kw in call.keywords:
        if kw.arg == "methods":
            return {e.value for e in kw.value.elts}
    return {"GET"}


def _routes_in(tree: ast.Module, owners: set) -> dict:
    """``{path: methods}`` for ``@<owner>.route(...)`` decorators and
    ``<owner>.add_url_rule(...)`` calls, ``owner`` in ``owners``."""
    consts = _constants(tree)
    out: dict[str, set] = {}

    def is_owner(func) -> bool:
        return (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id in owners)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and is_owner(dec.func) and dec.func.attr == "route"
                        and dec.args):
                    out.setdefault(_path_of(dec.args[0], consts), set()).update(_methods(dec))
        elif (isinstance(node, ast.Call) and is_owner(node.func)
              and node.func.attr == "add_url_rule" and node.args):
            out.setdefault(_path_of(node.args[0], consts), set()).update(_methods(node))
    return out


def _blueprint_names(tree: ast.Module) -> set:
    names = set()
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", getattr(node.value.func, "attr", None)) == "Blueprint"):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def registered_routes() -> dict[str, frozenset]:
    """``{path: methods}`` for every route the app registers."""
    app_tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    out = _routes_in(app_tree, {"app"})
    for node in ast.walk(app_tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register_blueprint" and node.args
                and isinstance(node.args[0], ast.Attribute)
                and isinstance(node.args[0].value, ast.Name)):
            module = node.args[0].value.id
            tree = ast.parse((ROOT / f"{module}.py").read_text(encoding="utf-8"))
            bps = _blueprint_names(tree)
            assert node.args[0].attr in bps, f"{module}.{node.args[0].attr} is not a Blueprint"
            for path, methods in _routes_in(tree, bps).items():
                out.setdefault(path, set()).update(methods)
    return {p: frozenset(m) for p, m in out.items()}


def plan_table() -> list[tuple[str, frozenset, str]]:
    text = PLAN.read_text(encoding="utf-8")
    m = re.search(r"<!-- route-fates:begin -->(.*?)<!-- route-fates:end -->", text, re.S)
    assert m, "route-fates markers missing from the T4 plan"
    rows = []
    for line in m.group(1).splitlines():
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if len(cells) < 3 or not cells[0].startswith("`"):
            continue
        path = cells[0].strip("`")
        methods = frozenset(x.strip() for x in cells[1].split(","))
        rows.append((path, methods, cells[2]))
    return rows


class RouteFateTests(unittest.TestCase):
    def test_table_is_well_formed(self):
        rows = plan_table()
        self.assertGreater(len(rows), 40)
        paths = [r[0] for r in rows]
        self.assertEqual(len(paths), len(set(paths)), "a route is listed twice")
        for path, methods, fate in rows:
            self.assertIn(fate, FATES, path)
            self.assertTrue(methods <= {"GET", "POST", "PUT", "PATCH", "DELETE"}, path)

    def test_registered_routes_equal_the_table(self):
        live = {(p, m) for p, m, fate in plan_table() if fate != "removed"}
        self.assertEqual(set(registered_routes().items()), live)

    def test_blueprint_routes_are_seen(self):
        registered = registered_routes()
        for path in ("/api/ingest", "/api/agent/heartbeat", "/api/admin/setup",
                     "/api/lem/machines"):
            self.assertIn(path, registered)

    def test_lem_machines_is_a_get_only_new_route(self):
        rows = {p: (m, fate) for p, m, fate in plan_table()}
        self.assertEqual(rows.get("/api/lem/machines"), (frozenset({"GET"}), "new"))
        self.assertEqual(registered_routes().get("/api/lem/machines"), frozenset({"GET"}))

    def test_removed_routes_are_not_registered(self):
        registered = registered_routes()
        removed = [p for p, _m, fate in plan_table() if fate == "removed"]
        self.assertTrue(removed)
        for path in removed:
            self.assertNotIn(path, registered, f"{path} should be removed")

    def test_no_route_takes_a_file_path(self):
        for path in registered_routes():
            self.assertNotIn("<path:", path)


if __name__ == "__main__":
    unittest.main()
