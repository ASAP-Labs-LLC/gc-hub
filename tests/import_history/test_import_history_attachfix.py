"""Attach fixes after the 2D re-review: never attach a CDF to a result-only
sample whose time is unverifiable and differs from the CDF's (P13/P13b), a
review note for an unmapped method on live attach (P15), the reason on a
conflict against a result-only sample (P16), and the last run's CSV path."""
from __future__ import annotations

from import_history_testlib import (  # noqa: F401  (hub: the fixture)
    ALIASES, by_lab, hub, one, results, revisions, row, sample, samples, src, table_counts,
    write_csv)

from datetime import datetime

import pytest

import pipeline
import store
from jobs.import_history import import_history, last_run

T = datetime(2026, 9, 17, 14, 50, 18)
TM = datetime(2026, 9, 25, 0, 24, 50)          # v1 on 3.11+ wrote 02:45:00
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


def _lost_cdf_history(hub, processed):
    """Result-only X at 02:45:00 (its own CDF, stamp 024500, is lost)."""
    csv_path = write_csv(hub.root / "r.csv",
                         [row("X", TM_V1, source=src("X_09252026_024500.CDF"), ibp="777.7")])
    _run(hub, processed, csv_path)
    ro = one(hub, "X")
    assert ro["time_unverifiable"] == 1
    return csv_path, ro


def test_P13_live_submit_never_attaches_to_an_unverifiable_time(hub, processed):
    _csv, ro = _lost_cdf_history(hub, processed)
    notes = []

    res = hub.submit(sample(hub.root / "live", "X", TM), notifier=lambda lvl, msg: notes.append(msg))

    assert res.outcome == "created" and res.sample_id != ro["id"]
    kept = hub.sample(ro["id"])
    assert kept["cdf_sha256"] is None and kept["legacy_unverified"] == 1
    assert kept["time_unverifiable"] == 1 and kept["injection_dt"] == TM_V1
    new = hub.sample(res.sample_id)
    assert new["injection_dt"] == _iso(TM) and new["status"] == "received"
    assert f"possible match with result-only sample {ro['id']}" in new["review_note"]
    assert "time unverifiable" in new["review_note"]
    assert f"sample {res.sample_id}" in kept["review_note"]
    assert "time unverifiable" in kept["review_note"]
    assert len(notes) == 1 and str(ro["id"]) in notes[0]
    assert [x["reason"] for x in revisions(hub, ro["id"])] == ["import"]


def test_P13b_importer_lists_it_and_does_not_attach(hub, processed):
    csv_path, ro = _lost_cdf_history(hub, processed)
    sample(processed, "X", TM)            # the other injection appears in the folder

    summary = _run(hub, processed, csv_path)

    assert [s["id"] for s in by_lab(hub)["X"]] == [ro["id"]]
    kept = hub.sample(ro["id"])
    assert kept["cdf_sha256"] is None and kept["time_unverifiable"] == 1
    assert summary["counts"]["attach_ambiguous_time"] == 1
    ex = summary["examples"]["attach_ambiguous_time"][0]
    assert ex["sample_id"] == ro["id"]
    assert summary["counts"]["attached_to_result_only"] == 0


def test_an_unverifiable_time_equal_to_the_correct_time_still_attaches(hub, processed):
    t = datetime(2026, 9, 25, 2, 45, 0)      # a genuine whole-minute injection
    csv_path = write_csv(hub.root / "r.csv", [row("W", _iso(t), source=src("W.CDF"))])
    _run(hub, processed, csv_path)
    ro = one(hub, "W")
    assert ro["time_unverifiable"] == 1
    res = hub.submit(sample(hub.root / "live", "W", t))
    assert res.sample_id == ro["id"]
    assert hub.sample(ro["id"])["legacy_unverified"] == 0


def test_P16_the_verifiable_candidate_wins_over_an_unverifiable_one(hub, processed):
    csv_path = write_csv(hub.root / "r.csv", [row("Z", _iso(TM)), row("Z", TM_V1)])
    _run(hub, processed, csv_path)
    correct = [s for s in by_lab(hub)["Z"] if s["injection_dt"] == _iso(TM)][0]
    res = hub.submit(sample(hub.root / "live", "Z", TM))
    assert res.sample_id == correct["id"]
    assert store.conflicts.list(db=hub.db) == []


def test_P16_a_conflict_against_a_result_only_sample_says_why_replace_is_impossible(hub, processed):
    # two result-only candidates that both look verifiable: nothing is attached,
    # and the key at the correct time makes the file a conflict
    for dt in (_iso(TM), TM_V1):
        sid = store.samples.insert_received("gc1", "Z", dt, "csv", cdf_sha256=None, cdf_path=None,
                                            backfill=1, status="final", legacy_unverified=1,
                                            db=hub.db)
    res = hub.submit(sample(hub.root / "live", "Z", TM))
    assert res.outcome == "conflict"
    c = store.conflicts.get(res.conflict_id, db=hub.db)
    assert "Replace not possible" in c["error"] and "no CDF" in c["error"]
    assert "Keep existing" in c["error"]


def test_P15_live_attach_of_an_unmapped_method_keeps_status_and_notes_it(hub, processed):
    csv_path = write_csv(hub.root / "r.csv", [row("Y", _iso(T))])
    _run(hub, processed, csv_path)
    ro = one(hub, "Y")
    res = hub.submit(sample(hub.root / "live", "Y", T, method="D7096.M"))
    s = hub.sample(ro["id"])
    assert res.sample_id == ro["id"]
    assert s["status"] == "final" and s["method_name"] == "D7096.M"
    assert s["review_note"] == "CDF method D7096.M not mapped for this instrument"
    assert [x["reason"] for x in revisions(hub, ro["id"])] == ["import"]
    assert table_counts(hub)["jobs"] == 0


def test_mapped_live_attach_has_no_review_note(hub, processed):
    csv_path = write_csv(hub.root / "r.csv", [row("Y", _iso(T))])
    _run(hub, processed, csv_path)
    hub.submit(sample(hub.root / "live", "Y", T))
    assert one(hub, "Y")["review_note"] is None


def test_last_run_gives_the_previous_csv_path_and_the_summary_warns_on_a_new_one(hub, processed):
    assert last_run("gc1", db=hub.db) is None
    csv_path = write_csv(hub.root / "r.csv", [row("Q", _iso(T))])
    first = _run(hub, processed, csv_path)
    lr = last_run("gc1", db=hub.db)
    assert lr["results_csv"] == str(csv_path) and lr["run_id"] == first["run_id"]
    assert lr["processed_dir"] == str(processed) and lr["finished_at"]
    assert first["previous_csv"] is None
    other = write_csv(hub.root / "copy.csv", [row("Q", _iso(T))])
    second = _run(hub, processed, other)
    assert second["previous_csv"] == str(csv_path)
    assert any("different CSV" in w for w in second["warnings"])
    assert last_run("gc1", db=hub.db)["results_csv"] == str(other)
    assert last_run("gc2", db=hub.db) is None
