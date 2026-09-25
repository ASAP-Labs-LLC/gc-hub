"""run.pyw: the legacy tray launcher must not hand the Flask subprocess a
stray ``PORT`` env var.

``instance.resolve_port()`` only honours ``PORT`` when ``GC_DATA_DIR`` is set
(deployed mode). ``run.pyw`` never sets ``GC_DATA_DIR`` — it's the legacy
share launcher, port picked from its own dialog/``GC_PORT`` — so ``PORT``
should never matter here. But ``subprocess.Popen`` with no ``env=`` inherits
the launcher's full environment, and if the launcher's own process ever
picked up a stray ``PORT`` (e.g. a leftover shell var, or a future world
where the same box also runs the updater), the spawned Flask process would
see it. Since the current ``resolve_port()`` gate already protects legacy
mode, this is defence in depth, not a live bug — but it costs nothing to
close.

AST-only: run.pyw imports pystray/ctypes.windll and doesn't run outside
Windows, so it can never be imported by the test suite.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent


def _tree() -> ast.Module:
    return ast.parse((WEBAPP_DIR / "run.pyw").read_text(encoding="utf-8"))


def _find_popen_call(tree: ast.Module) -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Popen"):
            return node
    raise AssertionError("no subprocess.Popen(...) call found in run.pyw")


class ChildEnvExcludesPortTests(unittest.TestCase):
    def test_popen_passes_an_explicit_env(self) -> None:
        call = _find_popen_call(_tree())
        env_kw = next((kw for kw in call.keywords if kw.arg == "env"), None)
        self.assertIsNotNone(
            env_kw,
            "subprocess.Popen(...) for the Flask subprocess must pass an "
            "explicit env= (a filtered copy of os.environ), not inherit it "
            "unmodified",
        )

    def test_source_strips_port_before_launch(self) -> None:
        source = (WEBAPP_DIR / "run.pyw").read_text(encoding="utf-8")
        self.assertIn(
            'pop("PORT"', source,
            "run.pyw must remove PORT from the Flask subprocess's env "
            "before launch — PORT is the updater's handshake, not the "
            "legacy launcher's, and must never leak into a share copy",
        )


if __name__ == "__main__":
    unittest.main()
