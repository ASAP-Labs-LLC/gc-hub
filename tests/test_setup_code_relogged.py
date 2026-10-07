"""v3.0.0 carry-over: the one-time admin setup code is logged at WARNING in
``app.log`` at EVERY start until the password is set (not only the start that
created it), so whoever opens app.log after a restart still finds it; once the
password is set it is never logged again and the code file is gone.

Boots the real app.py (twice, same data folder), the way the updater does.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("flask")

from bootapp import booted, setup_admin, wait_for  # noqa: E402

LINE = "Admin setup code: "


def _log(data: Path) -> str:
    p = data / "app.log"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def _code_lines(data: Path, code: str) -> int:
    return sum(1 for ln in _log(data).splitlines()
               if "WARNING" in ln and f"{LINE}{code}" in ln)


def test_setup_code_is_logged_at_every_start_until_the_password_is_set(tmp_path):
    with booted(tmp_path) as (_port, _proc, data, _home):
        code_file = data / "admin-setup-code.txt"
        assert wait_for(code_file.exists, timeout=10)
        code = code_file.read_text(encoding="utf-8").strip()
        assert wait_for(lambda: _code_lines(data, code) == 1, timeout=10), _log(data)[-2000:]

    with booted(tmp_path) as (port, _proc, data, _home):
        # the same code (the file is kept), logged again by this start
        assert (data / "admin-setup-code.txt").read_text(encoding="utf-8").strip() == code
        assert wait_for(lambda: _code_lines(data, code) == 2, timeout=10), _log(data)[-2000:]
        setup_admin(port, data)
        assert not (data / "admin-setup-code.txt").exists()

    with booted(tmp_path) as (_port, _proc, data, _home):
        # booted() returns once /healthz answers: the blueprint's start-up hook has run
        assert _code_lines(data, code) == 2
        assert LINE not in _log(data).split(f"{LINE}{code}")[-1], "a code logged after setup"
        assert not (data / "admin-setup-code.txt").exists()
