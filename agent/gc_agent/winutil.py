"""Windows-only probes behind plain functions, so POSIX tests can run them
(or stub them) without ctypes.windll."""
from __future__ import annotations

import sys

_IS_WIN = sys.platform == "win32"

if _IS_WIN:  # pragma: no cover - exercised on the Windows CI leg
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.restype = wintypes.BOOL
    _INVALID = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80


def can_open_exclusively(path):
    """True when nothing else has *path* open (Windows: CreateFileW with
    share mode 0 succeeds). ChemStation keeps a CDF open while writing it.
    On POSIX there is no such lock, so this is always True and the
    stable-seconds rule alone decides."""
    if not _IS_WIN:
        return True
    h = _k32.CreateFileW(str(path), _GENERIC_READ, 0, None, _OPEN_EXISTING,
                         _FILE_ATTRIBUTE_NORMAL, None)
    if h is None or h == _INVALID:
        return False
    _k32.CloseHandle(h)
    return True
