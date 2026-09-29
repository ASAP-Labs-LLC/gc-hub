# Phase 3 (lane P3): range-driven deviation bullets — implementation plan

Spec: `docs/superpowers/specs/2026-09-29-phase3-4-bullets-comments-design.md` (rev 2).
Branch: `p3/bullets` from `feat/phase3-4`. Strict TDD: every task starts with a
failing test (red), then the smallest change that makes it pass (green), then a
commit. Python `.venv/bin/python -m pytest`, frontend `node tests/js/run.js`.

Out of scope (P4): `store.py` migration v2, `comments.py`/`comments_api.py`, the
comments UI, the annotation code in `app.js` (`setupAnnotationHandler`, modal
save, `redrawAnnotations`, `clearAllAnnotations`), the presets admin,
`tests/test_route_fates.py`.

## Seam with P4 (temporary stub)

`app._comments_for_report(sample_id, db)` and `app._log_report(sample_id, db, **row)`
import `comments` guarded (`ModuleNotFoundError` naming `comments` only) and fall
back to `[]` / no-op. The frozen shape consumed: `comments.for_report(sample_id,
db) -> [{id, text, initials, created_at, t0, t1}]` (non-deleted, time order).
`comments.log_report(sample_id, db, **row)` is called with the `report_log`
column names as keywords (`revision, kind, standard_name, params_json,
ranges_json, windows_json, bullets_json, bullets_text, conclusion,
conclusion_edited, comment_ids_json, app_version, pdf_sha256, author_initials,
author_ip`). The integrator removes the fallback after P4 merges.

## Tasks

### T1 — settings keys
Red: `tests/test_settings.py` expects `analysis_min_width_min` (0.05),
`analysis_merge_gap_min` (0.10), `analysis_spike_report_threshold` ("") in
`DEFAULTS` and all three in `ADMIN_KEYS`; an AST test expects the
`api_save_analysis_defaults` key list to include them plus
`spike_min_width_min`. Green: `settings.py`, `app.py`.

### T2 — `range_windows` (one carbon↔time conversion)
Red (`tests/test_analysis_bullets.py`): linear interpolation inside the ladder,
linear extrapolation from the end pairs outside it, clipping to
`[t_min, t_max]` with `clipped` set, swapped `c_start > c_end`, width 0 →
`evaluable=false`, a window entirely past the axis → not evaluable, duplicate
labels keep their own index, `time_to_carbon` is the inverse. Green:
`analysis_core.range_windows`, `ladder_carbon_to_time`, `ladder_time_to_carbon`.

### T3 — `report_params` and spikes
Red: `report_params(body, conf)` takes the operator parameters from the body
(fallback settings) and the admin ones from settings only; an empty
`analysis_spike_report_threshold` means the moderate threshold in effect.
`find_spikes` returns signed qualifying spikes (spike min width, report
threshold, evaluation extent). Green: `analysis_core`.

### T4 — `build_deviation_report` rules
Red, one test per rule on synthetic difference arrays (0.001 min sampling):
each severity (marginal/moderate/significant); direction by area (a tall
narrow negative run loses to a broad positive one) with the max sign matching;
the "also" clause only for opposite runs ≥ moderate, with its own severity and
≤ 2 merged spans; `min_width` rejects a narrow trend run and applies after
clipping at a window edge; `frac_above`; spike-only range; spikes inside and
outside ranges, signed; the outside bullet (complement clipping, `merge_gap`,
≤ 3 spans + "+N more"); zero ranges ("Across the run"); explicit `[]`;
overlapping ranges both report; duplicate labels; not-evaluable windows; the
no-calibration bullet; no-deviation lines; the bound `≤ ranges + 1` over a
randomized sweep; evaluation stops at `x_max_min`. Green: `analysis_core`.

### T5 — `render_bullets` golden strings and the conclusion
Red: exact text for every template (range, range + also, range clipped,
spike-only, spikes >3 "incl.", below spikes, outside, outside with "+N more",
across the run, not evaluated, none / none within ranges, no-calibration with
and without deviation) and `deviation_conclusion` (none, one, several ranges,
elevated iff a positive qualifying run or spike, no-calibration text, zero
ranges). Green: `analysis_core.render_bullets`, `deviation_conclusion`,
`analyze_report` (one call used by the app).

### T6 — fixtures
Red: `tests/test_analysis_spikes.py` gasoline-spike fixture must produce a Gas
bullet (spike-only) and the single-range gas conclusion;
`tests/test_bullets_regression.py` loads
`tests/fixtures/diesel_pair.npz` (t, sample, standard, ladder) and asserts the
old `generate_conclusion` gave dozens of bullets while the new report gives
≤ ranges + 1. Green: fixture generator `tests/fixtures/make_diesel_pair.py`
(committed) and the `.npz`.

### T7 — `resolve_report_ranges`: explicit `[]` means no ranges
Red: `resolve_report_ranges([], conf) == []`; `None` still falls back. Green.

### T8 — `_report_content` and the export paths
Red: an in-process harness (`tests/report_harness.py`, run in a subprocess with
`GC_DATA_DIR` = a `hub_boot` hub, a fake `comments` module and a fake QBench
uploader) records every `_report_content` output while calling
`/api/analysis`, the direct export, the ZIP export and the QBench upload with
the same parameters: the spec keys must be identical across the four, each
PDF's text must contain every rendered bullet line, the footer must carry the
parameters/ranges/version, a client `bullets` string must be ignored,
`report_log` rows must be written (download, zip, qbench). AST pins: no
`params.get("bullets"` / `item["bullets"]` in `app.py`; `/api/analysis` calls
`resolve_report_ranges` (via `_report_content`); the QBench loop copies
`quantile, window, sigma, thresh_*, x_max_min`. Green: `app.py`.

### T9 — report HTML escaping and footer
Red: harness renders `_report_html` with a comment `<img src=file:///etc/passwd>`,
a lab ID/doc name/standard/range label containing markup: no raw `<img`
appears, escaped text does. Footer lists params, ranges, app version. Green.

### T10 — frontend payloads
Red (`tests/js/report_payload.test.js`): `captureReportParams` copies quantile,
window, sigma, thresholds, x_max_min; `buildReportItemPayload` carries
`item.params` (fallback: current params), never `bullets`, sends `ranges: []`
for an item queued with no ranges. Green: `report_payload.js`.

### T11 — UI
`app.js`/`index.html`: range boxes drawn from `result.windows` (not
`/api/calibration`), ±threshold dotted lines and counted-spike markers on the
difference plot, the deviation report read-only from `result.text`, the export
modal's bullets a read-only preview, queue items capture `params` and no
`bullets`, direct export sends no `bullets`; Settings gets the four deviation
settings (admin). Verified by a headless Chrome smoke
(`tests/test_ui_analysis_smoke.py`, skipped without Chrome/selenium).

### T12 — docs, full suite, push, CI
CLAUDE.md (analysis_core, report_payload.js), the full pytest suite + node
tests, push `p3/bullets`, CI green.
