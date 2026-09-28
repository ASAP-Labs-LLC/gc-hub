#!/usr/bin/env python3
"""
distill.py – ASTM D2887 simulated distillation + D86 correlation
----------------------------------------------------------------
Public API (unchanged so the UI & watcher continue to work):
    • cdf_metadata(path)          -> (sample_name, datetime)
    • gc_trace_from_cdf(path)     -> Plotly Scatter (time vs intensity)
    • process_cdf(path)           -> parse CDF & append CSV row, return new path

The code mirrors the workflow described in ASTM D2887‑23 (Proc‑A) and
ASTM D86‑23 Appendix X4.  No blank‑run subtraction is performed.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Sequence, Tuple

import numpy as np
from netCDF4 import Dataset
from plotly.graph_objects import Scatter
from scipy.interpolate import interp1d
from scipy.signal import find_peaks
from scipy.ndimage import minimum_filter1d

# ---------------------------------------------------------------------------
# Optional project‑level settings helper (silently mocked if absent)
# ---------------------------------------------------------------------------
try:
    import settings  # noqa: F401
except ModuleNotFoundError:  # stand‑alone usage
    class _FakeSettings:                   # type: ignore
        @staticmethod
        def load_settings() -> dict: return {}
    settings = _FakeSettings()             # type: ignore

import paths  # stdlib-only; where every on-disk state location lives

LOGGER = logging.getLogger("distill")
LOGGER.addHandler(logging.NullHandler())

_SETTINGS_CACHE: Dict[str, str] | None = None
_SETTINGS_MTIME: float | None = None
_SETTINGS_LOCK = threading.Lock()

# (resolved calibration path, assignment signature) -> (CDF mtime, function,
# anchors used). Keyed by signature too, so confs that share one calibration
# file but assign its peaks differently each keep their own entry instead of
# evicting another's. Least recently used first; at most
# CAL_CACHE_SIGNATURES_PER_PATH signatures are kept per path.
CAL_CACHE_SIGNATURES_PER_PATH = 8
_CAL_CACHE: "OrderedDict[Tuple[str, str], Tuple[float, Callable[[np.ndarray], np.ndarray], dict]]" = OrderedDict()
_CAL_LOCK = threading.Lock()
_NETCDF_LOCK = threading.Lock()
_CSV_LOCK = threading.Lock()  # guards all reads/writes to distill_results.csv

_round2 = lambda x: round(float(x), 2)          # 0.01 °C rounding

# ---------------------------------------------------------------------------
# CSV layout – must stay exactly the same for the UI
# ---------------------------------------------------------------------------
CSV_HEADER: list[str] = [
    "Lab ID",
    "InjectionDateTime",
    "2887 IBP", "2887 T5",  "2887 T10", "2887 T20", "2887 T30", "2887 T40",
    "2887 T50", "2887 T60", "2887 T70", "2887 T80", "2887 T90", "2887 T95", "2887 FBP",
    "D86 IBP",  "D86 T5",   "D86 T10",  "D86 T20",  "D86 T30",  "D86 T40",
    "D86 T50",  "D86 T60",  "D86 T70",  "D86 T80",  "D86 T90",  "D86 T95",  "D86 FBP",
    "Best Fit", "Fit Score",
    "Source File",
]
# interpolation levels – includes IBP (0.5%) and FBP (99.5%)
PERCENT_LEVELS: list[float] = [
    0.5, 5.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 95.0, 99.5
]

# n‑alkane normal‑boiling points (°C)  C5–C44
N_ALKANE_BP: list[float] = [
    36.0, 69.0, 98.0, 126.0, 151.0, 174.0, 196.0, 216.0, 254.0,
    271.0, 287.0, 302.0, 316.0, 344.0, 391.0, 431.0, 466.0, 496.0,
    522.0, 560.0,
]

# carbon numbers matching ``N_ALKANE_BP`` (C5→C44)
N_ALKANE_CARBON: list[int] = [
    5, 6, 7, 8, 9, 10, 11, 12,
    14, 15, 16, 17, 18, 20,
    24, 28, 32, 36, 40, 44,
]

# ASTM D86 App. X4 coefficients (Table X4.1) and relatives
_CONVERSION_COEFF: Dict[str, Tuple[float, float, float, float]] = {
    "IBP": (25.351, 0.32216, 0.71187, -0.04221),
    "5%": (18.822, 0.06602, 0.15803, 0.77898),
    "10%": (15.173, 0.20149, 0.30606, 0.48227),
    "20%": (13.141, 0.22677, 0.29042, 0.46023),
    "30%": (5.7766, 0.37218, 0.30313, 0.31118),
    "50%": (6.3753, 0.07763, 0.68984, 0.18302),
    "70%": (-2.8437, 0.16366, 0.42102, 0.38252),
    "80%": (-0.21536, 0.25614, 0.40925, 0.27995),
    "90%": (0.09966, 0.24335, 0.32051, 0.37357),
    "95%": (0.89880, -0.09790, 1.03816, -0.00894),
    "FBP": (19.444, -0.38161, 1.08571, 0.17729),
}

_CONVERSION_REL: Dict[str, Tuple[str, str, str]] = {
    "IBP": ("IBP", "5%", "10%"),
    "5%": ("IBP", "5%", "10%"),
    "10%": ("5%", "10%", "20%"),
    "20%": ("10%", "20%", "30%"),
    "30%": ("20%", "30%", "50%"),
    "50%": ("30%", "50%", "70%"),
    "70%": ("50%", "70%", "80%"),
    "80%": ("70%", "80%", "90%"),
    "90%": ("80%", "90%", "95%"),
    "95%": ("90%", "95%", "FBP"),
    "FBP": ("90%", "95%", "FBP"),
}

def _get_settings() -> Dict[str, str]:
    loader = getattr(settings, "load_settings", None)
    if loader is None:
        raise RuntimeError("settings.load_settings() is unavailable")
    config_path = getattr(settings, "CONFIG_PATH", None)
    mtime: float | None = None
    if config_path:
        try:
            cfg_path = Path(config_path)
            if cfg_path.exists():
                mtime = cfg_path.stat().st_mtime
        except Exception:  # noqa: BLE001
            mtime = None
    with _SETTINGS_LOCK:
        global _SETTINGS_CACHE, _SETTINGS_MTIME
        if _SETTINGS_CACHE is not None and mtime == _SETTINGS_MTIME:
            return dict(_SETTINGS_CACHE)
        conf = loader()
        if not isinstance(conf, dict):
            raise TypeError("settings.load_settings() must return a dict")
        _SETTINGS_CACHE = dict(conf)
        _SETTINGS_MTIME = mtime
        return dict(_SETTINGS_CACHE)

# ---------------------------------------------------------------------------
# NetCDF helpers
# ---------------------------------------------------------------------------
def _read_text(var) -> str:
    data = var[:]
    if hasattr(data, "tobytes"):
        txt = data.tobytes().decode("utf-8", "ignore")
    elif isinstance(data, bytes):
        txt = data.decode("utf-8", "ignore")
    elif getattr(data, "ndim", 1) == 0:
        txt = str(data.item())
    else:
        txt = str(data[0])
    return txt.replace("\x00", "").strip()


def _read_cdf(path: Path) -> Tuple[np.ndarray, np.ndarray]:
    """Thread-safe wrapper for NetCDF reads returning (time[min], intensity)."""
    with _NETCDF_LOCK:
        return _read_cdf_unlocked(path)


def _read_cdf_unlocked(path: Path) -> Tuple[np.ndarray, np.ndarray]:
    """Return (time[min], intensity) arrays from a NetCDF chromatogram."""
    with Dataset(path) as ds:
        vars_lc = {n.lower(): n for n in ds.variables}

        # intensity ----------------------------------------------------------
        for key in ("total_intensity", "intensity_values", "intensity", "ordinate_values"):
            if key in vars_lc:
                y = np.asarray(ds.variables[vars_lc[key]][:], float)
                break
        else:
            raise ValueError(f"{path.name}: intensity array missing")

        # time ---------------------------------------------------------------
        delay = 0.0
        if "actual_delay_time" in vars_lc:
            try:
                delay = float(ds.variables[vars_lc["actual_delay_time"]][0])
            except Exception:
                delay = 0.0

        for key in ("scan_acquisition_time", "time", "times", "abscissa_values", "retention_time"):
            if key in vars_lc:
                t = np.asarray(ds.variables[vars_lc[key]][:], float)
                break
        else:
            if "actual_sampling_interval" not in vars_lc:
                raise ValueError(f"{path.name}: no time axis")
            dt = float(ds.variables[vars_lc["actual_sampling_interval"]][0])
            t = np.arange(y.size) * dt
        t = t + delay

    return t / 60.0, y      # seconds → minutes (ASTM units)

# ---------------------------------------------------------------------------
# Calibration + maths
# ---------------------------------------------------------------------------
def _sensitivity_fracs(sensitivity: float) -> Tuple[float, float]:
    """Map a 0–100 sensitivity to (height_frac, prominence_frac).

    50 reproduces the original fixed thresholds (0.03 / 0.005). Each ±25 points
    scales the thresholds by 10× (log scale), so the full range spans 1e4×:
    at 100 the floor drops to ~0.03 % of the tallest peak — low enough to catch
    small, late-eluting alkanes that the solvent-peak-dominated default misses.
    Higher sensitivity → lower thresholds → more peaks detected.
    """
    s = float(min(100.0, max(0.0, sensitivity)))
    scale = 10.0 ** ((50.0 - s) / 25.0)
    return 0.03 * scale, 0.005 * scale


def _detect_nalkane_peaks(
    t: np.ndarray, y: np.ndarray, sensitivity: float = 50.0
) -> np.ndarray:
    height_frac, prominence_frac = _sensitivity_fracs(sensitivity)
    p, _ = find_peaks(
        y,
        height=y.max() * height_frac,
        prominence=y.max() * prominence_frac,
        distance=len(y) // 1500,
    )
    return np.sort(t[p])


def calibration_peak_times(cal_cdf: Path, sensitivity: float = 50.0) -> list[float]:
    """Return sorted retention times for calibration peaks."""
    t, y = _read_cdf(cal_cdf)
    return _detect_nalkane_peaks(t, y, sensitivity).tolist()


def build_calibration_from_anchors(
    rt: np.ndarray, bp: np.ndarray
) -> Callable[[np.ndarray], np.ndarray]:
    """Build a retention-time → boiling-point calibration from anchor pairs.

    ``rt``/``bp`` are matched arrays of anchor retention times (min) and their
    boiling points (°C). The interpolation order adapts to the number of
    anchors — cubic for ≥4, quadratic for 3, linear for 2 — because a cubic
    spline requires at least four points (assigning only 2–3 carbons must not
    crash the calibration). Linear extrapolation is used below the first anchor
    and above the 80th-percentile anchor to keep the edges physically sane.
    """
    rt = np.asarray(rt, float)
    bp = np.asarray(bp, float)
    idx = np.argsort(rt)
    rt_sorted = rt[idx]
    bp_sorted = bp[idx]

    n = len(rt_sorted)
    if n < 2:
        const = float(bp_sorted[0]) if n else 0.0
        return lambda rt_arr: np.full(np.asarray(rt_arr, float).shape, const)

    kind = "cubic" if n >= 4 else ("quadratic" if n == 3 else "linear")
    cubic = interp1d(rt_sorted, bp_sorted, kind=kind, fill_value="extrapolate")

    high_idx = int(0.8 * len(rt_sorted))
    if high_idx < len(rt_sorted) - 1:
        hi_rt = rt_sorted[high_idx:]
        hi_bp = bp_sorted[high_idx:]
        linear_hi = interp1d(hi_rt, hi_bp, kind="linear", fill_value="extrapolate")
    else:
        linear_hi = cubic

    # Linear extrapolation below C5 prevents cubic from producing unrealistically
    # low (or negative) temperatures before the first calibration peak.
    if len(rt_sorted) >= 2:
        linear_lo = interp1d(rt_sorted[:2], bp_sorted[:2], kind="linear", fill_value="extrapolate")
    else:
        linear_lo = cubic

    def _cal(rt_arr: np.ndarray) -> np.ndarray:
        rt_arr = np.asarray(rt_arr, float)
        out = cubic(rt_arr)
        # Below the first calibration point: clamp to the first anchor's BP.
        # Linear extrapolation put the start of the run at -155 °C.
        mask_lo = rt_arr < rt_sorted[0]
        if mask_lo.any():
            out[mask_lo] = bp_sorted[0]
        # Above 80th-percentile calibration point — use linear
        if linear_hi is not cubic:
            mask_hi = rt_arr > rt_sorted[high_idx]
            if mask_hi.any():
                out[mask_hi] = linear_hi(rt_arr[mask_hi])
        return out

    return _cal


def carbon_bp_map() -> Dict[int, float]:
    """Map each reference n-alkane carbon number to its boiling point (°C)."""
    return {c: bp for c, bp in zip(N_ALKANE_CARBON, N_ALKANE_BP)}


def _cal_key(cdf_path) -> str:
    """Canonical settings-map key for a calibration CDF path."""
    try:
        return str(Path(cdf_path).resolve())
    except Exception:  # noqa: BLE001
        return str(cdf_path)


def active_calibration_path(conf: Dict[str, str], *, honour_env: bool = True) -> Path | None:
    """Return the calibration CDF the distillation math will actually use.

    ``GC_CAL_CDF`` (an env override used for testing/ops) wins over the saved
    ``calibration_cdf`` setting unless ``honour_env`` is False (the hub, where
    one override would apply to every instrument). The distillation sites
    (``distillation_curve_from_cdf``, ``process_cdf``) and
    ``calibration_ladder`` all resolve the file through this one function, so
    the carbon-range labels shown on a chart always agree with the file the
    math used to build it. Returns ``None`` when nothing is configured.
    """
    env = os.environ.get("GC_CAL_CDF") if honour_env else None
    raw = env or conf.get("calibration_cdf") or ""
    raw = raw.strip()
    return Path(raw) if raw else None


def parse_assignment_map(raw: str) -> Dict[str, list]:
    """Parse the ``calibration_assignments`` settings JSON string.

    Returns ``{}`` for an empty string or any value that is not a JSON object
    — never raises, so a corrupt setting silently falls back to auto-detection.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _longest_increasing_by_carbon(
    pairs: list[Tuple[float, int]]
) -> list[Tuple[float, int]]:
    """Return the longest strictly-increasing-by-carbon run of ``pairs``
    (already sorted by retention time; carbons already de-duplicated, so no
    ties are possible). A single mis-assigned peak (e.g. the solvent wrongly
    given a high carbon number) should only cost that one entry, not every
    correct assignment after it — a greedy first-kept/drop-the-rest filter
    would throw away the whole correct run instead. Each dropped entry is
    logged individually so the operator can see which assignment was
    excluded and why. O(n^2), fine for the handful of calibration peaks this
    ever sees.
    """
    n = len(pairs)
    if n < 2:
        return pairs
    carbons = [c for _, c in pairs]
    lengths = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if carbons[j] < carbons[i] and lengths[j] + 1 > lengths[i]:
                lengths[i] = lengths[j] + 1
                prev[i] = j
    end = max(range(n), key=lambda i: lengths[i])
    keep: set[int] = set()
    i = end
    while i != -1:
        keep.add(i)
        i = prev[i]
    if len(keep) < n:
        for i in range(n):
            if i not in keep:
                rt, c = pairs[i]
                LOGGER.warning(
                    "Calibration ladder: dropping out-of-order assignment "
                    "(rt=%.3f min, C%d) - not part of the longest increasing "
                    "carbon run",
                    rt, c,
                )
    return [pairs[i] for i in sorted(keep)]


def _assignment_pairs(amap: Dict[str, list], cdf_path) -> list[Tuple[float, int]]:
    """Return sorted ``(rt, carbon)`` pairs from saved assignments for
    ``cdf_path`` — the single source ``anchors_for``, ``calibration_ladder``
    and the ``/api/calibration`` overlay all read.

    Drops ``ignore``d and incomplete entries, tolerates malformed ``rt``/
    ``carbon`` values (skips them rather than raising), drops carbons unknown
    to the reference n-alkane ladder, de-duplicates carbons (keeping the
    first occurrence after sorting by retention time), and keeps only the
    longest run of carbons that increases with retention time (see
    ``_longest_increasing_by_carbon``). Never raises.
    """
    entries = amap.get(_cal_key(cdf_path))
    if entries is None:
        entries = amap.get(str(cdf_path))
    if not entries:
        return []
    cbp = carbon_bp_map()
    raw: list[Tuple[float, int]] = []
    for e in entries:
        if not isinstance(e, dict) or e.get("ignore"):
            continue
        if e.get("carbon") is None or e.get("rt") is None:
            continue
        try:
            rt = float(e["rt"])
            carbon = int(e["carbon"])
        except (TypeError, ValueError):
            continue
        if carbon not in cbp:
            LOGGER.info(
                "Calibration ladder: dropping assignment at rt=%.3f min - "
                "C%d is not a reference n-alkane carbon",
                rt, carbon,
            )
            continue
        raw.append((rt, carbon))
    raw.sort(key=lambda p: p[0])
    seen: set[int] = set()
    pairs: list[Tuple[float, int]] = []
    for rt, carbon in raw:
        if carbon in seen:
            continue
        seen.add(carbon)
        pairs.append((rt, carbon))
    return _longest_increasing_by_carbon(pairs)


def validate_assignments(entries: list) -> list[str]:
    """Return human-readable errors if assigned carbons are not strictly
    increasing with retention time.

    Mirrors the ordering constraint ``calibration_ladder`` and the
    distillation math both depend on: a peak eluting later must be assigned
    a higher carbon number than every earlier-eluting assigned peak, or
    carbon-range labelling (``np.interp`` against the ladder) is silently
    wrong. ``ignore``d and unassigned entries are skipped. Never raises —
    malformed ``rt``/``carbon`` values are simply skipped.
    """
    pairs: list[Tuple[float, int]] = []
    for e in entries:
        if not isinstance(e, dict) or e.get("ignore"):
            continue
        if e.get("carbon") is None or e.get("rt") is None:
            continue
        try:
            rt = float(e["rt"])
            carbon = int(e["carbon"])
        except (TypeError, ValueError):
            continue
        pairs.append((rt, carbon))
    pairs.sort(key=lambda p: p[0])
    errors: list[str] = []
    last_rt: float | None = None
    last_carbon: int | None = None
    for rt, carbon in pairs:
        if last_carbon is not None and carbon <= last_carbon:
            errors.append(
                f"C{carbon} at {rt:.3f} min is not greater than C{last_carbon} "
                f"at {last_rt:.3f} min"
            )
            continue
        last_rt, last_carbon = rt, carbon
    return errors


def anchors_for(
    amap: Dict[str, list], cdf_path
) -> Tuple[np.ndarray, np.ndarray] | None:
    """Return sorted ``(rt, bp)`` calibration anchors for ``cdf_path``.

    Only entries carrying a ``carbon`` assignment contribute; ``ignore`` and
    unassigned peaks are dropped. Returns ``None`` when fewer than two usable
    anchors exist (too few to interpolate — caller falls back to auto-detect).
    """
    pairs = _assignment_pairs(amap, cdf_path)
    if len(pairs) < 2:
        return None
    cbp = carbon_bp_map()
    rt_arr = np.array([p[0] for p in pairs], float)
    bp_arr = np.array([cbp[p[1]] for p in pairs], float)
    return rt_arr, bp_arr


def calibration_ladder(conf: Dict[str, str],
                       sensitivity: float | None = None) -> Tuple[list, list]:
    """Return ``(times, carbons)`` for labelling carbon ranges on a trace.

    Uses the operator's saved peak->carbon assignments for the configured
    calibration CDF (the same source the distillation math uses, resolved via
    ``active_calibration_path`` so labels and math never disagree), so
    ignored peaks (CS2, impurities) never get a carbon number.
    ``_assignment_pairs`` keeps only the longest run of carbons that
    increases with retention time (logging each dropped entry), so a single
    mis-assigned peak doesn't silently mislabel every range after it. Falls
    back to sequential auto-detection only when fewer than two usable
    assignments survive, truncated to the known n-alkane ladder so the two
    lists are always the same length. Returns ``([], [])`` when no
    calibration is configured or it can't be read.
    """
    cal_path = active_calibration_path(conf)
    if cal_path is None:
        return [], []
    amap = parse_assignment_map(conf.get("calibration_assignments", ""))
    pairs = _assignment_pairs(amap, cal_path)
    if len(pairs) >= 2:
        times, carbons = [p[0] for p in pairs], [p[1] for p in pairs]
    else:
        if not cal_path.is_file():
            return [], []
        try:
            sens = float(sensitivity if sensitivity is not None
                         else conf.get("calibration_sensitivity", 50) or 50)
            peaks = calibration_peak_times(cal_path, sens)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Calibration ladder: could not read %s: %s", cal_path, exc)
            return [], []
        if len(peaks) != len(N_ALKANE_CARBON):
            LOGGER.warning(
                "Calibration ladder: auto-detected %d peak(s) but the reference "
                "ladder has %d n-alkanes; labels may be mislabeled - save peak "
                "assignments on the Calibration page.",
                len(peaks), len(N_ALKANE_CARBON),
            )
        n = min(len(peaks), len(N_ALKANE_CARBON))
        times, carbons = [float(t) for t in peaks[:n]], list(N_ALKANE_CARBON[:n])

    # Never hand out a 0<n<2-point ladder from either branch above — a
    # 1-point ladder still crashes every consumer that interpolates against
    # it (analysis_core._ladder requires >= 2 points).
    if len(times) < 2:
        LOGGER.warning(
            "Calibration ladder: only %d usable point(s) for %s; need at "
            "least 2 - returning no ladder.",
            len(times), cal_path,
        )
        return [], []
    return times, carbons


def upsert_assignments(raw: str, cdf_path, assignments: list) -> str:
    """Return an updated ``calibration_assignments`` JSON string.

    Stores ``assignments`` under the canonical key for ``cdf_path``, replacing
    any prior list for that CDF and leaving other CDFs' entries intact.
    """
    amap = parse_assignment_map(raw)
    amap[_cal_key(cdf_path)] = assignments
    return json.dumps(amap)


class AutoCalibrationRefused(ValueError):
    """The assigned calibration is unusable and auto-detection is off."""


def _refuse_auto(cal_cdf, why: str) -> AutoCalibrationRefused:
    return AutoCalibrationRefused(
        f"Calibration {Path(cal_cdf).name}: {why}, and auto-detection is off - "
        "assign the peaks on the Calibration page"
    )


def _calibration_anchor_set(
    cal_cdf: Path, conf: Dict[str, str], *, use_assignments: bool = True,
    allow_auto: bool = True,
) -> Tuple[np.ndarray, np.ndarray, list, str]:
    """Return ``(rt, bp, carbons, source)``: the anchors a calibration is built from.

    ``source`` is ``"assignments"`` when ``conf`` holds at least two usable
    peak→carbon assignments for ``cal_cdf`` (the same pairs ``anchors_for``
    uses), else ``"auto"``: peaks auto-detected in the CDF, zipped in order
    with the reference n-alkane ladder. With ``allow_auto`` False it raises
    ``AutoCalibrationRefused`` (a ``ValueError``) instead of auto-detecting.
    """
    if use_assignments:
        try:
            amap = parse_assignment_map(conf.get("calibration_assignments", ""))
            pairs = _assignment_pairs(amap, cal_cdf)
        except Exception as exc:  # noqa: BLE001
            if not allow_auto:
                raise _refuse_auto(cal_cdf, f"assignment lookup failed ({exc})") from exc
            LOGGER.warning("Calibration assignment lookup failed: %s", exc)
            pairs = []
        if len(pairs) >= 2:
            cbp = carbon_bp_map()
            return (np.array([p[0] for p in pairs], float),
                    np.array([cbp[p[1]] for p in pairs], float),
                    [p[1] for p in pairs], "assignments")
        if not allow_auto:
            raise _refuse_auto(cal_cdf, f"{len(pairs)} usable peak assignment(s), need at least 2")
    if not allow_auto:
        raise _refuse_auto(cal_cdf, "no assignments used")

    t, y = _read_cdf(cal_cdf)
    rt = _detect_nalkane_peaks(t, y)
    n = min(rt.size, len(N_ALKANE_BP))
    last_c = N_ALKANE_CARBON[n - 1] if n else 0
    LOGGER.debug("Calibration peaks detected: %d (C5–C%d)", n, last_c)
    return rt[:n], np.asarray(N_ALKANE_BP[:n], float), list(N_ALKANE_CARBON[:n]), "auto"


def _anchors_info(rt: np.ndarray, carbons: list, source: str) -> dict:
    return {"source": source,
            "anchors": [[float(r), int(c)] for r, c in zip(rt, carbons)]}


def _build_calibration_and_anchors(
    cal_cdf: Path, conf: Dict[str, str], *, allow_auto: bool = True
) -> Tuple[Callable[[np.ndarray], np.ndarray], dict]:
    """``_build_calibration`` plus the anchors it used (``_anchors_info`` shape)."""
    rt, bp, carbons, source = _calibration_anchor_set(cal_cdf, conf, allow_auto=allow_auto)
    if source == "assignments":
        try:
            cal = build_calibration_from_anchors(rt, bp)
            LOGGER.debug("Calibration from %d manual assignments", rt.size)
            return cal, _anchors_info(rt, carbons, source)
        except Exception as exc:  # noqa: BLE001
            if not allow_auto:
                raise _refuse_auto(cal_cdf, f"building from the assignments failed ({exc})") from exc
            # Never let a bad assignment set take down the whole pipeline —
            # fall back to auto-detection (which feeds every sample's distillation).
            LOGGER.warning(
                "Calibration from assignments failed (%s); using auto-detection", exc
            )
            rt, bp, carbons, source = _calibration_anchor_set(cal_cdf, conf, use_assignments=False)
    return build_calibration_from_anchors(rt, bp), _anchors_info(rt, carbons, source)


def _build_calibration(cal_cdf: Path, conf: Dict[str, str]) -> Callable[[np.ndarray], np.ndarray]:
    """Return a calibration function for a calibration CDF.

    Prefers the manual peak→carbon assignments in ``conf``
    (``calibration_assignments``); falls back to sequential auto-detection
    against the reference n-alkane ladder when no usable assignments exist.
    """
    return _build_calibration_and_anchors(cal_cdf, conf)[0]


def _assignment_signature(cal_path: Path, conf: Dict[str, str]) -> str:
    """Stable signature of the assignment pairs ``conf`` gives ``cal_path``.

    Lets the calibration cache refresh when assignments change — the CDF file's
    mtime alone is blind to assignment edits (they live in settings, not the CDF).
    It signs the ``(rt, carbon)`` pairs actually used (``_assignment_pairs``,
    which also finds entries saved under a legacy unresolved key), so confs
    whose used pairs differ never share a signature.
    """
    try:
        amap = parse_assignment_map(conf.get("calibration_assignments", ""))
        return json.dumps(_assignment_pairs(amap, cal_path))
    except Exception:  # noqa: BLE001
        return ""


def _calibration_entry(
    cal_cdf: Path, conf: Dict[str, str], *, allow_auto: bool = True
) -> Tuple[Callable[[np.ndarray], np.ndarray], dict]:
    """Cached ``_build_calibration_and_anchors(cal_cdf, conf)``.

    Cached per (resolved path, ``conf``'s assignment signature) and rebuilt
    when the CDF's mtime changes, so two confs never share a function built
    from the other's assignments. With ``allow_auto`` False an auto-detected
    calibration, cached or not, raises ``AutoCalibrationRefused``.
    """
    func, info = _calibration_entry_cached(cal_cdf, conf, allow_auto)
    if not allow_auto and info["source"] != "assignments":
        raise _refuse_auto(cal_cdf, "the assigned calibration fell back to auto-detection")
    return func, info


def _calibration_entry_cached(
    cal_cdf: Path, conf: Dict[str, str], allow_auto: bool
) -> Tuple[Callable[[np.ndarray], np.ndarray], dict]:
    cal_path = Path(cal_cdf)
    try:
        key = str(cal_path.resolve())
    except Exception:  # noqa: BLE001
        key = str(cal_path)
    sig = _assignment_signature(cal_path, conf)
    try:
        mtime = cal_path.stat().st_mtime
    except Exception:  # noqa: BLE001
        return _build_calibration_and_anchors(cal_path, conf, allow_auto=allow_auto)
    with _CAL_LOCK:
        cached = _CAL_CACHE.get((key, sig))
        if cached and cached[0] == mtime:
            _CAL_CACHE.move_to_end((key, sig))
            return cached[1], cached[2]
    func, info = _build_calibration_and_anchors(cal_path, conf, allow_auto=allow_auto)
    with _CAL_LOCK:
        # Entries for an older version of this file can never match again.
        for stale in [k for k, v in _CAL_CACHE.items() if k[0] == key and v[0] != mtime]:
            del _CAL_CACHE[stale]
        _CAL_CACHE[(key, sig)] = (mtime, func, info)
        _CAL_CACHE.move_to_end((key, sig))
        same_path = [k for k in _CAL_CACHE if k[0] == key]
        for old in same_path[:-CAL_CACHE_SIGNATURES_PER_PATH]:
            del _CAL_CACHE[old]
    return func, info


def _calibration_function(cal_cdf: Path, conf: Dict[str, str]) -> Callable[[np.ndarray], np.ndarray]:
    """Cached calibration function for ``cal_cdf`` under ``conf``."""
    return _calibration_entry(cal_cdf, conf)[0]


def calibration_anchors(cal_cdf: Path, conf: Dict[str, str], *, allow_auto: bool = True) -> dict:
    """The anchors the calibration for ``cal_cdf`` under ``conf`` is built from.

    Returns ``{"source": "assignments"|"auto", "anchors": [[rt, carbon], ...]}``
    (``rt`` in minutes, sorted), exactly what the distillation math uses,
    including the auto-detection fallback (``AutoCalibrationRefused`` instead
    when ``allow_auto`` is False).
    """
    info = _calibration_entry(cal_cdf, conf, allow_auto=allow_auto)[1]
    return {"source": info["source"], "anchors": [list(a) for a in info["anchors"]]}


def _cumulative_percent(t: np.ndarray, y: np.ndarray) -> np.ndarray:
    y = np.where(y < 0, 0, y)
    area = np.cumsum((y[:-1] + y[1:]) / 2 * np.diff(t))
    area = np.insert(area, 0, 0.0)
    if area[-1] <= 0:
        raise ValueError("Chromatogram has zero integrated area")
    return area / area[-1] * 100.0


def _interp_bp(pct: np.ndarray, bp: np.ndarray, targets: Sequence[float]) -> np.ndarray:
    idx = np.argsort(pct)
    pct_u, ui = np.unique(pct[idx], return_index=True)
    return np.interp(targets, pct_u, bp[idx][ui])


def _convert_to_d86(d2887: Dict[str, float]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for cut, (a0, a1, a2, a3) in _CONVERSION_COEFF.items():
        if cut not in _CONVERSION_REL:
            continue
        p_prev, p_curr, p_next = _CONVERSION_REL[cut]
        try:
            t_prev = d2887[p_prev]
            t_curr = d2887[p_curr]
            t_next = d2887[p_next]
        except KeyError as exc:
            LOGGER.error("Missing boiling point %s for D86 conversion", exc)
            continue
        out[cut] = _round2(a0 + a1 * t_prev + a2 * t_curr + a3 * t_next)
    return out


# Map from JSON "test" names (Agilent GC section) → D86 dict keys
_D86_CORRECTION_TEST_MAP: Dict[str, str] = {
    "IBP - D86": "IBP",
    "5% - D86":  "5%",
    "10% - D86": "10%",
    "20% - D86": "20%",
    "30% - D86": "30%",
    "50% - D86": "50%",
    "70% - D86": "70%",
    "80% - D86": "80%",
    "90% - D86": "90%",
    "95% - D86": "95%",
    "FBP - D86": "FBP",
}


def load_d86_corrections(json_path: str | Path) -> Dict[str, float]:
    """Load Agilent GC D86 correction factors from the shared JSON file.

    Returns a dict keyed by D86 cut name (e.g. ``"IBP"``, ``"10%"``, ``"FBP"``)
    → correction value to *add* to the converted temperature.
    Returns an empty dict if the file is missing or cannot be parsed.
    """
    try:
        with open(Path(json_path), encoding="utf-8") as fh:
            data = json.load(fh)
        gc = data.get("Agilent GC", {})
        corrections: Dict[str, float] = {}
        for test_name, entry in gc.items():
            key = _D86_CORRECTION_TEST_MAP.get(test_name)
            if key is not None:
                corrections[key] = float(entry["correction_value"])
        LOGGER.debug("Loaded %d D86 correction factors from %s", len(corrections), json_path)
        return corrections
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Could not load D86 correction factors from %s: %s", json_path, exc)
        return {}


def apply_d86_corrections(
    d86: Dict[str, float], corrections: Dict[str, float]
) -> Dict[str, float]:
    """Return a new D86 dict with each correction value added to the matching key."""
    return {k: _round2(v + corrections.get(k, 0.0)) for k, v in d86.items()}


def _apply_blank_and_clip(
    t: np.ndarray,
    y: np.ndarray,
    blank: tuple[np.ndarray, np.ndarray] | None,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply blank subtraction and ASTM clipping.

    Parameters
    ----------
    t, y:
        Chromatogram time array (minutes) and intensity.
    blank:
        Optional ``(t_blank, y_blank)`` arrays.  If provided the blank is
        interpolated onto ``t`` before subtraction.
    Returns
    -------
    tuple[np.ndarray, np.ndarray]
        The corrected and clipped ``(t, y)`` arrays.
    """

    def _offset(arr: np.ndarray, dt: float) -> float:
        n = max(5, int(np.ceil(1.0 / 60.0 / dt)))
        n = min(n, arr.size)
        head = arr[:n]
        m = float(head.mean())
        sd = float(head.std())
        good = np.abs(head - m) <= sd
        if good.any():
            m = float(head[good].mean())
        return m

    dt = np.median(np.diff(t)) if t.size > 1 else 0.01

    y = y - _offset(y, dt)

    if blank is not None:
        tb, yb = blank
        if tb.size != t.size or not np.allclose(tb, t):
            yb = np.interp(t, tb, yb)
        yb = yb - _offset(yb, dt)
        # Guard: a blank must not carry sample-like signal. Subtracting one
        # that does wipes the sample out and leaves a 10-second "curve".
        h_sample = _peak_height_outside_solvent(t, y)
        h_blank = _peak_height_outside_solvent(t, yb)
        if h_sample > 0 and h_blank > BLANK_MAX_HEIGHT_FRACTION * h_sample:
            raise BlankRejected(
                f"blank peak height {h_blank:.0f} pA is {h_blank / h_sample:.0%} of the sample's "
                f"{h_sample:.0f} pA (limit {BLANK_MAX_HEIGHT_FRACTION:.0%}) - not a blank"
            )
        y = y - yb
    else:
        LOGGER.warning("No blank available – ASTM 12.2 not applied")

    y = np.maximum(y, 0.0)

    pct = _cumulative_percent(t, y)
    d_pct = np.gradient(pct, t * 60)
    mask = d_pct > 1e-5
    if not mask.any():
        raise ValueError("No elution window found")
    start = mask.argmax()
    end = len(mask) - mask[::-1].argmax() - 1
    return t[start : end + 1], y[start : end + 1]


# ---------------------------------------------------------------------------
# Blank plausibility (added 2026-09-18 after a diesel run named "Blank" was
# cached as the reference blank and subtracted from every sample)
# ---------------------------------------------------------------------------
class BlankRejected(ValueError):
    """Raised when the reference blank carries sample-like signal."""


# A genuine ASTM D2887 blank has no peaks outside the solvent window. Anything
# above this height (pA, after removing the slow bleed ramp) is a sample.
BLANK_MAX_INTENSITY_PA = 200.0
BLANK_SOLVENT_END_MIN = 0.35
# At subtraction time the blank may not carry more than this fraction of the
# sample's own peak height, whatever the absolute numbers are.
BLANK_MAX_HEIGHT_FRACTION = 0.25


def _peak_height_outside_solvent(t: np.ndarray, y: np.ndarray,
                                 solvent_end_min: float = BLANK_SOLVENT_END_MIN) -> float:
    """Tallest peak after the solvent window, with the slow bleed ramp removed.

    A 1-minute rolling minimum tracks column bleed (which rises with the oven
    ramp in every run, blank or not) so only real peaks are measured.
    """
    if t.size < 3:
        return 0.0
    dt = float(np.median(np.diff(t))) if t.size > 1 else 0.01
    win = max(3, int(round(1.0 / dt)))
    baseline = minimum_filter1d(y, size=win, mode="nearest")
    resid = y - baseline
    mask = t > solvent_end_min
    return float(resid[mask].max()) if mask.any() else 0.0


def is_plausible_blank(path: Path, max_intensity_pa: float | None = None) -> bool:
    """True if *path* looks like a blank run (no sample peaks after the solvent).

    The threshold can be tuned in settings as ``blank_max_intensity_pa``.
    """
    if max_intensity_pa is None:
        try:
            max_intensity_pa = float(_get_settings().get("blank_max_intensity_pa", BLANK_MAX_INTENSITY_PA))
        except Exception:  # noqa: BLE001
            max_intensity_pa = BLANK_MAX_INTENSITY_PA
    try:
        t, y = _read_cdf(Path(path))
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Cannot read candidate blank %s: %s", path, exc)
        return False
    height = _peak_height_outside_solvent(t, y)
    ok = height <= max_intensity_pa
    if not ok:
        LOGGER.warning("Rejecting %s as blank: %.0f pA of sample signal after %.2f min (limit %.0f pA)",
                       Path(path).name, height, BLANK_SOLVENT_END_MIN, max_intensity_pa)
    return ok

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def processed_cdf_filename(lab_id: str, inj_dt: datetime, suffix: str = ".CDF") -> str:
    """Build the processed-CDF filename ``<Lab ID>_<MMDDYYYY>_<HHMMSS><.CDF>``.

    The run *time* is included (not just the date) so two same-day re-runs of
    the same sample get distinct files instead of overwriting each other. The
    Lab ID itself is never altered — this only names the file on disk.
    """
    stamp = inj_dt.strftime("%m%d%Y_%H%M%S")
    safe = re.sub(r'[\/:*?"<>|]', "_", f"{lab_id}_{stamp}".strip()) or "Sample"
    return f"{safe}{suffix.upper()}"


# ANDI/AIA compact stamp: 14 digits plus an optional zone we discard (``Z``
# or ``±HH[:]MM``). Matched FIRST: see ``parse_injection_datetime``.
_ANDI_COMPACT = re.compile(r"^(\d{14})(?:Z|\s*[+-]\d{2}:?\d{2})?$")
# ISO is tried only for text that starts with a date written with separators.
_ISO_DATE_PREFIX = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")
_OTHER_DT_FORMATS = ("%d-%b-%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")


def parse_injection_datetime(raw: str) -> datetime | None:
    """Parse an injection timestamp from the many formats GC files use.

    Agilent/Thermo ANDI (.CDF) files store ``injection_date_time_stamp`` as a
    compact ``YYYYMMDDHHMMSS`` string, often with a trailing ``±ZZZZ`` zone;
    others use ISO, ``DD-Mon-YYYY HH:MM:SS`` or US ``MM/DD/YYYY HH:MM:SS``.

    The compact form is matched explicitly **before** any ISO attempt, and
    ISO is tried only when the text starts with a ``-``/``/``-separated
    date. (v1 tried ``datetime.fromisoformat`` first; on Python >= 3.11 that
    accepts ``20260925002450+0000`` and misreads it as 02:45:00, taking the
    9th character as the date/time separator. ``v1_parse_injection_datetime``
    keeps v1's answer for matching old CSV rows.) This is the same parse as
    ``import_match._correct_parse``, so the hub and the history importer
    agree on every sample's injection time.

    Returns a *naive* ``datetime`` (any zone offset is dropped — all runs from
    one instrument share a zone, so wall-clock time keeps ordering correct and
    matches the naive datetimes used elsewhere in this module), or ``None`` if
    nothing parses so the caller can fall back to file mtime.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None

    m = _ANDI_COMPACT.match(text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            return None

    # ISO (" " or "T" separator, optional offset), only with date separators.
    if _ISO_DATE_PREFIX.match(text):
        try:
            return datetime.fromisoformat(text.replace("/", "-")).replace(tzinfo=None)
        except ValueError:
            pass

    for fmt in _OTHER_DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def v1_parse_injection_datetime(raw: str) -> datetime | None:
    """What v1 (phase 1, ``fromisoformat`` first) parsed ``raw`` as on Python
    3.11-3.13, the interpreters the share copies ran: bug for bug, and the
    same answer on any interpreter. Used only to compute
    ``samples.legacy_injection_dt`` (the string v1 wrote to the CSV). Delegates
    to ``import_match._v1_parse(raw, 'py311_313')``, the one emulation of it.
    """
    import import_match  # deferred: import_match imports distill
    return import_match._v1_parse(raw or "", "py311_313")


def normalise_method_name(raw) -> str:
    """ChemStation method name as the hub compares it: trimmed, any directory
    part stripped (``\\`` or ``/``), upper-cased; ``''`` if absent."""
    text = str(raw or "").strip()
    if not text:
        return ""
    return re.split(r"[\\/]", text)[-1].strip().upper()


def _cdf_names(path: Path) -> Tuple[str, str, str]:
    """``(sample_name or '', raw injection stamp, raw detection_method_name)``."""
    with _NETCDF_LOCK:
        with Dataset(path) as ds:
            vars_lc = {n.lower(): n for n in ds.variables}
            attrs_lc = {n.lower(): n for n in ds.ncattrs()}

            def _get(key: str) -> str:
                k = key.lower()
                if k in vars_lc:
                    return _read_text(ds.variables[vars_lc[k]])
                if k in attrs_lc:
                    return str(getattr(ds, attrs_lc[k]))
                return ""

            sample = _get("sample_name")
            raw_date = (
                _get("injection_date_time_stamp")
                or _get("injection_date")
                or _get("injection_time")
            )
            method = _get("detection_method_name")
    return sample, raw_date, method


def cdf_metadata(path: Path) -> Tuple[str, datetime]:
    """Return (sample_name, injection_datetime)."""
    sample, inj_dt, _source, _method, _raw = cdf_identity(path)
    return sample, inj_dt


def cdf_identity(path: Path, *, mtime: datetime | None = None
                 ) -> Tuple[str, datetime, str, str, str]:
    """``(sample, injection_dt, dt_source, method_name, raw_stamp)`` for a CDF.

    ``sample`` is the ``sample_name`` (the file stem if absent);
    ``injection_dt`` a naive ``datetime`` from ``parse_injection_datetime``,
    with ``dt_source`` ``'cdf'``, or, when the stamp is missing or
    unparseable, ``mtime`` (the sender's file time; the file's own mtime when
    not given) with ``dt_source`` ``'mtime'``. ``method_name`` is the
    ``detection_method_name`` normalised (``normalise_method_name``; ``''``
    if absent). ``raw_stamp`` is the stamp text as read.
    """
    path = Path(path)
    sample, raw_date, method = _cdf_names(path)
    sample = sample or path.stem or "Unknown"
    inj_dt = parse_injection_datetime(raw_date)
    source = "cdf"
    if inj_dt is None:
        source = "mtime"
        inj_dt = mtime if mtime is not None else datetime.fromtimestamp(path.stat().st_mtime)
    return sample, inj_dt, source, normalise_method_name(method), raw_date


def gc_trace_from_cdf(path: Path) -> Scatter:
    t, y = _read_cdf(path)
    return Scatter(x=t, y=y, name=path.stem,
                   hovertemplate="%{x:.2f} min<br>%{y:.0f}")


def gc_xy_from_cdf(path: Path) -> Tuple[np.ndarray, np.ndarray]:
    """Return (time[min], intensity) arrays from a NetCDF chromatogram.

    Lightweight helper for UIs that do not use Plotly but still rely on
    distill.py for CDF parsing. Keeps public API stable for existing code.
    """
    return _read_cdf(path)

def distillation_curve_from_cdf(path: Path, *, blank_path: Path | None = None,
                                conf: Dict[str, str] | None = None) -> Tuple[np.ndarray, np.ndarray]:
    """Return (percent, temperature) arrays for plotting the distillation curve.

    ``conf`` supplies the calibration; the global settings when omitted.
    """
    conf = conf if conf is not None else _get_settings()
    t, y = _read_cdf(path)
    if blank_path is not None:
        try:
            tb, yb = _read_cdf(Path(blank_path))
            t, y = _apply_blank_and_clip(t, y, (tb, yb))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Blank subtraction failed: %s", exc)
            t, y = _apply_blank_and_clip(t, y, None)
    else:
        t, y = _apply_blank_and_clip(t, y, None)

    cal_cdf = active_calibration_path(conf)
    if cal_cdf is None or not cal_cdf.is_file():
        raise FileNotFoundError("Calibration CDF not found – set settings['calibration_cdf'] or GC_CAL_CDF")
    cal_fn = _calibration_function(cal_cdf, conf)
    bp_curve = cal_fn(t)

    pct_curve = _cumulative_percent(t, y)
    return pct_curve, bp_curve


# On Windows os.replace fails with PermissionError while any other handle
# without delete-sharing has the target open — antivirus, the indexer, a
# backup, an SMB client — usually for a moment only.
CSV_REPLACE_ATTEMPTS = 10
CSV_REPLACE_BACKOFF_SECONDS = (0.1, 0.2)   # grows from the first to the second


def _replace_retrying(tmp: Path, path: Path) -> None:
    lo, hi = CSV_REPLACE_BACKOFF_SECONDS
    for attempt in range(CSV_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            if attempt == CSV_REPLACE_ATTEMPTS - 1:
                raise PermissionError(exc.errno, exc.strerror, str(path)) from exc
            LOGGER.debug("%s is busy (%s); retrying the replace", path, exc)
            time.sleep(lo + (hi - lo) * attempt / max(1, CSV_REPLACE_ATTEMPTS - 2))


def _csv_temp_prefix(path: Path) -> str:
    return f".{path.name}."


def sweep_stale_csv_temps(csv_path) -> int:
    """Remove temp files a killed ``_atomic_write_csv`` left next to
    *csv_path*. Call only while no rewrite can be running (startup).
    Returns how many were removed."""
    csv_path = Path(csv_path)
    removed = 0
    try:
        leftovers = list(csv_path.parent.glob(_csv_temp_prefix(csv_path) + "*.tmp"))
    except OSError:
        return 0
    for stale in leftovers:
        try:
            stale.unlink()
            removed += 1
        except OSError as exc:
            LOGGER.warning("Could not remove leftover %s: %s", stale, exc)
    return removed


def _atomic_write_csv(path, fieldnames, rows) -> None:
    """Rewrite the whole CSV at *path* (header + *rows*, as dicts) atomically.

    Written to a temp file in the same folder, flushed and fsynced, then
    ``os.replace``d over the original, so an exit mid-write (a restart under
    the updater ends in os._exit or taskkill /F) can never leave the results
    CSV truncated: readers see the old file or the new one. The bytes are
    exactly what ``open("w", newline="", encoding="utf-8")`` + DictWriter
    wrote before.

    On failure the original is untouched and the temp file removed. A
    PermissionError from the replace (Windows: someone, e.g. Excel, has the
    CSV open) is retried briefly, then re-raised naming *path*, as the old
    ``open("w")`` did. Callers hold ``_CSV_LOCK``.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(prefix=_csv_temp_prefix(path), suffix=".tmp",
                                    dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        except OSError:
            pass  # new file, or a filesystem without POSIX modes
        _replace_retrying(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _injection_row_key(row) -> Tuple[str, str, str]:
    """Identity of a results row for the library reorder: the dedupe key
    (Lab ID + InjectionDateTime) plus Source File, so two rows that share a
    Lab ID and time but came from different CDFs are never confused."""
    return ((row.get("Lab ID") or "").strip(),
            (row.get("InjectionDateTime") or "").strip(),
            (row.get("Source File") or "").strip())


def derive_injection_times(rows, read_metadata=None):
    """Re-derive InjectionDateTime for each row from its Source File.

    Called with ``_CSV_LOCK`` **released**: it opens one CDF per row, which
    can take minutes on a share. Returns ``({row key: new value}, unreadable)``
    where the key is ``_injection_row_key`` of the row as it was read, and
    ``unreadable`` counts rows whose CDF is missing or could not be read.
    Rows without a Source File are skipped and not counted.
    """
    if read_metadata is None:
        read_metadata = cdf_metadata
    derived: Dict[Tuple[str, str, str], str] = {}
    unreadable = 0
    for row in rows:
        src = (row.get("Source File") or "").strip()
        if not src:
            continue
        src_path = Path(src)
        if not src_path.is_file():
            unreadable += 1
            continue
        try:
            _sample, inj_dt = read_metadata(src_path)
            derived[_injection_row_key(row)] = inj_dt.isoformat(sep=" ")
        except Exception as exc:
            LOGGER.debug("Reindex: could not read %s: %s", src, exc)
            unreadable += 1
    return derived, unreadable


def apply_injection_times(rows, derived) -> int:
    """Apply ``derive_injection_times`` output to ``rows`` (a fresh read of the
    CSV, under ``_CSV_LOCK``), in place. Only rows whose key is still present
    are touched: rows appended, deleted or edited since the snapshot are left
    exactly as they are. Returns how many rows changed."""
    updated = 0
    for row in rows:
        new_val = derived.get(_injection_row_key(row))
        if new_val is None:
            continue
        if (row.get("InjectionDateTime") or "").strip() != new_val:
            row["InjectionDateTime"] = new_val
            updated += 1
    return updated


def _append_csv_row(dest_csv, row_data: list) -> None:
    """Always append *row_data* as a new line (writing CSV_HEADER first if the
    file is new). It does NOT replace an
    existing row — every call adds a distinct line, so a reprocess / Export-to-
    LIMS of the same sample produces a new row. Thread-safe via ``_CSV_LOCK``."""
    with _CSV_LOCK:
        write_header = not dest_csv.exists()
        with dest_csv.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if write_header:
                w.writerow(CSV_HEADER)
            w.writerow(row_data)


def compute(cdf_path: Path, conf: Dict[str, str], blank_path: Path | None = None, *,
            corrections: Dict[str, float] | None = None,
            honour_env: bool = True, allow_auto: bool = True) -> dict:
    """Run the distillation for one sample; write nothing.

    ``conf`` supplies the calibration, the correction file and the best-fit
    settings. ``corrections`` maps D86 cut → value to add (as
    ``load_d86_corrections`` returns); ``None`` loads them from
    ``conf["correction_factors_json"]`` exactly as ``process_cdf`` always has.
    The defaults are v1's behaviour. The hub passes ``honour_env=False``
    (``GC_CAL_CDF`` ignored), ``allow_auto=False`` (an unusable assigned
    calibration raises ``AutoCalibrationRefused``, a ``ValueError``, instead
    of silently auto-detecting) and explicit ``corrections``.

    Returns a dict:

    * ``row``: every ``CSV_HEADER`` column, in order, holding the values
      ``process_cdf`` writes (same rounding, ``""`` for a missing D86 cut),
      with ``Source File`` = ``""``;
    * ``d2887``, ``d86_uncorrected`` and ``d86`` (corrected), keyed by cut;
    * ``lab_id`` and ``injection_dt`` (a naive ``datetime``);
    * ``calibration``: ``{cdf, anchors_source, anchors}``.

    Raises ``FileNotFoundError`` when the calibration CDF is missing.
    """
    path = Path(cdf_path)

    # 1 Chromatogram
    t, y = _read_cdf(path)
    if blank_path is not None:
        try:
            tb, yb = _read_cdf(Path(blank_path))
            LOGGER.debug("Applying blank from %s", blank_path)
            t, y = _apply_blank_and_clip(t, y, (tb, yb))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Blank subtraction failed: %s", exc)
            t, y = _apply_blank_and_clip(t, y, None)
    else:
        t, y = _apply_blank_and_clip(t, y, None)

    # 2 Calibration
    cal_cdf = active_calibration_path(conf, honour_env=honour_env)
    if cal_cdf is None or not cal_cdf.is_file():
        raise FileNotFoundError("Calibration CDF not found – set settings['calibration_cdf'] or GC_CAL_CDF")
    cal_fn, cal_info = _calibration_entry(cal_cdf, conf, allow_auto=allow_auto)
    bp_curve = cal_fn(t)

    # 3 Cumulative %
    pct_curve = _cumulative_percent(t, y)

    # 4 Interpolate D2887 points
    vals = _interp_bp(pct_curve, bp_curve, PERCENT_LEVELS)
    keys = [
        "IBP",
        "5%",
        "10%",
        "20%",
        "30%",
        "40%",
        "50%",
        "60%",
        "70%",
        "80%",
        "90%",
        "95%",
        "FBP",
    ]
    d2887 = dict(zip(keys, map(_round2, vals)))

    # 5 D86 correlation
    d86 = _convert_to_d86(d2887)
    # ASTM D86 App. X4 has no equations for 40% and 60%; interpolate linearly
    if "30%" in d86 and "50%" in d86:
        d86["40%"] = _round2((d86["30%"] + d86["50%"]) / 2)
    if "50%" in d86 and "70%" in d86:
        d86["60%"] = _round2((d86["50%"] + d86["70%"]) / 2)

    d86_uncorrected = dict(d86)

    # 5b Apply EQM correction factors (always written to CSV per lab policy)
    if corrections is None:
        corr_path = conf.get("correction_factors_json", "").strip()
        corrections = load_d86_corrections(corr_path) if corr_path else {}
    if corrections:
        d86 = apply_d86_corrections(d86, corrections)

    # 5c Fuel-type best fit against the comparison standards (failure-safe:
    # blank columns rather than blocking the distillation on any error)
    best_fit_label = ""
    best_fit_score = ""
    try:
        if str(conf.get("bestfit_enabled", "true")).lower() == "true":
            import fuel_fit  # deferred: keeps distill importable without scipy.optimize
            standards = fuel_fit.load_standards(
                Path(conf.get("comparison_defaults_dir", str(paths.standards_dir()))), gc_xy_from_cdf
            )
            if standards:
                fit = fuel_fit.classify(
                    t, y, standards,
                    threshold=float(conf.get("bestfit_threshold", 0.93)),
                    shift_tolerance_min=float(conf.get("bestfit_shift_tolerance_min", 0.05)),
                    mix_min_frac=float(conf.get("bestfit_mix_min_frac", 0.10)),
                    x_max_min=float(conf.get("analysis_x_max_min", 7.0)),
                )
                best_fit_label = fit["label"]
                if fit["ranking"]:
                    best_fit_score = f"{fit['score']:.3f}"
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Best-fit classification failed: %s", exc)

    # 6 Metadata
    sample, inj_dt = cdf_metadata(path)
    lab_id = sample  # plain sample name for CSV

    values = [
        lab_id,
        inj_dt.isoformat(sep=" "),
        d2887["IBP"], d2887["5%"],  d2887["10%"], d2887["20%"], d2887["30%"], d2887["40%"],
        d2887["50%"], d2887["60%"], d2887["70%"], d2887["80%"], d2887["90%"], d2887["95%"], d2887["FBP"],
        d86.get("IBP", ""), d86.get("5%", ""),  d86.get("10%", ""), d86.get("20%", ""), d86.get("30%", ""), d86.get("40%", ""),
        d86.get("50%", ""), d86.get("60%", ""), d86.get("70%", ""), d86.get("80%", ""), d86.get("90%", ""), d86.get("95%", ""), d86.get("FBP", ""),
        best_fit_label, best_fit_score,
        "",
    ]
    return {
        "row": dict(zip(CSV_HEADER, values)),
        "d2887": d2887,
        "d86_uncorrected": d86_uncorrected,
        "d86": d86,
        "lab_id": lab_id,
        "injection_dt": inj_dt,
        "calibration": {
            "cdf": str(cal_cdf),
            "anchors_source": cal_info["source"],
            "anchors": [list(a) for a in cal_info["anchors"]],
        },
    }


def process_cdf(path: Path, *, blank_path: Path | None = None, reprocess: bool = False) -> Path:
    """Parse *path* and append one row of distillation data to distill_output CSV.

    Returns the final path of the processed CDF (may be moved/renamed).
    """
    conf = _get_settings()
    dest_csv = Path(conf.get("distill_output", str(paths.default_results_csv())))

    # 1-6 The numbers (compute writes nothing)
    result = compute(path, conf, blank_path)
    lab_id = result["lab_id"]
    inj_dt = result["injection_dt"]

    # 7 Write CSV  (move section 8 first so we know the final path)
    proc_dir = Path(conf.get("processed_cdf_dir", str(paths.default_processed_dir()))).expanduser()
    if proc_dir:
        proc_dir.mkdir(parents=True, exist_ok=True)
        final_dst = proc_dir / processed_cdf_filename(lab_id, inj_dt, path.suffix)
        source_file = str(final_dst)
    else:
        final_dst = None
        source_file = str(path)

    dest_csv.parent.mkdir(parents=True, exist_ok=True)
    row = dict(result["row"], **{"Source File": source_file})
    row_data = [row[col] for col in CSV_HEADER]
    if reprocess:
        # Reprocess / Export-to-LIMS always appends a new row (per lab policy:
        # each run is a distinct line in the results CSV, duplicates allowed).
        _append_csv_row(dest_csv, row_data)
        LOGGER.info("Appended distillation row (reprocess) for %s", lab_id)
    else:
        with _CSV_LOCK:
            # Check for existing row BEFORE appending to prevent duplicates
            # (can happen if two threads process the same CDF concurrently).
            already_exists = False
            if dest_csv.is_file():
                try:
                    with dest_csv.open("r", encoding="utf-8", newline="") as fh:
                        for existing in csv.DictReader(fh):
                            if ((existing.get("Lab ID") or "").strip() == lab_id
                                    and (existing.get("InjectionDateTime") or "").strip()
                                    == inj_dt.isoformat(sep=" ")):
                                already_exists = True
                                break
                except Exception:
                    pass  # if read fails, append anyway
            if already_exists:
                LOGGER.info("Row already exists for %s — skipping append", lab_id)
            else:
                write_header = not dest_csv.exists()
                with dest_csv.open("a", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    if write_header:
                        w.writerow(CSV_HEADER)
                    w.writerow(row_data)
                LOGGER.info("Appended distillation row for %s", lab_id)

    # 8 Move / rename processed CDF (optional)
    final_path = path
    if final_dst is not None:
        try:
            if final_dst.exists() and final_dst.resolve() != path.resolve():
                final_dst.unlink()
            path.replace(final_dst)
            final_path = final_dst
            LOGGER.debug("CDF moved → %s", final_dst)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Could not move CDF: %s", exc)

    return final_path


if __name__ == "__main__":  # simple CLI test
    import argparse, pprint
    ap = argparse.ArgumentParser()
    ap.add_argument("cdf", type=Path, help="Path to NetCDF chromatogram")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    pprint.pp(cdf_metadata(args.cdf))
    print("Writing CSV …")
    process_cdf(args.cdf)
    print("Done.")
