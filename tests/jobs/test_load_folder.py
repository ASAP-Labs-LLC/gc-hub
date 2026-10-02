"""The folder loader (2A1 T6): ``jobs.load_folder.load_folder`` and its CLI.

It submits every CDF under a folder through ``pipeline.submit`` in
injection-time order, never writes to the source, is resumable (a re-run is
all duplicates), counts every outcome, never releases backfill, and reports
the late-blank review notes instead of notifying.
"""
from __future__ import annotations

from loader_testlib import SIMDIS, hub, make_read_only, make_writable, plain_blank, snapshot  # noqa: F401

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import cdf_fixtures as fx
import pipeline
import store
from jobs.load_folder import load_folder

REPO = Path(__file__).resolve().parent.parent.parent
CLI = REPO / "tools" / "load_folder.py"


def _load(hub, folder, *, backfill=False, instrument="gc1", **kw):
    return load_folder(instrument, folder, backfill=backfill, db=hub.db, data_dir=hub.data,
                       conf=hub.conf, **kw)


def _samples(hub, instrument="gc1"):
    return store.samples.search(instrument=instrument, limit=1000, db=hub.db)


@pytest.fixture()
def folder(tmp_path):
    d = tmp_path / "robocopy"
    d.mkdir()
    yield d
    if d.exists():
        make_writable(d)


# ── enumeration and submission ──────────────────────────────────────────────

def test_loads_every_cdf_recursively_whatever_the_case_of_the_extension(hub, folder):
    hub.gc1()
    (folder / "sub" / "deeper").mkdir(parents=True)
    a = fx.sample_cdf(folder / "a.CDF", name="40301", injected=datetime(2026, 9, 25, 14, 23, 0),
                      method_name=SIMDIS)
    b = fx.sample_cdf(folder / "sub" / "b.cdf", name="40302", injected=datetime(2026, 9, 25, 15, 23, 0),
                      method_name=SIMDIS)
    c = fx.sample_cdf(folder / "sub" / "deeper" / "c.Cdf", name="40303",
                      injected=datetime(2026, 9, 25, 16, 23, 0), method_name=SIMDIS)
    (folder / "notes.txt").write_text("not a CDF")
    (folder / "sub" / "a.CDF.bak").write_bytes(a.read_bytes())
    os.utime(b, (1_700_000_000, 1_700_000_000))

    summary = _load(hub, folder)

    assert summary["files"] == 3
    assert summary["created"] == 3
    assert (summary["duplicate"], summary["conflict"], summary["cross_instrument"],
            summary["rejected"], summary["failed"]) == (0, 0, 0, 0, 0)
    got = {s["lab_id"]: s for s in _samples(hub)}
    assert set(got) == {"40301", "40302", "40303"}
    assert got["40302"]["source_name"] == "b.cdf"            # the original file name
    assert got["40303"]["source_name"] == "c.Cdf"
    stored = hub.data / got["40302"]["cdf_path"]
    assert int(stored.stat().st_mtime) == 1_700_000_000      # the original mtime


def test_submits_in_injection_time_order_so_a_history_blank_raises_no_late_blank_flag(hub, folder):
    """The blank is injected before the sample but its file name sorts after
    it. The hub's worker runs between submissions (as it does live): in file
    name order the sample would be final before its blank arrived and get a
    late-blank review note; in injection order it never does."""
    hub.gc1()
    sample = fx.sample_cdf(folder / "a_40304.CDF", name="40304",
                           injected=datetime(2026, 9, 25, 14, 23, 0), method_name=SIMDIS)
    blank = fx.blank_cdf(folder / "z_blank.CDF", injected=datetime(2026, 9, 25, 9, 30, 27),
                         method_name=SIMDIS)
    worker = hub.worker()
    order = []

    def progress(event):
        if event.get("phase") == "submit":
            order.append(Path(event["file"]).name)
            worker.run_until_idle()

    summary = _load(hub, folder, progress=progress)

    assert order == ["z_blank.CDF", "a_40304.CDF"]
    assert summary["late_blank_review_notes"] == 0
    worker.run_until_idle()
    s = store.samples.find_by_key("gc1", "40304", "2026-09-25 14:23:00", db=hub.db)
    b = store.samples.find_by_key("gc1", "Blank", "2026-09-25 09:30:27", db=hub.db)
    assert s["status"] == "final" and s["review_note"] is None
    assert store.get_revision(s["id"], db=hub.db)["blank_used"] == b["id"]

    # Control: the same files submitted in file-name order do get flagged.
    other = hub.root / "other"
    other.mkdir()
    hub2 = type(hub)(other)
    hub2.gc1()
    w2 = hub2.worker()
    for p in sorted(folder.iterdir()):
        hub2.submit(p)
        w2.run_until_idle()
    flagged = store.samples.find_by_key("gc1", "40304", "2026-09-25 14:23:00", db=hub2.db)
    assert flagged["review_note"]


def test_the_late_blank_review_notes_are_counted_in_the_summary(hub, folder):
    hub.gc1()
    first = hub.root / "first"
    first.mkdir()
    fx.sample_cdf(first / "s.CDF", name="40304", injected=datetime(2026, 9, 25, 14, 23, 0),
                  method_name=SIMDIS)
    _load(hub, first)
    hub.worker().run_until_idle()
    fx.blank_cdf(folder / "b.CDF", injected=datetime(2026, 9, 25, 9, 30, 27), method_name=SIMDIS)

    summary = _load(hub, folder)       # notifier=None: counted in the summary instead

    assert summary["created"] == 1
    assert summary["late_blank_review_notes"] == 1


def test_a_rerun_is_a_no_op(hub, folder):
    hub.gc1()
    fx.sample_cdf(folder / "a.CDF", name="40301", method_name=SIMDIS)
    fx.blank_cdf(folder / "b.CDF", method_name=SIMDIS)
    first = _load(hub, folder)
    jobs_before = store.jobs.list(db=hub.db)

    again = _load(hub, folder)

    assert first["created"] == 2
    assert again["created"] == 0 and again["duplicate"] == 2
    assert len(_samples(hub)) == 2
    assert store.jobs.list(db=hub.db) == jobs_before


def test_the_source_folder_is_never_written(hub, folder):
    hub.gc1()
    (folder / "sub").mkdir()
    fx.sample_cdf(folder / "a.CDF", name="40301", method_name=SIMDIS)
    fx.blank_cdf(folder / "sub" / "b.CDF", method_name=SIMDIS)
    make_read_only(folder)
    before = snapshot(folder)

    summary = _load(hub, folder)
    hub.worker().run_until_idle()

    assert summary["created"] == 2
    assert snapshot(folder) == before


# ── outcomes ────────────────────────────────────────────────────────────────

def test_counts_conflicts_cross_instrument_and_rejections(hub, folder):
    hub.gc1()
    hub.gc2()
    fx.sample_cdf(folder / "a.CDF", name="40301", injected=datetime(2026, 9, 25, 14, 23, 0),
                  method_name=SIMDIS)
    fx.sample_cdf(folder / "a_again.CDF", name="40301", injected=datetime(2026, 9, 25, 14, 23, 0),
                  method_name=SIMDIS, shift=0.1)                       # same key, other bytes
    held = fx.sample_cdf(hub.src / "gc2.CDF", name="40309", method_name=SIMDIS)
    hub.submit(held, instrument="gc2")
    (folder / "gc2_copy.CDF").write_bytes(held.read_bytes())
    whole = fx.sample_cdf(hub.src / "whole.CDF", name="40310", method_name=SIMDIS)
    fx.truncated_copy(whole, folder / "cut.CDF", 0.9)
    (folder / "junk.CDF").write_bytes(b"this is not a netCDF file")

    summary = _load(hub, folder)

    assert summary["files"] == 5
    assert summary["created"] == 1
    assert summary["conflict"] == 1
    assert summary["cross_instrument"] == 1
    assert summary["rejected"] == 2
    assert summary["truncated"] == 1
    reasons = {Path(r["file"]).name: r["reason"] for r in summary["rejected_files"]}
    assert set(reasons) == {"cut.CDF", "junk.CDF"}
    assert "truncated" in reasons["cut.CDF"]
    assert len(summary["conflicts"]) == 1
    assert Path(summary["conflicts"][0]["file"]).name in {"a.CDF", "a_again.CDF"}


def test_requires_an_existing_instrument_and_folder(hub, folder):
    with pytest.raises(pipeline.UnknownInstrument):
        _load(hub, folder)
    hub.gc1()
    with pytest.raises(NotADirectoryError):
        _load(hub, folder / "missing")


def test_a_disabled_instrument_stops_the_load(hub, folder):
    hub.gc1()
    store.instruments.upsert({"id": "gc1", "enabled": 0}, db=hub.db)
    fx.sample_cdf(folder / "a.CDF", name="40301", method_name=SIMDIS)
    with pytest.raises(pipeline.InstrumentDisabled):
        _load(hub, folder)


def test_progress_reports_every_file(hub, folder):
    hub.gc1()
    fx.sample_cdf(folder / "a.CDF", name="40301", injected=datetime(2026, 9, 25, 14, 23, 0),
                  method_name=SIMDIS)
    fx.sample_cdf(folder / "b.CDF", name="40302", injected=datetime(2026, 9, 25, 15, 23, 0),
                  method_name=SIMDIS)
    events = []
    _load(hub, folder, progress=events.append)
    submits = [e for e in events if e["phase"] == "submit"]
    assert [(e["done"], e["total"], e["outcome"]) for e in submits] == [(1, 2, "created"),
                                                                       (2, 2, "created")]
    assert events[-1]["phase"] == "done"


# ── backfill ────────────────────────────────────────────────────────────────

def test_live_samples_export_when_backfill_is_off(hub, folder):
    hub.gc1(live_since=datetime(2020, 1, 1))
    fx.sample_cdf(folder / "a.CDF", name="40301", method_name=SIMDIS)
    summary = _load(hub, folder, backfill=False)
    hub.worker().run_until_idle()
    (s,) = _samples(hub)
    assert s["backfill"] == 0 and summary["backfill"] == 0
    assert len(store.export_rows.rows_after("gc1", 0, db=hub.db)) == 1


def test_the_backfill_flag_forces_backfill_and_nothing_is_released(hub, folder):
    hub.gc1(live_since=datetime(2020, 1, 1))
    fx.sample_cdf(folder / "a.CDF", name="40301", method_name=SIMDIS)
    fx.blank_cdf(folder / "b.CDF", method_name=SIMDIS)
    summary = _load(hub, folder, backfill=True)
    hub.worker().run_until_idle()
    rows = _samples(hub)
    assert {s["backfill"] for s in rows} == {1}
    assert {s["status"] for s in rows} == {"final"}
    assert all(s["released_at"] is None for s in rows)
    assert summary["backfill"] == 2
    assert store.export_rows.rows_after("gc1", 0, db=hub.db) == []


def test_injections_before_live_since_are_backfill_without_the_flag(hub, folder):
    hub.gc1(live_since=datetime(2030, 1, 1))
    fx.sample_cdf(folder / "a.CDF", name="40301", method_name=SIMDIS)
    summary = _load(hub, folder, backfill=False)
    hub.worker().run_until_idle()
    (s,) = _samples(hub)
    assert s["backfill"] == 1 and summary["backfill"] == 1
    assert store.export_rows.rows_after("gc1", 0, db=hub.db) == []


def test_submit_force_backfill_is_additive(hub):
    hub.gc1(live_since=datetime(2020, 1, 1))
    a = hub.submit(hub.cdf(name="40301"))
    b = hub.submit(hub.cdf(name="40302"), force_backfill=True)
    assert hub.sample(a.sample_id)["backfill"] == 0
    assert hub.sample(b.sample_id)["backfill"] == 1


# ── the CLI ─────────────────────────────────────────────────────────────────

def _cli(*args):
    env = dict(os.environ)
    env.pop("GC_DATA_DIR", None)
    return subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True,
                          text=True, env=env, timeout=120)


def test_cli_loads_and_prints_the_summary(hub, folder):
    hub.gc1()
    fx.sample_cdf(folder / "a.CDF", name="40301", method_name=SIMDIS)
    make_read_only(folder)
    before = snapshot(folder)

    res = _cli("--data-dir", hub.data, "--json", "gc1", folder)

    assert res.returncode == 0, res.stderr
    summary = json.loads(res.stdout)
    assert summary["created"] == 1
    assert snapshot(folder) == before
    again = _cli("--data-dir", hub.data, "gc1", folder)
    assert again.returncode == 0, again.stderr
    assert "1 duplicate" in again.stdout


def test_cli_exit_codes(hub, folder):
    hub.gc1()
    assert _cli("--data-dir", hub.data, "gc9", folder).returncode == 2       # unknown instrument
    assert _cli("--data-dir", hub.data, "gc1", folder / "missing").returncode == 2
    (folder / "junk.CDF").write_bytes(b"not a CDF")
    res = _cli("--data-dir", hub.data, "gc1", folder)
    assert res.returncode == 1                                               # a file was rejected
    assert "junk.CDF" in res.stdout


def test_a_lem_note_added_to_a_late_blank_note_is_not_counted_as_a_new_one(hub):
    # v5.1.0: an export during the load can append the LEM warning to a sample
    # that already had a late-blank note; that is not a new late blank.
    import jobs.load_folder as lf
    hub.gc1()
    sid = store.samples.insert_received("gc1", "40304, rerun", "2026-09-25 14:23:00", "cdf",
                                        cdf_sha256="ab" * 32, cdf_path=None, method_name=SIMDIS,
                                        source_name="s.CDF", is_blank=0, backfill=0, db=hub.db)
    late = pipeline.LATE_BLANK_NOTE.format(blank_id=7)
    store.samples.update(sid, review_note=late, db=hub.db)
    before = lf._late_blank_notes("gc1", hub.db)
    store.samples.update(sid, review_note=late + "; Lab ID 'x,y' contains a comma. LEM splits",
                         db=hub.db)
    summary = {}
    lf._finish(summary, "gc1", hub.db, before, 0.0)
    assert summary["late_blank_review_notes"] == 0
    # a different late blank on the same sample still counts
    store.samples.update(sid, review_note=pipeline.LATE_BLANK_NOTE.format(blank_id=9), db=hub.db)
    lf._finish(summary, "gc1", hub.db, before, 0.0)
    assert summary["late_blank_review_notes"] == 1
