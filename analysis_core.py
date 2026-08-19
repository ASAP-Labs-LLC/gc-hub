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
    raw_diff = np.asarray(y_sample, dtype=float) - np.asarray(y_std, dtype=float)
    if gaussian_filter1d is not None and SPIKE_SIGMA_PTS > 0:
        raw_diff = gaussian_filter1d(raw_diff, SPIKE_SIGMA_PTS)
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
    if cal_times:
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


def resolve_report_ranges(body_ranges, conf: dict) -> list[dict]:
    """Resolve the region list for analysis/report generation.

    Order: explicit *body_ranges* (from the request) → saved overlay defaults
    (``analysis_range_overlays`` JSON in settings) → legacy Gas/Oil settings.
    Every returned entry has ``label``, ``c_start``, ``c_end``, ``color``.
    """
    import json as _json

    def _clean(raw: list) -> list[dict]:
        out = []
        for i, r in enumerate(raw):
            try:
                out.append({
                    "label": str(r.get("label", "Range")),
                    "c_start": int(r.get("c_start", 5)),
                    "c_end": int(r.get("c_end", 15)),
                    "color": str(r.get("color") or
                                 _DEFAULT_RANGE_COLORS[i % len(_DEFAULT_RANGE_COLORS)]),
                })
            except Exception:
                continue
        return out

    if body_ranges:
        cleaned = _clean(body_ranges)
        if cleaned:
            return cleaned

    saved = conf.get("analysis_range_overlays", "")
    if saved:
        try:
            raw = _json.loads(saved) if isinstance(saved, str) else saved
            cleaned = _clean(raw)
            if cleaned:
                return cleaned
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


def carbon_to_time(
    carbon_number: float,
    cal_times: list[float],
    cal_carbons: list[int],
) -> float:
    cn = np.array(cal_carbons[: len(cal_times)], dtype=float)
    ct = np.array(cal_times, dtype=float)
    return float(np.interp(carbon_number, cn, ct))


def segment_carbon_range(
    t_start: float,
    t_end: float,
    cal_times: list[float],
    cal_carbons: list[int],
) -> str:
    ct = np.array(cal_times, dtype=float)
    cn = np.array(cal_carbons[: len(cal_times)], dtype=float)
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
    range_defs: list[tuple[str, int, int, float, float]] = []
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
