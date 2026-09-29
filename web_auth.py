"""web_auth.py: browser sign-in, sessions and the session gate (spec D1-D8, rev 2).

The hub is reachable at https://gc.asaplabs.net (a Cloudflare tunnel) and on
the lab LAN (http://ASAPSV1:5560); both need a signed-in session. One rule
everywhere, with these exceptions (``route_class``):

* **open** — ``/healthz``; ``/login``; ``/api/login``, ``/api/login/card``,
  ``/api/login/admin``; ``/api/logout``; ``/static/*`` and ``/favicon.ico``;
  the agent paths ``/api/ingest``, ``/api/agent/heartbeat``,
  ``/api/agent/results``, ``/api/agent/package``, ``/api/agent/package.zip``
  (bearer token; ``/api/agents`` is NOT one of them).
* **local** — the hub tray's paths ``GET /api/hub/status``,
  ``POST /api/admin/hub/pause-processing|resume-processing|stop`` and
  ``POST /api/restart``: no session needed when ``netctx.is_local()`` (every
  other check of theirs still applies). Not local: ``/api/admin/hub/*`` is
  403 regardless of session; the other two need a session.
* **setup** — ``/admin/setup`` and ``POST /api/admin/setup``: open while no
  admin password is set **and** the request did not come through Cloudflare.
  Through Cloudflare they need a (LabLink) session, and setup still needs the
  one-time code. Once a password is set, setup is closed everywhere
  (``admin_auth`` answers 409).
* everything else — **session**.

Refusals: an ``/api/`` request gets 401 ``{error, login_required: true}`` and
the header ``X-GC-Login-Required: 1`` (the front end's fetch wrapper keys on
it, so a wrong-password 401 never redirects); a page gets a 302 to
``/login?next=<path>``. A store error while checking a session is a 503, not
a 401.

Before any of that, ``require_https``: a request that came through Cloudflare
but not over https gets a 308 to the https URL (every method). Responses on
https carry HSTS (``max-age=31536000``).

**Sign-in** (JSON, 64 KiB, the app's cross-site guard applies):

* ``POST /api/login {username, password}`` and ``POST /api/login/card {code}``
  go to LabCore (``labcore_auth``; card = the code as both fields). The
  session takes LabCore's canonical account name.
* ``POST /api/login/admin {password}``: the break-glass, only when the request
  did not come through Cloudflare (LAN or local); through the tunnel, only
  LabLink sign-in exists. Checked by ``admin_auth.check`` (its own throttle).
  Session name ``Admin (break-glass)``, method ``admin``; changing the admin
  password revokes every ``admin`` session.
* Through Cloudflare a sign-in over plain http is refused (403).
* LabCore unreachable (``LabCoreUnavailable``) is 503 with
  ``labcore_unavailable: true``, and is not a failure for the throttle.

**Throttle** (``LoginThrottle``, in memory, a restart forgets it): per
(client address, username) 5 failures in 10 min → refused for 60 s; per
client address 30 in 10 min → 60 s; hub-wide 100 in 10 min, counted and
applied only to requests through Cloudflare (the LAN and the console are
never refused by it). Card attempts are keyed (address, "card"). Addresses
are ``netctx.throttle_key`` (IPv6 by /64); each map holds at most 10 000 keys.
An attempt is reserved before LabCore is asked (so parallel attempts can't
overshoot) and refunded when it tested nothing.

**Sessions** (``store.web_sessions``, schema v3): a new random token
(``secrets.token_urlsafe(32)``) on every sign-in, and the session of any
cookie the browser presented is revoked. Only its sha256 is stored. Cookie
``__Host-gc_session`` over https, ``gc_session`` over plain http; HttpOnly,
SameSite=Lax, Path=/, Secure on https. Idle limit 12 h; absolute 14 days
(7 days for a session created through Cloudflare). Validated sessions are
cached in memory for 30 s (revoking clears the cache). ``last_seen`` is kept
in memory and written by a background refresher at most once a minute per
session, and only for activity (``note_seen``, called by the app's activity
tracker, which skips its non-activity paths). Sign out is ``POST
/api/logout``. ``GET /api/session`` → ``{name, method}`` (or the gate's 401).

**Logging**: a sign-in logs the name, method and client address; a failed
one logs the address, the method and ``sha256(username)[:8]`` — never the
username, a card code or a password. The cookie value is never logged.
"""
from __future__ import annotations

import collections
import hashlib
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from flask import Blueprint, g, jsonify, make_response, redirect, render_template, request

import admin_auth
import labcore_auth
import netctx
import store

log = logging.getLogger("web_auth")

COOKIE_HTTP = "gc_session"
COOKIE_HTTPS = "__Host-gc_session"
IDLE_SECONDS = store.WEB_SESSION_IDLE_SECONDS
ABSOLUTE_SECONDS = 14 * 86400
ABSOLUTE_PROXIED_SECONDS = 7 * 86400
CACHE_SECONDS = 30.0
TOUCH_SECONDS = 60.0
CACHE_MAX = 10_000
PRUNE_DAYS = store.WEB_SESSION_PRUNE_DAYS
TOKEN_MAX = 256
ADMIN_NAME = "Admin (break-glass)"
LOGIN_PATH = "/login"
LOGIN_REQUIRED_HEADER = "X-GC-Login-Required"

LABCORE_DOWN = "LabVision can't be reached. Try again, or sign in with the admin password."
LABCORE_DOWN_TUNNEL = "LabVision can't be reached. Try again in a minute."
TUNNEL_NO_ADMIN = ("The admin password sign-in works only on the lab network. "
                   "Sign in with your LabLink account.")

# ── what needs a session ────────────────────────────────────────────────────

OPEN_PATHS = frozenset({
    "/healthz", LOGIN_PATH, "/api/login", "/api/login/card", "/api/login/admin",
    "/api/logout", "/favicon.ico",
    # the agents (bearer token, ingest_api)
    "/api/ingest", "/api/agent/heartbeat", "/api/agent/results", "/api/agent/package",
    "/api/agent/package.zip",
})
TRAY_ADMIN_PATHS = frozenset({"/api/admin/hub/pause-processing",
                              "/api/admin/hub/resume-processing", "/api/admin/hub/stop"})
LOCAL_PATHS = TRAY_ADMIN_PATHS | {"/api/hub/status", "/api/restart"}
SETUP_PATHS = frozenset({admin_auth.SETUP_PATH, "/api/admin/setup"})
NOT_LOCAL_MESSAGE = ("This control works only on the hub machine itself "
                     "(the hub tray, or http://localhost:5560 on ASAPSV1).")

_NEXT_RE = re.compile(r"^/(?![/\\])[^\\\x00-\x1f]*$")


def route_class(path: str) -> str:
    """``open`` | ``local`` | ``setup`` | ``session`` for a request path."""
    if path in OPEN_PATHS or path.startswith("/static/"):
        return "open"
    if path in LOCAL_PATHS:
        return "local"
    if path in SETUP_PATHS:
        return "setup"
    return "session"


def safe_next(raw: Any) -> str:
    """A relative path to go back to after sign-in, or ``/``."""
    if not isinstance(raw, str) or len(raw) > 2000:
        return "/"
    if not _NEXT_RE.match(raw):
        return "/"
    low = raw.lower()
    if low.startswith("/api/") or low == "/api" or low.startswith(LOGIN_PATH):
        return "/"
    return raw


class StoreUnavailable(RuntimeError):
    """The sessions table could not be read or written."""


# ── store access ────────────────────────────────────────────────────────────

def _db():
    try:
        return admin_auth.hub_db()
    except Exception as exc:  # noqa: BLE001 - NoStore, sqlite3.Error, OSError
        raise StoreUnavailable(str(exc)) from None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _epoch(iso: Optional[str]) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="microseconds")


# ── the cache and last_seen ─────────────────────────────────────────────────

_lock = threading.Lock()
_cache: "collections.OrderedDict[str, tuple]" = collections.OrderedDict()  # hash -> (at, row)
_seen: dict = {}        # session id -> epoch of the last noted activity
_pending: dict = {}     # session id -> iso to write
_refresher: Optional[threading.Thread] = None
_clock = time.time


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def reset() -> None:
    """Forget everything held in memory (tests): the cache, the noted
    activity and the sign-in throttle."""
    with _lock:
        _cache.clear()
        _seen.clear()
        _pending.clear()
    throttle.clear()


def _cache_get(h: str) -> Optional[dict]:
    with _lock:
        hit = _cache.get(h)
        if hit is None:
            return None
        at, row = hit
        if _clock() - at >= CACHE_SECONDS:
            _cache.pop(h, None)
            return None
        return row


def _cache_put(h: str, row: dict) -> None:
    with _lock:
        _cache[h] = (_clock(), row)
        _cache.move_to_end(h)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


def _row_view(row: dict) -> dict:
    return {"id": row["id"], "name": row["name"], "method": row["method"],
            "token_hash": row["token_hash"], "revoked": bool(row["revoked_at"]),
            "expires": _epoch(row["expires_at"]), "last_seen": _epoch(row["last_seen"])}


def _valid(view: dict, now: float) -> bool:
    if view["revoked"] or now >= view["expires"]:
        return False
    with _lock:
        seen = max(view["last_seen"], _seen.get(view["id"], 0.0))
    return now - seen < IDLE_SECONDS


def note_seen(session: Optional[dict]) -> None:
    """A signed-in request that counts as activity: remember it; the
    refresher writes ``last_seen`` at most once a minute per session."""
    if not session:
        return
    now = _clock()
    with _lock:
        if now - _seen.get(session["id"], 0.0) < TOUCH_SECONDS:
            return
        _seen[session["id"]] = now
        _pending[session["id"]] = _iso(now)
    _ensure_refresher()


def flush_seen() -> int:
    """Write the pending ``last_seen`` values (the refresher's job)."""
    with _lock:
        batch = dict(_pending)
        _pending.clear()
    if not batch:
        return 0
    try:
        store.web_sessions.touch_many(batch, db=_db())
    except Exception:  # noqa: BLE001 - next round
        log.exception("could not record session activity")
        with _lock:
            for k, v in batch.items():
                _pending.setdefault(k, v)
        return 0
    return len(batch)


def _refresh_loop() -> None:
    while True:
        time.sleep(TOUCH_SECONDS)
        try:
            flush_seen()
        except Exception:  # noqa: BLE001
            log.exception("session refresher failed")


def _ensure_refresher() -> None:
    global _refresher
    if _refresher is not None and _refresher.is_alive():
        return
    with _lock:
        if _refresher is not None and _refresher.is_alive():
            return
        _refresher = threading.Thread(target=_refresh_loop, daemon=True,
                                      name="session-last-seen")
        _refresher.start()


# ── resolving the request's session ─────────────────────────────────────────

def cookie_name(https: Optional[bool] = None) -> str:
    return COOKIE_HTTPS if (netctx.is_https() if https is None else https) else COOKIE_HTTP


def _presented_tokens() -> list:
    out = []
    for name in (COOKIE_HTTPS, COOKIE_HTTP):
        v = request.cookies.get(name)
        if v and len(v) <= TOKEN_MAX:
            out.append(v)
    return out


def lookup(token: Optional[str]) -> Optional[dict]:
    """The live session for a cookie value, or None. Raises StoreUnavailable."""
    if not token or len(token) > TOKEN_MAX:
        return None
    h = _hash(token)
    view = _cache_get(h)
    if view is None:
        try:
            row = store.web_sessions.get_by_hash(h, db=_db())
        except StoreUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - sqlite3.Error and friends
            raise StoreUnavailable(str(exc)) from None
        if row is None:
            return None
        view = _row_view(row)
        _cache_put(h, view)
    return view if _valid(view, _clock()) else None


def _resolve() -> Optional[dict]:
    """This request's session (cached on ``g``). Raises StoreUnavailable."""
    if "web_session" in g:
        return g.web_session
    token = request.cookies.get(cookie_name())
    s = lookup(token)
    g.web_session = s
    return s


def current_user() -> Optional[dict]:
    """``{id, name, method, token_hash, ...}`` of the signed-in person, or None."""
    try:
        return g.get("web_session")
    except RuntimeError:     # outside a request
        return None


def current_name() -> Optional[str]:
    s = current_user()
    return s["name"] if s else None


def actor() -> str:
    """``'<name> (<client address>)'`` for logs and ``by`` fields; the address
    alone when nobody is signed in (agents, the tray)."""
    ip = netctx.client_ip() or "unknown"
    name = current_name()
    return f"{name} ({ip})" if name else ip


def initials(name: Any) -> str:
    """Initials from an account name: the first letter of each word (split on
    non-letters), A–Z only, at most 4; ``X`` if nothing is left."""
    if not isinstance(name, str):
        return "X"
    words = [w for w in re.split(r"[^A-Za-z]+", name) if w]
    out = "".join(w[0] for w in words).upper()[:4]
    return out or "X"


# ── the before_request hooks ────────────────────────────────────────────────

def require_https():
    """Through Cloudflare but not https: 308 to the https URL (every method).
    Registered before every other before_request."""
    if netctx.is_proxied() and not netctx.is_https():
        return redirect(netctx.https_url(), 308)
    return None


def _login_required():
    if request.path.startswith("/api/"):
        resp = jsonify({"error": "Sign in required", "login_required": True})
        resp.status_code = 401
        resp.headers[LOGIN_REQUIRED_HEADER] = "1"
        return resp
    nxt = request.path + (("?" + request.query_string.decode("latin-1"))
                          if request.query_string else "")
    from urllib.parse import quote
    return redirect(f"{LOGIN_PATH}?next={quote(safe_next(nxt), safe='/')}", 302)


def _store_down():
    log.error("session check failed: the store is unavailable")
    if request.path.startswith("/api/"):
        return jsonify({"error": "The hub's database is unavailable; try again shortly."}), 503
    return ("The hub's database is unavailable; try again shortly.", 503,
            {"Content-Type": "text/plain; charset=utf-8", "Retry-After": "5"})


def gate():
    """The session gate (registered after the cross-site guard)."""
    path = request.path
    cls = route_class(path)
    if cls == "open":
        if path.startswith("/static/") or path in ("/healthz", "/favicon.ico") \
                or path.startswith(("/api/ingest", "/api/agent/")):
            return None
        try:
            _resolve()                     # the login page and logout want to know
        except StoreUnavailable:
            g.web_session = None
        return None
    if cls == "local":
        if netctx.is_local():
            return None
        if path in TRAY_ADMIN_PATHS:
            log.warning("refused %s %s from %s: not local", request.method, path,
                        netctx.client_ip())
            return jsonify({"error": NOT_LOCAL_MESSAGE}), 403
    if cls == "setup" and not netctx.is_proxied():
        try:
            if not admin_auth.is_set():
                try:
                    _resolve()
                except StoreUnavailable:
                    g.web_session = None
                return None
        except Exception:  # noqa: BLE001 - store trouble: fall through to the session check
            pass
    try:
        s = _resolve()
    except StoreUnavailable:
        return _store_down()
    if s is None:
        return _login_required()
    return None


def add_security_headers(response):
    """HSTS on https responses (after_request)."""
    try:
        if netctx.is_https():
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    except Exception:  # noqa: BLE001
        pass
    return response


def template_context() -> dict:
    """``web_user`` for every template (the signed-in line by the version badge)."""
    s = current_user()
    return {"web_user": {"name": s["name"], "method": s["method"]} if s else None}


# ── the sign-in throttle ────────────────────────────────────────────────────

class LoginThrottle:
    WINDOW = 600.0
    LOCK = 60.0
    PAIR_LIMIT = 5
    IP_LIMIT = 30
    GLOBAL_LIMIT = 100
    MAX_KEYS = 10_000

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict = {}          # key -> {"fails": deque, "locked_until": float}
        self._global: collections.deque = collections.deque()

    def _prune_deque(self, dq, now):
        while dq and dq[0] <= now - self.WINDOW:
            dq.popleft()

    def _state(self, key, now):
        st = self._keys.get(key)
        if st is None:
            if len(self._keys) >= self.MAX_KEYS:
                for k in [k for k, v in self._keys.items()
                          if v["locked_until"] <= now and
                          (not v["fails"] or v["fails"][-1] <= now - self.WINDOW)]:
                    del self._keys[k]
                while len(self._keys) >= self.MAX_KEYS:
                    del self._keys[next(iter(self._keys))]
            st = self._keys[key] = {"fails": collections.deque(), "locked_until": 0.0}
        self._prune_deque(st["fails"], now)
        return st

    def begin(self, keys: list, *, proxied: bool):
        """``keys`` = [(key, limit), ...]. Returns ``(ticket, 0)`` or
        ``(None, retry_after)``. The attempt counts as a failure until
        ``finish`` says otherwise."""
        with self._lock:
            now = self._clock()
            self._prune_deque(self._global, now)
            if proxied and len(self._global) >= self.GLOBAL_LIMIT:
                return None, max(1.0, self._global[0] + self.WINDOW - now)
            states = [(k, limit, self._state(k, now)) for k, limit in keys]
            wait = max([st["locked_until"] - now for _k, _l, st in states] + [0.0])
            if wait > 0:
                return None, wait
            ticket = {"t": now, "keys": [], "proxied": proxied}
            for k, limit, st in states:
                st["fails"].append(now)
                prev = st["locked_until"]
                if len(st["fails"]) >= limit:
                    st["locked_until"] = now + self.LOCK
                ticket["keys"].append((k, prev))
            if proxied:
                self._global.append(now)
            return ticket, 0.0

    def finish(self, ticket: dict, outcome: str, *, reset: tuple = ()) -> None:
        """``fail`` keeps the reserved failure; ``refund`` undoes it; ``ok``
        undoes it and forgets every key in ``reset``."""
        if outcome == "fail":
            return
        with self._lock:
            t = ticket["t"]
            for k, prev in ticket["keys"]:
                st = self._keys.get(k)
                if st is None:
                    continue
                try:
                    st["fails"].remove(t)
                except ValueError:
                    pass
                st["locked_until"] = prev
            if ticket["proxied"]:
                try:
                    self._global.remove(t)
                except ValueError:
                    pass
            if outcome == "ok":
                for k in reset:
                    self._keys.pop(k, None)

    def clear(self) -> None:
        with self._lock:
            self._keys.clear()
            self._global.clear()


throttle = LoginThrottle()


def _user_hash(username: str) -> str:
    return hashlib.sha256(username.strip().lower().encode("utf-8")).hexdigest()


# ── routes ──────────────────────────────────────────────────────────────────

bp = Blueprint("web_auth", __name__)


def _err(message: str, status: int, **extra):
    return jsonify(dict(extra, error=message)), status


def _body():
    return admin_auth._json_body()     # JSON only, 64 KiB, an object


def _refuse_plain_http_through_tunnel():
    if netctx.is_proxied() and not netctx.is_https():
        return _err("Sign in over https.", 403)
    return None


def _revoke_presented() -> None:
    for token in _presented_tokens():
        h = _hash(token)
        try:
            row = store.web_sessions.get_by_hash(h, db=_db())
            if row is not None and not row["revoked_at"]:
                store.web_sessions.revoke(row["id"], db=_db())
        except Exception:  # noqa: BLE001 - best effort; the new session still starts
            log.exception("could not revoke the previous session")
        with _lock:
            _cache.pop(h, None)


def _start_session(name: str, method: str, next_url: str):
    """Mint a session for ``name``; the response sets its cookie."""
    proxied = netctx.is_proxied()
    https = netctx.is_https()
    _revoke_presented()
    token = secrets.token_urlsafe(32)
    lifetime = ABSOLUTE_PROXIED_SECONDS if proxied else ABSOLUTE_SECONDS
    now = _clock()
    try:
        store.web_sessions.add(_hash(token), name=name, method=method,
                               ip=netctx.client_ip(), user_agent=request.headers.get("User-Agent"),
                               expires_at=_iso(now + lifetime), created_at=_iso(now), db=_db())
    except Exception:  # noqa: BLE001
        log.exception("could not store a new session")
        return _err("The hub's database is unavailable; try again shortly.", 503)
    log.info("sign-in: %s (%s) from %s%s", name, method, netctx.client_ip(),
             " via Cloudflare" if proxied else "")
    resp = make_response(jsonify({"ok": True, "name": name, "method": method,
                                  "next": safe_next(next_url)}))
    other = COOKIE_HTTP if https else COOKIE_HTTPS
    resp.set_cookie(cookie_name(https), token, max_age=int(lifetime), path="/", secure=https,
                    httponly=True, samesite="Lax")
    if request.cookies.get(other) is not None:
        resp.delete_cookie(other, path="/", secure=(other == COOKIE_HTTPS),
                           httponly=True, samesite="Lax")
    return resp


def _labcore_down(exc: Exception, method: str):
    log.warning("sign-in (%s) from %s: LabCore unavailable: %s", method, netctx.client_ip(),
                exc)
    msg = LABCORE_DOWN_TUNNEL if netctx.is_proxied() else LABCORE_DOWN
    return _err(msg, 503, labcore_unavailable=True)


def _throttled(wait: float):
    secs = int(wait) + 1
    resp = jsonify({"error": f"Too many failed sign-ins. Try again in {secs} s.",
                    "retry_after": secs})
    resp.status_code = 429
    resp.headers["Retry-After"] = str(secs)
    return resp


def _labcore_sign_in(method: str, ask, user_key: str, next_url: str, failed_message: str,
                     detail: str = ""):
    ip_key = netctx.throttle_key(netctx.client_ip())
    pair = f"pair:{ip_key}:{user_key}"
    keys = [(pair, LoginThrottle.PAIR_LIMIT), (f"ip:{ip_key}", LoginThrottle.IP_LIMIT)]
    ticket, wait = throttle.begin(keys, proxied=netctx.is_proxied())
    if ticket is None:
        log.warning("sign-in (%s) from %s refused: throttled", method, netctx.client_ip())
        return _throttled(wait)
    outcome = "refund"
    try:
        try:
            name = ask()
        except labcore_auth.LabCoreUnavailable as exc:
            return _labcore_down(exc, method)
        if not name:
            outcome = "fail"
            log.warning("failed sign-in (%s) from %s%s", method, netctx.client_ip(), detail)
            return _err(failed_message, 401)
        outcome = "ok"
    finally:
        throttle.finish(ticket, outcome, reset=(pair,))
    return _start_session(name, method, next_url)


@bp.route("/api/login", methods=["POST"])
def api_login():
    refused = _refuse_plain_http_through_tunnel()
    if refused:
        return refused
    body, err = _body()
    if err:
        return err
    username = body.get("username")
    password = body.get("password")
    if not isinstance(username, str) or not username.strip() \
            or not isinstance(password, str) or not password:
        return _err("Enter your LabLink username and password.", 400)
    if len(username) > 200 or len(password) > 1024:
        return _err("That username or password is too long.", 400)
    uh = _user_hash(username)
    return _labcore_sign_in("password",
                            lambda: labcore_auth.authenticate_user(username, password),
                            uh, body.get("next"), "Wrong username or password.",
                            f", user#{uh[:8]}")


@bp.route("/api/login/card", methods=["POST"])
def api_login_card():
    refused = _refuse_plain_http_through_tunnel()
    if refused:
        return refused
    body, err = _body()
    if err:
        return err
    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        return _err("No card code received.", 400)     # a stray Enter is not an attempt
    if len(code) > 256:
        return _err("That card code is too long.", 400)
    return _labcore_sign_in("card", lambda: labcore_auth.authenticate_card(code), "card",
                            body.get("next"), "Card not recognised.")


@bp.route("/api/login/admin", methods=["POST"])
def api_login_admin():
    if netctx.is_proxied():
        return _err(TUNNEL_NO_ADMIN, 403)
    body, err = _body()
    if err:
        return err
    try:
        res = admin_auth.check(body.get("password"),
                               netctx.throttle_key(netctx.client_ip()))
    except Exception:  # noqa: BLE001
        log.exception("admin sign-in: the check failed")
        return _err("The admin password could not be checked.", 503)
    if not res.ok:
        if res.reason == "throttled":
            return _throttled(res.retry_after or 1.0)
        if res.reason in ("no-store", "error"):
            return _err(res.message, 503)
        if res.reason == "not-set":
            return _err(res.message, 409, setup_url=admin_auth.SETUP_PATH)
        log.warning("failed sign-in (admin) from %s", netctx.client_ip())
        return _err("Incorrect admin password.", 401)
    return _start_session(ADMIN_NAME, "admin", body.get("next"))


@bp.route("/api/logout", methods=["POST"])
def api_logout():
    s = current_user()
    if s is not None:
        try:
            store.web_sessions.revoke(s["id"], db=_db())
        except Exception:  # noqa: BLE001
            log.exception("could not revoke the session at sign-out")
            return _err("The hub's database is unavailable; try again shortly.", 503)
        with _lock:
            _cache.pop(s["token_hash"], None)
        log.info("sign-out: %s from %s", s["name"], netctx.client_ip())
    resp = make_response(jsonify({"ok": True}))
    for name in (COOKIE_HTTP, COOKIE_HTTPS):
        if request.cookies.get(name) is not None:
            resp.delete_cookie(name, path="/", secure=(name == COOKIE_HTTPS), httponly=True,
                               samesite="Lax")
    return resp


@bp.route("/api/session", methods=["GET"])
def api_session():
    s = current_user()
    if s is None:           # the gate refuses first; kept for safety
        return _login_required()
    return jsonify({"name": s["name"], "method": s["method"]})


@bp.route(LOGIN_PATH, methods=["GET"])
def login_page():
    nxt = safe_next(request.args.get("next"))
    if current_user() is not None:
        return redirect(nxt, 302)
    try:
        from version import APP_VERSION
    except Exception:  # noqa: BLE001
        APP_VERSION = "dev"
    return render_template("login.html", next_url=nxt, app_version=APP_VERSION,
                           allow_admin=not netctx.is_proxied())


# ── sessions admin (used by hub_admin) ──────────────────────────────────────

def active_sessions() -> list:
    """The live sessions for the admin page (no token hashes)."""
    flush_seen()
    rows = store.web_sessions.list_active(idle_seconds=IDLE_SECONDS, db=_db())
    return [{"id": r["id"], "name": r["name"], "method": r["method"], "ip": r["ip"],
             "created_at": r["created_at"], "last_seen": r["last_seen"],
             "user_agent": r["user_agent"]} for r in rows]


def revoke(session_id: int) -> bool:
    ok = store.web_sessions.revoke(int(session_id), db=_db())
    clear_cache()
    return ok


def revoke_name(name: str) -> list:
    ids = store.web_sessions.revoke_name(name, db=_db())
    clear_cache()
    return ids


def revoke_method(method: str) -> list:
    ids = store.web_sessions.revoke_method(method, db=_db())
    clear_cache()
    return ids


def prune(db=None) -> int:
    return store.web_sessions.prune(older_than_days=PRUNE_DAYS, idle_seconds=IDLE_SECONDS,
                                    db=db if db is not None else _db())
