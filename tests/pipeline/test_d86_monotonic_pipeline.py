"""v7.0.0 "Corrected D86 never decreases" through the pipeline: a new result
stores (and exports to the results CSV LEM tails) the held corrected series
and the plain uncorrected one; a stored revision from before v7.0.0 is
history and is never rewritten: Export to LIMS re-sends it as stored, and a
Re-process (recorded blank and corrections) recomputes it under the rule."""
from __future__ import annotations

from pipeline_helpers import hub  # noqa: F401  (hub: the fixture)

import csv
import io
import json

import corrections
import distill
import pipeline
import store


class Fixed:
    def __init__(self, values):
        self.values = values

    def get(self, instrument):
        return corrections.Corrections(source="hub", updated_at="2026-10-07T10:00:00+00:00",
                                       values=dict(self.values), updated_by="ryan")


def _run(hub, values):
    return hub.worker(corrections_provider=Fixed(values)).run_until_idle()


def _line_row(line: str) -> dict:
    return dict(zip(distill.CSV_HEADER, next(csv.reader(io.StringIO(line)))))


def _lines(hub, sid):
    return [r["line"] for r in store.export_rows.rows_after("gc1", 0, db=hub.db)
            if r["sample_id"] == sid]


def _dipping_factors(hub):
    """Factors under which this fixture's corrected T10 falls 5 °C below its
    corrected T5 (the uncorrected conversion doesn't dip)."""
    hub.gc1()
    hub.submit(hub.cdf("blank"))          # with the blank this fixture's conversion doesn't dip
    probe = hub.submit(hub.cdf(name="probe")).sample_id
    _run(hub, {c: 0.0 for c in corrections.D86_CUTS})
    unc = json.loads(store.get_revision(probe, db=hub.db)["d86_uncorrected"])
    assert distill.monotonic_d86(unc) == unc
    values = {c: 0.0 for c in corrections.D86_CUTS}
    values["10%"] = round(unc["5%"] - unc["10%"] - 5.0, 2)
    return values, unc


def test_a_new_result_stores_and_exports_the_held_series(hub):
    values, unc = _dipping_factors(hub)
    sid = hub.submit(hub.cdf(name="40999")).sample_id
    _run(hub, values)
    s = hub.sample(sid)
    assert s["status"] == "final", s["error"]
    rev = store.get_revision(sid, db=hub.db)
    results = json.loads(rev["results"])
    stored_unc = json.loads(rev["d86_uncorrected"])
    assert stored_unc["10%"] == unc["10%"]                          # the plain math, kept
    assert results["D86 T10"] == results["D86 T5"] == unc["5%"]     # held at T5
    (line,) = _lines(hub, sid)
    row = _line_row(line)
    assert row["D86 T10"] == row["D86 T5"] == str(unc["5%"])
    series = [float(row[c]) for c in distill.CSV_HEADER[15:28]]
    assert series == sorted(series)


def test_an_old_revision_is_history_until_reprocessed(hub):
    values, unc = _dipping_factors(hub)
    sid = hub.submit(hub.cdf(name="41000")).sample_id
    _run(hub, values)
    # make revision 1 look like a pre-v7.0.0 result: the corrected T10 dips
    rev = store.get_revision(sid, db=hub.db)
    old = json.loads(rev["results"])
    dipped = round(unc["10%"] + values["10%"], 2)
    assert dipped < old["D86 T5"]
    old["D86 T10"] = dipped
    with store.connection(hub.db) as conn:
        conn.execute("UPDATE sample_results SET results = ? WHERE sample_id = ? AND revision = 1",
                     (json.dumps(old), sid))
        conn.commit()
    # Export to LIMS re-sends the stored revision as it is (never recomputes)
    pipeline.export_to_lims(sid, by="ryan", db=hub.db, data_dir=hub.data)
    assert _line_row(_lines(hub, sid)[-1])["D86 T10"] == str(dipped)
    assert json.loads(store.get_revision(sid, db=hub.db)["results"])["D86 T10"] == dipped
    # Re-process with the recorded blank and corrections applies the rule
    pipeline.request_reprocess(sid, by="ryan", db=hub.db)
    _run(hub, {c: 99.0 for c in corrections.D86_CUTS})       # current factors: not used
    cur = store.get_revision(sid, db=hub.db)
    assert cur["reason"] == "reprocess"
    assert json.loads(cur["corrections_used"])["values"] == values
    assert json.loads(cur["results"])["D86 T10"] == unc["5%"]
    assert _line_row(_lines(hub, sid)[-1])["D86 T10"] == str(unc["5%"])
    # history: revisions 1 and 2 still hold the dip
    for n in (1, 2):
        assert json.loads(store.get_revision(sid, n, db=hub.db)["results"])["D86 T10"] == dipped
