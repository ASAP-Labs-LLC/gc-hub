"""v5.1.0: a results-CSV line that LEM will read differently from a CSV
reader is flagged, never changed.

The hub's line is correct CSV, but LEM (lem_station_module.py) decodes the
text it tails (UTF-8, cp1252 fallback), takes each ``str.splitlines()`` line
as one print and each print's cells as ``line.split(",")``: CSV quotes are
neither honoured nor removed. So a comma shifts the later columns, a line
break makes another row, and any field ``csv.writer`` had to quote (a double
quote, an inch mark) reaches LEM with its quotes. When the hub writes such a
row it keeps it byte for byte, marks the sample for review
(``samples.review_note``) and raises one notification per sample, however
often the sample is exported again.
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


def _note_of(hub, sid):
    return pipeline.lem_line_note(_rows(hub, sid)[-1]["line"])


def _line(lab, best="", source="cdf/gc1/2026/09/x_1.CDF"):
    return exports.format_line({"Lab ID": lab, "2887 IBP": "101.5", "Best Fit": best}, source)


PRE = "LEM splits each line at every comma and line break and keeps CSV quotes, so it will "
LABCORE = (" If that ID (after any Lab ID cleanup set up in LEM) matches no LabCore sample, LEM "
           "holds the reading; if it matches another sample's ID, the reading is filed there.")
FIX_LAB = " Rename the sample in QBench/LEM by hand."
FIX_OTHER = " Check this reading in LEM by hand."
END = " Once LEM is right there is nothing more to do in the hub; this note stays as a record."


# ── LEM's reading of a line (pure) ──────────────────────────────────────────

def test_lem_reads_a_line_as_split_cells_with_the_quotes_kept():
    line = _line('Q"A, b')
    assert exports.lem_reading(line)[0][:3] == ['"Q""A', ' b"', '']
    assert exports.csv_fields(line)[:3] == ['Q"A, b', '', '101.5']
    assert exports.lem_reading(_line("a\nb"))[1][0] == 'b"'
    assert exports.csv_fields("garbage") is None


@pytest.mark.parametrize("ch", ["\r", "\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85",
                                "\u2028", "\u2029"])
def test_every_splitlines_boundary_is_caught(ch):
    assert ("a" + ch + "b").splitlines() == ["a", "b"]          # the premise
    note = pipeline.lem_line_note(_line("40304" + ch + "x"))
    assert note is not None and "line break" in note and exports.LEM_LINE_BREAKS[ch] in note


@pytest.mark.parametrize("name", ["40304", "40304 rerun", "40311/A:B*?", "Échantillon µ-7",
                                  "a\tb", "", " 40304 "])
def test_ordinary_names_are_not_caught(name):
    assert pipeline.lem_line_note(_line(name)) is None
    assert pipeline.lem_line_note(_line("40304", best="Mix: A + B (80/20)")) is None


def test_the_comma_note_says_what_lem_reads_and_what_to_do():
    assert pipeline.lem_line_note(_line("40304, rerun")) == (
        "Lab ID '40304, rerun' contains a comma. " + PRE + "read the Lab ID as '\"40304' and "
        "every value after the Lab ID one column off." + LABCORE + FIX_LAB + END)


def test_two_commas_are_two_columns_off():
    assert pipeline.lem_line_note(_line("a,b,c")) == (
        "Lab ID 'a,b,c' contains 2 commas. " + PRE + "read the Lab ID as '\"a' and every value "
        "after the Lab ID two columns off." + LABCORE + FIX_LAB + END)


def test_a_double_quote_reaches_lem_with_the_csv_quotes():
    assert pipeline.lem_line_note(_line('Q"A')) == (
        "Lab ID 'Q\"A' contains a double quote. " + PRE + "read the Lab ID as '\"Q\"\"A\"'."
        + LABCORE + FIX_LAB + END)


def test_an_inch_mark_reaches_lem_with_the_csv_quotes():
    assert pipeline.lem_line_note(_line('Pipe 2"')) == (
        "Lab ID 'Pipe 2\"' contains a double quote. " + PRE + "read the Lab ID as "
        "'\"Pipe 2\"\"\"'." + LABCORE + FIX_LAB + END)


def test_a_line_break_is_shown_escaped_never_raw():
    note = pipeline.lem_line_note(_line("40304\nrerun"))
    assert note == (
        "Lab ID '40304\\nrerun' contains a line break (\\n). " + PRE + "read the Lab ID as "
        "'\"40304' and see this line as 2 rows; the second starts 'rerun\"', which it may "
        "record as a fake row with that Lab ID." + LABCORE + FIX_LAB + END)
    assert not any(ch in note for ch in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")
    assert note.splitlines() == [note]


def test_an_unquoted_line_boundary_still_splits_the_row():
    # csv.writer quotes only \r and \n; LEM splits at \x85 too
    assert pipeline.lem_line_note(_line("a\x85b")) == (
        "Lab ID 'a\\x85b' contains a line break (\\x85). " + PRE + "read the Lab ID as 'a' and "
        "see this line as 2 rows; the second starts 'b', which it may record as a fake row "
        "with that Lab ID." + LABCORE + FIX_LAB + END)


def test_a_comma_and_line_breaks_together():
    note = pipeline.lem_line_note(_line("a,b\r\nc\u2028d"))
    assert note.startswith("Lab ID 'a,b\\r\\nc\\u2028d' contains a comma and 2 line breaks "
                           "(\\r, \\n, \\u2028). " + PRE + "read the Lab ID as '\"a' and see "
                           "this line as 3 rows; the second starts 'c', ")
    assert note.endswith(FIX_LAB + END)


def test_another_field_is_named_and_the_fix_is_to_check_the_reading():
    assert pipeline.lem_line_note(_line("40304", best="Mix: A, B")) == (
        "Best Fit 'Mix: A, B' contains a comma. " + PRE + "read every value after Best Fit one "
        "column off." + FIX_OTHER + END)


def test_every_field_with_a_problem_is_named():
    note = pipeline.lem_line_note(_line("40304, rerun",
                                        source="cdf/gc1/2026/09/40304, rerun_1.CDF"))
    assert note.startswith("Lab ID '40304, rerun' contains a comma; Source File contains a "
                           "comma. ")


def test_a_very_long_name_is_shortened_in_the_note():
    note = pipeline.lem_line_note(_line("x," + "y" * 500))
    assert len(note) < 700
    assert "…" in note
    assert "y" * 100 not in note


def test_another_field_on_a_real_export_is_flagged_and_notified_once(hub):
    # Best Fit names a comparison standard; one with a comma shifts Fit Score
    # and Source File. Checked where every export row is written.
    hub.gc1()
    sid = _final(hub, "40304")
    line = _line("40304", best="Mix: A, B")
    with store.connection(hub.db) as conn:
        with store.write_txn(conn):
            rev = store.get_revision(sid, db=conn)["revision"]
            seq, msg = pipeline._append_export_row(conn, "gc1", sid, rev, line)
            again = pipeline._append_export_row(conn, "gc1", sid, rev, line)[1]
    assert _rows(hub, sid)[-1]["line"] == line                  # written as given
    assert msg == f"Sample {sid} on GC-1: " + pipeline.lem_line_note(line)
    assert again is None
    assert hub.sample(sid)["review_note"] == pipeline.lem_line_note(line)


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
    assert s["review_note"] == _note_of(hub, sid)
    # the stored file's name carries the comma too (Source File)
    assert notes.calls == [("warning",
                            f"Sample {sid} on GC-1: Lab ID '40304, rerun' contains a comma; Source "
                            f"File contains a comma. " + PRE + "read the Lab ID as '\"40304' and "
                            "every value after the Lab ID one column off." + LABCORE + FIX_LAB
                            + END)]


@pytest.mark.parametrize("name, lem", [('Q"A', '"Q""A"'), ('Pipe 2"', '"Pipe 2"""')])
def test_a_quoted_lab_id_is_flagged_once_and_the_row_is_unchanged(hub, name, lem):
    hub.gc1()
    notes = Notes()
    sid = _final(hub, name, notes)
    s = hub.sample(sid)
    (row,) = _rows(hub, sid)
    rev = store.get_revision(sid, db=hub.db)
    assert row["line"] == exports.format_line(rev["results"], s["cdf_path"])
    assert row["line"].startswith(lem + ",")
    assert f"read the Lab ID as '{lem}'" in s["review_note"]
    assert [lvl for lvl, _ in notes.calls] == ["warning"]
    pipeline.export_to_lims(sid, by="ryan", db=hub.db, data_dir=hub.data, notifier=notes)
    assert len(notes.calls) == 1


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
    lem = _note_of(hub, sid)
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
    assert hub.sample(sid)["review_note"] == _note_of(hub, sid)
    assert len(notes.calls) == 1


def test_releasing_a_backfill_sample_is_when_it_is_flagged(hub):
    hub.gc1(live_since=datetime(2026, 10, 1))
    notes = Notes()
    sid = _final(hub, "40304, rerun", notes)
    assert _rows(hub, sid) == []                             # backfill: not exported yet
    assert hub.sample(sid)["review_note"] is None
    assert notes.calls == []
    pipeline.release_backfill(sid, by="ryan", db=hub.db, data_dir=hub.data, notifier=notes)
    assert hub.sample(sid)["review_note"] == _note_of(hub, sid)
    assert len(notes.calls) == 1


def test_without_a_notifier_the_export_still_succeeds_and_flags(hub):
    hub.gc1()
    sid = _final(hub, "a,b")
    pipeline.export_to_lims(sid, by="ryan", db=hub.db, data_dir=hub.data)
    assert hub.sample(sid)["review_note"] == _note_of(hub, sid)


def test_a_failing_notifier_never_undoes_the_export(hub):
    hub.gc1()

    def broken(level, message):
        raise RuntimeError("tray full")

    sid = _final(hub, "a,b", broken)
    assert len(_rows(hub, sid)) == 1
    assert hub.sample(sid)["review_note"]
