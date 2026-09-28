"""pipeline.Worker (2A1 T2): the status machine, blank at-or-before, corrections,
the one-transaction final write, the export gate, reprocess revisions,
restart requeue and the enqueue hooks."""
from __future__ import annotations

from pipeline_helpers import SIMDIS, hub  # noqa: F401  (hub: the fixture)

import csv
import io
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

import corrections
import distill
import exports
import pipeline
import store


def _run(hub, **kw) -> int:
    return hub.worker(**kw).run_until_idle()


def _rev(hub, sid, revision=None):
    return store.get_revision(sid, revision, db=hub.db)


def _exports(hub, instrument="gc1"):
    return store.export_rows.rows_after(instrument, 0, db=hub.db)


def _queued(hub, sid):
    return [j for j in store.jobs.list(state="queued", kind="process", db=hub.db)
            if j["sample_id"] == sid]


class FixedCorrections:
    """An injected provider (the D4b StoreProvider seam)."""

    def __init__(self, values=None, source="hub"):
        self.values = values if values is not None else {c: 0.0 for c in corrections.D86_CUTS}
        self.source = source
        self.calls = 0

    def get(self, instrument):
        self.calls += 1
        return corrections.Corrections(source=self.source, updated_at="2026-09-28T10:00:00+00:00",
                                       values=dict(self.values), updated_by="ryan")


# ── final ───────────────────────────────────────────────────────────────────

def test_final_writes_revision_export_row_and_status(hub):
    hub.gc1()
    blank = hub.submit(hub.cdf("blank")).sample_id
    sid = hub.submit(hub.cdf()).sample_id
    assert _run(hub) == 2
    s = hub.sample(sid)
    assert s["status"] == "final"
    assert s["error"] is None
    assert s["current_revision"] == 1
    rev = _rev(hub, sid)
    assert rev["reason"] == "processed"
    assert rev["blank_used"] == blank
    results = json.loads(rev["results"])
    assert list(results) == distill.CSV_HEADER
    assert results["Source File"] == s["cdf_path"]
    assert results["Lab ID"] == "40304"
    assert results["InjectionDateTime"] == "2026-09-25 14:23:00"
    used = json.loads(rev["corrections_used"])
    assert used["source"] == "file"
    assert used["values"]["IBP"] == -12.08 and used["values"]["20%"] == 0.0
    assert set(used["values"]) == set(corrections.D86_CUTS)
    cal = json.loads(rev["calibration_used"])
    assert cal["cdf"] == str(hub.cal)
    assert cal["sensitivity"] == 50.0
    assert cal["anchors"] == distill.calibration_anchors(hub.cal, hub.conf)["anchors"]
    assert json.loads(rev["d86_uncorrected"])["IBP"] != results["D86 IBP"]
    # the export row: the frozen CSV line of this revision
    rows = [r for r in _exports(hub) if r["sample_id"] == sid]
    assert len(rows) == 1
    assert rows[0]["revision"] == 1
    assert rows[0]["line"] == exports.format_line(rev["results"], s["cdf_path"])
    assert rows[0]["line"].endswith("\r\n")
    # the blank itself is processed without a blank
    b = _rev(hub, blank)
    assert b["blank_used"] is None
    assert hub.sample(blank)["status"] == "final"
    # the job is done
    assert store.jobs.list(state="queued", db=hub.db) == []


def test_format_line_is_injectable(hub):
    hub.gc1()
    calls = []

    def fmt(results_json, source_file):
        calls.append((json.loads(results_json)["Lab ID"], source_file))
        return "LINE\r\n"

    sid = hub.submit(hub.cdf()).sample_id
    _run(hub, format_line=fmt)
    assert calls == [("40304", hub.sample(sid)["cdf_path"])]
    assert [r["line"] for r in _exports(hub)] == ["LINE\r\n"]


def test_no_blank_available(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub)
    assert _rev(hub, sid)["blank_used"] is None
    assert hub.sample(sid)["status"] == "final"


def test_gc_cal_cdf_is_ignored(hub, monkeypatch):
    monkeypatch.setenv("GC_CAL_CDF", str(hub.root / "nope.CDF"))
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub)
    assert hub.sample(sid)["status"] == "final"


# ── method branches ─────────────────────────────────────────────────────────

def test_unmapped_method_is_other_method(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf(method_name="GASOLINE.M")).sample_id
    _run(hub)
    s = hub.sample(sid)
    assert s["status"] == "other_method"
    assert s["current_revision"] is None
    assert _exports(hub) == []
    assert _queued(hub, sid) == []


def test_missing_method_is_review_method(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf(method_name=None)).sample_id
    _run(hub)
    assert hub.sample(sid)["status"] == "review_method"
    assert hub.sample(sid)["current_revision"] is None


def test_mapping_a_method_queues_its_other_method_samples(hub):
    hub.gc1()
    gas = hub.submit(hub.cdf(method_name="gasoline.m")).sample_id
    other = hub.submit(hub.cdf(method_name="OTHER.M", shift=0.1, name="40999")).sample_id
    _run(hub)
    mm = {"SIMDISB.M": "D2887", "SIMDISTB.M": "D2887", "GASOLINE.M": "D2887"}
    store.instruments.upsert({"id": "gc1", "method_map": mm}, db=hub.db)
    assert pipeline.on_method_mapped("gc1", " Gasoline.M ", db=hub.db) == 1
    _run(hub)
    assert hub.sample(gas)["status"] == "final"
    assert hub.sample(other)["status"] == "other_method"


# ── calibration ─────────────────────────────────────────────────────────────

def test_unusable_calibration_is_awaiting_calibration_until_saved(hub):
    row = hub.gc1()
    store.instruments.upsert({"id": "gc1", "calibration_assignments": "[]"}, db=hub.db)
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub)
    s = hub.sample(sid)
    assert s["status"] == "awaiting_calibration"
    assert "usable peak assignment" in s["error"]
    assert s["current_revision"] is None
    assert _queued(hub, sid) == []
    # the calibration is saved: only awaiting_calibration samples are queued
    store.instruments.upsert({"id": "gc1", "calibration_assignments": row["calibration_assignments"]},
                             db=hub.db)
    assert pipeline.on_calibration_saved("gc1", db=hub.db) == 1
    _run(hub)
    s = hub.sample(sid)
    assert (s["status"], s["error"], s["current_revision"]) == ("final", None, 1)


def test_missing_calibration_cdf_is_awaiting_calibration(hub):
    hub.gc1()
    store.instruments.upsert({"id": "gc1", "calibration_cdf": str(hub.root / "gone.CDF")}, db=hub.db)
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub)
    assert hub.sample(sid)["status"] == "awaiting_calibration"


# ── corrections ─────────────────────────────────────────────────────────────

def test_corrections_unavailable_is_pending_with_a_retry_in_5_minutes(hub):
    hub.gc1()
    hub.corrections.unlink()
    sid = hub.submit(hub.cdf()).sample_id
    t0 = datetime.now(timezone.utc)
    _run(hub)
    s = hub.sample(sid)
    assert s["status"] == "pending_corrections"
    assert "does not exist" in s["error"]
    assert s["current_revision"] is None
    assert store.list_revisions(sid, db=hub.db) == []
    assert _exports(hub) == []
    (job,) = _queued(hub, sid)
    due = datetime.fromisoformat(job["not_before"])
    assert t0 + timedelta(minutes=4, seconds=50) <= due <= datetime.now(timezone.utc) + timedelta(minutes=5)
    # not due yet: nothing runs
    assert _run(hub) == 0
    # the file comes back; five minutes later the retry makes it final
    hub.corrections.write_text(json.dumps({"Agilent GC": {"IBP - D86": {"correction_value": -1}}}))
    later = lambda: datetime.now(timezone.utc) + timedelta(minutes=6)  # noqa: E731
    assert _run(hub, now_fn=later) == 1
    s = hub.sample(sid)
    assert (s["status"], s["error"], s["current_revision"]) == ("final", None, 1)
    assert json.loads(_rev(hub, sid)["corrections_used"])["values"]["IBP"] == -1.0


def test_other_instruments_have_no_file_corrections(hub):
    hub.gc1()
    hub.gc2(calibration_cdf=str(hub.cal), calibration_assignments=hub.gc1()["calibration_assignments"])
    sid = hub.submit(hub.cdf(), instrument="gc2").sample_id
    _run(hub)
    assert hub.sample(sid)["status"] == "pending_corrections"


def test_a_corrections_read_error_leaves_the_sample_and_retries(hub):
    hub.gc1()

    class Broken:
        def get(self, instrument):
            raise sqlite3.OperationalError("database is locked")

    sid = hub.submit(hub.cdf()).sample_id
    _run(hub, corrections_provider=Broken())
    s = hub.sample(sid)
    assert s["status"] == "received"      # never 'error'
    assert s["error"] is None
    (job,) = _queued(hub, sid)
    assert job["not_before"] is not None
    assert "database is locked" in job["last_error"]
    later = lambda: datetime.now(timezone.utc) + timedelta(minutes=10)  # noqa: E731
    _run(hub, now_fn=later)
    assert hub.sample(sid)["status"] == "final"


def test_corrections_provider_is_injectable(hub):
    hub.gc1()
    prov = FixedCorrections({c: 0.0 for c in corrections.D86_CUTS} | {"IBP": -2.0})
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub, corrections_provider=prov)
    used = json.loads(_rev(hub, sid)["corrections_used"])
    assert (used["source"], used["updated_by"], used["values"]["IBP"]) == ("hub", "ryan", -2.0)
    assert prov.calls == 1


def test_corrections_are_read_and_computed_outside_the_transaction(hub):
    hub.gc1()
    seen = []
    real_compute = distill.compute

    class Probe(FixedCorrections):
        def get(self, instrument):
            seen.append(("corrections", store._txn_depth()))
            return super().get(instrument)

    def compute(*a, **kw):
        seen.append(("compute", store._txn_depth()))
        return real_compute(*a, **kw)

    hub.submit(hub.cdf())
    with mock.patch.object(distill, "compute", compute):
        _run(hub, corrections_provider=Probe())
    assert seen == [("corrections", 0), ("compute", 0)]


# ── errors and atomicity ────────────────────────────────────────────────────

def test_an_exception_is_error_with_the_message(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    with mock.patch.object(distill, "compute", side_effect=RuntimeError("boom")):
        _run(hub)
    s = hub.sample(sid)
    assert s["status"] == "error"
    assert "boom" in s["error"]
    assert _queued(hub, sid) == []
    # reprocessing an error sample makes it final
    pipeline.request_reprocess(sid, by="ryan", db=hub.db)
    _run(hub)
    s = hub.sample(sid)
    assert (s["status"], s["error"], s["current_revision"]) == ("final", None, 1)
    assert _rev(hub, sid)["reason"] == "processed"


def test_the_final_write_is_one_transaction(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    real = store.export_rows.append_pending
    calls = []

    def failing(conn, *a, **kw):
        calls.append(store.get_revision(sid, db=conn))     # add_revision already ran
        raise RuntimeError("disk full")

    with mock.patch.object(store.export_rows, "append_pending", failing):
        _run(hub)
    assert calls and calls[0]["revision"] == 1
    s = hub.sample(sid)
    assert store.list_revisions(sid, db=hub.db) == []
    assert s["current_revision"] is None
    assert _exports(hub) == []
    assert s["status"] == "error"          # set after the rollback, by the error branch
    assert "disk full" in s["error"]
    assert store.export_rows.append_pending is real


def test_the_final_write_rolls_back_the_status_too(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    w = hub.worker()
    job = store.jobs.claim_next(db=hub.db)
    with mock.patch.object(store.samples, "set_status", side_effect=RuntimeError("late")):
        with pytest.raises(RuntimeError):
            w._write_final(hub.sample(sid), job, results_json="{}", line="x\r\n",
                           reason="processed", by=None, extra={})
    s = hub.sample(sid)
    assert (s["status"], s["current_revision"]) == ("received", None)
    assert store.list_revisions(sid, db=hub.db) == []
    assert _exports(hub) == []
    assert store.jobs.get(job["id"], db=hub.db)["state"] == "running"


# ── blank: latest genuine blank at or before, same method family ────────────

def test_blank_at_or_before_with_out_of_order_arrival(hub):
    hub.gc1()
    b1 = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 8, 0, 0))).sample_id
    # the sample arrives before a blank that was injected later
    s1 = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0))).sample_id
    b2 = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 12, 0, 0), name="Blank2")).sample_id
    # ...and a later sample arrives before an earlier-injected blank
    s2 = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 14, 0, 0), name="40305")).sample_id
    b0 = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 13, 0, 0), name="[b] blank")).sample_id
    at = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 13, 0, 0), name="40306")).sample_id
    _run(hub)
    assert _rev(hub, s1)["blank_used"] == b1
    assert _rev(hub, s2)["blank_used"] == b0      # 13:00 is the latest at or before 14:00
    assert _rev(hub, at)["blank_used"] == b0      # "at" counts
    assert b2


def test_a_blank_from_another_method_is_ignored(hub):
    hub.gc1()
    mm = {"SIMDISB.M": "D2887", "SIMDISTB.M": "D2887", "GASOLINE.M": "D7096"}
    store.instruments.upsert({"id": "gc1", "method_map": mm}, db=hub.db)
    good = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 8, 0, 0),
                              method_name="SIMDISTB.M")).sample_id
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 9, 0, 0), method_name="GASOLINE.M"))
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 9, 30, 0), method_name="UNMAPPED.M"))
    sid = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0))).sample_id
    _run(hub)
    assert _rev(hub, sid)["blank_used"] == good


def test_a_blank_from_another_instrument_is_ignored(hub):
    row = hub.gc1()
    hub.gc2(calibration_cdf=str(hub.cal), calibration_assignments=row["calibration_assignments"])
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 9, 0, 0)), instrument="gc2")
    sid = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0))).sample_id
    _run(hub)
    assert _rev(hub, sid)["blank_used"] is None


def test_a_fake_blank_is_never_used(hub):
    hub.gc1()
    genuine = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 8, 0, 0))).sample_id
    hub.submit(hub.cdf(name="Blank", injected=datetime(2026, 9, 25, 9, 0, 0)))   # carries signal
    sid = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0), shift=0.1)).sample_id
    _run(hub)
    assert _rev(hub, sid)["blank_used"] == genuine


def test_a_missing_blank_file_is_an_error_not_a_silent_no_blank(hub):
    hub.gc1()
    blank = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 8, 0, 0))).sample_id
    _run(hub)
    (hub.data / hub.sample(blank)["cdf_path"]).unlink()
    sid = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0))).sample_id
    _run(hub)
    s = hub.sample(sid)
    assert s["status"] == "error"
    assert "blank" in s["error"].lower()
    assert s["current_revision"] is None


# ── the export gate (D11) ───────────────────────────────────────────────────

def test_backfill_is_final_without_an_export_row_until_released(hub):
    hub.gc1(live_since=datetime(2026, 10, 1))
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub)
    s = hub.sample(sid)
    assert (s["status"], s["backfill"], s["current_revision"]) == ("final", 1, 1)
    assert _exports(hub) == []
    assert not store.samples.is_gated(sid, db=hub.db)
    # reprocessing an unreleased backfill sample: a revision, still no export row
    pipeline.request_reprocess(sid, by="ryan", db=hub.db)
    _run(hub)
    assert hub.sample(sid)["current_revision"] == 2
    assert _exports(hub) == []
    # released: the next final write exports
    store.samples.update(sid, released_at=store.now_iso(), released_by="ryan", db=hub.db)
    pipeline.request_reprocess(sid, by="ryan", db=hub.db)
    _run(hub)
    rows = _exports(hub)
    assert [(r["sample_id"], r["revision"]) for r in rows] == [(sid, 3)]


# ── reprocess: revisions, D5 ────────────────────────────────────────────────

def test_reprocess_adds_a_revision_and_keeps_blank_and_corrections(hub):
    hub.gc1()
    b1 = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 8, 0, 0))).sample_id
    sid = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0))).sample_id
    _run(hub)
    first = _rev(hub, sid)
    # a newer blank (still at or before) and changed corrections
    b2 = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 9, 0, 0), name="Blank2")).sample_id
    hub.corrections.write_text(json.dumps({"Agilent GC": {"IBP - D86": {"correction_value": -1}}}))
    _run(hub)
    pipeline.request_reprocess(sid, by="ryan", db=hub.db)
    _run(hub)
    second = _rev(hub, sid)
    assert (second["revision"], second["reason"], second["by"]) == (2, "reprocess", "ryan")
    assert second["blank_used"] == b1
    assert second["corrections_used"] == first["corrections_used"]
    assert second["results"] == first["results"]
    assert hub.sample(sid)["current_revision"] == 2
    assert [r["revision"] for r in _exports(hub) if r["sample_id"] == sid] == [1, 2]
    # asking for current ones
    pipeline.request_reprocess(sid, by="ryan", use_current_blank=True,
                               use_current_corrections=True, db=hub.db)
    _run(hub)
    third = _rev(hub, sid)
    assert third["revision"] == 3
    assert third["blank_used"] == b2
    assert json.loads(third["corrections_used"])["values"]["IBP"] == -1.0
    assert [r["revision"] for r in store.list_revisions(sid, db=hub.db)] == [1, 2, 3]


def test_reprocess_last_intent_wins(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub)
    pipeline.request_reprocess(sid, by="a", db=hub.db)
    pipeline.request_reprocess(sid, by="b", use_current_blank=True, db=hub.db)
    assert len(_queued(hub, sid)) == 1
    _run(hub)
    assert _rev(hub, sid)["by"] == "b"
    assert hub.sample(sid)["current_revision"] == 2


def test_reprocess_of_a_final_sample_without_corrections_keeps_it_final(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub)
    hub.corrections.unlink()
    pipeline.request_reprocess(sid, by="ryan", use_current_corrections=True, db=hub.db)
    _run(hub)
    s = hub.sample(sid)
    assert (s["status"], s["current_revision"]) == ("final", 1)
    assert s["error"].startswith("last reprocess failed: ") and "does not exist" in s["error"]
    failed = store.jobs.list(state="failed", kind="process", db=hub.db)
    assert len(failed) == 1 and "does not exist" in failed[0]["last_error"]
    assert _queued(hub, sid) == []
    # the next successful reprocess clears it
    hub.corrections.write_text(json.dumps({"Agilent GC": {"IBP - D86": {"correction_value": -1}}}))
    pipeline.request_reprocess(sid, by="ryan", db=hub.db)
    _run(hub)
    s = hub.sample(sid)
    assert (s["status"], s["current_revision"], s["error"]) == ("final", 2, None)


def test_reprocess_of_a_final_sample_that_errors_keeps_it_final(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub)
    pipeline.request_reprocess(sid, by="ryan", db=hub.db)
    with mock.patch.object(distill, "compute", side_effect=RuntimeError("boom")):
        _run(hub)
    s = hub.sample(sid)
    assert (s["status"], s["current_revision"]) == ("final", 1)
    assert "boom" in store.jobs.list(state="failed", db=hub.db)[0]["last_error"]


def test_a_stray_process_job_for_a_final_sample_does_nothing(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub)
    store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid, db=hub.db)
    _run(hub)
    assert hub.sample(sid)["current_revision"] == 1


# ── restart and the thread ──────────────────────────────────────────────────

def test_requeue_on_start(hub):
    hub.gc1()
    a = hub.submit(hub.cdf()).sample_id
    b = hub.submit(hub.cdf(name="40305", shift=0.1)).sample_id
    c = hub.submit(hub.cdf(name="40306", shift=0.2)).sample_id
    # a: its job was running when the hub stopped
    job_a = store.jobs.claim_next(db=hub.db)
    assert job_a["sample_id"] == a
    # b: received but its job was lost; c: pending_corrections with a retry far away
    with store.connection(hub.db) as conn:
        conn.execute("DELETE FROM jobs WHERE sample_id=?", (b,))
        conn.execute("UPDATE jobs SET not_before=? WHERE sample_id=?",
                     ((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), c))
    store.samples.set_status(c, "pending_corrections", db=hub.db)
    counts = pipeline.requeue_on_start(db=hub.db)
    assert counts["stale_running"] == 1
    assert {j["sample_id"] for j in store.jobs.list(state="queued", db=hub.db)} == {a, b, c}
    assert all(j["not_before"] is None for j in store.jobs.list(state="queued", db=hub.db))
    assert _run(hub) == 3
    assert {hub.sample(x)["status"] for x in (a, b, c)} == {"final"}


def test_worker_thread_processes_and_stops(hub):
    hub.gc1()
    w = hub.worker(poll_seconds=0.05)
    w.start()
    try:
        sid = hub.submit(hub.cdf()).sample_id
        w.wake()
        deadline = time.monotonic() + 30
        while hub.sample(sid)["status"] != "final" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hub.sample(sid)["status"] == "final"
    finally:
        w.stop(timeout=10)
    assert not w.is_alive()


def test_worker_start_requeues(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    store.jobs.claim_next(db=hub.db)            # left running by a crash
    w = hub.worker(poll_seconds=0.05)
    w.start()
    try:
        deadline = time.monotonic() + 30
        while hub.sample(sid)["status"] != "final" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hub.sample(sid)["status"] == "final"
    finally:
        w.stop(timeout=10)


def test_worker_only_claims_process_jobs(hub):
    hub.gc1()
    other = store.jobs.enqueue("export-flush", {"instrument": "gc1"}, db=hub.db)
    assert _run(hub) == 0
    assert store.jobs.get(other, db=hub.db)["state"] == "queued"
