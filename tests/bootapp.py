"""Boot the real app.py in a subprocess, the way the ASAPSV1 updater does.

app.py cannot be imported in-process without side effects (import starts
threads), so route-level tests launch it: cwd = repo root, PORT + GC_DATA_DIR
in env, HOME redirected so nothing leaks into the real home directory.

The child's environment is built here explicitly (PORT, GC_DATA_DIR set;
GC_PORT and QBench credentials removed), so it does not depend on what
``tests/conftest.py`` happens to have stripped from the parent.
"""
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port, path, timeout=3.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def post(port, path, body, timeout=10.0):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def send(port, path, data=b"", headers=None, method="POST", timeout=10.0):
    """A raw request with exactly the given body bytes and headers (for
    content-type and cross-site checks). Returns ``(status, parsed JSON or
    None)``."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = e.code
    try:
        return code, json.loads(raw or b"null")
    except ValueError:
        return code, None


def wait_for(predicate, timeout=20.0, interval=0.25) -> bool:
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def child_env(tmp: Path, port: int, extra_env=None) -> dict:
    data = tmp / "data"
    home = tmp / "home"
    data.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({"GC_DATA_DIR": str(data), "PORT": str(port), "HOME": str(home),
                "USERPROFILE": str(home), "APPDATA": str(home / "AppData"),
                "QBENCH_STORE_PATH": str(home / "qbench.json")})
    env.pop("GC_PORT", None)
    env.pop("GC_CAL_CDF", None)
    env.pop("QBENCH_CLIENT_ID", None)
    env.pop("QBENCH_CLIENT_SECRET", None)
    env.update(extra_env or {})
    return env


def _start(tmp: Path, cmd, args, extra_env, wait):
    """Launch once; return ``(port, proc, log)`` after /healthz is 200.

    Raises ``_PortTaken`` if the child lost the race for its port (the free
    port was probed, closed, then grabbed by someone else before the bind).
    """
    port = free_port()
    env = child_env(tmp, port, extra_env)
    log_path = tmp / "boot.log"
    argv = list(cmd) if cmd is not None else [sys.executable, "app.py", *args]
    log = open(log_path, "w", encoding="utf-8")
    proc = None
    try:
        proc = subprocess.Popen(argv, cwd=ROOT, env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + wait
        while time.time() < deadline:
            if proc.poll() is not None:
                text = log_path.read_text(encoding="utf-8", errors="replace")
                if "Address already in use" in text or "address is already in use" in text.lower():
                    raise _PortTaken(text[-3000:])
                raise RuntimeError(f"app exited {proc.returncode}: {text[-3000:]}")
            try:
                code, _ = get(port, "/healthz", timeout=1.0)
                if code == 200:
                    return port, proc, log
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"no /healthz within {wait}s: "
                           f"{log_path.read_text(encoding='utf-8', errors='replace')[-3000:]}")
    except BaseException:
        _stop(proc, log)
        raise


class _PortTaken(RuntimeError):
    pass


def _stop(proc, log) -> None:
    try:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)
    finally:
        log.close()


@contextlib.contextmanager
def booted(tmp: Path, *, args=("--no-tray",), cmd=None, extra_env=None, wait=60.0):
    """Launch app.py and yield ``(port, proc, data_dir, home_dir)`` once
    ``/healthz`` answers 200. The process is stopped on exit.

    ``cmd`` overrides the whole command line (e.g. a runpy bootstrap like
    ``run.pyw`` uses); by default it is ``python app.py *args``. A lost race
    for the probed port is retried once on a fresh port.
    """
    try:
        port, proc, log = _start(tmp, cmd, args, extra_env, wait)
    except _PortTaken:
        port, proc, log = _start(tmp, cmd, args, extra_env, wait)
    try:
        yield port, proc, tmp / "data", tmp / "home"
    finally:
        _stop(proc, log)
