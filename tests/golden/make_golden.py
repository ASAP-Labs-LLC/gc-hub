"""Produce tests/golden/d2887_rows.json: the rows distill.process_cdf writes
for the synthetic fixtures in tests/cdf_fixtures.py.

Run once, on the code the golden file pins (v1.0.0, before the phase 2
``distill`` refactor), from the repo root:

    .venv/bin/python tests/golden/make_golden.py

tests/test_distill_golden.py reruns the same cases through ``run_all`` and
compares them with the committed JSON, string for string. Everything the
cases need is written into a temp dir (including the correction factors,
embedded below), so neither this script nor the test depends on the lab
share or a local snapshot.
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

import cdf_fixtures as fx  # noqa: E402
import distill  # noqa: E402
import settings  # noqa: E402

GOLDEN_JSON = GOLDEN_DIR / "d2887_rows.json"
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

# name -> (sample key, use the blank?, settings overrides)
CASES = {
    "sample_40304_noblank": ("40304", False, {}),
    "sample_40304_blank": ("40304", True, {}),
    "sample_40305_blank": ("40305", True, {}),
    "auto_detect_no_assignments": ("40304", True, {"calibration_assignments": ""}),
    "no_corrections": ("40304", True, {"correction_factors_json": ""}),
}


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
    """Write the calibration, blank, sample CDFs and corrections under root/src."""
    src = root / "src"
    src.mkdir(parents=True)
    cal = fx.calibration_cdf(src / "CAL_09162026_090000.CDF")
    corr = src / "correction_factors.json"
    corr.write_text(json.dumps(correction_factors_doc(), indent=2), encoding="utf-8")
    return {
        "cal": cal,
        "blank": fx.blank_cdf(src / "Blank_09242026_153027.CDF"),
        "corrections": corr,
        "samples": {
            "40304": fx.sample_cdf(src / "40304.CDF"),
            "40305": fx.sample_cdf(src / "40305.CDF", name="40305", shift=0.4),
        },
    }


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
    conf.update(CASES[name][2])
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
    sample_key, use_blank, _ = CASES[name]
    case_dir = root / name
    (case_dir / "in").mkdir(parents=True)
    settings_path = case_dir / "settings.json"
    settings_path.write_text(json.dumps(case_conf(root, inputs, name), indent=2), encoding="utf-8")
    src = inputs["samples"][sample_key]
    cdf = case_dir / "in" / src.name
    shutil.copyfile(src, cdf)          # process_cdf moves its input

    settings.CONFIG_PATH = settings_path
    _clear_distill_caches()
    distill.process_cdf(cdf, blank_path=inputs["blank"] if use_blank else None)

    with (case_dir / "distill_results.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) != 1:
        raise AssertionError(f"{name}: expected 1 CSV row, got {len(rows)}")
    return {k: v for k, v in rows[0].items() if k not in EXCLUDED_COLUMNS}


def run_all(root: Path) -> dict:
    """{case name: row} for every case, restoring settings and distill caches."""
    saved_config = settings.CONFIG_PATH
    try:
        inputs = build_inputs(root)
        return {name: run_case(root, inputs, name) for name in CASES}
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
    with tempfile.TemporaryDirectory(prefix="gc-golden-") as tmp:
        rows = run_all(Path(tmp))
    _check_plausible(rows)
    GOLDEN_JSON.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} cases to {GOLDEN_JSON}")
    for name, row in rows.items():
        print(f"  {name}: 2887 IBP/T50/FBP {row['2887 IBP']}/{row['2887 T50']}/{row['2887 FBP']}"
              f"  D86 IBP/T50/FBP {row['D86 IBP']}/{row['D86 T50']}/{row['D86 FBP']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
