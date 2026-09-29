"""2B1 T1: the additive ``agents.package_sha256`` column (schema v1, unreleased)."""
from __future__ import annotations

import store


def test_fresh_store_has_agents_package_sha256(tmp_path):
    db = tmp_path / "gc.db"
    store.migrate(db)
    with store.connection(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
    assert "package_sha256" in cols


def test_required_columns_include_it():
    assert "package_sha256" in store.REQUIRED_COLUMNS["agents"]
