"""The hub side of the agent HTTP contract (phase 2, 2B1; contracts §1).

Tokens
======

``mint_token(instrument_id)`` returns ``secrets.token_urlsafe(32)`` and
stores only its sha256 (``instruments.token_hash``) and
``token_issued_at``. One token per instrument: minting another revokes the
previous one. ``verify_token(token)`` looks the hash up and compares it
with ``hmac.compare_digest``; it returns the instrument row or ``None``.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Any, Optional

import admin_auth
import store

log = logging.getLogger("ingest_api")


def _db(db):
    return db if db is not None else admin_auth.hub_db()


# ── tokens ──────────────────────────────────────────────────────────────────

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token(instrument_id: str, *, db=None) -> str:
    """A new token for ``instrument_id`` (revoking any previous one)."""
    db = _db(db)
    token = secrets.token_urlsafe(32)
    with store.connection(db) as conn:
        with store.write_txn(conn):
            if store.instruments.get(instrument_id, db=conn) is None:
                raise LookupError(f"unknown instrument {instrument_id!r}")
            store.instruments.upsert({"id": instrument_id, "token_hash": token_hash(token),
                                      "token_issued_at": store.now_iso()}, db=conn)
    log.warning("agent token minted for %s (any previous token is revoked)", instrument_id)
    return token


def verify_token(token: Any, *, db=None) -> Optional[dict]:
    """The instrument this token belongs to, or ``None``."""
    if not isinstance(token, str) or not token or len(token) > 512:
        return None
    digest = token_hash(token)
    with store.connection(_db(db)) as conn:
        row = conn.execute("SELECT * FROM instruments WHERE token_hash=?", (digest,)).fetchone()
    if row is None or not hmac.compare_digest(str(row["token_hash"]), digest):
        return None
    return dict(row)


def bearer_token(header: Optional[str]) -> Optional[str]:
    """The token from an ``Authorization: Bearer <token>`` header, or ``None``."""
    if not isinstance(header, str):
        return None
    parts = header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    tok = parts[1].strip()
    return tok or None
