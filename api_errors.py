"""api_errors.py: every failure on an ``/api/`` path answers JSON (v4.0.0).

Before this, an ``HTTPException`` without its own handler (a 405, werkzeug's
400 for malformed JSON) or an uncaught exception answered werkzeug's HTML
page, and the front end showed ``SyntaxError: Unexpected token '<'``.
``install(app)`` registers two app-level handlers:

* any ``HTTPException`` on an ``/api/`` path → ``{error, status, ref}`` with
  the exception's own description, its status and headers (``Allow`` on a
  405, ``Retry-After`` on a 503), logged at INFO (a deliberate 502/503/504
  keeps its status and is logged at ERROR; a plain 500 is the generic answer
  below);
* any uncaught ``Exception`` on an ``/api/`` path → 500 ``{error, status,
  ref}`` with a generic message naming the ref, and the full traceback logged
  at ERROR with the ref, method, path and ``netctx.client_ip()``. Never the
  request body, never internals in the answer.

Every request-derived field in a log line (method, path) goes through
``netctx.log_safe``: ``request.path`` is percent-decoded, and a ``%0A`` in a
segment would otherwise write a forged line into ``app.log``.

``ref`` is a short random id that ties what the person saw to the line in
``app.log``. Handlers registered for a specific code or exception class
(app.py's JSON 404, ``HubUnavailable``, admin_auth's JSON 413, ...) are more
specific, so Flask keeps using them. Page (non-``/api/``) errors keep
Flask's HTML pages: the handlers hand those back untouched.

Every response also carries ``X-GC-Hub: 1``, so the front end
(``GCSession.readJson``) can tell the hub's own page from Cloudflare's (through
the tunnel every response carries ``cf-ray``).
"""
from __future__ import annotations

import logging
import secrets

from flask import jsonify, request
from werkzeug.exceptions import HTTPException

import netctx

log = logging.getLogger("api_errors")

HUB_HEADER = "X-GC-Hub"
GENERIC_500 = ("Something went wrong on the hub (ref {ref}). Send the diagnostics bundle "
               "or app.log to have it looked at.")
# Headers of an HTTPException worth carrying over to the JSON answer.
_KEEP_HEADERS = frozenset({"allow", "retry-after", "www-authenticate"})


def new_ref() -> str:
    """A short random id for one failure (8 hex characters)."""
    return secrets.token_hex(4).upper()


def _is_api() -> bool:
    return request.path.startswith("/api/")


def _client() -> str:
    try:
        return netctx.client_ip() or "unknown"
    except Exception:  # noqa: BLE001 - never fail while reporting a failure
        return "unknown"


def _answer(message: str, status: int, ref: str):
    resp = jsonify({"error": message, "status": status, "ref": ref})
    resp.status_code = status
    return resp


def _where() -> tuple:
    """(method, path) for a log line, control characters escaped: the path is
    percent-decoded, so ``%0A`` in a segment would otherwise start a forged
    line in app.log."""
    return netctx.log_safe(request.method), netctx.log_safe(request.path)


def _server_error(exc: BaseException):
    ref = new_ref()
    method, path = _where()
    log.error("unhandled error ref %s on %s %s from %s", ref, method, path,
              _client(), exc_info=(type(exc), exc, exc.__traceback__))
    return _answer(GENERIC_500.format(ref=ref), 500, ref)


def handle_http_exception(exc: HTTPException):
    if not _is_api() or exc.code is None or exc.code < 400:
        return exc
    original = getattr(exc, "original_exception", None)
    if exc.code >= 500 and original is not None:
        return _server_error(original)          # Flask's wrapper around an uncaught error
    if exc.code == 500:
        return _server_error(exc)
    ref = new_ref()
    method, path = _where()
    # a deliberate 5xx (502/503/504: upstream or busy) keeps its own status
    level = logging.ERROR if exc.code >= 500 else logging.INFO
    log.log(level, "HTTP %s ref %s on %s %s from %s: %s", exc.code, ref, method, path,
            _client(), exc.name)
    resp = _answer(exc.description or exc.name, exc.code, ref)
    for key, value in exc.get_headers():
        if key.lower() in _KEEP_HEADERS:
            resp.headers[key] = value
    return resp


def handle_exception(exc: Exception):
    if not _is_api():
        raise exc      # Flask logs it and serves its HTML 500 page, as before
    return _server_error(exc)


def _mark_hub(response):
    response.headers.setdefault(HUB_HEADER, "1")
    return response


def install(app) -> None:
    """Register the ``/api/`` JSON error handlers and the ``X-GC-Hub`` header."""
    app.register_error_handler(HTTPException, handle_http_exception)
    app.register_error_handler(Exception, handle_exception)
    app.after_request(_mark_hub)
