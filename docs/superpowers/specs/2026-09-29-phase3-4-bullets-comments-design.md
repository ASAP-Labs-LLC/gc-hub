# Phases 3 and 4: Range-driven deviation bullets and comment presets (design)

Date: 2026-09-29
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

Other bugs found in phase 1 that this phase fixes:
- A direct export sends the whole report textarea as "bullets", header lines
  included.
- The queue/ZIP export sends no trend parameters, so its PDF charts use the
  saved defaults while its bullets used the on-screen parameters.
- "Clear Annotations" deletes every line starting with "•" that contains
  " min", which also removes auto-generated bullets.

## Phase 3: Bullets

### Rules

**Inputs.** Bullets are computed on the server from the same inputs as the
difference graph:
- the trend difference, using the trend parameters in effect (quantile,
  window, sigma);
- the spike channel;
- the thresholds (marginal, moderate, significant);
- the range overlays from `analysis_core.resolve_report_ranges`;
- the sample's instrument calibration ladder
  (`distill.calibration_ladder(ctx)`).

**Per range, including only ranges that deviate:**
- **Window.** The carbon range is converted to a time window with the
  sample's ladder.
- **Deviation test.** The range deviates when some contiguous run of the
  trend difference, inside the window and at least `min_width_min` wide
  (a new setting, default 0.05 min), reaches |diff| ≥ marginal.
- **Severity.** The highest threshold reached by max |diff| within the
  window: slight, moderate or significant (named as today).
- **Direction.** HIGHER or LOWER, taken from the sign that carries the larger
  above-marginal **area** within the window. If the other sign also has a
  run at or above moderate, the bullet adds "; also lower 3.2–3.4 min" (or
  "also higher"), naming that run's span.
- **Details (in parentheses):**
  - `max ±X at T min` (signed peak);
  - `P% of range above marginal` (the fraction of window time with
    |diff| ≥ marginal);
  - sharp spikes from the spike channel whose height reaches ≥ moderate
    inside the window, as a count plus up to 3 times ("2 sharp spikes at
    0.84, 1.40 min"; "5 sharp spikes incl. 0.84, 1.40, 1.93 min").
- **Text:** `• Gas (C5–C11): HIGHER than Diesel #2 — moderate (max +620 at 1.12 min; 38% of range above marginal; 2 sharp spikes at 0.84, 1.40 min)`

**Outside ranges.** At most one bullet covering above-marginal runs (trend,
wider than `min_width_min`) that fall entirely outside every range window.
- It reports the worst severity, the dominant direction by area, and up to 3
  merged spans ("4.1–4.6, 5.2–5.3 min"), plus "+N more" when there are more.
- Runs separated by less than `merge_gap_min` (a new setting, default 0.10
  min) are merged first.
- Spikes outside all ranges are counted the same way.

**No deviations.** The report gets one line: "No deviations above the marginal
threshold within the defined ranges." When nothing deviates outside the ranges
either, it says "No deviations above the marginal threshold."

**No usable calibration ladder** (fewer than 2 points). Ranges can't be placed,
so the report gets a single bullet built from the outside rule over the whole
run, prefixed "Calibration unavailable — ranges not evaluated:".

**Bound.** There are never more bullets than **(number of ranges + 1)**.

**Conclusion.** It keeps today's overlap-based text, but reads the deviating
ranges from the new per-range results, so the conclusion and the bullets
always agree.

### Where bullets come from (one source)

- `analysis_core.build_deviation_report(...)` returns structured items:
  `[{kind: 'range'|'outside'|'none'|'no-calibration', label, c_start, c_end,
  t0, t1, direction, severity, max_diff, max_at, frac_above, spikes: [t...],
  also: {direction, t0, t1}|None, spans: [...]}]`.
- `render_bullets(items, standard_name)` turns those items into text.
- `/api/analysis` returns both the items and the text.
- **Every export computes bullets on the server** from the same parameters.
  The client's "bullets" text is no longer trusted for any export:
  - direct export;
  - queue/ZIP;
  - QBench PDF via `_run_export_analysis`.
- The export payloads carry the trend parameters and thresholds. The queue/ZIP
  gets them from `report_payload.js`, which fixes the mismatch.
- The report text area in the UI shows the generated bullets **read-only**.
  Operator free text moves to comments (phase 4).
- The existing **annotations** feature (click-to-annotate on the graph)
  becomes a source of comments: an annotation adds a comment with its time
  span. "Clear Annotations" removes only annotation comments. The bullet
  text can never be deleted by text matching.

### Settings

- New: `analysis_min_width_min` (0.05) and `analysis_merge_gap_min` (0.10).
- Both are editable where the other `analysis_*` defaults are, behind the
  admin password (as today's "Set as Default").

## Phase 4: Comments

### Data

Additive store migration (schema v2, the first post-release migration; it must
be additive):

```
comment_presets(id INTEGER PRIMARY KEY, text TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1, created_by TEXT, created_at TEXT, updated_at TEXT)
sample_comments(id INTEGER PRIMARY KEY, sample_id INTEGER NOT NULL REFERENCES samples(id),
                text TEXT NOT NULL, preset_id INTEGER, source TEXT NOT NULL,   -- 'preset'|'free'|'annotation'
                t0 REAL, t1 REAL,                                              -- annotation span (min), else NULL
                author TEXT, created_at TEXT NOT NULL, deleted_at TEXT, deleted_by TEXT)
```

- Deleting a comment is a soft delete (`deleted_*`), because comments feed
  reports.
- A comment's text is **copied** from the preset when it is added, so later
  preset edits don't rewrite history.

### Author

There is no user login. The Analysis tab has a "Your initials" box, remembered
per browser (localStorage with try/catch) and required before adding a
comment. `author` = initials plus the client IP (e.g. `RC@10.0.0.12`). It is
recorded as given and not authenticated; the release notes say so.

### UI

**Analysis tab, Comments section, under the bullets:**
- **Preset chips** (active presets in sort order). One click adds that
  comment.
- **A free-text box** with an Add button.
- **The list of the sample's comments**, each showing its text, author and
  time, with a delete button. Deleting asks for confirmation.
- **Annotation comments** show their time span.
- All strings are rendered with `textContent`.

**Presets admin** (a section on `/admin/hub`, admin-gated):
- add, edit, reorder and deactivate presets;
- seeded with Ryan's examples: "Renewable fuel — not the typical fuel.",
  "This sample is gasoline.", "Suspected contamination.",
  "Re-run requested.".

### Reports

- The analysis PDF, the ZIP export and the QBench PDF each get a **Comments**
  section after the bullets. It lists the sample's non-deleted comments in
  time order, as `text (author, date)`.
- The section is omitted when there are no comments.

### Routes

- `GET /api/samples/<id>/comments`
- `POST /api/samples/<id>/comments` with `{text | preset_id, author, t0?, t1?}`.
  Operator action, same-origin JSON, 64 KiB cap, text ≤ 500 chars.
- `POST /api/samples/<id>/comments/<cid>/delete` with `{author}`
- `GET /api/comment-presets`
- Admin (password): `/api/admin/comment-presets` (create, update, reorder,
  deactivate).

## Delivery

Two lanes in parallel, each with its own critic, then an integration critic.
They ship as **v2.1.0** (MINOR by the numbers, but bullets change the report
text, so RELEASING's "reviewer would be surprised" rule makes it **MAJOR →
v3.0.0**; decide at tag time, recommended v3.0.0).

| Lane | Scope | Files |
|---|---|---|
| P3 | bullets engine, export payload fix, read-only bullets UI, annotation→comment hook (via the P4 API) | `analysis_core.py`, `app.py` report routes, `static/js/report_payload.js`, the bullets area of `app.js` |
| P4 | comments data, routes, Analysis-tab comments UI, presets admin, report Comments section | `store.py` (migration v2), new `comments_api.py`, new `static/js/comments.js`, `templates/hub_admin.html` section, report templates |

**Seam:** P3's report builder calls `comments.for_report(sample_id)` (owned by
P4) to render the Comments section. Until P4 merges, P3 uses a stub that
returns [].

## Testing

- **Bullets engine:** unit tests on synthetic difference arrays, covering:
  - each severity;
  - direction by area, including "also" runs;
  - `min_width` and `merge_gap`;
  - spikes inside and outside ranges;
  - no-calibration and no-deviation cases;
  - the bullet count bound;
  - exact text for every template (golden strings).
- **Consistency:** a boot test runs the Analysis tab route, the direct export,
  the queue/ZIP and the QBench PDF on the same sample and parameters, and all
  must produce identical bullets.
- **Comments:**
  - store migration v1→v2 is additive, and v1 code still starts on a v2
    database;
  - routes (limits, soft delete, preset text copied);
  - the admin gate on presets;
  - the reports include comments;
  - escaping tested in a headless browser.
- **Regression:** the real-data example that produced dozens of bullets must
  produce at most ranges+1. It is built from the snapshot CDFs when present,
  or a synthetic diesel-like fixture otherwise.

## Open items

- The version bump: v3.0.0 recommended, because the report text changes.
- Whether "slight" is still the right label for the lowest severity (kept
  from today's wording).
