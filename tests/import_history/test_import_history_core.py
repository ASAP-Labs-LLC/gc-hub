"""History import (plan 2D), task 2: the core classes and verbatim revisions.

CDF-backed samples keep every CSV row as an ``import`` revision (CSV order,
last current) with the CSV strings verbatim; orphan CDFs are ``raw_only``;
result-only rows are ``final`` samples with no CDF. Everything is backfill,
nothing is queued, nothing is exported.
"""
from __future__ import annotations

from import_history_testlib import (  # noqa: F401  (hub: the fixture)
    ALIASES, SIMDIS, blank, csv_lines, hub, one, results, revisions, row, sample, samples,
    src, table_counts, v1_name, write_csv)

import csv
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

import exports
import store
from jobs.import_history import import_history

T1 = datetime(2026, 9, 17, 14, 50, 18)
T2 = datetime(2026, 9, 18, 15, 33, 41)
T3 = datetime(2026, 9, 19, 16, 43, 5)


def _run(hub, processed, csv_path, **kw):
    kw.setdefault("conf", hub.conf)
    return import_history("gc1", processed, csv_path, instrument_folder_aliases=ALIASES,
                          db=hub.db, data_dir=hub.data, **kw)


def _iso(d: datetime) -> str:
    return d.isoformat(sep=" ")


@pytest.fixture()
def processed(hub):
    hub.gc1()
    return hub.root / "robocopy" / "processed_cdfs2"


def test_attached_sample_keeps_every_row_as_a_verbatim_import_revision(hub, processed):
    a = sample(processed, "AF26", T1)
    os.utime(a, (1_758_000_000, 1_758_000_000))
    r1 = row("AF26", _iso(T1), source=src(a.name), ibp="106.61", best_fit="Mix", fit_score="0.451")
    r2 = row("AF26", _iso(T1), source=src(a.name), ibp="106.70", best_fit="Diesel", fit_score="0.912")
    csv_path = write_csv(hub.root / "distill_results.csv", [r1, r2])

    summary = _run(hub, processed, csv_path)

    s = one(hub, "AF26")
    assert s["status"] == "final"
    assert s["backfill"] == 1 and s["released_at"] is None
    assert s["legacy_unverified"] == 0 and s["time_unverifiable"] == 0
    assert s["injection_dt"] == _iso(T1) and s["injection_dt_source"] == "cdf"
    assert s["legacy_injection_dt"] == _iso(T1) and s["time_corrected"] == 0
    assert s["method_name"] == SIMDIS
    assert s["source_name"] == a.name
    assert s["is_blank"] == 0
    stored = hub.data / s["cdf_path"]
    assert stored.read_bytes() == a.read_bytes()
    assert s["cdf_path"].startswith("cdf/gc1/2026/09/")
    assert int(stored.stat().st_mtime) == 1_758_000_000
    assert s["cdf_sha256"] == __import__("hashlib").sha256(a.read_bytes()).hexdigest()

    revs = revisions(hub, s["id"])
    assert [r["revision"] for r in revs] == [1, 2]
    assert s["current_revision"] == 2
    for rev, v1 in zip(revs, (r1, r2)):
        assert results(rev) == v1                       # every CSV cell, verbatim strings
        assert rev["reason"] == "import"
        assert json.loads(rev["corrections_used"]) == {"source": "legacy"}
        assert rev["blank_used"] is None and rev["calibration_used"] is None
        assert rev["d86_uncorrected"] is None and rev["flags"] is None
        assert rev["cdf_sha256"] == s["cdf_sha256"] and rev["cdf_path"] == s["cdf_path"]
        assert rev["by"] == "import"
    assert revs[0]["best_fit"] == "Mix" and revs[0]["fit_score"] == pytest.approx(0.451)
    assert revs[1]["best_fit"] == "Diesel" and revs[1]["fit_score"] == pytest.approx(0.912)
    notes = [json.loads(r["notes"]) for r in revs]
    assert [n["import"]["line_no"] for n in notes] == [2, 3]

    counts = table_counts(hub)
    assert counts["jobs"] == 0                # never recomputed
    assert counts["export_rows"] == 0         # D11: backfill is never exported
    assert summary["counts"]["attached"] == 1
    assert summary["counts"]["revisions"] == 2


def test_format_line_of_an_imported_revision_reproduces_the_v1_line(hub, processed):
    a = sample(processed, "40301", T1)
    b = sample(processed, 'Odd, "quoted" lab', T2, file_name="odd_09182026_153341.CDF")
    rows = [
        row("40301", _iso(T1), source=src(a.name)),
        row('Odd, "quoted" lab', _iso(T2), source=src(b.name), best_fit='Mix: A, B "80/20"',
            fit_score="0.500"),
        row("40301", _iso(T1), source=src(a.name), ibp="1e-05", fit_score=""),
    ]
    csv_path = write_csv(hub.root / "distill_results.csv", rows)
    lines = csv_lines(csv_path)

    _run(hub, processed, csv_path)

    got = []
    for s in samples(hub):
        for rev in revisions(hub, s["id"]):
            n = json.loads(rev["notes"])["import"]["line_no"]
            res = results(rev)
            got.append(n)
            v1 = lines[n - 2]
            # byte for byte with v1's own Source File...
            assert exports.format_line(rev["results"], res["Source File"]) == v1
            # ...and the hub's export differs only in Source File
            hub_cells = next(csv.reader([exports.format_line(rev["results"], s["cdf_path"])]))
            v1_cells = next(csv.reader([v1]))
            assert hub_cells[:-1] == v1_cells[:-1]
            assert hub_cells[-1] == s["cdf_path"]
    assert sorted(got) == [2, 3, 4]


def test_orphan_cdf_is_raw_only_backfill_with_no_revision(hub, processed):
    sample(processed, "40310", T1)
    csv_path = write_csv(hub.root / "distill_results.csv", [])
    summary = _run(hub, processed, csv_path)
    s = one(hub, "40310")
    assert s["status"] == "raw_only" and s["backfill"] == 1
    assert s["current_revision"] is None and revisions(hub, s["id"]) == []
    assert s["cdf_path"] and (hub.data / s["cdf_path"]).exists()
    assert table_counts(hub)["jobs"] == 0
    assert summary["counts"]["orphans"] == 1


def test_result_only_rows_become_final_samples_without_a_cdf(hub, processed):
    processed.mkdir(parents=True)
    r1 = row("GAS test1", "2026-06-29 09:48:42", source=src("GAS test1_06292026_094842.CDF"))
    r2 = row("GAS test1", "2026-06-29 09:48:42", source=src("GAS test1_06292026_094842.CDF"),
             ibp="205.45")
    # a whole-minute time v1 could have misparsed from another stamp
    r3 = row("40401", "2026-09-25 02:45:00", source=src("40401_09252026_002450.CDF"))
    csv_path = write_csv(hub.root / "distill_results.csv", [r1, r2, r3])

    summary = _run(hub, processed, csv_path)

    s = one(hub, "GAS test1")
    assert s["status"] == "final" and s["backfill"] == 1
    assert s["legacy_unverified"] == 1
    assert s["cdf_sha256"] is None and s["cdf_path"] is None
    assert s["method_name"] is None
    assert s["injection_dt"] == "2026-06-29 09:48:42" and s["injection_dt_source"] == "csv"
    assert s["time_unverifiable"] == 0
    revs = revisions(hub, s["id"])
    assert [results(r) for r in revs] == [r1, r2]
    assert all(r["cdf_sha256"] is None and r["cdf_path"] is None for r in revs)
    assert s["current_revision"] == 2
    u = one(hub, "40401")
    assert u["time_unverifiable"] == 1 and u["legacy_unverified"] == 1
    assert summary["counts"]["result_only"] == 2
    assert summary["counts"]["time_unverifiable"] == 1
    assert table_counts(hub)["export_rows"] == 0


def test_v1_misparsed_time_matches_on_the_legacy_string_and_stores_the_correct_time(hub, processed):
    t = datetime(2026, 9, 25, 0, 24, 50)          # v1 on 3.11+ wrote 02:45:00
    a = sample(processed, "40305", t)
    r = row("40305", "2026-09-25 02:45:00", source=src(a.name))
    csv_path = write_csv(hub.root / "distill_results.csv", [r])
    summary = _run(hub, processed, csv_path)
    s = one(hub, "40305")
    assert s["injection_dt"] == "2026-09-25 00:24:50"
    assert s["legacy_injection_dt"] == "2026-09-25 02:45:00"
    assert s["time_corrected"] == 1
    assert s["status"] == "final" and s["cdf_path"]
    assert results(revisions(hub, s["id"])[0])["InjectionDateTime"] == "2026-09-25 02:45:00"
    assert summary["counts"]["time_corrected"] == 1


def test_stampless_cdf_is_stored_at_the_whole_second_of_its_mtime(hub, processed):
    a = sample(processed, "40320", T1, raw_stamp="")
    os.utime(a, (1_758_100_000.654321, 1_758_100_000.654321))
    legacy = datetime.fromtimestamp(a.stat().st_mtime).isoformat(sep=" ")   # what v1 wrote
    r = row("40320", legacy, source=src(a.name))
    csv_path = write_csv(hub.root / "distill_results.csv", [r])
    _run(hub, processed, csv_path)
    s = one(hub, "40320")
    whole = datetime.fromtimestamp(1_758_100_000).isoformat(sep=" ")
    assert s["injection_dt"] == whole
    assert s["injection_dt_source"] == "mtime"
    assert s["legacy_injection_dt"] == legacy and "." in legacy
    assert s["time_corrected"] == 1
    assert s["status"] == "final"
    assert len(revisions(hub, s["id"])) == 1


def test_method_rule_other_and_review_method_keep_their_rows(hub, processed):
    a = sample(processed, "GAS7", T1, method="D7096GAS.M")
    b = sample(processed, "NOMETH", T2, method=None)
    c = sample(processed, "ORPH", T3, method="D7096GAS.M")
    csv_path = write_csv(hub.root / "distill_results.csv", [
        row("GAS7", _iso(T1), source=src(a.name)), row("NOMETH", _iso(T2), source=src(b.name))])
    summary = _run(hub, processed, csv_path)
    ga = one(hub, "GAS7")
    assert ga["status"] == "other_method" and ga["method_name"] == "D7096GAS.M"
    assert len(revisions(hub, ga["id"])) == 1 and ga["current_revision"] == 1
    nm = one(hub, "NOMETH")
    assert nm["status"] == "review_method" and nm["method_name"] == ""
    assert len(revisions(hub, nm["id"])) == 1
    assert one(hub, "ORPH")["status"] == "other_method"
    assert summary["counts"]["other_method"] == 2
    assert summary["counts"]["review_method"] == 1
    assert table_counts(hub)["jobs"] == 0


def test_a_genuine_blank_is_marked_is_blank(hub, processed):
    b = blank(processed, datetime(2026, 9, 24, 15, 30, 27))
    csv_path = write_csv(hub.root / "distill_results.csv",
                         [row("Blank", "2026-09-24 15:30:27", source=src(b.name))])
    _run(hub, processed, csv_path)
    s = one(hub, "Blank")
    assert s["is_blank"] == 1 and s["status"] == "final"
    # a live sample injected later finds it as its blank
    assert store.samples.latest_blank("gc1", "2026-09-25 00:00:00", [SIMDIS], db=hub.db)["id"] == s["id"]


def test_results_csv_none_imports_cdfs_only(hub, processed):
    sample(processed, "40330", T1)
    summary = _run(hub, processed, None)
    assert one(hub, "40330")["status"] == "raw_only"
    assert summary["counts"]["orphans"] == 1


def test_unknown_instrument_is_refused(hub, processed):
    processed.mkdir(parents=True)
    import pipeline
    with pytest.raises(pipeline.UnknownInstrument):
        import_history("nope", processed, None, instrument_folder_aliases=ALIASES, db=hub.db,
                       data_dir=hub.data, conf=hub.conf)
