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

## Live updates (v3.1)

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

## GC setup guide (v3.1)

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

## Sendable sample links (v3.1)

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

## Purge instrument data (v3.1)

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

## Later releases

- **v3.2:** new Samples page, opt-in at `/next`; classic stays at `/`.
  - The list: inline "Filter lab ID" box, instrument/status chips merged into one row, 48px rows grouped by day, full lab IDs, `display_name` run numbers, backfill and time-corrected marks.
  - Held and error rows name their reason and link to the fix (Calibration / Corrections / Methods / Re-process / Release backfill). "N held · N error" are filters.
  - A "Not sent to QBench" filter (`qbench_uploaded_at`).
  - Multi-select: a hover checkbox, shift-click, and a bulk bar with Re-process, Export to LIMS, Add to report queue and Download reports; right-click is kept.
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

## Build rules

- No build step: window globals plus `module.exports`, as today; `package_release.sh` ships tracked source only.
- TDD: pure modules first:
  - Python: `setup_state`, `live` ring/cursor;
  - node: live reducer, status→glyph/text, list grouping/filtering, queue persistence.
- Selenium: tests keep running on the classic pages until each view is ported. New pages get smoke tests keyed on `data-testid`.
- The `/api/` routes stay compatible; new UI pages call the same endpoints.
