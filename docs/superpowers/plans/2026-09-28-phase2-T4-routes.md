# Phase 2 / 2A1 T4: route migration and UI to `sample_id` (implementation plan)

> Parent plan: `2026-09-28-phase2-2A1-store-pipeline.md` (T4). Spec: "Route
> migration (2A1)", "Status machine", "Exports", "Processing per instrument".
> Branch `a1/routes` from `feat/phase2`. TDD: red first, per task.

**Goal.** Every route that named a sample by file path names it by
`sample_id` and reads the store. Reports, the dashboard numbers and Export to
LIMS read the **stored revision** and never recompute. The file list is a
store query that never reads a CDF. The scan/rebuild/refresh routes and
their UI controls are gone.

**Not in T4 (T5):** refusing to start without `GC_DATA_DIR`, one startup
function owning the Worker/exporter/backups, deleting the Looker/watcher and
the legacy tests of it, `_NON_ACTIVITY_PATHS` tidy-up (2B1 edits it; the stale
`/api/scan/status` entry is harmless), packaging, docs.

## How the routes reach the store

- `_hub()` → `(data_dir, db_path)`. `HubUnavailable` (→ **503** JSON) when
  `GC_DATA_DIR` is unset or `gc.db` does not exist yet. The app does **not**
  migrate or bootstrap in T4; `instruments.startup` does (T5 wires it; the
  boot tests call it before booting the app).
- `_sample_or_404(sid)` → the `samples` row; `SampleNotFound` → **404** JSON.
- A JSON 404 handler for `/api/*` (so a removed route answers
  `{"error": "Not found"}` with 404, not HTML).
- `_gc1()` → the `gc1` instruments row (503 if missing); `_ctx(conf)` =
  `instruments.context(gc1, conf)`, the conf every calibration consumer
  gets (GC_CAL_CDF is never honoured).
- The export/QBench gate is `store.samples.is_gated` (`GATE_SQL`), checked
  on the server by sample id.

## Route-fate table

`tests/test_route_fates.py` parses this table and asserts that the set of
`(path, methods)` app.py registers (AST) equals every row whose fate is not
`removed`, and that no `removed` path is registered. Paths are exactly as
written in `@app.route`.

<!-- route-fates:begin -->
| Route | Methods | Fate | Request → response |
|---|---|---|---|
| `/api/files` | GET | migrated | query `instrument, status (comma list), q, date_from, date_to, method, backfill (0/1), limit (default 500, max 5000), offset` → `{samples:[{sample_id, instrument, lab_id, display_name, injection_dt, status, error, flags, best_fit:{label,score}\|null, backfill, released, time_corrected, method_name, current_revision, review_note}], total, limit, offset, instruments:[id], cache_pending}`; never reads a CDF |
| `/api/files/refresh` | POST | removed | 404 |
| `/api/metadata/<path:filepath>` | GET | removed | 404; see `/api/samples/<int:sample_id>/metadata` |
| `/api/samples/<int:sample_id>/metadata` | GET | new | → `{sample_id, instrument, lab_id, sample_name, injection_datetime, injection_dt_source, legacy_injection_dt, time_corrected, method_name, source_name, status, error, review_note, backfill, released_at, current_revision, qbench_revision, qbench_uploaded_at, revisions:[{revision, reason, by, processed_at}]}`; 404 unknown id |
| `/api/trace` | GET | removed | 404; see `/api/samples/<int:sample_id>/trace` |
| `/api/samples/<int:sample_id>/trace` | GET | new | → `{sample_id, x, y, name}` from the sample's stored CDF; 404 unknown id or no CDF |
| `/api/distillation-curve` | GET | removed | 404; see `/api/samples/<int:sample_id>/distillation-curve` |
| `/api/samples/<int:sample_id>/distillation-curve` | GET | new | query `revision` (default current) → `{sample_id, revision, percent, temperature, d2887, d86, d86_uncorrected, blank_used, calibration:{cdf, anchors}}`; the curve is rebuilt from **that revision's** `calibration_used` anchors and `blank_used` sample's CDF; the numbers are the revision's `results`/`d86_uncorrected` (never recomputed); 404 unknown id, 409 no revision |
| `/api/table` | GET | migrated | → `{columns: CSV_HEADER, rows:[[str...]], sample_ids:[...]}`: the current revision of every sample that has one, oldest injection first, cells as the CSV writes them |
| `/api/calibration` | GET, POST | migrated | same shapes; reads/writes the `gc1` row (`calibration_cdf`, `calibration_assignments` list, `calibration_sensitivity`). POST adds `queued` (= `pipeline.on_calibration_saved('gc1')`) |
| `/api/calibration/active` | GET | migrated | same shape from the `gc1` context; `mode` is `manual` or `unusable`, plus `problem` |
| `/api/scan` | POST | removed | 404 |
| `/api/scan/status` | GET | removed | 404 |
| `/api/scan/stream` | GET | removed | 404 |
| `/api/stop-scan` | POST | removed | 404 |
| `/api/rebuild-db` | POST | removed | 404 |
| `/api/library/reindex-times` | POST | removed | 404 |
| `/api/reprocess` | POST | migrated | `{sample_ids:[int], use_current_blank?, use_current_corrections?, missing?:[str]}` or `{query, instrument}` (a lab-ID selection **requires** `instrument`, else 400) → `{status:'queued', count, sample_ids, job_ids, refused:[{sample_id, error}]}`; 404 if any id is unknown; result-only samples are refused; each queued via `pipeline.request_reprocess` |
| `/api/reprocess/preview` | POST | migrated | `{query, instrument}` (400 without `instrument`) → `{matched:[lab_id], missing:[str], sample_ids:[int], instrument}` (latest injection per lab ID) |
| `/api/reprocess/status` | GET | migrated | query `sample_ids=1,2,3` → `{phase:'processing'\|'done'\|'idle', total, processed, errors, pending, samples:[{sample_id, status, current_revision, error}]}` |
| `/api/export-lims` | POST | migrated | `{sample_ids:[int]}` → `{exported:[{sample_id, revision, seq}], refused:[{sample_id, error}]}`; 200 if any exported, **409** if every one is refused by the gate (`pipeline.export_to_lims`: a new revision `export-lims` + its export row, one txn); 404 if any id is unknown; 400 if none given |
| `/api/qbench-upload` | POST | migrated | `{queue:[{sample_id, standard_name, sample_name?, conclusion?, bullets?, overlay_standards?, ranges?}], username?, password?, …}`; 404 any unknown id; **409** `{refused:[…]}` (nothing queued) if any sample fails the gate; the server resolves the CDF and builds the PDF; `pdf_path`/`sample_path` from the client are ignored; after a successful upload `samples.qbench_revision`/`qbench_uploaded_at` are set to the revision the PDF was built from (gate re-checked in the thread) |
| `/api/analysis` | POST | migrated | `sample_path` → `sample_id`; the ladder is the current revision's `calibration_used` anchors (else the gc1 context's) |
| `/api/export-analysis-report` | POST | migrated | `sample_path` → `sample_id`; `lab_id` comes from the store |
| `/api/export-analysis-reports-zip` | POST | migrated | items: `sample_path` → `sample_id`; unknown ids skipped |
| `/api/export-pdf` | POST | migrated | `{sample_id}`; 404 unknown |
| `/api/export-comparison` | POST | migrated | `{sample_ids:[int]}` (was `sample_paths`) |
| `/api/best-fit` | POST | migrated | `{sample_id}` → the live classification (`label, best_standard, score, ranking, mix`) plus `recorded:{best_fit, fit_score, revision}` from the stored revision; refreshes `sample_cache` |
| `/api/comparison-standard` | POST | unchanged | also accepts `{sample_id, name}` in place of `source_path` |
| `/api/settings` | GET, POST | unchanged | GET shows the gc1 row's `calibration_cdf`; a POST that changes `calibration_cdf` updates the gc1 row (assignments for the new CDF from the settings map, if any) and queues `awaiting_calibration` |
| `/api/save-analysis-defaults` | POST | unchanged | |
| `/api/notifications` | GET | unchanged | |
| `/api/notifications/<notif_id>/dismiss` | POST | unchanged | |
| `/api/notifications/dismiss-all` | POST | unchanged | |
| `/api/restart` | POST | unchanged | |
| `/api/server-status` | GET | unchanged | |
| `/api/comparison-standards` | GET | unchanged | |
| `/api/comparison-standard/<name>` | DELETE | unchanged | |
| `/api/comparison-standard/rename` | POST | unchanged | |
| `/api/qbench-credentials` | GET | unchanged | |
| `/api/qbench-upload/stream` | GET | unchanged | |
| `/api/qbench-upload-status` | GET | unchanged | |
| `/api/qbench-skip-item` | POST | unchanged | |
| `/api/qbench-cancel` | POST | unchanged | |
| `/api/qbench-update-credentials` | POST | unchanged | |
| `/api/qbench-api-credentials` | GET, POST | unchanged | |
| `/api/browse` | POST | unchanged | |
| `/api/open-folder` | GET | unchanged | |
| `/healthz` | GET | unchanged | |
| `/` | GET | unchanged | |
| `/calibration` | GET | unchanged | |
<!-- route-fates:end -->

## Flags and best-fit (`sample_cache`)

- The list reads `sample_cache` (one connection per request) and the current
  revision's `best_fit`/`fit_score`. `flags` are shown when
  `rules_fingerprint` matches the current rules; `best_fit` from the cache
  when `bestfit_fingerprint` matches, else the revision's recorded label.
- Stale or missing rows are filled by a single-flight background thread
  (`_refresh_sample_cache`), which reads the CDFs off the request path; the
  response says `cache_pending: n` and the UI refetches once.
- The JSON caches (`.sample_flags_cache.json`, `.bestfit_cache.json`) and
  the CSV-built `_files_cache` are no longer read by any route; `_init_app`
  stops loading them. (The watcher code that still updates `_files_cache`
  goes in T5.)

## UI (keep the current look; the redesign is phase 5)

- **All lists** (`static/js/samples.js`, pure, node-tested):
  `fileEntryFromSample(s)` maps a store row to the entry the lists use
  (`uid = String(sample_id)`, `name = lab_id`, …); `statusBadge(s)` gives
  `{text, cls, title}` for every non-`final` status (`awaiting_calibration`,
  `pending_corrections`, `other_method`, `review_method`, `error`,
  `raw_only`, `received`), the title being the hold reason from
  `samples.error`; `timeCorrectedTitle(s)` for the `time_corrected` marker.
  `renderFileList` shows the badge and a small "⏱" marker; the Flagged
  filter and search keep working.
- **Dashboard:** `/api/samples/<id>/trace` and `…/distillation-curve`; the
  D2887/D86 tables are the revision's numbers. A non-final sample with no
  revision shows the chromatogram and a notice with the hold reason.
- **Chromatograms / Distillation curve tabs:** traces keyed by `sample_id`.
- **Analysis tab:** `sample_id` in `/api/analysis`, `/api/best-fit`, both
  report exports (`report_payload.js`: `sample_id` replaces
  `sample_path`), the analysis queue and the QBench queue (`{sample_id, …}`;
  a 409 lists the refused samples).
- **Context menu:** Reprocess and Export to LIMS send `sample_ids`; Export
  shows refused samples. "Set as comparison standard" sends `sample_id`.
- **Re-process modal:** preview/confirm send `instrument` (the list's
  instrument, `gc1` today) and confirm with `sample_ids`; the toast polls
  `/api/reprocess/status?sample_ids=…`.
- **Removed:** the Scan & Parse, Stop Scan and Rebuild DB toolbar buttons,
  the Settings "Re-derive injection times & reorder" block, the scan SSE and
  scan-status polling code, and `/api/files/refresh` in Refresh.
- **Export PDF / comparison:** `sample_id(s)`.

## Tasks (TDD, one commit each)

1. **Route fates.** `tests/test_route_fates.py` (AST vs this table) — red:
   new routes missing, removed ones present. Update `tests/test_app_routes.py`
   to the new surface.
2. **Boot-test harness.** `tests/hub_boot.py`: a hub data folder built with
   `instruments.startup` (worker stopped, jobs run in the test), synthetic
   CDFs from `tests/cdf_fixtures.py`, `settings.json` written, then
   `bootapp.booted`. Samples: final gated, final backfill (unreleased),
   `awaiting_calibration`-style hold, `other_method`.
3. **Read routes:** files, samples/<id>/metadata|trace|distillation-curve,
   table (+ 404s, 503 without a store). Red boot tests, then implement.
4. **Removed routes** answer JSON 404; delete their code and UI controls.
5. **Actions:** reprocess (+preview/status), export-lims (gate), QBench
   queue validation (gate, unknown ids) and revision recording.
6. **Analysis/report routes, best-fit, export-pdf/comparison,
   comparison-standard by `sample_id`.**
7. **Calibration** on the gc1 row, `on_calibration_saved`; settings mirror of
   `calibration_cdf`. Update the AST guards in `test_calibration_ladder.py`.
8. **`sample_cache`** refresher; `_init_app` stops loading JSON caches/file
   cache.
9. **UI** (`samples.js` + node tests, `app.js`, `index.html`,
   `report_payload.js`).
10. Retire the in-process legacy route tests that exercised removed/migrated
    behaviour (`test_scan_stop_sticky`, `test_export_lims_route`,
    `test_reprocess_preview`, `test_reprocess_status`,
    `test_cache_rebuild_throttle`, the reindex AST guards in
    `test_library_reorder`) and adjust `test_startup`/`test_healthz`.
11. Full suite + `node tests/js/run.js`.
