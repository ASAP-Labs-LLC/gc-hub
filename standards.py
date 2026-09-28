"""Comparison standards tagged by instrument (phase 2, 2A2, spec D12).

The standards' CDFs stay where they are, in the comparison-standards folder
(``settings['comparison_defaults_dir']``, default ``paths.standards_dir()``).
The store's ``standards`` table tags each file with an instrument:

* **Migration.** The first time ``sync`` finds the folder with at least one
  ``*.cdf`` and ``gc1`` present, every file in it is registered as ``gc1``
  (they were all run on GC-1), once, recorded in ``settings_kv[MIGRATED_KEY]``.
  An unreachable or empty folder, or no ``gc1`` yet, registers nothing and
  leaves the migration for a later call.
* **Later files** (added through the old comparison-standard routes) are
  registered untagged (``instrument_id`` NULL) until an admin tags them.
* A registered file that has gone is listed with ``missing: true``; its tag is
  kept (the file may come back).

``for_sample(dir, instrument)`` is the picker's order: the sample's own
instrument's standards first, then the rest marked ``cross_instrument`` with
the warning text the screen and the PDF show. ``paths_for`` is the set
best-fit scores against (the sample's own instrument's files).

API::

    sync(comp_dir, *, db) -> {migrated, registered}
    list_standards(comp_dir, instrument=None, *, db) -> [{id, name, instrument_id,
        instrument_name, file_name, missing, added_at}]           # never server paths
    set_instrument(standard_id, instrument_id | None, *, db) -> row   # LookupError
    for_sample(comp_dir, sample_instrument, *, db) -> [row + {cross_instrument, warning}]
    cross_instrument_warning(sample_inst_name, standard_name, standard_inst_name | None) -> str
    paths_for(comp_dir, instrument, *, db) -> [Path]

Stdlib + ``store`` only.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import store

log = logging.getLogger("standards")

MIGRATED_KEY = "standards_migrated"
GC1 = "gc1"


def _files(comp_dir) -> list:
    d = Path(comp_dir)
    if not d.is_dir():
        return []
    return sorted(p.resolve() for p in d.iterdir() if p.suffix.lower() == ".cdf" and p.is_file())


def sync(comp_dir, *, db: store.Db = None) -> dict:
    """Register the folder's files: all as ``gc1`` on the first run with gc1
    present, afterwards new ones untagged."""
    try:
        files = _files(comp_dir)
    except OSError as exc:              # an unreachable share: try again next time
        log.warning("standards: cannot list %s: %s", comp_dir, exc)
        return {"migrated": 0, "registered": 0}
    migrated = registered = 0
    with store.connection(db) as conn:
        with store.write_txn(conn):
            if conn.execute("SELECT 1 FROM instruments WHERE id=?", (GC1,)).fetchone() is None:
                return {"migrated": 0, "registered": 0}
            first = store.settings_kv.get(MIGRATED_KEY, db=conn) is None
            if first and not files:
                # Review I2: the migration counts as done only once it has seen
                # the folder with at least one CDF (a share offline at first use,
                # or an empty folder, must not turn later files into "untagged").
                return {"migrated": 0, "registered": 0}
            known = {r["cdf_path"] for r in conn.execute("SELECT cdf_path FROM standards")}
            for p in files:
                if str(p) in known:
                    continue
                conn.execute("INSERT INTO standards(name, instrument_id, cdf_path, added_at) "
                             "VALUES (?,?,?,?)", (p.stem, GC1 if first else None, str(p),
                                                  store.now_iso()))
                if first:
                    migrated += 1
                else:
                    registered += 1
            if first:
                store.settings_kv.set(MIGRATED_KEY, store.now_iso(), db=conn)
    if migrated:
        log.warning("standards: %d existing comparison standard(s) tagged gc1 (D12)", migrated)
    return {"migrated": migrated, "registered": registered}


def _public(r: dict, names: dict) -> dict:
    p = Path(r["cdf_path"] or "")
    return {"id": r["id"], "name": r["name"], "instrument_id": r["instrument_id"],
            "instrument_name": names.get(r["instrument_id"]) if r["instrument_id"] else None,
            "file_name": p.name, "missing": not p.is_file(), "added_at": r["added_at"]}


def list_standards(comp_dir, instrument: Optional[str] = None, *, db: store.Db = None) -> list:
    """Every registered standard (after ``sync``), by name; only ``instrument``'s
    when given. Rows under another folder than ``comp_dir`` are left out."""
    sync(comp_dir, db=db)
    base = Path(comp_dir).resolve() if Path(comp_dir).exists() else None
    names = {r["id"]: r["name"] for r in store.instruments.list(db=db)}
    sql, args = "SELECT * FROM standards", []
    if instrument:
        sql += " WHERE instrument_id=?"
        args.append(instrument)
    with store.connection(db) as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY name COLLATE NOCASE, id", args)]
    if base is None:
        return []
    return [_public(r, names) for r in rows if Path(r["cdf_path"]).parent == base]


def set_instrument(standard_id, instrument_id: Optional[str], *, db: store.Db = None) -> dict:
    """Tag a standard with an instrument (``None`` untags it). ``LookupError`` if
    either is unknown."""
    with store.connection(db) as conn:
        with store.write_txn(conn):
            row = conn.execute("SELECT * FROM standards WHERE id=?", (int(standard_id),)).fetchone()
            if row is None:
                raise LookupError(f"standard {standard_id} not found")
            if instrument_id is not None and store.instruments.get(instrument_id, db=conn) is None:
                raise LookupError(f"unknown instrument {instrument_id!r}")
            conn.execute("UPDATE standards SET instrument_id=? WHERE id=?", (instrument_id, int(standard_id)))
            names = {r["id"]: r["name"] for r in store.instruments.list(db=conn)}
            out = _public(dict(conn.execute("SELECT * FROM standards WHERE id=?",
                                            (int(standard_id),)).fetchone()), names)
    log.warning("standards: %s tagged %s", out["name"], instrument_id)
    return out


def cross_instrument_warning(sample_inst_name: str, standard_name: str,
                             standard_inst_name: Optional[str]) -> str:
    if not standard_inst_name:
        return (f"Standard {standard_name} is not tagged with an instrument; it may not have been "
                f"run on {sample_inst_name}, so compare with care.")
    return (f"Standard {standard_name} was run on {standard_inst_name}, not {sample_inst_name}: "
            f"retention times can differ between instruments, so compare with care.")


def for_sample(comp_dir, sample_instrument: str, *, db: store.Db = None) -> list:
    """The picker for a sample of ``sample_instrument``: its own standards first
    (no warning), then every other one with ``cross_instrument`` and a warning."""
    rows = list_standards(comp_dir, db=db)
    inst = store.instruments.get(sample_instrument, db=db) if sample_instrument else None
    inst_name = (inst or {}).get("name") or sample_instrument or "this instrument"
    own, other = [], []
    for r in rows:
        if r["instrument_id"] and r["instrument_id"] == sample_instrument:
            own.append(dict(r, cross_instrument=False, warning=None))
        else:
            other.append(dict(r, cross_instrument=True, warning=cross_instrument_warning(
                inst_name, r["name"], r["instrument_name"])))
    return own + other


def paths_for(comp_dir, instrument: str, *, db: store.Db = None) -> list:
    """The existing files tagged ``instrument`` (best-fit's standards set)."""
    rows = list_standards(comp_dir, instrument, db=db)
    base = Path(comp_dir).resolve()
    return [base / r["file_name"] for r in rows if not r["missing"]]
