"""The GC agent suite (fixtures in conftest.py).

A package so that its conftest registers as ``agent.conftest`` instead of
replacing the top-level ``conftest`` module that tests/test_test_isolation.py
imports. The path setup lives here too, so ``unittest discover`` can import
these modules as well as pytest."""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parents[1] / "agent"), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
