"""The agent runs on the GC PCs' Python, which may be as old as 3.9, and
ships on its own: it must never import hub modules."""
import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCES = sorted(list((REPO / "agent").rglob("*.py")) + list((REPO / "agent").glob("*.pyw"))
                 + [REPO / "scripts" / "build_agent_zip.py"])
HUB_MODULES = {p.stem for p in REPO.glob("*.py")}
STDLIB_OK_THIRD_PARTY = {"pystray", "PIL"}


def test_sources_found():
    names = {p.name for p in SOURCES}
    assert {"agent_main.py", "launcher.pyw", "install.pyw", "core.py"} <= names


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_parses_as_python_3_9(path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path), feature_version=(3, 9))
    for node in ast.walk(tree):
        assert type(node).__name__ not in ("Match", "TryStar"), path
        # PEP 604 unions are only safe inside postponed annotations
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            for side in (node.left, node.right):
                assert not (isinstance(side, ast.Constant) and side.value is None), \
                    "%s:%d uses X | None at runtime" % (path, node.lineno)


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_imports_only_stdlib_own_package_or_tray_deps(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods = [node.module.split(".")[0]]
        else:
            continue
        for m in mods:
            if m == "gc_agent" or m in STDLIB_OK_THIRD_PARTY:
                continue
            assert m not in HUB_MODULES, "%s imports hub module %s" % (path.name, m)
            if hasattr(sys, "stdlib_module_names"):
                assert m in sys.stdlib_module_names, "%s imports non-stdlib %s" % (path.name, m)
