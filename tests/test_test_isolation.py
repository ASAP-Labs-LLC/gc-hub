"""Guard: the test run never sees the developer's real home directory.

In legacy mode (no GC_DATA_DIR) settings, notifications and caches live in
``Path.home()``. The in-process tests that ``import app`` would otherwise load
the developer's real ``~/.gc_viewer_settings.json`` — whose watch_dir and
processed_cdf_dir point at the live lab share — and the watcher would scan it
and rewrite production caches. ``tests/conftest.py`` redirects HOME (and
friends) at import time; these tests pin that.
"""
import os
from pathlib import Path

import conftest


def _under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def test_home_is_the_session_temp_dir():
    assert Path.home().resolve() == conftest.TEST_HOME.resolve()
    for var in ("HOME", "USERPROFILE"):
        assert Path(os.environ[var]).resolve() == conftest.TEST_HOME.resolve(), var
    assert _under(Path(os.environ["APPDATA"]), conftest.TEST_HOME)
    assert _under(Path(os.environ["QBENCH_STORE_PATH"]), conftest.TEST_HOME)


def test_import_time_state_paths_are_under_the_temp_home():
    import notifications
    import settings

    assert _under(settings.CONFIG_PATH, conftest.TEST_HOME), settings.CONFIG_PATH
    assert _under(notifications.DEFAULT_PATH, conftest.TEST_HOME), notifications.DEFAULT_PATH
