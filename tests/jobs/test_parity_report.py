"""The v1 parity report (2A1 T6, the 2A1 acceptance gate): every difference
between v1's results CSV and the hub's current revisions is tagged with its
explanation, and anything unexplained fails the report.

The scenario loads synthetic CDFs through the folder loader, lets the worker
process them, and writes the v1 CSV v1 would have written for the same files
(its processing order, its newest-registered blank, its corrections file,
its misparsed injection times and absolute ``Source File`` paths). Each tag
is produced deliberately.
"""
from __future__ import annotations

from loader_testlib import (SIMDIS, hub, plain_blank, v1_row, write_v1_csv)  # noqa: F401

import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import cdf_fixtures as fx
import corrections
import distill
import pipeline
import store
from jobs.load_folder import load_folder

REPO = Path(__file__).resolve().parent.parent.parent
CLI = REPO / "tools" / "parity_report.py"
sys.path.insert(0, str(REPO / "tools"))

from parity_report import parity_report  # noqa: E402

DAY = datetime(2026, 9, 25)


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


class ShiftedIBP:
    """The corrections file with IBP moved by +1.5 °C (the hub's values differ from v1's file)."""

    def __init__(self, path):
        self.inner = corrections.FileProvider(str(path))

    def get(self, instrument):
        c = self.inner.get(instrument)
        values = dict(c.values)
        values["IBP"] = values["IBP"] + 1.5
        return corrections.Corrections(source="hub", updated_at=c.updated_at, values=values)


class Scenario:
    def __init__(self, hub, *, alter=True):
        self.hub = hub
        hub.gc1()
        src = hub.root / "robocopy"
        src.mkdir()
        f = {}
        f["S4"] = fx.sample_cdf(src / "s4.CDF", name="40320", injected=at(0, 24, 50), method_name=SIMDIS)
        f["B1"] = fx.blank_cdf(src / "b1.CDF", injected=at(9, 0, 27), method_name=SIMDIS)
        f["B0"] = fx.blank_cdf(src / "b0.CDF", injected=at(8, 0, 27), method_name=SIMDIS)
        f["F"] = fx.sample_cdf(src / "f.CDF", name="Blank2", injected=at(18, 33), method_name=SIMDIS)
        f["S1"] = fx.sample_cdf(src / "s1.CDF", name="40304", injected=at(14, 23), method_name=SIMDIS)
        f["S2"] = fx.sample_cdf(src / "s2.CDF", name="40310", injected=at(15, 13), method_name=SIMDIS,
                                shift=0.05)
        f["S3"] = fx.sample_cdf(src / "s3.CDF", name="(Blank)", injected=at(16, 43), method_name=SIMDIS)
        f["S5"] = fx.sample_cdf(src / "s5.CDF", name="40330", injected=at(17, 13), method_name="D7096.M")
        f["S6"] = fx.sample_cdf(src / "s6.CDF", name="40340", injected=at(18, 13), method_name=SIMDIS,
                                shift=0.1)
        f["S7"] = fx.sample_cdf(src / "s7.CDF", name="40350", injected=at(18, 43), method_name=SIMDIS,
                                shift=-0.05)
        f["B2"] = plain_blank(src / "b2.CDF", at(19, 30, 27), ramp=9.0, offset=60.0)
        self.files = f
        summary = load_folder("gc1", src, backfill=False, db=hub.db, data_dir=hub.data, conf=hub.conf)
        assert summary["created"] == len(f)
        hub.worker().run_until_idle()
        self.ids = {k: store.samples.find_by_sha(_sha(p), db=hub.db)["id"] for k, p in f.items()}
        pipeline.request_reprocess(self.ids["S6"], use_current_corrections=True, db=hub.db)
        hub.worker(corrections_provider=ShiftedIBP(hub.corrections)).run_until_idle()
        assert store.get_revision(self.ids["S6"], db=hub.db)["reason"] == "reprocess"

        s1_old = v1_row(hub, f["S1"], blank=f["B1"])
        s1_old["2887 T50"] = "999.99"                                  # superseded by the next row
        s7 = v1_row(hub, f["S7"], blank=f["B1"])
        if alter:
            s7["2887 T50"] = str(float(s7["2887 T50"]) + 0.01)         # nothing explains this
        ghost = v1_row(hub, f["S1"], blank=f["B1"])
        ghost.update({"Lab ID": "99999", "InjectionDateTime": "2026-09-20 10:00:00"})
        # v1's processing order: S4 before any blank; B0 (older) arrives after
        # B1 and is not registered; "Blank2" carries sample signal and is refused;
        # B2 is registered after S6 and used (as the newest blank) for S2,
        # (Blank) and the D7096 run.
        self.rows = [
            v1_row(hub, f["S4"]),
            v1_row(hub, f["B1"]),
            v1_row(hub, f["B0"]),
            s1_old,
            v1_row(hub, f["S1"], blank=f["B1"]),
            v1_row(hub, f["F"]),
            s7,
            v1_row(hub, f["S6"], blank=f["B1"]),
            v1_row(hub, f["B2"]),
            v1_row(hub, f["S2"], blank=f["B2"]),
            v1_row(hub, f["S3"], blank=f["B2"]),
            v1_row(hub, f["S5"], blank=f["B2"]),
            ghost,
        ]
        self.csv = write_v1_csv(hub.root / "distill_results.csv", self.rows)
        self.out = hub.root / "reports"

    def rewrite(self):
        self.csv = write_v1_csv(self.csv, self.rows)

    def report(self, **kw):
        kw.setdefault("v1_corrections", self.hub.corrections)
        kw.setdefault("conf", self.hub.conf)
        return parity_report("gc1", self.csv, db=self.hub.db, out_dir=self.out, **kw)


def _sha(p):
    import hashlib
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _by(report, lab_id):
    return [d for d in report["differences"] if d["lab_id"] == lab_id]


def _tags(diffs):
    return {d["tag"] for d in diffs}


# ── the tags ────────────────────────────────────────────────────────────────

@pytest.fixture()
def scenario(hub):
    return Scenario(hub)


def test_every_difference_is_tagged(scenario):
    rep = scenario.report()
    S = rep["summary"]

    # Source File: hub-relative vs v1's absolute path, on every compared row.
    for lab in ("40320", "Blank", "Blank2", "40304", "40350", "40340", "40310", "(Blank)"):
        sf = [d for d in _by(rep, lab) if d["column"] == "Source File"]
        assert sf and {d["tag"] for d in sf} == {"source-file"}, lab

    # The v1 misparse: 00:24:50 written as 02:45:00.
    (fix,) = [d for d in _by(rep, "40320") if d["tag"] != "source-file"]
    assert (fix["column"], fix["tag"]) == ("InjectionDateTime", "injection-time-fix")
    assert (fix["v1"], fix["hub"]) == ("2026-09-25 02:45:00", "2026-09-25 00:24:50")

    # v1's newest-registered blank (B2) vs the hub's at-or-before blank (B1).
    s2 = [d for d in _by(rep, "40310") if d["tag"] != "source-file"]
    assert s2 and _tags(s2) == {"blank-rule"}
    assert any(d["column"].startswith("2887 ") for d in s2)
    # "(Blank)": v1 blank-subtracted it; the hub never subtracts a blank-named sample.
    s3 = [d for d in _by(rep, "(Blank)") if d["tag"] != "source-file"]
    assert s3 and _tags(s3) == {"blank-rule"}
    assert all("blank-named" in d["detail"] for d in s3)

    # Corrections: the hub's IBP value differs from v1's file; nothing else does.
    (corr,) = [d for d in _by(rep, "40340") if d["tag"] != "source-file"]
    assert (corr["column"], corr["tag"]) == ("D86 IBP", "corrections")
    assert float(corr["hub"]) == pytest.approx(float(corr["v1"]) + 1.5)

    # The D7096 run is not processed by the hub.
    (excl,) = _by(rep, "40330")
    assert excl["tag"] == "method-excluded" and "other_method" in excl["detail"]

    # A v1 row whose sample isn't in the hub.
    (ghost,) = _by(rep, "99999")
    assert ghost["tag"] == "not-in-hub"

    # The altered number. v1's blank for it is B1, like the hub's: the older B0
    # registered later and the non-genuine "Blank2" don't replace it.
    (bad,) = [d for d in _by(rep, "40350") if d["tag"] != "source-file"]
    assert (bad["column"], bad["tag"]) == ("2887 T50", "unexplained")

    # Rows that agree apart from Source File: S1 (current row), both blanks.
    for lab in ("40304", "Blank", "Blank2"):
        assert _tags(_by(rep, lab)) == {"source-file"}, lab

    assert S["v1_rows"] == 13
    assert S["superseded_v1_rows"] == 1
    assert S["hub_samples"] == 11
    assert S["compared_rows"] == 11
    assert S["v1_rows_not_in_hub"] == 1
    # every row with a hub result whose numbers agree or are proven: all but S7
    # (unexplained) and the D7096 run (no result)
    assert S["rows_numerically_verified"] == 9
    assert S["rows_matching"] == 5          # B1, B0, S1, "Blank2", B2: only Source File differs
    assert S["tags"]["unexplained"] == {"differences": 1, "rows": 1}
    assert S["tags"]["blank-rule"]["rows"] == 2
    assert S["tags"]["not-in-hub"]["rows"] == 1
    assert S["tags"]["method-excluded"]["rows"] == 1
    assert S["tags"]["corrections"] == {"differences": 1, "rows": 1}
    assert S["tags"]["injection-time-fix"] == {"differences": 1, "rows": 1}
    assert S["unexplained"] == 1
    assert rep["exit_code"] == 1


def test_no_unexplained_difference_passes(hub):
    rep = Scenario(hub, alter=False).report()
    assert rep["summary"]["unexplained"] == 0
    assert "unexplained" not in rep["summary"]["tags"]
    assert rep["exit_code"] == 0


def test_a_blank_rows_own_numbers_are_never_put_down_to_the_blank_rule(hub):
    sc = Scenario(hub, alter=False)
    b1 = sc.rows[1]
    assert b1["Lab ID"] == "Blank"
    b1["2887 T10"] = str(float(b1["2887 T10"]) + 0.01)
    sc.rewrite()
    rep = sc.report()
    bad = [d for d in rep["differences"] if d["tag"] != "source-file"
           and d["v1_injection_dt"] == b1["InjectionDateTime"]]
    assert [(d["column"], d["tag"]) for d in bad] == [("2887 T10", "unexplained")]


def test_without_the_v1_corrections_file_a_corrections_difference_is_unexplained(scenario):
    rep = scenario.report(v1_corrections=None)
    (d,) = [d for d in _by(rep, "40340") if d["tag"] != "source-file"]
    assert d["tag"] == "unexplained" and "--v1-corrections" in d["detail"]


def test_the_report_files(scenario):
    rep = scenario.report()
    csv_path, html_path = Path(rep["csv"]), Path(rep["html"])
    assert csv_path.parent == scenario.out and html_path.parent == scenario.out
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(rep["differences"])
    assert set(rows[0]) >= {"v1_line", "lab_id", "v1_injection_dt", "sample_id", "column", "v1",
                            "hub", "tag", "detail"}
    html = html_path.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    for tag in ("blank-rule", "corrections", "unexplained", "not-in-hub", "source-file"):
        assert tag in html
    assert "<script src" not in html and "<link" not in html


# ── the other statuses ──────────────────────────────────────────────────────

def test_a_sample_awaiting_calibration_is_auto_detect_off(hub, tmp_path):
    hub.gc1()
    hub.gc2()                                                # no calibration
    src = tmp_path / "gc2src"
    src.mkdir()
    cdf = fx.sample_cdf(src / "a.CDF", name="40401", method_name=SIMDIS)
    load_folder("gc2", src, backfill=False, db=hub.db, data_dir=hub.data, conf=hub.conf)
    hub.worker().run_until_idle()
    assert store.samples.search(instrument="gc2", db=hub.db)[0]["status"] == "awaiting_calibration"
    v1 = write_v1_csv(tmp_path / "v1.csv", [v1_row(hub, cdf, instrument="gc2", auto=True)])
    rep = parity_report("gc2", v1, db=hub.db, out_dir=tmp_path / "out", conf=hub.conf,
                        v1_corrections=hub.corrections)
    (d,) = rep["differences"]
    assert d["tag"] == "auto-detect-off" and "auto-detected" in d["detail"]
    # Proven, but no hub numbers were compared: the report can't pass on it alone.
    assert rep["summary"]["rows_numerically_verified"] == 0
    assert rep["exit_code"] == 1


def test_awaiting_calibration_is_unexplained_unless_auto_detection_reproduces_v1(hub, tmp_path):
    hub.gc1()
    hub.gc2()
    src = tmp_path / "gc2src"
    src.mkdir()
    cdf = fx.sample_cdf(src / "a.CDF", name="40401", method_name=SIMDIS)
    load_folder("gc2", src, backfill=False, db=hub.db, data_dir=hub.data, conf=hub.conf)
    hub.worker().run_until_idle()
    row = v1_row(hub, cdf, instrument="gc2", auto=True)
    row["2887 T50"] = str(float(row["2887 T50"]) + 2)
    v1 = write_v1_csv(tmp_path / "v1.csv", [row])
    rep = parity_report("gc2", v1, db=hub.db, out_dir=tmp_path / "out", conf=hub.conf,
                        v1_corrections=hub.corrections)
    (d,) = rep["differences"]
    assert d["tag"] == "unexplained" and "awaiting_calibration" in d["detail"]


def test_an_unprocessed_sample_fails_the_report(hub, tmp_path):
    hub.gc1()
    src = tmp_path / "src2"
    src.mkdir()
    cdf = fx.sample_cdf(src / "a.CDF", name="40401", method_name=SIMDIS)
    load_folder("gc1", src, backfill=False, db=hub.db, data_dir=hub.data, conf=hub.conf)
    v1 = write_v1_csv(tmp_path / "v1.csv", [v1_row(hub, cdf)])
    rep = parity_report("gc1", v1, db=hub.db, out_dir=tmp_path / "out", conf=hub.conf)
    (d,) = rep["differences"]
    assert d["tag"] == "not-processed" and "received" in d["detail"]
    assert rep["exit_code"] == 1


def test_when_v1s_blank_is_unknown_a_candidate_blank_must_reproduce_v1(hub, tmp_path):
    """No blank row precedes the sample in the v1 CSV (v1 may have had a cached
    blank). v1 used an earlier genuine blank B0; the hub uses B1 (the latest at
    or before). Recomputing with B0 reproduces v1's row: the blank rule, and
    the detail names B0. A blank that isn't in the hub proves nothing."""
    hub.gc1()
    src = tmp_path / "src3"
    src.mkdir()
    b0 = plain_blank(src / "b0.CDF", at(8, 0, 27), ramp=9.0, offset=60.0)
    fx.blank_cdf(src / "b1.CDF", injected=at(9, 0, 27), method_name=SIMDIS)
    s = fx.sample_cdf(src / "s.CDF", name="40404", injected=at(14, 23), method_name=SIMDIS)
    load_folder("gc1", src, backfill=False, db=hub.db, data_dir=hub.data, conf=hub.conf)
    hub.worker().run_until_idle()
    b0_id = store.samples.find_by_key("gc1", "Blank", "2026-09-25 08:00:27", db=hub.db)["id"]
    rows = [v1_row(hub, s, blank=b0),
            v1_row(hub, hub.data / store.samples.find_by_key(
                "gc1", "Blank", "2026-09-25 08:00:27", db=hub.db)["cdf_path"])]
    rows[1].update({"Lab ID": "Blank", "InjectionDateTime": "2026-09-25 08:00:27"})
    b1s = store.samples.find_by_key("gc1", "Blank", "2026-09-25 09:00:27", db=hub.db)
    rows.append(v1_row(hub, hub.data / b1s["cdf_path"]))
    rows[2].update({"InjectionDateTime": "2026-09-25 09:00:27"})
    v1 = write_v1_csv(tmp_path / "v1.csv", rows)
    rep = parity_report("gc1", v1, db=hub.db, out_dir=tmp_path / "out", conf=hub.conf,
                        v1_corrections=hub.corrections)
    diffs = [d for d in _by(rep, "40404") if d["tag"] != "source-file"]
    assert diffs and _tags(diffs) == {"blank-rule"}
    assert all(f"blank sample {b0_id}" in d["detail"] for d in diffs)
    assert rep["exit_code"] == 0, rep["summary"]

    outside = plain_blank(tmp_path / "old_blank.CDF", at(7, 0, 27), ramp=12.0, offset=70.0)
    v1b = write_v1_csv(tmp_path / "v1b.csv", [v1_row(hub, s, blank=outside)] + rows[1:])
    rep = parity_report("gc1", v1b, db=hub.db, out_dir=tmp_path / "out", conf=hub.conf,
                        v1_corrections=hub.corrections)
    diffs = [d for d in _by(rep, "40404") if d["tag"] != "source-file"]
    assert diffs and _tags(diffs) == {"unexplained"}
    assert rep["exit_code"] == 1


# ── the CLI ─────────────────────────────────────────────────────────────────

def test_cli(scenario):
    env = dict(os.environ)
    env.pop("GC_DATA_DIR", None)
    (scenario.hub.data / "settings.json").write_text(json.dumps(scenario.hub.conf), encoding="utf-8")
    res = subprocess.run([sys.executable, str(CLI), "--data-dir", str(scenario.hub.data),
                          "--v1-corrections", str(scenario.hub.corrections),
                          "--out-dir", str(scenario.out), "gc1", str(scenario.csv)],
                         capture_output=True, text=True, env=env, timeout=120)
    assert res.returncode == 1, res.stderr
    assert "unexplained: 1" in res.stdout
    assert list(scenario.out.glob("*.html")) and list(scenario.out.glob("*.csv"))
    bad = subprocess.run([sys.executable, str(CLI), "--data-dir", str(scenario.hub.data), "gc1",
                          str(scenario.hub.root / "missing.csv")],
                         capture_output=True, text=True, env=env, timeout=120)
    assert bad.returncode == 2
