# Tests

Regression suite for the GC Viewer webapp. Built while consolidating the two
divergent copies (`GC 2026.5 WEB` = canonical, `GC 2026.5 WEB - Copy` = backup)
and fixing the QBench upload outage.

## Run

```bash
# from webapp/
python -m pytest tests/ -v          # with pytest installed
python -m unittest discover -s tests -v   # stdlib only, no pytest
```

The structural QBench guards (`test_qbench_import.py`) run on a bare
interpreter with **no third-party deps** — by design, so they catch the
"module not importable on the server" class of bug in the same constrained
conditions where it bites. Tests that need numpy / netCDF4 / flask import those
modules and are skipped automatically if the dep is absent.

## Coverage

| File | What it locks in |
|------|------------------|
| `test_qbench_import.py` | The fix: `qbench_client.py` is vendored in `webapp/` and imports locally; **no** code depends on the dead `…\COA Reviewer\V2\Past Data Manager\API` share path; the uploader keeps `LoginFailedError` + post-upload verification that `app.py` relies on. |
| `test_app_routes.py` | The `@app.route` endpoints the frontend calls are still registered (AST scan — never imports app.py, which starts the hub's threads). `test_route_fates.py` pins every registered route against the T4 plan's route-fate table. |
| `test_hub_start.py` | `hub.start`: migrate, gc1 bootstrap, gc1 corrections seeded once (never zeros), the Worker (hub-owned corrections by default), the exporter woken on each final, the nightly backup and job prune; a booted hub turns a submitted CDF into an appended export row with no worker in the test; no `GC_DATA_DIR`, no start. |
| `test_hub_pause.py`, `test_hub_control.py`, `test_hub_control_boot.py`, `tray/` | The hub tray: `HubRuntime.pause/resume` (threads stop, ingest queues, persisted flag honoured at start); `hub_control`'s routes in process (loopback only via a faked `REMOTE_ADDR`, then same-origin, JSON, 64 KiB, password; pause notice; Stop writes the updater's `paused` marker; the status snapshot and CPU sampler); booted: pause survives a restart, Stop exits with nothing respawning on the port, `/healthz` keeps its contract and carries `hub`; `tray/` (stdlib + pytest, also on Windows in `tray-ci.yml`): status → colour/text/menu, busy rule, config, client against a fake hub, the actions (password kept in memory only, Start via the updater's `resume` with fallbacks), the menu with a stub pystray, autostart and single instance. |
| `test_live.py`, `test_live_publishers.py`, `test_live_boot.py`, `test_ui_live_smoke.py` | v3.1 live updates: `live.py`'s ring, cursor, reset (missing/foreign/future cursor, overflow), thread safety, a `publish` that never raises; `poll` answers fast without opening SQLite; one test per publisher (pipeline, `ingest_api`, `instrument_admin`, notifications, exporter; `hub_control` in `test_hub_control.py`; the QBench upload record through `report_harness.py` in `test_report_content.py`); booted: `/api/live` is session-gated, an ingested CDF and a heartbeat show on the next poll, polling and the `X-GC-Background` follow-ups are not activity (the same GET without the header, or a POST with it, is); Selenium: an ingested CDF appears on the open Samples page without duplicates, a reprocess keeps the open run open, a heartbeat updates the instrument's page, all without a reload. Node: `live.test.js` (reducer), `live_poller.test.js` (cadence, backoff cap, overlapping polls, late subscriber, `bgFetch`). |
| `test_purge.py`, `test_purge_boot.py` (+ `purge_helpers.py`), `agent/test_agent_sender.py`, `js/purge.test.js` | v3.1 purge: the schema guard (every table referencing samples is handled; a new one is found and refused), a two-GC `hub_boot` hub where purging GC-1 leaves every other row identical (full dumps) and GC-2's CDFs byte-identical, no orphans, `foreign_key_check`/`integrity_check` clean, preview counts = what was removed, a second run is a no-op, `scope=backfill` spares live samples and a blank they used, referenced CDFs stay, files moved never deleted, the backup written (and never pruned), a crash inside the transaction changes nothing, the wait for running jobs, wrong confirmation/paths refused, the results CSV untouched and appends continue after it (sidecar relink), the Worker skipping a paused instrument; booted: the routes' refusals, one admin job at a time, ingest 503 during the purge then 201; the agent backs off on the 503 and resends; the panel's Start rule. |
| `test_purge_recovery.py` (+ `purge_crash_child.py`), `test_restart_guard.py`, `test_import_interrupted.py` (+ `import_crash_child.py`), `tray/test_tray_actions.py` | v3.1 review: a purge SIGKILLed at every stage (during/after the backup, inside the transaction, after the commit, mid-move) and its recovery killed at every stage end abandoned (nothing changed, backup deleted) or finished (rows gone, files moved, CSV unlinked, notified once), also through a booted hub; `busy_reasons`/`blocking_reasons` behind Stop, `POST /api/restart` (409 unless `force`, never during a purge) and `/healthz` `idle_seconds: 0`; a history import killed mid-batch is marked interrupted at the next start (notified with N of M) and resumes to exactly the uninterrupted result; the tray's Restart anyway / purge refusal. |
| `test_startup_recovery_live.py` | v3.1 re-review: the start-up recovery runs once per process and data folder and never touches a purge or import live in this process (the critic's case: a purge held after its backup keeps it and finishes `done`); admin jobs answer 503 until it has run and 409 while a restart is claimed. |
| `test_hub_admin.py` | The admin folder-loader job and the export actions (adopt, new path, write fresh), booted, admin-gated. |
| `test_admin_long_requests.py`, `test_download_jobs.py`, `test_reports_zip_job.py` | v3.0.1 (Cloudflare's 100 s, HTTP 524): the history dry run is an admin job (202 at once while a fake run blocks, request errors before any job, progress, Stop, one job at a time, `result: {summary}`); `download_jobs` (a file built on a thread, owner only, fetched once, expiry, at most 2 builds); the report ZIP booted: 202 at once, polled, downloaded once, all-skipped fails the job (the page side is the report queue's Download all in `test_ui_compare_smoke.py`). The diagnostics bundle's job is in `test_diagnostics_routes.py`/`test_diagnostics_boot.py`, the dry run's booted and page runs in `test_hub_admin_import_history.py`, and `test_route_fates.py`'s `LONG_WORK` pins which routes answer 202 `{job}`. |
| `test_netctx.py` | Sign-in D4: `client_ip` believes `CF-Connecting-IP` only from a trusted proxy (loopback + `trusted_proxies`), spoofed headers from the LAN are ignored, `is_https`/`is_proxied`/`is_local` (the tunnel is never local), the one cross-site rule (https origin through the tunnel, no hub-URL-on-another-Host exception), IPv6 throttle keys by /64. |
| `test_labcore_auth.py` (+ `labcore_stub.py`) | LabLink sign-in against a local LabCore stub: canonical name, card as both fields, `User-Agent: gc-hub/…`, and the rev 2 classification (only a JSON 4xx is a failed sign-in; 5xx, 429, `cf-mitigated`, `error code: 1010`, non-JSON 200, timeouts are `LabCoreUnavailable`). |
| `store/test_store_v3.py` | Schema v3 is one additive step (the v2 step untouched): `web_sessions` (token_hash UNIQUE and nullable), the name columns; v2.0.0's frozen store.py starts on it; the session helpers (revoke, revoke by name/method, active, prune). |
| `test_web_auth.py` | Sign-in in process (bare Flask app wired like app.py): the gate's classes and refusals (302 `next`, 401 marker, 503 on a store error), password/card/break-glass sign-in, cookies (`__Host-` + Secure through the tunnel, 7-day limit), 308 to https, HSTS, the throttles (5 per user+address, 30 per address, 100 hub-wide through Cloudflare only, IPv6 /64, card keys), revocation, idle/absolute expiry, `last_seen` off the request path, the tray's local paths, setup through the tunnel (session + code + https Host rule). |
| `test_tunnel_boot.py` | The booted app through a simulated gc.asaplabs.net with a stub LabCore: LabLink sign-in → admin setup with the code → create GC-2 → installer (hub_url https://gc.asaplabs.net, lan_url) → history dry run (a polled job), logged by name; what the tunnel never allows (http, the break-glass, the tray's controls, cross-site posts, `/healthz` internals). |
| `test_sample_links.py`, `js/deeplink.test.js`, `test_ui_sample_links_smoke.py` | v3.1 sendable links: `/lab/<lab_id>` resolution in process (exact then case-insensitive, never a substring or a LIKE wildcard, decoded once, newest final run across instruments, the other runs, unknown → friendly 404, escaped), `/samples/<id>[/compare|/data]`, `/api/lab/<id>`, the session gate and `/login?next=` round trip, `/api/session`'s `link_url` over the LAN and for a LAN-only hub URL, `next` keeping `%3F`/`%23`/`%25`/`%5C`, huge ids, an old exact ID among 5,000 newer near misses, ASCII-only case folding, the classic page's old links (`/classic/...`) redirecting to the same run and view (v6.0.0); `store/test_store_lab_lookup.py`: `samples.with_lab_id` (indexed, no limit) and the ensured `samples_lab_nocase` index; the copied link (hub URL, never `location.origin`) and the other runs under Node; in headless Chrome on the Samples page, the linked run opened in its view, other runs listed, `?standard=`, the old `/classic/...` links, runs beyond a 5,000-row list. |
| `store/test_store_v2.py`, `test_comments.py`, `test_comments_api.py`, `test_ui_comments.py` | Phase 4: schema v2 is additive, seeds the presets exactly once, and v2.0.0's store.py (frozen in `store/v2_0_0/`) starts on it; the comment/preset rules (the signed-in author and derived initials, limits, preset text copied, soft delete), `comments.for_report`'s shape and `comments.log_report`; the routes booted (JSON only, 64 KiB, cross-site guard, admin gate); v6: the `conclusion-presets` routes beside the kept v5 names; in headless Chrome, the conclusion presets admin panel (Compare's presets, notes and annotations: `test_ui_compare_smoke.py`). |
| `replay/test_replay_v1.py` (+ `replay/replay_harness.py`, `replay/v1_runner.py`, `replay/record_v1.py`) | Differential replay against the v1 production code (the share snapshot's `webapp-live`): a sweep of synthetic sample CDFs (light/heavy/narrow/wide, noise, drift, short/long runs, saturation, near-zero, odd sampling, float32, NetCDF formats, names, stamps, methods, duplicates, a second and a bleeding blank) through the hub's pipeline (submit → Worker → export line, then Export to LIMS and both Re-process modes) and through v1 in a subprocess (its own `Looker` ingestion, and `process_cdf` with the hub's blank), under several calibrations (operator, auto-zipped, 3 and 2 anchors, mis-assigned, none) and corrections (reference, all eleven, null, missing, beyond the bound); every CSV column compared as exact strings, only the documented differences allowed (each proven, e.g. v1 given the hub's blank reproduces the hub). Always: the synthetic sweep against v1's recorded output (`replay/v1_recorded_synthetic.json`). With the snapshot: v1 live on the synthetic sweep (the recording must still match), on the real calibration run and blank, and v1's 80 real results rows' D86 (Python and the Results page's JS). |
| `test_distill.py` | Core science: cumulative-area→percent, boiling-point interpolation, the ASTM D86 X4 polynomial, and the CSV upsert/dedup. |
| `test_settings.py` | `DEFAULTS` surface (incl. the `early_signal_*` keys) and a save/load round-trip in a temp folder; v1's per-port settings and instance seeding are gone. |
| `test_instance.py` | `instance.py` — port resolution precedence (`PORT` > `--port` > `GC_PORT` > 5560), validation and the port-in-use probe. Stdlib-only. |
| `test_paths.py` | Every state location under `GC_DATA_DIR`; without it every location refuses (`DataDirMissing`). |
| `test_analysis_bullets.py` | Phase 3 deviation bullets: `range_windows` (one carbon↔time conversion, extrapolation, clipping, not-evaluable), `report_params`, spikes, every rule of `build_deviation_report` and a golden string for every `render_bullets` template, the conclusion, the ranges + 1 bound. |
| `test_bullets_regression.py` | `fixtures/diesel_pair.npz` (synthetic diesel pair on the real calibration axis, built by `fixtures/make_diesel_pair.py`): the old per-excursion text gave 29 bullets, the range-driven report ≤ ranges + 1; the same-product pair and its retention-shift/±50% variants report no deviation, diesel + 1/2/5% gasoline flags Gas only. |
| `test_analysis_settings_routes.py` | Deviation-bullet admin settings are validated when saved (400, nothing written); Set as Default keeps the saved overlays unless sent. |
| `test_report_content.py` | `app._report_content` is identical across `/api/analysis`, the direct export, the ZIP and the QBench PDF (`report_harness.py` drives all four in one app process in a subprocess, with fake `comments` and QBench uploader); PDFs print the computed bullets, never client ones; escaping, footer, `report_log` rows; annotation comments print `text (Cx–Cy, a–b min; initials, date)` from the report's ladder; v6: no Comments section, annotations under "Marked regions", earlier notes under the conclusion headed "Notes"; `comments` is a hard import. |
| `test_ui_ladder_per_instrument.py` | Carbon labels per instrument: `/api/samples/<id>/trace` serves the (current or `?revision=`) revision's ladder; in headless Chrome, a gc2 sample whose anchors differ from gc1's is labelled with its own ladder on the Samples page's chromatogram; no `(i + 5)` fallback, no `/api/calibration` read in the pages' scripts. |
| `test_ui_backfill_select.py`, `js/backfill.test.js` | v5.0 Backfill select-many: range, drag (from the selection the drag began with), select-all states, pruning, 500-id chunks, the why-backfill line (the `store.is_backfill` comparison; `live_since_set_at` for runs that arrived before it was set); headless Chrome on 30 runs: a mouse drag held across a live update, a drag back, shift-click, Space/Shift+Space, rows released elsewhere drop out, Select all + indeterminate, release in chunks with progress, a touch drag. |
| `js/compare_logic.test.js`, `js/report_queue.test.js`, `test_ui_compare_smoke.py` (+ `ui_compare_harness.py`, `fixtures/compare_harness.*`) | v5.0.0 lane C: the Compare logic (slider mapping, defaults, `?standard=`, the standard pick and remembered picks, findings rows = the server's lines, Adjust validation) and the report queue store (sessionStorage, one item per sample, payloads unchanged, broken storage) under Node; in headless Chrome on a test-only harness (the real `_layout.html` rendered by jinja2, a proxy to a booted hub with a realistic diesel sample and two standards): both themes at 1366x768 and 1440x900 (no sideways scroll, AA, charts 240/150 px at 1366x768, the drawer beside the page and `gc:adjust`, "Report queue N" and its sheet), the sticky right column at 1680, best fit → pick remembered → `setStandard`, Adjust validation then recompute then Save as default through the admin dialog, the Conclusion's Edit, Add to queue, Export report (a real PDF), Download all (a real ZIP job), the QBench upload over a scripted stream (sign-in, progress, a new password, Skip, Stop), Annotate (the regions listed in its menu), v6's conclusion presets (Insert preset appends, then inserts at the cursor; the queue item carries the edited conclusion) and earlier notes (read-only, Add to conclusion, Remove), no Comments section, re-theme, unmount. |

## Not covered here

The live Selenium upload to QBench (real Chrome, real credentials, the live
QBench DOM) cannot be exercised from a dev box — it needs the production
Windows host. These tests prove the upload no longer fails at the
import/API-lookup step; the browser-automation steps still need a manual
smoke test against a real sample.

## Frontend (JS) pure-logic tests

Pure, DOM-free helpers in `static/js/` (`report_payload.js`,
`samples.js`, `flagrules.js`, `restart.js`, `qbench_api.js`,
`instruments_logic.js`, `comments.js`, `ladder.js`, `live.js`, `results_logic.js`,
`settings_logic.js`, `notifications_panel.js`, and the pure parts of
`hub_admin.js` and `diagnostics.js`) are unit-tested under Node:

```bash
node tests/js/run.js        # requires Node
```

The runner (`tests/js/run.js`) is zero-dependency (uses Node's `assert`). Keep
the helpers DOM/fetch-free so they `require` cleanly outside the browser.

## Results, Settings, Help and notifications (v5.0 lane R)

- `test_results_settings_pages.py`: source checks (shell pages, no inline
  script, text-only DOM, `readJson`, no `window.prompt`, no hard-coded
  D2887/D86 column, key lists equal to `settings.py`, bell and menu wiring,
  Restart in Admin) and a booted app (session-gated pages, `/api/settings`
  rules unchanged).
- `test_results_d86_js.py`: the Results page's uncorrected D86 equals
  `distill.x4_midpoints(distill._convert_to_d86(...))` (node).
- `test_ui_results_settings_smoke.py`: Selenium, both themes at 1366x768 and
  1440x900, Results/Settings/Help/the panel; screenshots to `GC_UI_SHOTS`.
