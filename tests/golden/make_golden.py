"""Produce the golden rows in tests/golden/: what v1.0.0's distill.process_cdf
writes for

* ``d2887_rows.json`` (``CASES``): the synthetic fixtures in tests/cdf_fixtures.py;
* ``d2887_bestfit_rows.json`` (``BESTFIT_CASES``): the same with the fuel-type
  best fit on, against a one-file standards folder;
* ``d2887_snapshot_rows.json`` (``SNAPSHOT_CASES``): the real calibration run
  (``Feb24 n-alkanes.CDF``, the D2887-12 standard, auto-detected) and the
  real blank from the 2026-09-25 share snapshot. Written only when the
  snapshot exists (``SNAPSHOT_DIR``); its test skips otherwise.

The rows must come from the code they pin (v1.0.0, before the phase 2
``distill`` refactor). Run from the repo root against an export of it:

    git archive v1.0.0 | tar -x -C /tmp/gc-v1
    GC_GOLDEN_CODE_ROOT=/tmp/gc-v1 .venv/bin/python tests/golden/make_golden.py

(the fixtures and this script come from the checkout; distill, settings and
fuel_fit from ``GC_GOLDEN_CODE_ROOT``). tests/test_distill_golden.py reruns
the same cases through ``run_all`` and compares them with the committed
JSON, string for string. Everything the cases need is copied into a temp dir
(including the correction factors, embedded below), so nothing on the lab
share or in the snapshot is read in place or modified.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

if __name__ == "__main__":
    # Same isolation the test suite's conftest applies: these would redirect
    # settings/paths at import time or override the calibration CDF.
    for _name in ("PORT", "GC_PORT", "GC_DATA_DIR", "GC_CAL_CDF"):
        os.environ.pop(_name, None)

GOLDEN_DIR = Path(__file__).resolve().parent
TESTS_DIR = GOLDEN_DIR.parent
REPO_ROOT = TESTS_DIR.parent
for _p in (REPO_ROOT, TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if __name__ == "__main__" and os.environ.get("GC_GOLDEN_CODE_ROOT"):
    sys.path.insert(0, os.environ["GC_GOLDEN_CODE_ROOT"])

import cdf_fixtures as fx  # noqa: E402
import distill  # noqa: E402
import settings  # noqa: E402

GOLDEN_JSON = GOLDEN_DIR / "d2887_rows.json"
BESTFIT_JSON = GOLDEN_DIR / "d2887_bestfit_rows.json"
SNAPSHOT_JSON = GOLDEN_DIR / "d2887_snapshot_rows.json"

# The share snapshot sits next to this checkout (~/Projects/); override with
# GC_SNAPSHOT_DIR. Resolved from the repo path, not Path.home(): the test
# suite points HOME at a temp dir.
SNAPSHOT_DIR = Path(os.environ.get("GC_SNAPSHOT_DIR")
                    or REPO_ROOT.parent / "gc-share-snapshot-2026-09-25")
SNAPSHOT_CAL = SNAPSHOT_DIR / "cdf" / "Feb24 n-alkanes.CDF"
SNAPSHOT_BLANK = SNAPSHOT_DIR / "cdf" / "processed-sample" / "Blank_09242026_153027.CDF"
EXCLUDED_COLUMNS = ("Source File",)   # holds temp paths

# Phase 1 shape of the EQM correction factors file. The five non-zero values
# are the "Agilent GC" section of
# ~/Projects/gc-share-snapshot-2026-09-25/ref/correction_factors.json (which
# lists only those five cuts); the other six cuts are 0, which distill treats
# exactly like an absent cut.
D86_CORRECTIONS = {
    "IBP - D86": -12.08,
    "5% - D86": 0.0,
    "10% - D86": -5.25,
    "20% - D86": 0.0,
    "30% - D86": 0.0,
    "50% - D86": -4.06,
    "70% - D86": 0.0,
    "80% - D86": 0.0,
    "90% - D86": -3.46,
    "95% - D86": 0.0,
    "FBP - D86": -5.57,
}

SOLVENT_RT = 0.30
IMPURITY_RT = 1.13

# name -> (sample key, blank key or None, settings overrides)
CASES = {
    "sample_40304_noblank": ("40304", None, {}),
    "sample_40304_blank": ("40304", "blank", {}),
    "sample_40305_blank": ("40305", "blank", {}),
    "auto_detect_no_assignments": ("40304", "blank", {"calibration_assignments": ""}),
    "no_corrections": ("40304", "blank", {"correction_factors_json": ""}),
}
BESTFIT_CASES = {
    "bestfit_40304_blank": ("40304", "blank", {"bestfit_enabled": "true"}),
    "bestfit_40305_noblank": ("40305", None, {"bestfit_enabled": "true"}),
}
# The snapshot has one real calibration run and one real blank, no assigned
# peaks: the calibration is auto-detected, and the D2887-12 standard itself
# is the real chromatogram processed as a sample.
SNAPSHOT_CASES = {
    "snapshot_std_blank": ("snapshot_std", "snapshot_blank", {}),
    "snapshot_std_noblank": ("snapshot_std", None, {}),
    "snapshot_40304_blank": ("40304", "snapshot_blank", {}),
}
ALL_CASES = {**CASES, **BESTFIT_CASES, **SNAPSHOT_CASES}


# v7.0.0 "Corrected D86 never decreases": the D86 columns (the corrected,
# reported series) in percent order. The golden JSON stays v1.0.0's output;
# the hub's row is that row with this one documented change applied.
D86_COLUMNS = ("D86 IBP", "D86 T5", "D86 T10", "D86 T20", "D86 T30", "D86 T40", "D86 T50",
               "D86 T60", "D86 T70", "D86 T80", "D86 T90", "D86 T95", "D86 FBP")


def v7_expected(row: dict) -> dict:
    """The hub's row for a golden (v1.0.0) row: every D86 cell below an earlier
    D86 cell is replaced by the highest earlier cell's string (a running
    maximum, IBP → FBP). Written here on the strings, independently of
    ``distill.monotonic_d86``; nothing else in the row changes."""
    out = dict(row)
    best = None                       # (float, string) of the highest cell so far
    for col in D86_COLUMNS:
        cell = out.get(col, "")
        if cell == "":
            continue
        if best is not None and float(cell) < best[0]:
            out[col] = best[1]
        else:
            best = (float(cell), cell)
    return out


def snapshot_available() -> bool:
    return SNAPSHOT_CAL.is_file() and SNAPSHOT_BLANK.is_file()


def correction_factors_doc() -> dict:
    return {"Agilent GC": {
        test: {"equipment": "Agilent GC", "test": test, "correction_value": value}
        for test, value in D86_CORRECTIONS.items()
    }}


def calibration_entries() -> list:
    """What the Calibration page saves: every detected peak, the solvent and
    the impurity ignored, the ladder assigned C5..C44."""
    entries = [{"rt": SOLVENT_RT, "ignore": True}, {"rt": IMPURITY_RT, "ignore": True}]
    entries += [{"rt": rt, "carbon": c}
                for rt, c in zip(fx.ladder_times(), distill.N_ALKANE_CARBON[:20])]
    return sorted(entries, key=lambda e: e["rt"])


def build_inputs(root: Path) -> dict:
    """Write the calibration, blank, sample CDFs, corrections and the one-file
    standards folder under root/src; copy the snapshot's CDFs there too when
    it exists."""
    src = root / "src"
    src.mkdir(parents=True)
    cal = fx.calibration_cdf(src / "CAL_09162026_090000.CDF")
    corr = src / "correction_factors.json"
    corr.write_text(json.dumps(correction_factors_doc(), indent=2), encoding="utf-8")
    standards = src / "standards_one"
    standards.mkdir()
    # A diesel-like envelope close to (not equal to) the samples'.
    fx.sample_cdf(standards / "Diesel Std.CDF", name="Diesel Std", shift=0.1)
    inputs = {
        "cal": cal,
        "blanks": {"blank": fx.blank_cdf(src / "Blank_09242026_153027.CDF")},
        "corrections": corr,
        "standards_one": standards,
        "samples": {
            "40304": fx.sample_cdf(src / "40304.CDF"),
            "40305": fx.sample_cdf(src / "40305.CDF", name="40305", shift=0.4),
        },
    }
    if snapshot_available():
        snap = src / "snapshot"
        snap.mkdir()
        inputs["snapshot_cal"] = Path(shutil.copy2(SNAPSHOT_CAL, snap / SNAPSHOT_CAL.name))
        inputs["blanks"]["snapshot_blank"] = Path(shutil.copy2(SNAPSHOT_BLANK, snap / SNAPSHOT_BLANK.name))
        inputs["samples"]["snapshot_std"] = inputs["snapshot_cal"]
    return inputs


def case_paths(inputs: dict, name: str) -> tuple:
    """``(sample CDF, blank CDF or None)`` for a case (the originals; copy the
    sample before handing it to process_cdf, which moves it)."""
    sample_key, blank_key, _ = ALL_CASES[name]
    return inputs["samples"][sample_key], (inputs["blanks"][blank_key] if blank_key else None)


def case_conf(root: Path, inputs: dict, name: str) -> dict:
    """The settings.json for one case (every path inside root)."""
    case_dir = root / name
    conf = {
        "calibration_cdf": str(inputs["cal"]),
        "calibration_assignments": distill.upsert_assignments(
            "", inputs["cal"], calibration_entries()),
        "correction_factors_json": str(inputs["corrections"]),
        "distill_output": str(case_dir / "distill_results.csv"),
        "processed_cdf_dir": str(case_dir / "processed"),
        "comparison_defaults_dir": str(root / "standards"),
        "bestfit_enabled": "false",
    }
    if name in BESTFIT_CASES:
        conf["comparison_defaults_dir"] = str(inputs["standards_one"])
    if name in SNAPSHOT_CASES:
        conf["calibration_cdf"] = str(inputs["snapshot_cal"])
        conf["calibration_assignments"] = ""
    conf.update(ALL_CASES[name][2])
    return conf


def _clear_distill_caches() -> None:
    distill._SETTINGS_CACHE = None
    distill._SETTINGS_MTIME = None
    with distill._CAL_LOCK:
        distill._CAL_CACHE.clear()


def run_case(root: Path, inputs: dict, name: str) -> dict:
    """Run one case through process_cdf; return its CSV row minus EXCLUDED_COLUMNS.

    Points settings.CONFIG_PATH at the case's settings file; the caller
    restores it."""
    src, blank = case_paths(inputs, name)
    case_dir = root / name
    (case_dir / "in").mkdir(parents=True)
    settings_path = case_dir / "settings.json"
    settings_path.write_text(json.dumps(case_conf(root, inputs, name), indent=2), encoding="utf-8")
    cdf = case_dir / "in" / src.name
    shutil.copyfile(src, cdf)          # process_cdf moves its input

    settings.CONFIG_PATH = settings_path
    _clear_distill_caches()
    distill.process_cdf(cdf, blank_path=blank)

    with (case_dir / "distill_results.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) != 1:
        raise AssertionError(f"{name}: expected 1 CSV row, got {len(rows)}")
    return {k: v for k, v in rows[0].items() if k not in EXCLUDED_COLUMNS}


def run_all(root: Path, cases=None) -> dict:
    """{case name: row} for every case in ``cases`` (default ``CASES``),
    restoring settings and distill caches."""
    cases = CASES if cases is None else cases
    saved_config = settings.CONFIG_PATH
    try:
        inputs = build_inputs(root)
        return {name: run_case(root, inputs, name) for name in cases}
    finally:
        settings.CONFIG_PATH = saved_config
        _clear_distill_caches()


def _check_plausible(rows: dict) -> None:
    for name, row in rows.items():
        for prefix in ("2887", "D86"):
            ibp, t50, fbp = (float(row[f"{prefix} {k}"]) for k in ("IBP", "T50", "FBP"))
            if not ibp < t50 < fbp:
                raise AssertionError(f"{name}: {prefix} IBP/T50/FBP not increasing: {ibp}, {t50}, {fbp}")
        for k, v in row.items():
            if k.startswith(("2887 ", "D86 ")) and not v:
                raise AssertionError(f"{name}: {k} is empty")


def main() -> int:
    print(f"distill from {Path(distill.__file__).resolve()}")
    targets = [(GOLDEN_JSON, CASES), (BESTFIT_JSON, BESTFIT_CASES)]
    if snapshot_available():
        targets.append((SNAPSHOT_JSON, SNAPSHOT_CASES))
    else:
        print(f"No snapshot at {SNAPSHOT_DIR}; {SNAPSHOT_JSON.name} not written")
    for target, cases in targets:
        with tempfile.TemporaryDirectory(prefix="gc-golden-") as tmp:
            rows = run_all(Path(tmp), cases)
        _check_plausible(rows)
        target.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {len(rows)} cases to {target}")
        for name, row in rows.items():
            print(f"  {name}: 2887 IBP/T50/FBP {row['2887 IBP']}/{row['2887 T50']}/{row['2887 FBP']}"
                  f"  D86 IBP/T50/FBP {row['D86 IBP']}/{row['D86 T50']}/{row['D86 FBP']}"
                  f"  fit {row['Best Fit']!r} {row['Fit Score']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
