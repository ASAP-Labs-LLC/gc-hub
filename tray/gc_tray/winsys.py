"""Windows glue behind plain functions (patterns from agent/launcher.pyw and
agent/install.pyw): one tray per logon session (a named mutex in the
session's ``Local\\`` namespace, so two RDP sessions each get their own; an
flock'd file elsewhere, for tests), and autostart through ``HKCU\\...\\Run``
for the installing user only. ``winreg`` is injectable so the registry code
is tested without touching a real one."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

_IS_WIN = sys.platform == "win32"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ASAPLabs GC Hub Tray"
MUTEX_NAME = "Local\\ASAPLabs.gc-hub-tray"
ERROR_ALREADY_EXISTS = 183


class _Held:
    def __init__(self, release):
        self._release = release

    def release(self) -> None:
        if self._release:
            self._release()
            self._release = None


def acquire_single_instance(lock_dir=None, *, wait: float = 0.0) -> Optional[_Held]:
    """A held lock, or None when another tray in this logon session holds
    it (still, after ``wait`` seconds: a relaunching tray waits for the old
    one to let go). ``lock_dir`` (POSIX/tests only) is where the lock file
    goes."""
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        held = _try_acquire(lock_dir)
        if held is not None or time.monotonic() >= deadline:
            return held
        time.sleep(0.2)


def _try_acquire(lock_dir) -> Optional[_Held]:
    if _IS_WIN and lock_dir is None:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateMutexW(None, False, MUTEX_NAME)
        err = ctypes.get_last_error()
        if not h:
            return None
        if err == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(h)
            return None
        return _Held(lambda: k32.CloseHandle(h))
    d = Path(lock_dir) if lock_dir is not None else Path(tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    fh = open(str(d / "gc-hub-tray.lock"), "a+")
    try:
        if _IS_WIN:  # pragma: no cover - the Windows leg with an explicit lock_dir
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None

    def _rel():
        try:
            if _IS_WIN:  # pragma: no cover
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
    return _Held(_rel)


def pythonw_for(executable: str) -> str:
    """``pythonw.exe`` beside ``python.exe`` when there is one (no console
    window at logon), else ``executable`` unchanged."""
    p = Path(executable)
    if p.name.lower() == "python.exe":
        w = p.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(executable)


def autostart_command(python: str, script: str, *, config: Optional[str] = None) -> str:
    cmd = f'"{python}" "{script}"'
    if config:
        cmd += f' --config "{config}"'
    return cmd


def _winreg(winreg):
    if winreg is not None:
        return winreg
    if not _IS_WIN:
        return None
    import winreg as real  # pragma: no cover - Windows only
    return real


def install_autostart(cmd: str, *, winreg=None) -> bool:
    """Start the tray at this user's logon (HKCU Run). False off Windows."""
    reg = _winreg(winreg)
    if reg is None:
        return False
    with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as k:
        reg.SetValueEx(k, RUN_VALUE, 0, reg.REG_SZ, cmd)
    return True


def uninstall_autostart(*, winreg=None) -> bool:
    """Remove the Run value; False when it was not there (or off Windows)."""
    reg = _winreg(winreg)
    if reg is None:
        return False
    with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as k:
        try:
            reg.DeleteValue(k, RUN_VALUE)
        except FileNotFoundError:
            return False
    return True


def run_elevated(cmd) -> bool:  # pragma: no cover - Windows only, shows a UAC prompt
    """Run ``cmd`` as administrator via ShellExecuteW's ``runas`` verb, no
    window. False when refused (UAC "No") or off Windows. Fire and forget:
    the tray's poll shows the result."""
    if not _IS_WIN:
        return False
    import ctypes
    import subprocess
    params = subprocess.list2cmdline([str(c) for c in cmd[1:]])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(cmd[0]), params, None, 0)
    return int(rc) > 32


def default_config_path() -> Path:
    """``%APPDATA%\\ASAPLabs\\gc-hub-tray.json`` (per user, outside the
    release folder, which is replaced on every update)."""
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "ASAPLabs" / "gc-hub-tray.json"


def default_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "ASAPLabs" / "gc-hub-tray.log"
