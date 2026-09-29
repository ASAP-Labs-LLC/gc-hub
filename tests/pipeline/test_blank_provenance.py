"""Blank provenance (2D): a revision records the blank *file* it subtracted
(``blank_cdf_sha256``/``blank_cdf_path``), a reprocess that keeps the
recorded blank (D5) subtracts that file even after the blank sample's CDF was
replaced, and a Replace that changes a blank flags the final results it
affects. Also: a plain job never supersedes a conflict Replace job."""
from __future__ import annotations

from pipeline_helpers import SIMDIS, hub  # noqa: F401  (hub: the fixture)

import json
from datetime import datetime
from unittest import mock

import cdf_fixtures as fx
import distill
import pipeline
import store

B_AT = datetime(2026, 9, 25, 8, 0, 0)
S_AT = datetime(2026, 9, 25, 10, 0, 0)


def _run(hub, **kw) -> int:
    return hub.worker(**kw).run_until_idle()


def _rev(hub, sid, revision=None):
    return store.get_revision(sid, revision, db=hub.db)


def _other_blank(hub, name="Blank", injected=B_AT):
    """A genuine blank with a different baseline from ``fx.blank_cdf``: the
    subtraction changes the result."""
    hub._n += 1
    t = fx._axis()
    y = fx.gaussian(t, 0.30, 50000, 0.01) + 40 + 5 * t + fx.gaussian(t, 3.5, 60, 1.2)
    return fx.write_cdf(hub.src / f"ob{hub._n}.CDF", t, y, name, injected, method_name=SIMDIS)


def _replace(hub, cid):
    return pipeline.resolve_conflict_replace(cid, by="ryan", db=hub.db, data_dir=hub.data)


def _blank_and_sample(hub):
    hub.gc1()
    blank = hub.submit(hub.cdf("blank", injected=B_AT)).sample_id
    sid = hub.submit(hub.cdf(injected=S_AT)).sample_id
    _run(hub)
    assert _rev(hub, sid)["blank_used"] == blank
    return blank, sid


def _replace_blank(hub, blank, notifier=None):
    res = hub.submit(_other_blank(hub))
    assert (res.outcome, res.sample_id) == ("conflict", blank)
    _replace(hub, res.conflict_id)
    _run(hub, notifier=notifier)
    assert store.conflicts.get(res.conflict_id, db=hub.db)["resolved"] == "replaced"
    return res


# ── the revision records the blank file ─────────────────────────────────────

def test_a_revision_records_the_blank_file_it_subtracted(hub):
    blank, sid = _blank_and_sample(hub)
    b = hub.sample(blank)
    r = _rev(hub, sid)
    assert (r["blank_cdf_sha256"], r["blank_cdf_path"]) == (b["cdf_sha256"], b["cdf_path"])
    assert pipeline.revision_blank_path(sid, db=hub.db, data_dir=hub.data) == hub.data / b["cdf_path"]
    assert pipeline.revision_blank_path(sid, 1, db=hub.db, data_dir=hub.data) == hub.data / b["cdf_path"]
    # a blank sample itself subtracts nothing
    assert _rev(hub, blank)["blank_cdf_path"] is None
    assert pipeline.revision_blank_path(blank, db=hub.db, data_dir=hub.data) is None
    assert pipeline.revision_blank_path(sid, 99, db=hub.db, data_dir=hub.data) is None


def test_export_to_lims_copies_the_recorded_blank_file(hub):
    blank, sid = _blank_and_sample(hub)
    first = _rev(hub, sid)
    _replace_blank(hub, blank)
    pipeline.export_to_lims(sid, by="ryan", db=hub.db, data_dir=hub.data)
    r = _rev(hub, sid)
    assert r["reason"] == "export-lims"
    assert (r["blank_cdf_sha256"], r["blank_cdf_path"]) == (first["blank_cdf_sha256"],
                                                            first["blank_cdf_path"])


# ── a kept blank is the recorded file (the critic's numbers) ───────────────

def test_a_reprocess_keeping_the_blank_subtracts_the_recorded_file(hub):
    blank, sid = _blank_and_sample(hub)
    first = _rev(hub, sid)
    old_blank = hub.sample(blank)
    _replace_blank(hub, blank)
    assert hub.sample(blank)["cdf_path"] != old_blank["cdf_path"]
    pipeline.request_reprocess(sid, by="ryan", db=hub.db)
    _run(hub)
    again = _rev(hub, sid)
    assert (again["revision"], again["reason"]) == (first["revision"] + 1, "reprocess")
    assert json.loads(again["results"]) == json.loads(first["results"])
    assert again["d86_uncorrected"] == first["d86_uncorrected"]
    assert again["blank_used"] == blank
    assert (again["blank_cdf_sha256"], again["blank_cdf_path"]) == (old_blank["cdf_sha256"],
                                                                    old_blank["cdf_path"])
    assert pipeline.revision_blank_path(sid, db=hub.db, data_dir=hub.data) == \
        hub.data / old_blank["cdf_path"]


def test_the_new_blank_file_really_changes_the_numbers(hub):
    """Guards the test above: with the current blank the numbers move."""
    blank, sid = _blank_and_sample(hub)
    first = _rev(hub, sid)
    _replace_blank(hub, blank)
    pipeline.request_reprocess(sid, by="ryan", use_current_blank=True, db=hub.db)
    _run(hub)
    now = _rev(hub, sid)
    assert now["blank_used"] == blank
    assert now["blank_cdf_sha256"] == hub.sample(blank)["cdf_sha256"]
    assert json.loads(now["results"])["2887 IBP"] != json.loads(first["results"])["2887 IBP"]
    assert now["d86_uncorrected"] != first["d86_uncorrected"]


def test_a_missing_recorded_blank_file_is_an_error_not_the_current_file(hub):
    blank, sid = _blank_and_sample(hub)
    old = hub.sample(blank)
    _replace_blank(hub, blank)
    (hub.data / old["cdf_path"]).unlink()
    pipeline.request_reprocess(sid, by="ryan", db=hub.db)
    _run(hub)
    s = hub.sample(sid)
    assert s["status"] == "final" and s["current_revision"] == 1
    assert "blank" in s["error"].lower()


def test_a_blank_file_swapped_during_the_compute_is_stale(hub):
    hub.gc1()
    blank = hub.submit(hub.cdf("blank", injected=B_AT)).sample_id
    sid = hub.submit(hub.cdf(injected=S_AT)).sample_id
    hub.worker().run_once()                                  # the blank
    real = distill.compute

    def compute(*a, **kw):
        out = real(*a, **kw)
        store.samples.update(blank, cdf_sha256="swapped", db=hub.db)
        return out

    with mock.patch.object(distill, "compute", compute):
        hub.worker().run_once()
    assert hub.sample(sid)["current_revision"] is None
    assert [j["sample_id"] for j in store.jobs.list(state="queued", db=hub.db)] == [sid]


# ── a Replace that changes a blank flags what it affects ───────────────────

def test_replacing_a_used_blank_flags_its_users_and_notifies_once(hub):
    blank, sid = _blank_and_sample(hub)
    other = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 11, 0, 0), name="40305")).sample_id
    _run(hub)
    assert _rev(hub, other)["blank_used"] == blank
    heard = []
    _replace_blank(hub, blank, notifier=lambda level, msg: heard.append((level, msg)))
    for s in (sid, other):
        note = hub.sample(s)["review_note"]
        assert note and f"blank sample {blank}" in note
        assert hub.sample(s)["current_revision"] == 1                   # not reprocessed
    assert len(heard) == 1 and heard[0][0] == "warning"
    assert "2" in heard[0][1]
    # the notification names the affected lab IDs
    for s in (sid, other):
        assert hub.sample(s)["lab_id"] in heard[0][1], heard


def test_a_replace_that_makes_a_sample_a_genuine_blank_flags_late_blank(hub):
    hub.gc1()
    fake = hub.submit(hub.cdf(name="Blank", injected=B_AT)).sample_id   # carries signal
    sid = hub.submit(hub.cdf(injected=S_AT)).sample_id
    _run(hub)
    assert hub.sample(fake)["is_blank"] == 0 and _rev(hub, sid)["blank_used"] is None
    heard = []
    res = hub.submit(hub.cdf("blank", injected=B_AT))
    _replace(hub, res.conflict_id)
    _run(hub, notifier=lambda level, msg: heard.append((level, msg)))
    assert hub.sample(fake)["is_blank"] == 1
    assert hub.sample(sid)["review_note"]
    assert len(heard) == 1


def test_a_replace_that_unmakes_a_genuine_blank_flags_its_users(hub):
    blank, sid = _blank_and_sample(hub)
    res = hub.submit(hub.cdf(name="Blank", injected=B_AT))      # a 'Blank' with signal
    assert res.outcome == "conflict"
    heard = []
    _replace(hub, res.conflict_id)
    _run(hub, notifier=lambda level, msg: heard.append((level, msg)))
    assert hub.sample(blank)["is_blank"] == 0
    assert f"blank sample {blank}" in hub.sample(sid)["review_note"]
    assert len(heard) == 1


def test_replacing_a_plain_sample_flags_nothing(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf(injected=S_AT)).sample_id
    other = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 11, 0, 0), name="40305")).sample_id
    _run(hub)
    heard = []
    res = hub.submit(hub.cdf(injected=S_AT, shift=0.3))
    _replace(hub, res.conflict_id)
    _run(hub, notifier=lambda level, msg: heard.append((level, msg)))
    assert hub.sample(other)["review_note"] is None
    assert heard == []


# ── a plain job never supersedes a Replace ─────────────────────────────────

def test_a_running_replace_survives_a_restart_with_a_plain_job_queued(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf(injected=S_AT)).sample_id
    _run(hub)
    res = hub.submit(hub.cdf(injected=S_AT, shift=0.3))
    _replace(hub, res.conflict_id)
    running = store.jobs.claim_next(kind=pipeline.PROCESS, db=hub.db)   # the worker took it...
    assert running["payload"]["reason"] == "replace"
    store.jobs.enqueue(pipeline.PROCESS, {"sample_id": sid}, sample_id=sid, db=hub.db)  # a plain twin
    # ...and the hub restarted before it finished
    pipeline.requeue_on_start(db=hub.db)
    _run(hub)
    c = store.conflicts.get(res.conflict_id, db=hub.db)
    assert c["resolved"] == "replaced"
    assert hub.sample(sid)["cdf_sha256"] == res.sha256
