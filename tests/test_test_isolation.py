"""Guard: the test run never sees the developer's real home directory.

``tests/conftest.py`` redirects HOME (and friends) at import time, so
nothing a test runs (QBench secrets, anything that falls back to the home
folder) reads or writes the developer's real one, and strips GC_DATA_DIR so a
shell pointing at a real data folder can't aim tests at it. These tests pin
that.
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


def test_import_time_state_paths_point_at_no_real_data_folder():
    # No GC_DATA_DIR in the test process: the import-time locations are
    # unset rather than aimed at a real data folder.
    import notifications
    import settings

    assert "GC_DATA_DIR" not in os.environ
    assert settings.CONFIG_PATH is None or _under(settings.CONFIG_PATH, conftest.TEST_HOME)
    assert (notifications.DEFAULT_PATH is None
            or _under(notifications.DEFAULT_PATH, conftest.TEST_HOME))
