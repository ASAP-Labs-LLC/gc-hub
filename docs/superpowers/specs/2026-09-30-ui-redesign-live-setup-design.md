# UI redesign, live updates and the GC setup guide (v3.1 → v4.x)

Status: design rev 1 (2026-09-30). Ryan asked for:
- "a reimagining of the UI … Hyper monochromatic, well spaced out, chatGPT website inspo … like COA Reviewer"
- "a little step by step guide for setting up a GC for the first time"
- "agent stuff should update immediately, and not need a page refresh"

He delegated approval to the internal critic. This spec folds in that critique; the mockups and critique are in the session scratchpad (`ui/mock/`, `NOTES.md`).

## Visual system (applies to every new page)

- **Tokens.** New `static/css/tokens.css`: COA Reviewer's tokens, same names, with light as the default and `data-theme="dark"`, plus hub additions:
  - status colours: final `#15803d`, held `#b45309`, error `#b91c1c`, processing ink;
  - deviation fill: above `#fecaca`/red line, below `#bfdbfe`/blue line;
  - chart ink and grid.
- **Monochrome.** Colour only for status, the deviation fill and the focus ring. The primary button is ink (near-black). Pill buttons, 12px card radius, hairline borders, system UI font, 14px body text.
- **Contrast.** Every piece of text meets WCAG AA.
  - `--text-subtle` (#94a3b8) is decoration only, never text.
  - Captions use `--text-muted` #64748b.
  - Muted text on sunken tracks uses #5b6678.
- **Status never by colour alone.**

  | Status | Glyph |
  |---|---|
  | final | filled dot |
  | held | hollow ring with a pause mark |
  | error | triangle |
  | processing | spinner |

  Every non-final row also shows its reason text.
- **Charts stay Plotly.** Charts use a token-driven monochrome template: modebar hidden, drag to zoom, double-click to reset.
  - The sample is a 1.5px ink line drawn on top.
  - The standard is a light filled area behind it.
  - Range bands are light grey.
  - Annotations and comments.js keep using Plotly shapes.
- **Version badge** stays bottom-right on every page, and content reserves room for it. The version also appears in the user menu.
- **Shell.** A left sidebar of 260px that collapses to an icon rail below 1400px. It holds:
  - the "GC Hub" mark;
  - search (Ctrl K);
  - nav: Samples, Results, Instruments, Admin;
  - "Setup guide · Step N", shown only while an instrument is unfinished;
  - Recent (labelled "on this computer", from localStorage);
  - a footer with a "Live" dot, the bell and the user chip. The chip's menu holds Theme, Settings, Help, "Update ready: restart to install vX" and Sign out; the chip shows only the name, since there are no roles.

## Live updates (v4.0)

### Event bus
New module `live.py`, in memory:
- a ring of 1,000 events, a `boot_id` (random at start) and a monotonically increasing `seq`;
- `publish(kind, payload)` is thread-safe and never raises into the caller;
- event kinds:
  - `sample` (`{sample_id}`), published on submit, status change, final, error, reprocess, export row appended and QBench uploaded;
  - `agent` (`{instrument_id}`), on heartbeat or command delivered;
  - `instrument` (`{instrument_id}`), on any `instrument_admin` change, calibration saved, corrections saved or setup event;
  - `notification`;
  - `hub`, on pause/resume or a staged update.

Publishers:
- pipeline Worker / `submit`
- `ingest_api` heartbeat
- `instrument_admin`
- `notifications` store
- exporter
- `hub_control`

### `GET /api/live?since=<boot_id>:<seq>`
Session-gated, and listed in `_NON_ACTIVITY_PATHS`, so an open tab never blocks the 3 AM restart or the updater's idle deploy, and never keeps a session alive.

Response: `{cursor, reset, samples:[ids], instruments:[ids], agents:[{instrument_id, last_seen, version, host, status}], notifications_unread, hub:{state, staged_update}}`.
- **Cursor unchanged:** answer from memory. Nothing touches SQLite except the agent snapshot, which is read from the `hub_control` refresher's cache.
- **Different `boot_id`, or cursor older than the ring:** `reset: true`, and the client reloads its data.
- **The client** then fetches only the changed rows through the existing endpoints (`/api/files?ids=`, `/api/instruments/<id>`, …); `/api/files` gains an `ids` filter.
- **Cadence:** 3 s while the tab is visible, 30 s while hidden, and an immediate poll on `visibilitychange`/focus.

The live client is `static/js/live.js`: pure cursor/reducer logic, tested with node, plus a thin poller. It replaces the notification 30 s poll and the reprocess status poll, and the Refresh button goes. "Checked in 12 s ago" ticks on the client from `last_seen`. SSE stays only for the QBench upload stream.

## GC setup guide (v4.0)

### Derived, not stored
`setup_state.py` computes each step from existing rows, as one pure function with tests: `steps(instrument_row, facts) -> [{key, status: done|current|waiting|blocked, detail, blocker, action}]`.

Endpoint: `GET /api/instruments/<id>/setup` (session).

**Steps, in this order:**

| # | Step | Done when |
|---|---|---|
| 1 | **Create** (name, method D2887, LEM machine from the dropdown) | the instrument row exists; the LEM machine is optional but prompted |
| 2 | **Correction factors** | 11 rows in `instrument_corrections` |
| 3 | **Install the agent** | `token_issued_at` set. The installer download needs the admin password. Plain-language steps: copy the zip to the GC PC, run Install, "Connected". |
| 4 | **Agent checks in** | `agents.last_seen` present; live |
| 5 | **Calibration** (run the n-alkane standard on the GC, pick it from this instrument's received samples, assign peaks) | the calibration-usable rule |
| 6 | **Method** | the first CDF's ChemStation method is mapped (`method_map`), offered as soon as one arrives. Unmapped → samples wait as `other_method`/`review_method`. |
| 7 | **Results file and go live** | `export_path` set (or the default `results/<inst>_results.csv`) and `live_since` set. Before `live_since`, samples count as backfill and are never exported to LEM. The UI explains this in plain words. |
| 8 | **First result** | a sample of this instrument is `final` and has an `export_rows.hub_appended_at` |

Samples that arrive before calibration or correction factors wait and are re-queued automatically. The guide says so, so step order is advice, not a lock.

### `instrument_events` (additive, schema v4)
Columns: `id, instrument_id, kind, by, at, detail`.

Written by:
- installer download
- calibration saved
- corrections saved
- method mapped
- export path set
- `live_since` set
- token revoked

It feeds "done by Ryan C · 12:31" on steps and the Instruments Activity feed. Other activity is synthesised from:
- `samples.received_at`
- `corrections_audit`
- `report_log`
- `export_rows`
- `agents`

Older code must still start on this DB.

### Pages (new design)
- **`/instruments`:** a card grid. Each card shows:
  - the name;
  - the LEM machine title;
  - agent status with its glyph: Live / last seen N min ago / Never;
  - agent version;
  - samples today, held and waiting to export;
  - setup "Step N of 8" or "Ready".

  Plus "Add a GC" and a live Activity feed.
- **`/instruments/<id>`:**
  - a setup checklist at the top while unfinished, collapsed to "Ready" after;
  - sections: Agent (status, host, clock skew, commands, installer/revoke), Calibration (re-homed editor), Correction factors, Results file / export, Methods, Backfill, Conflicts;
  - the same operations as today's page, over the same routes.
- **`/setup` (Setup guide):** the same step cards for one instrument, with an instrument picker. It suits the first-time flow, with each step's explanation, primary button and blocker. "Add a new GC" starts at step 1.
- **Admin actions** keep needing the admin password. Until the elevation work (v4.1), the page prompts for it and keeps it in a JS closure for 15 minutes, never in storage.

## Sendable sample links (v4.0)

Ryan: "have the samples create custom links so that the links are sendable, and could be opened from for example COA Reviewer".

- **`/lab/<lab_id>`** (page, session): resolves a lab ID to its newest run.
  - The newest run is the latest `injection_dt`, preferring final samples, across instruments.
  - Several matches: open the newest; the page lists the others ("Other runs of 40329: GC-2 · Sep 28") to switch to.
  - No match: a plain "No GC result for lab ID … yet" page with a link to search.
  - Lab IDs are matched exactly, then case-insensitively. `/` is not allowed in lab IDs, and anything URL-encoded is decoded once.
  - COA Reviewer and other apps link here, because they know lab IDs, not hub sample ids.
- **`/samples/<sample_id>`**, **`/samples/<sample_id>/compare[?standard=<name>]`** and **`/samples/<sample_id>/data`** (pages, session): one exact run.
  - v3.1: these open the classic main page with the sample selected and the matching tab (Dashboard / Analysis with the standard picked).
  - v3.2+: the new Samples page takes over the same URLs, so shared links keep working.
  - An unknown id gives the same friendly not-found page.
- **Resolver API.** `GET /api/lab/<lab_id>` returns `{sample_id, runs:[{sample_id, instrument, injection_dt, status}]}` (session).
- **Sign-in.** Links work from signed-out browsers: the gate's `/login?next=` already carries the path, and `safe_next` accepts these paths.
- **"Copy link".** A "Copy link" action on every sample (classic: in the sample's context menu and the Dashboard header; new UI: in the sample header). It copies `<effective hub_url>/samples/<id>`, always the gc.asaplabs.net form, even when opened over the LAN, with a toast "Link copied".
- **Out of scope.** A "View in GC Hub" link inside COA Reviewer is a COA change and needs Ryan's go-ahead separately.

## Purge instrument data (v4.0)

Ryan wants to "purge only GC-1's samples, and keep GC-2 and the settings". A sample spans `samples`, `revisions`, `jobs`, `export_rows`, `sample_cache`, `conflicts`, `sample_comments` and `report_log`, plus stored CDFs. Hand-editing `gc.db` is unsafe.

- **Module and routes.** `purge.py` (an admin operation), with routes on the `hub_admin` blueprint. All of them need the admin password, JSON and a session.
  - `POST /api/admin/purge/preview {instrument, scope: all|backfill}` → counts per table, CDF file count/bytes, how many purged samples were already appended to the results CSV (`export_rows.hub_appended_at`), and any CDFs that are kept because something else references them.
  - `POST /api/admin/purge/start {instrument, scope, confirm_text: "PURGE <INSTRUMENT NAME>", new_results_path?}` → 202 `{job}` on `AdminJobs` (one admin job at a time). Progress and result come through `/api/admin/jobs/status`.
- **Order of work.**
  1. Refuse if another admin job is running.
  2. Pause processing for that instrument: its queued and running jobs are cancelled or waited for, and no new jobs are queued for it while the purge runs. Ingest for it is answered 503 "purge in progress; retry", and the agent retries.
  3. Back up `gc.db` with `VACUUM INTO backups/pre-purge-<inst>-<ts>.db`. This is never pruned by the 14-day rule.
  4. In ONE write transaction, delete every row tied to the purged samples:
     - `revisions`, `jobs`, `export_rows`, `sample_cache`, `conflicts`, `sample_comments`, `report_log`, `samples`, and any other table that references `samples.id`. Found from the schema, with a test that fails if a new referencing table is added and not handled.
     - `PRAGMA foreign_key_check` must be clean afterwards.
  5. After the commit, MOVE the CDF files to `<data>/purged/<inst>-<ts>/` (same relative layout), never delete them.
  6. Record an `instrument_events` row, raise a notification ("Ryan C purged 1,234 GC-1 samples (all) · backup … · files in purged\…"), and publish `live` events.
- **Kept, always:**
  - the instrument row and its settings: calibration CDF and assignments, corrections, `method_map`, `lem_machine_uid`, `live_since`, `export_path`, agent and token;
  - other instruments' data;
  - global settings and presets.
- **Referenced CDFs are never moved.** This covers any CDF referenced by an instrument's calibration, a comparison standard, or a revision of a sample that is NOT purged (e.g. a blank used across the scope boundary). The preview lists them.
- **`scope: backfill`** purges only `is_backfill` samples, i.e. imported history. This is for redoing an import.
- **The results CSV is append-only and is not edited.** If any purged sample had been appended, the preview warns that re-sent runs will be appended again. It offers `new_results_path` (validated like `exports new-path`), applied through `HubExporter` after the purge.
- **UI.** An Admin "Purge instrument data" panel:
  1. instrument + scope;
  2. Preview;
  3. a typed confirmation with the instrument name;
  4. Start, with progress.

  It includes plain-language text about the backup and the purged folder, and how to restore (DEPLOY.md: stop the hub, copy the backup over `gc.db`, move the files back).
- **Tests.** Cover:
  - purging GC-1 leaves GC-2 byte-identical, row for row;
  - the settings survive;
  - no orphan rows (every table checked);
  - CDFs are moved, and referenced ones are kept;
  - the backup is written;
  - it refuses while a job runs;
  - a wrong confirmation is refused;
  - `scope=backfill` spares live samples;
  - a purge crash mid-transaction leaves the DB unchanged;
  - ingest during the purge answers 503 and succeeds after it.

## Global status, a way home, and Hub admin (v4.0 lane E)

Ryan said: "there is no UI indicator for any dry runs, imports running, and a way to get back to the home page from the admin hub … more information does not always equal clarity, deliberate design does … tell me which GC agents are live from the home page … the sample list should be numerical, or chronological, but it seems sporadic."

The site-wide UX review (session scratchpad `ui/UX-REVIEW.md`) drives this section.

- **Task registry.** `tasks.py`, in memory, fed by:
  - `AdminJobs`: load-folder, history import, dry run, purge;
  - the diagnostics runner;
  - `download_jobs` (report ZIPs);
  - the QBench upload thread;
  - reprocess batches.

  Each task is `{id, kind, title, instrument?, state: running|done|failed|stopped|interrupted, progress: {done,total,text}?, by, started_at, ended_at, open_url}`. There are no paths, summaries or tokens in it.
- **Task visibility and the `/api/live` payload.**
  - Every signed-in user sees titles, progress and "by".
  - Only the task's owner, or an admin, can open its result or download it. That rule is unchanged; the result stays behind its existing auth.
  - `/api/live` carries `tasks`: running tasks, plus tasks that finished within the last 30 minutes.
  - `hub` gains the queue counts and `processing_paused` that `hub_control` already holds.
- **Agent liveness: one rule, on the server.** Live means `last_seen` within 3× the heartbeat interval (90 s), measured on the hub's clock. `/api/live` `agents[]` carries `{instrument_id, name, live, last_seen_age_s, …}`. Every page uses it; the pages' own 15 min and 150 s rules are removed.
- **"Running now" indicator on every page.**
  - It sits in the sidebar footer on shell pages, and replaces "Ready" at bottom-left on the classic main page.
  - While work runs it reads "Importing GC-2 history · 3,000/12,000".
  - Click it for a list of tasks with Open / Download / Dismiss.
  - A finished or failed task stays for 30 minutes with a clear outcome line.
  - A banner shows on every page while processing is paused.
- **Home page GC strip.**
  - Compact chips on the classic main page's top bar, e.g. "GC-1 ● Live · GC-2 ○ Never checked in". They replace the removed Refresh slot.
  - Clicking a chip opens `/instruments/<id>`.
  - The sidebar footer shows "2 of 2 GCs connected".
  - Status shows as glyph + text, never colour alone.
- **Sample list order.**
  - Each row shows its injection date/time.
  - Rows are grouped under day headings: Today / Yesterday / date.
  - A sort switch offers **Newest run** (the default; `injection_dt` desc) and **Lab ID** (natural numeric, so `40318-RERUN-2` sorts after `40318`). The choice is remembered per browser.
  - Runs with no readable injection time are grouped last under "No injection time".
  - Full lab IDs are shown, never truncated to "4…".
- **A way home everywhere.**
  - `/admin/hub` and `/calibration` render inside the new shell (sidebar, light tokens), so the logo leads home and nav works.
  - No page is a dead end.
- **Hub admin fixes:**
  - **Bug:** the Load-folder instrument select (`#lf-inst`) is never filled.
  - **Bug:** history-import progress renders into the Load-folder card; a dry run shows twice.
  - Unlock once with the admin password: a 15-minute closure, as on the new pages, then everything loads. No per-card "Load" buttons.
  - A running job is picked up when the page loads.
  - Messages appear next to the button that caused them.
  - Summaries render as a short table of counts, never raw JSON.
  - Stop is enabled only while a job runs.
  - Sections are reordered by frequency of use: status and running work first; one-time setup (hub address) last.
  - Settings' admin prompts stop using `window.prompt` (the password shows in plain text); they use the unlock closure.

## Later releases

- **v3.2:** new Samples page, opt-in at `/next`; classic stays at `/`.
  - The list: inline "Filter lab ID" box, instrument/status chips merged into one row, 48px rows grouped by day, full lab IDs, `display_name` run numbers, backfill and time-corrected marks.
  - Held and error rows name their reason and link to the fix (Calibration / Corrections / Methods / Re-process / Release backfill). "N held · N error" are filters.
  - A "Not sent to QBench" filter (`qbench_uploaded_at`).
  - Multi-select: a hover checkbox, shift-click, and a bulk bar with Re-process, Export to LIMS, Add to report queue and Download reports; right-click is kept.
  - **Select all matching the filter** (Ryan, 2026-09-30): "Select all" ticks the rows shown. When more rows match the current filter than are loaded, the bulk bar then offers **"Select all 1,234 matching this filter"**, as Gmail does. It uses the server-side filter, not the loaded page. Bulk actions on a filter selection run as a background task with progress (tasks.py), respect the per-call limits by chunking, and show a confirmation naming the count and the filter. "Clear selection" undoes it. The same pattern applies to every list with bulk actions: Samples, Results, and each GC's Backfill section.
  - Detail: header plus Overview (chromatogram and the Results card with Recovery | D86 | D2887, the corrected toggle, best fit and flags) and Data (numbers, calibration used, revision history, blank used). Keyboard: up/down, j/k, Ctrl K.
- **v3.3:** Compare (standard picker defaulting to the best fit, a manual pick remembered per sample; Trend/Difference with Annotate; Findings, Conclusion and Comments; the Adjust drawer, which hides the list). At ≥1600px, findings sit in a sticky right column; at 1366, the charts are 240/150px.
  - **Report queue sheet**, persisted in sessionStorage: Download all (ZIP), Upload to QBench (sign-in, progress, skip, stop, re-entering credentials), Remove, Clear.
  - "Add to queue" replaces "Send to QBench", with a toast naming the standard.
  - The Export report sheet, notification panel, Ctrl K palette and annotate popover must be mocked first.
  - **Comparison Export is dropped.** Ryan (2026-09-30): "I do not use comparison export". Its button, `/api/export-comparison` and its help text go when classic is removed (v4.1); the new UI never offers it.
- **v4.0 (MAJOR: the default UI changes how results are displayed):** the new UI at `/`, classic at `/classic` for one release.
- **v4.1:**
  - Results page: key points by default, with All points remembered per browser;
  - Settings page;
  - Admin consolidation, including change admin password, LEM address and hub address;
  - a server-side elevation flag for the "Unlocked" chip, after a security review;
  - classic and dead code removed.

## Next major release: direction from Ryan (2026-09-30)

- **Main page and Analysis.** Ryan: "don't just theme it, redesign it, same with the analysis tab". Rebuild them from the instrument-detail template (`/instruments/<id>`): the shell, sections with a left title and intro, hairline rows, and System/Light/Dark. This replaces the "opt-in /next then default" phasing, so the redesigned Samples page (Overview · Compare · Data) and a Results page become the main UI in the next major release. `/classic` stays for one release as a fallback.
- **The address bar is the link.** Every view has a URL that the page keeps current with `history.pushState`:
  - `/samples/<id>`, `/samples/<id>/compare?standard=<name>`, `/samples/<id>/data`;
  - `/samples?instrument=gc2&status=held&q=403` for filters and search;
  - `/results?…`.

  Back and Forward navigate views, and reloading restores the view. Links are relative to the host in use, so `http://asapsv1:5560/...` works on the lab network and `https://gc.asaplabs.net/...` works anywhere. "Copy link" keeps copying the gc.asaplabs.net form, and the address bar gives the local form. `/lab/<lab_id>` resolves as today.
- **Dynamic themes on every page:** System (the default; follows the OS live), Light and Dark.

## v5.0.0: the build plan (2026-09-30, ship target: today)

Ryan: "roll it all into the next major update, and send it out today". v5.0.0 contains:
- the v4.0.1 hotfix work (Backfill select all, shift-click and drag; System/Light/Dark);
- the redesigned main page and Analysis;
- the address bar as the link;
- "select all matching this filter".

Integration branch: `release/v5`.

**Template.** `/instruments/<id>` (the shell, tokens, sections, hairline rows). The mockups are `ui/mock/shots/samples-*.png` and `results.png`/`settings.png`, adjusted by the critique folded into "Later releases" above: status glyphs plus reason, AA contrast, Plotly kept, "Add to queue" not "Send to QBench", a Report queue with Download ZIP plus Upload to QBench, and held samples linking to their fix.

**Routes.** All pages extend `_layout.html`.
- `/`, `/samples` and `/samples/<id>[/compare|/data]` all render `templates/samples.html` (lane S). The client router (`static/js/samples_router.js`) keeps the URL current with `pushState`: filters, search and sort go in the query (`?instrument=&status=&q=&sort=&notsent=1`), and a reload restores the view.
- `/lab/<lab_id>` resolves as today and lands on `/samples/<id>`.
- `/results` (lane R): the all-samples table.
- `/settings` (lane R): the settings page.
- `/classic`: the old `index.html`, kept for one release. Every old deep link (`?sample=`, `/samples/<id>` on classic) keeps working, or redirects.

**Lanes, with fixed contracts between them.**

**Lane S: Samples page.**
- The list:
  - filter chips (instrument and status, including "Not sent to QBench"), an inline lab-ID filter, day headings, the sort switch (`sample_order.js`);
  - live rows (`live.js`) and held-reason text linking to the fix;
  - multi-select: hover checkbox, shift-click, drag, keyboard;
  - a bulk bar (Re-process, Export to LIMS, Add to report queue, Download reports), plus **"Select all N matching this filter"**, which runs as a server-side task with a confirmation.
- The detail pane:
  - header: lab ID, status pill with reason, instrument, injection time, revision; actions **Export report**, **Add to queue**, and "⋯" (Re-process, Export to LIMS, Download CDF, Copy link);
  - a segmented control, Overview · Compare · Data.
- **Overview:** the chromatogram (Plotly monochrome template, drag to zoom, carbon ticks from `ladder.js`), the Results card (Recovery | D86 | D2887, the corrected toggle, 40/60 midpoints with tooltip, best fit, flags), and other runs of this lab ID.
- **Data:** the full numbers, the calibration used, revision history (why), the blank used, `qbench_uploaded_at`.
- **Compare:** mounts lane C's module through the contract below.
- Keyboard: up/down and j/k through the list, and Ctrl K to focus the filter.
- The main page's "running now", GC summary and paused banner come from the shell footer; nothing is duplicated.

**Lane C: Compare, report export and the report queue.**
- It provides `static/js/compare_view.js`: `window.GCCompare.mount(el, {sample, standards, settings, onUrlChange}) -> {unmount(), setStandard(name)}`. It owns:
  - the standard picker (default is the best fit; a manual pick is remembered per sample in localStorage);
  - the Trend and Difference graphs with range bands, Annotate, fullscreen, drag-zoom;
  - **Findings** (the deviation bullets as a list), **Conclusion** (read-only until Edit), and **Comments** (`comments.js`, with preset chips and the composer);
  - the **Adjust** drawer (baseline, detail, smoothing, thresholds, range overlays, x-max, Save as default with the admin unlock); opening it hides the list, via a `gc:adjust` event the page listens to.
  - It calls `onUrlChange({standard})` so lane S can keep `?standard=` in the URL.
- It also provides `static/js/report_queue.js`: `window.GCReportQueue` (`add(item)`, `remove(id)`, `clear()`, `items()`, `openSheet()`). The queue is persisted in sessionStorage. The sheet offers Download all (ZIP job via `download_jobs`) and Upload to QBench (the existing SSE flow: sign-in, progress, skip, stop, re-entering credentials).
- An **Export report** sheet (one sample: PDF download with the current Compare parameters). Report payloads are built by `report_payload.js`, unchanged in format, so the server is untouched.
- The shell top bar shows "Report queue N" (a button that opens the sheet).

**Lane R: Results, Settings, Help and notifications.**
- `/results`: the table with grouped D2887 and D86 headers built from `/api/table` `columns`; Key points by default and All points on a toggle (remembered); filters in the URL; CSV download; a row links to `/samples/<id>/data`; select rows to overlay distillation curves.
- `/settings`: grouped sections.
  - Display: flag rules and their editor (`flagrules.js`).
  - Best fit and analysis defaults: admin.
  - Comparison standards: tag, rename, delete; admin.
  - QBench: API credentials (admin) and the web login.
  - Server paths: read-only, collapsed.
  - Everything the classic Settings modal did, with no `window.prompt`.
- `/help`: a short plain-language page.
- A notifications panel in the shell (the bell), replacing the classic dropdown.
- Wire the user menu's Settings and Help to these pages.

**Shared rules.**
- `readJson` everywhere; `GCLive.bgFetch` for background GETs; `textContent` only.
- One admin unlock per page (`GCAdminUnlock`).
- System/Light/Dark from the shell.
- AA contrast (`test_ui_contrast.py`).
- Selenium smoke tests keyed on `data-testid` at 1366x768 and 1440x900, in both themes.
- No server behaviour changes except small read-only additions, which must be listed in the lane report.
- Comparison Export is not offered.
- Each lane adds its own section to `docs/release-notes/v5.0.0.md`.

## Build rules

- No build step: window globals plus `module.exports`, as today; `package_release.sh` ships tracked source only.
- TDD: pure modules first:
  - Python: `setup_state`, `live` ring/cursor;
  - node: live reducer, status→glyph/text, list grouping/filtering, queue persistence.
- Selenium: tests keep running on the classic pages until each view is ported. New pages get smoke tests keyed on `data-testid`.
- The `/api/` routes stay compatible; new UI pages call the same endpoints.
