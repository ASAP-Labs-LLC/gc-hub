"""Shared test isolation.

Two sources of cross-test pollution this guards against:

1. ``PORT`` / ``GC_PORT`` / ``GC_DATA_DIR`` / ``GC_CAL_CDF`` — env vars that
   several modules read at *import* time (``settings.CONFIG_PATH``,
   ``settings.DEFAULTS``, ``notifications.DEFAULT_PATH``) or per call
   (``instance.resolve_port()``, ``paths``). No test imports ``app``
   in-process (it refuses to start without GC_DATA_DIR and starts the hub's
   threads); route tests boot it in a subprocess (``tests/bootapp.py``) with
   their own data folder. Stripping these vars before every test (and
   letting monkeypatch's own teardown put back whatever was there
   beforehand) keeps a test that sets one from leaking into the next.

2. ``distill._SETTINGS_CACHE`` / ``_SETTINGS_MTIME`` — module-level cache
   keyed on ``settings.CONFIG_PATH``'s mtime. A test that redirects
   ``settings.CONFIG_PATH`` (or reloads the ``settings`` module) without
   clearing this cache can see a previous test's settings dict.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

# ── A throwaway home for the whole session ────────────────────────────────
# Done at MODULE TOP LEVEL, before any test module is collected, so nothing a
# test runs can read or write the developer's real home folder (QBench
# secrets resolve APPDATA / QBENCH_STORE_PATH; Path.home() honours HOME on
# POSIX and USERPROFILE on Windows). Subprocess boots (tests/bootapp.py)
# inherit this and then set their own per-test home on top.
TEST_HOME = Path(tempfile.mkdtemp(prefix="gc-hub-test-home-")).resolve()
os.environ["HOME"] = str(TEST_HOME)
os.environ["USERPROFILE"] = str(TEST_HOME)
os.environ["APPDATA"] = str(TEST_HOME / "AppData")
os.environ["QBENCH_STORE_PATH"] = str(TEST_HOME / "qbench.json")
atexit.register(shutil.rmtree, TEST_HOME, ignore_errors=True)
# Likewise strip the deployment variables *before* collection: settings and
# notifications resolve GC_DATA_DIR at import time, so a shell with e.g.
# GC_DATA_DIR pointing at a real data folder would otherwise aim them at it
# (the per-test fixture below runs too late).
for _name in ("PORT", "GC_PORT", "GC_DATA_DIR", "GC_CAL_CDF"):
    os.environ.pop(_name, None)

_DEPLOY_ENV_VARS = ("PORT", "GC_PORT", "GC_DATA_DIR", "GC_CAL_CDF", "LEM_URL")

# ── No silent skips where the deps are supposed to be there ──────────────
# Tests that need numpy/netCDF4/flask/... skip when those are absent, which is
# right on a bare interpreter and wrong on CI: a broken install there would
# turn most of the suite into skips and still go green. CI sets
# GC_REQUIRE_DEPS=1, which makes any missing runtime dependency end the session
# before a single test runs.
REQUIRED_DEPS = ("flask", "netCDF4", "numpy", "scipy", "pandas", "plotly", "jwt",
                 "requests", "xhtml2pdf")


def _missing_required_deps() -> list:
    import importlib

    missing = []
    for mod in REQUIRED_DEPS:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # ImportError, or a broken binary wheel
            missing.append(f"{mod} ({type(exc).__name__}: {exc})")
    return missing


if os.environ.get("GC_REQUIRE_DEPS", "").strip() not in ("", "0"):
    _missing = _missing_required_deps()
    if _missing:
        pytest.exit("GC_REQUIRE_DEPS is set but these runtime dependencies cannot be "
                    "imported: " + "; ".join(_missing), returncode=3)


def _clear_distill_settings_cache() -> None:
    try:
        import distill
    except Exception:
        # distill.py needs numpy/netCDF4/scipy; tests that don't need it
        # (and environments without those deps) must not be broken by this
        # fixture merely trying to tidy up after it.
        return
    distill._SETTINGS_CACHE = None
    distill._SETTINGS_MTIME = None


@pytest.fixture(autouse=True)
def _clean_deploy_env(monkeypatch):
    """Give every test a deployment-env-free start, and clean up after it."""
    for name in _DEPLOY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    _clear_distill_settings_cache()

    yield

    _clear_distill_settings_cache()
    # monkeypatch's own teardown (which runs after this fixture's teardown
    # code executes, since delenv() registered its undo first) restores
    # PORT/GC_PORT/GC_DATA_DIR/GC_CAL_CDF to whatever they were before this
    # test — including deleting them again if a test set one that wasn't
    # there before.
