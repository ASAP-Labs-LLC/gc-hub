"""2B1 T3: agent tokens. Only the sha256 is stored; a new mint revokes the old."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

import pytest

import ingest_api
import store


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "gc.db"
    store.migrate(p)
    for iid in ("gc1", "gc2"):
        store.instruments.upsert({"id": iid, "name": iid.upper(),
                                  "live_since": datetime(2020, 1, 1)}, db=p)
    return p


def _dump(db) -> str:
    with sqlite3.connect(db) as c:
        return "\n".join(c.iterdump())


def test_mint_stores_only_the_hash(db):
    tok = ingest_api.mint_token("gc1", db=db)
    assert isinstance(tok, str) and len(tok) >= 40
    row = store.instruments.get("gc1", db=db)
    assert row["token_hash"] == hashlib.sha256(tok.encode()).hexdigest()
    assert row["token_issued_at"]
    assert tok not in _dump(db)


def test_verify(db):
    t1 = ingest_api.mint_token("gc1", db=db)
    t2 = ingest_api.mint_token("gc2", db=db)
    assert ingest_api.verify_token(t1, db=db)["id"] == "gc1"
    assert ingest_api.verify_token(t2, db=db)["id"] == "gc2"
    for bad in ("", None, "x" * 43, t1 + "x", t1[:-1], 123):
        assert ingest_api.verify_token(bad, db=db) is None


def test_second_mint_revokes_the_first(db):
    old = ingest_api.mint_token("gc1", db=db)
    new = ingest_api.mint_token("gc1", db=db)
    assert old != new
    assert ingest_api.verify_token(old, db=db) is None
    assert ingest_api.verify_token(new, db=db)["id"] == "gc1"


def test_unknown_instrument(db):
    with pytest.raises(LookupError):
        ingest_api.mint_token("gc9", db=db)


def test_bearer_parsing():
    assert ingest_api.bearer_token("Bearer abc") == "abc"
    assert ingest_api.bearer_token("bearer  abc ") == "abc"
    for bad in (None, "", "Basic abc", "Bearer", "Bearer ", "abc"):
        assert ingest_api.bearer_token(bad) is None
