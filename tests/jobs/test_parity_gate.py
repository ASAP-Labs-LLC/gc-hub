"""The parity report as a go-live gate (2A1 T6 after review, C1/C2/I1-I3).

The critic's four false passes are tests here, and each now fails the
report: an altered first row swallowed by the blank rule, an altered
blank-named sample, a hub sample whose v1 row has another time, and a v1 CSV
checked against an empty hub. The report is driven by the hub's samples,
explanations are proven by recomputation, and a report that verified no
numbers cannot pass.
"""
from __future__ import annotations

from loader_testlib import SIMDIS, hub, plain_blank, v1_row, write_v1_csv  # noqa: F401

import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import cdf_fixtures as fx
import distill
import store
from jobs.load_folder import load_folder

REPO = Path(__file__).resolve().parent.parent.parent
CLI = REPO / "tools" / "parity_report.py"
if str(REPO / "tools") not in sys.path:
    sys.path.insert(0, str(REPO / "tools"))

from parity_report import parity_report  # noqa: E402

DAY = datetime(2026, 9, 25)
SNAPSHOT_CSV = Path("/Users/rynatical/Projects/gc-share-snapshot-2026-09-25/webapp-live/distill_results.csv")


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


def _loaded(hub, tmp_path, make, *, name="robocopy", instrument="gc1"):
    src = tmp_path / name
    src.mkdir()
    files = make(src)
    summary = load_folder(instrument, src, backfill=False, db=hub.db, data_dir=hub.data, conf=hub.conf)
    hub.worker().run_until_idle()
    return files, summary


def _report(hub, tmp_path, rows, **kw):
    kw.setdefault("conf", hub.conf)
    kw.setdefault("v1_corrections", hub.corrections)
    v1 = write_v1_csv(tmp_path / "v1.csv", rows) if isinstance(rows, list) else rows
    return parity_report("gc1", v1, db=hub.db, out_dir=tmp_path / "out", **kw)


def _nonsrc(rep, lab=None):
    """The differences other than the expected ones: Source File, and the
    corrected D86 cells v1 let dip (v7.0.0; these blank-less fixtures' T10)."""
    return [d for d in rep["differences"] if d["tag"] not in ("source-file", "d86-monotonic")
            and (lab is None or d["lab_id"] == lab)]


def _sample(hub, lab, dt):
    return store.samples.find_by_key("gc1", lab, dt, db=hub.db)


# ── the critic's probes ─────────────────────────────────────────────────────

def test_probe_an_altered_first_row_is_not_swallowed_by_the_blank_rule(hub, tmp_path):
    """v1 used the same blank as the hub, and three values are altered; no
    blank row precedes it in the CSV. Nothing reproduces v1's row."""
    hub.gc1()
    (b1, s), _ = _loaded(hub, tmp_path, lambda d: (
        fx.blank_cdf(d / "b1.CDF", injected=at(9, 0, 27), method_name=SIMDIS),
        fx.sample_cdf(d / "s.CDF", name="40404", injected=at(14, 23), method_name=SIMDIS)))
    row = v1_row(hub, s, blank=b1)
    row["2887 T50"] = str(float(row["2887 T50"]) + 5)
    row["D86 T90"] = str(float(row["D86 T90"]) - 7)
    row["Best Fit"] = "Totally Different Fuel"
    rep = _report(hub, tmp_path, [row, v1_row(hub, b1)])
    got = {(d["column"], d["tag"]) for d in _nonsrc(rep, "40404")}
    assert got == {("2887 T50", "unexplained"), ("D86 T90", "unexplained"),
                   ("Best Fit", "unexplained")}
    assert rep["exit_code"] == 1


def test_probe_an_altered_blank_named_sample_is_unexplained(hub, tmp_path):
    hub.gc1()
    (s,), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "s.CDF", name="[b] Blank", injected=at(14, 23), method_name=SIMDIS),))
    row = v1_row(hub, s)                         # v1 had no blank: the numbers should agree
    row["2887 T50"] = str(float(row["2887 T50"]) + 5)
    rep = _report(hub, tmp_path, [row])
    assert [(d["column"], d["tag"]) for d in _nonsrc(rep)] == [("2887 T50", "unexplained")]
    assert rep["exit_code"] == 1


def test_probe_a_hub_sample_whose_v1_row_has_another_time_fails(hub, tmp_path):
    hub.gc1()
    (s,), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "s.CDF", name="40404", injected=at(14, 23), method_name=SIMDIS),))
    row = v1_row(hub, s, injection_dt="2026-09-25 14:23:07")
    row["2887 T50"] = "1.0"
    rep = _report(hub, tmp_path, [row])
    tags = {d["tag"]: d for d in rep["differences"]}
    assert set(tags) == {"lab-id-other-time", "not-in-hub"}
    assert "2026-09-25 14:23:07" in tags["lab-id-other-time"]["detail"]
    assert rep["summary"]["rows_numerically_verified"] == 0
    assert rep["summary"]["rows_matching"] == 0
    assert rep["exit_code"] == 1


def _eighty_rows(hub, tmp_path):
    s = fx.sample_cdf(tmp_path / "x.CDF", name="40000", injected=at(14, 23), method_name=SIMDIS)
    base = v1_row(hub, s)
    rows = []
    for i in range(80):
        r = dict(base)
        r["Lab ID"] = f"4{i:04d}"
        rows.append(r)
    return rows


def test_probe_a_v1_csv_against_an_empty_hub_fails(hub, tmp_path):
    hub.gc1()
    rep = _report(hub, tmp_path, _eighty_rows(hub, tmp_path))
    S = rep["summary"]
    assert S["hub_samples"] == 0 and S["rows_numerically_verified"] == 0
    assert S["tags"]["not-in-hub"]["rows"] == 80
    assert rep["exit_code"] == 1
    assert any("numerically verified" in r for r in S["fail_reasons"])
    html = Path(rep["html"]).read_text(encoding="utf-8")
    verdict = html.split('class="verdict', 1)[1].split("</p>", 1)[0]
    assert "FAIL" in verdict and "rows numerically verified: 0" in verdict


@pytest.mark.skipif(not SNAPSHOT_CSV.is_file(), reason="the share snapshot is not on this machine")
def test_probe_the_snapshot_csv_against_an_empty_hub_fails(hub, tmp_path):
    hub.gc1()
    rep = parity_report("gc1", SNAPSHOT_CSV, db=hub.db, out_dir=tmp_path / "out", conf=hub.conf)
    assert rep["summary"]["rows_numerically_verified"] == 0
    assert rep["exit_code"] == 1


# ── C1: driven by the hub's samples ─────────────────────────────────────────

def test_a_hub_sample_with_no_v1_row_fails(hub, tmp_path):
    hub.gc1()
    (a, b), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "a.CDF", name="40401", injected=at(14, 23), method_name=SIMDIS),
        fx.sample_cdf(d / "b.CDF", name="40402", injected=at(15, 23), method_name=SIMDIS)))
    rep = _report(hub, tmp_path, [v1_row(hub, a)])
    (d,) = _nonsrc(rep)
    assert (d["lab_id"], d["tag"]) == ("40402", "no-v1-row")
    assert rep["summary"]["rows_numerically_verified"] == 1
    assert rep["exit_code"] == 1


def test_the_scope_can_be_restricted_to_sample_ids_or_a_receive_time(hub, tmp_path):
    hub.gc1()
    (b,), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "b.CDF", name="40402", injected=at(15, 23), method_name=SIMDIS),), name="first")
    since = store.now_iso()
    (a,), summary = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "a.CDF", name="40401", injected=at(14, 23), method_name=SIMDIS),), name="second")
    rows = [v1_row(hub, a)]
    assert _report(hub, tmp_path, rows)["exit_code"] == 1                  # 40402 has no v1 row
    scoped = _report(hub, tmp_path, rows, sample_ids=summary["sample_ids"])
    assert scoped["exit_code"] == 0, scoped["summary"]
    assert scoped["summary"]["hub_samples"] == 1
    assert _report(hub, tmp_path, rows, since=since)["exit_code"] == 0


def test_an_ambiguous_match_fails(hub, tmp_path):
    """A's stamp 00:24:50 was misparsed by v1 as 02:45:00; B was injected at
    02:45:00. A v1 row at 02:45:00 could be either."""
    hub.gc1()
    (a, b), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "a.CDF", name="40500", injected=at(0, 24, 50), method_name=SIMDIS),
        fx.sample_cdf(d / "b.CDF", name="40500", injected=at(2, 45, 0), method_name=SIMDIS,
                      shift=0.1)))
    assert _sample(hub, "40500", "2026-09-25 00:24:50")["legacy_injection_dt"] == "2026-09-25 02:45:00"
    rep = _report(hub, tmp_path, [v1_row(hub, b)])
    (d,) = rep["differences"]
    assert d["tag"] == "ambiguous-match"
    assert rep["exit_code"] == 1


def test_superseded_rows_are_counted_per_hub_sample(hub, tmp_path):
    """v1 wrote the sample under the correct time, then (after the misparse
    began) under the legacy time: two rows, one sample; the last is compared."""
    hub.gc1()
    (s,), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "s.CDF", name="40320", injected=at(0, 24, 50), method_name=SIMDIS),))
    first = v1_row(hub, s, injection_dt="2026-09-25 00:24:50")
    first["2887 T50"] = "999.99"
    rep = _report(hub, tmp_path, [first, v1_row(hub, s)])
    S = rep["summary"]
    assert S["superseded_v1_rows"] == 1 and S["compared_rows"] == 1
    assert [(d["column"], d["tag"]) for d in _nonsrc(rep)] == [("InjectionDateTime",
                                                               "injection-time-fix")]
    assert rep["exit_code"] == 0


# ── C2: pre-31-column rows and empty cells ──────────────────────────────────

OLD_HEADER = [c for c in distill.CSV_HEADER
              if c not in ("2887 T40", "2887 T60", "D86 T40", "D86 T60", "Best Fit", "Fit Score",
                           "Source File")]


def test_columns_an_old_header_lacks_are_v1_short_row_only(hub, tmp_path):
    hub.gc1()
    (s,), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "s.CDF", name="40404", injected=at(14, 23), method_name=SIMDIS),))
    row = v1_row(hub, s)
    path = tmp_path / "old.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(OLD_HEADER)
        w.writerow([row[c] for c in OLD_HEADER])
    rep = _report(hub, tmp_path, path)
    tags = {(d["column"], d["tag"]) for d in rep["differences"] if d["tag"] != "d86-monotonic"}
    assert tags == {(c, "v1-short-row") for c in ("2887 T40", "2887 T60", "D86 T40", "D86 T60",
                                                   "Source File")}
    assert rep["summary"]["rows_numerically_verified"] == 1
    assert rep["exit_code"] == 0


def test_a_value_empty_on_one_side_is_unexplained(hub, tmp_path):
    hub.gc1()
    (b1, s, b2), _ = _loaded(hub, tmp_path, lambda d: (
        fx.blank_cdf(d / "b1.CDF", injected=at(9, 0, 27), method_name=SIMDIS),
        fx.sample_cdf(d / "s.CDF", name="40404", injected=at(14, 23), method_name=SIMDIS),
        plain_blank(d / "b2.CDF", at(19, 30, 27), ramp=9.0, offset=60.0)))
    row = v1_row(hub, s, blank=b2)             # the blank rule explains the numbers...
    row["D86 T40"] = ""                        # ...but not an empty cell
    rows = [v1_row(hub, b1), v1_row(hub, b2), row]
    rep = _report(hub, tmp_path, rows)
    got = {d["column"]: d["tag"] for d in _nonsrc(rep, "40404")}
    assert got["D86 T40"] == "unexplained"
    assert got["2887 T50"] == "blank-rule"
    assert rep["exit_code"] == 1


# ── C2/I2: explanations are proven by recomputation ─────────────────────────

def test_a_hub_revision_the_recompute_cannot_reproduce_proves_nothing(hub, tmp_path):
    hub.gc1()
    (b1, s, b2), _ = _loaded(hub, tmp_path, lambda d: (
        fx.blank_cdf(d / "b1.CDF", injected=at(9, 0, 27), method_name=SIMDIS),
        fx.sample_cdf(d / "s.CDF", name="40404", injected=at(14, 23), method_name=SIMDIS),
        plain_blank(d / "b2.CDF", at(19, 30, 27), ramp=9.0, offset=60.0)))
    sid = _sample(hub, "40404", "2026-09-25 14:23:00")["id"]
    rev = store.get_revision(sid, db=hub.db)
    results = json.loads(rev["results"])
    results["2887 T10"] = results["2887 T10"] + 3                 # the hub's own row no longer fits
    with store.connection(hub.db) as conn:
        conn.execute("UPDATE sample_results SET results=? WHERE sample_id=? AND revision=?",
                     (json.dumps(results), sid, rev["revision"]))
    rows = [v1_row(hub, b1), v1_row(hub, b2), v1_row(hub, s, blank=b2)]
    rep = _report(hub, tmp_path, rows)
    diffs = _nonsrc(rep, "40404")
    assert diffs and {d["tag"] for d in diffs} == {"unexplained"}
    assert all("reproduce" in d["detail"] for d in diffs)


def test_corrections_need_the_v1_file_and_the_file_must_yield_cuts(hub, tmp_path):
    hub.gc1()
    (s,), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "s.CDF", name="40404", injected=at(14, 23), method_name=SIMDIS),))
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"Other GC": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        _report(hub, tmp_path, [v1_row(hub, s)], v1_corrections=empty)
    with pytest.raises(ValueError):
        _report(hub, tmp_path, [v1_row(hub, s)], v1_corrections=tmp_path / "missing.json")


def test_when_v1s_blank_is_known_no_other_blank_may_explain_the_numbers(hub, tmp_path):
    """The replayed CSV says v1's blank was B1 (the hub's too), but the row's
    numbers are what B2 would give. Only B1 may be tried: unexplained."""
    hub.gc1()
    (b1, s, b2), _ = _loaded(hub, tmp_path, lambda d: (
        fx.blank_cdf(d / "b1.CDF", injected=at(9, 0, 27), method_name=SIMDIS),
        fx.sample_cdf(d / "s.CDF", name="40404", injected=at(14, 23), method_name=SIMDIS),
        plain_blank(d / "b2.CDF", at(19, 30, 27), ramp=9.0, offset=60.0)))
    rows = [v1_row(hub, b1), v1_row(hub, s, blank=b2), v1_row(hub, b2)]
    rep = _report(hub, tmp_path, rows)
    diffs = _nonsrc(rep, "40404")
    assert diffs and {d["tag"] for d in diffs} == {"unexplained"}


def test_a_v1_blank_name_is_never_explained_by_a_subtraction(hub, tmp_path):
    """v1 never blank-subtracted a run named "Blank" (here one carrying sample
    signal); a row whose numbers are that run minus B1 is unexplained, even
    though that recompute reproduces it."""
    hub.gc1()
    (b1, named), _ = _loaded(hub, tmp_path, lambda d: (
        fx.blank_cdf(d / "b1.CDF", injected=at(9, 0, 27), method_name=SIMDIS),
        fx.sample_cdf(d / "n.CDF", name="Blank", injected=at(14, 23), method_name=SIMDIS)))
    lying = v1_row(hub, named, blank=b1)
    rep = _report(hub, tmp_path, [v1_row(hub, b1), lying])
    diffs = [d for d in _nonsrc(rep) if d["v1_injection_dt"] == lying["InjectionDateTime"]]
    assert diffs and {d["tag"] for d in diffs} == {"unexplained"}


# ── I1: method exclusions ───────────────────────────────────────────────────

def test_a_d2887_method_name_among_the_excluded_fails(hub, tmp_path):
    hub.gc1()
    store.instruments.upsert({"id": "gc1", "method_map": json.dumps({"SIMDISTB.M": "D2887"})},
                             db=hub.db)
    (x, ok), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "x.CDF", name="40401", injected=at(14, 23), method_name="SIMDISB.M"),
        fx.sample_cdf(d / "ok.CDF", name="40402", injected=at(15, 23), method_name="SIMDISTB.M")))
    rep = _report(hub, tmp_path, [v1_row(hub, x), v1_row(hub, ok)])
    S = rep["summary"]
    assert S["method_excluded_names"] == {"SIMDISB.M": 1}
    assert S["method_excluded_d2887"] == ["SIMDISB.M"]
    assert S["rows_numerically_verified"] == 1
    assert any("SIMDISB.M" in r for r in S["fail_reasons"])
    assert rep["exit_code"] == 1
    assert "SIMDISB.M" in Path(rep["html"]).read_text(encoding="utf-8")


def test_an_other_method_run_passes_only_when_its_name_is_accepted(hub, tmp_path):
    hub.gc1()
    (x, ok), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "x.CDF", name="40401", injected=at(14, 23), method_name="D7096.M"),
        fx.sample_cdf(d / "ok.CDF", name="40402", injected=at(15, 23), method_name=SIMDIS)))
    rows = [v1_row(hub, x), v1_row(hub, ok)]
    rep = _report(hub, tmp_path, rows)
    assert rep["summary"]["method_excluded_names"] == {"D7096.M": 1}
    (d,) = _nonsrc(rep, "40401")
    assert d["tag"] == "method-not-accepted"
    assert rep["exit_code"] == 1
    ok_rep = _report(hub, tmp_path, rows, accept_excluded_methods=[" d7096.m "])   # normalised
    (d,) = _nonsrc(ok_rep, "40401")
    assert d["tag"] == "method-excluded"
    assert ok_rep["summary"]["accepted_methods"] == ["D7096.M"]
    assert ok_rep["exit_code"] == 0
    assert "D7096.M" in ok_rep["summary"]["verdict_line"]
    assert "accepted" in Path(ok_rep["html"]).read_text(encoding="utf-8")


def test_a_d2887_name_cannot_be_accepted(hub, tmp_path):
    hub.gc1()
    store.instruments.upsert({"id": "gc1", "method_map": json.dumps({"SIMDISTB.M": "D2887"})},
                             db=hub.db)
    (x, ok), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "x.CDF", name="40401", injected=at(14, 23), method_name="SIMDISB.M"),
        fx.sample_cdf(d / "ok.CDF", name="40402", injected=at(15, 23), method_name="SIMDISTB.M")))
    rep = _report(hub, tmp_path, [v1_row(hub, x), v1_row(hub, ok)],
                  accept_excluded_methods=["SIMDISB.M"])
    (d,) = _nonsrc(rep, "40401")
    assert d["tag"] == "method-not-accepted" and "D2887" in d["detail"]
    assert rep["exit_code"] == 1
    assert any("SIMDISB.M" in r for r in rep["summary"]["fail_reasons"])


# ── the second review's probes ──────────────────────────────────────────────

def _good_plus(hub, tmp_path, extra_name, extra_method, *, alter=True):
    hub.gc1()
    b1, good, bad = _loaded(hub, tmp_path, lambda d: (
        fx.blank_cdf(d / "b1.CDF", injected=at(9, 0, 27), method_name=SIMDIS),
        fx.sample_cdf(d / "g.CDF", name="40401", injected=at(14, 23), method_name=SIMDIS),
        fx.sample_cdf(d / "x.CDF", name=extra_name, injected=at(15, 23), method_name=extra_method,
                      shift=0.05)))[0]
    rows = [v1_row(hub, b1), v1_row(hub, good, blank=b1)]
    r = v1_row(hub, bad, blank=b1)
    if alter:
        r["2887 T50"] = str(float(r["2887 T50"]) + 9)
    rows.append(r)
    return rows


def test_probe_other_method_d2887_like(hub, tmp_path):
    """A D2887 run under a name nobody mapped (SIMDIS2.M), T50 altered by 9 °C:
    excluded from processing, so its numbers are never compared. Not accepted:
    the report fails."""
    rows = _good_plus(hub, tmp_path, "40402", "SIMDIS2.M")
    rep = _report(hub, tmp_path, rows)
    (d,) = _nonsrc(rep, "40402")
    assert d["tag"] == "method-not-accepted" and "SIMDIS2.M" in d["detail"]
    assert rep["exit_code"] == 1
    assert any("SIMDIS2.M" in r for r in rep["summary"]["fail_reasons"])


def test_probe_review_method(hub, tmp_path):
    rows = _good_plus(hub, tmp_path, "40402", None)
    rep = _report(hub, tmp_path, rows)
    (d,) = _nonsrc(rep, "40402")
    assert d["tag"] == "review-method"
    assert rep["exit_code"] == 1
    # accepting names never accepts a CDF with no method name
    assert _report(hub, tmp_path, rows, accept_excluded_methods=[""])["exit_code"] == 1


def test_probe_header_without_numbers(hub, tmp_path):
    """The altered row sits under a mid-file header with no numeric columns
    (30 cells: long-row). Nothing numeric is checked for it: not-verified."""
    rows = _good_plus(hub, tmp_path, "40402", SIMDIS)
    p = tmp_path / "v1.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(distill.CSV_HEADER)
        for r in rows[:2]:
            w.writerow([r.get(c, "") for c in distill.CSV_HEADER])
        w.writerow(["Lab ID", "InjectionDateTime", "Source File"])
        w.writerow([rows[2].get(c, "") for c in distill.CSV_HEADER[:-1]])     # 30 cells
    rep = _report(hub, tmp_path, p)
    tags = {d["tag"] for d in _nonsrc(rep, "40402")}
    assert "not-verified" in tags
    assert rep["summary"]["rows_numerically_verified"] == 2                  # blank + good only
    assert rep["exit_code"] == 1


def test_a_header_without_numbers_fails_even_on_a_well_formed_row(hub, tmp_path):
    rows = _good_plus(hub, tmp_path, "40402", SIMDIS, alter=False)
    p = tmp_path / "v1.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(distill.CSV_HEADER)
        for r in rows[:2]:
            w.writerow([r.get(c, "") for c in distill.CSV_HEADER])
        w.writerow(["Lab ID", "InjectionDateTime", "Source File"])
        w.writerow([rows[2][c] for c in ("Lab ID", "InjectionDateTime", "Source File")])
    rep = _report(hub, tmp_path, p)
    (d,) = [d for d in _nonsrc(rep, "40402") if d["tag"] != "v1-short-row"]
    assert d["tag"] == "not-verified"
    assert rep["exit_code"] == 1


def test_a_row_with_a_decode_error_fails(hub, tmp_path):
    rows = _good_plus(hub, tmp_path, "40402", SIMDIS, alter=False)
    p = write_v1_csv(tmp_path / "v1.csv", rows)
    data = p.read_bytes().replace(b"40402", b"40402", 1)
    lines = data.split(b"\r\n")
    lines[3] = lines[3] + b"\x81"            # undefined in UTF-8 and in cp1252
    p.write_bytes(b"\r\n".join(lines))
    rep = _report(hub, tmp_path, p)
    assert "not-verified" in {d["tag"] for d in _nonsrc(rep, "40402")}
    assert rep["exit_code"] == 1


# ── scope, conflicts, magnitudes ────────────────────────────────────────────

def test_the_scope_and_open_conflicts_are_reported(hub, tmp_path):
    rows = _good_plus(hub, tmp_path, "40402", SIMDIS, alter=False)
    good = _sample(hub, "40401", "2026-09-25 14:23:00")
    blank = _sample(hub, "Blank", "2026-09-25 09:00:27")
    held = fx.sample_cdf(tmp_path / "held.CDF", name="40401", injected=at(14, 23), method_name=SIMDIS,
                         shift=0.2)
    assert hub.submit(held).outcome == "conflict"
    rep = _report(hub, tmp_path, rows, sample_ids=[good["id"], blank["id"]])
    S = rep["summary"]
    assert S["scope"] == {"sample_ids": 2, "since": None}
    assert S["v1_rows_out_of_scope"] == 1
    assert "v1 rows outside the scope: 1" in S["verdict_line"]
    (c,) = S["open_conflicts"]
    assert c["lab_id"] == "40401" and c["existing_sample_id"] == good["id"]
    html = Path(rep["html"]).read_text(encoding="utf-8")
    assert "Open conflicts" in html and "sample ids (2)" in html


def test_the_largest_difference_per_proven_tag(hub, tmp_path):
    hub.gc1()
    (b1, s, b2), _ = _loaded(hub, tmp_path, lambda d: (
        fx.blank_cdf(d / "b1.CDF", injected=at(9, 0, 27), method_name=SIMDIS),
        fx.sample_cdf(d / "s.CDF", name="40404", injected=at(14, 23), method_name=SIMDIS),
        plain_blank(d / "b2.CDF", at(19, 30, 27), ramp=9.0, offset=60.0)))
    rows = [v1_row(hub, b1), v1_row(hub, b2), v1_row(hub, s, blank=b2)]
    rep = _report(hub, tmp_path, rows)
    diffs = [d for d in _nonsrc(rep, "40404") if d["tag"] == "blank-rule" and d["column"] != "Best Fit"]
    expected = max(abs(float(d["v1"]) - float(d["hub"])) for d in diffs)
    assert rep["summary"]["max_abs_difference"]["blank-rule"] == pytest.approx(expected)
    assert "Largest difference" in Path(rep["html"]).read_text(encoding="utf-8")


# ── the CLI ─────────────────────────────────────────────────────────────────

def _cli(hub, *args):
    env = dict(os.environ)
    env.pop("GC_DATA_DIR", None)
    (hub.data / "settings.json").write_text(json.dumps(hub.conf), encoding="utf-8")
    return subprocess.run([sys.executable, str(CLI), "--data-dir", str(hub.data), *map(str, args)],
                          capture_output=True, text=True, env=env, timeout=120)


def test_cli_validates_the_v1_corrections_file(hub, tmp_path):
    hub.gc1()
    v1 = write_v1_csv(tmp_path / "v1.csv", [])
    assert _cli(hub, "--v1-corrections", tmp_path / "missing.json", "gc1", v1).returncode == 2
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    res = _cli(hub, "--v1-corrections", empty, "gc1", v1)
    assert res.returncode == 2 and "correction" in res.stderr


def test_cli_scope_from_a_loader_summary(hub, tmp_path):
    hub.gc1()
    (b,), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "b.CDF", name="40402", injected=at(15, 23), method_name=SIMDIS),), name="first")
    (a,), summary = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "a.CDF", name="40401", injected=at(14, 23), method_name=SIMDIS),), name="second")
    scope = tmp_path / "loaded.json"
    scope.write_text(json.dumps(summary), encoding="utf-8")
    v1 = write_v1_csv(tmp_path / "v1.csv", [v1_row(hub, a)])
    res = _cli(hub, "--v1-corrections", hub.corrections, "--sample-ids", f"@{scope}", "gc1", v1)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "rows numerically verified: 1" in res.stdout
    ids = ",".join(str(i) for i in summary["sample_ids"])
    assert _cli(hub, "--v1-corrections", hub.corrections, "--sample-ids", ids, "gc1",
                v1).returncode == 0
    assert _cli(hub, "--v1-corrections", hub.corrections, "gc1", v1).returncode == 1


def test_cli_accept_excluded_method(hub, tmp_path):
    hub.gc1()
    (x, ok), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "x.CDF", name="40401", injected=at(14, 23), method_name="D7096.M"),
        fx.sample_cdf(d / "ok.CDF", name="40402", injected=at(15, 23), method_name=SIMDIS)))
    v1 = write_v1_csv(tmp_path / "v1.csv", [v1_row(hub, x), v1_row(hub, ok)])
    assert _cli(hub, "--v1-corrections", hub.corrections, "gc1", v1).returncode == 1
    res = _cli(hub, "--v1-corrections", hub.corrections, "--accept-excluded-method", "d7096.m",
               "gc1", v1)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "accepted excluded methods: D7096.M" in res.stdout


# ── v7.0.0: a corrected D86 that never decreases ────────────────────────────

def _dipping_sample(hub, tmp_path):
    hub.gc1()
    (s,), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "s.CDF", name="40404", injected=at(14, 23), method_name=SIMDIS),))
    row = v1_row(hub, s)                          # v1: the corrected T10 dips below T5
    assert float(row["D86 T10"]) < float(row["D86 T5"])
    return s, row


def test_a_held_d86_cell_is_d86_monotonic_and_passes(hub, tmp_path):
    s, row = _dipping_sample(hub, tmp_path)
    rep = _report(hub, tmp_path, [row])
    held = [d for d in rep["differences"] if d["tag"] == "d86-monotonic"]
    assert [(d["column"], d["v1"], d["hub"]) for d in held] == [("D86 T10", str(row["D86 T10"]), str(row["D86 T5"]))]
    assert _nonsrc(rep) == []
    assert rep["summary"]["rows_numerically_verified"] == 1
    assert rep["exit_code"] == 0


def test_a_d86_cell_held_at_anything_else_is_unexplained(hub, tmp_path):
    """The tag is proven, never assumed: a v1 row whose dip the hub's value
    doesn't equal the earlier cut's isn't d86-monotonic."""
    s, row = _dipping_sample(hub, tmp_path)
    row["D86 T5"] = str(float(row["D86 T5"]) + 0.01)      # v1's T5 no longer what the hub held at
    rep = _report(hub, tmp_path, [row])
    assert "d86-monotonic" not in {d["tag"] for d in rep["differences"]}
    assert {d["column"] for d in _nonsrc(rep) if d["tag"] == "unexplained"} >= {"D86 T5", "D86 T10"}
    assert rep["exit_code"] == 1


def test_a_revision_stored_before_v7_still_proves(hub, tmp_path):
    """A hub revision from before v7.0.0 (its T10 dipping, like v1's) equals
    v1's row; the control recomputation, which now holds T10, is compared with
    that revision under the same rule, so a corrections difference elsewhere
    is still proven."""
    s, row = _dipping_sample(hub, tmp_path)
    sample = store.samples.find_by_key("gc1", "40404", "2026-09-25 14:23:00", db=hub.db)
    rev = store.get_revision(sample["id"], db=hub.db)
    results = json.loads(rev["results"])
    results["D86 T10"] = float(row["D86 T10"])           # as stored before v7.0.0
    results["D86 IBP"] = results["D86 IBP"] + 1.5         # and the hub's IBP factor differed
    used = json.loads(rev["corrections_used"])
    used["values"]["IBP"] = used["values"]["IBP"] + 1.5
    with store.connection(hub.db) as conn:
        conn.execute("UPDATE sample_results SET results = ?, corrections_used = ? "
                     "WHERE sample_id = ? AND revision = ?",
                     (json.dumps(results), json.dumps(used), sample["id"], rev["revision"]))
        conn.commit()
    rep = _report(hub, tmp_path, [row])
    assert [(d["column"], d["tag"]) for d in _nonsrc(rep)] == [("D86 IBP", "corrections")]
    assert rep["exit_code"] == 0
