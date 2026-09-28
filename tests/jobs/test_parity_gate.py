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
    return [d for d in rep["differences"] if d["tag"] != "source-file"
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
    tags = {(d["column"], d["tag"]) for d in rep["differences"]}
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


def test_an_other_method_run_is_listed_and_passes(hub, tmp_path):
    hub.gc1()
    (x, ok), _ = _loaded(hub, tmp_path, lambda d: (
        fx.sample_cdf(d / "x.CDF", name="40401", injected=at(14, 23), method_name="D7096.M"),
        fx.sample_cdf(d / "ok.CDF", name="40402", injected=at(15, 23), method_name=SIMDIS)))
    rep = _report(hub, tmp_path, [v1_row(hub, x), v1_row(hub, ok)])
    assert rep["summary"]["method_excluded_names"] == {"D7096.M": 1}
    assert rep["summary"]["method_excluded_d2887"] == []
    assert rep["exit_code"] == 0


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
