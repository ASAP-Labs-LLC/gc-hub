"""Fixes from the critic review of 2A1 T2: honest blank_used, v1 name and
time fallbacks, resolved-conflict re-sends, late blanks, blank-named
samples, corrections error classes, and the worker/startup lifecycle."""
from __future__ import annotations

from pipeline_helpers import SIMDIS, file_corrections, hub  # noqa: F401  (hub: the fixture)

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

import cdf_fixtures as fx
import corrections
import distill
import instruments
import pipeline
import store


def _run(hub, **kw) -> int:
    return hub.worker(**kw).run_until_idle()


def _rev(hub, sid, revision=None):
    return store.get_revision(sid, revision, db=hub.db)


class Notes:
    """An injected notifier: ``notifier(level, message)``."""

    def __init__(self):
        self.calls = []

    def __call__(self, level, message):
        self.calls.append((level, message))


# ── 1: blank_used only when a blank was subtracted ──────────────────────────

def test_a_rejected_blank_is_recomputed_without_a_blank_and_noted(hub, caplog):
    hub.gc1()
    earlier = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 7, 0, 0))).sample_id
    blank = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 8, 0, 0),
                               name="Blank2")).sample_id
    _run(hub)
    sid = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0))).sample_id
    real = distill.compute
    calls = []

    def compute(cdf, conf, blank_path=None, **kw):
        calls.append((blank_path, kw.get("strict_blank")))
        if blank_path is not None:
            raise distill.BlankRejected("blank peak height is 90% of the sample's - not a blank")
        return real(cdf, conf, blank_path, **kw)

    with mock.patch.object(distill, "compute", compute), caplog.at_level("WARNING", "pipeline"):
        _run(hub)
    s = hub.sample(sid)
    assert s["status"] == "final"
    rev = _rev(hub, sid)
    assert rev["blank_used"] is None                     # never an earlier blank either
    assert json.loads(rev["notes"]) == {"blank_rejected": {
        "sample_id": blank, "reason": "blank peak height is 90% of the sample's - not a blank"}}
    assert [c[0] is None for c in calls] == [False, True]
    assert all(strict for _, strict in calls)
    assert str(hub.data / hub.sample(blank)["cdf_path"]) == str(calls[0][0])
    assert "not a blank" in caplog.text
    assert earlier


def test_an_unreadable_blank_is_an_error(hub):
    hub.gc1()
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 8, 0, 0)))
    _run(hub)
    sid = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0))).sample_id
    with mock.patch.object(distill, "compute", side_effect=distill.BlankUnreadable("truncated")):
        _run(hub)
    s = hub.sample(sid)
    assert s["status"] == "error" and "truncated" in s["error"]
    assert s["current_revision"] is None


def test_blank_used_needs_blank_applied(hub):
    hub.gc1()
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 8, 0, 0)))
    _run(hub)
    sid = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0))).sample_id
    real = distill.compute

    def compute(*a, **kw):
        out = real(*a, **kw)
        out["blank_applied"] = False
        return out

    with mock.patch.object(distill, "compute", compute):
        _run(hub)
    assert _rev(hub, sid)["blank_used"] is None


def test_a_good_blank_has_no_notes(hub):
    hub.gc1()
    b = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 8, 0, 0))).sample_id
    sid = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0))).sample_id
    _run(hub)
    rev = _rev(hub, sid)
    assert (rev["blank_used"], rev["notes"]) == (b, None)


# ── 2: an empty sample name falls back to the sender's file name (v1) ───────

@pytest.mark.parametrize("name", ["", "   "])
def test_an_empty_sample_name_uses_the_senders_file_stem(hub, name):
    hub.gc1()
    src = fx.sample_cdf(hub.src / "40777.CDF", name=name, method_name=SIMDIS)
    sid = hub.submit(src).sample_id
    s = hub.sample(sid)
    assert s["lab_id"] == "40777"
    assert Path(s["cdf_path"]).name == f"40777_{sid}.CDF"
    _run(hub)
    assert json.loads(_rev(hub, sid)["results"])["Lab ID"] == "40777"
    assert store.export_rows.rows_after("gc1", 0, db=hub.db)[0]["line"].startswith("40777,")


def test_bytes_without_a_name_use_the_source_name(hub):
    hub.gc1()
    src = fx.sample_cdf(hub.src / "whatever.CDF", name="", method_name=SIMDIS)
    sid = hub.submit(src.read_bytes(), mtime=datetime(2026, 9, 25), source_name="40778.cdf").sample_id
    assert hub.sample(sid)["lab_id"] == "40778"


# ── 3: stamp-less CDFs keep the sender's time, in whole seconds ─────────────

def _stampless(path):
    from netCDF4 import Dataset
    fx.sample_cdf(path, name="40555", method_name=SIMDIS)
    with Dataset(path, "a") as ds:
        ds.delncattr("injection_date_time_stamp")
    return path


def test_a_stampless_cdf_is_stored_with_the_senders_mtime(hub):
    hub.gc1()
    body = _stampless(hub.src / "ns.CDF").read_bytes()
    sid = hub.submit(body, mtime="2026-09-01T08:00:05.700000", source_name="ns.CDF").sample_id
    s = hub.sample(sid)
    assert (s["injection_dt"], s["injection_dt_source"]) == ("2026-09-01 08:00:05", "mtime")
    assert s["legacy_injection_dt"] == "2026-09-01 08:00:05.700000"   # v1's exact string
    stored = hub.data / s["cdf_path"]
    assert datetime.fromtimestamp(stored.stat().st_mtime) == datetime(2026, 9, 1, 8, 0, 5, 700000)
    _run(hub)
    results = json.loads(_rev(hub, sid)["results"])
    assert results["InjectionDateTime"] == "2026-09-01 08:00:05"


def test_a_paths_mtime_is_truncated_to_seconds(hub):
    hub.gc1()
    p = _stampless(hub.src / "ns2.CDF")
    os.utime(p, (1_790_000_000.75, 1_790_000_000.75))
    s = hub.sample(hub.submit(p).sample_id)
    assert s["injection_dt"] == datetime.fromtimestamp(1_790_000_000).isoformat(sep=" ")


# ── 4: a resolved conflict re-sent ──────────────────────────────────────────

def test_a_resolved_conflict_resent_creates_nothing(hub):
    hub.gc1()
    a = hub.cdf(name="40304")
    b = hub.cdf(name="40304", shift=0.2)
    hub.submit(a)
    first = hub.submit(b)
    store.conflicts.resolve(first.conflict_id, "kept-existing", by="ryan", db=hub.db)
    again = hub.submit(b)
    assert (again.outcome, again.conflict_id) == ("conflict", first.conflict_id)
    assert len(store.conflicts.list("gc1", unresolved_only=False, db=hub.db)) == 1
    assert store.samples.count(db=hub.db) == 1


# ── 5: an earlier-injected blank arriving late flags, never reprocesses ─────

def test_a_late_blank_flags_the_samples_it_would_have_served(hub):
    hub.gc1()
    b0 = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 7, 0, 0))).sample_id
    bn = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 12, 30, 0), name="Blank3")).sample_id
    s1 = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0), name="1")).sample_id
    s2 = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 12, 0, 0), name="2")).sample_id
    s3 = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 13, 0, 0), name="3")).sample_id
    gas = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 11, 0, 0), name="4",
                             method_name="GASOLINE.M")).sample_id
    _run(hub)
    waiting = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 11, 30, 0), name="5")).sample_id
    assert _rev(hub, s1)["blank_used"] == b0
    notes = Notes()
    late = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 9, 0, 0), name="Blank2"),
                      notifier=notes).sample_id
    flagged = {sid for sid in (s1, s2, s3, gas, waiting, b0, bn)
               if hub.sample(sid)["review_note"]}
    assert flagged == {s1, s2}
    assert f"blank sample {late}" in hub.sample(s1)["review_note"]
    assert "earlier-injected blank arrived after processing" in hub.sample(s1)["review_note"]
    assert len(notes.calls) == 1
    level, message = notes.calls[0]
    assert level == "warning" and "2 " in message
    # nothing was reprocessed: revisions untouched, nothing queued for them
    assert hub.sample(s1)["current_revision"] == 1
    assert {j["sample_id"] for j in store.jobs.list(state="queued", db=hub.db)} == {waiting, late}
    # reprocessing with the current blank clears the note; keeping the old one doesn't
    _run(hub)
    pipeline.request_reprocess(s1, by="ryan", use_current_blank=True, db=hub.db)
    pipeline.request_reprocess(s2, by="ryan", db=hub.db)
    _run(hub)
    assert _rev(hub, s1)["blank_used"] == late
    assert hub.sample(s1)["review_note"] is None
    assert hub.sample(s2)["review_note"] is not None


def test_the_late_blank_notification_names_the_flagged_lab_ids(hub):
    hub.gc1()
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 7, 0, 0)))
    s1 = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0), name="40301")).sample_id
    s2 = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 11, 0, 0), name="40302")).sample_id
    _run(hub)
    notes = Notes()
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 9, 0, 0), name="Blank2"),
               notifier=notes)
    assert hub.sample(s1)["review_note"] and hub.sample(s2)["review_note"]
    (level, message), = notes.calls
    assert level == "warning"
    assert "40301" in message and "40302" in message
    assert "more" not in message


def test_the_late_blank_notification_caps_the_lab_ids_it_lists(hub):
    hub.gc1()
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 7, 0, 0)))
    labs = [f"5{i:04d}" for i in range(12)]
    for i, lab in enumerate(labs):
        hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, i, 0), name=lab))
    _run(hub)
    notes = Notes()
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 9, 0, 0), name="Blank2"),
               notifier=notes)
    (_level, message), = notes.calls
    assert "12 " in message
    assert all(lab in message for lab in labs[:10]), message
    assert labs[10] not in message and labs[11] not in message
    assert "and 2 more" in message


def test_a_late_blank_with_nothing_to_flag_is_quiet(hub):
    hub.gc1()
    notes = Notes()
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 9, 0, 0)), notifier=notes)
    assert notes.calls == []


# ── 6: blank-named samples are never blank-subtracted (v1) ──────────────────

def test_a_blank_named_sample_with_signal_gets_no_blank(hub):
    hub.gc1()
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 8, 0, 0)))
    fake = hub.submit(hub.cdf(name="Blank", injected=datetime(2026, 9, 25, 9, 0, 0))).sample_id
    _run(hub)
    s = hub.sample(fake)
    assert (s["is_blank"], s["status"]) == (0, "final")
    assert _rev(hub, fake)["blank_used"] is None


# ── 7: corrections error classes and the stuck-retry notification ───────────

class Raising:
    def __init__(self, exc):
        self.exc = exc

    def get(self, instrument):
        raise self.exc


def test_a_non_io_corrections_failure_is_an_error(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub, corrections_provider=Raising(KeyError("values")))
    s = hub.sample(sid)
    assert s["status"] == "error" and "values" in s["error"]


def test_an_oserror_from_corrections_retries(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    _run(hub, corrections_provider=Raising(PermissionError("share offline")))
    assert hub.sample(sid)["status"] == "received"
    (job,) = store.jobs.list(state="queued", db=hub.db)
    assert "share offline" in job["last_error"]


def test_ten_transient_retries_notify_once_per_instrument_until_a_success(hub, caplog):
    hub.gc1()
    a = hub.submit(hub.cdf(name="1")).sample_id
    b = hub.submit(hub.cdf(name="2", shift=0.1)).sample_id
    notes = Notes()
    clock = [datetime.now(timezone.utc)]
    w = hub.worker(corrections_provider=Raising(OSError("share offline")), notifier=notes,
                   now_fn=lambda: clock[0])
    for _ in range(12):
        clock[0] += timedelta(minutes=2)
        w.run_until_idle()
    attempts = {j["sample_id"]: j["attempts"] for j in store.jobs.list(state="queued", db=hub.db)}
    assert attempts[a] >= 10 and attempts[b] >= 10
    assert len(notes.calls) == 1
    assert notes.calls[0][0] == "error" and "GC-1" in notes.calls[0][1]
    assert hub.sample(a)["status"] == "received"
    # a success clears the latch; a new run of failures notifies again
    w.corrections_provider = file_corrections
    clock[0] += timedelta(minutes=2)
    w.run_until_idle()
    assert hub.sample(a)["status"] == "final"
    c = hub.submit(hub.cdf(name="3", shift=0.2)).sample_id
    w.corrections_provider = Raising(OSError("again"))
    for _ in range(11):
        clock[0] += timedelta(minutes=2)
        w.run_until_idle()
    assert len(notes.calls) == 2
    assert c


# ── minor ───────────────────────────────────────────────────────────────────

def test_a_map_to_an_unregistered_hub_method_is_other_method(hub):
    hub.gc1()
    store.instruments.upsert({"id": "gc1", "method_map": {"SIMDISB.M": "D2887", "GAS.M": "D7096"}},
                             db=hub.db)
    sid = hub.submit(hub.cdf(method_name="GAS.M")).sample_id
    _run(hub)
    s = hub.sample(sid)
    assert s["status"] == "other_method"
    assert "D7096" in s["error"]


def test_a_sample_changed_during_compute_is_requeued(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf()).sample_id
    real = distill.compute
    fired = []

    def compute(*a, **kw):
        out = real(*a, **kw)
        if not fired:
            fired.append(1)
            with store.connection(hub.db) as conn, store.write_txn(conn):
                store.add_revision(conn, sid, {"x": 1}, reason="replace")
        return out

    with mock.patch.object(distill, "compute", compute):
        hub.worker().run_once()
        s = hub.sample(sid)
        assert s["current_revision"] == 1                 # only the concurrent write
        assert store.export_rows.rows_after("gc1", 0, db=hub.db) == []
        (job,) = store.jobs.list(state="queued", db=hub.db)
        assert "changed" in job["last_error"]
        _run(hub)
    assert hub.sample(sid)["current_revision"] == 2


def test_a_disabled_instrument_refuses_submits(hub):
    hub.gc1()
    store.instruments.upsert({"id": "gc1", "enabled": 0}, db=hub.db)
    with pytest.raises(pipeline.InstrumentDisabled) as got:
        hub.submit(hub.cdf())
    assert isinstance(got.value, pipeline.SubmitRejected)


def test_start_sweeps_incoming_leftovers(hub):
    hub.gc1()
    incoming = hub.data / "cdf" / ".incoming"
    incoming.mkdir(parents=True)
    (incoming / "dead.CDF").write_bytes(b"x")
    os.utime(incoming / "dead.CDF", (time.time() - 3600, time.time() - 3600))
    w = hub.worker(poll_seconds=0.05)
    w.start()
    try:
        assert list(incoming.iterdir()) == []
    finally:
        w.stop()


def test_the_sweep_keeps_files_younger_than_10_minutes(hub):
    incoming = hub.data / "cdf" / ".incoming"
    incoming.mkdir(parents=True)
    old, new = incoming / "old.CDF", incoming / "new.CDF"
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    os.utime(old, (time.time() - 601, time.time() - 601))
    os.utime(new, (time.time() - 540, time.time() - 540))
    assert pipeline.sweep_incoming(hub.data) == 1
    assert [p.name for p in incoming.iterdir()] == ["new.CDF"]


def test_a_blank_arriving_during_compute_requeues(hub):
    hub.gc1()
    b0 = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 7, 0, 0))).sample_id
    _run(hub)
    sid = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0))).sample_id
    real = distill.compute
    late = []

    def compute(*a, **kw):
        out = real(*a, **kw)
        if not late:
            late.append(hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 9, 0, 0),
                                           name="Blank2")).sample_id)
        return out

    with mock.patch.object(distill, "compute", compute):
        w = hub.worker()
        while True:        # run until the sample's first job has been handled
            job = store.jobs.claim_next(db=hub.db)
            if job["sample_id"] == sid:
                w._handle(job)
                break
            w._handle(job)
    s = hub.sample(sid)
    assert s["current_revision"] is None
    assert store.list_revisions(sid, db=hub.db) == []
    job = [j for j in store.jobs.list(state="queued", db=hub.db) if j["sample_id"] == sid][0]
    assert "changed" in job["last_error"]
    _run(hub)
    assert _rev(hub, sid)["blank_used"] == late[0]
    assert b0


def test_a_kept_blank_is_not_rechecked(hub):
    hub.gc1()
    b0 = hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 7, 0, 0))).sample_id
    sid = hub.submit(hub.cdf(injected=datetime(2026, 9, 25, 10, 0, 0))).sample_id
    _run(hub)
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 9, 0, 0), name="Blank2"))
    _run(hub)
    pipeline.request_reprocess(sid, by="ryan", db=hub.db)
    _run(hub)
    assert _rev(hub, sid)["revision"] == 2
    assert _rev(hub, sid)["blank_used"] == b0


def test_one_worker_per_process(hub):
    hub.gc1()
    w1 = hub.worker(poll_seconds=0.05)
    w2 = hub.worker(poll_seconds=0.05)
    w1.start()
    try:
        with pytest.raises(RuntimeError):
            w2.start()
    finally:
        w1.stop()
    w2.start()
    w2.stop()
    assert not w2.is_alive()


def test_start_after_a_timed_out_stop_waits_for_the_old_thread(hub):
    hub.gc1()
    w = hub.worker(poll_seconds=0.05)
    release = threading.Event()
    entered = threading.Event()
    real = w.run_once

    def slow():
        entered.set()
        release.wait(5)
        return real()

    w.run_once = slow
    w.start()
    assert entered.wait(5)
    old = w._thread
    w.stop(timeout=0.01)
    assert old.is_alive()
    threading.Timer(0.2, release.set).start()
    w.run_once = real
    w.start()
    try:
        assert not old.is_alive()
        assert w._thread is not old and w.is_alive()
    finally:
        w.stop()


def test_instruments_startup_bootstraps_requeues_and_starts(hub):
    notes = Notes()
    incoming = hub.data / "cdf" / ".incoming"
    incoming.mkdir(parents=True)
    (incoming / "dead.CDF").write_bytes(b"x")
    os.utime(incoming / "dead.CDF", (time.time() - 3600, time.time() - 3600))
    w = instruments.startup(hub.conf, notes, db=hub.db, data_dir=hub.data,
                            conf_fn=lambda: hub.conf, poll_seconds=0.05,
                            corrections_provider=file_corrections)
    try:
        assert w.is_alive()
        assert store.instruments.get("gc1", db=hub.db)["live_since"] is not None
        assert list(incoming.iterdir()) == []
        assert w.notifier is notes
        import exports
        assert w.format_line is exports.format_line     # the frozen export line, not the fallback
        sid = hub.submit(hub.cdf(injected=datetime.now().replace(microsecond=0))).sample_id
        w.wake()
        deadline = time.monotonic() + 30
        while hub.sample(sid)["status"] != "final" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hub.sample(sid)["status"] == "final"
    finally:
        w.stop()
