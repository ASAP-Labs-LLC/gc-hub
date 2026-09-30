"""Purge instrument data (v3.1, spec "Purge instrument data"): ``purge.py``
in-process, on a real two-instrument hub (``tests/purge_helpers.py``).

The hub is built once per module and copied for each test. No app import:
the routes and the ingest 503 are covered by ``test_purge_boot.py``.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("netCDF4")
pytest.importorskip("scipy")

import purge_helpers as ph  # noqa: E402
from purge_helpers import dump, tree, without  # noqa: E402

import exports  # noqa: E402
import pipeline  # noqa: E402
import purge  # noqa: E402
import store  # noqa: E402
from hub_boot import SIMDIS  # noqa: E402


_Copy = ph.HubCopy


@pytest.fixture(scope="module")
def template():
    root = Path(tempfile.mkdtemp(prefix="gc-purge-tpl-"))
    try:
        yield ph.build_two_gc_hub(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def h(template, tmp_path):
    pipeline.resume_instrument("gc1")
    pipeline.resume_instrument("gc2")
    yield _Copy(template, tmp_path)
    assert not pipeline.paused_instruments(), "a purge left an instrument paused"


def _fk_and_integrity(db):
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def _removed(before, after):
    return {t: len(before[t]) - len(after.get(t, [])) for t in before}


# ── the schema guard ────────────────────────────────────────────────────────

def test_every_table_referencing_samples_is_handled(tmp_path):
    db = tmp_path / "gc.db"
    store.migrate(db)
    with store.connection(db) as conn:
        found = purge.sample_references(conn)
    assert found == purge.KNOWN_REFERENCES, (
        "a table referencing samples (or a table that does) is not handled by purge.py: "
        f"{sorted(found ^ purge.KNOWN_REFERENCES)}")
    assert {t for t, _c, _p in found} == set(purge.OWNER_COLUMNS)


def test_a_new_referencing_table_is_found_and_refused(h):
    before = dump(h.db)
    conn = sqlite3.connect(str(h.db))
    conn.execute("CREATE TABLE sample_tags(id INTEGER PRIMARY KEY, "
                 "sample_id INTEGER REFERENCES samples(id), tag TEXT)")
    conn.execute("CREATE TABLE loose(id INTEGER PRIMARY KEY, blank_sample_id INTEGER)")
    conn.commit()
    conn.close()
    with store.connection(h.db) as c:
        assert set(purge.unhandled_references(c)) == {("sample_tags", "sample_id", "samples"),
                                                     ("loose", "blank_sample_id", "samples")}
    pv = h.preview()
    assert pv["unhandled"] and not pv["ok"]
    with pytest.raises(purge.PurgeRefused, match="sample_tags"):
        h.run()
    after = dump(h.db)
    after.pop("sample_tags"), after.pop("loose")
    assert after == before
    assert not (h.data / "backups").exists() or not list((h.data / "backups").glob("pre-purge-*"))


# ── purge all of gc1 ────────────────────────────────────────────────────────

def test_purge_gc1_leaves_gc2_and_the_settings_row_for_row(h):
    settings_before = (h.data / "settings.json").read_bytes()
    gc2_files = tree(h.data / "cdf" / "gc2")
    all_files = set(tree(h.data / "cdf").values())
    gc1_ids = ph.instrument_sample_ids(h.db, "gc1")
    before = dump(h.db)

    summary = h.run()

    after = dump(h.db)
    assert after == without(before, gc1_ids)            # every other row, byte for byte
    assert not ph.instrument_sample_ids(h.db, "gc1")
    # the settings: instrument rows, corrections, audit, standards, kv, presets...
    for t in ("instruments", "instrument_corrections", "corrections_audit", "standards",
              "settings_kv", "comment_presets", "agents", "import_runs", "web_sessions"):
        assert after[t] == before[t], t
    assert (h.data / "settings.json").read_bytes() == settings_before
    assert tree(h.data / "cdf" / "gc2") == gc2_files
    _fk_and_integrity(h.db)

    # no orphans in any table that references samples
    with store.connection(h.db) as conn:
        for table, col, parent in purge.KNOWN_REFERENCES:
            if parent != "samples":
                continue
            n = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NOT NULL AND '
                             f'"{col}" NOT IN (SELECT id FROM samples)').fetchone()[0]
            assert n == 0, (table, col)

    # files moved, never deleted, same layout under purged/<inst>-<ts>/
    moved_root = Path(summary["purged_folder"])
    assert moved_root.parent == h.data / "purged" and moved_root.name.startswith("gc1-")
    moved = tree(moved_root)
    assert moved and all(p.startswith("cdf/gc1/") or p == "manifest.json" for p in moved)
    # only gc1's calibration (one of its own samples' files) stays: still referenced
    cal = store.instruments.get("gc1", db=h.db)["calibration_cdf"]
    assert {f"cdf/gc1/{p}" for p in tree(h.data / "cdf" / "gc1")} == {cal}
    assert store.samples.get(h.ids["g1_cal"], db=h.db) is None          # its row went
    left = set(tree(h.data / "cdf").values())
    assert (left | set(moved.values())) >= all_files     # nothing lost
    journal = json.loads((moved_root / "manifest.json").read_text(encoding="utf-8"))
    assert journal["state"] == "done" and journal["notified"] and not journal["warnings"]
    assert len(journal["moves"]) == summary["files"]["moved"] == len(moved) - 1
    assert sorted(journal["sample_ids"]) == sorted(gc1_ids)
    assert summary["state"] == "done" and summary["completed_with_warnings"] is False
    assert purge.latest_journal(h.data)["state"] == "done"

    # the backup: VACUUM INTO backups/pre-purge-gc1-<ts>.db, with the old rows
    backup = Path(summary["backup"])
    assert backup.parent == h.data / "backups" and backup.name.startswith("pre-purge-gc1-")
    assert ph.instrument_sample_ids(backup, "gc1") == gc1_ids
    assert dump(backup) == before

    assert summary["samples"] == len(gc1_ids)
    assert summary["appended"] > 0
    level, message = h.notes[-1]
    assert "Ryan C" in message and "GC-1" in message and f"{len(gc1_ids)}" in message
    assert "backup" in message and "purged" in message
    assert not pipeline.paused_instruments()


def test_preview_counts_match_what_was_removed(h):
    pv = h.preview()
    before = dump(h.db)
    files_before = tree(h.data / "cdf")
    summary = h.run()
    after = dump(h.db)
    removed = _removed(before, after)
    assert pv["ok"] and pv["confirm_text"] == "PURGE GC-1"
    assert pv["samples"] == removed["samples"] == summary["samples"]
    for table, n in pv["tables"].items():
        assert removed[table] == n, table
    assert summary["tables"] == pv["tables"]
    assert {t for t, n in removed.items() if n} <= set(pv["tables"]) | {"samples"}
    moved = {p: s for p, s in files_before.items() if p not in tree(h.data / "cdf")}
    assert pv["files"]["move"] == summary["files"]["moved"] == len(moved)
    assert pv["files"]["bytes"] == sum(
        (Path(summary["purged_folder"]) / "cdf" / p).stat().st_size for p in moved)
    assert pv["appended"] == summary["appended"] > 0


def test_a_second_purge_is_a_no_op(h):
    h.run()
    before = dump(h.db)
    files = tree(h.data)
    pv = h.preview()
    assert pv["samples"] == 0 and all(n == 0 for n in pv["tables"].values())
    summary = h.run()
    assert summary["samples"] == 0 and summary["nothing_to_do"] is True
    assert summary["backup"] is None and summary["purged_folder"] is None
    assert dump(h.db) == before
    assert tree(h.data) == files


# ── scope=backfill ──────────────────────────────────────────────────────────

def test_scope_backfill_spares_live_samples_and_a_blank_they_used(h):
    bf = ph.instrument_sample_ids(h.db, "gc1", backfill_only=True)
    live = ph.instrument_sample_ids(h.db, "gc1") - bf
    assert h.ids["bf_blank"] in bf and h.ids["live_on_bf"] in live
    blank_file = h.data / store.samples.get(h.ids["bf_blank"], db=h.db)["cdf_path"]
    before = dump(h.db)

    pv = h.preview(scope="backfill")
    kept = {k["id"] for k in pv["kept_samples"]}
    assert kept == {h.ids["bf_blank"]}
    assert "blank" in pv["kept_samples"][0]["reason"]
    summary = h.run(scope="backfill")

    purged = bf - kept
    assert dump(h.db) == without(before, purged)
    assert ph.instrument_sample_ids(h.db, "gc1") == live | kept
    assert blank_file.is_file()                          # still referenced: never moved
    assert summary["samples"] == len(purged) == pv["samples"]
    _fk_and_integrity(h.db)


# ── CDFs that something else still references ──────────────────────────────

def test_referenced_cdfs_are_kept_in_place(h):
    cal = store.samples.get(h.ids["rerun"], db=h.db)["cdf_path"]          # data-relative
    std = h.data / store.samples.get(h.ids["slashed"], db=h.db)["cdf_path"]  # absolute
    store.instruments.upsert({"id": "gc1", "calibration_cdf": cal}, db=h.db)
    with store.connection(h.db) as conn:
        with store.write_txn(conn):
            conn.execute("INSERT INTO standards(name, instrument_id, cdf_path, added_at) "
                         "VALUES ('Std', 'gc1', ?, ?)", (str(std), store.now_iso()))
    pv = h.preview()
    reasons = {Path(k["path"]).name: k["reason"] for k in pv["kept_files"]}
    assert "calibration" in reasons[Path(cal).name]
    assert "standard" in reasons[std.name]
    h.run()
    assert (h.data / cal).is_file() and std.is_file()
    assert store.samples.get(h.ids["rerun"], db=h.db) is None             # rows still go
    assert store.instruments.get("gc1", db=h.db)["calibration_cdf"] == cal


# ── refusals and failures ───────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["PURGE gc1", "purge GC-1", "PURGE GC-2", "", "PURGE GC-1 "])
def test_a_wrong_confirmation_is_refused(h, text):
    before = dump(h.db)
    with pytest.raises(purge.PurgeRefused):
        h.run(confirm=text or "x")
    assert dump(h.db) == before
    assert not (h.data / "backups").exists() or not list((h.data / "backups").glob("pre-purge-*"))


def test_bad_scope_and_unknown_instrument_are_refused(h):
    with pytest.raises(purge.PurgeRefused):
        purge.preview("gc1", "everything", db=h.db, data_dir=h.data)
    with pytest.raises(purge.PurgeRefused) as exc:
        purge.preview("gc9", "all", db=h.db, data_dir=h.data)
    assert exc.value.status == 404


def test_a_crash_inside_the_transaction_changes_nothing(h):
    before = dump(h.db)
    files = tree(h.data / "cdf")

    def boom(conn):
        # every delete has run in this transaction by now
        assert conn.execute("SELECT COUNT(*) FROM samples WHERE instrument_id='gc1'").fetchone()[0] == 0
        raise RuntimeError("power cut")

    with pytest.raises(RuntimeError, match="power cut"):
        h.run(_before_commit=boom)
    assert dump(h.db) == before
    assert tree(h.data / "cdf") == files
    assert not pipeline.paused_instruments()
    _fk_and_integrity(h.db)
    # the journal says abandoned, its backup is gone, and a notification says so
    journal = purge.latest_journal(h.data)
    assert journal["state"] == "abandoned" and "power cut" in journal["reason"]
    assert not Path(journal["backup"]).exists()
    assert not list((h.data / "backups").glob("*pre-purge-*"))
    assert "nothing was removed" in h.notes[-1][1] and "GC-1" in h.notes[-1][1]
    assert set(tree(Path(journal["purged_folder"]))) == {"manifest.json"}


def test_failed_purges_do_not_pile_up_backups(h):
    def boom(conn):
        raise RuntimeError("disk trouble")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            h.run(_before_commit=boom)
    assert not list((h.data / "backups").glob("*pre-purge-*"))
    h.run()
    assert len(list((h.data / "backups").glob("pre-purge-*"))) == 1


def test_it_waits_for_a_running_job_and_gives_up_cleanly(h):
    sid = h.ids["final"]
    job = store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid, db=h.db)
    with store.connection(h.db) as conn:
        conn.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))
    before = dump(h.db)
    with pytest.raises(purge.PurgeError, match="still running"):
        h.run(wait_seconds=0.5, poll_seconds=0.05)
    assert dump(h.db) == before and not pipeline.paused_instruments()

    # the job finishes while the purge waits: it goes ahead
    out = {}
    t = threading.Thread(target=lambda: out.update(s=h.run(wait_seconds=30, poll_seconds=0.05)))
    t.start()
    time.sleep(0.5)
    assert pipeline.instrument_paused("gc1") and "s" not in out
    with pytest.raises(pipeline.InstrumentPaused):
        pipeline.submit("gc1", h.cdf("77777", datetime(2026, 9, 26, 9, 0)), conf=h.conf,
                        data_dir=h.data, db=h.db)
    store.jobs.complete(job, db=h.db)
    t.join(30)
    assert out["s"]["samples"] > 0 and not pipeline.paused_instruments()


# ── exports after the purge ─────────────────────────────────────────────────

def test_the_results_csv_is_not_edited_and_appends_continue(h):
    csv = h.exporter.export_path("gc1")
    old = csv.read_bytes()
    summary = h.run()
    assert summary["export"]["relinked"] is True
    assert csv.read_bytes() == old                                  # never edited
    sid = pipeline.submit("gc1", h.cdf("88888", datetime(2026, 9, 27, 9, 0)), conf=h.conf,
                          data_dir=h.data, db=h.db).sample_id
    h.worker().run_until_idle()
    assert store.samples.get(sid, db=h.db)["status"] == "final"
    r = h.exporter.flush("gc1")
    assert r.error is None and r.appended == 1
    new = csv.read_bytes()
    assert new.startswith(old) and b"88888," in new[len(old):]
    assert h.exporter.status("gc1")["refused"] is None


def test_new_results_path_goes_through_the_exporter(h, tmp_path):
    target = tmp_path / "lem" / "gc1_after_purge.csv"
    target.parent.mkdir()
    summary = h.run(new_results_path=str(target))
    assert summary["export"]["new_path"] == str(target)
    assert store.instruments.get("gc1", db=h.db)["export_path"] == str(target)


@pytest.mark.parametrize("bad", ["relative.csv", "/no/such/folder/x.csv", "x.txt"])
def test_a_bad_new_results_path_is_refused_before_anything(h, bad):
    before = dump(h.db)
    with pytest.raises(purge.PurgeRefused):
        h.run(new_results_path=bad)
    assert dump(h.db) == before


def test_another_instruments_results_file_is_refused(h):
    before = dump(h.db)
    with pytest.raises(purge.PurgeRefused, match="gc2"):
        h.run(new_results_path=str(h.exporter.export_path("gc2")))
    assert dump(h.db) == before


# ── pausing one instrument ──────────────────────────────────────────────────

def test_the_worker_skips_a_paused_instruments_jobs(h):
    a = pipeline.submit("gc1", h.cdf("90001", datetime(2026, 9, 27, 10, 0)), conf=h.conf,
                        data_dir=h.data, db=h.db).sample_id
    b = pipeline.submit("gc2", h.cdf("90002", datetime(2026, 9, 27, 10, 5)), conf=h.conf,
                        data_dir=h.data, db=h.db).sample_id
    pipeline.pause_instrument("gc1")
    try:
        w = h.worker()
        w.run_until_idle()
        assert store.samples.get(b, db=h.db)["status"] == "final"
        assert store.samples.get(a, db=h.db)["status"] == "received"
        assert [j["sample_id"] for j in store.jobs.list(state="queued", db=h.db)
                if j["sample_id"] == a] == [a]
        with pytest.raises(pipeline.InstrumentPaused, match="retry"):
            pipeline.submit("gc1", h.cdf("90003", datetime(2026, 9, 27, 11, 0)), conf=h.conf,
                            data_dir=h.data, db=h.db)
        assert pipeline.submit("gc2", h.cdf("90004", datetime(2026, 9, 27, 11, 5)), conf=h.conf,
                               data_dir=h.data, db=h.db).outcome == "created"
    finally:
        pipeline.resume_instrument("gc1")
    h.worker().run_until_idle()
    assert store.samples.get(a, db=h.db)["status"] == "final"


def test_claim_next_can_exclude_instruments(h):
    a = pipeline.submit("gc1", h.cdf("91001", datetime(2026, 9, 27, 10, 0)), conf=h.conf,
                        data_dir=h.data, db=h.db).sample_id
    with store.connection(h.db) as conn:
        conn.execute("DELETE FROM jobs WHERE state='queued' AND sample_id<>?", (a,))
    assert store.jobs.claim_next(kind="process", exclude_instruments=("gc1",), db=h.db) is None
    job = store.jobs.claim_next(kind="process", exclude_instruments=("gc2",), db=h.db)
    assert job["sample_id"] == a


# ── records ─────────────────────────────────────────────────────────────────

def test_an_instrument_events_row_when_the_table_exists(h):
    with store.connection(h.db) as conn:
        conn.execute('CREATE TABLE instrument_events(id INTEGER PRIMARY KEY, instrument_id TEXT, '
                     'kind TEXT, "by" TEXT, at TEXT, detail TEXT)')
    summary = h.run()
    with store.connection(h.db) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM instrument_events")]
    assert len(rows) == 1 and rows[0]["kind"] == "purge" and rows[0]["instrument_id"] == "gc1"
    assert rows[0]["by"] == "Ryan C (10.0.0.9)"
    assert json.loads(rows[0]["detail"])["samples"] == summary["samples"]


def test_nightly_pruning_never_removes_a_pre_purge_backup(h):
    summary = h.run()
    backup = Path(summary["backup"])
    for day in range(1, 6):
        store.backup_nightly(h.db, keep=2, settings_path=h.data / "settings.json",
                             now=datetime(2026, 10, day, 2, 0))
    assert backup.is_file()
    assert len(list((h.data / "backups").glob("gc-*.db"))) == 2


# ── review fixes (v3.1 critic) ──────────────────────────────────────────────

def _add_conflict(h, instrument, existing_sample_id, rel):
    src = h.data / store.samples.get(h.ids["final"], db=h.db)["cdf_path"]
    (h.data / rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, h.data / rel)
    with store.connection(h.db) as conn:
        with store.write_txn(conn):
            cur = conn.execute(
                "INSERT INTO conflicts(instrument_id, lab_id, injection_dt, existing_sample_id, "
                "cdf_sha256, cdf_path, received_at) VALUES (?, 'X', '2026-09-25 14:23:00', ?, "
                "?, ?, ?)", (instrument, existing_sample_id, f"sha-{rel}", rel, store.now_iso()))
            return cur.lastrowid


@pytest.mark.parametrize("scope", ["all", "backfill"])
def test_conflicts_are_selected_by_instrument(h, scope):
    lonely = _add_conflict(h, "gc1", None, "cdf/gc1/conflicts/2026/09/lonely.CDF")
    cross = _add_conflict(h, "gc2", h.ids["released"], "cdf/gc2/conflicts/2026/09/cross.CDF")
    pv = h.preview(scope=scope)
    kept = {k["id"]: k["reason"] for k in pv["kept_samples"]}
    assert "conflict" in kept[h.ids["released"]]        # another instrument's conflict keeps it
    h.run(scope=scope)
    with store.connection(h.db) as conn:
        left = {r[0] for r in conn.execute("SELECT id FROM conflicts")}
    assert cross in left and (h.data / "cdf/gc2/conflicts/2026/09/cross.CDF").is_file()
    assert store.samples.get(h.ids["released"], db=h.db) is not None
    if scope == "all":           # the instrument's conflicts without a sample go too
        assert lonely not in left
        assert not (h.data / "cdf/gc1/conflicts/2026/09/lonely.CDF").exists()
    else:
        assert lonely in left
    _fk_and_integrity(h.db)


def test_a_calibration_a_kept_result_was_computed_with_is_kept(h):
    rel = store.samples.get(h.ids["rerun"], db=h.db)["cdf_path"]
    with store.connection(h.db) as conn:
        with store.write_txn(conn):
            conn.execute("UPDATE sample_results SET calibration_used=? WHERE sample_id=?",
                         (json.dumps({"cdf": str(h.data / rel), "sensitivity": 50.0}),
                          h.ids["g2_final"]))
    pv = h.preview()
    reasons = {k["path"]: k["reason"] for k in pv["kept_files"]}
    assert "calibration" in reasons[rel] and "computed with" in reasons[rel]
    h.run()
    assert (h.data / rel).is_file()


def test_two_instruments_with_one_name_confirm_with_the_id(h):
    store.instruments.upsert({"id": "gc2", "name": "gc-1"}, db=h.db)
    pv = h.preview()
    assert pv["confirm_text"] == "PURGE GC-1 (gc1)"
    with pytest.raises(purge.PurgeRefused, match=r"PURGE GC-1 \(gc1\)"):
        h.run(confirm="PURGE GC-1")
    assert h.run(confirm="PURGE GC-1 (gc1)")["samples"] > 0


def test_a_failure_after_the_commit_completes_with_warnings(h, tmp_path, monkeypatch):
    def broken(_inst):
        raise OSError("share offline")

    def refuse(_inst, path):
        raise exports.ExportRefused("in-use", Path(path), "taken meanwhile")

    monkeypatch.setattr(h.exporter, "after_purge", broken)
    monkeypatch.setattr(h.exporter, "new_path", refuse)
    summary = h.run(new_results_path=str(tmp_path / "new.csv"))
    assert summary["state"] == "done" and summary["completed_with_warnings"] is True
    assert any("share offline" in w for w in summary["warnings"])
    assert any("new results file was refused" in w for w in summary["warnings"])
    assert not ph.instrument_sample_ids(h.db, "gc1")
    assert "completed with warnings" in h.notes[-1][1]


def _until(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_no_lock_is_held_while_waiting_for_a_job(h, monkeypatch):
    """A job starts between the wait and the transaction (the _Retry path): the
    purge waits again WITHOUT holding gc1's export lock, so the exporter's pass
    over every instrument is not blocked meanwhile."""
    sid = h.ids["final"]
    job = store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid, db=h.db)
    calls = {"n": 0}
    orig = purge._wait_for_running

    def wait(*a, **k):
        calls["n"] += 1
        orig(*a, **k)
        if calls["n"] == 2:                      # the one right before the transaction
            with store.connection(h.db) as c:
                c.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))

    monkeypatch.setattr(purge, "_wait_for_running", wait)
    out = {}
    t = threading.Thread(target=lambda: out.update(s=h.run(poll_seconds=0.05, wait_seconds=30)))
    t.start()
    assert _until(lambda: calls["n"] >= 3)
    tick = threading.Thread(target=h.exporter.tick, daemon=True)
    tick.start()
    tick.join(5)
    assert not tick.is_alive(), "the exporter was blocked by the waiting purge"
    store.jobs.complete(job, db=h.db)
    t.join(30)
    assert out["s"]["samples"] > 0


def test_a_purge_publishes_live_events(h):
    import live
    gc1 = ph.instrument_sample_ids(h.db, "gc1")
    start = live.poll(None)["cursor"]
    h.run()
    out = live.poll(start)
    assert "gc1" in out["instruments"]
    assert set(out["samples"]) >= gc1
