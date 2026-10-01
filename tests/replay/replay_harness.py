"""Differential replay: the hub's pipeline against the v1 production code.

The bar is the v1 code that ran on the lab share (``webapp-live`` in the
2026-09-25 share snapshot: ``distill.py``, ``looker.py``, ``fuel_fit.py``).
The same CDFs are fed, in the same arrival order, through

* **the hub**: a scratch data folder, ``store.migrate``, ``gc1`` bootstrapped
  from the calibration CDF and its peak assignments, gc1's corrections seeded
  from the phase-1 file (``instrument_admin.seed_gc1``), then
  ``pipeline.submit`` + the ``pipeline.Worker`` with its default (store-only)
  corrections provider, one file at a time. The output compared is the line
  the hub appends to the results CSV LEM tails (``export_rows``). Then every
  final sample goes through Export to LIMS, Re-process (recorded blank and
  corrections) and Re-process (current ones); each must write the same bytes;
* **v1**, in a subprocess (``v1_runner.py``) so its modules never meet the
  hub's: v1's own ingestion (``Looker._handle_new`` → ``process_cdf``, v1's
  blank choice), and ``process_cdf`` again with the blank the hub chose
  (identical inputs).

Every one of the 31 ``CSV_HEADER`` columns but ``Source File`` (the hub
writes its own stored path) is compared as the exact string each side
writes, and the line's bytes before it too. Both round to 2 dp, so equal
strings is the bar. Each comparison gets a verdict; only these are allowed
(``DOCUMENTED``), each a deliberate change named in docs/release-notes and,
where numbers differ, proven by re-running v1 with the hub's input:

=======================  =================================================
``match``                identical
``injection-time-fix``   v2.0.0 "Injection-time parse fix": v1 misread some
                         ANDI compact stamps on Python >= 3.11 (and wrote an
                         unrounded file time for a CDF without one); v1's
                         value must be the sample's ``legacy_injection_dt``
``lab-id-fallback``      v2.0.0 "Lab ID fallback": no / blank sample name →
                         the sender's file name (v1: its processed copy's stem)
``lab-id-strip``         v2.0.0 "Lab ID fallback": outer whitespace stripped
                         (allowed; today the exported line still carries
                         v1's unstripped name, so this never shows)
``blank-rule``           v2.0.0 "the blank injected at or before the sample",
                         "Wider blank names": v1 used another blank; v1 given
                         the hub's blank reproduces the hub
``late-blank-review``    v2.0.0: a blank arriving late flags final samples;
                         Re-process with the current blank uses it (proven)
``auto-detect-off``      v2.0.0 "Calibration auto-detection is off"
``calibration-filter``   v1.0.0: duplicate / out-of-order assignments are
                         dropped (v1 given the kept ones reproduces the hub)
``corrections-pending``  v2.0.0 "Stricter corrections": a corrections file the
                         hub can't use is never a silent zero
``method-excluded``,     v2.0.0 "Other methods are not processed" / no method
``review-method``        name is held for review
``truncated-refused``    v2.0.0 "Truncated CDFs are refused"
``no-new-row``           a duplicate file / same lab ID and time: neither side
                         writes a row
``both-failed``          neither side produces a result
``fp-platform``          only against a recording made on another machine:
                         a ``Case.fp_fragile`` case (a signal near the noise
                         floor) whose numbers moved by at most
                         ``FP_PLATFORM_TOL`` from floating-point differences
                         between CPUs. Live runs compare it exactly.
``v1-bug``               investigated, v1 wrong (``Case.v1_bug`` says why)
=======================  =================================================

Anything else is ``UNEXPLAINED``: a hub bug until shown otherwise.
"""
from __future__ import annotations

import csv
import io
import re
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import numpy as np

REPLAY_DIR = Path(__file__).resolve().parent
TESTS_DIR = REPLAY_DIR.parent
REPO_ROOT = TESTS_DIR.parent
for _p in (REPO_ROOT, TESTS_DIR, TESTS_DIR / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cdf_fixtures as fx  # noqa: E402
import distill  # noqa: E402

SNAPSHOT_NAME = "gc-share-snapshot-2026-09-25"


def snapshot_dir() -> Optional[Path]:
    """The share snapshot: ``GC_SNAPSHOT_DIR``, else a ``gc-share-snapshot-2026-09-25``
    beside the checkout or beside any folder above it (a git worktree lives
    under ``.claude/worktrees/``). Resolved from the repo path, never HOME
    (the suite redirects it). ``None`` when absent."""
    env = os.environ.get("GC_SNAPSHOT_DIR")
    candidates = [Path(env)] if env else [p / SNAPSHOT_NAME for p in REPO_ROOT.parents]
    for c in candidates:
        if (c / "webapp-live" / "distill.py").is_file():
            return c
    return None


SNAP = snapshot_dir()
V1_DIR = SNAP / "webapp-live" if SNAP else None
REAL_CAL = SNAP / "cdf" / "Feb24 n-alkanes.CDF" if SNAP else None
REAL_BLANK = SNAP / "cdf" / "processed-sample" / "Blank_09242026_153027.CDF" if SNAP else None
REAL_CORRECTIONS = SNAP / "ref" / "correction_factors.json" if SNAP else None
V1_RESULTS_CSV = SNAP / "webapp-live" / "distill_results.csv" if SNAP else None


def snapshot_available() -> bool:
    return bool(SNAP and REAL_CAL.is_file() and REAL_BLANK.is_file()
                and REAL_CORRECTIONS.is_file() and (V1_DIR / "looker.py").is_file())


SAMPLE_METHOD = "SIMDISB.M"
BLANK_METHOD = "SIMDISTB.M"
COLUMNS = list(distill.CSV_HEADER)
NUMERIC = [c for c in COLUMNS if c.startswith(("2887 ", "D86 "))]

# The phase-1 corrections file with all eleven cuts non-zero (one as text,
# which both v1 and the hub's seed accept).
ALL11_DOC = {"Agilent GC": {
    f"{cut} - D86": {"test": f"{cut} - D86", "correction_value": v}
    for cut, v in [("IBP", -12.08), ("5%", 1.5), ("10%", -5.25), ("20%", 2.25), ("30%", -0.75),
                   ("50%", -4.06), ("70%", "0.5"), ("80%", 3.1), ("90%", -3.46), ("95%", -1.2),
                   ("FBP", -5.57)]}}


# ── the suites: an axis, a baseline, a ladder ──────────────────────────────

@dataclass
class Suite:
    name: str
    t: np.ndarray                 # minutes, evenly spaced
    base: np.ndarray              # the blank's own signal on ``t`` (bleed + solvent)
    solvent_rt: float
    ladder_lo: float              # retention of the first anchor (u = 0)
    ladder_hi: float              # retention of the last anchor (u = 1)
    blank_path: Path              # the reference blank CDF
    blank_dt: datetime            # its injection time
    cal_path: Path
    cal_configs: dict             # name -> assignments list (None = no assignments)
    corrections_docs: dict        # name -> phase-1 corrections document (dict) or path
    cal_dt: str = ""              # the calibration run's injection time, as stamped

    def u(self, u: float) -> float:
        return self.ladder_lo + u * (self.ladder_hi - self.ladder_lo)


def real_suite(work: Path) -> Suite:
    """The snapshot's calibration run and blank (copied; never read in place twice)."""
    src = work / "inputs"
    src.mkdir(parents=True, exist_ok=True)
    cal = src / "Feb24 n-alkanes.CDF"
    blank = src / "Blank_09242026_153027.CDF"
    shutil.copy2(REAL_CAL, cal)
    shutil.copy2(REAL_BLANK, blank)
    t, y = distill._read_cdf(blank)
    peaks = [float(p) for p in distill.calibration_peak_times(cal)]
    carbons = list(distill.N_ALKANE_CARBON)
    # "auto_zip": every detected peak zipped with the ladder (v1's auto-detection, saved);
    # "operator": the first peak (CS2) ignored, the rest C5 upwards.
    zipped = [{"rt": rt, "carbon": c} for rt, c in zip(peaks, carbons)]
    operator = [{"rt": peaks[0], "ignore": True}] + [
        {"rt": rt, "carbon": c} for rt, c in zip(peaks[1:], carbons)]
    op = {e["carbon"]: e["rt"] for e in operator if "carbon" in e}
    configs = {
        "operator": operator,
        "auto_zip": zipped,
        "three": [{"rt": op[10], "carbon": 10}, {"rt": op[20], "carbon": 20},
                  {"rt": op[36], "carbon": 36}],
        "two": [{"rt": op[8], "carbon": 8}, {"rt": op[28], "carbon": 28}],
        "misassigned": misassigned(operator, peaks[0]),
        "none": None,
    }
    corr = src / "correction_factors.json"
    shutil.copy2(REAL_CORRECTIONS, corr)
    return Suite("real", t, np.asarray(y, float), solvent_rt=peaks[0], ladder_lo=op[5],
                 ladder_hi=max(op.values()), blank_path=blank,
                 blank_dt=datetime(2026, 9, 24, 15, 30, 27), cal_path=cal, cal_configs=configs,
                 corrections_docs={"ref": corr, "all11": ALL11_DOC, **BAD_CORRECTIONS},
                 cal_dt="2022-02-24 15:09:25")    # its stamp: 20220224150925+0000


def synthetic_suite(work: Path) -> Suite:
    """``tests/cdf_fixtures.py``'s calibration ladder and blank."""
    import make_golden
    src = work / "inputs"
    src.mkdir(parents=True, exist_ok=True)
    cal = fx.calibration_cdf(src / "CAL_09162026_090000.CDF", method_name=SAMPLE_METHOD)
    blank = fx.blank_cdf(src / "Blank_09242026_153027.CDF", method_name=BLANK_METHOD)
    t, y = distill._read_cdf(blank)
    lad = fx.ladder_times()
    entries = make_golden.calibration_entries()
    peaks = [float(p) for p in distill.calibration_peak_times(cal)]
    configs = {
        "operator": entries,
        "auto_zip": [{"rt": rt, "carbon": c} for rt, c in zip(peaks, distill.N_ALKANE_CARBON)],
        "three": [{"rt": lad[5], "carbon": 10}, {"rt": lad[13], "carbon": 20},
                  {"rt": lad[17], "carbon": 36}],
        "two": [{"rt": lad[3], "carbon": 8}, {"rt": lad[15], "carbon": 28}],
        "misassigned": misassigned(entries, 0.30),
        "none": None,
    }
    return Suite("synthetic", t, np.asarray(y, float), solvent_rt=0.30, ladder_lo=lad[0],
                 ladder_hi=lad[-1], blank_path=blank, blank_dt=datetime(2026, 9, 24, 15, 30, 27),
                 cal_path=cal, cal_configs=configs,
                 corrections_docs={"ref": make_golden.correction_factors_doc(), "all11": ALL11_DOC,
                                   **BAD_CORRECTIONS},
                 cal_dt="2026-09-16 09:00:00")    # cdf_fixtures.calibration_cdf


# ── the sample sweep ───────────────────────────────────────────────────────

def _g(t, c, h, w):
    return h * np.exp(-0.5 * ((t - c) / w) ** 2)


def hydrocarbon(s: Suite, t, u0, u1, height, *, peaks=True, skew=1.0):
    """A boiling-range envelope between ladder fractions u0..u1: a broad hump
    with n-alkane-like peaks riding on it."""
    a, b = s.u(u0), s.u(u1)
    x = np.clip((t - a) / max(b - a, 1e-9), 0.0, 1.0)
    hump = height * np.sin(np.pi * x ** skew) ** 1.5
    if peaks:
        step = (s.ladder_hi - s.ladder_lo) / 38.0
        for c in np.arange(a + step / 2, b, step):
            xc = np.clip((c - a) / max(b - a, 1e-9), 0, 1)
            hump = hump + _g(t, c, 0.7 * height * np.sin(np.pi * xc ** skew) ** 1.5, 0.006)
    return hump


@dataclass
class Case:
    name: str
    build: Callable            # (suite, t) -> y on t (base not included)
    lab: Optional[str] = None  # the CDF's sample_name (None: written as the case name)
    injected: datetime = datetime(2026, 9, 25, 14, 23, 0)
    raw_stamp: Optional[str] = None
    base_scale: float = 1.0     # times the blank's own signal
    run_frac: float = 1.0       # fraction of the blank's run length kept
    dt_factor: int = 1          # sample every n-th point (a coarser sampling)
    delay_shift_s: float = 0.0
    dtype: str = "f8"
    method: str = SAMPLE_METHOD
    mtime: Optional[float] = None   # the file's mtime (epoch), for a CDF with no stamp
    omit_name: bool = False     # no sample_name attribute at all
    fmt: str = "NETCDF3_CLASSIC"
    truncate: float = 0.0       # cut this fraction off the end of the file (an interrupted copy)
    source: Optional[str] = None    # "calibration": a copy of the suite's calibration run
    same_bytes_as: Optional[str] = None  # an identical copy of an earlier case's file
    note: str = ""
    v1_bug: str = ""            # a documented v1 defect explains this case's difference
    # A signal so close to the noise floor that the last bits of a sum move its
    # cut points by a hundredth of a degree between CPUs (Mac arm64 vs CI's
    # Linux x86-64, same numpy/scipy): exact live, ``fp-platform`` vs a recording.
    fp_fragile: bool = False
    # The InjectionDateTime the hub must write, known independently of the hub's
    # parser (default: ``injected``, when the stamp is written from it).
    expect_dt: Optional[str] = None
    configs: tuple = ("operator",)  # extra cal configs it also runs in (always "operator")


def _noise(t, sigma, seed):
    return np.random.RandomState(seed).normal(0.0, sigma, t.size)


def sweep() -> list[Case]:
    S = 3.0e4   # a typical envelope height (pA) over the real blank (~10 pA bleed)
    most = ("operator", "three", "two", "all11", "nofit", "misassigned")
    return [
        Case("diesel", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S),
             configs=most + ("auto_zip", "none") + CORRECTION_CONFIGS),
        Case("gasoline_light", lambda s, t: hydrocarbon(s, t, 0.0, 0.18, S), configs=most),
        Case("kerosene_narrow", lambda s, t: hydrocarbon(s, t, 0.15, 0.32, S), configs=most),
        Case("lube_heavy", lambda s, t: hydrocarbon(s, t, 0.55, 1.05, S), configs=most),
        Case("crude_wide", lambda s, t: hydrocarbon(s, t, -0.02, 1.08, S, skew=0.6),
             configs=most + ("auto_zip", "none") + CORRECTION_CONFIGS),
        Case("single_peak", lambda s, t: _g(t, s.u(0.4), S, 0.02), configs=most),
        Case("two_peaks", lambda s, t: _g(t, s.u(0.2), S, 0.02) + _g(t, s.u(0.7), S / 2, 0.02)),
        Case("bimodal_gas_diesel", lambda s, t: hydrocarbon(s, t, 0.0, 0.18, S)
             + hydrocarbon(s, t, 0.2, 0.6, 0.6 * S), configs=most),
        Case("noisy", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S) + _noise(t, 300.0, 1),
             configs=most),
        Case("very_noisy", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S / 10) + _noise(t, 800.0, 2)),
        Case("noise_only", lambda s, t: _noise(t, 50.0, 3)),
        Case("drift_up", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S) + 2000.0 * t / t[-1],
             configs=most),
        Case("drift_down", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S) - 1500.0 * t / t[-1]),
        Case("drift_quadratic", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S)
             + 3000.0 * (t / t[-1]) ** 2),
        Case("offset_negative", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S) - 500.0),
        Case("offset_high", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S) + 5000.0),
        Case("short_run_60pct", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S), run_frac=0.6,
             configs=most),
        Case("short_run_cuts_envelope", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S),
             run_frac=0.35),
        Case("saturating", lambda s, t: np.minimum(hydrocarbon(s, t, 0.15, 0.60, 20 * S), 2.0e5),
             configs=most),
        Case("saturating_solvent", lambda s, t: np.minimum(
            _g(t, s.solvent_rt, 5e6, 0.01) + hydrocarbon(s, t, 0.15, 0.60, S), 1.0e6)),
        Case("near_zero", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, 3.0)),
        Case("near_zero_noisy", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, 3.0) + _noise(t, 1.0, 4),
             fp_fragile=True),
        Case("flat_zero", lambda s, t: np.zeros_like(t), base_scale=0.0),
        Case("same_as_blank", lambda s, t: np.zeros_like(t)),
        Case("double_blank", lambda s, t: np.zeros_like(t), base_scale=2.0),
        Case("solvent_heavy", lambda s, t: _g(t, s.solvent_rt, 1.5e5, 0.008)
             + hydrocarbon(s, t, 0.15, 0.60, S), configs=most),
        Case("early_only",lambda s, t: _g(t, s.u(-0.01), S, 0.01)),
        Case("late_only", lambda s, t: hydrocarbon(s, t, 1.0, 1.1, S)),
        Case("spikes_on_diesel", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S)
             + sum(_g(t, s.u(u), 4 * S, 0.004) for u in (0.05, 0.08, 0.11))),
        Case("negative_spike", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S)
             - _g(t, s.u(0.3), 3 * S, 0.01)),
        Case("coarse_sampling_x10", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S), dt_factor=10),
        Case("coarse_sampling_x3", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S), dt_factor=3,
             configs=most),
        Case("delay_offset", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S), delay_shift_s=30.0),
        Case("float32", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S) + _noise(t, 20.0, 5),
             dtype="f4", configs=most),
        Case("big_numbers", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, 1.0e8)),
        Case("tiny_numbers", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, 1.0e-3), base_scale=1e-7),
        Case("blank_named_diesel", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S), lab="Blank",
             injected=datetime(2026, 9, 25, 14, 33, 0),
             note="named Blank but carries signal: never subtracted, never a reference blank"),
        Case("paren_blank_named", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S), lab="(Blank)",
             injected=datetime(2026, 9, 25, 14, 34, 0),
             note="v2.0.0 'Wider blank names': v1 blank-subtracted a '(Blank)' sample"),
        Case("lab_id_whitespace", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S), lab="  40310 ",
             injected=datetime(2026, 9, 25, 14, 35, 0)),
        Case("injected_before_blank", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S),
             injected=datetime(2026, 9, 23, 16, 40, 0),
             note="v2.0.0 blank rule: the hub has no blank injected at or before it"),
        Case("misparsed_stamp", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S),
             injected=datetime(2026, 9, 26, 0, 24, 50),
             note="v1 reads 20260926002450+0000 as 02:45:00"),
        Case("stamp_with_colon_zone", lambda s, t: hydrocarbon(s, t, 0.25, 0.5, S),
             raw_stamp="20260925143510+00:00", injected=datetime(2026, 9, 25, 14, 35, 10),
             expect_dt="2026-09-25 14:35:10"),
        Case("iso_stamp", lambda s, t: hydrocarbon(s, t, 0.25, 0.5, S),
             raw_stamp="2026-09-25 14:36:10", injected=datetime(2026, 9, 25, 14, 36, 10),
             expect_dt="2026-09-25 14:36:10"),
        Case("no_stamp_mtime", lambda s, t: hydrocarbon(s, t, 0.25, 0.5, S), raw_stamp="",
             mtime=datetime(2026, 9, 25, 14, 37, 10, 250000).timestamp(),
             expect_dt="2026-09-25 14:37:10"),   # the sender's file time, to the second
        # a fraction of 0.5 s or more: the hub truncates the file time, never rounds
        Case("no_stamp_mtime_late_fraction", lambda s, t: hydrocarbon(s, t, 0.25, 0.5, S),
             raw_stamp="", mtime=datetime(2026, 9, 25, 14, 38, 10, 750000).timestamp(),
             expect_dt="2026-09-25 14:38:10"),
        *stamp_cases(),
        Case("five_points", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S), run_frac=5e-4),
        Case("empty_signal", lambda s, t: np.zeros_like(t), run_frac=0.0),
        Case("long_run_130pct", lambda s, t: hydrocarbon(s, t, 0.15, 1.2, S), run_frac=1.3),
        Case("nan_point", lambda s, t: np.where(np.arange(t.size) == t.size // 2, np.nan,
                                                hydrocarbon(s, t, 0.15, 0.60, S)),
             injected=datetime(2026, 9, 25, 14, 38, 0)),
        Case("netcdf3_64bit", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S),
             fmt="NETCDF3_64BIT_OFFSET"),
        Case("netcdf4_hdf5", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S), fmt="NETCDF4_CLASSIC"),
        Case("truncated_copy", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S), truncate=0.05,
             note="v2.0.0 'Truncated CDFs are refused'"),
        Case("no_sample_name", lambda s, t: hydrocarbon(s, t, 0.25, 0.5, S), omit_name=True,
             injected=datetime(2026, 9, 25, 14, 39, 0)),
        Case("whitespace_name", lambda s, t: hydrocarbon(s, t, 0.25, 0.5, S), lab="   ",
             injected=datetime(2026, 9, 25, 14, 39, 30)),
        Case("csv_quoting_name", lambda s, t: hydrocarbon(s, t, 0.25, 0.5, S),
             lab='Sample "A", 2', injected=datetime(2026, 9, 25, 14, 40, 0)),
        Case("filename_unsafe_name", lambda s, t: hydrocarbon(s, t, 0.25, 0.5, S),
             lab="40311/A:B*?", injected=datetime(2026, 9, 25, 14, 41, 0)),
        Case("unicode_name", lambda s, t: hydrocarbon(s, t, 0.25, 0.5, S),
             lab="Échantillon µ-7", injected=datetime(2026, 9, 25, 14, 42, 0)),
        Case("other_method", lambda s, t: hydrocarbon(s, t, 0.0, 0.18, S), method="GASOLINE.M",
             injected=datetime(2026, 9, 25, 14, 43, 0), note="v2.0.0 'Other methods are not processed'"),
        Case("no_method", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S), method=None,
             injected=datetime(2026, 9, 25, 14, 44, 0), note="v2.0.0: no method name is held for review"),
        Case("method_with_path", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S),
             method="C:\\Chem32\\1\\METHODS\\simdisb.m", injected=datetime(2026, 9, 25, 14, 45, 0)),
        Case("duplicate_bytes", None, same_bytes_as="diesel"),
        Case("conflict_same_key", lambda s, t: hydrocarbon(s, t, 0.15, 0.62, S), lab="diesel"),
        Case("calibration_run_as_sample", None, source="calibration",
             note="the calibration run itself (real data in the real suite), injected before the blank"),
        Case("calibration_run_restamped", None, source="calibration", lab="CAL as sample",
             raw_stamp="20260925151000+0000", expect_dt="2026-09-25 15:10:00"),
        # a second, newer genuine blank, then samples after it
        Case("blank2", lambda s, t: 150.0 * (t / t[-1]) ** 2, lab="Blank2",   # more bleed
             method=BLANK_METHOD, injected=datetime(2026, 9, 25, 14, 50, 0)),
        Case("after_blank2", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S),
             injected=datetime(2026, 9, 25, 15, 0, 0), configs=most),
        Case("before_blank2_arriving_after", lambda s, t: hydrocarbon(s, t, 0.15, 0.60, S),
             injected=datetime(2026, 9, 25, 14, 46, 0),
             note="v2.0.0 blank rule: v1 used the newest blank processed, the hub the one "
                  "injected at or before"),
        # a genuine blank with heavy, slowly rising column bleed (a gentle ramp
        # passes the plausibility check and the height guard), then a weak
        # sample whose peaks all sit below that bleed: subtracting the blank
        # leaves nothing at all
        Case("bleed_blank", lambda s, t: bleed_ramp(s, t, 1000.0), lab="Blank3",
             method=BLANK_METHOD, injected=datetime(2026, 9, 25, 15, 20, 0)),
        Case("weak_sample_under_bleed", lambda s, t: weak_peaks(s, t, 900.0),
             injected=datetime(2026, 9, 25, 15, 25, 0),
             note="subtraction wipes the sample out (zero area): v1 computes it with no blank"),
        Case("weak_sample_partly_under_bleed",
             lambda s, t: weak_peaks(s, t, 900.0) + _g(t, s.u(0.05), 300.0, 0.01),
             injected=datetime(2026, 9, 25, 15, 26, 0)),
    ]


STAMP_MTIME = datetime(2026, 9, 25, 13, 59, 59, 125000).timestamp()


def stamp_cases() -> list[Case]:
    """Injection stamps: every hour of the compact ANDI form (v1's ``fromisoformat``
    misreads some), and the other forms GC files use, each with a known file
    time for when a stamp doesn't parse."""
    def body(s, t):
        return hydrocarbon(s, t, 0.25, 0.5, 3.0e4)

    out = []
    for day, hours in ((24, range(16, 24)), (25, range(0, 14))):
        for h in hours:
            when = datetime(2026, 9, day, h, 24, 50)
            out.append(Case(f"stamp_{day}_{h:02d}", body, injected=when, mtime=STAMP_MTIME))
    # (stamp, the time it means); one that doesn't parse means the file time
    # (STAMP_MTIME + i = 13:59:59.125 + i s, to the second)
    for i, (raw, means) in enumerate([
        ("20260925132450Z", "2026-09-25 13:24:50"),
        ("20260925132451", "2026-09-25 13:24:51"),
        ("20260925132452-0500", "2026-09-25 13:24:52"),          # the zone is dropped
        ("20260925132453 +0100", "2026-09-25 13:24:53"),
        ("25-Sep-2026 13:24:54", "2026-09-25 13:24:54"),
        ("09/25/2026 13:24:55", "2026-09-25 13:24:55"),
        ("2026-09-25T13:24:56+05:00", "2026-09-25 13:24:56"),
        ("2026-09-25 13:24:57.500", "2026-09-25 13:24:57.500000"),
        ("2026/09/25 13:24:58", "2026-09-25 13:24:58"),
        ("  20260925132459+0000  ", "2026-09-25 13:24:59"),
        ("not a date", "2026-09-25 14:00:09"),
        ("20260931132450+0000", "2026-09-25 14:00:10"),            # 31 September
    ]):
        out.append(Case(f"stamp_form_{i:02d}", body, raw_stamp=raw, mtime=STAMP_MTIME + i,
                        expect_dt=means, note=f"stamp {raw!r}"))
    return out


def bleed_ramp(s: Suite, t, level):
    """Column bleed rising gently from u = 0 to ``level`` at u = 0.6, then flat."""
    return level * np.clip((t - s.u(0.0)) / (s.u(0.6) - s.u(0.0)), 0.0, 1.0)


def weak_peaks(s: Suite, t, height):
    return sum(_g(t, s.u(u), height, 0.006) for u in (0.75, 0.8, 0.85, 0.9))


def expected_injection_dt(s: "Suite", case: Optional[Case]) -> Optional[str]:
    """The InjectionDateTime the hub must write for ``case``, independent of
    the hub's parser: ``expect_dt``, else the time the stamp was written from
    (for a copy of the calibration run, that run's own stamp)."""
    if case is None:
        return s.blank_dt.isoformat(sep=" ")
    if case.expect_dt is not None:
        return case.expect_dt
    if case.raw_stamp is not None:
        raise ValueError(f"case {case.name}: a raw stamp needs expect_dt")
    if case.source == "calibration":
        return s.cal_dt
    return case.injected.isoformat(sep=" ")


def build_case_cdf(s: Suite, case: Case, dest: Path) -> Path:
    if case.source == "calibration":
        shutil.copyfile(s.cal_path, dest)
        if case.raw_stamp is not None:
            from netCDF4 import Dataset
            with Dataset(dest, "a") as ds:
                ds.injection_date_time_stamp = case.raw_stamp
                if case.lab is not None:
                    ds.sample_name = case.lab
        return dest
    dt = float(s.t[1] - s.t[0])
    n = max(3, int(round(s.t.size * case.run_frac))) if case.run_frac else 0
    t = (s.t[0] + dt * np.arange(n))[::case.dt_factor]
    base = np.interp(t, s.t, s.base) if case.base_scale else np.zeros_like(t)
    # The blank's own signal (bleed + the CS2 solvent peak a sample shares) plus the case's.
    y = case.base_scale * base + case.build(s, t)
    t_written = t + case.delay_shift_s / 60.0
    name = None if case.omit_name else (case.name if case.lab is None else case.lab)
    fx.write_cdf(dest, t_written, y, name, case.injected, method_name=case.method,
                 raw_stamp=case.raw_stamp, dtype=case.dtype, fmt=case.fmt)
    if case.truncate:
        data = dest.read_bytes()
        dest.write_bytes(data[:int(len(data) * (1.0 - case.truncate))])
    if case.mtime is not None:
        os.utime(dest, (case.mtime, case.mtime))
    return dest


# ── configurations ─────────────────────────────────────────────────────────

CONFIGS = {
    # name: (calibration config, corrections doc, best fit on)
    "operator": ("operator", "ref", True),
    "three": ("three", "ref", True),
    "two": ("two", "ref", True),
    "all11": ("operator", "all11", True),
    "nofit": ("operator", "ref", False),
    "auto_zip": ("auto_zip", "ref", True),
    "none": ("none", "ref", True),
    # documented v1.0.0 change: duplicate / out-of-order assignments dropped
    "misassigned": ("misassigned", "ref", True),
    # documented v2.0.0 change: corrections the hub cannot use are never a silent zero
    "corr_null": ("operator", "null", True),
    "corr_missing": ("operator", "missing", True),
    "corr_huge": ("operator", "huge", True),
}
CORRECTION_CONFIGS = ("corr_null", "corr_missing", "corr_huge")

# One cut without a number: v1's loader fails on it and silently applies no
# correction at all.
NULL_DOC = {"Agilent GC": dict(ALL11_DOC["Agilent GC"],
                               **{"50% - D86": {"test": "50% - D86", "correction_value": None}})}
# A value beyond the hub's sanity bound (corrections.MAX_ABS_CORRECTION_C).
HUGE_DOC = {"Agilent GC": dict(ALL11_DOC["Agilent GC"],
                               **{"FBP - D86": {"test": "FBP - D86", "correction_value": 60.0}})}
MISSING = "<missing>"
BAD_CORRECTIONS = {"null": NULL_DOC, "missing": MISSING, "huge": HUGE_DOC}


def misassigned(entries: list, solvent_rt: float) -> list:
    """An operator's assignments with two mistakes v1 used as they were: the
    solvent given C20 (out of order) and C14 assigned twice."""
    out = [e for e in entries if e.get("rt") != solvent_rt]
    out.append({"rt": solvent_rt, "carbon": 20})
    c14 = next(e["rt"] for e in entries if e.get("carbon") == 14)
    c15 = next(e["rt"] for e in entries if e.get("carbon") == 15)
    out.append({"rt": round((c14 + c15) / 2, 4), "carbon": 14})
    return sorted(out, key=lambda e: e["rt"])


def hub_filtered(entries: list, cal: Path) -> list:
    """The assignments the hub keeps (``distill._assignment_pairs``): what v1
    is given to prove a ``calibration-filter`` difference."""
    amap = distill.parse_assignment_map(distill.upsert_assignments("", cal, entries))
    return [{"rt": rt, "carbon": c} for rt, c in distill._assignment_pairs(amap, cal)]


def standards(s: Suite, folder: Path) -> Path:
    """Three comparison standards on the suite's axis (the best fit's reference set)."""
    folder.mkdir(parents=True, exist_ok=True)
    S = 3.0e4
    for i, (name, y) in enumerate([
        ("Diesel #2", hydrocarbon(s, s.t, 0.15, 0.60, S)),
        ("ASTM Gasoline", hydrocarbon(s, s.t, 0.0, 0.18, S)),
        ("Jet A", hydrocarbon(s, s.t, 0.15, 0.32, S)),
    ]):
        fx.write_cdf(folder / f"{name}.CDF", s.t, s.base + y, name,
                     datetime(2026, 9, 1, 13, 10 + i, 0), method_name=SAMPLE_METHOD)
    return folder


@dataclass
class Item:
    id: str
    path: Path
    case: Optional[Case]
    expect_dt: Optional[str] = None   # expected_injection_dt: what the hub must write


def inputs(s: Suite, config: str, folder: Path, cases: list[Case]) -> list[Item]:
    """The arrival order: the reference blank first, then the config's cases."""
    folder.mkdir(parents=True, exist_ok=True)
    items = [Item("BLANK", folder / "Blank_09242026_153027.CDF", None,
                  expected_injection_dt(s, None))]
    shutil.copy2(s.blank_path, items[0].path)
    for case in cases:
        if config == "operator" or config in case.configs:
            dest = folder / f"{case.name}.CDF"
            if case.same_bytes_as:
                shutil.copy2(folder / f"{case.same_bytes_as}.CDF", dest)
            else:
                build_case_cdf(s, case, dest)
            items.append(Item(case.name, dest, case, expected_injection_dt(s, case)))
    return items


def base_conf(s: Suite, config: str, work: Path) -> dict:
    cal_cfg, corr_name, fit = CONFIGS[config]
    corr = s.corrections_docs[corr_name]
    if isinstance(corr, dict):
        path = work / f"corrections-{corr_name}.json"
        path.write_text(json.dumps(corr, indent=1), encoding="utf-8")
        corr = path
    elif corr == MISSING:
        corr = work / "no-such-correction-factors.json"
    entries = s.cal_configs[cal_cfg]
    return {
        "calibration_cdf": str(s.cal_path),
        "calibration_assignments": (distill.upsert_assignments("", s.cal_path, entries)
                                    if entries is not None else ""),
        "calibration_sensitivity": "50",
        "correction_factors_json": str(corr),
        "comparison_defaults_dir": str(work.parent / "standards"),
        "bestfit_enabled": "true" if fit else "false",
        "bestfit_threshold": "0.93",
        "bestfit_shift_tolerance_min": "0.05",
        "bestfit_mix_min_frac": "0.10",
        "analysis_x_max_min": "7.0",
        "blank_max_intensity_pa": "200",
    }


# ── the hub ────────────────────────────────────────────────────────────────

def run_hub(items: list[Item], conf: dict, root: Path) -> dict:
    """Every item through the hub's pipeline, in order; per item id:
    ``{status, error, line (the exported CSV line) , row (dict), blank (the
    chosen blank's item id), legacy_injection_dt, injection_dt, sample_id}``."""
    import instrument_admin
    import instruments
    import pipeline
    import store

    distill._CAL_CACHE.clear()
    data = root / "data"
    data.mkdir(parents=True)
    db = data / "gc.db"
    store.migrate(db)
    instruments.bootstrap_gc1(conf, db=db, now=datetime(2020, 1, 1))
    try:
        instrument_admin.seed_gc1(conf, by="replay", db=db)
    except instrument_admin.AdminError as exc:    # gc1 then waits as pending_corrections
        seed_error = str(exc)
    else:
        seed_error = None
    worker = pipeline.Worker(db=db, data_dir=data, conf_fn=lambda: dict(conf))
    out: dict = {}
    sid_to_item: dict = {}

    def chosen_blank(rev) -> Optional[str]:
        """The item id of the blank the hub chose for a revision: the one it
        subtracted, or the one it rejected (the height guard) and recorded."""
        notes = rev.get("notes")
        notes = json.loads(notes) if isinstance(notes, str) and notes else (notes or {})
        chosen = rev.get("blank_used")
        if chosen is None and isinstance(notes, dict) and notes.get("blank_rejected"):
            chosen = notes["blank_rejected"]["sample_id"]
        return sid_to_item.get(chosen) if chosen is not None else None

    for it in items:
        rec: dict = {"status": None, "error": None, "line": None, "row": None, "blank": None,
                     "seed_error": seed_error}
        try:
            res = pipeline.submit("gc1", it.path, conf=dict(conf), data_dir=data, db=db)
        except pipeline.SubmitRejected as exc:
            rec.update(status="rejected", error=str(exc))
            out[it.id] = rec
            continue
        except Exception as exc:  # noqa: BLE001 - a crash is a finding, not a harness error
            rec.update(status="submit-crash", error=f"{type(exc).__name__}: {exc}")
            out[it.id] = rec
            continue
        if res.outcome != "created":       # a duplicate or a held conflict: nothing new
            rec.update(status=res.outcome, error=res.message)
            out[it.id] = rec
            continue
        sid_to_item[res.sample_id] = it.id
        try:
            worker.run_until_idle()
        except Exception as exc:  # noqa: BLE001
            rec.update(status="worker-crash", error=f"{type(exc).__name__}: {exc}")
            out[it.id] = rec
            continue
        s = store.samples.get(res.sample_id, db=db)
        rec.update(sample_id=res.sample_id, status=s["status"], error=s.get("error"),
                   injection_dt=s["injection_dt"], legacy_injection_dt=s.get("legacy_injection_dt"))
        rev = store.get_revision(res.sample_id, db=db)
        if rev is not None:
            rec["blank"] = chosen_blank(rev)
            rec["blank_applied"] = rev.get("blank_used") is not None
            rec["notes"] = rev.get("notes")
        lines = [r for r in store.export_rows.rows_after("gc1", 0, limit=100000, db=db)
                 if r["sample_id"] == res.sample_id]
        if lines:
            rec["line"] = lines[-1]["line"]
            rec["row"] = dict(zip(COLUMNS, next(csv.reader(io.StringIO(lines[-1]["line"])))))
        out[it.id] = rec

    # The other ways a result reaches the CSV: Export to LIMS (the stored
    # revision, no recompute), Re-process keeping the recorded blank and
    # corrections, and Re-process with the current ones. Each must write the
    # same bytes as the first export.
    def newest_line(sid):
        rows = [r for r in store.export_rows.rows_after("gc1", 0, limit=100000, db=db)
                if r["sample_id"] == sid]
        return rows[-1]["line"] if rows else None

    for rec in out.values():
        if rec.get("row") is None:
            continue
        sid = rec["sample_id"]
        rec["review_note"] = store.samples.get(sid, db=db).get("review_note")
        try:
            pipeline.export_to_lims(sid, by="replay", db=db, data_dir=data)
            rec["lims_line"] = newest_line(sid)
            pipeline.request_reprocess(sid, by="replay", db=db)
            worker.run_until_idle()
            rec["reprocess_line"] = newest_line(sid)
            pipeline.request_reprocess(sid, by="replay", use_current_blank=True,
                                       use_current_corrections=True, db=db)
            worker.run_until_idle()
            rec["reprocess_current_line"] = newest_line(sid)
            rec["current_blank"] = chosen_blank(store.get_revision(sid, db=db))
            rec["final_status"] = store.samples.get(sid, db=db)["status"]
        except Exception as exc:  # noqa: BLE001
            rec["second_pass_error"] = f"{type(exc).__name__}: {exc}"
    distill._CAL_CACHE.clear()
    return out


# ── v1 ─────────────────────────────────────────────────────────────────────

def run_v1(items: list[Item], conf: dict, hub: dict, root: Path) -> dict:
    """``v1_runner.py`` in a subprocess: the looker pass and the direct pass
    (each sample with the blank the hub chose)."""
    by_id = {it.id: it for it in items}
    job = {
        "v1_dir": str(V1_DIR),
        "work": str(root / "v1"),
        "conf": conf,
        "files": [{"id": it.id, "path": str(it.path)} for it in items],
        "direct": [],
    }

    def direct(entry_id, it, blank_id):
        job["direct"].append({"id": entry_id, "path": str(it.path), "blank_id": blank_id,
                              "blank": str(by_id[blank_id].path) if blank_id else None})

    for it in items:
        direct(it.id, it, hub[it.id].get("blank"))
    # A Re-process with the current blank that chose another blank (a newer
    # blank arrived late): v1 with that blank too.
    for it in items:
        h = hub[it.id]
        if h.get("row") is not None and h.get("current_blank") != h.get("blank"):
            direct(it.id + "#current", it, h.get("current_blank"))
    job_path, out_path = root / "v1-job.json", root / "v1-out.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "GC_CAL_CDF", "GC_DATA_DIR", "PORT", "GC_PORT")}
    proc = subprocess.run([sys.executable, str(REPLAY_DIR / "v1_runner.py"), str(job_path),
                           str(out_path)], cwd=str(root), env=env, capture_output=True,
                          text=True, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"v1 runner failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    out = json.loads(out_path.read_text(encoding="utf-8"))
    for entry, rec in zip(job["direct"], out["direct"]):
        rec["blank_id"] = entry["blank_id"]
    return {"looker": {r["id"]: r for r in out["looker"]},
            "direct": {r["id"]: r for r in out["direct"]}}


# ── recorded v1 output (the CI replay, where the share snapshot is absent) ─

_ABS_DIRS = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\s\\/'\"]+)+[\\/]")


def _scrub(lines: list) -> list:
    """v1's log lines without the folders in their paths (a recording holds
    no temp or user paths; the file names stay)."""
    return [_ABS_DIRS.sub("", line) for line in lines]


def compact_v1(v1: Optional[dict]) -> Optional[dict]:
    """What ``compare`` needs of a v1 run, small enough to commit: rows as
    lists without ``Source File``; the looker row only where it differs from
    the direct one."""
    if v1 is None:
        return None

    def row(r):
        return None if r is None else [r.get(c, "") for c in COLUMNS[:-1]]

    direct = {k: {"row": row(r["row"]), "line": _strip_source(r.get("line")) or None,
                  "blank_id": r["blank_id"], "errors": _scrub(r["errors"][-2:]),
                  "rows_added": r["rows_added"]}
              for k, r in v1["direct"].items()}
    looker = {}
    for k, r in v1["looker"].items():
        d = v1["direct"].get(k) or {}
        same = r["row"] is not None and r["row"] == d.get("row")
        looker[k] = {"same_as_direct": same, "row": None if same else row(r["row"]),
                     "blank": r["blank"], "errors": _scrub(r["errors"][-2:]),
                     "rows_added": r["rows_added"]}
    return {"looker": looker, "direct": direct}


def expand_v1(rec: Optional[dict]) -> Optional[dict]:
    """``compact_v1`` back into ``run_v1``'s shape (``Source File`` and the
    line's last field read as empty)."""
    if rec is None:
        return None

    def row(values):
        return None if values is None else dict(zip(COLUMNS, list(values) + [""]))

    direct = {k: dict(r, row=row(r["row"]), line=(r["line"] + ",") if r["line"] else None)
              for k, r in rec["direct"].items()}
    looker = {}
    for k, r in rec["looker"].items():
        values = rec["direct"][k]["row"] if r["same_as_direct"] else r["row"]
        looker[k] = dict(r, row=row(values))
    return {"looker": looker, "direct": direct}


# ── comparison ─────────────────────────────────────────────────────────────

@dataclass
class Finding:
    suite: str
    config: str
    case: str
    verdict: str
    detail: str = ""
    columns: dict = field(default_factory=dict)   # column -> (v1, hub)


DOCUMENTED = {
    "match",               # identical (Source File aside)
    "injection-time-fix",  # v2.0.0 "Injection-time parse fix"
    "lab-id-strip",        # v2.0.0 "Lab ID fallback" (outer whitespace stripped)
    "lab-id-fallback",     # v2.0.0 "Lab ID fallback" (no/blank name: the sender's file name)
    "blank-rule",          # v2.0.0 "Blank subtraction ... at or before" / "Wider blank names"
    "late-blank-review",   # v2.0.0: a late blank flags final samples; Re-process (current) uses it
    "calibration-filter",  # v1.0.0: duplicate / unknown / out-of-order assignments dropped
    "corrections-pending",  # v2.0.0 "Stricter corrections": never a silent zero
    "auto-detect-off",     # v2.0.0 "Calibration auto-detection is off"
    "method-excluded",     # v2.0.0 "Other methods are not processed"
    "review-method",       # v2.0.0 "... a run with no method name is held for review"
    "truncated-refused",   # v2.0.0 "Truncated CDFs are refused"
    "no-new-row",          # a duplicate / same key: neither side writes a new row
    "both-failed",         # neither side produces a result
    "fp-platform",         # a Case.fp_fragile case vs a recording from another CPU, within tolerance
    "v1-bug",              # investigated: v1 is wrong, the hub right (Case.v1_bug says why)
}


# How far an ``fp_fragile`` case's numbers may move between CPUs (°F/°C, as
# printed); seen on CI: at most 0.05.
FP_PLATFORM_TOL = 0.1


def _within_fp_tolerance(left: dict) -> bool:
    for v1_val, hub_val in left.values():
        try:
            if abs(float(v1_val) - float(hub_val)) > FP_PLATFORM_TOL:
                return False
        except (TypeError, ValueError):
            return False
    return bool(left)


def _row_diff(v1: dict, hub: dict) -> dict:
    return {c: (v1.get(c, ""), hub.get(c, "")) for c in COLUMNS
            if c != "Source File" and v1.get(c, "") != hub.get(c, "")}


def _strip_source(line: Optional[str]) -> str:
    """The exact bytes of a CSV line before its last field (``Source File``,
    which may itself be quoted and hold commas)."""
    text = (line or "").rstrip("\r\n")
    if not text:
        return ""
    last = next(csv.reader(io.StringIO(text)))[-1]
    for k, ch in enumerate(text):
        if ch == "," and next(csv.reader(io.StringIO(text[k + 1:])), [""]) == [last]:
            return text[:k]
    raise ValueError(f"cannot find the last field of {text!r}")


def _explain_columns(diff: dict, hub_rec: dict, case: Optional[Case], item: Item) -> tuple[list, dict]:
    """Split a column diff into documented verdicts and what's left."""
    verdicts, left = [], dict(diff)
    if "InjectionDateTime" in left:
        v1_val, hub_val = left["InjectionDateTime"]
        if (item.expect_dt is not None and hub_val == item.expect_dt
                and v1_val == (hub_rec.get("legacy_injection_dt") or "")):
            verdicts.append("injection-time-fix")
            left.pop("InjectionDateTime")
    if "Lab ID" in left and case is not None:
        v1_val, hub_val = left["Lab ID"]
        raw = None if case.omit_name else (case.name if case.lab is None else case.lab)
        if raw is not None and raw.strip() and v1_val == raw and hub_val == raw.strip():
            verdicts.append("lab-id-strip")
            left.pop("Lab ID")
        elif (raw is None or not raw.strip()) and hub_val == item.path.stem:
            verdicts.append("lab-id-fallback")
            left.pop("Lab ID")
    return verdicts, left


_HOLDS = {"other_method": "method-excluded", "review_method": "review-method"}


def compare(suite: str, config: str, items: list[Item], hub: dict, v1: dict,
            proof: Optional[dict] = None, cross_platform: bool = False) -> list[Finding]:
    out: list[Finding] = []
    auto_off = CONFIGS[config][0] == "none"
    for it in items:
        case = it.case
        name = it.id
        h, lk, dr = hub[name], v1["looker"][name], v1["direct"][name]

        def add(verdict, detail="", columns=None):
            out.append(Finding(suite, config, name, verdict, detail, columns or {}))

        hub_ok = h.get("row") is not None
        status = h.get("status")
        if hub_ok and h["row"].get("InjectionDateTime") != it.expect_dt:
            add("UNEXPLAINED", "the hub wrote the wrong injection time",
                {"InjectionDateTime": (it.expect_dt, h["row"].get("InjectionDateTime"))})
            continue
        if dr.get("blank_id") != h.get("blank"):
            add("UNEXPLAINED", f"v1 was run with blank {dr.get('blank_id')}, the hub chose "
                               f"{h.get('blank')} (a recording made before a change?)")
            continue

        # Documented: the hub holds or refuses what v1 processed.
        if status in _HOLDS and lk["row"] is not None:
            add(_HOLDS[status], f"hub {status}: {h.get('error')}")
            continue
        if status == "pending_corrections" and h.get("seed_error") and lk["row"] is not None:
            add("corrections-pending", f"the hub would not seed gc1 ({h['seed_error']}); "
                                       f"v1 applied what it could read")
            continue
        if status == "rejected" and "truncated" in (h.get("error") or "") and lk["row"] is not None:
            add("truncated-refused", f"hub refused it ({h['error']}); v1 read the missing data")
            continue
        if status in ("duplicate", "conflict"):
            add("no-new-row" if lk["row"] is None else "UNEXPLAINED",
                f"hub {status}: {h.get('error')}; v1 rows added: {lk['rows_added']}")
            continue
        if auto_off:
            if status == "awaiting_calibration" and lk["row"] is not None:
                add("auto-detect-off", f"hub: {h['error']}")
            elif not hub_ok and lk["row"] is None:
                add("both-failed", f"hub {status}: {h['error']}; v1: {lk['errors'][-1:]}")
            else:
                add("UNEXPLAINED", f"hub {status} ({h['error']}), v1 row "
                                   f"{'present' if lk['row'] else 'absent'}")
            continue

        # 1. identical inputs: the hub vs v1 given the hub's blank
        if not hub_ok and dr["row"] is None:
            add("both-failed", f"hub {status}: {h['error']}; v1: {dr['errors'][-1:]}")
            continue
        if hub_ok != (dr["row"] is not None):
            detail = (f"hub {status}: {h['error']}" if not hub_ok else f"v1: {dr['errors'][-2:]}")
            if case is not None and case.v1_bug and hub_ok:
                add("v1-bug", f"{case.v1_bug} [{detail}]")
            else:
                add("UNEXPLAINED", f"only {'the hub' if hub_ok else 'v1'} produced a row; {detail}")
            continue
        diff = _row_diff(dr["row"], h["row"])
        verdicts, left = _explain_columns(diff, h, case, it)
        if left and proof is not None:
            # v1.0.0: duplicate / out-of-order assignments are dropped. v1
            # given only the assignments the hub keeps reproduces the hub.
            p = proof["direct"].get(name) or {}
            if p.get("row") is not None and not _explain_columns(
                    _row_diff(p["row"], h["row"]), h, case, it)[1]:
                add("+".join(verdicts + ["calibration-filter"]),
                    "v1 given the assignments the hub keeps reproduces the hub", left)
                continue
        if left:
            if (cross_platform and case is not None and case.fp_fragile
                    and _within_fp_tolerance(left)):
                add("+".join(verdicts + ["fp-platform"]),
                    f"within {FP_PLATFORM_TOL} of v1's recording from another machine", left)
            elif case is not None and case.v1_bug:
                add("v1-bug", case.v1_bug, left)
            else:
                add("UNEXPLAINED", "same inputs, different output", left)
            continue
        if not diff and _strip_source(dr.get("line")) != _strip_source(h.get("line")):
            add("UNEXPLAINED", "same values, different CSV bytes",
                {"line": (_strip_source(dr.get("line")), _strip_source(h.get("line")))})
            continue
        if h.get("second_pass_error"):
            add("UNEXPLAINED", f"Export to LIMS / Re-process failed: {h['second_pass_error']}")
            continue
        again = {k: (h["line"], h.get(k)) for k in ("lims_line", "reprocess_line",
                                                     "reprocess_current_line")
                 if h.get(k) != h["line"]}
        late = v1["direct"].get(name + "#current")
        if set(again) == {"reprocess_current_line"} and late is not None:
            # v2.0.0: a blank that arrives late flags the sample for review;
            # a Re-process with the current blank then uses it. Proven by v1
            # given that blank.
            cur_row = dict(zip(COLUMNS, next(csv.reader(io.StringIO(h["reprocess_current_line"])))))
            late_left = _explain_columns(_row_diff(late["row"] or {}, cur_row), h, case, it)[1]
            if late["row"] is not None and not late_left and h.get("review_note"):
                verdicts.append("late-blank-review")
                again = {}
        if again:
            add("UNEXPLAINED", "Export to LIMS / Re-process wrote different bytes", again)
            continue

        # 2. v1's own ingestion (its own blank choice)
        if lk["blank"] != h.get("blank"):
            same = lk["row"] is not None and not _row_diff(lk["row"], dr["row"])
            if not same:
                add("blank-rule", f"v1 used blank {lk['blank']}, the hub chose {h.get('blank')}; "
                                  f"v1 given the hub's blank reproduces the hub",
                    _row_diff(lk["row"] or {}, h["row"]))
                continue
            # the blank v1 chose changed nothing (e.g. refused by the height guard)
        elif (lk["row"] is None) != (dr["row"] is None) or (
                lk["row"] is not None and _row_diff(lk["row"], dr["row"])):
            lk_diff = _row_diff(lk["row"] or {}, dr["row"] or {})
            lk_verdicts, lk_left = _explain_columns(
                {c: (a, h["row"].get(c, "")) for c, (a, _b) in lk_diff.items()}, h, case, it)
            if lk["row"] is None or lk_left:
                add("UNEXPLAINED", "v1's own run differs from v1 given the same blank",
                    _row_diff(lk["row"] or {}, dr["row"] or {}))
                continue
            verdicts = sorted(set(verdicts) | set(lk_verdicts))
        add("match" if not verdicts else "+".join(verdicts))
    return out


def run_config(s: Suite, config: str, root: Path, cases: list[Case],
               recorded: Optional[dict] = None) -> tuple[list[Finding], dict]:
    """One configuration: the hub, then v1 live (``recorded`` None) or v1's
    recorded output for it (``recorded[config]``, from ``record``)."""
    root.mkdir(parents=True, exist_ok=True)
    items = inputs(s, config, root / "in", cases)
    conf = base_conf(s, config, root)
    hub = run_hub(items, conf, root / "hub")
    entries = s.cal_configs[CONFIGS[config][0]]
    needs_proof = entries is not None and hub_filtered(entries, s.cal_path) != [
        e for e in entries if "carbon" in e and not e.get("ignore")]
    if recorded is not None:
        v1 = expand_v1(recorded[config]["v1"])
        proof = expand_v1(recorded[config]["proof"])
    else:
        v1 = run_v1(items, conf, hub, root)
        proof = None
        if needs_proof:
            filtered = dict(conf, calibration_assignments=distill.upsert_assignments(
                "", s.cal_path, hub_filtered(entries, s.cal_path)))
            (root / "proof").mkdir()
            proof = run_v1(items, filtered, hub, root / "proof")
    missing = sorted({it.id for it in items} - set(v1["looker"]))
    if missing:
        raise AssertionError(f"{config}: no v1 output for {missing} (re-record: "
                             f"tests/replay/record_v1.py)")
    return (compare(s.name, config, items, hub, v1, proof, cross_platform=recorded is not None),
            {"hub": hub, "v1": v1, "items": items, "proof": proof})


def run_suite(s: Suite, work: Path, configs=None, cases=None,
              recorded: Optional[dict] = None) -> tuple[list[Finding], dict]:
    cases = cases if cases is not None else sweep()
    standards(s, work / "standards")
    findings, raw = [], {}
    for config in configs or CONFIGS:
        f, r = run_config(s, config, work / config, cases, recorded)
        findings += f
        raw[config] = r
    return findings, raw


def record(raw: dict) -> dict:
    """``run_suite``'s raw output → the committed recording of v1's side."""
    return {config: {"v1": compact_v1(r["v1"]), "proof": compact_v1(r["proof"])}
            for config, r in raw.items()}


def counts(findings: list[Finding]) -> dict:
    out: dict = {}
    for f in findings:
        out[f.verdict] = out.get(f.verdict, 0) + 1
    return out


def report(findings: list[Finding]) -> str:
    """One line per verdict with the cases it covers; the details of anything
    unexplained."""
    by_verdict: dict = {}
    for f in findings:
        by_verdict.setdefault(f.verdict, []).append(f"{f.config}/{f.case}")
    lines = [f"{len(findings)} comparisons ({findings[0].suite if findings else '-'} inputs):"]
    for verdict, where in sorted(by_verdict.items()):
        shown = ", ".join(where[:12]) + (f", +{len(where) - 12} more" if len(where) > 12 else "")
        lines.append(f"  {verdict}: {len(where)}" + ("" if verdict == "match" else f"  [{shown}]"))
    for f in unexplained(findings):
        lines.append(f"UNEXPLAINED {f.suite}/{f.config}/{f.case}: {f.detail}")
        for col, (a, b) in f.columns.items():
            lines.append(f"    {col}: v1={a!r} hub={b!r}")
    return "\n".join(lines)


def unexplained(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if not set(f.verdict.split("+")) <= DOCUMENTED]
