"""The hub's admin password (phase 2, 2B1; spec D13).

The password is a salted PBKDF2-HMAC-SHA256 hash in the store's
``settings_kv`` table under ``admin_password``, as
``pbkdf2_sha256$<iterations>$<salt hex>$<digest hex>``. The salt is 16
random bytes chosen when the password is set (per install); the iteration
count is read back from the stored value, so raising ``ITERATIONS`` later
only affects passwords set after that.

There is no default password. Until one is set, every admin-gated action is
refused with a message pointing to ``/admin/setup``. Agent tokens are minted
behind this gate, so they are only as safe as it is.

**First-use setup needs a one-time code** (security review C1). While no
password is set, ``ensure_setup_code()`` keeps ``secrets.token_urlsafe(9)``
in ``GC_DATA_DIR/admin-setup-code.txt`` (mode 0600 where supported) and logs
it at WARNING (so it is in ``app.log``). It runs when the blueprint is
registered (app start) and whenever the setup page is opened. ``setup``
needs ``{password, setup_code}``; the code is compared with
``hmac.compare_digest`` and the file is deleted on success. Resetting a
forgotten password = deleting the ``settings_kv`` row on the server; the next
start (or setup page view) writes and logs a new code. The setup route also
refuses a ``Host`` header that isn't an IP literal, ``localhost``, one of the
machine's own names or the configured ``hub_url`` host (``host_allowed``):
defence in depth against DNS rebinding, on top of the cross-site guard.

API::

    is_set(*, db=None) -> bool
    ensure_setup_code(*, db=None) -> str | None     # None once a password is set
    setup(password, setup_code, *, db=None, client=None)
        # AlreadySet | SetupCodeError (wrong/missing code, throttled) | PasswordError
    change(current, new, *, db=None, client=None)   # WrongPassword / PasswordError
    check(password, client=None, *, db=None) -> CheckResult(ok, reason, message, retry_after)
        # reason: 'ok' | 'wrong' | 'not-set' | 'throttled' | 'no-store' | 'error'
    check_admin_body(body, *, db=None, client=None) -> bool
        # app._check_admin: body['password']; in a request, the client is the
        # remote address and a refusal other than 'wrong' is remembered so the
        # route's 403 carries its message (see ``bp``)
    host_allowed(host, *, db=None) -> bool
    limit_json_body()                                # request.max_content_length = 64 KiB
    hub_db() -> Path                                 # the default store, migrated once
    bp                                               # /admin/setup, /api/admin/setup,
                                                     # /api/admin/password; JSON 413s

**Throttling** (in memory; a restart forgets it). Each attempt (password or
setup code) is *reserved* under a lock before any hashing:

* per client address: one attempt in flight at a time (a parallel one is
  refused); the attempt counts as a failure up front and is refunded on
  success; from the ``FREE_ATTEMPTS``-th consecutive failure on, the client
  is locked out for ``2 ** (n - FREE_ATTEMPTS)`` s (capped at
  ``MAX_BACKOFF_SECONDS``);
* hub-wide: at most ``GLOBAL_FAILURE_BUDGET`` failures per
  ``GLOBAL_WINDOW_SECONDS`` from all clients together, after which every
  attempt is refused until the window moves on;
* CPU: at most ``HASH_CONCURRENCY`` PBKDF2 computations run at once.

**Store.** ``db=None`` means the hub's store (``GC_DATA_DIR/gc.db``), which
``hub_db()`` migrates once per process before first use (idempotent), so
this works before the hub's start-up wiring runs, and on the health check's
empty data folder. Without ``GC_DATA_DIR`` (legacy mode) there is no store:
admin actions are refused (``no-store``).
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import paths
import store

log = logging.getLogger("admin_auth")

KEY = "admin_password"
HUB_URL_KEY = "hub_url"
ALGO = "pbkdf2_sha256"
ITERATIONS = 310_000
SALT_BYTES = 16
MIN_LENGTH = 8
MAX_LENGTH = 1024
FREE_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 300.0
GLOBAL_FAILURE_BUDGET = 30
GLOBAL_WINDOW_SECONDS = 600.0
HASH_CONCURRENCY = 2
HASH_WAIT_SECONDS = 10.0
MAX_JSON_BODY = 64 * 1024
SETUP_PATH = "/admin/setup"
SETUP_CODE_FILE = "admin-setup-code.txt"

NOT_SET_MESSAGE = ("No admin password has been set on this hub yet. Set one at "
                   f"{SETUP_PATH} (first use only).")
NO_STORE_MESSAGE = ("Admin actions need the hub's data folder (GC_DATA_DIR); "
                    "none is configured.")
WRONG_CODE_MESSAGE = (f"Wrong or missing setup code. It is in {SETUP_CODE_FILE} in the hub's "
                      "data folder, and in a WARNING line of app.log.")


class PasswordError(ValueError):
    """The password is unacceptable (too short, not text) or the current one is wrong."""


class WrongPassword(PasswordError):
    """``change`` with a current password that isn't right (or while throttled)."""


class SetupCodeError(PermissionError):
    """``setup`` with a wrong or missing one-time code (or while throttled)."""


class AlreadySet(RuntimeError):
    """``setup`` when a password already exists."""


class NoStore(RuntimeError):
    """``GC_DATA_DIR`` is unset, so there is no store to hold the password."""


class _Busy(RuntimeError):
    """No hashing slot became free in time."""


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    reason: str
    message: str = ""
    retry_after: float = 0.0


# ── the store ───────────────────────────────────────────────────────────────

_migrated: set = set()
_migrate_lock = threading.Lock()


def hub_db() -> Path:
    """The hub's store (``GC_DATA_DIR/gc.db``), migrated once per process."""
    d = paths.data_dir()
    if d is None:
        raise NoStore(NO_STORE_MESSAGE)
    p = Path(d) / store.DB_FILENAME
    key = str(p)
    if key not in _migrated:
        with _migrate_lock:
            if key not in _migrated:
                store.migrate(p)
                _migrated.add(key)
    return p


def _db(db):
    return db if db is not None else hub_db()


# ── hashing ─────────────────────────────────────────────────────────────────

_hash_slots = threading.BoundedSemaphore(HASH_CONCURRENCY)


def _validate(password: Any) -> str:
    if not isinstance(password, str):
        raise PasswordError("The password must be text.")
    if len(password) < MIN_LENGTH:
        raise PasswordError(f"The password must be at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise PasswordError(f"The password must be at most {MAX_LENGTH} characters.")
    return password


def _hash(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def _bounded_hash(password: str, salt: bytes, iterations: int) -> bytes:
    """``_hash`` with at most ``HASH_CONCURRENCY`` running at once (CPU)."""
    if not _hash_slots.acquire(timeout=HASH_WAIT_SECONDS):
        raise _Busy("the hub is busy checking passwords; try again")
    try:
        return _hash(password, salt, iterations)
    finally:
        _hash_slots.release()


def _encode(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    derived = _bounded_hash(password, salt, ITERATIONS)
    return f"{ALGO}${ITERATIONS}${salt.hex()}${derived.hex()}"


def _matches(password: Any, stored: str) -> bool:
    """Constant-time comparison of ``password`` with the stored hash."""
    if not isinstance(password, str) or len(password) > MAX_LENGTH:
        return False
    try:
        algo, iters, salt_hex, expected_hex = stored.split("$")
        if algo != ALGO:
            return False
        iterations = int(iters)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except (ValueError, AttributeError):
        log.error("admin password: the stored hash is unreadable")
        return False
    derived = _bounded_hash(password, salt, iterations)
    return hmac.compare_digest(derived, expected)


# ── throttling ──────────────────────────────────────────────────────────────

_clock = time.monotonic


class _Throttle:
    MAX_CLIENTS = 1000

    def __init__(self):
        self._lock = threading.Lock()
        self._state: Dict[str, dict] = {}
        self._global: collections.deque = collections.deque()   # (time, ticket id)

    def _prune(self, now: float) -> None:
        while self._global and self._global[0][0] <= now - GLOBAL_WINDOW_SECONDS:
            self._global.popleft()

    def retry_after(self, client: str) -> float:
        with self._lock:
            st = self._state.get(client)
            return max(0.0, st["locked_until"] - _clock()) if st else 0.0

    def begin(self, client: str):
        """Reserve one attempt: ``(ticket, 0, '')`` or ``(None, wait, message)``.
        The attempt counts as a failure until ``finish`` says otherwise."""
        with self._lock:
            now = _clock()
            self._prune(now)
            if len(self._global) >= GLOBAL_FAILURE_BUDGET:
                wait = self._global[0][0] + GLOBAL_WINDOW_SECONDS - now
                return None, max(wait, 1.0), (f"Too many wrong admin passwords on this hub. "
                                              f"Try again in {int(wait) + 1} s.")
            st = self._state.get(client)
            if st is None:
                if len(self._state) >= self.MAX_CLIENTS:
                    idle = [c for c, s in self._state.items() if not s["in_flight"]]
                    if idle:
                        del self._state[min(idle, key=lambda c: self._state[c]["locked_until"])]
                st = self._state[client] = {"failures": 0, "locked_until": 0.0, "in_flight": False}
            wait = st["locked_until"] - now
            if wait > 0:
                return None, wait, (f"Too many wrong admin passwords. "
                                    f"Try again in {int(wait) + 1} s.")
            if st["in_flight"]:
                return None, 1.0, ("Another admin password check from this address is in "
                                   "progress. Try again in 1 s.")
            ticket = {"client": client, "id": object(), "prev_locked_until": st["locked_until"]}
            st["failures"] += 1
            st["in_flight"] = True
            if st["failures"] >= FREE_ATTEMPTS:
                st["locked_until"] = now + min(2.0 ** (st["failures"] - FREE_ATTEMPTS),
                                               MAX_BACKOFF_SECONDS)
            self._global.append((now, ticket["id"]))
            return ticket, 0.0, ""

    def finish(self, ticket: dict, outcome: str) -> None:
        """``outcome``: ``'ok'`` (reset the client), ``'fail'`` (keep the count),
        ``'refund'`` (the attempt didn't test anything: undo it)."""
        with self._lock:
            st = self._state.get(ticket["client"])
            if st is not None:
                st["in_flight"] = False
            if outcome == "fail":
                return
            self._global = collections.deque(e for e in self._global if e[1] is not ticket["id"])
            if st is None:
                return
            if outcome == "ok":
                del self._state[ticket["client"]]
            else:
                st["failures"] = max(0, st["failures"] - 1)
                st["locked_until"] = ticket["prev_locked_until"]

    def clear(self) -> None:
        with self._lock:
            self._state.clear()
            self._global.clear()


_throttle = _Throttle()


def reset_throttle() -> None:
    """Forget every client's failures (tests)."""
    _throttle.clear()


# ── the one-time setup code ─────────────────────────────────────────────────

_code_lock = threading.Lock()
_code_logged: set = set()


def _code_path(db) -> Path:
    if db is None:
        return hub_db().parent / SETUP_CODE_FILE
    if hasattr(db, "execute"):
        d = paths.data_dir()
        if d is None:
            raise NoStore(NO_STORE_MESSAGE)
        return Path(d) / SETUP_CODE_FILE
    return Path(db).parent / SETUP_CODE_FILE


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(str(tmp), 0o600)
    except OSError:  # pragma: no cover - Windows: the data folder's ACL applies
        pass
    os.replace(str(tmp), str(path))


def _read_code(path: Path) -> Optional[str]:
    try:
        code = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return code or None


def _remove_code(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("could not delete %s", path)


def ensure_setup_code(*, db=None) -> Optional[str]:
    """The one-time setup code while no password is set (written and logged
    at WARNING if new), else ``None`` (and any stale code file is removed)."""
    path = _code_path(db)
    db = _db(db)
    with _code_lock:
        if is_set(db=db):
            _remove_code(path)
            return None
        code = _read_code(path)
        if code is None:
            code = secrets.token_urlsafe(9)
            _write_private(path, code + "\n")
        if code not in _code_logged:
            _code_logged.add(code)
            log.warning("No admin password is set. Admin setup code: %s (also in %s). "
                        "Open %s on the hub to set the password.", code, path, SETUP_PATH)
        return code


# ── the Host check (DNS rebinding) ──────────────────────────────────────────

_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_machine_names_cache: Optional[set] = None


def _machine_names() -> set:
    global _machine_names_cache
    if _machine_names_cache is None:
        names = {"localhost"}
        for fn in (socket.gethostname, socket.getfqdn):
            try:
                n = (fn() or "").strip().lower().rstrip(".")
            except OSError:
                continue
            if n:
                names.add(n)
                names.add(n.split(".")[0])
        _machine_names_cache = names
    return _machine_names_cache


def _hostname(host: Any) -> Optional[str]:
    """The lower-case host name of a ``Host`` header value, or ``None``."""
    if not isinstance(host, str) or not host.strip():
        return None
    try:
        parts = urllib.parse.urlsplit("//" + host.strip())
        name = parts.hostname
        parts.port   # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return None
    return name.lower().rstrip(".") if name else None


def is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def configured_hub_host(*, db=None) -> Optional[str]:
    try:
        url = store.settings_kv.get(HUB_URL_KEY, db=_db(db))
    except Exception:  # noqa: BLE001
        return None
    return _hostname(urllib.parse.urlsplit(url).netloc) if url else None


def is_machine_name(name: str) -> bool:
    return name in _machine_names()


def host_allowed(host: Any, *, db=None) -> bool:
    """Is ``host`` (a ``Host`` header) one this hub answers to: an IP literal
    (a rebinding attack arrives under the attacker's *name*), ``localhost``,
    one of this machine's names, or the configured ``hub_url``'s host."""
    name = _hostname(host)
    if name is None:
        return False
    if is_ip_literal(name):
        return True
    if not _HOSTNAME_RE.match(name):
        return False
    if name == "localhost" or is_machine_name(name):
        return True
    return name == configured_hub_host(db=db)


# ── public API ──────────────────────────────────────────────────────────────

def is_set(*, db=None) -> bool:
    return bool(store.settings_kv.get(KEY, db=_db(db)))


def setup(password: Any, setup_code: Any, *, db=None, client: Optional[str] = None) -> None:
    """Set the first admin password; needs the one-time ``setup_code``."""
    _validate(password)
    path = _code_path(db)
    db = _db(db)
    if is_set(db=db):
        raise AlreadySet("An admin password is already set.")
    ticket, _wait, message = _throttle.begin(client or "?")
    if ticket is None:
        raise SetupCodeError(message)
    outcome = "fail"
    try:
        expected = _read_code(path)
        supplied = setup_code if isinstance(setup_code, str) else ""
        good = bool(expected) and hmac.compare_digest(supplied.encode("utf-8"),
                                                      expected.encode("utf-8"))
        if not good:
            log.warning("admin setup refused from %s: wrong setup code", client or "?")
            raise SetupCodeError(WRONG_CODE_MESSAGE)
        try:
            encoded = _encode(password)
        except _Busy as exc:
            outcome = "refund"
            raise SetupCodeError(str(exc)) from None
        with store.connection(db) as conn:
            with store.write_txn(conn):
                if store.settings_kv.get(KEY, db=conn):
                    outcome = "refund"
                    raise AlreadySet("An admin password is already set.")
                store.settings_kv.set(KEY, encoded, db=conn)
        outcome = "ok"
    finally:
        _throttle.finish(ticket, outcome)
    _remove_code(path)
    log.warning("admin password set (first use, from %s)", client or "?")


def check(password: Any, client: Optional[str] = None, *, db=None) -> CheckResult:
    """Check ``password`` against the stored hash, with the throttling above."""
    ticket, wait, message = _throttle.begin(client or "?")
    if ticket is None:
        return CheckResult(False, "throttled", message, wait)
    outcome = "refund"
    try:
        try:
            stored = store.settings_kv.get(KEY, db=_db(db))
        except NoStore:
            return CheckResult(False, "no-store", NO_STORE_MESSAGE)
        except Exception:  # noqa: BLE001 - never let the gate open on an error
            log.exception("admin password: could not read the store")
            return CheckResult(False, "error", "The admin password could not be checked "
                                               "(store error).")
        if not stored:
            return CheckResult(False, "not-set", NOT_SET_MESSAGE)
        try:
            ok = _matches(password, stored)
        except _Busy as exc:
            return CheckResult(False, "throttled", str(exc), 1.0)
        if ok:
            outcome = "ok"
            return CheckResult(True, "ok")
        outcome = "fail"
        log.warning("wrong admin password from %s", client or "?")
        return CheckResult(False, "wrong", "Incorrect password")
    finally:
        _throttle.finish(ticket, outcome)


def change(current: Any, new: Any, *, db=None, client: Optional[str] = None) -> None:
    """Replace the password; ``current`` must be right (throttling applies).
    The write is a compare-and-set: if the stored hash changed while
    ``current`` was being checked, nothing is written."""
    _validate(new)
    db = _db(db)
    before = store.settings_kv.get(KEY, db=db)
    res = check(current, client, db=db)
    if not res.ok:
        raise WrongPassword(res.message)
    encoded = _encode(new)
    with store.connection(db) as conn:
        with store.write_txn(conn):
            if store.settings_kv.get(KEY, db=conn) != before:
                raise PasswordError("The admin password was changed meanwhile; try again.")
            store.settings_kv.set(KEY, encoded, db=conn)
    log.warning("admin password changed (from %s)", client or "?")


def check_admin_body(body: Any, *, db=None, client: Optional[str] = None) -> bool:
    """``app._check_admin``: True when ``body['password']`` is the admin password."""
    supplied = body.get("password") if isinstance(body, dict) else None
    in_request = False
    try:
        from flask import g, has_request_context, request
        in_request = has_request_context()
        if in_request and client is None:
            client = request.remote_addr
    except ImportError:  # pragma: no cover - flask is a hub dependency
        pass
    res = check(supplied, client, db=db)
    if not res.ok and res.reason != "wrong" and in_request:
        g.admin_refusal = res
    return res.ok


# ── routes ──────────────────────────────────────────────────────────────────

from flask import Blueprint, g, jsonify, render_template, request  # noqa: E402
from werkzeug.exceptions import RequestEntityTooLarge  # noqa: E402

bp = Blueprint("admin_auth", __name__)


def limit_json_body(limit: int = MAX_JSON_BODY) -> None:
    """Cap this request's body before it is read (chunked bodies included):
    reading more raises ``RequestEntityTooLarge`` (a JSON 413, see below)."""
    request.max_content_length = limit


@bp.record_once
def _startup_setup_code(_state) -> None:
    """At app start: write and log the setup code if no password is set."""
    try:
        ensure_setup_code()
    except NoStore:
        pass
    except Exception:  # noqa: BLE001 - never stop the app starting
        log.exception("could not prepare the admin setup code")


@bp.app_errorhandler(RequestEntityTooLarge)
def _too_large(exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": "The request body is too large."}), 413
    return exc


def _json_body():
    limit_json_body()
    if not request.is_json:
        return None, (jsonify({"error": "Expected Content-Type: application/json"}), 415)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None, (jsonify({"error": "Expected a JSON object"}), 400)
    return body, None


def _cross_site() -> bool:
    """Same rule as app's guard (which already covers /api/), kept here so the
    setup route is same-origin whatever its path."""
    site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        return True
    origin = request.headers.get("Origin")
    if origin is not None:
        try:
            parts = urllib.parse.urlsplit(origin.strip())
        except ValueError:
            return True
        return parts.scheme not in ("http", "https") or parts.netloc.lower() != request.host.lower()
    return False


@bp.after_app_request
def _admin_refusal_message(response):
    """Give an admin route's 403 the real reason (not set up / throttled),
    keeping the route's other keys."""
    refusal = g.pop("admin_refusal", None)
    if refusal is not None and response.status_code == 403 and response.is_json:
        body = response.get_json(silent=True)
        body = dict(body) if isinstance(body, dict) else {}
        body["error"] = refusal.message
        if refusal.reason == "not-set":
            body["setup_url"] = SETUP_PATH
        response.set_data(jsonify(body).get_data())
    return response


@bp.route(SETUP_PATH, methods=["GET"])
def admin_setup_page():
    try:
        done = ensure_setup_code() is None
    except NoStore:
        done = None
    try:
        from version import APP_VERSION
    except Exception:  # noqa: BLE001
        APP_VERSION = "dev"
    return render_template("admin_setup.html", is_set=done, app_version=APP_VERSION,
                           min_length=MIN_LENGTH, code_file=SETUP_CODE_FILE)


def _refuse_host():
    if host_allowed(request.host):
        return None
    log.warning("refused %s from %s: Host %r is not this hub", request.path,
                request.remote_addr, request.host)
    return jsonify({"error": "This request's Host is not this hub's address. Open the hub by "
                             "its own name or IP address."}), 403


@bp.route("/api/admin/setup", methods=["POST"])
def api_admin_setup():
    if _cross_site():
        return jsonify({"error": "Cross-site request refused"}), 403
    refused = _refuse_host()
    if refused:
        return refused
    body, err = _json_body()
    if err:
        return err
    try:
        setup(body.get("password"), body.get("setup_code"), client=request.remote_addr)
    except NoStore:
        return jsonify({"error": NO_STORE_MESSAGE}), 503
    except AlreadySet:
        return jsonify({"error": "An admin password is already set. Change it with the "
                                 "current password instead."}), 409
    except SetupCodeError as exc:
        return jsonify({"error": str(exc)}), 403
    except PasswordError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True}), 201


@bp.route("/api/admin/password", methods=["POST"])
def api_admin_password():
    if _cross_site():
        return jsonify({"error": "Cross-site request refused"}), 403
    body, err = _json_body()
    if err:
        return err
    try:
        change(body.get("password"), body.get("new_password"), client=request.remote_addr)
    except NoStore:
        return jsonify({"error": NO_STORE_MESSAGE}), 503
    except WrongPassword as exc:
        return jsonify({"error": str(exc)}), 403
    except PasswordError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})
