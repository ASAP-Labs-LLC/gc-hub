"""Conflict Replace (2D): ``resolve_conflict_replace`` only queues a job; the
Worker computes from the held file and, in one transaction, swaps the
sample's CDF, adds the ``replace`` revision (and export row), and marks the
conflict ``replaced``. A failure writes nothing to the sample and records the
error on the conflict."""
from __future__ import annotations

from pipeline_helpers import hub  # noqa: F401  (hub: the fixture)

import json
from datetime import datetime
from unittest import mock

import pytest

import distill
import pipeline
import store


def _run(hub, **kw) -> int:
    return hub.worker(**kw).run_until_idle()


def _rows(hub, instrument="gc1"):
    return store.export_rows.rows_after(instrument, 0, db=hub.db)


def _final(hub, **kw):
    sid = hub.submit(hub.cdf(**kw)).sample_id
    _run(hub)
    assert hub.sample(sid)["status"] == "final"
    return sid


def _replace(hub, cid, by="ryan"):
    return pipeline.resolve_conflict_replace(cid, by=by, conf=hub.conf, db=hub.db,
                                             data_dir=hub.data)


def _conflict(hub, sid, **kw):
    s = hub.sample(sid)
    kw.setdefault("name", s["lab_id"])
    kw.setdefault("injected", datetime.fromisoformat(s["injection_dt"]))
    kw.setdefault("shift", 0.3)
    res = hub.submit(hub.cdf(**kw))
    assert res.outcome == "conflict" and res.sample_id == sid
    return res


def _job(hub, job_id):
    return store.jobs.get(job_id, db=hub.db)


# ── the request only queues ─────────────────────────────────────────────────

def test_replace_only_enqueues(hub):
    hub.gc1()
    sid = _final(hub)
    before = hub.sample(sid)
    res = _conflict(hub, sid)
    job_id = _replace(hub, res.conflict_id)
    job = _job(hub, job_id)
    assert job["state"] == "queued" and job["sample_id"] == sid
    assert job["payload"]["reason"] == "replace"
    assert job["payload"]["conflict_id"] == res.conflict_id
    assert job["payload"]["by"] == "ryan"
    assert store.conflicts.get(res.conflict_id, db=hub.db)["resolved"] is None
    assert hub.sample(sid) == before


def test_replace_refuses_a_missing_or_resolved_conflict(hub):
    hub.gc1()
    sid = _final(hub)
    res = _conflict(hub, sid)
    with pytest.raises(ValueError):
        _replace(hub, res.conflict_id + 99)
    store.conflicts.resolve(res.conflict_id, "kept-existing", by="ryan", db=hub.db)
    with pytest.raises(ValueError):
        _replace(hub, res.conflict_id)


def test_asking_twice_returns_the_same_job(hub):
    hub.gc1()
    sid = _final(hub)
    res = _conflict(hub, sid)
    first = _replace(hub, res.conflict_id)
    assert _replace(hub, res.conflict_id, by="ann") == first
    assert _job(hub, first)["payload"]["by"] == "ryan"          # the first request stands


def test_a_second_conflict_cannot_queue_over_a_queued_replace(hub):
    hub.gc1()
    sid = _final(hub)
    first = _conflict(hub, sid, shift=0.3)
    second = _conflict(hub, sid, shift=0.5)
    _replace(hub, first.conflict_id)
    with pytest.raises(ValueError):
        _replace(hub, second.conflict_id)


def test_a_reprocess_request_does_not_overwrite_a_queued_replace(hub):
    hub.gc1()
    sid = _final(hub)
    res = _conflict(hub, sid)
    job_id = _replace(hub, res.conflict_id)
    assert pipeline.request_reprocess(sid, by="ann", db=hub.db) == job_id
    assert _job(hub, job_id)["payload"]["reason"] == "replace"
    _run(hub)
    assert store.conflicts.get(res.conflict_id, db=hub.db)["resolved"] == "replaced"


# ── success: everything in one transaction ─────────────────────────────────

def test_a_successful_replace_swaps_revises_exports_and_resolves(hub):
    hub.gc1()
    sid = _final(hub)
    old = hub.sample(sid)
    res = _conflict(hub, sid)
    held = store.conflicts.get(res.conflict_id, db=hub.db)
    _replace(hub, res.conflict_id)
    _run(hub)
    s = hub.sample(sid)
    assert (s["cdf_sha256"], s["cdf_path"]) == (res.sha256, held["cdf_path"])
    assert s["status"] == "final" and s["current_revision"] == 2
    r1, r2 = store.list_revisions(sid, db=hub.db)
    assert (r1["cdf_sha256"], r1["cdf_path"]) == (old["cdf_sha256"], old["cdf_path"])
    assert (r2["cdf_sha256"], r2["cdf_path"]) == (res.sha256, held["cdf_path"])
    assert (r2["reason"], r2["by"]) == ("replace", "ryan")
    assert json.loads(r2["results"])["Source File"] == held["cdf_path"]
    assert [r["revision"] for r in _rows(hub)] == [1, 2]
    c = store.conflicts.get(res.conflict_id, db=hub.db)
    assert (c["resolved"], c["resolved_by"], c["error"]) == ("replaced", "ryan", None)
    assert c["resolved_at"]


def test_the_replace_write_is_one_transaction(hub):
    hub.gc1()
    sid = _final(hub)
    before = hub.sample(sid)
    res = _conflict(hub, sid)
    _replace(hub, res.conflict_id)
    with mock.patch.object(store.conflicts, "resolve", side_effect=RuntimeError("late")):
        hub.worker().run_once()
    assert hub.sample(sid)["cdf_sha256"] == before["cdf_sha256"]
    assert hub.sample(sid)["current_revision"] == 1
    assert hub.sample(sid)["is_blank"] == before["is_blank"]
    assert len(store.list_revisions(sid, db=hub.db)) == 1
    assert [r["revision"] for r in _rows(hub)] == [1]
    assert store.conflicts.get(res.conflict_id, db=hub.db)["resolved"] is None


def test_replace_uses_the_current_blank_and_corrections(hub):
    hub.gc1()
    sid = _final(hub, injected=datetime(2026, 9, 25, 10, 0, 0))
    assert store.get_revision(sid, db=hub.db)["blank_used"] is None
    blank = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 8, 0, 0))).sample_id
    _run(hub)
    res = _conflict(hub, sid)
    _replace(hub, res.conflict_id)
    _run(hub)
    assert store.get_revision(sid, db=hub.db)["blank_used"] == blank


def test_a_backfill_replace_writes_no_export_row(hub):
    hub.gc1(live_since=datetime(2027, 1, 1))
    sid = _final(hub)
    res = _conflict(hub, sid)
    _replace(hub, res.conflict_id)
    _run(hub)
    assert store.get_revision(sid, db=hub.db)["reason"] == "replace"
    assert _rows(hub) == []


# ── failure: nothing written to the sample ─────────────────────────────────

def _assert_untouched(hub, sid, before, cid):
    assert hub.sample(sid) == before
    assert len(store.list_revisions(sid, db=hub.db)) == (before["current_revision"] or 0)
    c = store.conflicts.get(cid, db=hub.db)
    assert c["resolved"] is None and c["resolved_by"] is None and c["resolved_at"] is None
    return c


def test_a_failing_replace_leaves_the_sample_and_conflict(hub):
    hub.gc1()
    sid = _final(hub)
    before = hub.sample(sid)
    res = _conflict(hub, sid)
    job_id = _replace(hub, res.conflict_id)
    with mock.patch.object(distill, "compute", side_effect=RuntimeError("boom")):
        _run(hub)
    c = _assert_untouched(hub, sid, before, res.conflict_id)
    assert "boom" in c["error"]
    assert _job(hub, job_id)["state"] == "failed"
    assert [r["revision"] for r in _rows(hub)] == [1]
    assert _run(hub) == 0                                  # not retried on its own


def test_a_hold_during_replace_is_a_conflict_error(hub):
    hub.gc1()
    sid = _final(hub)
    before = hub.sample(sid)
    res = _conflict(hub, sid, method_name="UNMAPPED.M")
    _replace(hub, res.conflict_id)
    _run(hub)
    c = _assert_untouched(hub, sid, before, res.conflict_id)
    assert "other_method" in c["error"]


def test_a_missing_held_file_is_a_conflict_error(hub):
    hub.gc1()
    sid = _final(hub)
    before = hub.sample(sid)
    res = _conflict(hub, sid)
    (hub.data / store.conflicts.get(res.conflict_id, db=hub.db)["cdf_path"]).unlink()
    _replace(hub, res.conflict_id)
    _run(hub)
    c = _assert_untouched(hub, sid, before, res.conflict_id)
    assert c["error"]


def test_after_a_failure_the_admin_can_retry_or_keep_existing(hub):
    hub.gc1()
    sid = _final(hub)
    res = _conflict(hub, sid)
    _replace(hub, res.conflict_id)
    with mock.patch.object(distill, "compute", side_effect=RuntimeError("boom")):
        _run(hub)
    _replace(hub, res.conflict_id)                          # retry
    _run(hub)
    c = store.conflicts.get(res.conflict_id, db=hub.db)
    assert (c["resolved"], c["error"]) == ("replaced", None)
    assert hub.sample(sid)["cdf_sha256"] == res.sha256

    other = _conflict(hub, sid, shift=0.6)
    _replace(hub, other.conflict_id)
    with mock.patch.object(distill, "compute", side_effect=RuntimeError("boom")):
        _run(hub)
    assert store.conflicts.get(other.conflict_id, db=hub.db)["error"]
    store.conflicts.resolve(other.conflict_id, "kept-existing", by="ryan", db=hub.db)
    assert hub.sample(sid)["cdf_sha256"] == res.sha256


def test_a_failing_replace_does_not_change_is_blank(hub):
    """A 'Blank'-named sample with signal (not genuine) conflicts with a genuine
    blank file: until the replace succeeds, no other sample may use it."""
    hub.gc1()
    fake = _final(hub, name="Blank", injected=datetime(2026, 9, 25, 8, 0, 0))
    assert hub.sample(fake)["is_blank"] == 0
    res = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 8, 0, 0)))
    assert (res.outcome, res.sample_id) == ("conflict", fake)
    _replace(hub, res.conflict_id)
    with mock.patch.object(distill, "compute", side_effect=RuntimeError("boom")):
        _run(hub)
    assert hub.sample(fake)["is_blank"] == 0
    later = _final(hub, injected=datetime(2026, 9, 25, 10, 0, 0))
    assert store.get_revision(later, db=hub.db)["blank_used"] is None

    _replace(hub, res.conflict_id)
    _run(hub)
    assert hub.sample(fake)["is_blank"] == 1
    assert store.get_revision(fake, db=hub.db)["blank_used"] is None   # a blank is never subtracted
    after = _final(hub, injected=datetime(2026, 9, 25, 11, 0, 0), name="40305")
    assert store.get_revision(after, db=hub.db)["blank_used"] == fake


def test_a_transient_failure_retries_and_notes_the_conflict(hub):
    hub.gc1()
    sid = _final(hub)
    before = hub.sample(sid)
    res = _conflict(hub, sid)
    job_id = _replace(hub, res.conflict_id)

    class Broken:
        def get(self, inst):
            raise OSError("share offline")

    _run(hub, corrections_provider=Broken())
    c = _assert_untouched(hub, sid, before, res.conflict_id)
    assert "share offline" in c["error"]
    job = _job(hub, job_id)
    assert job["state"] == "queued" and job["not_before"]    # retried later


# ── stale detection ─────────────────────────────────────────────────────────

def _during_compute(action):
    real = distill.compute

    def compute(*a, **kw):
        out = real(*a, **kw)
        action()
        return out
    return mock.patch.object(distill, "compute", compute)


def test_a_conflict_resolved_during_the_compute_is_stale(hub):
    hub.gc1()
    sid = _final(hub)
    before = hub.sample(sid)
    res = _conflict(hub, sid)
    job_id = _replace(hub, res.conflict_id)
    with _during_compute(lambda: store.conflicts.resolve(res.conflict_id, "kept-existing",
                                                         by="ann", db=hub.db)):
        hub.worker().run_once()
    assert hub.sample(sid) == before
    assert _job(hub, job_id)["state"] == "queued"            # requeued...
    _run(hub)
    assert _job(hub, job_id)["state"] == "done"              # ...then nothing to do
    assert hub.sample(sid) == before
    c = store.conflicts.get(res.conflict_id, db=hub.db)
    assert (c["resolved"], c["resolved_by"]) == ("kept-existing", "ann")


def test_a_sample_file_changed_during_the_compute_is_stale(hub):
    hub.gc1()
    sid = _final(hub)
    res = _conflict(hub, sid)
    job_id = _replace(hub, res.conflict_id)
    with _during_compute(lambda: store.samples.update(sid, cdf_sha256="elsewhere", db=hub.db)):
        hub.worker().run_once()
    assert hub.sample(sid)["cdf_sha256"] == "elsewhere"
    assert hub.sample(sid)["current_revision"] == 1
    assert store.conflicts.get(res.conflict_id, db=hub.db)["resolved"] is None
    assert _job(hub, job_id)["state"] == "queued"


# ── revisions record their CDF ─────────────────────────────────────────────

def test_every_revision_records_the_cdf_that_produced_it(hub):
    hub.gc1()
    sid = _final(hub)
    s = hub.sample(sid)
    pipeline.request_reprocess(sid, by="ryan", db=hub.db)
    _run(hub)
    pipeline.export_to_lims(sid, by="ryan", db=hub.db, data_dir=hub.data)
    revs = store.list_revisions(sid, db=hub.db)
    assert [r["reason"] for r in revs] == ["processed", "reprocess", "export-lims"]
    assert {(r["cdf_sha256"], r["cdf_path"]) for r in revs} == {(s["cdf_sha256"], s["cdf_path"])}


# ── re-sending files after a replace ────────────────────────────────────────

def test_resending_the_original_after_a_replace_is_a_duplicate(hub):
    """The original file produced revision 1 of the sample, so the hub knows it:
    a re-send answers ``duplicate`` for that sample and creates nothing (not a
    new conflict)."""
    hub.gc1()
    original = hub.cdf()
    sid = hub.submit(original).sample_id
    _run(hub)
    res = _conflict(hub, sid)
    _replace(hub, res.conflict_id)
    _run(hub)
    again = hub.submit(original)
    assert (again.outcome, again.sample_id) == ("duplicate", sid)
    assert "replaced" in again.message
    assert len(store.conflicts.list(unresolved_only=False, db=hub.db)) == 1
    assert store.samples.count(db=hub.db) == 1
    assert _run(hub) == 0
    # the replacement is the sample's file now: a plain duplicate
    assert hub.submit(hub.data / hub.sample(sid)["cdf_path"]).outcome == "duplicate"


def test_resending_the_original_to_another_instrument_is_cross_instrument(hub):
    row = hub.gc1()
    hub.gc2(calibration_cdf=str(hub.cal), calibration_assignments=row["calibration_assignments"])
    original = hub.cdf()
    sid = hub.submit(original).sample_id
    _run(hub)
    res = _conflict(hub, sid)
    _replace(hub, res.conflict_id)
    _run(hub)
    again = hub.submit(original, instrument="gc2")
    assert (again.outcome, again.instrument_id, again.sample_id) == ("cross_instrument", "gc1", sid)
