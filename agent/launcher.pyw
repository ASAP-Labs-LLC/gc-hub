"""GC agent launcher: the resident supervisor. It is never self-updated, so
it is kept small and stdlib-only.

    pythonw launcher.pyw [--root <agent root>]      (root defaults to this file's folder)

* Holds a single-instance lock: a named mutex on Windows (ctypes), an fcntl
  lock on ``launcher.lock`` elsewhere. A second launcher exits 0 at once.
* Runs ``versions/<current.txt>/agent_main.py --root <root>`` as a child with
  the interpreter recorded in ``agent.json`` ("python"; the installer writes
  the pythonw.exe it ran under).
* Child exit 0 → quit. 3 → restart, re-reading ``current.txt``, at most one
  start per second; after 5 restarts in a row that each ran under 30 s it
  backs off (2 s doubling to 60 s), so an exit-3 loop cannot spin. Anything
  else is a crash → restart with backoff (1 s doubling to 60 s).
* A version that ``current.txt`` newly switched to and that crashes three
  times, each within 30 s of starting, is reverted to ``previous.txt``. The
  revert is written to ``revert.json`` (the agent reports it in its next
  heartbeat) and its package sha256 to ``bad_packages.json`` (so the updater
  does not install it again).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EXIT_QUIT = 0
EXIT_RESTART = 3
QUICK_CRASH_SECONDS = 30
CRASHES_TO_REVERT = 3
BACKOFF_MAX = 60
RESTART_FLOOR = 1.0
RAPID_RESTARTS_FREE = 5
MISSING_VERSION_WAIT = 30
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_IS_WIN = sys.platform == "win32"

log = logging.getLogger("gc_launcher")


# ── small file helpers ───────────────────────────────────────────────────
def atomic_write_text(path, text, retries=10):
    """Temp file + fsync + os.replace, retried briefly on Windows sharing
    violations (another process has the target open for a moment)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp, str(path))
                break
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_pointer(root, name):
    try:
        v = (Path(root) / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return v if _VERSION_RE.match(v) else ""


def local_iso():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ── single instance ──────────────────────────────────────────────────────
class _Held:
    def __init__(self, release):
        self._release = release

    def release(self):
        if self._release:
            self._release()
            self._release = None


def _lock_name(root):
    key = os.path.normcase(str(Path(root).resolve()))
    return "Local\\ASAPLabs.gc-agent.launcher." + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def acquire_single_instance(root):
    """Return a held lock, or None when another launcher holds it."""
    if _IS_WIN:  # pragma: no cover - Windows CI leg
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateMutexW(None, False, _lock_name(root))
        err = ctypes.get_last_error()
        if not h:
            return None
        if err == 183:  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return None
        return _Held(lambda: k32.CloseHandle(h))
    import fcntl
    Path(root).mkdir(parents=True, exist_ok=True)
    fh = open(str(Path(root) / "launcher.lock"), "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None

    def _rel():
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
    return _Held(_rel)


# ── supervisor ───────────────────────────────────────────────────────────
def _spawn(cmd, cwd):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WIN else 0
    try:
        return subprocess.call(cmd, cwd=cwd, creationflags=flags)
    except OSError as exc:
        log.error("cannot start %s: %s", cmd[0], exc)
        return 127


class Launcher:
    def __init__(self, root, sleep=time.sleep, clock=time.monotonic, spawn=_spawn):
        self.root = Path(root)
        self.sleep = sleep
        self.clock = clock
        self.spawn = spawn

    def python(self):
        try:
            p = json.loads((self.root / "agent.json").read_text(encoding="utf-8")).get("python")
        except (OSError, ValueError, AttributeError):
            p = None
        return p if isinstance(p, str) and p and os.path.exists(p) else sys.executable

    def _version_dir(self, v):
        return self.root / "versions" / v

    def _revert(self, bad):
        prev = read_pointer(self.root, "previous.txt")
        if not prev or prev == bad or not (self._version_dir(prev) / "agent_main.py").is_file():
            log.error("version %s keeps crashing and there is no previous version to revert to", bad)
            return False
        try:
            sha = (self._version_dir(bad) / "PACKAGE_SHA256").read_text(encoding="utf-8").strip()
        except OSError:
            sha = ""
        atomic_write_text(self.root / "current.txt", prev + "\n")
        atomic_write_text(self.root / "revert.json", json.dumps(
            {"from": bad, "to": prev, "at": local_iso(), "package_sha256": sha}) + "\n")
        if sha:
            try:
                bads = json.loads((self.root / "bad_packages.json").read_text(encoding="utf-8"))
                if not isinstance(bads, list):
                    bads = []
            except (OSError, ValueError):
                bads = []
            if sha not in bads:
                bads.append(sha)
            atomic_write_text(self.root / "bad_packages.json", json.dumps(bads) + "\n")
        log.error("reverted from %s to %s after %d quick crashes", bad, prev, CRASHES_TO_REVERT)
        return True

    def run(self):
        running = None
        newly_switched = False
        quick_crashes = 0
        rapid_restarts = 0
        backoff = 1
        while True:
            v = read_pointer(self.root, "current.txt")
            main = self._version_dir(v) / "agent_main.py" if v else None
            if main is None or not main.is_file():
                log.error("no runnable agent version (current.txt=%r); waiting", v)
                self.sleep(MISSING_VERSION_WAIT)
                continue
            if running is not None and v != running:
                newly_switched, quick_crashes, rapid_restarts = True, 0, 0
                log.info("switched from %s to %s", running, v)
            running = v
            cmd = [self.python(), str(main), "--root", str(self.root)]
            start = self.clock()
            rc = self.spawn(cmd, str(main.parent))
            ran = self.clock() - start
            if rc == EXIT_QUIT:
                log.info("agent quit")
                return 0
            if rc == EXIT_RESTART:
                log.info("agent asked for a restart")
                backoff = 1
                rapid_restarts = rapid_restarts + 1 if ran < QUICK_CRASH_SECONDS else 0
                if rapid_restarts > RAPID_RESTARTS_FREE:
                    delay = min(2 ** (rapid_restarts - RAPID_RESTARTS_FREE), BACKOFF_MAX)
                    log.warning("%d quick restarts in a row; waiting %d s", rapid_restarts, delay)
                    self.sleep(delay)
                elif ran < RESTART_FLOOR:
                    self.sleep(RESTART_FLOOR - ran)
                continue
            rapid_restarts = 0
            log.warning("agent %s exited with %s after %.0f s", v, rc, ran)
            if newly_switched:
                if ran < QUICK_CRASH_SECONDS:
                    quick_crashes += 1
                    if quick_crashes >= CRASHES_TO_REVERT:
                        newly_switched, quick_crashes = False, 0
                        if self._revert(v):
                            running = read_pointer(self.root, "current.txt")
                            backoff = 1
                            continue
                else:
                    newly_switched, quick_crashes = False, 0
            if ran >= BACKOFF_MAX:
                backoff = 1
            self.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="launcher.pyw")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent))
    args = ap.parse_args(argv)
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    held = acquire_single_instance(root)
    if held is None:
        return 0                     # another launcher is running
    h = logging.handlers.RotatingFileHandler(str(root / "launcher.log"), maxBytes=500_000,
                                             backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)
    try:
        return Launcher(root).run()
    finally:
        held.release()


if __name__ == "__main__":
    sys.exit(main())
