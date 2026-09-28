"""History import (plan 2D), task 3: conflicts, cross-instrument files,
truncated CDFs, and the row classes that are listed but never imported."""
from __future__ import annotations

from import_history_testlib import (  # noqa: F401  (hub: the fixture)
    ALIASES, OTHER_SHARE, by_lab, hub, one, revisions, row, sample, src, table_counts,
    write_csv)

import csv
from datetime import datetime

import pytest

import cdf_fixtures as fx
import store
from jobs.import_history import import_history

T1 = datetime(2026, 9, 17, 14, 50, 18)
T2 = datetime(2026, 9, 18, 15, 33, 41)


def _run(hub, processed, csv_path, instrument="gc1", **kw):
    kw.setdefault("conf", hub.conf)
    return import_history(instrument, processed, csv_path, instrument_folder_aliases=ALIASES,
                          db=hub.db, data_dir=hub.data, **kw)


def _iso(d):
    return d.isoformat(sep=" ")


@pytest.fixture()
def processed(hub):
    hub.gc1()
    return hub.root / "robocopy" / "processed_cdfs2"


def test_key_collision_keeps_one_and_stores_the_other_as_a_conflict(hub, processed):
    a = sample(processed, "40350", T1)                       # 40350_09172026_145018.CDF
    b = sample(processed, "40350", T1, shift=0.2, file_name="40350_09172026_145018 (2).CDF")
    csv_path = write_csv(hub.root / "r.csv", [row("40350", _iso(T1), source=src(a.name))])

    summary = _run(hub, processed, csv_path)

    s = one(hub, "40350")
    assert s["source_name"] == a.name                  # the file the row names is kept
    assert len(revisions(hub, s["id"])) == 1
    cs = store.conflicts.list("gc1", db=hub.db)
    assert len(cs) == 1
    c = cs[0]
    assert c["existing_sample_id"] == s["id"]
    assert c["lab_id"] == "40350" and c["injection_dt"] == _iso(T1)
    assert c["cdf_path"].startswith("cdf/gc1/conflicts/2026/09/")
    assert (hub.data / c["cdf_path"]).read_bytes() == b.read_bytes()
    assert summary["counts"]["key_collisions"] == 1
    assert summary["counts"]["conflicts"] == 1
    assert summary["examples"]["conflicts"][0]["cdf"] == str(b)


def test_a_key_already_held_by_another_file_is_a_conflict_and_its_rows_are_not_imported(hub, processed):
    live = hub.submit(sample(hub.src, "40351", T1, shift=0.3))
    assert live.outcome == "created"
    a = sample(processed, "40351", T1)
    csv_path = write_csv(hub.root / "r.csv", [row("40351", _iso(T1), source=src(a.name))])

    summary = _run(hub, processed, csv_path)

    s = one(hub, "40351")
    assert s["id"] == live.sample_id and revisions(hub, s["id"]) == []
    c = store.conflicts.list("gc1", db=hub.db)
    assert [x["existing_sample_id"] for x in c] == [live.sample_id]
    assert (hub.data / c[0]["cdf_path"]).read_bytes() == a.read_bytes()
    assert summary["counts"]["conflicts"] == 1
    assert summary["counts"]["rows_not_imported"] == 1


def test_a_file_held_by_another_instrument_is_reported_not_imported(hub, processed):
    hub.gc2()
    a = sample(processed, "40352", T1)
    other = hub.submit(a, instrument="gc2")
    assert other.outcome == "created"
    csv_path = write_csv(hub.root / "r.csv", [row("40352", _iso(T1), source=src(a.name))])

    summary = _run(hub, processed, csv_path)

    assert "40352" not in by_lab(hub, "gc1")
    assert summary["counts"]["cross_instrument"] == 1
    ex = summary["examples"]["cross_instrument"][0]
    assert ex["instrument_id"] == "gc2" and ex["sample_id"] == other.sample_id
    assert summary["counts"]["imported"] == 0


def test_truncated_cdf_is_not_imported_nor_its_rows(hub, processed):
    good = sample(hub.src, "40353", T1)
    processed.mkdir(parents=True)
    bad = fx.truncated_copy(good, processed / good.name)
    csv_path = write_csv(hub.root / "r.csv", [row("40353", _iso(T1), source=src(bad.name))])

    summary = _run(hub, processed, csv_path)

    assert "40353" not in by_lab(hub)
    assert summary["counts"]["truncated"] == 1
    assert summary["counts"]["rows_not_imported"] == 1
    assert "truncated" in summary["examples"]["truncated"][0]["problem"]


def test_ambiguous_rows_are_listed_and_not_imported(hub, processed):
    # A: stamped 02:45:00 (read correctly). B: stamped 00:24:50, which v1 on
    # Python 3.11+ wrote as 02:45:00. One CSV key fits both CDFs.
    a = sample(processed, "40354", datetime(2026, 9, 25, 2, 45, 0))
    b = sample(processed, "40354", datetime(2026, 9, 25, 0, 24, 50))
    csv_path = write_csv(hub.root / "r.csv",
                         [row("40354", "2026-09-25 02:45:00", source=src(a.name))])

    summary = _run(hub, processed, csv_path)

    got = by_lab(hub)["40354"]
    assert len(got) == 2
    assert all(s["status"] == "raw_only" and s["current_revision"] is None for s in got)
    assert summary["counts"]["ambiguous_rows"] == 1
    ex = summary["examples"]["ambiguous_rows"][0]
    assert ex["line_no"] == 2 and ex["chosen"] == str(a) and ex["others"] == [str(b)]


def test_held_mixed_and_keyless_rows_are_listed_and_not_imported(hub, processed):
    a = sample(processed, "40355", T1)
    csv_path = hub.root / "r.csv"
    good = row("40355", _iso(T1), source=src(a.name))
    mixed = row("40356", _iso(T2), source=f"{OTHER_SHARE}\\40356_09182026_153341.CDF")
    keyless = row("", _iso(T2), source=src("x.CDF"))
    write_csv(csv_path, [good, mixed, keyless])
    with open(csv_path, "a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow(["40357", _iso(T2), "1.0"])          # a short record

    summary = _run(hub, processed, csv_path)

    assert set(by_lab(hub)) == {"40355"}
    c = summary["counts"]
    assert (c["held_rows"], c["mixed_rows"], c["unmatched_without_key"]) == (1, 1, 1)
    assert summary["examples"]["held_rows"][0]["line_no"] == 5
    assert summary["examples"]["mixed_rows"][0]["lab_id"] == "40356"
    assert summary["examples"]["unmatched_without_key"][0]["line_no"] == 4
    assert table_counts(hub)["sample_results"] == 1
