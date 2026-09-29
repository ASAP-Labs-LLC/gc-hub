"""Boot the real app.py in a subprocess, the way the ASAPSV1 updater does.

app.py cannot be imported in-process without side effects (import starts
threads), so route-level tests launch it: cwd = repo root, PORT + GC_DATA_DIR
in env, HOME redirected so nothing leaks into the real home directory.

The child's environment is built here explicitly (PORT, GC_DATA_DIR set;
GC_PORT and QBench credentials removed, ``LABCORE_URL`` pointed at a closed
local port so nothing can reach the real LabCore), so it does not depend on
what ``tests/conftest.py`` happens to have stripped from the parent.

**Signing in** (the hub needs a session for almost every route, spec D3):
``booted()`` inserts a ``web_sessions`` row straight into the booted app's
store (``sign_in``) and remembers its cookie for that port, so ``get``,
``post`` and ``send`` are signed in by default. Pass ``auth=False`` to make an
anonymous request, or ``booted(..., sign_in_as=None)`` to boot without one.
``cookie_header(port)`` gives the header for other clients, and
``browser_sign_in(driver, port)`` sets the cookie in a Selenium browser. This
is test plumbing only: the app has no switch that turns sign-in off.
"""
import contextlib
import hashlib
import json
import os
import secrets
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


# ── sessions ────────────────────────────────────────────────────────────────

TEST_USER = "Test Operator"
_SESSIONS: dict = {}     # port -> cookie value


def sign_in(port, data_dir, name=TEST_USER, method="password", *, remember=True) -> str:
    """Insert a live ``web_sessions`` row into the booted app's store and
    return its cookie value (remembered for ``port`` unless ``remember`` is
    False). The same thing a sign-in does, minus LabCore."""
    from datetime import datetime, timedelta, timezone
    import store
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="microseconds")
    store.web_sessions.add(hashlib.sha256(token.encode()).hexdigest(), name=name, method=method,
                           ip="127.0.0.1", user_agent="tests", expires_at=expires,
                           db=Path(data_dir) / store.DB_FILENAME)
    if remember:
        _SESSIONS[port] = token
    return token


def cookie_header(port, token=None) -> dict:
    """``{"Cookie": "gc_session=..."}`` for the port's session (or ``token``)."""
    token = token or _SESSIONS.get(port)
    return {"Cookie": f"gc_session={token}"} if token else {}


def _with_auth(port, headers, auth) -> dict:
    h = dict(headers or {})
    if auth and not any(k.lower() == "cookie" for k in h):
        h.update(cookie_header(port))
    return h


def browser_sign_in(driver, port, token=None) -> None:
    """Give a Selenium browser the port's session cookie (visit a static file
    on the host first: a cookie can only be set for the page's own host)."""
    driver.get(f"http://127.0.0.1:{port}/static/favicon.svg")
    driver.add_cookie({"name": "gc_session", "value": token or _SESSIONS[port], "path": "/"})


def get(port, path, timeout=3.0, *, auth=True, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=_with_auth(port, headers, auth))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def get_text(port, path, timeout=5.0, *, auth=True, headers=None) -> str:
    """A page or static file's body as text (signed in by default)."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=_with_auth(port, headers, auth))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def post(port, path, body, timeout=10.0, *, auth=True, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers=_with_auth(port, dict({"Content-Type": "application/json"},
                                                               **(headers or {})), auth))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def send(port, path, data=b"", headers=None, method="POST", timeout=10.0, *, auth=True):
    """A raw request with exactly the given body bytes and headers (for
    content-type and cross-site checks), plus the session cookie unless
    ``auth=False`` or ``headers`` has a Cookie. Returns ``(status, parsed JSON
    or None)``."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method, headers=_with_auth(port, headers, auth))
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


TEST_ADMIN_PASSWORD = "test-admin-pw"


def setup_admin(port, data_dir, password=TEST_ADMIN_PASSWORD) -> str:
    """Set the booted app's admin password through the first-use setup API
    (2B1, D13: there is no default password), with the one-time code the app
    wrote to ``<data_dir>/admin-setup-code.txt``. Returns the password."""
    setup_code = (Path(data_dir) / "admin-setup-code.txt").read_text(encoding="utf-8").strip()
    code, body = post(port, "/api/admin/setup", {"password": password, "setup_code": setup_code},
                      auth=False)
    if code != 201:
        raise RuntimeError(f"admin setup failed: {code} {body}")
    return password


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
    env["LABCORE_URL"] = "http://127.0.0.1:9"   # never the real LabCore (tests use a stub)
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
                code, _ = get(port, "/healthz", timeout=1.0, auth=False)
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
def booted(tmp: Path, *, args=("--no-tray",), cmd=None, extra_env=None, wait=60.0,
           sign_in_as=TEST_USER):
    """Launch app.py and yield ``(port, proc, data_dir, home_dir)`` once
    ``/healthz`` answers 200. The process is stopped on exit.

    ``cmd`` overrides the whole command line; by default it is
    ``python app.py *args``. A lost race
    for the probed port is retried once on a fresh port. Unless
    ``sign_in_as`` is None (or ``cmd`` is given), a session for that name is
    inserted into the store and ``get``/``post``/``send`` use it.
    """
    try:
        port, proc, log = _start(tmp, cmd, args, extra_env, wait)
    except _PortTaken:
        port, proc, log = _start(tmp, cmd, args, extra_env, wait)
    try:
        if sign_in_as is not None and cmd is None:
            sign_in(port, tmp / "data", sign_in_as)
        yield port, proc, tmp / "data", tmp / "home"
    finally:
        _SESSIONS.pop(port, None)
        _stop(proc, log)
