# Background scan responsiveness + batch reprocessing — design

**Date:** 2026-06-16
**Status:** Approved pending user review
**Scope:** `webapp/app.py`, `webapp/static/js/app.js`, `webapp/templates/index.html`, tests under `webapp/tests/`

## Problem

Three related complaints about the GC Viewer web app:

1. **Library doesn't populate until the initial scan finishes.** The user wants
   the sample library usable immediately while a background scan processes the
   rest.
2. **Buttons are dead during the initial scan.** "Stop Scan", "Refresh", etc.
   appear to do nothing while the watcher is grinding through the backlog.
3. **No batch operations.** The user wants shift/ctrl-click multi-select in the
   sample library to batch-reprocess several samples at once.

## Investigation findings

### The "Stop Scan does nothing" bug (root cause)

`_watcher_loop` (`app.py` ~line 2049) is a **continuous** loop. At the top of
*every* iteration it unconditionally runs `_scan_halt.clear()` (line 2060).

Sequence when the user clicks Stop:
1. `/api/stop-scan` sets `_scan_halt` → the in-flight batch halts within ~0.5s.
   The batched stop-points work correctly.
2. The loop hits its tail `_scan_stop.wait(WATCHER_POLL_SECONDS)` (5s), loops,
   **clears `_scan_halt`, and re-scans the entire backlog from scratch.**

So Stop *works* for ~5 seconds, then the watcher resumes itself. From the user's
seat, Stop "does nothing." The fix is to make Stop **sticky**, not to change the
batching.

### The "frozen UI / empty library during scan" issue

Two contributing causes:

1. **Lock contention.** `_watcher_loop` calls `_rebuild_files_cache()` after
   *every* 50-file batch (line ~2163). Each call re-reads the full distillation
   CSV under `distill._CSV_LOCK`. `/api/files` and `/api/table` need the same
   lock / read the same cache, so request handling serializes behind the scan —
   badly on a network share.
2. **GIL starvation.** Up to 4 netCDF worker threads doing CPU-bound parsing
   starve the Flask dev-server request threads.

The library cache is *already* built in `_bg_init` **before** the watcher starts,
so the data is ready on startup — the problem is that the scan makes `/api/files`
slow to answer, and the continuous re-scan never lets the system idle.

## Design

### Feature 1 — Library usable immediately + non-blocking scan

- **Throttle full cache rebuilds.** Replace the per-batch `_rebuild_files_cache()`
  with a throttled rebuild that runs at most once per `CACHE_REBUILD_MIN_INTERVAL`
  (default 10s). Add a helper `_maybe_rebuild_files_cache(force=False)` that
  tracks the last rebuild time and skips if called too soon. The watcher uses the
  throttled version mid-scan; the end-of-scan summary forces one final rebuild.
- This removes most repeated full-CSV-under-lock reads, so `/api/files` (which
  returns the prebuilt in-memory cache) answers promptly during a scan.
- `_files_cache_ready` already unblocks `/api/files`; no change needed there.

### Feature 2 — Sticky Stop + live watcher (Stop/Refresh work)

- Introduce a module-level **`_suppressed_paths: set[str]`** guarded by
  `_suppressed_lock`. Paths in this set are excluded from auto-processing.
- **`/api/stop-scan`** is unchanged in spirit: it sets `_scan_halt` +
  `lk._stop_event`, invalidates the dir cache, and flips
  `_scan_status["phase"] = "stopped"` as today. It does **not** compute the
  backlog itself (the route has no handle on the discovered file list without
  re-running rglob, which would be racy). Recording the suppressed backlog is the
  **watcher's** job — see below.
- **`_watcher_loop`:**
  - Remove the unconditional `_scan_halt.clear()` from the top of the loop.
    `_scan_halt` is cleared **only** when a new scan is explicitly requested
    (`/api/scan`) — see below.
  - Candidate filter becomes
    `candidates = [fp for fp in all_cdfs if fp not in lk._seen and str(fp) not in _suppressed_paths]`.
    Genuinely new files (never seen, never suppressed) are still processed — the
    live-watcher behavior the user chose.
  - When a stop is detected mid-batch, record the remaining un-processed
    candidates into `_suppressed_paths` before breaking.
- **`/api/scan`** clears both `_scan_halt` and `_suppressed_paths` (re-attack the
  whole backlog), then wakes the watcher as today.
- Net effect: Stop abandons the backlog permanently until the user presses
  Scan & Parse; newly-arrived files are still auto-processed; the watcher never
  silently resumes the stopped backlog.

### Feature 3 — Shift/Ctrl multi-select + batch reprocess (all panes)

Frontend only — `/api/reprocess` already accepts `samples[]` and `paths[]`.

- **Selection state:** `state.selectedUids` — a `Set` of uids — plus
  `state.selectionAnchor` (last plainly-clicked uid). The existing
  `state.selectedFile` remains the "active" single selection for chart loading.
- **Click semantics** in `onFileClick` across all four lists
  (`dash-file-list`, `chrom-file-list`, `dcurve-file-list`,
  `analysis-sample-list`):
  - **Plain click:** clears the multi-selection, selects just this item, sets it
    active, sets anchor. Existing per-mode actions (load dashboard, run analysis)
    still fire.
  - **Ctrl/Cmd-click:** toggles this uid in `selectedUids`; updates anchor; does
    **not** trigger the per-mode load action.
  - **Shift-click:** selects the contiguous visible range between anchor and this
    item (inclusive); does not trigger the per-mode load action.
- **Pure, unit-testable helpers** `computeRangeSelection(orderedUids, anchorUid, targetUid)`
  and `selectionFilesOr(files, selectedUids, file)` → extracted as standalone,
  DOM-free functions in `static/js/selection.js` so they can be `require`d and
  unit-tested under Node (see Testing). `app.js` consumes them.
- **Rendering:** `renderFileList` adds a `selected-multi` class to every item
  whose uid is in `selectedUids` (distinct styling from the single active
  `selected`). New CSS rule in `style.css`.
- **Context menu:** when `selectedUids.size > 1`, the reprocess item reads
  **"Reprocess N selected"** and posts `{ paths: [...] }` (real paths) /
  `{ samples: [...] }` for the whole selection, mirroring the existing
  single-sample payload logic. With ≤1 selected, behavior is unchanged.

### Feature 4 — "Export to LIMS" (re-send results through the existing CSV tunnel)

**Decision:** ONE export system. "Export to LIMS" does **not** introduce a second
CSV file or a second writer. It re-sends each selected sample's already-computed
result row down the **same** path the auto-pipeline uses — `distill._upsert_csv_row`
— into the main results CSV (`distill_output`). Because that helper is
replace-or-append keyed on `(Lab ID, InjectionDateTime)`, re-sending a row is
**idempotent** (it refreshes the existing row; no duplicates). Future CSV-format
changes touch exactly one writer.

- **No new setting, no new CSV file, no `_LIMS_LOCK`.** Reuse `distill._CSV_LOCK`
  (the upsert already takes it) and `distill_output`.
- **Backend route:** `POST /api/export-lims` accepting
  `{ "rows": [ {"lab_id": str, "inj_dt": str}, ... ] }`.
  - For each request, locate the sample's result row in the current results CSV
    keyed on `(Lab ID, InjectionDateTime)`; blank/unmatched `inj_dt` falls back to
    the most-recent row for that `lab_id`.
  - Re-write each found row via `distill._upsert_csv_row` (one row → one idempotent
    upsert into `distill_output`).
  - Return `{ "status": "ok", "exported": N, "missing": [lab_ids...] }`. Missing
    Lab IDs (no resolvable result row) surface in the notification tray, mirroring
    the reprocess-missing pattern.
- **Pure helper** `lims_export.collect_export_rows(csv_rows, requests)` →
  `(found_rows, missing_lab_ids)` in a new dependency-free module
  `webapp/lims_export.py`, so the row-matching is unit-testable without flask or a
  real CSV. `app.py` reads the CSV (under `_CSV_LOCK`), delegates matching to this
  helper, then upserts each found row back through `distill._upsert_csv_row`.
- **Frontend context menu:** add a static item `#ctx-export-lims`. With
  `selectedUids.size > 1` it reads "Export N to LIMS" and posts the full
  selection's `{lab_id, inj_dt}` rows; otherwise it exports just the right-clicked
  sample. On success, show a notification with the exported count.

## Testing strategy (TDD — tests written first)

Follow `tests/` conventions: import-`app` tests stop the watcher in `setUp`
(like `test_reprocess_preview.py`); pure-logic tests stay dependency-free.

1. **`test_scan_stop_sticky.py`** (imports `app`, flask-gated):
   - After setting `_scan_halt` and populating `_suppressed_paths`, a simulated
     watcher iteration does **not** clear `_scan_halt` and does **not** re-queue
     suppressed paths. (Test the candidate-filter + halt-persistence logic by
     calling the extracted helper, not by running the real rglob.)
   - `/api/scan` clears `_scan_halt` and `_suppressed_paths`.
   - The watcher's halt handler records its un-processed candidates into
     `_suppressed_paths` (test the extracted recording helper directly).
2. **`test_cache_rebuild_throttle.py`** (pure / minimal):
   - `_maybe_rebuild_files_cache` skips when called within the throttle window
     and runs when `force=True` or the window has elapsed. Use a monkeypatched
     clock so no real time passes.
3. **Frontend JS unit tests** (real, run under Node — TDD is not backend-only):
   `tests/js/selection.test.js` exercises `static/js/selection.js`:
   - `computeRangeSelection` returns the correct inclusive slice for forward,
     backward, single-item, and anchor==target cases, and is order-stable.
   - `selectionFilesOr` returns the full multi-selection when >1 uid is selected
     and falls back to `[file]` otherwise.
   Run via `node tests/js/run.js` (a tiny zero-dependency assert runner); command
   documented in `tests/README.md`. Helpers must be DOM/fetch-free so they
   `require` cleanly under Node.
4. **`test_lims_export.py`** (pure, dep-free — imports only `lims_export`):
   - `collect_export_rows` returns the exact `(Lab ID, InjectionDateTime)` match
     when present; falls back to the newest row for a `lab_id` when `inj_dt` is
     blank; reports unmatched `lab_id`s in `missing`; preserves request order.
5. **`test_export_lims_route.py`** (imports `app`, flask-gated, watcher stopped
   in `setUp`): `POST /api/export-lims` upserts matched rows into a temp
   `distill_output` via `distill._upsert_csv_row`, is idempotent on repeat, and
   returns `exported`/`missing`.
6. **`test_app_routes.py`** — add `/api/export-lims` to `EXPECTED_ROUTES`
   (the only new route).

## Out of scope (YAGNI)

- No change to the netCDF parsing, D2887/D86 math, or CSV schema.
- Batch operations are limited to **reprocess** and **export-to-LIMS** (no batch
  delete / batch comparison-standard). Export-to-LIMS reuses the existing
  `distill._upsert_csv_row` tunnel into `distill_output` — no second CSV file,
  no second writer, no bespoke LIMS schema mapping until requested.
- No move off the Werkzeug dev server / no multiprocessing rework. The GIL
  mitigation is limited to removing avoidable lock contention and (optionally)
  trimming initial-backlog worker count; we do not re-architect the server.
- Windows-isms (`run.pyw`, chromedriver, UNC paths) are untouched.

## Addendum — 2026-06-16 debugging (CSV append did not work)

User report: Export-to-LIMS and reprocess both "did not append to the CSV." A
reproduction harness (`tests/manual/run_repro.sh`) drove the real distillation
tunnel against the one on-box CDF and proved:

- **Root cause (Export-to-LIMS):** the first implementation *read* `distill_output`
  and *re-wrote matching rows*. Every library sample is already in that CSV, so
  re-sending was a silent in-place no-op; a sample absent from the CSV matched
  nothing and wrote nothing. Export could therefore never produce a result row.
- **Reprocess** works end-to-end **when `calibration_cdf` is configured** (the
  harness wrote/updated a row). With no calibration set, `process_cdf` raises
  `FileNotFoundError` and the task records `error` — silently, from the user's
  seat. It also **upserted** (replace-in-place), so no *new* row appeared.

**Revised decisions (supersede Feature 4 above):**

1. **Export-to-LIMS reuses the reprocess tunnel.** It resolves the selected
   sample(s) to CDF path(s)/Lab IDs and runs them through the *same* compute path
   as reprocess (`looker.reprocess_paths` / `reprocess_samples` → `process_cdf`),
   writing into `distill_output`. One tunnel, one system. The read-and-rewrite
   `lims_export.py` module + `collect_export_rows` are **removed** (dead).
2. **Reprocess + export APPEND a new row every run** (user choice — duplicates on
   `(Lab ID, InjectionDateTime)` are acceptable). `process_cdf(reprocess=True)`
   now calls a new `distill._append_csv_row` (always append) instead of
   `_upsert_csv_row`. The auto-scan path keeps its dedup guard so background
   scanning never duplicates. `_upsert_csv_row` stays (still unit-tested) but is
   no longer on the reprocess path.
   - The results **table** (`/api/table`) shows *all* CSV rows, so each run is a
     visible new line. The **library** list dedups on `(Lab ID, inj_dt)`, so it
     stays clean (one entry per sample).
3. **Surface reprocess/export errors** in the notification tray when a run yields
   ≥1 error (e.g. missing calibration), so silent failures stop reading as
   "nothing happened."

**Revised tests:**
- `test_distill.py`: `_append_csv_row` appends a second row for an identical
  `(Lab ID, InjectionDateTime)` (proves append-always); `_upsert_csv_row` tests
  unchanged.
- `test_export_lims_route.py`: rewritten — `/api/export-lims` dispatches the
  selection through the (stubbed) reprocess tunnel and a row is appended; errors
  surface to the notification tray.
- `tests/manual/run_repro.sh` + `repro_csv_append.py`: kept as a living manual
  reproduction/verification harness.
