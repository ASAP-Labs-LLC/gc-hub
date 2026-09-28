"""The hub's admin password (phase 2, 2B1; spec D13).

The password is a salted PBKDF2-HMAC-SHA256 hash in the store's
``settings_kv`` table under ``admin_password``, as
``pbkdf2_sha256$<iterations>$<salt hex>$<digest hex>``. The salt is 16
random bytes chosen when the password is set (per install); the iteration
count is read back from the stored value, so raising ``ITERATIONS`` later
only affects passwords set after that.

There is no default password. Until one is set, every admin-gated action is
refused with a message pointing to ``/admin/setup``, where the first user
sets it (only while none is set). Agent tokens are minted behind this gate,
so they are only as safe as it is.

API::

    is_set(*, db=None) -> bool
    setup(password, *, db=None)                 # first use only: AlreadySet otherwise
    change(current, new, *, db=None, client=None)   # WrongPassword / PasswordError
    check(password, client=None, *, db=None) -> CheckResult(ok, reason, message, retry_after)
        # reason: 'ok' | 'wrong' | 'not-set' | 'throttled' | 'no-store' | 'error'
    check_admin_body(body, *, db=None, client=None) -> bool
        # app._check_admin: body['password']; in a request, the client is the
        # remote address and a refusal other than 'wrong' is remembered so the
        # route's 403 carries its message (see ``bp``)
    hub_db() -> Path                            # the default store, migrated once
    bp                                          # Blueprint: /admin/setup, /api/admin/setup,
                                                #            /api/admin/password

**Backoff.** In memory, per client address: the first ``FREE_ATTEMPTS``
consecutive failures cost nothing; after that each failure locks the client
out for ``2 ** (n - FREE_ATTEMPTS)`` seconds (capped at
``MAX_BACKOFF_SECONDS``), during which every attempt is refused without
checking the password. A success resets the client. A restart forgets it.

**Store.** ``db=None`` means the hub's store (``GC_DATA_DIR/gc.db``), which
``hub_db()`` migrates once per process before first use (idempotent), so
this works before the hub's start-up wiring runs, and on the health check's
empty data folder. Without ``GC_DATA_DIR`` there is no store: admin actions
are refused (``no-store``).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import paths
import store

log = logging.getLogger("admin_auth")

KEY = "admin_password"
ALGO = "pbkdf2_sha256"
ITERATIONS = 310_000
SALT_BYTES = 16
MIN_LENGTH = 8
MAX_LENGTH = 1024
FREE_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 300.0
SETUP_PATH = "/admin/setup"

NOT_SET_MESSAGE = ("No admin password has been set on this hub yet. Set one at "
                   f"{SETUP_PATH} (first use only).")
NO_STORE_MESSAGE = ("Admin actions need the hub's data folder (GC_DATA_DIR); "
                    "none is configured.")


class PasswordError(ValueError):
    """The password is unacceptable (too short, not text) or the current one is wrong."""


class WrongPassword(PasswordError):
    """``change`` with a current password that isn't right (or while throttled)."""


class AlreadySet(RuntimeError):
    """``setup`` when a password already exists."""


class NoStore(RuntimeError):
    """``GC_DATA_DIR`` is unset, so there is no store to hold the password."""


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


def _encode(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    derived = _hash(password, salt, ITERATIONS)
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
    derived = _hash(password, salt, iterations)
    return hmac.compare_digest(derived, expected)


# ── backoff ─────────────────────────────────────────────────────────────────

_clock = time.monotonic


class _Throttle:
    MAX_CLIENTS = 1000

    def __init__(self):
        self._lock = threading.Lock()
        self._state: Dict[str, list] = {}     # client -> [failures, locked_until]

    def retry_after(self, client: str) -> float:
        with self._lock:
            st = self._state.get(client)
            return max(0.0, st[1] - _clock()) if st else 0.0

    def failure(self, client: str) -> None:
        with self._lock:
            if client not in self._state and len(self._state) >= self.MAX_CLIENTS:
                oldest = min(self._state, key=lambda c: self._state[c][1])
                del self._state[oldest]
            st = self._state.setdefault(client, [0, 0.0])
            st[0] += 1
            if st[0] >= FREE_ATTEMPTS:
                st[1] = _clock() + min(2.0 ** (st[0] - FREE_ATTEMPTS), MAX_BACKOFF_SECONDS)

    def success(self, client: str) -> None:
        with self._lock:
            self._state.pop(client, None)

    def clear(self) -> None:
        with self._lock:
            self._state.clear()


_throttle = _Throttle()


def reset_throttle() -> None:
    """Forget every client's failures (tests)."""
    _throttle.clear()


# ── public API ──────────────────────────────────────────────────────────────

def is_set(*, db=None) -> bool:
    return bool(store.settings_kv.get(KEY, db=_db(db)))


def setup(password: Any, *, db=None) -> None:
    """Set the first admin password. ``AlreadySet`` if one exists."""
    encoded = _encode(_validate(password))
    with store.connection(_db(db)) as conn:
        with store.write_txn(conn):
            if store.settings_kv.get(KEY, db=conn):
                raise AlreadySet("An admin password is already set.")
            store.settings_kv.set(KEY, encoded, db=conn)
    log.warning("admin password set (first use)")


def check(password: Any, client: Optional[str] = None, *, db=None) -> CheckResult:
    """Check ``password`` against the stored hash, with the per-client backoff."""
    client = client or "?"
    wait = _throttle.retry_after(client)
    if wait > 0:
        return CheckResult(False, "throttled",
                           f"Too many wrong admin passwords. Try again in {int(wait) + 1} s.",
                           wait)
    try:
        stored = store.settings_kv.get(KEY, db=_db(db))
    except NoStore:
        return CheckResult(False, "no-store", NO_STORE_MESSAGE)
    except Exception:  # noqa: BLE001 - never let the gate open on an error
        log.exception("admin password: could not read the store")
        return CheckResult(False, "error", "The admin password could not be checked (store error).")
    if not stored:
        return CheckResult(False, "not-set", NOT_SET_MESSAGE)
    if _matches(password, stored):
        _throttle.success(client)
        return CheckResult(True, "ok")
    _throttle.failure(client)
    log.warning("wrong admin password from %s", client)
    return CheckResult(False, "wrong", "Incorrect password")


def change(current: Any, new: Any, *, db=None, client: Optional[str] = None) -> None:
    """Replace the password; ``current`` must be right (backoff applies)."""
    res = check(current, client, db=db)
    if not res.ok:
        raise WrongPassword(res.message)
    store.settings_kv.set(KEY, _encode(_validate(new)), db=_db(db))
    log.warning("admin password changed")


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

bp = Blueprint("admin_auth", __name__)


def _json_body():
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
        from urllib.parse import urlsplit
        try:
            parts = urlsplit(origin.strip())
        except ValueError:
            return True
        return parts.scheme not in ("http", "https") or parts.netloc.lower() != request.host.lower()
    return False


@bp.after_app_request
def _admin_refusal_message(response):
    """Give an admin route's 403 the real reason (not set up / throttled)."""
    refusal = g.pop("admin_refusal", None)
    if refusal is not None and response.status_code == 403 and response.is_json:
        body = {"error": refusal.message}
        if refusal.reason == "not-set":
            body["setup_url"] = SETUP_PATH
        response.set_data(jsonify(body).get_data())
    return response


@bp.route(SETUP_PATH, methods=["GET"])
def admin_setup_page():
    try:
        done = is_set()
    except NoStore:
        done = None
    try:
        from version import APP_VERSION
    except Exception:  # noqa: BLE001
        APP_VERSION = "dev"
    return render_template("admin_setup.html", is_set=done, app_version=APP_VERSION,
                           min_length=MIN_LENGTH)


@bp.route("/api/admin/setup", methods=["POST"])
def api_admin_setup():
    if _cross_site():
        return jsonify({"error": "Cross-site request refused"}), 403
    body, err = _json_body()
    if err:
        return err
    try:
        setup(body.get("password"))
    except NoStore:
        return jsonify({"error": NO_STORE_MESSAGE}), 503
    except AlreadySet:
        return jsonify({"error": "An admin password is already set. Change it with the "
                                 "current password instead."}), 409
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
