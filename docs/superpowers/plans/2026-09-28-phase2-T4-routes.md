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
- `_sample_or_404(sid)` → the `samples` row; `SampleNotFound` → **404** JSON;
  a non-integer id → **400**.
- A revision's files: the trace and curve read the revision's own
  `sample_results.cdf_path` (a1/replace) and the blank file recorded on the
  revision (`pipeline.revision_blank_path`, a1/blankprov), never the
  sample's or blank sample's *current* file.
- A JSON 404 handler for `/api/*` (so a removed route answers
  `{"error": "Not found"}` with 404, not HTML).
- `_gc1()` → the `gc1` instruments row (503 if missing); `_ctx(conf)` =
  `instruments.context(gc1, conf)`, the conf every calibration consumer
  gets (GC_CAL_CDF is never honoured).
- The export/QBench gate is `store.samples.is_gated` (`GATE_SQL`), checked
  on the server by sample id.

## Route-fate table

`tests/test_route_fates.py` parses this table and asserts that the set of
`(path, methods)` the app registers (AST: `@app.route`/`add_url_rule` in
app.py plus the routes of every Blueprint app.py registers) equals every row
whose fate is not `removed`, and that no `removed` path is registered. Paths
are exactly as written in the decorator (module constants resolved).

**Auth** (sign-in, v3.1.0; `web_auth.route_class`, pinned by the same test):
`session` needs a signed-in session (a page redirects to `/login?next=`, an
API answers 401 `{login_required: true}`); `open` needs none (health, the
sign-in routes, the agents' bearer-token paths, which are listed exactly:
`/api/agents` is `session`); `local` needs none only from the hub machine
itself (`netctx.is_local()`: the hub tray), otherwise `/api/admin/hub/*` is
403 and the others need a session; `setup` is open until an admin password
is set and only when not through Cloudflare, else it needs a session. Admin
routes also need the admin password in the body, as before. `—` = removed.

<!-- route-fates:begin -->
| Route | Methods | Fate | Auth | Request → response |
|---|---|---|---|---|
| `/api/files` | GET | migrated | session | query `instrument, status (comma list), q, date_from, date_to, method, backfill (0/1), limit (default 500, max 5000), offset` → `{samples:[{sample_id, instrument, lab_id, display_name, injection_dt, status, error, flags, best_fit:{label,score}\|null, backfill, released, time_corrected, method_name, current_revision, review_note}], total, limit, offset, instruments:[id], cache_pending}`; never reads a CDF |
| `/api/files/refresh` | POST | removed | — | 404 |
| `/api/metadata/<path:filepath>` | GET | removed | — | 404; see `/api/samples/<int:sample_id>/metadata` |
| `/api/samples/<int:sample_id>/metadata` | GET | new | session | → `{sample_id, instrument, lab_id, sample_name, injection_datetime, injection_dt_source, legacy_injection_dt, time_corrected, method_name, source_name, status, error, review_note, backfill, released_at, current_revision, qbench_revision, qbench_uploaded_at, revisions:[{revision, reason, by, processed_at}]}`; 404 unknown id |
| `/api/trace` | GET | removed | — | 404; see `/api/samples/<int:sample_id>/trace` |
| `/api/samples/<int:sample_id>/trace` | GET | new | session | → `{sample_id, x, y, name}` from the sample's stored CDF; 404 unknown id or no CDF |
| `/api/distillation-curve` | GET | removed | — | 404; see `/api/samples/<int:sample_id>/distillation-curve` |
| `/api/samples/<int:sample_id>/distillation-curve` | GET | new | session | query `revision` (default current) → `{sample_id, revision, percent, temperature, d2887, d86, d86_uncorrected, blank_used, calibration:{cdf, anchors}}`; the curve is rebuilt from **that revision's** `calibration_used` anchors and `blank_used` sample's CDF; the numbers are the revision's `results`/`d86_uncorrected` (never recomputed); 404 unknown id, 409 no revision |
| `/api/table` | GET | migrated | session | → `{columns: CSV_HEADER, rows:[[str...]], sample_ids:[...]}`: the current revision of every sample that has one, oldest injection first, cells as the CSV writes them |
| `/api/calibration` | GET, POST | migrated | session | same shapes; reads/writes the `gc1` row (`calibration_cdf`, `calibration_assignments` list, `calibration_sensitivity`). POST adds `queued` (= `pipeline.on_calibration_saved('gc1')`) |
| `/api/calibration/active` | GET | migrated | session | same shape from the `gc1` context; `mode` is `manual` or `unusable`, plus `problem` |
| `/api/scan` | POST | removed | — | 404 |
| `/api/scan/status` | GET | removed | — | 404 |
| `/api/scan/stream` | GET | removed | — | 404 |
| `/api/stop-scan` | POST | removed | — | 404 |
| `/api/rebuild-db` | POST | removed | — | 404 |
| `/api/library/reindex-times` | POST | removed | — | 404 |
| `/api/reprocess` | POST | migrated | session | `{sample_ids:[int], use_current_blank?, use_current_corrections?, missing?:[str]}` or `{query, instrument}` (a lab-ID selection **requires** `instrument`, else 400) → `{status:'queued', count, sample_ids, job_ids, refused:[{sample_id, error}]}`; 404 if any id is unknown; result-only samples are refused; each queued via `pipeline.request_reprocess` |
| `/api/reprocess/preview` | POST | migrated | session | `{query, instrument}` (400 without `instrument`) → `{matched:[lab_id], missing:[str], sample_ids:[int], instrument}` (latest injection per lab ID) |
| `/api/reprocess/status` | GET | migrated | session | query `sample_ids=1,2,3` → `{phase:'processing'\|'done'\|'idle', total, processed, errors, pending, samples:[{sample_id, status, current_revision, error}]}` |
| `/api/export-lims` | POST | migrated | session | `{sample_ids:[int]}` → `{exported:[{sample_id, revision, seq}], refused:[{sample_id, error}]}`; 200 if any exported, **409** if every one is refused by the gate (`pipeline.export_to_lims`: a new revision `export-lims` + its export row, one txn); 404 if any id is unknown; 400 if none given |
| `/api/qbench-upload` | POST | migrated | session | `{queue:[{sample_id, standard_name, sample_name?, conclusion?, bullets?, overlay_standards?, ranges?}], username?, password?, …}`; 404 any unknown id; **409** `{refused:[…]}` (nothing queued) if any sample fails the gate; the server resolves the CDF and builds the PDF; `pdf_path`/`sample_path` from the client are ignored; after a successful upload `samples.qbench_revision`/`qbench_uploaded_at` are set to the revision the PDF was built from (gate re-checked in the thread) |
| `/api/analysis` | POST | migrated | session | `sample_path` → `sample_id`; the ladder is the current revision's `calibration_used` anchors (else the gc1 context's) |
| `/api/export-analysis-report` | POST | migrated | session | `sample_path` → `sample_id`; `lab_id` comes from the store |
| `/api/export-analysis-reports-zip` | POST | migrated | session | items: `sample_path` → `sample_id`; unknown ids skipped. v3.0.1: → 202 `{job}` at once (the ZIP is built on a thread by `download_jobs`: N PDFs outlasted Cloudflare's 100 s); 400 no/bad items; 409 two builds already running; every item skipped = a failed job |
| `/api/export-analysis-reports-zip/<job_id>` | GET | new | session | v3.0.1: the report ZIP job `{job: {id, state, done, total, result: {written, skipped}, error, download}}` (only the signed-in name that started it; 404 unknown/expired) |
| `/api/export-analysis-reports-zip/<job_id>/download` | GET | new | session | v3.0.1: the finished ZIP (attachment, `Cache-Control: no-store`), streamed once then deleted; 404 unknown/unfinished/fetched; expires 10 minutes after the build |
| `/api/export-pdf` | POST | migrated | session | `{sample_id}`; 404 unknown |
| `/api/export-comparison` | POST | migrated | session | `{sample_ids:[int]}` (was `sample_paths`) |
| `/api/best-fit` | POST | migrated | session | `{sample_id}` → the live classification (`label, best_standard, score, ranking, mix`) plus `recorded:{best_fit, fit_score, revision}` from the stored revision; refreshes `sample_cache` |
| `/api/comparison-standard` | POST | unchanged | session | T5 review: admin (`password`), `{sample_id, name}` only (`source_path` → 400); the standards folder is fixed under the data folder |
| `/api/settings` | GET, POST | unchanged | session | GET shows the gc1 row's `calibration_cdf` and the fixed standards/export folders; POST (JSON only) changes only `settings.OPERATOR_KEYS`, and `settings.ADMIN_KEYS` with the admin `password` (403 without); any other changed key → 400 (T5 review C1) |
| `/api/save-analysis-defaults` | POST | unchanged | session | |
| `/api/notifications` | GET | unchanged | session | |
| `/api/live` | GET | new | session | v3.1 live updates (`live.py`, `static/js/live.js`): `?since=<boot_id>:<seq>` → `{cursor, reset, samples, instruments, agents, notifications_unread, hub: {state, staged_update}}`, answered from memory (the event ring, `hub_control`'s cache, the notification store); never SQLite; not activity |
| `/api/notifications/<notif_id>/dismiss` | POST | unchanged | session | |
| `/api/notifications/dismiss-all` | POST | unchanged | session | |
| `/api/restart` | POST | unchanged | local | |
| `/api/server-status` | GET | unchanged | session | |
| `/api/comparison-standards` | GET | unchanged | session | |
| `/api/comparison-standard/<name>` | DELETE | unchanged | session | T5 review: admin (JSON body with `password`); only inside the standards folder |
| `/api/comparison-standard/rename` | POST | unchanged | session | T5 review: admin (`password`) |
| `/api/qbench-credentials` | GET | unchanged | session | |
| `/api/qbench-upload/stream` | GET | unchanged | session | |
| `/api/qbench-upload-status` | GET | unchanged | session | |
| `/api/qbench-skip-item` | POST | unchanged | session | |
| `/api/qbench-cancel` | POST | unchanged | session | |
| `/api/qbench-update-credentials` | POST | unchanged | session | |
| `/api/qbench-api-credentials` | GET, POST | unchanged | session | |
| `/api/browse` | POST | removed | — | 404 (T5 review: a server-side desktop file picker; no meaning on ASAPSV1) |
| `/api/open-folder` | GET | removed | — | 404 (T5 review: opened a folder on the server's desktop) |
| `/admin/setup` | GET | new | setup | 2B1 `admin_auth` blueprint; unchanged by T4 |
| `/api/admin/setup` | POST | new | setup | 2B1 `admin_auth` blueprint; unchanged by T4 |
| `/api/admin/password` | POST | new | session | 2B1 `admin_auth` blueprint; unchanged by T4 |
| `/api/ingest` | POST | new | open | 2B1 `ingest_api` blueprint; unchanged by T4 |
| `/api/agent/heartbeat` | POST | new | open | 2B1 `ingest_api` blueprint; unchanged by T4 |
| `/api/agent/results` | GET | new | open | 2B1 `ingest_api` blueprint; unchanged by T4 |
| `/api/agent/package` | GET | new | open | 2B1 `ingest_api` blueprint; unchanged by T4 |
| `/api/agent/package.zip` | GET | new | open | 2B1 `ingest_api` blueprint; unchanged by T4 |
| `/api/agents` | GET | new | session | 2B1 `ingest_api` blueprint; unchanged by T4 |
| `/api/admin/instruments/<instrument_id>/installer` | POST | new | session | 2B1 `ingest_api` blueprint; unchanged by T4 |
| `/api/admin/instruments/<instrument_id>/revoke-token` | POST | new | session | 2B1 `ingest_api` blueprint; unchanged by T4 |
| `/api/admin/instruments/<instrument_id>/agent-command` | POST | new | session | 2B1 `ingest_api` blueprint; unchanged by T4 |
| `/api/admin/hub-url` | POST | new | session | 2B1 `ingest_api` blueprint; unchanged by T4 |
| `/api/admin/load-folder` | POST | new | session | T5 `hub_admin` blueprint: `{password, instrument, folder, backfill?}` → 202 `{job}` (the T6 loader in a background thread; the hub Worker processes what it submits); 400 bad folder, 404 unknown instrument, 409 a job is running |
| `/api/admin/jobs/status` | POST | new | session | T5 `hub_admin`: `{password}` → `{job}` (current or last admin job: state, progress, counts, recent files, summary; v3.0.1: `result: {summary}` once finished) |
| `/api/admin/jobs/stop` | POST | new | session | T5 `hub_admin`: `{password}` → `{job}`; asks the running job (load-folder, import-history or its dry run) to stop between batches, by raising from its progress callback; 409 if none is running |
| `/api/admin/import-history/dry-run` | POST | new | session | T5 `hub_admin`: `{password, instrument, processed_dir, results_csv?, aliases?, batch_size?}` → 202 `{job}` at once (v3.0.1; kind `import-history-dry-run` on the same `AdminJobs` runner, one job at a time, stoppable; the finished job carries `result: {summary}`; 2D `jobs.import_history`, `dry_run=True`; nothing written); 400 bad folder/CSV/aliases, 404 unknown instrument (answered before the job), 409 a job is running |
| `/api/admin/import-history/start` | POST | new | session | T5 `hub_admin`: `{password, instrument, processed_dir, results_csv?, aliases?, batch_size?, confirm: true}` → 202 `{job}` (kind `import-history`, the same `AdminJobs` runner as load-folder); 400 missing/false `confirm` or bad folder/CSV/aliases, 404 unknown instrument, 409 a job is running |
| `/api/admin/import-history/last-run` | POST | new | session | T5 `hub_admin`: `{password, instrument}` → `{last_run}` (`jobs.import_history.last_run`; defaults the admin page's form) |
| `/api/admin/exports` | POST | new | session | T5 `hub_admin`: `{password}` → `{instruments:[HubExporter.status]}`; 503 until the exporter runs |
| `/api/admin/exports/<instrument_id>/adopt` | POST | new | session | T5 `hub_admin`: adopt the export file; 409 `{error, reason}` on a refusal, 404 unknown instrument |
| `/api/admin/exports/<instrument_id>/new-path` | POST | new | session | T5 `hub_admin`: `{password, path}` (absolute `.csv` in an existing folder) |
| `/api/admin/exports/<instrument_id>/write-fresh` | POST | new | session | T5 `hub_admin`: `{password, path}` → `{status, rows}`; 409 `exists`/`in-use` |
| `/api/admin/diagnostics/estimate` | POST | new | session | `hub_admin` diagnostics: `{password}` → `{options:[{key, label, default, bytes, files, note}]}` (uncompressed size per bundle option, cached a minute; the same estimate the bundle's disk check uses); `Cache-Control: no-store`; 403 wrong password |
| `/api/admin/diagnostics/bundle` | POST | new | session | `hub_admin` diagnostics: `{password, options?: {summary, logs, tables, settings, database, exports, reports, problem_cdfs, calibration, updater, environment, all_cdfs: bool}}` → 202 `{job}` at once (v3.0.1; kind `diagnostics-bundle`, its own runner `DIAG_JOBS`; `diagnostics.build_bundle` to `<data>/diagnostics-tmp`; secrets never included, the given password redacted too; the finished job's `result.summary` is `{download, name, size, files, skipped}`; not enough free disk fails the job, 507 before v3.0.1); 400 bad options, 409 a bundle is already being built |
| `/api/admin/diagnostics/status` | POST | new | session | v3.0.1 `hub_admin` diagnostics: `{password}` → `{job}` (the current or last bundle build; `Cache-Control: no-store`) |
| `/api/admin/diagnostics/download/<token>` | GET | new | session | `hub_admin` diagnostics: the built zip (attachment, `Cache-Control: no-store`), streamed once then deleted; the token is random, single-use, 10 minutes; 404 unknown/expired |
| `/admin/hub` | GET | new | session | T5 `hub_admin`: the admin page (load folder, history import, exports, diagnostics) |
| `/api/samples/<int:sample_id>/comments` | GET, POST | new | session | phase 4 `comments_api`: GET → `{comments:[{id, sample_id, revision, text, preset_id, source, t0, t1, initials, created_at}]}` (non-deleted, oldest first; never an IP); POST `{initials, text \| preset_id, t0?, t1?}` (JSON, 64 KiB) → 201 `{comment}`; 400 bad initials (`^[A-Z]{1,4}$` after upper-casing) / text > 500, 404 unknown sample or preset, 409 already 100 comments |
| `/api/samples/<int:sample_id>/comments/<int:comment_id>/delete` | POST | new | session | phase 4 `comments_api`: `{initials}` → `{comment}`; soft delete recording `deleted_by_initials`/`deleted_by_ip`; 404 not this sample's comment, 409 already deleted |
| `/api/comment-presets` | GET | new | session | phase 4 `comments_api`: → `{presets:[{id, text, sort}]}` (active, in order) |
| `/api/admin/comment-presets` | POST | new | session | phase 4 `comments_api` (admin password): `{password, action: list\|create\|update\|reorder\|deactivate\|activate, text?, id?, ids?}` → `{presets, preset?}` (create 201; ≤ 200 chars, ≤ 50 active; reorder names every preset once) |
| `/instruments` | GET | new | session | 2A2 `instruments_api` blueprint: the Instruments page |
| `/api/instruments` | GET | new | session | 2A2 `instruments_api` blueprint: every instrument + summary (open read) |
| `/api/instruments/<iid>` | GET | new | session | 2A2 `instruments_api` blueprint: one instrument with corrections/methods/export (open read) |
| `/api/admin/instruments` | POST | new | session | 2A2 `instruments_api` blueprint: admin: create |
| `/api/admin/instruments/<iid>` | POST | new | session | 2A2 `instruments_api` blueprint: admin: edit name/enabled/method/live_since/lem_machine_uid |
| `/api/lem/machines` | GET | new | session | D10 `instruments_api` blueprint: LEM's machine list for the LEM machine dropdown, fetched server-side by `lem_machines` (60 s cache, stale on failure; read-only, never writes to LEM) |
| `/api/admin/instruments/<iid>/export-path` | POST | new | session | 2A2 `instruments_api` blueprint: admin: HubExporter.new_path |
| `/api/admin/instruments/<iid>/export-adopt` | POST | new | session | 2A2 `instruments_api` blueprint: admin: HubExporter.adopt |
| `/api/instruments/<iid>/calibration` | GET | new | session | 2A2 `instruments_api` blueprint: the Calibration page payload for the instrument |
| `/api/instruments/<iid>/calibration-candidates` | GET | new | session | 2A2 `instruments_api` blueprint: the instrument's own samples (open read) |
| `/api/admin/instruments/<iid>/calibration-cdf` | POST | new | session | 2A2 `instruments_api` blueprint: admin: calibration CDF from a sample or path |
| `/api/admin/instruments/<iid>/calibration` | POST | new | session | 2A2 `instruments_api` blueprint: admin: assignments + sensitivity → on_calibration_saved |
| `/api/instruments/<iid>/corrections` | GET | new | session | 2A2 `instruments_api` blueprint: values, source, audit (open read) |
| `/api/admin/instruments/<iid>/corrections` | POST | new | session | 2A2 `instruments_api` blueprint: admin: the corrections editor (D4b) |
| `/api/admin/instruments/<iid>/corrections/seed` | POST | new | session | 2A2 `instruments_api` blueprint: admin: gc1 once from the phase-1 file |
| `/api/instruments/<iid>/methods` | GET | new | session | 2A2 `instruments_api` blueprint: methods seen (open read) |
| `/api/admin/instruments/<iid>/methods` | POST | new | session | 2A2 `instruments_api` blueprint: admin: map/unmap → on_method_mapped |
| `/api/admin/instruments/<iid>/review-method` | POST | new | session | 2A2 `instruments_api` blueprint: admin: review_method → other_method |
| `/api/instruments/<iid>/backfill` | GET | new | session | 2A2 `instruments_api` blueprint: backfill samples (open read) |
| `/api/admin/instruments/<iid>/backfill/release` | POST | new | session | 2A2 `instruments_api` blueprint: admin: pipeline.release_backfill per id |
| `/api/conflicts` | GET | new | session | 2A2 `instruments_api` blueprint: conflicts with both files' identity (open read) |
| `/api/admin/conflicts/<cid>/keep` | POST | new | session | 2A2 `instruments_api` blueprint: admin: keep existing |
| `/api/admin/conflicts/<cid>/replace` | POST | new | session | 2A2 `instruments_api` blueprint: admin: pipeline.resolve_conflict_replace |
| `/api/standards` | GET | new | session | 2A2 `instruments_api` blueprint: D12 standards, `for_instrument` picker order (open read) |
| `/api/admin/standards/<sid>/instrument` | POST | new | session | 2A2 `instruments_api` blueprint: admin: tag a standard |
| `/api/hub/status` | GET | new | local | hub tray `hub_control`: `status_snapshot()` (version, pid, uptime, state, processing/updater paused, queue sizes, exporter pending rows, CPU %, RSS, staged update); open, read-only, not activity |
| `/api/admin/hub/pause-processing` | POST | new | local | hub tray `hub_control` (loopback only + admin password, JSON, 64 KiB): stop the Worker/exporter/maintenance, persisted in `settings_kv`; ingest keeps queueing |
| `/api/admin/hub/resume-processing` | POST | new | local | hub tray `hub_control` (loopback only + admin password): start them again |
| `/api/admin/hub/stop` | POST | new | local | hub tray `hub_control` (loopback only + admin password): write the updater's `paused` marker, stop the hub, exit without a respawn → 202 |
| `/healthz` | GET | unchanged | open | |
| `/` | GET | unchanged | session | |
| `/calibration` | GET | unchanged | session | |
| `/lab/<lab_id>` | GET | new | session | v3.1 `sample_links` (sendable links): the classic page with the lab ID's newest run selected (latest `injection_dt`, final runs first, any instrument; exact then case-insensitive match; decoded once); other runs listed on the page; 404 friendly "No GC result for lab ID … yet" page |
| `/samples/<int:sample_id>` | GET | new | session | v3.1 `sample_links`: the classic page, that run selected, Dashboard tab; 404 friendly page |
| `/samples/<int:sample_id>/compare` | GET | new | session | v3.1 `sample_links`: as above, Analysis tab; `?standard=<name>` picks the comparison standard |
| `/samples/<int:sample_id>/data` | GET | new | session | v3.1 `sample_links`: as above, Distillation Data tab |
| `/api/lab/<lab_id>` | GET | new | session | v3.1 `sample_links`: → `{lab_id, sample_id, runs:[{sample_id, lab_id, instrument, instrument_name, injection_dt, status}]}` (newest first); 404 `{error, lab_id}` |
| `/login` | GET | new | open | the sign-in page (`web_auth`): card, LabLink username/password, and on the LAN the admin-password break-glass; `?next=` (sanitised) where to go after; a signed-in visitor is redirected there |
| `/api/login` | POST | new | open | `{username, password, next?}` → LabCore `POST /api/login`; 200 `{ok, name, method: "password", next}` + the session cookie; 401 wrong, 429 throttled, 503 `{labcore_unavailable}`; through Cloudflare https only |
| `/api/login/card` | POST | new | open | `{code, next?}` (the card code as both LabCore fields) → as `/api/login`, method `card`; the code is never logged |
| `/api/login/admin` | POST | new | open | `{password, next?}` → the break-glass session `Admin (break-glass)`, method `admin`; 403 through Cloudflare; `admin_auth.check` and its throttle |
| `/api/logout` | POST | new | open | revokes the session, clears the cookies → `{ok: true}` |
| `/api/session` | GET | new | session | → `{name, method, link_url}` of the signed-in person (`link_url` = `admin_auth.sendable_hub_url()`, the base of a copied sample link: the hub URL unless it is LAN-only, then https://gc.asaplabs.net) (the gate's 401 otherwise); the SSE stream's check |
| `/api/admin/sessions` | POST | new | session | admin password; `{action: list\|revoke\|revoke-name, id?, name?}` → `{sessions: [{id, name, method, ip, created_at, last_seen, user_agent}]}` (never a token hash) |
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
