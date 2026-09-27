"""Shared test isolation.

Two sources of cross-test pollution this guards against:

1. ``PORT`` / ``GC_PORT`` / ``GC_DATA_DIR`` / ``GC_CAL_CDF`` — env vars that
   several modules read at *import* time (``settings.CONFIG_PATH``,
   ``settings.DEFAULTS``, ``notifications.DEFAULT_PATH``) or that
   ``instance.resolve_port()`` reads per-call. A handful of tests
   (``tests/test_export_lims_route.py``, ``test_scan_stop_sticky.py``,
   ``test_cache_rebuild_throttle.py``, ``test_reprocess_preview.py``,
   ``test_reprocess_status.py``) import ``app`` directly in-process (not via
   a subprocess), and importing ``app`` runs ``GC_PORT = instance.resolve_port()``
   followed by ``os.environ["GC_PORT"] = str(GC_PORT)`` at module scope —
   a real, permanent mutation of the live environment the first time any of
   them runs. Stripping these four vars before every test (and letting
   monkeypatch's own teardown put back whatever was there beforehand)
   neutralizes that regardless of test order: even if `import app` sets
   GC_PORT mid-test, the next test's own fixture invocation starts clean
   again.

2. ``distill._SETTINGS_CACHE`` / ``_SETTINGS_MTIME`` — module-level cache
   keyed on ``settings.CONFIG_PATH``'s mtime. A test that redirects
   ``settings.CONFIG_PATH`` (or reloads the ``settings`` module) without
   clearing this cache can see a previous test's settings dict.
"""
from __future__ import annotations

import atexit
import json
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
# Done at MODULE TOP LEVEL, before any test module is collected: settings,
# notifications and instance compute their paths from Path.home() at *import*
# time. Without this, the in-process tests that ``import app`` run in legacy
# mode against the developer's real ~/.gc_viewer_settings.json — whose
# watch_dir / processed_cdf_dir point at the live lab share — so the watcher
# scans production data and rewrites its caches. Path.home() honours HOME on
# POSIX and USERPROFILE on Windows; APPDATA and QBENCH_STORE_PATH steer
# qbench_secrets. Subprocess boots (tests/bootapp.py) inherit this and then
# set their own per-test home on top.
TEST_HOME = Path(tempfile.mkdtemp(prefix="gc-hub-test-home-")).resolve()
os.environ["HOME"] = str(TEST_HOME)
os.environ["USERPROFILE"] = str(TEST_HOME)
os.environ["APPDATA"] = str(TEST_HOME / "AppData")
os.environ["QBENCH_STORE_PATH"] = str(TEST_HOME / "qbench.json")
atexit.register(shutil.rmtree, TEST_HOME, ignore_errors=True)
# Likewise strip the deployment variables *before* collection: settings and
# notifications resolve GC_DATA_DIR / GC_PORT at import time, so a shell with
# e.g. GC_DATA_DIR pointing at a real data folder would otherwise aim every
# in-process `import app` at it (the per-test fixture below runs too late).
for _name in ("PORT", "GC_PORT", "GC_DATA_DIR", "GC_CAL_CDF"):
    os.environ.pop(_name, None)
# Legacy defaults are cwd-relative (watch_dir = cwd, processed_cdf = cwd/
# processed_cdf), i.e. the repo. Seed the temp home's settings so in-process
# `import app` watches and writes inside the temp home instead.
(TEST_HOME / "watch").mkdir()
(TEST_HOME / ".gc_viewer_settings.json").write_text(json.dumps({
    "watch_dir": str(TEST_HOME / "watch"),
    "processed_cdf_dir": str(TEST_HOME / "processed_cdf"),
    "distill_output": str(TEST_HOME / "distill_results.csv"),
}), encoding="utf-8")

_DEPLOY_ENV_VARS = ("PORT", "GC_PORT", "GC_DATA_DIR", "GC_CAL_CDF")

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
    # test — including deleting them again if a test (or an `import app`
    # side effect during it) set one that wasn't there before.
