"""History import (plan 2D), task 4: idempotent re-runs, CSV deltas, batches,
stopping and resuming, per-sample failures."""
from __future__ import annotations

from import_history_testlib import (  # noqa: F401  (hub: the fixture)
    ALIASES, append_csv, by_lab, db_dump, hub, one, results, revisions, row, sample, src,
    table_counts, tree, write_csv)

from datetime import datetime

import pytest

import store
import jobs.import_history as ih
from jobs.import_history import import_history

T = [datetime(2026, 9, 17, 14, 50, 18), datetime(2026, 9, 18, 15, 33, 41),
     datetime(2026, 9, 19, 16, 43, 5), datetime(2026, 9, 20, 17, 53, 17),
     datetime(2026, 9, 21, 18, 55, 29)]


def _run(hub, processed, csv_path, **kw):
    kw.setdefault("conf", hub.conf)
    return import_history("gc1", processed, csv_path, instrument_folder_aliases=ALIASES,
                          db=hub.db, data_dir=hub.data, **kw)


def _iso(d):
    return d.isoformat(sep=" ")


@pytest.fixture()
def processed(hub):
    hub.gc1()
    return hub.root / "robocopy" / "processed_cdfs2"


def _history(hub, processed):
    """Two attached, one orphan, one result-only, one key collision."""
    a = sample(processed, "A1", T[0])
    b = sample(processed, "B2", T[1])
    sample(processed, "C3", T[2])
    sample(processed, "B2", T[1], shift=0.2, file_name="B2 copy.CDF")
    rows = [row("A1", _iso(T[0]), source=src(a.name)),
            row("B2", _iso(T[1]), source=src(b.name)),
            row("A1", _iso(T[0]), source=src(a.name), ibp="99.9"),
            row("R4", _iso(T[3]), source=src("R4_09202026_175317.CDF"))]
    return write_csv(hub.root / "distill_results.csv", rows)


def test_a_second_run_over_the_same_inputs_writes_nothing(hub, processed):
    csv_path = _history(hub, processed)
    first = _run(hub, processed, csv_path)
    assert first["counts"]["imported"] == 4 and first["counts"]["conflicts"] == 1
    before_db, before_files = db_dump(hub), tree(hub.data)

    second = _run(hub, processed, csv_path)

    assert db_dump(hub) == before_db
    assert tree(hub.data) == before_files
    c = second["counts"]
    assert c["imported"] == 0 and c["revisions"] == 0 and c["conflicts"] == 0
    assert c["already_imported"] == 4
    assert c["conflicts_existing"] == 1
    assert not list((hub.data / "cdf" / ".incoming").glob("*"))


def test_rows_appended_since_the_last_run_become_new_import_revisions(hub, processed):
    csv_path = _history(hub, processed)
    _run(hub, processed, csv_path)
    a = one(hub, "A1")
    extra_a = row("A1", _iso(T[0]), source=src("A1_09172026_145018.CDF"), ibp="100.5")
    extra_c = row("C3", _iso(T[2]), source=src("C3_09192026_164305.CDF"))
    extra_r = row("R4", _iso(T[3]), source=src("R4_09202026_175317.CDF"), ibp="7.0")
    append_csv(csv_path, [extra_a, extra_c, extra_r])

    summary = _run(hub, processed, csv_path)

    revs = revisions(hub, a["id"])
    assert len(revs) == 3 and results(revs[-1]) == extra_a
    assert one(hub, "A1")["current_revision"] == 3
    c3 = one(hub, "C3")                                  # the orphan gained a row
    assert c3["status"] == "final" and results(revisions(hub, c3["id"])[0]) == extra_c
    r4 = one(hub, "R4")
    assert [results(r)["2887 IBP"] for r in revisions(hub, r4["id"])] == ["251.63", "7.0"]
    assert summary["counts"]["delta_revisions"] == 3
    assert summary["counts"]["revisions"] == 3
    assert summary["counts"]["imported"] == 0
    assert table_counts(hub)["export_rows"] == 0 and table_counts(hub)["jobs"] == 0


def test_no_delta_after_the_hub_has_revised_the_sample(hub, processed):
    csv_path = _history(hub, processed)
    _run(hub, processed, csv_path)
    a = one(hub, "A1")
    with store.connection(hub.db) as conn:
        with store.write_txn(conn):
            store.add_revision(conn, a["id"], {"Lab ID": "A1"}, reason="reprocess")
    append_csv(csv_path, [row("A1", _iso(T[0]), source=src("A1_09172026_145018.CDF"))])

    summary = _run(hub, processed, csv_path)

    assert len(revisions(hub, a["id"])) == 3
    assert summary["counts"]["delta_refused"] == 1
    assert summary["examples"]["delta_refused"][0]["lab_id"] == "A1"


def test_changed_rows_are_reported_not_rewritten(hub, processed):
    a = sample(processed, "A1", T[0])
    csv_path = write_csv(hub.root / "r.csv", [row("A1", _iso(T[0]), source=src(a.name))])
    _run(hub, processed, csv_path)
    write_csv(csv_path, [row("A1", _iso(T[0]), source=src(a.name), ibp="1.0")])
    summary = _run(hub, processed, csv_path)
    assert summary["counts"]["rows_changed"] == 1
    assert results(revisions(hub, one(hub, "A1")["id"])[0])["2887 IBP"] == "251.63"


def test_v1_rows_never_attach_to_a_sample_the_hub_received_itself(hub, processed):
    a = sample(processed, "A1", T[0])
    live = hub.submit(a)
    assert live.outcome == "created"
    csv_path = write_csv(hub.root / "r.csv", [row("A1", _iso(T[0]), source=src(a.name))])
    summary = _run(hub, processed, csv_path)
    assert revisions(hub, live.sample_id) == []
    assert summary["counts"]["present_not_imported"] == 1
    assert summary["counts"]["rows_not_imported"] == 1


def test_a_result_only_row_matching_a_held_sample_is_not_duplicated(hub, processed):
    t = datetime(2026, 9, 25, 0, 24, 50)            # v1 wrote 02:45:00
    live = hub.submit(sample(hub.src, "L5", t))
    processed.mkdir(parents=True)
    csv_path = write_csv(hub.root / "r.csv",
                         [row("L5", "2026-09-25 02:45:00", source=src("L5_x.CDF"))])
    summary = _run(hub, processed, csv_path)
    assert [s["id"] for s in by_lab(hub)["L5"]] == [live.sample_id]
    assert summary["counts"]["key_taken"] == 1


def test_batches_commit_separately(hub, processed):
    csv_path = _history(hub, processed)
    events = []
    _run(hub, processed, csv_path, batch_size=2, progress=events.append)
    commits = [e for e in events if e["phase"] == "commit"]
    assert [e["done"] for e in commits] == [2, 4, 5]
    assert events[0] == {"phase": "scan"}
    assert events[-1]["phase"] == "done" and events[-1]["summary"]["counts"]["imported"] == 4
    assert any(e["phase"] == "match" for e in events)
    assert any(e["phase"] == "check" for e in events)


def test_a_stop_keeps_committed_batches_and_a_rerun_completes(hub, processed):
    csv_path = _history(hub, processed)
    seen = []

    def stop_on_third(event):
        if event["phase"] == "import":
            seen.append(event)
            if len(seen) == 3:
                raise KeyboardInterrupt("paused by the admin")

    with pytest.raises(KeyboardInterrupt) as ei:
        _run(hub, processed, csv_path, batch_size=2, progress=stop_on_third)
    partial = ei.value.import_summary
    assert "paused by the admin" in partial["stopped"]
    assert partial["counts"]["imported"] == 2                 # the first batch only
    assert len(store.samples.search(instrument="gc1", limit=100, db=hub.db)) == 2
    stored = {p.name for p in (hub.data / "cdf").rglob("*.CDF")}
    held = {s["cdf_path"].rsplit("/", 1)[-1] for s in store.samples.search(
        instrument="gc1", limit=100, db=hub.db) if s["cdf_path"]}
    assert stored == held                                      # the rolled-back batch left no file

    summary = _run(hub, processed, csv_path)
    assert summary["counts"]["imported"] == 2
    assert summary["counts"]["already_imported"] == 2
    assert summary["counts"]["conflicts"] == 1
    assert set(by_lab(hub)) == {"A1", "B2", "C3", "R4"}


def test_one_failing_sample_is_recorded_and_the_rest_are_imported(hub, processed, monkeypatch):
    csv_path = _history(hub, processed)
    real = ih._stage

    def flaky(src_path, sha, data_dir):
        if src_path.name.startswith("B2_"):
            raise OSError("share went away")
        return real(src_path, sha, data_dir)

    monkeypatch.setattr(ih, "_stage", flaky)
    summary = _run(hub, processed, csv_path)
    assert summary["counts"]["failed"] >= 1
    assert "share went away" in summary["examples"]["failed"][0]["error"]
    assert {"A1", "C3", "R4"} <= set(by_lab(hub))
    assert "B2" not in by_lab(hub) or by_lab(hub)["B2"][0]["source_name"] == "B2 copy.CDF"

    monkeypatch.setattr(ih, "_stage", real)
    again = _run(hub, processed, csv_path)
    assert one(hub, "B2")["source_name"] in ("B2_09182026_153341.CDF", "B2 copy.CDF")
    assert again["counts"]["failed"] == 0
