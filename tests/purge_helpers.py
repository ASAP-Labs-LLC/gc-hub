"""A two-instrument hub for the purge tests (v3.1), built on ``hub_boot``.

``build_two_gc_hub(root)`` starts from ``hub_boot.build_hub`` (gc1 with
blank/final/rerun/backfill/other/released/slashed/result-only samples, gc2
with one held sample) and makes gc2 a real second instrument with data in
every table that references a sample:

* gc2 is calibrated (gc1's ladder) and has its own corrections, so its held
  sample and three more (``g2_blank``, ``g2_final``, ``g2_backfill``) go
  final; ``g2_final`` has a conflict, a comment, a report_log row, a cache
  row and a queued reprocess job;
* gc1 gets the same (on ``final``), plus ``bf_blank`` (a backfill blank) and
  ``live_on_bf`` (a live sample whose revision subtracted ``bf_blank``: a
  blank used across the ``scope=backfill`` boundary);
* both instruments' results CSVs are flushed by a real ``HubExporter``, so
  export rows carry ``hub_appended_at`` and each CSV has a sidecar.

``dump(db)`` is every table's rows (``sqlite_sequence`` included) as sorted
lists of dicts, for row-for-row comparisons; ``tree(root)`` maps every file
under a folder to its sha256.

Not a test module; needs numpy, netCDF4, scipy.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import hub_boot
import make_golden
import store
from hub_boot import SIMDIS, build_hub

import cdf_fixtures as fx
import exports
import pipeline


def _corrections(h, instrument: str) -> None:
    with store.connection(h.db) as conn:
        with store.write_txn(conn):
            store.corrections.set_all(conn, instrument,
                                      {k.replace(" - D86", ""): v
                                       for k, v in make_golden.D86_CORRECTIONS.items()},
                                      by="test", reason="test")


def _extras(h, key: str, instrument_label: str) -> None:
    sid = h.ids[key]
    rev = store.samples.get(sid, db=h.db)["current_revision"]
    store.sample_comments.add(sid, text=f"{instrument_label} comment", source="free",
                              author_initials="TO", author_ip="10.0.0.1", revision=rev,
                              author_name="Test Operator", db=h.db)
    store.report_log.add(sid, kind="download", revision=rev, standard_name="Diesel",
                         user_name="Test Operator", db=h.db)
    store.sample_cache.put(sid, rules_fingerprint="fp", flags="[]", db=h.db)


def store_worker(h) -> pipeline.Worker:
    """A Worker on the hub's own (store) corrections: hub_boot's file provider
    serves gc1 only."""
    return pipeline.Worker(db=h.db, data_dir=h.data, conf_fn=lambda: h.conf)


def _calibrate_from_own_sample(h, instrument: str, key: str, when: datetime) -> None:
    """As production does it: the n-alkane run arrives as one of the
    instrument's own samples (a file under ``cdf/<inst>/...``) and the
    calibration is set to that sample's data-relative ``cdf_path``."""
    cal = fx.calibration_cdf(h.src / f"CAL_{instrument}.CDF", injected=when, method_name=SIMDIS)
    res = pipeline.submit(instrument, cal, conf=h.conf, data_dir=h.data, db=h.db)
    assert res.outcome == "created", res
    h.ids[key] = res.sample_id
    rel = store.samples.get(res.sample_id, db=h.db)["cdf_path"]
    assert rel.startswith(f"cdf/{instrument}/"), rel
    store.instruments.upsert({"id": instrument, "calibration_cdf": rel,
                              "calibration_assignments": json.dumps(
                                  make_golden.calibration_entries())}, db=h.db)


def build_two_gc_hub(root: Path):
    h = build_hub(Path(root))
    _calibrate_from_own_sample(h, "gc1", "g1_cal", datetime(2026, 9, 16, 9, 0, 0))
    _calibrate_from_own_sample(h, "gc2", "g2_cal", datetime(2026, 9, 16, 10, 0, 0))
    store.instruments.upsert({"id": "gc2", "lem_machine_uid": "lem-gc2"}, db=h.db)
    _corrections(h, "gc1")
    _corrections(h, "gc2")
    pipeline.on_calibration_saved("gc2", db=h.db)

    h.submit("g2_blank", h._cdf("blank", injected=datetime(2026, 9, 24, 9, 0, 0),
                                method_name=SIMDIS), instrument="gc2")
    h.submit("g2_final", h._cdf("sample", name="60001", injected=datetime(2026, 9, 25, 10, 0, 0),
                                shift=0.07, method_name=SIMDIS), instrument="gc2")
    h.submit("g2_backfill", h._cdf("sample", name="60000",
                                   injected=datetime(2026, 9, 20, 10, 0, 0), shift=0.08,
                                   method_name=SIMDIS), instrument="gc2")
    # a backfill blank on gc1, and a live gc1 sample that subtracts it
    h.submit("bf_blank", h._cdf("blank", injected=datetime(2026, 9, 21, 8, 0, 0),
                                method_name=SIMDIS))
    h.submit("live_on_bf", h._cdf("sample", name="40400", injected=datetime(2026, 9, 23, 10, 0, 0),
                                  shift=0.09, method_name=SIMDIS))
    store_worker(h).run_until_idle()

    # conflicts: same lab ID and injection time, other bytes
    for key, inst, name, when in (("final", "gc1", "40304", datetime(2026, 9, 25, 14, 23, 0)),
                                  ("g2_final", "gc2", "60001", datetime(2026, 9, 25, 10, 0, 0))):
        res = pipeline.submit(inst, h._cdf("sample", name=name, injected=when, shift=0.3,
                                           method_name=SIMDIS),
                              conf=h.conf, data_dir=h.data, db=h.db)
        assert res.outcome == "conflict", res
        h.ids[f"conflict_{key}"] = res.conflict_id

    exp = exports.HubExporter(h.db, data_dir=h.data)
    for inst in ("gc1", "gc2"):
        r = exp.flush(inst)
        assert r.error is None and r.appended > 0, r
    _extras(h, "final", "gc1")
    _extras(h, "g2_final", "gc2")
    # queued jobs, left queued
    pipeline.request_reprocess(h.ids["rerun"], by="test", db=h.db)
    pipeline.request_reprocess(h.ids["g2_final"], by="test", db=h.db)
    for key in ("g2_blank", "g2_final", "g2_backfill", "held", "live_on_bf"):
        s = store.samples.get(h.ids[key], db=h.db)
        assert s["status"] == "final", (key, s["status"], s["error"])
    rev = store.get_revision(h.ids["live_on_bf"], db=h.db)
    assert rev["blank_used"] == h.ids["bf_blank"], rev["blank_used"]
    h.exporter = exp
    return h


class HubCopy:
    """A copy of the module's built hub (same ids, conf and source CDFs)."""

    def __init__(self, template, root: Path):
        shutil.copytree(template.data, root / "data")
        self.root = root
        self.data = root / "data"
        self.db = self.data / store.DB_FILENAME
        self.ids = dict(template.ids)
        self.conf = template.conf
        self.src = template.src
        self._n = 1000
        self.exporter = exports.HubExporter(self.db, data_dir=self.data)
        self.notes = []

    def notifier(self, level, message):
        self.notes.append((level, message))

    def cdf(self, name, when, shift=0.11):
        self._n += 1
        return fx.sample_cdf(self.root / f"new{self._n}.CDF", name=name, injected=when,
                                shift=shift, method_name=SIMDIS)

    def worker(self):
        return store_worker(self)

    def run(self, instrument="gc1", scope="all", confirm=None, **kw):
        name = store.instruments.get(instrument, db=self.db)["name"]
        kw.setdefault("exporter", self.exporter)
        kw.setdefault("notifier", self.notifier)
        import purge
        return purge.run(instrument, scope, confirm_text=confirm or f"PURGE {name}",
                         by="Ryan C (10.0.0.9)", db=self.db, data_dir=self.data, **kw)

    def preview(self, instrument="gc1", scope="all"):
        import purge
        return purge.preview(instrument, scope, db=self.db, data_dir=self.data)


def dump(db) -> dict:
    """``{table: [row dict, ...]}`` for every table, rows sorted."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        out = {}
        for t in tables:
            rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{t}"')]
            out[t] = sorted(rows, key=lambda d: json.dumps(d, sort_keys=True, default=str))
        return out
    finally:
        conn.close()


def tree(root: Path) -> dict:
    """``{relative posix path: sha256}`` of every file under ``root``."""
    root = Path(root)
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def instrument_sample_ids(db, instrument: str, backfill_only: bool = False) -> set:
    sql = "SELECT id FROM samples WHERE instrument_id=?" + (" AND backfill=1" if backfill_only else "")
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute(sql, (instrument,))}
    finally:
        conn.close()


# The column that says which sample a row belongs to (what the tests expect
# the purge to delete by); samples itself by id.
OWNER = {"samples": "id", "sample_results": "sample_id", "export_rows": "sample_id",
         "jobs": "sample_id", "sample_cache": "sample_id", "conflicts": "existing_sample_id",
         "sample_comments": "sample_id", "report_log": "sample_id"}


def split_purge_events(after: dict) -> tuple:
    """``(dump without the purges' own instrument_events rows, those rows)``.
    ``instrument_events`` (schema v4) is instrument history, never sample
    data: a purge keeps every row there and adds one (kind ``purge``)."""
    rows = after.get("instrument_events")
    if rows is None:
        return after, []
    out = dict(after)
    out["instrument_events"] = [r for r in rows if r["kind"] != "purge"]
    return out, [r for r in rows if r["kind"] == "purge"]


def without(before: dict, purged: set) -> dict:
    """``before`` minus every row owned by a purged sample."""
    out = {}
    for t, rows in before.items():
        col = OWNER.get(t)
        out[t] = [r for r in rows if col is None or r[col] not in purged]
    return out


__all__ = ["build_two_gc_hub", "HubCopy", "store_worker", "split_purge_events", "dump", "tree", "instrument_sample_ids", "without", "OWNER",
           "hub_boot", "fx"]
