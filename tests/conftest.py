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

import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

_DEPLOY_ENV_VARS = ("PORT", "GC_PORT", "GC_DATA_DIR", "GC_CAL_CDF")


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
