"""History import (plan 2D), task 6: the local share snapshot, when present.

``/Users/rynatical/Projects/gc-share-snapshot-2026-09-25`` holds a copy of the
5570 instance's results CSV (``webapp-live/distill_results.csv``, rows naming
``...\\webapp\\processed_cdfs2``) and a sample of processed CDFs. Skipped
where the snapshot is absent (CI).
"""
from __future__ import annotations

from import_history_testlib import hub, results, revisions, samples, tree  # noqa: F401

import csv
import json
from pathlib import Path

import pytest

import exports
from jobs.import_history import format_summary, import_history

SNAP = Path("/Users/rynatical/Projects/gc-share-snapshot-2026-09-25")
CSV_PATH = SNAP / "webapp-live" / "distill_results.csv"
CDFS = SNAP / "cdf" / "processed-sample"

pytestmark = pytest.mark.skipif(not (CSV_PATH.is_file() and CDFS.is_dir()),
                                reason="local share snapshot not present")


def test_dry_run_over_the_snapshot_is_read_only_and_accounts_for_every_row():
    before = tree(SNAP)
    s = import_history("gc2", CDFS, CSV_PATH, instrument_folder_aliases=["processed_cdfs2"],
                       db=None, data_dir=None, dry_run=True)
    assert tree(SNAP) == before
    c = s["counts"]
    print(format_summary(s, examples=3))
    assert c["csv_rows"] > 0 and c["cdfs_read"] == c["cdf_files"]
    assert c["cdf_errors"] == 0 and c["truncated"] == 0
    assert c["mixed_rows"] == 0                     # the CSV is not mixed
    # every row is imported, or listed in exactly one class
    listed = (c["ambiguous_rows"] + c["held_rows"] + c["mixed_rows"]
              + c["unmatched_without_key"] + c["rows_not_imported"])
    assert c["revisions"] + listed == c["csv_rows"]
    assert set(s["method_names"]) <= {"SIMDISB.M", "SIMDISTB.M"}
    json.dumps(s)


def test_imported_snapshot_rows_reproduce_every_v1_line(hub):
    hub.gc2()
    s = import_history("gc2", CDFS, CSV_PATH, instrument_folder_aliases=["processed_cdfs2"],
                       db=hub.db, data_dir=hub.data, conf=hub.conf)
    assert s["counts"]["failed"] == 0
    raw = CSV_PATH.read_bytes().decode("utf-8").split("\r\n")
    by_line = {}
    for smp in samples(hub, "gc2"):
        for rev in revisions(hub, smp["id"]):
            n = json.loads(rev["notes"])["import"]["line_no"]
            by_line[n] = (smp, rev)
    assert len(by_line) == s["counts"]["revisions"] > 0
    for n, (smp, rev) in by_line.items():
        v1 = raw[n - 1] + "\r\n"
        assert exports.format_line(rev["results"], results(rev)["Source File"]) == v1, n
        hub_cells = next(csv.reader([exports.format_line(rev["results"], smp["cdf_path"])]))
        assert hub_cells[:-1] == next(csv.reader([v1]))[:-1]
    # re-running is a no-op
    again = import_history("gc2", CDFS, CSV_PATH, instrument_folder_aliases=["processed_cdfs2"],
                           db=hub.db, data_dir=hub.data, conf=hub.conf)
    assert again["counts"]["imported"] == 0 and again["counts"]["revisions"] == 0
