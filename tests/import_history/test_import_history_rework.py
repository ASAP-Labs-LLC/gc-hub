"""History import (plan 2D), critic rework: a CDF arriving for a result-only
sample (C1), corrected times in hub-written lines (I1), non-canonical CSV
times held (I3), strict deltas and vanished rows (I4), progress outside the
write lock (I5), and the minors."""
from __future__ import annotations

from import_history_testlib import (  # noqa: F401  (hub: the fixture)
    ALIASES, append_csv, blank, by_lab, hub, one, results, revisions, row, sample, samples,
    src, table_counts, write_csv)

import csv
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

import exports
import pipeline
import store
import jobs.import_history as ih
from jobs.import_history import import_history

REPO = Path(__file__).resolve().parent.parent.parent
T = datetime(2026, 9, 17, 14, 50, 18)
TM = datetime(2026, 9, 25, 0, 24, 50)        # v1 on 3.11+ wrote 02:45:00
TM_V1 = "2026-09-25 02:45:00"


def _iso(d):
    return d.isoformat(sep=" ")


def _run(hub, processed, csv_path, **kw):
    kw.setdefault("conf", hub.conf)
    return import_history("gc1", processed, csv_path, instrument_folder_aliases=ALIASES,
                          db=hub.db, data_dir=hub.data, **kw)


@pytest.fixture()
def processed(hub):
    hub.gc1()
    p = hub.root / "robocopy" / "processed_cdfs2"
    p.mkdir(parents=True)
    return p


# ── C1: a CDF for a result-only sample ─────────────────────────────────────

def test_a_cdf_arriving_later_upgrades_the_result_only_sample_in_place(hub, processed):
    r = row("A1", _iso(T), source=src("A1_09172026_145018.CDF"))
    csv_path = write_csv(hub.root / "r.csv", [r])
    _run(hub, processed, csv_path)
    ro = one(hub, "A1")
    assert ro["legacy_unverified"] == 1
    a = sample(processed, "A1", T)

    summary = _run(hub, processed, csv_path)

    s = one(hub, "A1")
    assert s["id"] == ro["id"]
    assert s["cdf_sha256"] and (hub.data / s["cdf_path"]).read_bytes() == a.read_bytes()
    assert s["cdf_path"] == f"cdf/gc1/2026/09/A1_{s['id']}.CDF"
    assert s["legacy_unverified"] == 0 and s["injection_dt_source"] == "cdf"
    assert s["method_name"] == "SIMDISB.M" and s["source_name"] == a.name
    assert s["status"] == "final" and s["backfill"] == 1
    assert [x["reason"] for x in revisions(hub, s["id"])] == ["import"]
    assert store.conflicts.list(db=hub.db) == []
    assert summary["counts"]["attached_to_result_only"] == 1
    assert summary["counts"]["conflicts"] == 0
    assert table_counts(hub)["jobs"] == 0


def test_a_cdf_for_a_misparsed_result_only_time_upgrades_it_to_the_correct_time(hub, processed):
    csv_path = write_csv(hub.root / "r.csv",
                         [row("40305", TM_V1, source=src("40305_09252026_002450.CDF"))])
    _run(hub, processed, csv_path)
    ro = one(hub, "40305")
    assert ro["time_unverifiable"] == 1
    sample(processed, "40305", TM)

    _run(hub, processed, csv_path)

    s = one(hub, "40305")
    assert s["id"] == ro["id"]
    assert s["injection_dt"] == _iso(TM) and s["legacy_injection_dt"] == TM_V1
    assert s["time_corrected"] == 1 and s["time_unverifiable"] == 0
    assert s["legacy_unverified"] == 0


def test_attaching_also_adds_rows_appended_since(hub, processed):
    r1 = row("A1", _iso(T), source=src("A1_09172026_145018.CDF"))
    csv_path = write_csv(hub.root / "r.csv", [r1])
    _run(hub, processed, csv_path)
    a = sample(processed, "A1", T)
    r2 = row("A1", _iso(T), source=src(a.name), ibp="300.0")
    append_csv(csv_path, [r2])

    summary = _run(hub, processed, csv_path)

    s = one(hub, "A1")
    revs = revisions(hub, s["id"])
    assert [results(x) for x in revs] == [r1, r2]
    assert revs[1]["cdf_sha256"] == s["cdf_sha256"]
    assert s["current_revision"] == 2
    assert summary["counts"]["delta_revisions"] == 1


def test_no_attach_when_the_stored_rows_are_not_a_prefix_of_the_csv(hub, processed):
    r1 = row("A1", _iso(T), source=src("A1_09172026_145018.CDF"))
    csv_path = write_csv(hub.root / "r.csv", [r1])
    _run(hub, processed, csv_path)
    sample(processed, "A1", T)
    write_csv(csv_path, [dict(r1, **{"2887 IBP": "1.0"})])

    summary = _run(hub, processed, csv_path)

    s = one(hub, "A1")
    assert s["cdf_sha256"] is None and s["legacy_unverified"] == 1
    assert summary["counts"]["attach_refused"] == 1
    assert store.conflicts.list(db=hub.db) == []


@pytest.mark.parametrize("when,v1_time", [(T, None), (TM, TM_V1)])
def test_a_live_submit_attaches_to_the_result_only_sample_without_recompute(hub, processed,
                                                                            when, v1_time):
    csv_path = write_csv(hub.root / "r.csv",
                         [row("B7", v1_time or _iso(when), source=src("B7.CDF"))])
    _run(hub, processed, csv_path)
    ro = one(hub, "B7")

    res = hub.submit(sample(hub.src, "B7", when))

    assert res.outcome == "created" and res.sample_id == ro["id"]
    assert "result-only" in res.message
    s = one(hub, "B7")
    assert s["status"] == "final" and s["current_revision"] == 1
    assert s["cdf_path"] and (hub.data / s["cdf_path"]).exists()
    assert s["injection_dt"] == _iso(when) and s["legacy_unverified"] == 0
    assert s["legacy_injection_dt"] == (v1_time or _iso(when))
    assert s["time_corrected"] == int(v1_time is not None)
    assert [x["reason"] for x in revisions(hub, s["id"])] == ["import"]
    assert table_counts(hub)["jobs"] == 0 and table_counts(hub)["conflicts"] == 0
    again = hub.submit(sample(hub.src, "B7", when))
    assert again.outcome == "duplicate" and again.sample_id == ro["id"]


def test_replace_refuses_a_result_only_existing_sample(hub, processed):
    csv_path = write_csv(hub.root / "r.csv", [row("A1", _iso(T), source=src("x.CDF"))])
    _run(hub, processed, csv_path)
    ro = one(hub, "A1")
    cid = store.conflicts.add("gc1", "A1", _iso(T), ro["id"], "f" * 64, "cdf/x.CDF", db=hub.db)
    with pytest.raises(ValueError, match="result-only"):
        pipeline.resolve_conflict_replace(cid, by="admin", db=hub.db, data_dir=hub.data)


def test_rows_of_an_unreadable_cdf_are_not_imported(hub, processed):
    a = sample(processed, "A1", T)
    a.write_bytes(b"XXXX" + a.read_bytes()[4:])
    csv_path = write_csv(hub.root / "r.csv", [row("A1", _iso(T), source=src(a.name))])
    summary = _run(hub, processed, csv_path)
    assert samples(hub) == []
    assert summary["counts"]["cdf_errors"] == 1
    assert summary["counts"]["rows_of_unreadable_cdfs"] == 1
    assert summary["examples"]["rows_of_unreadable_cdfs"][0]["line_no"] == 2


# ── I1: hub-written lines carry the corrected time ─────────────────────────

def test_hub_written_lines_use_the_corrected_injection_time(hub, processed):
    a = sample(processed, "40305", TM)
    csv_path = write_csv(hub.root / "r.csv", [row("40305", TM_V1, source=src(a.name))])
    _run(hub, processed, csv_path)
    s = one(hub, "40305")

    pipeline.release_backfill(s["id"], by="admin", db=hub.db, data_dir=hub.data)
    lims = pipeline.export_to_lims(s["id"], by="admin", db=hub.db, data_dir=hub.data)

    with store.connection(hub.db) as conn:
        lines = [r[0] for r in conn.execute('SELECT "row" FROM export_rows ORDER BY seq')]
    assert len(lines) == 2 and lims["seq"]
    for ln in lines:
        cells = next(csv.reader([ln]))
        assert cells[1] == _iso(TM)
        assert cells[-1] == s["cdf_path"]
    assert results(revisions(hub, s["id"])[0])["InjectionDateTime"] == TM_V1   # stored verbatim
    fresh, _seq = exports.HubExporter(hub.db, data_dir=hub.data)._fresh_lines("gc1")
    assert [next(csv.reader([x]))[1] for x in fresh] == [_iso(TM)]


def test_fresh_lines_for_a_row_without_a_ledger_line_use_the_corrected_time(hub, processed):
    a = sample(processed, "40305", TM)
    csv_path = write_csv(hub.root / "r.csv", [row("40305", TM_V1, source=src(a.name))])
    _run(hub, processed, csv_path)
    s = one(hub, "40305")
    store.samples.update(s["id"], released_at=store.now_iso(), released_by="t", db=hub.db)
    fresh, _seq = exports.HubExporter(hub.db, data_dir=hub.data)._fresh_lines("gc1")
    cells = next(csv.reader([fresh[0]]))
    assert cells[1] == _iso(TM) and cells[-1] == s["cdf_path"]


# ── I3: non-canonical CSV times ────────────────────────────────────────────

def test_result_only_rows_with_a_non_canonical_time_are_held(hub, processed):
    csv_path = write_csv(hub.root / "r.csv", [
        row("Z9", "9/17/2026 14:50"), row("Z8", "2026-09-17 14:50:18.123456"),
        row("Z7", "2026-09-17 14:50:18")])
    summary = _run(hub, processed, csv_path)
    assert set(by_lab(hub)) == {"Z7"}
    assert summary["counts"]["noncanonical_time"] == 2
    assert sorted(e["line_no"] for e in summary["examples"]["noncanonical_time"]) == [2, 3]


# ── I4: strict deltas, vanished rows ───────────────────────────────────────

def test_a_relabelled_row_is_neither_moved_nor_lost_silently(hub, processed):
    a = sample(processed, "A1", T)
    b = sample(processed, "B2", datetime(2026, 9, 18, 10, 0, 0))
    ra = row("A1", _iso(T), source=src(a.name))
    rb = row("B2", "2026-09-18 10:00:00", source=src(b.name))
    rx = row("A1X", _iso(T), source=src(a.name), ibp="5")
    csv_path = write_csv(hub.root / "r.csv", [ra, rb, rx])
    _run(hub, processed, csv_path)
    write_csv(csv_path, [ra, rb, dict(rx, **{"Lab ID": "A1"})])

    summary = _run(hub, processed, csv_path)

    assert [results(x)["2887 IBP"] for x in revisions(hub, one(hub, "A1")["id"])] == ["251.63"]
    c = summary["counts"]
    assert c["rows_moved"] == 1
    assert c["rows_vanished"] == 1
    ex = summary["examples"]["rows_vanished"][0]
    assert ex["line_no"] == 4 and ex["lab_id"] == "A1X"


def test_rows_inserted_before_stored_ones_refuse_the_delta(hub, processed):
    a = sample(processed, "A1", T)
    r1 = row("A1", _iso(T), source=src(a.name))
    csv_path = write_csv(hub.root / "r.csv", [r1])
    _run(hub, processed, csv_path)
    other = row("Q1", "2026-09-10 10:10:10")
    r2 = row("A1", _iso(T), source=src(a.name), ibp="2.0")
    write_csv(csv_path, [other, r1, r2])          # r1 moved from line 2 to line 3

    summary = _run(hub, processed, csv_path)

    assert len(revisions(hub, one(hub, "A1")["id"])) == 1
    assert summary["counts"]["rows_changed"] == 1
    assert summary["counts"]["rows_vanished"] == 1


def test_a_clean_run_reports_no_vanished_rows(hub, processed):
    a = sample(processed, "A1", T)
    csv_path = write_csv(hub.root / "r.csv", [row("A1", _iso(T), source=src(a.name))])
    _run(hub, processed, csv_path)
    assert _run(hub, processed, csv_path)["counts"]["rows_vanished"] == 0


# ── I5: progress never runs under the write lock ───────────────────────────

def test_progress_is_called_outside_any_write_transaction(hub, processed):
    for i in range(5):
        sample(processed, f"S{i}", datetime(2026, 9, 17, 10, 13 + i, 1))
    events = []

    def cb(e):
        events.append((e["phase"], store._txn_depth()))
        if e["phase"] == "import":      # another writer can get in right now
            with store.connection(hub.db) as conn:
                conn.execute("PRAGMA busy_timeout = 100")
                with store.write_txn(conn):
                    pass

    _run(hub, processed, None, batch_size=2, progress=cb)
    assert all(depth == 0 for _p, depth in events)
    phases = [p for p, _d in events]
    assert phases.count("import") == 5 and phases.count("commit") == 3


# ── minors ─────────────────────────────────────────────────────────────────

def test_staged_files_are_fresh_so_the_incoming_sweep_leaves_them(hub, processed, monkeypatch):
    a = sample(processed, "A1", T)
    os.utime(a, (1_600_000_000, 1_600_000_000))
    seen = []
    real = ih._stage

    def spy(src_path, sha, data_dir):
        tmp = real(src_path, sha, data_dir)
        seen.append(tmp.stat().st_mtime)
        return tmp

    monkeypatch.setattr(ih, "_stage", spy)
    _run(hub, processed, None)
    assert seen and all(m > time.time() - 600 for m in seen)
    assert int((hub.data / one(hub, "A1")["cdf_path"]).stat().st_mtime) == 1_600_000_000


def test_legacy_injection_dt_is_the_time_v1_actually_wrote(hub, processed):
    t = datetime(2026, 9, 25, 2, 40, 5)          # stamp ...024005: Python 3.14 wrote 09-26 00:00:00
    a = sample(processed, "P14", t)
    csv_path = write_csv(hub.root / "r.csv", [row("P14", "2026-09-26 00:00:00", source=src(a.name))])
    _run(hub, processed, csv_path)
    s = one(hub, "P14")
    assert s["injection_dt"] == _iso(t)
    assert s["legacy_injection_dt"] == "2026-09-26 00:00:00" and s["time_corrected"] == 1
    assert s["current_revision"] == 1


def test_whitespace_only_names_match_by_time_when_unambiguous(hub, processed):
    a = sample(processed, "WS", T, sample_name="   ", file_name="WS_09172026_145018.CDF")
    sample(processed, "W2", datetime(2026, 9, 18, 11, 11, 11), sample_name="  ",
           file_name="W2a.CDF")
    sample(processed, "W3", datetime(2026, 9, 18, 11, 11, 11), sample_name=" ",
           file_name="W2b.CDF")
    csv_path = write_csv(hub.root / "r.csv", [
        row("   ", _iso(T), source=src(a.name)),
        row("  ", "2026-09-18 11:11:11", source=src("W2a.CDF"))])
    summary = _run(hub, processed, csv_path)
    s = one(hub, "WS_09172026_145018")
    assert s["status"] == "final" and len(revisions(hub, s["id"])) == 1
    assert summary["counts"]["whitespace_matched"] == 1
    assert summary["counts"]["unmatched_without_key"] == 1        # the ambiguous one


def test_import_runs_are_recorded_and_revisions_name_their_run(hub, processed):
    a = sample(processed, "A1", T)
    csv_path = write_csv(hub.root / "r.csv", [row("A1", _iso(T), source=src(a.name))])
    _run(hub, processed, csv_path, dry_run=True)
    assert store.import_runs.list(db=hub.db) == []
    summary = _run(hub, processed, csv_path, by="ryan")
    runs = store.import_runs.list(db=hub.db)
    assert len(runs) == 1
    run = runs[0]
    assert run["instrument_id"] == "gc1" and run["by"] == "ryan" and run["finished_at"]
    assert json.loads(run["counts"])["imported"] == 1
    srcs = json.loads(run["sources"])
    assert srcs["processed_dir"] == str(processed) and srcs["results_csv"] == str(csv_path)
    assert srcs["csv_sha256"] == summary["csv_sha256"]
    note = json.loads(revisions(hub, one(hub, "A1")["id"])[0]["notes"])["import"]
    assert note["run_id"] == run["id"] and note["csv_sha256"] == summary["csv_sha256"]
    assert summary["run_id"] == run["id"]


def test_dry_run_reports_the_same_file_in_another_instruments_folder(hub, processed):
    a = sample(processed, "A1", T)
    other = hub.root / "robocopy" / "processed_cdf"
    other.mkdir()
    shutil.copy2(a, other / "copy.CDF")
    s = import_history("gc1", processed, None, instrument_folder_aliases=ALIASES, db=None,
                       data_dir=None, dry_run=True, compare_dirs=[other])
    assert s["counts"]["same_file_elsewhere"] == 1
    assert s["examples"]["same_file_elsewhere"][0]["other"] == str(other / "copy.CDF")


def test_cli_data_dir_uses_the_hubs_settings_and_store(hub, processed):
    blank(processed, datetime(2026, 9, 16, 9, 0, 0))
    cli = [sys.executable, str(REPO / "tools" / "import_history.py"), "--dry-run",
           "--instrument", "gc1", "--processed-dir", str(processed), "--alias", "processed_cdfs2",
           "--data-dir", str(hub.data), "--json"]
    out1 = hub.root / "a.json"
    p = subprocess.run(cli + [str(out1)], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert json.loads(out1.read_text())["counts"]["blanks"] == 1
    (hub.data / "settings.json").write_text(json.dumps({"blank_max_intensity_pa": 0.001}))
    out2 = hub.root / "b.json"
    p = subprocess.run(cli + [str(out2)], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    d = json.loads(out2.read_text())
    assert d["counts"]["blanks"] == 0 and d["store_checked"] is True


def test_the_importer_uses_only_public_pipeline_helpers():
    src_text = (REPO / "jobs" / "import_history.py").read_text(encoding="utf-8")
    assert "pipeline._" not in src_text
    for name in ("existing_result", "genuine_blank", "safe_stem", "rel_path", "load_conf",
                 "find_result_only", "attach_cdf_to_result_only", "v1_time_forms",
                 "cdf_problem", "INCOMING_DIR"):
        assert hasattr(pipeline, name), name
