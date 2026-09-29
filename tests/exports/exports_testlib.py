"""Shared helpers for the tests in tests/exports (a plain module, not a conftest).

No ``__init__.py`` in this folder on purpose: a ``tests/exports`` *package*
would shadow the top-level ``exports`` module.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta
import uuid
from pathlib import Path

import distill
import store

CSV_HEADER = distill.CSV_HEADER
_counter = itertools.count(1)
_BASE = datetime(2026, 9, 25)


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "gc.db"
    store.migrate(db)
    store.instruments.upsert({"id": "gc1", "name": "GC-1"}, db=db)
    store.instruments.upsert({"id": "gc2", "name": "GC-2"}, db=db)
    return db


def results_for(lab_id: str, n: int) -> dict:
    """A results dict keyed by CSV_HEADER names (as sample_results.results holds)."""
    row = {col: round(100.0 + n + i / 7.0, 2) for i, col in enumerate(CSV_HEADER)}
    row["Lab ID"] = lab_id
    row["InjectionDateTime"] = f"2026-09-25 00:{n % 60:02d}:00"
    row["Best Fit"] = "Diesel"
    row["Fit Score"] = 0.97
    row["Source File"] = ""
    return row


def add_final(db: Path, instrument: str = "gc1", lab_id: str | None = None) -> int:
    """One final sample with a revision and a pending export row; returns its seq."""
    import exports  # imported late so a missing module fails the test, not collection

    n = next(_counter)
    lab_id = lab_id or f"4{n:04d}"
    results = results_for(lab_id, n)
    cdf_path = f"cdf/{instrument}/2026/09/{lab_id}_{n}.CDF"
    with store.connection(db) as conn, store.write_txn(conn):
        sid = store.samples.insert_received(
            instrument, lab_id, str(_BASE + timedelta(seconds=n)), "cdf",
            cdf_sha256=f"sha-{uuid.uuid4().hex}", cdf_path=cdf_path,
            method_name="SIMDISB.M", source_name=f"{lab_id}.CDF", db=conn)
        rev = store.add_revision(conn, sid, results, reason="processed")
        seq = store.export_rows.append_pending(
            conn, instrument, sid, rev, exports.format_line(results, cdf_path))
        store.samples.set_status(sid, "final", db=conn)
    return seq


def pending(db: Path, instrument: str = "gc1") -> list[int]:
    return [r["seq"] for r in store.export_rows.pending_hub_appends(instrument, db=db)]


def ledger_lines(db: Path, instrument: str = "gc1") -> list[dict]:
    return store.export_rows.rows_after(instrument, 0, limit=100000, db=db)


def header_bytes() -> bytes:
    import exports

    return exports.header_line().encode("utf-8")
