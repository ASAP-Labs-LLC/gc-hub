# Phases 3 and 4: Range-driven deviation bullets and comment presets (design)

Date: 2026-09-29 — **rev 2 — approved with changes applied by the delegated critic**
Base: gc-hub v2.0.0 (`main`)
Decisions from Ryan (2026-09-29):
- **Bullets:** one per range overlay, only for ranges that deviate, plus a single catch-all for anything outside the ranges. The chosen preview was "Ranges, deviations only".
- **Comments:** one-click chips on the Analysis tab that feed the report's comment section (PDF and QBench upload). Each comment is saved on the sample with who and when, and the preset list is managed on an admin page.

## Problem

`analysis_core.generate_conclusion` writes **one bullet per excursion**: every
contiguous run above the marginal threshold, from both the trend channel and
the spike channel. It never merges them and never caps the count. On diesel
chromatograms this gives dozens of bullets, which operators delete by hand. The
operator's range overlays (`analysis_range_overlays`) only feed the one-line
conclusion, not the bullets.

Other bugs found in phase 1 and in the rev-2 review that this phase fixes:
- A direct export sends the whole report textarea as "bullets", header lines
  included.
- The queue/ZIP export sends no trend parameters, so its PDF charts use the
  saved defaults while its bullets used the on-screen parameters.
- The QBench upload builds `report_params` without any trend parameter or
  threshold (`app.py` QBench loop), so QBench PDFs always use the saved
  defaults.
- A client-sent `bullets` string overrides the computed one in three places:
  `_generate_analysis_report_pdf` (`params.get("bullets", …)`),
  `_run_export_analysis` (`params.get("bullets") or …`) and the QBench loop
  (copies `item["bullets"]`).
- "Clear Annotations" deletes every line starting with "•" that contains
  " min", which also removes auto-generated bullets.
- The Analysis tab draws range boxes from `/api/calibration` (always gc1's
  current calibration, with a fabricated `i + 5` carbon fallback), while the
  server evaluates with the sample revision's anchors (`_revision_ladder`).
  Carbon→time conversion also differs: `analysis_core.carbon_to_time` and the
  JS `linterp` clamp at the ladder ends, the PDF's `_c_to_time` extrapolates.
- `/api/analysis` does not use `resolve_report_ranges` (hardcoded Gas/Oil
  default), and an explicit empty range list means "no ranges" on screen but
  "saved or legacy Gas/Oil" in every export.
- The PDF's x-axis uses the saved `analysis_x_max_min`, not the request's.
- The report HTML is built with unescaped f-strings (bullets, conclusion,
  lab ID, standard name, document name).

## Phase 3: Bullets

### Rules

**Inputs.** Bullets are computed on the server from the same inputs as the
difference graph:
- the trend difference, using the trend parameters in effect (quantile,
  window, sigma);
- the spike channel (never removed: the trend's low quantile erases short
  tall spikes such as gasoline adulteration);
- the thresholds (marginal, moderate, significant);
- the range overlays from `analysis_core.resolve_report_ranges`, on every
  path including `/api/analysis`. An **explicit empty list means no ranges**
  on every path (`resolve_report_ranges` distinguishes `[]` from a missing
  key; `buildReportItemPayload` sends `ranges: []` rather than omitting it);
- the sample revision's calibration ladder (`_revision_ladder`, i.e. the
  revision's anchors, else `distill.calibration_ladder(ctx)`);
- the displayed axis limit `x_max_min`.

**Range windows (one conversion).**
`analysis_core.range_windows(ranges, ladder, t_min, t_max) -> [{label, c_start,
c_end, t0, t1, clipped, evaluable}]` is the one carbon→time conversion (linear
extrapolation from the end pairs, as the PDF does today). Windows are clipped
to the run and the displayed axis (`t_max = min(run end, x_max_min)`).
`c_start > c_end` is swapped. A window of width 0 is `evaluable=false` and
renders `• <label> (Cx–Cy): not evaluated — outside the calibrated/run range`.
A clipped window adds "(evaluated to Cn)" to its bullet. `/api/analysis`
returns these windows, and the UI and PDF draw the range boxes from them,
never from `/api/calibration`. Ranges are keyed by index, not label
(duplicate labels are allowed and each gets its own bullet).

**Evaluation extent.** Everything (range windows, the outside rule, spans,
spikes) is evaluated on `[t_start, x_max_min]` only — the graph actually shown.

**Qualifying runs.**
- A **trend run** is a contiguous run of the trend difference with
  |diff| ≥ marginal, **clipped to the window** (or, for the outside rule, to
  the complement of all windows), that is at least `min_width_min` wide after
  clipping.
- A **qualifying spike** is a spike-channel segment (its own
  `analysis_spike_min_width_min`) that reaches ≥
  `analysis_spike_report_threshold` (new admin setting, default = the moderate
  threshold).
- Spans are reported as the **above-threshold extents**, not the
  zero-crossing expansion used by today's segments.

**Per range, including only ranges that deviate:**
- **Deviation test.** A range deviates when either (a) a trend-difference run
  clipped to the window, at least `min_width_min` wide, reaches
  |diff| ≥ marginal, or (b) a spike-channel segment (its own
  `analysis_spike_min_width_min`) inside the window reaches ≥
  `analysis_spike_report_threshold` (new admin setting, default = moderate).
  A range that deviates only by (b) gets a bullet reading
  `— <severity>, sharp peaks only (N sharp spikes above <std> at …; trend within marginal)`.
  Severity is the maximum over the trend and the qualifying spikes.
- **Direction.** HIGHER or LOWER, from the sign with the larger **area**:
  the integral of |diff| over qualifying trend runs of that sign within the
  window (for a spike-only range, over the qualifying spikes).
- **Severity and max.** Taken from qualifying runs (and qualifying spikes) of
  the dominant direction: marginal, moderate or significant — the threshold
  names shown in the UI ("marginal≥"). The reported `max ±X at T min` is that
  same peak, so the sign always matches the direction.
- **"Also" clause.** When qualifying opposite-sign trend runs reach
  ≥ moderate, the bullet adds `; also lower <severity> 3.2–3.4 min` (or
  "also higher"), with its own severity and the merged spans of those runs,
  at most 2 spans.
- **Details (in parentheses):**
  - `max ±X at T min` (signed peak, as above);
  - `P% of range above marginal`: the time covered by qualifying trend runs
    over the clipped window's width;
  - qualifying spikes, signed, as a count plus up to 3 times ("2 sharp spikes
    above Diesel #2 at 0.84, 1.40 min"; "5 sharp spikes above Diesel #2 incl.
    0.84, 1.40, 1.93 min"; "1 sharp spike below …").
- **Text:** `• Gas (C5–C11): HIGHER than Diesel #2 — moderate (max +620 at 1.12 min; 38% of range above marginal; 2 sharp spikes above Diesel #2 at 0.84, 1.40 min)`
- **Overlapping ranges** each report the shared excursion; that is intended.

**Outside ranges.** At most one bullet, covering **the parts of runs outside
every window** (runs clipped to the complement of the windows, then
`min_width_min` applied to each clipped piece) and qualifying spikes outside
every window.
- Pieces separated by less than `merge_gap_min` (a new setting, default 0.10
  min) are merged first.
- It reports the worst severity, the dominant direction by area, and up to 3
  merged spans ("4.1–4.6, 5.2–5.3 min"), plus "+N more" when there are more.
- Label: `• Outside the defined ranges:`; with **zero ranges** it is the only
  possible bullet and reads `• Across the run:`.

**No deviations.**
- No range deviates and nothing outside: one line, "No deviations above the
  marginal threshold."
- No range deviates but the outside bullet exists: "No deviations above the
  marginal threshold within the defined ranges." followed by the outside
  bullet.
- Zero ranges and nothing deviates: "No deviations above the marginal
  threshold."

**No usable calibration ladder** (fewer than 2 points). Ranges can't be
placed, so the report gets a single bullet built from the outside rule over
the whole evaluated extent, prefixed "Calibration unavailable — ranges not
evaluated:". The conclusion reads: "Conclusion: Compared to <std>, the
defined ranges could not be evaluated because no usable calibration was
available for this sample."

**Bound.** There are never more bullet lines than **(number of ranges + 1)**;
with zero ranges, 1. (The "none within ranges" line plus the outside bullet
is 2 lines, which fits the bound whenever there is at least one range.)

**Conclusion.** It keeps today's overlap-based text, but reads the deviating
ranges from the new per-range results. A range counts as elevated for the
conclusion iff it has a qualifying positive trend run or a qualifying
positive spike, whatever the bullet's dominant direction. The conclusion
**stays editable** in the Analysis tab and the export modal; a non-empty
client conclusion is used as today (`params.get("conclusion") or
generated`). An edited conclusion can go stale against bullets recomputed
with other parameters; `report_log.conclusion_edited` records that it was
edited.

**Sampling-rate note.** `window` and `sigma` are counted in data points,
while `min_width_min`, `merge_gap_min` and the spike min width are in
minutes. An instrument with a different sampling rate gets different trend
smoothing in minutes for the same settings; the defaults assume the current
GC's rate (fixtures use 0.001 min).

### Where bullets come from (one source)

- `analysis_core.build_deviation_report(...)` returns structured items:
  `[{kind: 'range'|'outside'|'none'|'no-calibration'|'not-evaluated', index,
  label, c_start, c_end, t0, t1, clipped, direction, severity, max_diff,
  max_at, frac_above, spikes: [{t, sign}], spike_only, also: {direction,
  severity, spans}|None, spans: [...]}]`.
- `render_bullets(items, standard_name)` turns those items into text.
- `app._report_content(sample, params, conf, db)` returns `{items, text,
  conclusion_generated, windows, params_used, comments}` and is the **only
  input to the PDF** and the only analysis pass for every export.
  `/api/analysis` uses the same analysis functions and returns `items`,
  `text` and `windows`.
- **`bullets` is removed from every payload and ignored server-side at all
  three sites** (`_generate_analysis_report_pdf`, `_run_export_analysis`, the
  QBench loop). Every export computes bullets on the server:
  - direct export;
  - queue/ZIP;
  - QBench PDF.
- The export payloads carry the trend parameters, thresholds and
  `x_max_min`:
  - direct export: from the current state (as today);
  - queue items capture `params` at queue time, like `ranges` (fallback:
    current state), via `report_payload.js`; the queue/ZIP and QBench
    payloads send them;
  - the QBench path copies quantile, window, sigma, the three thresholds and
    `x_max_min` from the queue item into `report_params`;
  - the PDF's x-axis uses `params_used.x_max_min`.
- The report text area in the UI shows the generated bullets **read-only**.
  The export modal's `export-bullets` field becomes a read-only preview, and
  `item.bullets` is dropped from queue items. Operator free text moves to
  comments (phase 4). An operator who disagrees with a bullet changes the
  parameters or ranges and/or adds a comment.
- The annotation feature stops writing into the bullet text; its
  persistence as comments is phase 4 (below). `clearAllAnnotations`' line
  filter is deleted outright: the bullet text can never be deleted by text
  matching.

### Difference plot

The UI and PDF difference plots draw ±marginal/moderate/significant as
dotted lines and mark counted spikes (the qualifying spikes in the report),
so every bullet can be checked against the graph.

### Report HTML and footer

- Every interpolated string in the report HTML is passed through
  `html.escape` (bullets, conclusion, lab ID, standard name, document name,
  range labels, comments). Tested with a comment containing
  `<img src=file:…>`.
- The PDF footer prints the parameters used (quantile, window, sigma, the
  three thresholds, `x_max_min`, `min_width_min`, `merge_gap_min`, spike
  min width and report threshold), the ranges, and the app version.

### Settings

- New: `analysis_min_width_min` (0.05), `analysis_merge_gap_min` (0.10) and
  `analysis_spike_report_threshold` (default empty = the moderate threshold).
- All three are added to `settings.DEFAULTS`, `settings.ADMIN_KEYS` and the
  key list in `api_save_analysis_defaults` (which also gains the existing
  `analysis_spike_min_width_min`, missing today). Editable behind the admin
  password, as today's "Set as Default".

## Phase 4: Comments

### Data

Additive store migration (schema v2, the first post-release migration; it must
be additive):

```
comment_presets(id INTEGER PRIMARY KEY, text TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1, created_by TEXT, created_at TEXT, updated_at TEXT)
sample_comments(id INTEGER PRIMARY KEY, sample_id INTEGER NOT NULL REFERENCES samples(id),
                revision INTEGER,                                  -- current_revision when added
                text TEXT NOT NULL, preset_id INTEGER, source TEXT NOT NULL,   -- 'preset'|'free'|'annotation'
                t0 REAL, t1 REAL,                                  -- annotation span (min), else NULL
                author_initials TEXT NOT NULL, author_ip TEXT, created_at TEXT NOT NULL,
                deleted_at TEXT, deleted_by_initials TEXT, deleted_by_ip TEXT)
CREATE INDEX sample_comments_sample ON sample_comments(sample_id, created_at)
report_log(id INTEGER PRIMARY KEY, sample_id INTEGER NOT NULL REFERENCES samples(id), revision INTEGER,
           kind TEXT NOT NULL,                                     -- 'download'|'zip'|'qbench'
           standard_name TEXT, params_json TEXT, ranges_json TEXT, windows_json TEXT,
           bullets_json TEXT, bullets_text TEXT, conclusion TEXT, conclusion_edited INTEGER,
           comment_ids_json TEXT, app_version TEXT, pdf_sha256 TEXT, created_at TEXT NOT NULL,
           author_initials TEXT, author_ip TEXT)
CREATE INDEX report_log_sample ON report_log(sample_id, created_at)
```

- The preset seeds are INSERTs inside the v2 migration step (same
  transaction), so they are seeded exactly once and never again at startup.
- `REQUIRED_COLUMNS` is extended with the three tables.
- Deleting a comment is a soft delete (`deleted_*`), because comments feed
  reports.
- A comment's text is **copied** from the preset when it is added, so later
  preset edits don't rewrite history.
- `report_log` is written for every QBench upload (required) and for
  downloads/ZIP (each PDF). Bullets stay computed on demand; the log is the
  record of what was reported.
- **Rollback.** v2.0.0 starts on a v2 database (`migrate` tolerates a newer
  `user_version`; nothing deletes samples, so the foreign keys are harmless).
  Reports produced by v2.0.0 omit comments, and v2.0.0 writes no
  `report_log` rows; the release notes say so.

### Author

> **Superseded in v3.1.0** by sign-in (`2026-09-29-public-url-login-design.md`,
> D6): the author is the signed-in account name, the initials are derived
> from it, and the initials box is gone. The text below is the v3.0.0 design.

There is no user login. The Analysis tab has a "Your initials" box, remembered
per browser (localStorage with try/catch) and required before adding or
deleting a comment. The server validates initials as `^[A-Z]{1,4}$` (after
upper-casing) and stores `author_initials` and `author_ip` (the request's
address) in separate columns; likewise `deleted_by_*`. Reports print
**initials only**, never the IP. Identity is self-declared and not
authenticated; the release notes say so. The cross-site write guard applies
to every comment route.

### Limits

- Comment text ≤ 500 chars; at most 100 active comments per sample.
- Preset text ≤ 200 chars; at most 50 presets.
- JSON bodies capped at 64 KiB.

### UI

**Analysis tab, Comments section, under the bullets:**
- **Preset chips** (active presets in sort order). One click adds that
  comment.
- **A free-text box** with an Add button.
- **The list of the sample's comments**, each showing its text, initials and
  time, with a delete button. Deleting asks for confirmation.
- **Annotation comments** show their time span.
- All strings are rendered with `textContent`. Plotly annotation text
  (comment labels on the trend plot) has `<` and `>` escaped before
  `redrawAnnotations`.

**Annotations become comments (owned by P4):**
- Saving the annotation modal POSTs one comment (`source='annotation'`,
  `t0`, `t1`). Text is required, or defaults to "Marked region Cx–Cy" (the
  span's carbon range from the revision ladder).
- The global `annotationData` array goes away: annotation shapes are redrawn
  from GET comments on every sample change and after each analysis.
- `item.annotations` is removed from queue items, so restoring a queue item
  cannot re-post duplicates.
- "Clear Annotations" soft-deletes the sample's persisted annotation
  comments after a confirmation naming the count, recording `deleted_by_*`.
  The shape filter by fillcolor stays.

**Presets admin** (a section on `/admin/hub`, admin-gated):
- add, edit, reorder and deactivate presets;
- seeded with Ryan's examples, worded for a report (QBench PDFs may reach
  customers):
  - "Sample appears to be a renewable fuel, not conventional petroleum diesel."
  - "Sample appears to be gasoline."
  - "Possible contamination; results should be interpreted with caution."
  - "Re-run requested."

### Reports

- The analysis PDF, the ZIP export and the QBench PDF each get a **Comments**
  section after the bullets. It lists the sample's non-deleted comments in
  time order, as `text (initials, date)`; annotation comments as
  `text (Cx–Cy, a–b min; initials, date)`. All escaped.
- The section is omitted when there are no comments.
- The comment ids included are recorded in `report_log.comment_ids_json`.

### Routes

- `GET /api/samples/<id>/comments`
- `POST /api/samples/<id>/comments` with `{text | preset_id, initials, t0?, t1?}`.
  Operator action, same-origin, `Content-Type: application/json` required,
  64 KiB cap, limits above.
- `POST /api/samples/<id>/comments/<cid>/delete` with `{initials}`
- `GET /api/comment-presets`
- Admin (password, `_admin_json_body`): `/api/admin/comment-presets`
  (create, update, reorder, deactivate).
- All new routes are added to `tests/test_route_fates.py`.

## Delivery

Two lanes in parallel, each with its own critic, then an integration critic.
**Merge order: P4's store and API first**, then P3, then P4's UI.

| Lane | Scope | Files |
|---|---|---|
| P3 | bullets engine, `range_windows`, `_report_content`, export payload fix (all three `bullets` sites, QBench params), read-only bullets UI and export-modal preview, threshold lines/spike markers, report HTML escaping and footer, `report_log` writes | `analysis_core.py`, `app.py` report routes and `_generate_analysis_report_pdf`, `static/js/report_payload.js`, the bullets/diff-plot/export-modal areas of `app.js`, `settings.py` |
| P4 | migration v2 (all three tables, seeds), comments data and routes, Analysis-tab comments UI, annotation→comment persistence, presets admin | `store.py`, new `comments_api.py`, new `static/js/comments.js`, the annotation area of `app.js`, `templates/hub_admin.html` section |

**Seam (frozen now):** P3 owns `_generate_analysis_report_pdf`, which takes a
`comments: list[{text, initials, created_at, t0, t1}]` argument and renders
and escapes it. P4's `comments.for_report(sample_id, db)` returns exactly that
shape (non-deleted, time order, plus `id` for `report_log`). Until P4 merges,
P3 uses a stub that returns [].

**Shared files:**

| File | P3 owns | P4 owns |
|---|---|---|
| `app.py` | report routes, `_run_export_analysis`/`_report_content`, the PDF builder, `/api/analysis`, QBench loop params | the `comments_api` Blueprint registration (one line) |
| `static/js/app.js` | bullets display, diff plot, export modal, queue payload | annotation code (`setupAnnotationHandler`, modal save, `redrawAnnotations`, `clearAllAnnotations`), init wiring for `comments.js` |
| `templates/index.html` | report text area, export-modal bullets field | Comments section, initials box, `comments.js` script tag |
| `settings.py` | new `analysis_*` keys | — |
| `tests/test_route_fates.py` | — | new routes |
| `store.py` | `report_log` helper calls only (via P4's functions) | migration v2, `REQUIRED_COLUMNS`, helpers for all three tables |

## Testing

- **Bullets engine:** unit tests on synthetic difference arrays, covering:
  - each severity;
  - direction by area, including "also" runs with their own severity;
  - `min_width`, clipping at window edges, and `merge_gap`;
  - spikes inside and outside ranges, signed;
  - **spike-only range:** the gasoline-spike fixture in
    `tests/test_analysis_spikes.py` must produce a Gas bullet and the
    single-range gas conclusion;
  - `range_windows`: extrapolation, clipping, swapped bounds, not-evaluable;
  - zero ranges, explicit `[]`, duplicate labels, overlapping ranges;
  - no-calibration (bullet and conclusion) and no-deviation cases;
  - the bullet count bound;
  - exact text for every template (golden strings).
- **Consistency:** a boot test calls the Analysis tab route, the direct
  export, the queue/ZIP and the QBench PDF path on the same sample and
  parameters; `_report_content` output must be identical across all four,
  and the PDF text (extracted) must contain the rendered bullets. No
  PDF-byte comparison.
- **Payloads:** node tests for `report_payload.js` (params captured, no
  `bullets`, `ranges: []` sent).
- **Comments:**
  - store migration v1→v2 is additive, seeds exactly once, and v1 code still
    starts on a v2 database;
  - routes (limits, initials validation, soft delete, preset text copied);
  - the admin gate on presets;
  - the reports include comments (initials only, no IP) and `report_log`
    rows;
  - escaping: report HTML with `<img src=file:…>`; UI and Plotly labels in a
    headless browser.
- **Regression:** a committed small derived fixture (`.npz` of t/y for one
  real diesel sample and its standard, no customer identifiers) that
  produced dozens of bullets must produce at most ranges+1, in CI.

## Versioning and release notes

Ships as **v3.0.0**: CLAUDE.md makes any change to what is displayed or
recorded a MAJOR bump, and the report text changes.

`docs/release-notes/v3.0.0.md` covers:
- new bullet rules (one per deviating range plus one outside; spike-only
  ranges reported; threshold lines and spike markers on the plot);
- bullets are read-only; free text moves to comments; the conclusion stays
  editable;
- comments and presets; initials are self-declared, not authenticated;
- the report footer lists parameters, ranges and version; `report_log`
  records every QBench upload and export;
- migration v2 (additive); rollback to v2.0.0 is safe, but v2.0.0's reports
  omit comments and it writes no `report_log`;
- new settings: `analysis_min_width_min`, `analysis_merge_gap_min`,
  `analysis_spike_report_threshold`.

CLAUDE.md updates: the `analysis_core` description (`range_windows`,
`build_deviation_report`; `/api/analysis` now really uses
`resolve_report_ranges`, which it wrongly claims today), `report_payload.js`
(params captured, no bullets), the comments module, and schema v2.

## Impact elsewhere

- Parity report, dashboard numbers, `/api/table` and Export to LIMS: no
  impact; they carry no bullets.
- Imported v1 revisions without a CDF already can't produce reports (the
  QBench path refuses result-only samples).

## Open items

- None. (The lowest severity keeps the name **"marginal"**, matching today's
  text and the UI's threshold label; "slight" was never used.)
