# Analysis Tab Upgrades — Design

Date: 2026-07-20
Scope: `GC 2026.5 2887 Advanced analysis/webapp`

Four changes: (1) spike-aware deviation detection, (2) dynamic regions in the
exported analysis report, (3) generalized operator-defined sample flag rules,
(4) fuel-type best-fit classification.

## 1. Spike-aware deviation detection

**Problem.** `/api/analysis` computes deviations from `compute_trend_line`
(rolling 20th-percentile, 301-point window). A low quantile is insensitive to
narrow tall peaks by construction, so short high spikes (e.g. gasoline
adulteration) vanish before the difference is computed — regardless of the
smoothing (sigma) setting.

**Fix.** Two detection channels, merged:

- *Trend channel* (unchanged): existing trend-difference segments — catches
  broad envelope deviations.
- *Spike channel* (new): raw difference `y_sample − y_std_interp`, lightly
  smoothed (fixed small sigma ≈ 3 points to suppress single-point noise).
  Contiguous runs where `|raw_diff| ≥ marginal` **and** run duration ≥
  `analysis_spike_min_width_min` (setting, default 0.02 min) become segments,
  classified with the same marginal/moderate/significant thresholds and
  tagged `"source": "spike"`.

Merge: concatenate both segment lists, sort by start time, merge overlaps
keeping wider range / higher severity (existing merge logic, extracted so both
channels share it). Wired into `/api/analysis`, `/api/export-analysis-report`,
and the ZIP route via a shared `_run_analysis_core()` helper (removes the
current copy-paste divergence where the export route smooths trends instead of
the diff).

## 2. Dynamic regions in the exported report

**Problem.** `_generate_analysis_report_pdf` reads only the legacy
`analysis_gas_*` / `analysis_oil_*` settings; frontend export calls don't send
the operator's region list. Custom regions never reach the PDF.

**Fix.**
- Frontend: `exportToPC()` (single + ZIP payloads) and the queue-add call send
  `ranges: [{label, c_start, c_end, color}]` from `state.rangeOverlays`.
  Queue items capture the ranges active when the sample was queued.
- Backend: `_generate_analysis_report_pdf` builds one rectangle + label per
  entry in `params["ranges"]`, using each range's color (fill at low alpha,
  border stronger). Fallback order when absent: `conf["analysis_range_overlays"]`
  (saved defaults, JSON) → legacy Gas/Oil settings.
- Conclusion generation in the export routes uses the same resolved ranges.

## 3. Generalized sample flag rules

Replaces Early High-Signal Detection (per user decision) with an operator-
editable rule list; legacy settings migrate automatically.

**Rule shape** (stored as JSON in settings key `sample_flag_rules`):

```json
{"name": "Early High-Signal", "condition": "above", "threshold": 7500,
 "t_start": 0.0, "t_end": 0.5, "color": "#e67e22", "enabled": true}
```

- `above`: flag if **any** point in `[t_start, t_end]` exceeds `threshold`.
- `below`: flag if **all** points in `[t_start, t_end]` are under `threshold`
  (the "no signal for this long" case).

**Migration.** If `sample_flag_rules` is missing/empty, build it from the
legacy `early_signal_*` keys (preserving enabled/threshold/time) plus a seeded
blue "No Signal" rule (`below`, threshold 500, 0–6.5 min, `#3498db`, enabled).

**Backend.** New module `sample_flags.py` (importable without `app.py`'s
side effects): `load_rules(conf)`, `evaluate_rules(t, y, rules)` →
`[{name, color}]`, `migrate_legacy(conf)`. `app.py` keeps its lazy per-file
cache, now keyed on a fingerprint of the rule list so edits invalidate it.
File entries gain `flags: [{name, color}]`; `early_signal` stays as
`bool(flags)` for compatibility.

**Frontend.** File lists render one colored tag per matched rule. The
"High Signal" filter button becomes a generic "Flagged" filter (any rule).
Settings modal: rule editor rows (name, condition, threshold, window, color,
enabled, add/remove). Pure row↔rule logic lives in `static/js/flagrules.js`
(window global + module.exports, like `selection.js`) and is node-tested.

## 4. Fuel-type best fit

New module `fuel_fit.py`, pure numpy/scipy, no Flask imports.

**Method** (chromatographic rationale): injection volume/loading varies run to
run, so amplitude carries little identity — traces are resampled to a common
uniform grid over `[0, x_max]`, clipped at 0, and normalized to unit area
(full "up/down" leniency). Retention times on a fixed method are stable, so
only a small global shift search (± `bestfit_shift_tolerance_min`, default
0.05 min) is allowed ("side to side" strictness). Score = max cosine
similarity over the shift search, after light smoothing.

**Decision line** (`bestfit_threshold`, default 0.93, operator-tunable):
1. Best single standard score ≥ threshold → that fuel type.
2. Else NNLS blend (`scipy.optimize.nnls`) over all standards; if the blend
   score ≥ threshold and ≥2 components carry ≥ `bestfit_mix_min_frac`
   (default 0.10) of the weight → `Mix: A + B (80/20)`.
3. Else → `Mix`.

API: `classify(t, y, standards, config) → {label, best_standard, score,
ranking: [{name, score}], mix}`.

**Wiring.**
- *Parsing*: `distill.process_cdf` computes best fit when
  `bestfit_enabled` (default true) and `comparison_defaults_dir` has
  standards; failure-safe (blank values, never blocks distillation). CSV
  gains two columns: `Best Fit`, `Fit Score`. `_migrate_csv_header`'s
  early-return check moves from `"Source File"` to `"Best Fit"` so existing
  CSVs upgrade in place.
- *Library badge*: file enrichment adds `best_fit` (lazy, disk-cached like
  the early-signal cache; standards loaded once with mtime invalidation).
  Frontend shows a small badge with label.
- *Analysis tab*: on sample select, `/api/best-fit` returns the ranking; the
  UI auto-selects the winning standard and shows the ranked scores.

**Settings** (all in Settings modal, Analysis section): `bestfit_enabled`,
`bestfit_threshold`, `bestfit_shift_tolerance_min`, `bestfit_mix_min_frac`.

## Testing (TDD)

- Python (pytest, skip-if-no-numpy pattern as existing tests):
  - `tests/test_analysis_spikes.py` — synthetic narrow spike missed by trend
    channel, caught by spike channel; min-width rejection; severity; merge.
  - `tests/test_report_ranges.py` — range-def resolution (params → saved
    overlays → legacy gas/oil), colors honored (pure helper).
  - `tests/test_sample_flags.py` — above/below semantics, window masking,
    migration from legacy keys, fingerprint invalidation.
  - `tests/test_fuel_fit.py` — scale invariance, small-shift tolerance,
    blend → named mix, garbage → Mix, ranking order.
  - `tests/test_distill.py` additions — CSV header + best-fit columns,
    header migration.
  - `tests/test_app_routes.py` additions — new/changed route surface (AST).
- JS (`node tests/js/run.js`): `flagrules.test.js`, `report_payload.test.js`
  (pure payload builder incl. ranges).

## Out of scope

Desktop PyQt build; QBench flow changes; re-scoring historical CSV rows.
