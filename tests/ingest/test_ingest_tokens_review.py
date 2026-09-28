"""Security review 1 of 2B1: token minting under a race (M2), a pre-generated
token (M3), revocation (M1), public error messages (M4)."""
from __future__ import annotations

from datetime import datetime

import pytest

import ingest_api
import store


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "gc.db"
    store.migrate(p)
    store.instruments.upsert({"id": "gc1", "name": "GC-1", "live_since": datetime(2020, 1, 1)},
                             db=p)
    return p


def test_require_no_token_is_checked_in_the_write(db):
    first = ingest_api.mint_token("gc1", db=db, require_no_token=True)
    with pytest.raises(ingest_api.TokenExists):
        ingest_api.mint_token("gc1", db=db, require_no_token=True)
    assert ingest_api.verify_token(first, db=db)["id"] == "gc1"


def test_pre_generated_token(db):
    tok = ingest_api.new_token()
    assert ingest_api.mint_token("gc1", db=db, token=tok) == tok
    assert ingest_api.verify_token(tok, db=db)["id"] == "gc1"


def test_revoke(db):
    tok = ingest_api.mint_token("gc1", db=db)
    ingest_api.revoke_token("gc1", db=db)
    assert ingest_api.verify_token(tok, db=db) is None
    row = store.instruments.get("gc1", db=db)
    assert row["token_hash"] is None and row["token_issued_at"] is None
    with pytest.raises(LookupError):
        ingest_api.revoke_token("gc9", db=db)


def test_public_message_strips_server_paths():
    msg = ("not a readable CDF: [Errno -101] NetCDF: HDF error: "
           "'/srv/data/cdf/.incoming/82f5.CDF'")
    assert ingest_api.public_message(msg) == \
        "not a readable CDF: [Errno -101] NetCDF: HDF error: '82f5.CDF'"
    assert ingest_api.public_message(r"bad C:\ASAPApps\gc\data\cdf\x.CDF") == "bad x.CDF"
    assert ingest_api.public_message("truncated NetCDF file: 5 bytes") == \
        "truncated NetCDF file: 5 bytes"
