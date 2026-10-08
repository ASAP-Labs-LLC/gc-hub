"""Numeric core of the Analysis tab — importable without app.py's side effects.

Two deviation-detection channels:

* **Trend channel** — rolling low-quantile trend lines are differenced; catches
  broad envelope deviations (e.g. oil-range contamination raising the hump).
* **Spike channel** — the *raw* sample−standard difference; catches short tall
  peaks (e.g. gasoline adulteration) that a low rolling quantile erases by
  construction.  A minimum-width guard rejects single-point glitches and
  peak-misalignment noise.

Both channels share thresholds and severity classification and their segments
are merged, so downstream (conclusion text, report, UI) sees one list.
"""
from __future__ import annotations

import math
import re

import numpy as np

try:
    from scipy.ndimage import gaussian_filter1d
except Exception:  # pragma: no cover - scipy is in requirements
    gaussian_filter1d = None

#: Fixed light smoothing (in samples) applied to the raw difference before
#: spike detection — just enough to suppress one-sample electrical glitches.
SPIKE_SIGMA_PTS = 3.0

#: Default minimum duration (minutes) an above-threshold run must last to
#: count as a spike segment.
DEFAULT_SPIKE_MIN_WIDTH_MIN = 0.02

_SEV_ORDER = {"marginal": 0, "moderate": 1, "significant": 2}


def compute_trend_line(
    t: np.ndarray,
    y: np.ndarray,
    quantile: float = 0.20,
    window: int = 301,
    sigma: float = 34.0,
) -> np.ndarray:
    """Rolling low-quantile + Gaussian smooth baseline."""
    half = window // 2
    n = len(y)
    trend = np.empty(n)
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        trend[i] = np.quantile(y[lo:hi], quantile)
    if sigma and sigma > 0 and gaussian_filter1d is not None:
        trend = gaussian_filter1d(trend, sigma)
    trend = np.maximum(trend, 0.0)
    return trend


def _find_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs in *mask* as inclusive (start, end) index pairs."""
    runs: list[tuple[int, int]] = []
    in_run = False
    start = 0
    for i in range(len(mask)):
        if mask[i] and not in_run:
            start = i
            in_run = True
        elif not mask[i] and in_run:
            runs.append((start, i - 1))
            in_run = False
    if in_run:
        runs.append((start, len(mask) - 1))
    return runs


def _runs_to_segments(
    runs: list[tuple[int, int]],
    diff: np.ndarray,
    t: np.ndarray,
    marginal: float,
    moderate: float,
    significant: float,
    cal_times: list[float],
    cal_carbons: list[int],
    source: str | None = None,
) -> list[dict]:
    """Expand runs to zero-crossings, classify severity, build segment dicts."""
    abs_diff = np.abs(diff)
    segments: list[dict] = []
    for s, e in runs:
        while s > 0 and diff[s] * diff[s - 1] > 0:
            s -= 1
        while e < len(diff) - 2 and diff[e] * diff[e + 1] > 0:
            e += 1

        peak_val = float(np.max(abs_diff[s : e + 1]))
        peak_diff_signed = float(diff[s + np.argmax(abs_diff[s : e + 1])])

        if peak_val >= significant:
            severity = "significant"
        elif peak_val >= moderate:
            severity = "moderate"
        else:
            severity = "marginal"

        direction = "positive" if peak_diff_signed > 0 else "negative"
        carbon_range = segment_carbon_range(
            float(t[s]), float(t[e]), cal_times, cal_carbons
        )

        seg = {
            "start_time": round(float(t[s]), 3),
            "end_time": round(float(t[e]), 3),
            "carbon_range": carbon_range,
            "severity": severity,
            "direction": direction,
            "peak_diff": round(peak_diff_signed, 1),
        }
        if source:
            seg["source"] = source
        segments.append(seg)
    return segments


def merge_overlapping_segments(
    segments: list[dict],
    cal_times: list[float],
    cal_carbons: list[int],
) -> list[dict]:
    """Merge time-overlapping segments, keeping the wider range and the higher
    severity / larger peak.  Input order does not matter."""
    if len(segments) <= 1:
        return segments
    segments = sorted(segments, key=lambda s: (s["start_time"], s["end_time"]))
    merged: list[dict] = [segments[0]]
    for seg in segments[1:]:
        prev = merged[-1]
        if seg["start_time"] <= prev["end_time"]:
            prev["end_time"] = max(prev["end_time"], seg["end_time"])
            if _SEV_ORDER.get(seg["severity"], 0) > _SEV_ORDER.get(prev["severity"], 0):
                prev["severity"] = seg["severity"]
                if seg.get("source"):
                    prev["source"] = seg["source"]
            if abs(seg["peak_diff"]) > abs(prev["peak_diff"]):
                prev["peak_diff"] = seg["peak_diff"]
                prev["direction"] = seg["direction"]
            prev["carbon_range"] = segment_carbon_range(
                prev["start_time"], prev["end_time"], cal_times, cal_carbons
            )
        else:
            merged.append(seg)
    return merged


def detect_deviation_segments(
    diff: np.ndarray,
    t: np.ndarray,
    marginal: float,
    moderate: float,
    significant: float,
    cal_times: list[float],
    cal_carbons: list[int],
    *,
    min_width_min: float = 0.0,
    source: str | None = None,
) -> list[dict]:
    """Find contiguous runs where |diff| >= marginal, expand to zero-crossings,
    and classify severity.  Runs shorter than *min_width_min* (minutes,
    measured on the above-threshold run before expansion) are discarded."""
    abs_diff = np.abs(diff)
    above = abs_diff >= marginal
    if not above.any():
        return []

    runs = _find_runs(above)
    if min_width_min > 0:
        runs = [
            (s, e) for s, e in runs
            if float(t[e]) - float(t[s]) >= min_width_min
        ]
        if not runs:
            return []

    segments = _runs_to_segments(
        runs, diff, t, marginal, moderate, significant,
        cal_times, cal_carbons, source=source,
    )
    return merge_overlapping_segments(segments, cal_times, cal_carbons)


def spike_difference(y_sample, y_std) -> np.ndarray:
    """The spike channel's signal: the raw sample−standard difference with a
    fixed light denoise (``SPIKE_SIGMA_PTS``)."""
    raw_diff = np.asarray(y_sample, dtype=float) - np.asarray(y_std, dtype=float)
    if gaussian_filter1d is not None and SPIKE_SIGMA_PTS > 0:
        raw_diff = gaussian_filter1d(raw_diff, SPIKE_SIGMA_PTS)
    return raw_diff


def detect_spike_segments(
    t: np.ndarray,
    y_sample: np.ndarray,
    y_std: np.ndarray,
    marginal: float,
    moderate: float,
    significant: float,
    cal_times: list[float],
    cal_carbons: list[int],
    *,
    min_width_min: float = DEFAULT_SPIKE_MIN_WIDTH_MIN,
) -> list[dict]:
    """Spike channel: detect deviations on the raw sample−standard difference."""
    raw_diff = spike_difference(y_sample, y_std)
    return detect_deviation_segments(
        raw_diff, t, marginal, moderate, significant,
        cal_times, cal_carbons,
        min_width_min=min_width_min, source="spike",
    )


def analyze_pair(
    t: np.ndarray,
    y_sample: np.ndarray,
    y_std: np.ndarray,
    *,
    thresh_marginal: float,
    thresh_moderate: float,
    thresh_significant: float,
    quantile: float = 0.20,
    window: int = 301,
    sigma: float = 0.0,
    cal_times: list[float],
    cal_carbons: list[int],
    spike_min_width_min: float = DEFAULT_SPIKE_MIN_WIDTH_MIN,
) -> dict:
    """Run both detection channels once and return ``{"diff", "segments"}``.

    ``diff`` is the trend difference shown in the difference chart.  *sigma*
    is the operator smoothing applied to that trend difference only — the
    spike channel always sees the (lightly denoised) raw difference.
    Segments are detected only when calibration data is available, matching
    the previous behaviour.
    """
    trend_sample = compute_trend_line(t, y_sample, quantile, window, sigma=0)
    trend_std = compute_trend_line(t, y_std, quantile, window, sigma=0)
    if sigma > 0 and gaussian_filter1d is not None:
        trend_diff = np.maximum(gaussian_filter1d(trend_sample, sigma), 0.0) \
                   - np.maximum(gaussian_filter1d(trend_std, sigma), 0.0)
    else:
        trend_diff = trend_sample - trend_std

    segments: list[dict] = []
    if len(cal_times) >= 2:
        trend_segments = detect_deviation_segments(
            trend_diff, t,
            thresh_marginal, thresh_moderate, thresh_significant,
            cal_times, cal_carbons,
        )
        spike_segments = detect_spike_segments(
            t, y_sample, y_std,
            thresh_marginal, thresh_moderate, thresh_significant,
            cal_times, cal_carbons,
            min_width_min=spike_min_width_min,
        )
        segments = merge_overlapping_segments(
            trend_segments + spike_segments, cal_times, cal_carbons
        )
    return {
        "diff": trend_diff,
        "spike_diff": spike_difference(y_sample, y_std),
        "segments": segments,
        "trend_sample": trend_sample,
        "trend_std": trend_std,
    }


def detect_all_segments(
    t: np.ndarray,
    y_sample: np.ndarray,
    y_std: np.ndarray,
    **kwargs,
) -> list[dict]:
    """Run both detection channels and return one merged segment list."""
    return analyze_pair(t, y_sample, y_std, **kwargs)["segments"]


#: Default region colors when a range carries none (legacy Gas/Oil hues, then
#: a small rotating palette for extra operator regions).
_DEFAULT_RANGE_COLORS = [
    "#f0a500", "#a05014", "#3498db", "#27ae60", "#8e44ad", "#e67e22",
]


#: Longest range label kept (bullets, charts and the PDF print it).
RANGE_LABEL_MAX = 40


def clean_range_label(label) -> str:
    """One short line: control/format/line-separator characters (newlines
    included, so a label can't forge a bullet line) become spaces, runs of
    whitespace collapse, capped at ``RANGE_LABEL_MAX``; empty → "Range"."""
    import unicodedata
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch
                   for ch in str("" if label is None else label))
    text = " ".join(text.split())[:RANGE_LABEL_MAX].rstrip()
    return text or "Range"


def resolve_report_ranges(body_ranges, conf: dict) -> list[dict]:
    """Resolve the region list for analysis/report generation.

    Order: explicit *body_ranges* (from the request) → saved overlay defaults
    (``analysis_range_overlays`` JSON in settings) → legacy Gas/Oil settings.
    An explicit list is final even when empty: ``[]`` means no ranges; only a
    missing value (``None``) falls back. Every returned entry has ``label``,
    ``c_start``, ``c_end``, ``color``.
    """
    import json as _json

    def _clean(raw: list) -> list[dict]:
        out = []
        for i, r in enumerate(raw):
            try:
                out.append({
                    "label": clean_range_label(r.get("label", "Range")),
                    "c_start": int(r.get("c_start", 5)),
                    "c_end": int(r.get("c_end", 15)),
                    "color": str(r.get("color") or
                                 _DEFAULT_RANGE_COLORS[i % len(_DEFAULT_RANGE_COLORS)]),
                })
            except Exception:
                continue
        return out

    # An explicit list, even an empty one, is the answer: `[]` means "no
    # ranges" (the operator removed them all), on every path. Only a missing
    # key (None) falls back.
    if isinstance(body_ranges, list):
        return _clean(body_ranges)

    saved = conf.get("analysis_range_overlays", "")
    if saved:
        try:
            raw = _json.loads(saved) if isinstance(saved, str) else saved
            if isinstance(raw, list):
                return _clean(raw)
        except Exception:
            pass

    return [
        {"label": "Gas", "color": "#f0a500",
         "c_start": int(conf.get("analysis_gas_c_start", 5)),
         "c_end": int(conf.get("analysis_gas_c_end", 11))},
        {"label": "Oil", "color": "#a05014",
         "c_start": int(conf.get("analysis_oil_c_start", 20)),
         "c_end": int(conf.get("analysis_oil_c_end", 44))},
    ]


def range_color_rgba(color: str, alpha: float) -> str:
    """``#rrggbb`` or ``#rrggbbaa`` → ``rgba(r,g,b,alpha)``; safe on garbage."""
    try:
        c = color.lstrip("#")
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except Exception:
        r, g, b = 128, 128, 128
    a = f"{alpha:g}"
    return f"rgba({r},{g},{b},{a})"


def _ladder(cal_times, cal_carbons):
    if len(cal_times) != len(cal_carbons):
        raise ValueError(
            f"calibration ladder mismatch: {len(cal_times)} peak times vs "
            f"{len(cal_carbons)} carbon numbers - use distill.calibration_ladder()")
    n = len(cal_times)
    if n < 2:
        raise ValueError(
            f"calibration ladder has {n} point(s); need at least 2 - "
            "use distill.calibration_ladder()")
    ct = np.array(cal_times, dtype=float)
    cn = np.array(cal_carbons, dtype=float)
    if np.any(np.diff(cn) <= 0):
        raise ValueError(
            "calibration ladder carbons are not increasing with retention "
            f"time (carbons={cal_carbons}) - use distill.calibration_ladder()")
    return ct, cn


def carbon_to_time(
    carbon_number: float,
    cal_times: list[float],
    cal_carbons: list[int],
) -> float:
    ct, cn = _ladder(cal_times, cal_carbons)
    return float(np.interp(carbon_number, cn, ct))


def segment_carbon_range(
    t_start: float,
    t_end: float,
    cal_times: list[float],
    cal_carbons: list[int],
) -> str:
    ct, cn = _ladder(cal_times, cal_carbons)
    c_start = np.interp(t_start, ct, cn)
    c_end = np.interp(t_end, ct, cn)
    return f"C{int(round(c_start))}-C{int(round(c_end))}"


def generate_conclusion(
    segments: list[dict],
    ranges: list[dict],
    cal_times: list[float],
    cal_carbons: list[int],
    standard_name: str = "standard",
) -> tuple[str, str]:
    """Return (conclusion_text, bullet_points) matching the desktop version.

    *ranges* is a list of ``{"label": "Gas", "c_start": 5, "c_end": 11}``.
    Uses overlap detection (not midpoint) to match the original logic.
    """
    # ── Bullet points (deviation report) ──────────────────────────────
    if not segments:
        bullets = (
            "No deviations detected above the marginal threshold."
        )
    else:
        lines: list[str] = []
        for seg in sorted(segments, key=lambda s: s["start_time"]):
            a = f"{seg['start_time']:.2f}"
            b = f"{seg['end_time']:.2f}"
            sev = seg["severity"]
            direction = "higher" if seg["direction"] == "positive" else "lower"
            cr = seg.get("carbon_range", "")
            cr_str = f" ({cr})" if cr else ""
            lines.append(
                f"• At {a} min to {b} min{cr_str}: "
                f"{sev} {direction} intensity than {standard_name}"
            )
        bullets = "\n".join(lines)

    # ── Conclusion (overlap-based, matching desktop exactly) ──────────
    # No usable calibration ladder (e.g. nothing configured, or an
    # unreadable/short auto-detect fallback) -> skip carbon-range mapping
    # entirely rather than reach np.interp with too few points. Matches
    # analyze_pair's `len(cal_times) >= 2` guard: segments (and therefore any
    # deviation) never get computed without a usable ladder either.
    range_defs: list[tuple[str, int, int, float, float]] = []
    if len(cal_times) >= 2:
        for rng in ranges:
            c_s, c_e = int(rng["c_start"]), int(rng["c_end"])
            t0 = carbon_to_time(c_s, cal_times, cal_carbons)
            t1 = carbon_to_time(c_e, cal_times, cal_carbons)
            range_defs.append((rng.get("label", "Range"), c_s, c_e, t0, t1))

    range_pos: dict[str, bool] = {}
    for label, c_s, c_e, t0, t1 in range_defs:
        hit = False
        for seg in segments:
            if seg["direction"] != "positive":
                continue
            if seg["start_time"] <= t1 and seg["end_time"] >= t0:
                hit = True
                break
        range_pos[label] = hit

    sn = standard_name
    hit_labels = [label for label, _, _, _, _ in range_defs if range_pos.get(label)]

    if len(hit_labels) == 0:
        conclusion = (
            f"Conclusion: Compared to {sn}, this sample shows no significant deviation "
            "in the defined ranges. The chromatographic profile is consistent "
            "with the reference standard."
        )
    elif len(hit_labels) == 1:
        label = hit_labels[0]
        rng = next(r for r in range_defs if r[0] == label)
        conclusion = (
            f"Conclusion: Compared to {sn}, this sample shows elevated intensity in the "
            f"{label.lower()} range (C{rng[1]}–C{rng[2]}), consistent with possible "
            f"{label.lower()} range contamination. "
            "These findings are indicative only and do not confirm specific substances."
        )
    else:
        parts = []
        for label in hit_labels:
            rng = next(r for r in range_defs if r[0] == label)
            parts.append(f"the {label.lower()} range (C{rng[1]}–C{rng[2]})")
        ranges_str = " and ".join(parts)
        conclusion = (
            f"Conclusion: Compared to {sn}, this sample shows elevated intensity in "
            f"{ranges_str}. This pattern is consistent with mixed contamination. "
            "These findings are indicative only and do not confirm specific substances."
        )

    return conclusion, bullets


# ===================================================================== #
#  Range-driven deviation bullets (phase 3)
# ===================================================================== #
# One bullet per deviating range, one catch-all for what lies outside every
# range, never more than ranges + 1 lines. Rules: the phase 3+4 spec
# (docs/superpowers/specs/2026-09-29-phase3-4-bullets-comments-design.md).
# Everything is evaluated on [t_start, x_max_min], the graph actually shown.
# (generate_conclusion above is the pre-phase-3 per-excursion text, kept only
# as the reference the regression test compares against.)

_EPS = 1e-9
NO_DEVIATION = "No deviations above the marginal threshold."
NO_DEVIATION_IN_RANGES = "No deviations above the marginal threshold within the defined ranges."
OUTSIDE_LABEL = "Outside the defined ranges"
ACROSS_LABEL = "Across the run"
NO_CALIBRATION_LABEL = "Calibration unavailable — ranges not evaluated"


def _has_ladder(ladder) -> bool:
    """A ladder with at least 2 points (anything shorter can't place ranges).
    Longer ladders are validated by ``_ladder`` (mismatch/ordering raise)."""
    if not ladder:
        return False
    times, carbons = ladder
    return len(times) >= 2 and len(carbons) >= 2


def ladder_carbon_to_time(carbon: float, ladder) -> float:
    """Carbon number → retention time: linear inside the ladder, linear
    extrapolation from the end pairs outside it. The one conversion used for
    range windows (UI boxes, PDF boxes and the bullets)."""
    ct, cn = _ladder(*ladder)
    c = float(carbon)
    if c <= cn[0]:
        return float(ct[0] + (ct[1] - ct[0]) / (cn[1] - cn[0]) * (c - cn[0]))
    if c >= cn[-1]:
        return float(ct[-1] + (ct[-1] - ct[-2]) / (cn[-1] - cn[-2]) * (c - cn[-1]))
    return float(np.interp(c, cn, ct))


def ladder_time_to_carbon(t: float, ladder) -> float:
    """The inverse of ``ladder_carbon_to_time`` (same extrapolation)."""
    ct, cn = _ladder(*ladder)
    x = float(t)
    if x <= ct[0]:
        return float(cn[0] + (cn[1] - cn[0]) / (ct[1] - ct[0]) * (x - ct[0]))
    if x >= ct[-1]:
        return float(cn[-1] + (cn[-1] - cn[-2]) / (ct[-1] - ct[-2]) * (x - ct[-1]))
    return float(np.interp(x, ct, cn))


def range_windows(ranges: list[dict], ladder, t_min: float, t_max: float) -> list[dict]:
    """Each range as a time window ``{index, label, color, c_start, c_end, t0,
    t1, clipped, evaluable, c_eval_start, c_eval_end}``, clipped to
    ``[t_min, t_max]`` (the run start and ``min(run end, x_max_min)``).
    ``c_start > c_end`` is swapped; a window of width 0 is not evaluable
    (``t0 = t1 = None``). ``[]`` without a usable ladder (< 2 points)."""
    if not _has_ladder(ladder):
        return []
    _ladder(*ladder)                     # mismatched/unordered ladders raise here
    out = []
    for i, r in enumerate(ranges):
        cs, ce = int(r["c_start"]), int(r["c_end"])
        if cs > ce:
            cs, ce = ce, cs
        # a single-carbon range (C7–C7) spans half a carbon each side
        half = 0.5 if cs == ce else 0.0
        raw0 = ladder_carbon_to_time(cs - half, ladder)
        raw1 = ladder_carbon_to_time(ce + half, ladder)
        t0, t1 = max(raw0, float(t_min)), min(raw1, float(t_max))
        evaluable = t1 - t0 > _EPS
        start_clipped = evaluable and t0 > raw0 + _EPS
        end_clipped = evaluable and t1 < raw1 - _EPS
        out.append({
            "index": i,
            "label": str(r.get("label", "Range")),
            "color": r.get("color") or "",
            "c_start": cs, "c_end": ce,
            "t0": t0 if evaluable else None,
            "t1": t1 if evaluable else None,
            "clipped": bool(start_clipped or end_clipped),
            "evaluable": bool(evaluable),
            "c_eval_start": (math.ceil(ladder_time_to_carbon(t0, ladder) - 1e-6)
                             if start_clipped else cs),
            "c_eval_end": (math.floor(ladder_time_to_carbon(t1, ladder) + 1e-6)
                           if end_clipped else ce),
        })
    return out


def report_params(body: dict, conf: dict) -> dict:
    """The parameters a report is computed with (``params_used``). The
    operator's (trend quantile/window/sigma, the three thresholds, the graph's
    ``x_max_min``) come from the request, else the saved defaults; the admin
    ones (min width, merge gap, spike min width, spike report threshold) only
    from settings. An empty spike report threshold is the moderate threshold
    in effect. ``ValueError`` on a non-numeric value."""
    body = body or {}

    def pick(key, conf_key, default):
        v = body.get(key) if key else None
        if v is None or v == "":
            v = conf.get(conf_key, default)
        if v is None or v == "":
            v = default
        return float(v)

    p = {
        "quantile": pick("quantile", "analysis_quantile", 0.20),
        "window": int(pick("window", "analysis_window", 301)),
        "sigma": pick("sigma", "analysis_sigma", 34.0),
        "thresh_marginal": pick("thresh_marginal", "analysis_thresh_marginal", 100),
        "thresh_moderate": pick("thresh_moderate", "analysis_thresh_moderate", 500),
        "thresh_significant": pick("thresh_significant", "analysis_thresh_significant", 2000),
        "x_max_min": pick("x_max_min", "analysis_x_max_min", 7.0),
        "min_width_min": pick(None, "analysis_min_width_min", 0.05),
        "merge_gap_min": pick(None, "analysis_merge_gap_min", 0.10),
        "spike_min_width_min": pick(None, "analysis_spike_min_width_min",
                                    DEFAULT_SPIKE_MIN_WIDTH_MIN),
    }
    p["spike_max_fwhm_min"] = pick(None, "analysis_spike_max_fwhm_min", 0.20)
    p["spike_min_dominance"] = pick(None, "analysis_spike_min_dominance", 0.6)
    srt = conf.get("analysis_spike_report_threshold", "")
    p["spike_report_threshold"] = float(srt) if srt not in (None, "") else p["thresh_moderate"]
    for k, v in p.items():
        if not math.isfinite(v):
            raise ValueError(f"{k} must be a finite number")
    if p["window"] < 1:
        raise ValueError("window must be at least 1")
    return p


#: The deviation-bullet admin settings and whether an empty value is allowed
#: (the spike report threshold: empty = the moderate threshold).
ANALYSIS_NUMERIC_SETTINGS = {
    "analysis_min_width_min": False,
    "analysis_merge_gap_min": False,
    "analysis_spike_min_width_min": False,
    "analysis_spike_max_fwhm_min": False,
    "analysis_spike_min_dominance": False,
    "analysis_spike_report_threshold": True,
}


def invalid_analysis_settings(changes: dict) -> list[str]:
    """The keys of *changes* that are deviation-bullet settings with a value
    that isn't a finite number ≥ 0 (empty allowed only where it means the
    default)."""
    bad = []
    for key, allow_empty in ANALYSIS_NUMERIC_SETTINGS.items():
        if key not in changes:
            continue
        v = changes[key]
        if (v is None or str(v).strip() == "") and allow_empty:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            bad.append(key)
            continue
        if not math.isfinite(f) or f < 0:
            bad.append(key)
    return bad


def severity_of(peak_abs: float, params: dict) -> str | None:
    """The threshold name a peak reaches (the UI's labels), else None."""
    if peak_abs >= params["thresh_significant"]:
        return "significant"
    if peak_abs >= params["thresh_moderate"]:
        return "moderate"
    if peak_abs >= params["thresh_marginal"]:
        return "marginal"
    return None


def _mask_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs as inclusive (start, end) index pairs (vectorized)."""
    if not len(mask):
        return []
    m = np.concatenate(([False], np.asarray(mask, dtype=bool), [False]))
    d = np.diff(m.astype(np.int8))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1) - 1
    return list(zip(starts.tolist(), ends.tolist()))


def _signed_runs(v: np.ndarray, level: float, n_ext: int) -> list[tuple[int, int, int]]:
    """``(start, end, sign)`` runs of ``v >= level`` / ``v <= -level`` in the
    evaluation extent (the first *n_ext* points), sorted by start."""
    head = v[:n_ext]
    runs = [(s, e, 1) for s, e in _mask_runs(head >= level)]
    runs += [(s, e, -1) for s, e in _mask_runs(head <= -level)]
    return sorted(runs)


def _piece(t: np.ndarray, v: np.ndarray, s: int, e: int, sign: int) -> dict:
    seg = v[s:e + 1]
    a = np.abs(seg)
    k = int(np.argmax(a))
    area = float(np.sum((a[:-1] + a[1:]) * 0.5 * np.diff(t[s:e + 1]))) if e > s else 0.0
    return {"s": s, "e": e, "sign": sign, "t0": float(t[s]), "t1": float(t[e]),
            "width": float(t[e] - t[s]), "peak": float(seg[k]), "peak_t": float(t[s + k]),
            "area": area}


def _extent(t: np.ndarray, params: dict) -> int:
    """How many leading points lie on the displayed axis (t ≤ x_max_min)."""
    return int(np.searchsorted(t, float(params["x_max_min"]) + _EPS, side="right"))


def _fwhm(t: np.ndarray, v: np.ndarray, k: int, sign: int) -> float:
    """Full width at half height of the peak of ``sign * v`` at index *k*."""
    half = abs(v[k]) / 2.0
    a = k
    while a > 0 and sign * v[a] > half:
        a -= 1
    b = k
    while b < len(v) - 1 and sign * v[b] > half:
        b += 1
    return float(t[b] - t[a])


def find_spikes(t: np.ndarray, spike_diff: np.ndarray, params: dict,
                heights=None) -> list[dict]:
    """Qualifying sharp peaks of the spike channel, on the displayed axis:

    * a run with |diff| ≥ marginal at least ``spike_min_width_min`` wide,
      whose apex reaches ``spike_report_threshold``;
    * **sharp**: its full width at half height is ≤ ``spike_max_fwhm_min``
      (a broad hump is the trend channel's, never a "sharp peak");
    * **dominant** (when *heights* ``(sample, standard)`` — each smoothed
      signal above its own low-quantile baseline — are given):
      |diff at the apex| ≥ ``spike_min_dominance`` × the larger local peak
      height, so a height difference of a peak both runs share is not a
      sharp peak; a peak one of them lacks is.

    Each is ``{t, sign, value, t0, t1, area, fwhm, dominance}`` (``t`` = the
    apex; ``dominance`` None without *heights*)."""
    t = np.asarray(t, dtype=float)
    v = np.asarray(spike_diff, dtype=float)
    hs = ht = None
    if heights is not None:
        hs, ht = (np.asarray(h, dtype=float) for h in heights)
    out = []
    for s, e, sign in _signed_runs(v, params["thresh_marginal"], _extent(t, params)):
        p = _piece(t, v, s, e, sign)
        if p["width"] + _EPS < params["spike_min_width_min"]:
            continue
        if abs(p["peak"]) + _EPS < params["spike_report_threshold"]:
            continue
        k = s + int(np.argmax(np.abs(v[s:e + 1])))
        fwhm = _fwhm(t, v, k, sign)
        if fwhm > params["spike_max_fwhm_min"] + _EPS:
            continue
        dominance = None
        if hs is not None:
            dominance = abs(p["peak"]) / max(float(hs[k]), float(ht[k]), 1.0)
            if dominance + _EPS < params["spike_min_dominance"]:
                continue
        out.append({"t": p["peak_t"], "sign": sign, "value": p["peak"],
                    "t0": p["t0"], "t1": p["t1"], "area": p["area"],
                    "fwhm": fwhm, "dominance": dominance})
    return out


#: Minimum normalized cross-correlation of the two runs' derivatives for a
#: shift to be applied (below it they share no peak pattern to align on).
ALIGN_MIN_CORRELATION = 0.5


def align_to_standard(t: np.ndarray, y_sample: np.ndarray, y_std: np.ndarray,
                      max_lag_min: float = 0.02) -> tuple[np.ndarray, float]:
    """The sample shifted onto the standard by one global sub-sample lag
    (cross-correlation of the smoothed first derivatives, searched within
    ±*max_lag_min*, refined by a parabola): ``(aligned sample, lag in
    minutes)``. The spike channel's input only — a retention drift between
    two runs of the same product must not read as sharp peaks. Input too
    short for the search is returned unchanged with lag 0."""
    t = np.asarray(t, dtype=float)
    ys = np.asarray(y_sample, dtype=float)
    yst = np.asarray(y_std, dtype=float)
    n = len(t)
    if n < 3:
        return ys, 0.0
    dt = float(np.median(np.diff(t)))
    lag_pts = int(max_lag_min / dt) if dt > 0 else 0
    if lag_pts < 1 or n <= 2 * lag_pts + 2:
        return ys, 0.0
    smooth = (lambda y: gaussian_filter1d(y, 2.0)) if gaussian_filter1d is not None \
        else (lambda y: y)
    a = np.gradient(smooth(ys))
    b = np.gradient(smooth(yst))[lag_pts:-lag_pts]
    lags = np.arange(-lag_pts, lag_pts + 1)
    cc = np.array([np.dot(np.roll(a, k)[lag_pts:-lag_pts], b) for k in lags])
    i = int(np.argmax(cc))
    norm = float(np.linalg.norm(a[lag_pts:-lag_pts]) * np.linalg.norm(b))
    if norm <= 0 or cc[i] / norm < ALIGN_MIN_CORRELATION:
        return ys, 0.0          # no shared peak pattern to align on
    frac = 0.0
    if 0 < i < len(lags) - 1:
        y0, y1, y2 = cc[i - 1], cc[i], cc[i + 1]
        denom = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / denom if denom != 0 else 0.0
    lag = float(np.clip((lags[i] + frac) * dt, -max_lag_min, max_lag_min))
    return np.interp(t, t + lag, ys), lag


def _clip(runs, intervals, t, v, min_width) -> list[dict]:
    """Runs intersected with index *intervals*; pieces narrower than
    *min_width* (minutes, after clipping) are dropped."""
    out = []
    for s, e, sign in runs:
        for a, b in intervals:
            s2, e2 = max(s, a), min(e, b)
            if s2 > e2:
                continue
            p = _piece(t, v, s2, e2, sign)
            if p["width"] + _EPS >= min_width:
                out.append(p)
    return sorted(out, key=lambda p: p["s"])


def _merge_spans(pieces: list[dict], gap: float) -> list[list[float]]:
    spans: list[list[float]] = []
    for p in sorted(pieces, key=lambda p: p["t0"]):
        if spans and p["t0"] - spans[-1][1] < gap - _EPS:
            spans[-1][1] = max(spans[-1][1], p["t1"])
        else:
            spans.append([p["t0"], p["t1"]])
    return [[round(a, 4), round(b, 4)] for a, b in spans]


_SEV_RANK = {"marginal": 0, "moderate": 1, "significant": 2}


def _max_sev(*sevs) -> str | None:
    sevs = [s for s in sevs if s]
    return max(sevs, key=lambda s: _SEV_RANK[s]) if sevs else None


def _assess(pieces: list[dict], spikes: list[dict], params: dict) -> dict | None:
    """Direction, verdict, severity, max, "also" and spans for one window
    (or the outside), from its qualifying trend pieces and spikes; None when
    neither.

    * ``direction`` — the dominant direction by area (trend pieces; spikes
      only when there are none).
    * ``also`` — trend runs the other way reaching moderate; with one the
      range is **mixed** (``verdict`` "mixed", ``mixed`` True).
    * ``verdict`` — "higher" / "lower" / "mixed": what the bullet leads with
      and what the conclusion says, so the two always agree. Sharp peaks
      never change it: they are named on their own.
    * ``severity`` — the bullet's tag: the dominant direction's largest
      |diff| (trend or spike), or the larger of both directions when mixed.
    * ``broad_severity`` — the same from the trend runs only (None for
      sharp peaks only): the conclusion's adverb.
    * ``elevated`` — the bullet reports something higher (a higher or mixed
      verdict, or a sharp peak above the standard)."""
    if not pieces and not spikes:
        return None
    spike_only = not pieces
    basis = [(p["sign"], p["area"]) for p in pieces] if pieces else \
            [(s["sign"], s["area"]) for s in spikes]
    area = {1: sum(a for sg, a in basis if sg == 1), -1: sum(a for sg, a in basis if sg == -1)}
    cands = [(abs(p["peak"]), p["peak"], p["peak_t"], p["sign"]) for p in pieces] + \
            [(abs(s["value"]), s["value"], s["t"], s["sign"]) for s in spikes]
    if area[1] > area[-1] + _EPS:
        dom = 1
    elif area[-1] > area[1] + _EPS:
        dom = -1
    else:
        dom = max(cands, key=lambda c: (c[0], -c[2]))[3]
    dom_cands = [c for c in cands if c[3] == dom] or cands
    peak_abs, peak, peak_t, _ = max(dom_cands, key=lambda c: (c[0], -c[2]))
    dom_pieces = [p for p in pieces if p["sign"] == dom]
    broad_dom = (severity_of(max(abs(p["peak"]) for p in dom_pieces), params) or "marginal") \
        if dom_pieces else None
    also = None
    opp = [p for p in pieces if p["sign"] == -dom
           and abs(p["peak"]) + _EPS >= params["thresh_moderate"]]
    if opp:
        also = {"direction": "higher" if dom == -1 else "lower",
                "severity": _max_sev(*(severity_of(abs(p["peak"]), params) for p in opp)),
                "spans": _merge_spans(opp, params["merge_gap_min"])}
    direction = "higher" if dom == 1 else "lower"
    mixed = also is not None
    severity = severity_of(peak_abs, params) or "marginal"
    if mixed:
        severity = _max_sev(severity, also["severity"])
    return {
        "direction": direction,
        "verdict": "mixed" if mixed else direction,
        "severity": severity,
        "broad_severity": _max_sev(broad_dom, also and also["severity"]),
        "dominant_severity": broad_dom,
        "max_diff": round(peak, 1),
        "max_at": round(peak_t, 4),
        "spikes": [{"t": round(s["t"], 4), "sign": s["sign"], "value": round(s["value"], 1),
                    "severity": severity_of(abs(s["value"]), params) or "marginal"}
                   for s in spikes],
        "spike_only": spike_only,
        "also": also,
        "spans": _merge_spans(dom_pieces, params["merge_gap_min"]),
        "mixed": mixed,
        "elevated": (dom == 1 or mixed or any(s["sign"] == 1 for s in spikes)),
    }


def _item(kind: str, **fields) -> dict:
    base = {"kind": kind, "index": None, "label": None, "c_start": None, "c_end": None,
            "t0": None, "t1": None, "clipped": False, "c_eval_start": None,
            "c_eval_end": None, "direction": None, "verdict": None, "severity": None,
            "broad_severity": None, "dominant_severity": None, "position": None,
            "max_diff": None, "max_at": None, "frac_above": None, "spikes": [],
            "spike_only": False, "also": None, "spans": [], "mixed": False,
            "elevated": False}
    base.update(fields)
    return base


# ── where a range sits in the standard's distribution ─────────────────
#: The standard's carbon distribution when none was measured (a typical
#: middle distillate): C at 10%, 50% and 90% of its area.
DEFAULT_STD_PROFILE = {"c10": 10.0, "c50": 16.0, "c90": 22.0}
#: The solvent window skipped when the standard's area is read (as
#: ``distill.BLANK_SOLVENT_END_MIN``; the CS2 peak would swamp the light end).
PROFILE_SOLVENT_END_MIN = 0.35


def standard_profile(t, y_std, ladder, params: dict,
                     solvent_end_min: float = PROFILE_SOLVENT_END_MIN) -> dict | None:
    """The standard's carbon distribution ``{c10, c50, c90}``: the carbon
    numbers at 10/50/90% of its area above its own floor (the 2nd
    percentile), after the solvent window and up to ``x_max_min``. None
    without a usable ladder or any area."""
    if not _has_ladder(ladder):
        return None
    t = np.asarray(t, dtype=float)
    y = np.asarray(y_std, dtype=float)
    n = _extent(t, params)
    m = np.zeros(len(t), dtype=bool)
    m[:n] = True
    m &= t > solvent_end_min
    if int(m.sum()) < 3:
        return None
    tt, yy = t[m], y[m]
    w = np.clip(yy - float(np.percentile(yy, 2)), 0.0, None)
    cum = np.concatenate(([0.0], np.cumsum((w[1:] + w[:-1]) * 0.5 * np.diff(tt))))
    total = float(cum[-1])
    if not math.isfinite(total) or total <= 0:
        return None
    return {f"c{int(q * 100)}": round(ladder_time_to_carbon(float(np.interp(q * total, cum, tt)),
                                                           ladder), 2)
            for q in (0.1, 0.5, 0.9)}


def range_position(c_start: float, c_end: float, profile: dict | None) -> str:
    """"light", "middle" or "heavy": the range's middle carbon against the
    standard's C10–C90 (``DEFAULT_STD_PROFILE`` without one). Never from the
    label: a C5–C11 range is the light end of a diesel and the body of a
    gasoline."""
    p = profile or DEFAULT_STD_PROFILE
    mid = (float(c_start) + float(c_end)) / 2.0
    if mid < p["c10"]:
        return "light"
    if mid > p["c90"]:
        return "heavy"
    return "middle"


def build_deviation_report(
    t: np.ndarray,
    trend_diff: np.ndarray,
    spike_diff: np.ndarray,
    *,
    ranges: list[dict],
    ladder,
    params: dict,
    spike_heights=None,
    std_profile: dict | None = None,
) -> list[dict]:
    """The structured deviation report: one item per bullet line.
    *spike_heights* ``(sample, standard)`` enables the dominance test of
    ``find_spikes``; *std_profile* (``standard_profile``) places each range
    in the standard's distribution (``position``: light / middle / heavy,
    ``DEFAULT_STD_PROFILE`` without one).

    Kinds: ``range`` (a deviating range), ``not-evaluated`` (its window has
    width 0), ``none`` (``within_ranges`` tells which sentence), ``outside``
    (``label`` "Outside the defined ranges", or "Across the run" with zero
    ranges) and ``no-calibration`` (ranges given but no usable ladder;
    ``deviates`` False when nothing qualifies). ``render_bullets`` turns them
    into text; there are never more than ``len(ranges) + 1`` items (1 with
    zero ranges)."""
    t = np.asarray(t, dtype=float)
    trend = np.asarray(trend_diff, dtype=float)
    spike = np.asarray(spike_diff, dtype=float)
    n_ext = _extent(t, params)
    if n_ext == 0:
        return [_item("none", within_ranges=False)]
    runs = _signed_runs(trend, params["thresh_marginal"], n_ext)
    spikes = find_spikes(t, spike, params, heights=spike_heights)
    min_w = params["min_width_min"]
    whole = [(0, n_ext - 1)]

    if ranges and not _has_ladder(ladder):
        a = _assess(_clip(runs, whole, t, trend, min_w), spikes, params)
        if a is None:
            return [_item("no-calibration", label=NO_CALIBRATION_LABEL, deviates=False)]
        return [_item("no-calibration", label=NO_CALIBRATION_LABEL, deviates=True, **a)]

    t_max = float(t[n_ext - 1])
    windows = range_windows(ranges, ladder, float(t[0]), t_max) if ranges else []
    items: list[dict] = []
    covered: list[tuple[int, int]] = []
    evaluable = 0
    for w in windows:
        common = dict(index=w["index"], label=w["label"], c_start=w["c_start"],
                      c_end=w["c_end"])
        if not w["evaluable"]:
            items.append(_item("not-evaluated", **common))
            continue
        evaluable += 1
        a_i = int(np.searchsorted(t, w["t0"] - _EPS, side="left"))
        b_i = min(int(np.searchsorted(t, w["t1"] + _EPS, side="right")) - 1, n_ext - 1)
        if a_i > b_i:
            continue
        covered.append((a_i, b_i))
        pieces = _clip(runs, [(a_i, b_i)], t, trend, min_w)
        inside = [s for s in spikes if w["t0"] - _EPS <= s["t"] <= w["t1"] + _EPS]
        a = _assess(pieces, inside, params)
        if a is None:
            continue
        width = w["t1"] - w["t0"]
        frac = sum(p["width"] for p in pieces) / width if width > 0 else 0.0
        items.append(_item("range", t0=w["t0"], t1=w["t1"], clipped=w["clipped"],
                           c_eval_start=w["c_eval_start"], c_eval_end=w["c_eval_end"],
                           frac_above=round(min(frac, 1.0), 4),
                           position=range_position(w["c_eval_start"], w["c_eval_end"],
                                                   std_profile),
                           **common, **a))

    # The complement of every evaluable window, on the displayed axis.
    free: list[tuple[int, int]] = []
    cursor = 0
    for a_i, b_i in sorted(covered):
        if a_i > cursor:
            free.append((cursor, a_i - 1))
        cursor = max(cursor, b_i + 1)
    if cursor <= n_ext - 1:
        free.append((cursor, n_ext - 1))
    out_pieces = _clip(runs, free, t, trend, min_w)
    in_any = [(w["t0"], w["t1"]) for w in windows if w["evaluable"]]
    out_spikes = [s for s in spikes
                  if not any(t0 - _EPS <= s["t"] <= t1 + _EPS for t0, t1 in in_any)]
    outside = _assess(out_pieces, out_spikes, params)

    if not any(i["kind"] == "range" for i in items):
        if outside is None:
            items.append(_item("none", within_ranges=False))
        elif evaluable:
            items.append(_item("none", within_ranges=True))
    if outside is not None:
        items.append(_item("outside", label=OUTSIDE_LABEL if ranges else ACROSS_LABEL,
                           **outside))
    return items


def _span_text(spans, limit: int) -> str:
    shown = ", ".join(f"{a:.2f}–{b:.2f}" for a, b in spans[:limit]) + " min"
    if len(spans) > limit:
        shown += f", +{len(spans) - limit} more"
    return shown


def _peak_times(group: list[dict]) -> str:
    """"at a, b min" for ≤ 3 peaks, else "incl." the 3 largest (time order)."""
    if len(group) <= 3:
        return "at " + ", ".join(f"{x['t']:.2f}" for x in sorted(group, key=lambda s: s["t"])) \
            + " min"
    top = sorted(group, key=lambda s: -abs(s["value"]))[:3]
    return "incl. " + ", ".join(f"{x['t']:.2f}" for x in sorted(top, key=lambda s: s["t"])) \
        + " min"


def _spike_text(spikes: list[dict]) -> str:
    """"N sharp peaks above[, M below] the standard at …"."""
    up = [x for x in spikes if x["sign"] == 1]
    down = [x for x in spikes if x["sign"] == -1]
    if up and down:
        return (f"{len(up)} sharp peak{'s' if len(up) != 1 else ''} above, {len(down)} below "
                f"the standard {_peak_times(up + down)}")
    group, word = (up, "above") if up else (down, "below")
    return (f"{len(group)} sharp peak{'s' if len(group) != 1 else ''} {word} the standard "
            f"{_peak_times(group)}")


def _pct(frac: float) -> str:
    p = frac * 100.0
    if 0 < p < 1:
        return "<1%"
    return f"{int(p + 0.5)}%"


_POS_SHORT = {"light": "light end", "middle": "main body", "heavy": "heavy end"}


def _assessment_text(it: dict, standard_name: str, *, spans: bool) -> str:
    """``<direction> than <std> — <severity>[, <part> elevated|reduced]
    (…)`` for a range/outside item. The direction is the item's verdict;
    sharp peaks only read "sharp peaks above|below <std>"."""
    sn = standard_name
    sev = it["severity"]
    if it["spike_only"]:
        up = any(x["sign"] == 1 for x in it["spikes"])
        down = any(x["sign"] == -1 for x in it["spikes"])
        where = "above and below" if up and down else ("above" if up else "below")
        return (f"sharp peaks {where} {sn} — {sev} ({_spike_text(it['spikes'])}; "
                "no broad deviation above the marginal threshold)")
    part = _POS_SHORT.get(it.get("position"))
    details = []
    if it["verdict"] == "mixed":
        head = f"mixed, higher and lower than {sn} — {sev}"
        if part:
            head += f", {part} differs in shape"
        mostly = f"mostly {it['direction']}, {it['dominant_severity']}"
        if spans and it["spans"]:
            mostly += " at " + _span_text(it["spans"], 3)
        also = it["also"]
        details += [mostly, f"{also['direction']} {also['severity']} at "
                    + _span_text(also["spans"], 2)]
    else:
        head = f"{it['verdict']} than {sn} — {sev}"
        if part:
            head += f", {part} {'elevated' if it['verdict'] == 'higher' else 'reduced'}"
        if spans and it["spans"]:
            head += " at " + _span_text(it["spans"], 3)
    details.append(f"max {it['max_diff']:+.0f} at {it['max_at']:.2f} min")
    if it["frac_above"] is not None:
        details.append(f"{_pct(it['frac_above'])} of range beyond the marginal threshold")
    if it["spikes"]:
        details.append(_spike_text(it["spikes"]))
    return f"{head} ({'; '.join(details)})"


def render_bullets(items: list[dict], standard_name: str) -> str:
    """The bullet text of a ``build_deviation_report`` result (one line per
    item). Plain text: the report HTML escapes it. Each deviating line is
    ``• <head>: <direction> than <std> — <severity>…``: ``report_layout``
    sets the head (up to the first ``(Cx–Cy…):`` or ``:``) in bold and reads
    the severity tag from the first ``— <severity>``; Compare splits the
    head at the item's label."""
    lines = []
    for it in items:
        kind = it["kind"]
        if kind == "none":
            lines.append(NO_DEVIATION_IN_RANGES if it.get("within_ranges") else NO_DEVIATION)
        elif kind == "not-evaluated":
            lines.append(f"• {it['label']} (C{it['c_start']}–C{it['c_end']}): "
                         "not evaluated — outside the evaluated window "
                         "(calibration, run end or x-axis limit)")
        elif kind == "range":
            clip = ""
            if it["clipped"]:
                lo = it["c_eval_start"] != it["c_start"]
                hi = it["c_eval_end"] != it["c_end"]
                if lo and hi:
                    clip = f", evaluated C{it['c_eval_start']}–C{it['c_eval_end']}"
                elif lo:
                    clip = f", evaluated from C{it['c_eval_start']}"
                else:
                    clip = f", evaluated to C{it['c_eval_end']}"
            lines.append(f"• {it['label']} (C{it['c_start']}–C{it['c_end']}{clip}): "
                         + _assessment_text(it, standard_name, spans=False))
        elif kind == "no-calibration" and not it.get("deviates"):
            lines.append(f"• {it['label']}: no deviations above the marginal threshold.")
        else:   # outside / no-calibration with a deviation
            lines.append(f"• {it['label']}: "
                         + _assessment_text(it, standard_name, spans=True))
    return "\n".join(lines)


# ── the conclusion ─────────────────────────────────────────────────────
# Wording rules: docs/superpowers/specs/2026-10-07-v7-conclusions.md. The
# conclusion is built from the same items as the bullets and says the same
# direction per range (the item's ``verdict``); it is indicative and never
# names a substance as present.

INDICATIVE = "These findings are indicative only and do not confirm specific substances."
_ADVERB = {"significant": "significantly", "moderate": "moderately", "marginal": "slightly"}
_ADJECTIVE = {"significant": "significant", "moderate": "moderate", "marginal": "slight"}
_POS_LONG = {"light": "light end", "middle": "main body of the distribution",
             "heavy": "heavy end"}
#: Range findings written out in full; the rest share one short sentence.
CONCLUSION_FULL_FINDINGS = 3
CONCLUSION_SHORT_FINDINGS = 4
#: The generated conclusion's length budget (``comments.CONCLUSION_MAX``,
#: the cap an edited one has and the report prints whole).
CONCLUSION_BUDGET = 1500
_PLAIN_WORD = re.compile(r"[A-Z]?[a-z]+")


def range_name(label) -> str:
    """A range's label as the conclusion names it: plain words lower-cased
    ("Gas" → "gas", "Lube oil" → "lube oil"), anything else as written
    ("GRO", "C10-C28 DRO"); a trailing "range" is dropped (the sentence
    adds it)."""
    words = str(label or "").split()
    if len(words) > 1 and words[-1].lower() == "range":
        words = words[:-1]
    if words and all(_PLAIN_WORD.fullmatch(w) for w in words):
        return " ".join(w.lower() for w in words)
    return " ".join(words)


def _finding_text(h: dict, sn: str) -> str:
    """One broad range finding: "<adverb> <elevated|lower> intensity in the
    <name> range (Cx–Cy): <what that means>, consistent with <cause>"."""
    name = f"the {h['name']} range (C{h['c_start']}–C{h['c_end']})"
    pos = h["position"] or range_position(h["c_start"], h["c_end"], None)
    sev = h["broad_severity"]
    if h["verdict"] == "mixed":
        also = h["also"]
        return (f"{_ADJECTIVE[sev]} deviations in both directions in {name}: the "
                f"{_POS_LONG[pos]} differs in shape from {sn} rather than only in amount "
                f"(mostly {h['direction']}; {also['direction']} at "
                f"{_span_text(also['spans'], 2)}), consistent with a different product "
                "or a blend")
    adv = _ADVERB[sev]
    if h["verdict"] == "higher":
        if pos == "light":
            boiling = " (gasoline-range)" if h["c_end"] <= 12 else ""
            what = (f"more light-end material than {sn}, consistent with possible "
                    f"light-end{boiling} contamination")
        elif pos == "heavy":
            cause = ("heavier components such as lube/oil-range material"
                     if h["c_start"] >= 20 else "heavier components (a heavier cut or blend)")
            what = f"more heavy-end material than {sn}, consistent with {cause}"
        else:
            what = (f"more material in the main body of the distribution than {sn}, "
                    "consistent with a different blend or product")
        return f"{adv} elevated intensity in {name}: {what}"
    if pos == "light":
        what = (f"the light end is reduced compared with {sn}, consistent with a heavier cut "
                "or loss of light components (weathering/evaporation)")
    elif pos == "heavy":
        what = (f"the heavy end is reduced compared with {sn}, consistent with a lighter cut "
                "or dilution with a lighter product")
    else:
        what = (f"the main body of the distribution is reduced compared with {sn}, "
                "consistent with dilution by a lighter or heavier product")
    return f"{adv} lower intensity in {name}: {what}"


def _short_finding(h: dict) -> str:
    word = {"higher": "higher", "lower": "lower", "mixed": "higher and lower"}[h["verdict"]]
    return (f"{_ADVERB[h['broad_severity']]} {word} in the {h['name']} range "
            f"(C{h['c_start']}–C{h['c_end']})")


def _together(hits: list[dict], sn: str) -> str | None:
    """What a light-end and a heavy-end finding mean together."""
    light = {h["verdict"] for h in hits if h["position"] == "light"}
    heavy = {h["verdict"] for h in hits if h["position"] == "heavy"}
    if len(light) != 1 or len(heavy) != 1 or "mixed" in light | heavy:
        return None
    pair = (light.pop(), heavy.pop())
    return {
        ("lower", "higher"): ("Together, a reduced light end and an elevated heavy end "
                              f"indicate a heavier overall distribution than {sn}."),
        ("higher", "lower"): ("Together, an elevated light end and a reduced heavy end "
                              f"indicate a lighter overall distribution than {sn}."),
        ("higher", "higher"): ("Elevated light and heavy ends together are consistent with a "
                               "blend of a lighter and a heavier product (possible mixed "
                               "contamination)."),
        ("lower", "lower"): ("Reduced light and heavy ends together indicate a narrower "
                             f"distribution than {sn}, concentrated in its main body."),
    }[pair]


def _range_hits(items: list[dict]) -> list[dict]:
    """The broad range findings, one per carbon span and verdict (ranges
    with the same span and verdict are named together: "gas / gasoline"),
    ordered by severity (significant first), then by range order."""
    groups: dict[tuple, dict] = {}
    for i in items:
        if i["kind"] != "range" or i["spike_only"]:
            continue
        key = (i["c_start"], i["c_end"], i["verdict"])
        name = range_name(i["label"])
        g = groups.get(key)
        if g is None:
            groups[key] = dict(i, name=name, names=[name])
        elif name.lower() not in (n.lower() for n in g["names"]):
            g["names"].append(name)
            g["name"] = " / ".join(g["names"])
    return sorted(groups.values(),
                  key=lambda h: (-_SEV_RANK[h["broad_severity"]], h["index"]))


def _spike_clause(items: list[dict], located: bool) -> str | None:
    """"isolated sharp peaks above the standard at … (in the gas range),
    which may indicate specific added components[, and … below …]"."""
    where: dict[float, list[str]] = {}
    for it in items:
        for s in it.get("spikes") or []:
            if it["kind"] == "range":
                place = range_name(it["label"])
            else:
                place = "outside"
            names = where.setdefault(s["t"], [])
            if place not in names:
                names.append(place)
    spikes = counted_spikes(items)
    parts = []
    for sign, word, meaning in ((1, "above", ("specific added components",
                                              "a specific added component")),
                                (-1, "below", ("specific components missing from the sample",
                                               "a specific component missing from the sample"))):
        group = [s for s in spikes if s["sign"] == sign]
        if not group:
            continue
        one = len(group) == 1
        text = (f"{'an isolated sharp peak' if one else 'isolated sharp peaks'} {word} the "
                f"standard {_peak_times(group)}")
        bits = []
        if located:
            places: list[str] = []
            for s in group:
                for p in where.get(s["t"], []):
                    if p not in places:
                        places.append(p)
            inside = [p for p in places if p != "outside"]
            if len(inside) == 1:
                bits.append(f"in the {inside[0]} range")
            elif 1 < len(inside) <= 3:
                bits.append(f"in the {', '.join(inside[:-1])} and {inside[-1]} ranges")
            elif inside:
                bits.append(f"in {len(inside)} of the defined ranges")
            if "outside" in places:
                bits.append("outside the defined ranges")
        # the largest one's size and grade (the bullet's tag may come from it)
        top = max(group, key=lambda x: abs(x["value"]))
        grade = (f"{'' if one else 'up to '}{top['value']:+.0f}, "
                 f"{top.get('severity') or 'marginal'}")
        text += f" ({' and '.join(bits)}; {grade})" if bits else f" ({grade})"
        text += f", which may indicate {meaning[1] if one else meaning[0]}"
        parts.append(text)
    return ", and ".join(parts) if parts else None


def _outside_text(o: dict, sn: str) -> str:
    """"moderately higher than <std> at a–b min" / "different from <std> in
    both directions (moderate), mostly higher at a–b min"."""
    sev = o["broad_severity"]
    spans = (" at " + _span_text(o["spans"], 3)) if o["spans"] else ""
    if o["verdict"] == "mixed":
        return (f"different from {sn} in both directions ({_ADJECTIVE[sev]}), mostly "
                f"{o['direction']}{spans}")
    return f"{_ADVERB[sev]} {o['verdict']} than {sn}{spans}"


def deviation_conclusion(items: list[dict], ranges: list[dict], standard_name: str) -> str:
    """The conclusion from the same items as the bullets: every range it
    names reads in its bullet's direction (``verdict``), the broad range
    findings ordered by severity, then what the light- and heavy-end
    findings mean together, the deviation outside the ranges, and the sharp
    peaks on their own. Indicative only. Within ``CONCLUSION_BUDGET``
    characters: the first ``CONCLUSION_FULL_FINDINGS`` findings in full and
    up to ``CONCLUSION_SHORT_FINDINGS`` more in short, fewer of each while
    the text is too long (a standard name of hundreds of characters could
    still exceed it; the report and Compare then shorten it as any other).
    Wording: docs/superpowers/specs/2026-10-07-v7-conclusions.md."""
    sn = standard_name
    if any(i["kind"] == "no-calibration" for i in items):
        return (f"Compared to {sn}, the defined ranges could not be evaluated "
                "because no usable calibration was available for this sample.")
    outside = next((i for i in items if i["kind"] == "outside"), None)
    if not ranges:
        if outside is None:
            return (f"Compared to {sn}, this sample shows no significant deviation across the "
                    "run. The chromatographic profile is consistent with the reference standard.")
        out = []
        if outside["spike_only"]:
            out.append(f"Compared to {sn}, this sample shows no broad deviation across the run, "
                       f"but {_spike_clause(items, located=False)}.")
        else:
            sev = outside["broad_severity"]
            spans = _span_text(outside["spans"], 3) if outside["spans"] else ""
            if outside["verdict"] == "mixed":
                out.append(f"Compared to {sn}, this sample differs from it in both directions "
                           f"across the run ({_ADJECTIVE[sev]}), mostly {outside['direction']}"
                           + (f" at {spans}" if spans else "") + ".")
            else:
                out.append(f"Compared to {sn}, this sample is {_ADVERB[sev]} "
                           f"{outside['verdict']} across the run"
                           + (f", at {spans}" if spans else "") + ".")
            clause = _spike_clause(items, located=False)
            if clause:
                out.append(f"It also shows {clause}.")
        out.append("No ranges were defined to attribute the deviation.")
        out.append(INDICATIVE)
        return " ".join(out)

    hits = _range_hits(items)
    range_spikes = any(i["kind"] == "range" and i["spikes"] for i in items)
    if not hits and outside is None and not range_spikes:
        return (f"Compared to {sn}, this sample shows no significant deviation in the defined "
                "ranges. The chromatographic profile is consistent with the reference standard.")
    # the most that fits the budget: fewer findings in full, then fewer
    # named in short, as the text grows
    for n_full, n_short in ((CONCLUSION_FULL_FINDINGS, CONCLUSION_SHORT_FINDINGS),
                            (3, 2), (2, 2), (1, 2), (1, 0)):
        text = _ranges_conclusion(items, sn, hits, outside, range_spikes, n_full, n_short)
        if len(text) <= CONCLUSION_BUDGET:
            break
    return text


def _ranges_conclusion(items, sn, hits, outside, range_spikes, n_full, n_short) -> str:
    out = []
    spikes_said = False
    if hits:
        full = hits[:n_full]
        out.append(f"Compared to {sn}, this sample shows {_finding_text(full[0], sn)}.")
        out += [f"It also shows {_finding_text(h, sn)}." for h in full[1:]]
        rest = hits[n_full:]
        if rest:
            shown = "; ".join(_short_finding(h) for h in rest[:n_short])
            more = len(rest) - n_short
            if more > 0 and not n_short:
                shown = f"{more} more range{'s' if more != 1 else ''} (see the findings)"
            elif more > 0:
                shown += f"; and {more} more (see the findings)"
            out.append(f"Further deviations: {shown}.")
        together = _together(hits, sn)
        if together:
            out.append(together)
    elif range_spikes:
        out.append(f"Compared to {sn}, this sample shows no broad deviation in the defined "
                   f"ranges, but {_spike_clause(items, located=True)}.")
        spikes_said = True
    else:
        out.append(f"Compared to {sn}, this sample shows no significant deviation in the "
                   "defined ranges.")
    broad_outside = outside is not None and not outside["spike_only"]
    if broad_outside:
        out.append(f"Outside the defined ranges it is {_outside_text(outside, sn)}.")
    if not spikes_said:
        clause = _spike_clause(items, located=True)
        if clause:
            opener = "It also shows" if (hits or broad_outside) else "It shows"
            out.append(f"{opener} {clause}.")
    out.append(INDICATIVE)
    return " ".join(out)


def counted_spikes(items: list[dict]) -> list[dict]:
    """Every spike the report counts (the difference plot's markers)."""
    seen: dict[float, dict] = {}
    for it in items:
        for s in it.get("spikes") or []:
            seen.setdefault(s["t"], s)
    return [seen[k] for k in sorted(seen)]


def analyze_report(
    t: np.ndarray,
    y_sample: np.ndarray,
    y_std: np.ndarray,
    *,
    ranges: list[dict],
    ladder,
    params: dict,
    standard_name: str,
) -> dict:
    """Both channels + the bullets for one sample/standard pair: ``{diff,
    spike_diff, trend_sample, trend_std, windows, items, text, conclusion,
    spikes, std_profile}``. The one analysis pass behind ``/api/analysis`` and every
    report export (``app._report_content``)."""
    t = np.asarray(t, dtype=float)
    ladder = (list(ladder[0]), list(ladder[1])) if ladder else ([], [])
    pair = analyze_pair(
        t, y_sample, y_std,
        thresh_marginal=params["thresh_marginal"],
        thresh_moderate=params["thresh_moderate"],
        thresh_significant=params["thresh_significant"],
        quantile=params["quantile"], window=params["window"], sigma=params["sigma"],
        cal_times=[], cal_carbons=[],          # the legacy segments are not used
        spike_min_width_min=params["spike_min_width_min"],
    )
    # The spike channel reads the sample aligned onto the standard, and each
    # run's height above its own low-quantile baseline (the dominance test).
    aligned, lag = align_to_standard(t, y_sample, y_std)
    spike_diff = spike_difference(aligned, y_std)
    smooth = (lambda y: gaussian_filter1d(np.asarray(y, dtype=float), SPIKE_SIGMA_PTS)) \
        if gaussian_filter1d is not None else (lambda y: np.asarray(y, dtype=float))
    heights = (smooth(aligned) - pair["trend_sample"], smooth(y_std) - pair["trend_std"])
    profile = standard_profile(t, y_std, ladder, params)
    items = build_deviation_report(t, pair["diff"], spike_diff, ranges=ranges,
                                   ladder=ladder, params=params, spike_heights=heights,
                                   std_profile=profile)
    n_ext = _extent(t, params)
    t_max = float(t[n_ext - 1]) if n_ext else float(t[0])
    windows = range_windows(ranges, ladder, float(t[0]), t_max) if ranges else []
    return {
        "diff": pair["diff"],
        "spike_diff": spike_diff,
        "alignment_lag_min": lag,
        "trend_sample": pair["trend_sample"],
        "trend_std": pair["trend_std"],
        "windows": windows,
        "items": items,
        "text": render_bullets(items, standard_name),
        "conclusion": deviation_conclusion(items, ranges, standard_name),
        "spikes": counted_spikes(items),
        "std_profile": profile,
    }
