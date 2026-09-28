"""Every route app.py registers has a fate in the T4 plan, and vice versa.

The route-fate table in ``docs/superpowers/plans/2026-09-28-phase2-T4-routes.md``
(between the ``route-fates`` markers) is the single list: this test derives
the registered ``(path, methods)`` from app.py by AST (never ``import app``)
and asserts it equals every row whose fate is not ``removed``, and that no
``removed`` path is registered. So no route can be forgotten, added without
a fate, or come back after removal.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "docs" / "superpowers" / "plans" / "2026-09-28-phase2-T4-routes.md"
FATES = {"migrated", "unchanged", "removed", "new"}


def registered_routes() -> dict[str, frozenset]:
    """``{path: methods}`` for every ``@app.route`` in app.py (GET by default)."""
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    out: dict[str, set] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "route" and isinstance(dec.func.value, ast.Name)
                    and dec.func.value.id == "app" and dec.args
                    and isinstance(dec.args[0], ast.Constant)):
                methods = {"GET"}
                for kw in dec.keywords:
                    if kw.arg == "methods":
                        methods = {e.value for e in kw.value.elts}
                out.setdefault(dec.args[0].value, set()).update(methods)
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
