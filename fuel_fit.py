"""Fuel-type best-fit classification against the comparison standards.

Chromatographic rationale
-------------------------
Injection volume and detector response vary run to run, so overall amplitude
carries almost no identity: traces are baseline-subtracted and normalized to
unit area before comparison (full "up/down" leniency).  Retention times on a
fixed method/column are stable, so only a small global time shift is searched
(limited "side to side" leniency).  The score is the cosine similarity of the
normalized envelopes, taken at the best shift.

Decision line (operator-tunable via settings):

1. Always solve a non-negative least-squares blend of all standards
   (``scipy.optimize.nnls``).  If a second component carries at least
   ``mix_min_frac`` of the blend, the blend explains the sample better than
   the best single standard, and the blend score clears ``threshold`` →
   ``"Mix: A + B (80/20)"``.
2. Else if the best single standard clears ``threshold`` → that fuel type.
3. Else → ``"Mix"`` (fits none of the standards cleanly).

Pure numpy/scipy — no Flask, no file watching.  CDF reading is injected so
this module stays import-cycle-free (see ``load_standards``).
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.optimize import nnls

LOGGER = logging.getLogger(__name__)

#: Number of points on the internal comparison grid.
GRID_N = 4096

#: Baseline percentile subtracted before normalization (removes detector
#: offset without touching real peaks).
BASELINE_PCT = 5.0

#: Light smoothing (grid points) applied before comparing envelopes.
SMOOTH_SIGMA_PTS = 4.0

#: A named mix must beat the best single-standard score by at least this much
#: (otherwise the extra component is fitting noise, not a real adulterant).
MIX_IMPROVEMENT = 0.005


def normalize_trace(t, y, x_max_min: float):
    """Baseline-subtract, clip, and area-normalize *y* on its own time axis.

    Returns an array the same length as *t*; only the portion inside
    ``[0, x_max_min]`` contributes to the area.  Zero-signal traces come back
    all zero.
    """
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    y = y - np.percentile(y, BASELINE_PCT)
    y = np.clip(y, 0.0, None)
    mask = (t >= 0) & (t <= x_max_min)
    area = float(np.trapezoid(y[mask], t[mask])) if mask.any() else 0.0
    if area <= 0:
        return np.zeros_like(y)
    return y / area


def _grid_vector(t, y, x_max_min: float) -> np.ndarray:
    """Sample a normalized trace onto the internal uniform grid."""
    grid = np.linspace(0.0, x_max_min, GRID_N)
    yn = normalize_trace(t, y, x_max_min)
    v = np.interp(grid, np.asarray(t, dtype=float), yn, left=0.0, right=0.0)
    if SMOOTH_SIGMA_PTS > 0:
        v = gaussian_filter1d(v, SMOOTH_SIGMA_PTS)
    return v


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na <= 0 or nb <= 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _best_shift_score(sample_v: np.ndarray, std_v: np.ndarray,
                      x_max_min: float, shift_tolerance_min: float) -> float:
    """Max cosine similarity over small global shifts of the sample."""
    if shift_tolerance_min <= 0:
        return _cosine(sample_v, std_v)
    step_min = x_max_min / (GRID_N - 1)
    max_steps = max(1, int(round(shift_tolerance_min / step_min)))
    # ~11 probe shifts across the window, always including 0
    probes = sorted(set(
        int(round(s)) for s in np.linspace(-max_steps, max_steps, 11)
    ) | {0})
    best = 0.0
    idx = np.arange(GRID_N, dtype=float)
    for k in probes:
        shifted = np.interp(idx - k, idx, sample_v, left=0.0, right=0.0)
        best = max(best, _cosine(shifted, std_v))
    return best


def classify(
    t,
    y,
    standards: list[dict],
    *,
    threshold: float = 0.93,
    shift_tolerance_min: float = 0.05,
    mix_min_frac: float = 0.10,
    x_max_min: float = 7.0,
) -> dict:
    """Classify a chromatogram against *standards*.

    *standards* is ``[{"name", "t", "y"}, ...]``.  Returns::

        {"label": "Diesel" | "Mix: Diesel + Gasoline (80/20)" | "Mix" | "",
         "best_standard": str, "score": float,
         "ranking": [{"name", "score"}, ...],   # sorted best-first
         "mix": {"components": [(name, frac), ...], "score": float} | None}
    """
    empty = {"label": "", "best_standard": "", "score": 0.0,
             "ranking": [], "mix": None}
    if not standards:
        return empty

    sample_v = _grid_vector(t, y, x_max_min)
    if float(np.max(sample_v)) <= 0:
        return empty

    std_vectors: list[np.ndarray] = []
    ranking: list[dict] = []
    for std in standards:
        v = _grid_vector(std["t"], std["y"], x_max_min)
        std_vectors.append(v)
        score = _best_shift_score(sample_v, v, x_max_min, shift_tolerance_min)
        ranking.append({"name": std["name"], "score": round(score, 4)})

    order = sorted(range(len(ranking)), key=lambda i: ranking[i]["score"],
                   reverse=True)
    ranking = [ranking[i] for i in order]
    best_name = ranking[0]["name"]
    best_score = ranking[0]["score"]

    # ── Blend attempt (always solved; decides single vs named mix) ────
    mix = None
    if len(standards) >= 2:
        try:
            S = np.column_stack(std_vectors)
            w, _ = nnls(S, sample_v)
            total = float(w.sum())
            if total > 0:
                fracs = w / total
                blend_v = S @ w
                blend_score = _cosine(blend_v, sample_v)
                comps = sorted(
                    ((standards[i]["name"], float(fracs[i]))
                     for i in range(len(standards)) if fracs[i] >= mix_min_frac),
                    key=lambda c: c[1], reverse=True,
                )
                if (len(comps) >= 2
                        and blend_score >= threshold
                        and blend_score >= best_score + MIX_IMPROVEMENT):
                    mix = {"components": comps, "score": round(blend_score, 4)}
        except Exception as exc:
            LOGGER.debug("NNLS blend failed: %s", exc)

    if mix:
        pct = "/".join(str(int(round(frac * 100))) for _, frac in mix["components"])
        names = " + ".join(name for name, _ in mix["components"])
        label = f"Mix: {names} ({pct})"
        return {"label": label, "best_standard": best_name,
                "score": mix["score"], "ranking": ranking, "mix": mix}

    if best_score >= threshold:
        return {"label": best_name, "best_standard": best_name,
                "score": best_score, "ranking": ranking, "mix": None}

    return {"label": "Mix", "best_standard": best_name,
            "score": best_score, "ranking": ranking, "mix": None}


# ── Standards loading (reader injected to avoid import cycles) ────────
_standards_cache: dict = {"key": None, "standards": []}


def load_standards(comp_dir: Path, reader) -> list[dict]:
    """Load all ``*.cdf`` chromatograms in *comp_dir* via *reader* (a callable
    ``path -> (t, y)``), with an mtime-based cache so repeated calls are free."""
    comp_dir = Path(comp_dir)
    if not comp_dir.is_dir():
        return []
    paths = sorted(
        p for p in comp_dir.iterdir()
        if p.suffix.lower() == ".cdf" and p.is_file()
    )
    key = tuple((str(p), p.stat().st_mtime) for p in paths)
    if key == _standards_cache["key"]:
        return _standards_cache["standards"]
    standards: list[dict] = []
    for p in paths:
        try:
            t, y = reader(p)
            standards.append({"name": p.stem, "t": t, "y": y})
        except Exception as exc:
            LOGGER.warning("Could not load standard %s: %s", p.name, exc)
    _standards_cache["key"] = key
    _standards_cache["standards"] = standards
    return standards
