"""v5.1.0: a Lab ID that LEM will misread is flagged, never changed.

The hub's results-CSV line quotes the Lab ID properly, but LEM's parser
(``split_cells`` in lem_station_module.py) splits each line on the delimiter
and on ``str.splitlines()`` without honouring quotes. So a comma in the Lab ID
shifts every later column, and a line break can make a fake row. When an
export row is written for such a Lab ID the hub keeps the row byte-for-byte,
marks the sample for review (``samples.review_note``) and raises one
notification per sample, however often the sample is exported again.
"""
from __future__ import annotations

from pipeline_helpers import file_corrections, hub  # noqa: F401  (hub: the fixture)

from datetime import datetime

import pytest

import exports
import pipeline
import store


class Notes:
    def __init__(self):
        self.calls = []

    def __call__(self, level, message):
        self.calls.append((level, message))


def _rows(hub, sid):
    return [r for r in store.export_rows.rows_after("gc1", 0, limit=100000, db=hub.db)
            if r["sample_id"] == sid]


def _final(hub, name, notes=None, **kw):
    sid = hub.submit(hub.cdf(name=name, **kw)).sample_id
    hub.worker(notifier=notes).run_until_idle()
    assert hub.sample(sid)["status"] == "final"
    return sid


# ── the rule (pure) ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("ch", ["\r", "\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85",
                                " ", " "])
def test_every_splitlines_boundary_is_caught(ch):
    assert ("a" + ch + "b").splitlines() == ["a", "b"]          # the premise
    assert exports.lem_misread_chars("40304" + ch + "x") == [ch]


@pytest.mark.parametrize("name", ["40304", "40304 rerun", "Sample \"A\" 2", "40311/A:B*?",
                                  "Échantillon µ-7", "a\tb", "", None])
def test_ordinary_names_are_not_caught(name):
    assert exports.lem_misread_chars(name) == []
    assert pipeline.lem_lab_id_note(name) is None


def test_the_characters_are_listed_once_in_order():
    assert exports.lem_misread_chars("a\nb,c,d\r\ne") == ["\n", ",", "\r"]


def test_the_comma_note_names_the_problem_and_the_fix():
    assert pipeline.lem_lab_id_note("40304, rerun") == (
        "Lab ID '40304, rerun' contains a comma; LEM will read this row's values one column "
        "off. Rename the sample in QBench/LEM by hand.")


def test_two_commas_are_two_columns_off():
    assert pipeline.lem_lab_id_note("a,b,c") == (
        "Lab ID 'a,b,c' contains 2 commas; LEM will read this row's values two columns "
        "off. Rename the sample in QBench/LEM by hand.")


def test_a_line_break_is_shown_escaped_never_raw():
    note = pipeline.lem_lab_id_note("40304\nrerun")
    assert note == (
        "Lab ID '40304\\nrerun' contains a line break (\\n); LEM will split this row at the "
        "line break, read its values out of place and may record a fake row. Rename the "
        "sample in QBench/LEM by hand.")
    assert not any(ch in note for ch in "\r\n\v\f\x1c\x1d\x1e\x85  ")
    assert note.splitlines() == [note]


def test_a_comma_and_line_breaks_together():
    note = pipeline.lem_lab_id_note("a,b\r\nc ")
    assert note == (
        "Lab ID 'a,b\\r\\nc\\u2028' contains a comma and line breaks (\\r, \\n, \\u2028); "
        "LEM will read this row's values out of place and may record a fake row. Rename the "
        "sample in QBench/LEM by hand.")


def test_a_very_long_name_is_shortened_in_the_note():
    note = pipeline.lem_lab_id_note("x," + "y" * 500)
    assert len(note) < 300
    assert "…" in note


def test_the_lab_id_is_read_from_the_written_line():
    line = exports.format_line({"Lab ID": "40304, rerun\nx"}, "cdf/a.CDF")
    assert exports.line_lab_id(line) == "40304, rerun\nx"
    assert exports.line_lab_id("garbage") is None
    assert exports.line_lab_id("") is None


# ── processing: flagged, notified, the row unchanged ───────────────────────

def test_a_comma_lab_id_is_flagged_and_notified_and_the_row_is_unchanged(hub):
    hub.gc1()
    notes = Notes()
    sid = _final(hub, "40304, rerun", notes)
    s = hub.sample(sid)
    (row,) = _rows(hub, sid)
    rev = store.get_revision(sid, db=hub.db)
    # byte-for-byte the line the hub always wrote: quoted, nothing sanitised
    assert row["line"] == exports.format_line(rev["results"], s["cdf_path"])
    assert row["line"].startswith('"40304, rerun",')
    assert s["review_note"] == pipeline.lem_lab_id_note("40304, rerun")
    assert notes.calls == [("warning",
                            f"Sample {sid} on GC-1: Lab ID '40304, rerun' contains a comma; LEM "
                            f"will read this row's values one column off. Rename the sample in "
                            f"QBench/LEM by hand.")]


def test_the_exported_name_is_checked_not_the_stripped_lab_id(hub):
    hub.gc1()
    notes = Notes()
    sid = _final(hub, "40304\n", notes)
    assert hub.sample(sid)["lab_id"] == "40304"               # stripped inside the hub
    (row,) = _rows(hub, sid)
    assert row["line"].startswith('"40304\n",')               # v1's name, kept verbatim
    assert "\\n" in hub.sample(sid)["review_note"]
    assert len(notes.calls) == 1


def test_an_ordinary_lab_id_is_neither_flagged_nor_notified(hub):
    hub.gc1()
    notes = Notes()
    sid = _final(hub, "40304 rerun", notes)
    assert hub.sample(sid)["review_note"] is None
    assert notes.calls == []


def test_reprocess_and_export_to_lims_notify_once_per_sample(hub):
    hub.gc1()
    notes = Notes()
    sid = _final(hub, "40304, rerun", notes)
    note = hub.sample(sid)["review_note"]
    pipeline.request_reprocess(sid, by="ryan", db=hub.db)
    hub.worker(notifier=notes).run_until_idle()
    pipeline.request_reprocess(sid, by="ryan", use_current_blank=True, db=hub.db)
    hub.worker(notifier=notes).run_until_idle()
    pipeline.export_to_lims(sid, by="ryan", db=hub.db, data_dir=hub.data, notifier=notes)
    rows = _rows(hub, sid)
    assert len(rows) == 4
    assert len({r["line"] for r in rows}) == 1              # every row the same bytes
    assert len(notes.calls) == 1
    assert hub.sample(sid)["review_note"] == note           # kept, not doubled


def test_a_fresh_blank_reprocess_clears_a_late_blank_note_but_keeps_the_lem_note(hub):
    hub.gc1()
    notes = Notes()
    sid = _final(hub, "40304, rerun", notes, injected=datetime(2026, 9, 25, 14, 0, 0))
    lem = pipeline.lem_lab_id_note("40304, rerun")
    # a blank injected before it arrives late: the late-blank note is added
    hub.submit(hub.cdf("blank", injected=datetime(2026, 9, 25, 13, 0, 0)), notifier=notes)
    s = hub.sample(sid)
    assert "earlier-injected blank arrived after processing" in s["review_note"]
    assert lem in s["review_note"]
    pipeline.request_reprocess(sid, by="ryan", use_current_blank=True, db=hub.db)
    hub.worker(notifier=notes).run_until_idle()
    assert hub.sample(sid)["review_note"] == lem
    assert sum("LEM" in m for _, m in notes.calls) == 1


def test_export_to_lims_flags_a_sample_exported_before_v5_1(hub):
    # A row written by v5.0 was never flagged, and the upgrade doesn't go
    # back over old rows; the sample's next export is checked like any other.
    hub.gc1()
    sid = _final(hub, "40304, rerun")
    with store.connection(hub.db) as conn:
        with store.write_txn(conn):
            store.samples.update(sid, review_note=None, db=conn)      # as v5.0 left it
    notes = Notes()
    pipeline.export_to_lims(sid, by="ryan", db=hub.db, data_dir=hub.data, notifier=notes)
    assert hub.sample(sid)["review_note"] == pipeline.lem_lab_id_note("40304, rerun")
    assert len(notes.calls) == 1


def test_releasing_a_backfill_sample_is_when_it_is_flagged(hub):
    hub.gc1(live_since=datetime(2026, 10, 1))
    notes = Notes()
    sid = _final(hub, "40304, rerun", notes)
    assert _rows(hub, sid) == []                             # backfill: not exported yet
    assert hub.sample(sid)["review_note"] is None
    assert notes.calls == []
    pipeline.release_backfill(sid, by="ryan", db=hub.db, data_dir=hub.data, notifier=notes)
    assert hub.sample(sid)["review_note"] == pipeline.lem_lab_id_note("40304, rerun")
    assert len(notes.calls) == 1


def test_without_a_notifier_the_export_still_succeeds_and_flags(hub):
    hub.gc1()
    sid = _final(hub, "a,b")
    pipeline.export_to_lims(sid, by="ryan", db=hub.db, data_dir=hub.data)
    assert hub.sample(sid)["review_note"] == pipeline.lem_lab_id_note("a,b")


def test_a_failing_notifier_never_undoes_the_export(hub):
    hub.gc1()

    def broken(level, message):
        raise RuntimeError("tray full")

    sid = _final(hub, "a,b", broken)
    assert len(_rows(hub, sid)) == 1
    assert hub.sample(sid)["review_note"]
