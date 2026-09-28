# Phase 2: Multi-instrument hub with push agents (design, rev 3: approved by delegated critic)

Date: 2026-09-28
Repo: `ASAP-Labs-LLC/gc-hub` (phase 1 shipped as v1.0.0)
Approval: Ryan approved the direction and Sections 1–2 in conversation, and
delegated sign-off of the rest to an internal critic. Rev 1 got **REWORK
(targeted)**. This revision takes every Critical and Important finding
(C1–C5, I1–I23), the Minor items that change the design, and the YAGNI cuts.

## Goal

Replace "one full app per GC PC, data split across folders" with **one hub on
ASAPSV1 at one URL**. The hub owns every sample from every GC. Each GC PC runs
a small **push agent**, which also hands finished results back so that LEM's
station module reads them where it does today. Each instrument has its own
calibration, blank and LEM correction factors. Search and analysis cover all
instruments. The method is a pluggable field; D2887 is the only one today.

## Decisions

From Ryan:

| # | Decision |
|---|---|
| D1 | Import all history into the hub, tagged by instrument. |
| D2 | `GC2025.1/processed_cdf` (port 5560) is **GC-1**. Everything in `processed_cdfs2` is **GC-2**, including rows from June to September 2026 that predate the 5570 instance; Ryan confirmed on 2026-09-28 that the second GC existed before its instance did. |
| D2b | **Non-conforming chromatograms** (e.g. D7096 gasoline runs on a D2887 instrument) are detected by the CDF's ChemStation method name (`detection_method_name`) and **stored but not processed** (status `other_method`). See "Method detection". |
| D3 | The agent has a **tray icon** (status, restart, pause, log). The Instruments page mirrors it. The agent **self-updates from the hub** on start, on restart and hourly. |
| D4 | Corrections come **from LEM, per instrument** (one `machine_uid` per GC). **The hub applies them** and records the values used. LEM's station module must not also apply them to GC results. |
| D5 | Reprocessing keeps a sample's recorded corrections, and now also its blank, unless the operator explicitly asks for current ones. |
| D6 | **SQLite** is the source of truth. CSV files are exports. |
| D7 | Raw CDFs are kept forever on ASAPSV1. |

Made by me with the critic, as delegated (flagged so Ryan can overrule):

| # | Decision |
|---|---|
| D8 | **CSV exports are append-only.** A row is appended each time a result becomes final. Bytes already written are never rewritten, because LEM tails the file by byte offset. |
| D9 | **LEM reads GC results through the agent.** The agent pulls newly finished rows for its instrument from the hub and appends them to a local CSV on the GC PC (`results_mirror_path`), at the path the station module tails today. No share is needed and LEM's read path doesn't change. The hub also keeps its own append-only export in the data folder. |
| D10 | **Results have revisions.** Reprocessing or re-exporting creates a new revision. Earlier revisions are kept, which is the ISO 17025 record of what was reported. |
| D11 | **Backfill is never exported automatically.** Imported, orphaned or first-sync samples injected before an instrument's `live_since` are recorded but reach an export or QBench only on an explicit admin action. |
| D12 | **Comparison standards are tagged by instrument.** The picker defaults to the sample's instrument, and comparing across instruments shows a warning on screen and in the PDF. |
| D13 | **The admin password becomes a real setting** (a salted hash in the data folder, set on first use), shipped with the ingest API in 2B1. Agent tokens are only as safe as the admin gate that mints them. |
| D14 | **Legacy (share) mode ends in v2.** `app.py` refuses to start without `GC_DATA_DIR`. `run.pyw`, `looker.py` and the per-port seeding are deleted. `instance.py` keeps port resolution and `--no-tray` stays accepted. The share copies keep running v1.x until cutover. Share fixes come from branch **`maint/v1`**, cut at v1.1.0, and are hand-copied per DEPLOY.md. They are **never published as GitHub releases**: the updater follows 'latest' by equality, so a later v1.x release would downgrade ASAPSV1. A tag is allowed only with `make_latest=false`. |

## Delivery: six plans, each released to ASAPSV1

Order: **2A0 → 2A1 → 2A2 → {2C, 2B1, 2D} → 2B2**. Every plan ends green and
correct, and ships as its own tag. Phase 1's "anything that changes results
is MAJOR" rule decides the version numbers.

| Plan | Content | Bump |
|---|---|---|
| **2A0** | `distill` refactor: calibration functions take `conf` explicitly; `compute()` returns a row without writing any CSV; golden tests. No behaviour change. | MINOR |
| **2A1** | Store, pipeline and revisions; `pipeline.submit()` plus the admin job **Load CDFs from a folder** (one-shot, read-only on the source, never watching); append-only export; route migration to sample IDs; flags and best-fit moved into the store; legacy removal; the "at or before" blank rule. One implicit instrument `gc1`, with corrections from the legacy JSON file (`source:"file"`). **Acceptance gate on ASAPSV1: a parity report.** Load a robocopied sample of the GC-1 processed folder and compare each hub result with v1's CSV row for the same (lab ID, injection time). Every difference must be explained by the release notes (blank rule, auto-detect off, stricter file corrections). | MAJOR |
| **2A2** | Instruments: per-instrument calibration, sensitivity and blank; the Instruments page; instrument filter and search; standards tagged by instrument (existing standards migrate to `gc1`); instrument shown in reports. | MAJOR |
| **2C** | LEM corrections provider (replaces `source:"file"`), cache, notifications. The LEM repo change goes on its own branch and is tagged only with Ryan's say-so. | MAJOR |
| **2B1** | Admin password; ingest, heartbeat, results-pull and package API; agent tokens; `live_since`; backfill release; conflicts screen. Tested with a fake agent. | MAJOR |
| **2D** | History import as an admin job inside the hub, reusing 2A1's folder loader and matcher. A read-only dry-run spike against the real share (or the snapshot while off-network) runs **early in 2A1**, before the schema is frozen. | MINOR |
| **2B2** | The agent: launcher/supervisor, tray, ledger, send, results mirror, self-update, installer; cutover runbook. | MINOR |

Version numbers are assigned at tag time (RELEASING.md §2 applies; there is no pre-live exemption).

Before 2A1 ships to ASAPSV1, check the v1.0.0 install there isn't watching a
live folder (DEPLOY.md: `updater.py status`, settings `watch_dir`).

## Architecture

```
GC PC n: agent ──HTTP POST /api/ingest  (Bearer gcN token)──────────────┐
         agent ◀─HTTP GET  /api/agent/results?after=<seq>───────────────┤
           └─ appends rows to local results_mirror_path ◀─ LEM station  │
                                                                         ▼
ASAPSV1: gc-hub (one process, one URL, updater-managed, plain HTTP :5560 on the LAN)
  ingest → store CDF → durable job queue (in SQLite) → worker (serial)
    → methods.get(instrument.method).compute(cdf, InstrumentContext)
    → sample_results revision → export_rows (append) → UI / SSE
  LEM client ── GET <LEM_URL>/api/machines/<uid>/corrections
```

The transport is plain HTTP on the LAN, like COA and LEM. The token is a
bearer secret on the LAN (see Security).

### Modules

| Module | Responsibility |
|---|---|
| `store.py` | SQLite schema and additive migrations (`PRAGMA user_version`). Short-lived connections (one per operation, not per thread). `busy_timeout=10000`, WAL, `synchronous=NORMAL`, `foreign_keys=ON`, `BEGIN IMMEDIATE` for writes. Nightly `VACUUM INTO data/backups/gc-<date>.db`, keeping 14. The store is copied before any migration runs. |
| `instruments.py` | Instrument model and `InstrumentContext`: global settings with per-instrument overrides. Token minting and verification. |
| `methods/` | Registry `get(name)`; `d2887.compute(cdf_path, ctx) -> ComputeResult` wraps `distill.compute` with no numeric change. |
| `pipeline.py` | Durable job queue (`jobs` table), one worker thread and a status machine. On start it requeues outstanding jobs. |
| `exports.py` | `export_rows` ledger, per-instrument appends to hub-side CSVs, sidecar safety, `GET /api/agent/results`. |
| `corrections.py` | The corrections provider interface: `file` (2A1) and `lem` (2C). Cache and notifications. The `file` provider **raises** on a missing or unreadable file or a missing `Agilent GC` section, and the sample goes `pending_corrections`; it never returns `{}` silently. From 2A2 it serves only `gc1`, and other instruments stay `pending_corrections` until 2C. |
| `ingest_api.py` | `/api/ingest`, `/api/agent/heartbeat`, `/api/agent/results`, `/api/agent/package`. |
| `admin_auth.py` | Salted admin-password hash, first-use setup, `_check_admin` replacement (2B1). |
| `jobs/import_history.py` | The history importer, run as an admin job inside the hub (2D). |
| `agent/` | The GC-PC agent package (2B2). |
| **deleted in 2A1** | `looker.py`, `run.pyw`, `instance.py`'s per-port seeding and `PER_INSTANCE_KEYS`, and the legacy branches of `paths.py`. `paths.py` keeps deployed mode only. Updated in the same plan: `scripts/package_release.sh` required-file list, `tests/test_release_package.py` import roots, `test_run_pyw.py` and `test_instance.py`, CLAUDE.md, DEPLOY.md. `requirements.txt` drops `pystray`/`Pillow` if the hub no longer imports them. |

### `distill` refactor (2A0)

- Every calibration path takes `conf` explicitly (`_build_calibration`,
  `_assignment_signature`, `_calibration_function`, `calibration_ladder`,
  `distillation_curve_from_cdf`). `_get_settings()` is used only for truly
  global values.
- `GC_CAL_CDF` is **ignored in hub mode**. It would otherwise override every
  instrument.
- `distill.compute(cdf_path, conf, blank_path) -> dict` returns the full
  `CSV_HEADER` row, **plus the uncorrected D86 values and the anchor pairs
  used**, without writing anything.
- The calibration cache key is (calibration path, mtime, assignment
  signature, sensitivity), so two instruments can never share a cached
  function.
- **Golden tests:** `compute` + the phase 1 CSV formatting must equal
  `process_cdf`'s row byte for byte, on synthetic fixtures and on the
  snapshot CDFs when those exist locally.

### Data model (schema v1, created in 2A1; later plans add tables/columns only)

```sql
instruments(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, method TEXT NOT NULL DEFAULT 'D2887',
  enabled INTEGER NOT NULL DEFAULT 1,
  live_since TEXT,                    -- ISO; injections before this are backfill (D11)
  calibration_cdf TEXT, calibration_assignments TEXT,  -- assignments: plain JSON list for this instrument's calibration CDF (re-keyed on migration)
  calibration_sensitivity REAL NOT NULL DEFAULT 50,
  lem_machine_uid TEXT, correction_map TEXT,          -- 2C
  token_hash TEXT, token_issued_at TEXT,              -- 2B1
  export_path TEXT,                                   -- hub-side append-only CSV
  method_map TEXT,                    -- JSON {chemstation_method_name_upper: hub_method}, e.g. {"SIMDISB.M":"D2887","SIMDISTB.M":"D2887"}
  created_at TEXT, updated_at TEXT)

samples(
  id INTEGER PRIMARY KEY,
  instrument_id TEXT NOT NULL REFERENCES instruments(id),
  lab_id TEXT NOT NULL,
  injection_dt TEXT NOT NULL,         -- canonical: naive datetime.isoformat(sep=" "), exactly as the CSV holds it
  injection_dt_source TEXT NOT NULL,
  method_name TEXT,                   -- the CDF's detection_method_name, e.g. 'SIMDISB.M' ('' if absent)  -- 'cdf'|'mtime'  (mtime = the sender's X-GC-Mtime, never the hub receive time)
  cdf_sha256 TEXT UNIQUE,             -- unique across ALL instruments (I18); NULL only for result-only imports
  cdf_path TEXT,                      -- relative to the data dir; NULL only for result-only imports
  legacy_unverified INTEGER NOT NULL DEFAULT 0,  -- imported CSV result with no matching CDF
  source_name TEXT,
  is_blank INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,               -- see "Status machine"
  backfill INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  current_revision INTEGER,
  released_at TEXT, released_by TEXT,  -- explicit admin release of a backfill sample (D11)
  qbench_revision INTEGER, qbench_uploaded_at TEXT,
  received_at TEXT NOT NULL,
  UNIQUE(instrument_id, lab_id, injection_dt))

sample_results(                       -- one row per computation (D10)
  sample_id INTEGER NOT NULL REFERENCES samples(id),
  revision INTEGER NOT NULL,
  results TEXT NOT NULL,              -- JSON keyed by CSV_HEADER names (corrected D86)
  d86_uncorrected TEXT,               -- JSON
  calibration_used TEXT,              -- JSON {cdf, sensitivity, anchors:[[rt,carbon],...]}
  blank_used INTEGER,                 -- samples.id
  corrections_used TEXT,              -- JSON {source:'lem'|'cache'|'file'|'legacy', fetched_at, values:{cut:val}}
  best_fit TEXT, fit_score REAL, flags TEXT,
  reason TEXT NOT NULL,               -- 'processed'|'reprocess'|'import'|'export-lims'|'corrections-released'|'replace'
  by TEXT, processed_at TEXT NOT NULL,
  PRIMARY KEY(sample_id, revision))

conflicts(id INTEGER PRIMARY KEY, instrument_id TEXT, lab_id TEXT, injection_dt TEXT,
  existing_sample_id INTEGER, cdf_sha256 TEXT, cdf_path TEXT, received_at TEXT,
  resolved TEXT, resolved_by TEXT, resolved_at TEXT)   -- resolved: NULL|'kept-existing'|'replaced'

export_rows(seq INTEGER PRIMARY KEY, instrument_id TEXT NOT NULL, sample_id INTEGER NOT NULL,
  revision INTEGER NOT NULL, row TEXT NOT NULL,       -- the exact CSV line, frozen
  hub_appended_at TEXT)               -- NULL = hub-side append still pending

jobs(id INTEGER PRIMARY KEY, kind TEXT, payload TEXT, state TEXT, attempts INTEGER,
  not_before TEXT, last_error TEXT, created_at TEXT)

agents(instrument_id TEXT PRIMARY KEY, version TEXT, state TEXT, queue_size INTEGER,
  rejected_count INTEGER, last_file TEXT, last_error TEXT, host TEXT,
  agent_time TEXT, last_seen TEXT, results_seq INTEGER,
  pending_command TEXT)               -- 'restart'|'pause'|'resume'|'retry-rejected'|'adopt-mirror'

corrections_cache(instrument_id TEXT PRIMARY KEY, values TEXT, methods TEXT, fetched_at TEXT)  -- 2C
standards(id INTEGER PRIMARY KEY, name TEXT, instrument_id TEXT, cdf_path TEXT, added_at TEXT) -- 2A2
sample_cache(sample_id INTEGER PRIMARY KEY, rules_fingerprint TEXT, flags TEXT,
  bestfit_fingerprint TEXT, best_fit TEXT, fit_score REAL)   -- replaces the JSON caches
settings_kv(key TEXT PRIMARY KEY, value TEXT)          -- admin hash, LEM_URL, etc.
-- Global analysis settings stay in data/settings.json. The nightly backup
-- copies it next to the VACUUM INTO snapshot. A rollback across v2.0.0 lands
-- on v1.1.0 in deployed mode, which ignores gc.db. That is safe only because
-- ASAPSV1's v1 install is confirmed not to watch a live folder (pre-check below).
```

**Rollback safety.** The updater rolls back unhealthy releases automatically,
so migrations are **additive only**. Older code must start on a newer
`user_version`: it refuses only if a column it needs is missing, never
because the version is higher.

### Status machine

`received` → (worker) → one of:
- `awaiting_calibration`: the instrument has no usable calibration. It is
  processed automatically when a calibration is saved.
- `pending_corrections`: D2887 is computed, but corrections are unavailable.
  It is retried every 5 minutes.
- `final`: a result revision exists and was computed with good corrections.
- `raw_only`: backfill or an orphan with no result. It is processed **only
  when someone asks**.
- `error`: processing failed. The message is shown and the sample can be
  reprocessed.
- `other_method`: the CDF's method name isn't in the instrument's
  `method_map` (e.g. a D7096 run). Stored, never processed, exported or
  uploaded, and never deleted. Mapping the name later queues these samples.
- `review_method`: the CDF has no method name. Held for an admin to
  classify: map it, or mark it `other_method`.

Rules:
- "Calibration usable" means a calibration CDF **and** at least two usable
  saved assignments. The silent auto-detect fallback is off in hub mode,
  because that fallback caused the 9/16 CS₂ mislabelling.
- Saving a calibration queues only that instrument's `awaiting_calibration`
  samples. It never reprocesses anything else.
- **The export and QBench gate** is `status='final' AND (backfill=0 OR
  released_at IS NOT NULL)`. Releasing a backfill sample writes its
  `export_rows` entry. Reprocessing an unreleased backfill sample writes a
  revision but no export row.
- **`pending_corrections`.** No `sample_results` row is written until
  corrections are resolved; D2887 is recomputed on retry. `current_revision`
  stays as it was (NULL for a new sample). A reprocess of a `final` sample
  that can't get corrections fails the request with a message and leaves
  the sample `final` at its existing revision.
- **Reprocessing a legacy revision** (imported: no correction values,
  `blank_used` NULL) always uses current corrections and the 'at or before'
  blank. The dialog says so, and the new revision records both. A
  result-only sample can't be reprocessed.
- **Every PDF, report number and Export to LIMS** reads the current
  revision and never recomputes. `qbench_revision` records which revision
  was uploaded.
- **One transaction.** `export_rows`, `sample_results` and
  `current_revision` are written together.
- **The QBench upload and Export to LIMS routes enforce this on the server,
  by sample ID.**

### Exports (D8, D9)

- **When rows are written.** A result becomes final (processed, released
  from pending, reprocessed, or Export to LIMS). Then one `export_rows`
  entry is written, holding the exact CSV line frozen in the phase 1
  31-column format, in the order the results became final.
- **Frozen row bytes.** Exactly `csv.writer` output with a `\r\n` terminator,
  as v1 writes it. The `Source File` column holds the hub-relative
  `cdf_path`. Writers (hub and agent) append those bytes verbatim.
- **Hub-side CSV.** `export_path` defaults to
  `<data>/results/<instrument>_results.csv` (`<data>/exports/` already holds
  report exports). Rows are appended in `seq`
  order, and a sidecar `<file>.gchub.json` records the size and sha256
  after each append.
- **Refusal rules.** The hub refuses to append if the file exists without a
  sidecar, or has shrunk or changed since the last recorded state. It then
  raises a notification with two admin choices: **Adopt** (append after the
  current content and record the new state) or **choose a new path**. The
  hub never truncates or rewrites a CSV.
- **Failed appends** (e.g. an Excel lock) leave `hub_appended_at` NULL.
  They are retried every minute, and a notification is raised after 10
  minutes.
- **The agent's mirror.** `GET /api/agent/results?after=<seq>` returns
  that instrument's `export_rows` rows in order. The agent appends them to
  `results_mirror_path` with the same sidecar rules, and reports its
  `results_seq` in heartbeats.
- **Headers.** A new or empty export file gets the header row first.
- **The admin action "Write a fresh export file"** writes every gated
  current revision to a **new** path. It is only used to start a clean
  file. Never point LEM at a fresh file without setting its tail offset to
  the end, or LEM re-reads all of history.

### Method detection

- **Identity comes from the CDF itself:** the global attribute
  `detection_method_name` (seen values: `SIMDISB.M`, `SIMDISTB.M`).
  Names are compared case-insensitively, after trimming, and after
  stripping any directory part.
- **Each instrument's `method_map`** maps method names to a hub method. A
  new instrument gets `{"SIMDISB.M":"D2887","SIMDISTB.M":"D2887"}` by
  default. The 2A1 spike and the 2D dry run confirm the real names.
- **The worker checks the method before processing:**
  - mapped → run that hub method's `compute`;
  - unmapped → `other_method`;
  - missing or empty → `review_method`.
  - A sample is never guessed into D2887 from the shape of its
    chromatogram.
- **The Instruments page lists "methods seen"** (name, count, first and
  last seen, mapped or not). Mapping a name queues its `other_method`
  samples for processing. Unmapping never deletes results; existing
  revisions stay.
- **Blanks come only from D2887-mapped runs,** so a D7096 blank is never
  subtracted from a D2887 sample.
- **The history import applies the same rule:** unmapped history is
  imported as `other_method` with its CSV rows kept as revisions, so
  nothing is lost. The dry run reports a histogram of method names per
  folder, so the maps can be set before the real import.
- **The agent sends every CDF.** Classification happens only on the hub,
  so the agent never needs updating when methods change.

### Processing per instrument

- **`InstrumentContext`** is the global settings with the instrument's
  calibration CDF, assignments and sensitivity overlaid, plus
  `instrument_id`. Every `distill` and `analysis_core` call that needs
  calibration gets this merged `conf`, including the Analysis tab and the
  PDF.
- **Blank.** The genuine-blank rules are kept (exact name
  `^blank[\s_\-]*\d*$`, `is_plausible_blank`). The blank used is **the
  latest genuine blank on the same instrument whose injection time is at or
  before the sample's**, so it doesn't depend on processing order.
  Reprocessing keeps `blank_used` unless the operator asks for the current
  blank.
- **Calibration CDF.** It is picked from the instrument's received samples,
  which is normal since calibration runs arrive through the agent, or from
  an absolute path. `calibration_used` records the anchor pairs actually
  used, so later edits to the assignments don't rewrite history.
- **Standards (D12).** Tagged by instrument and chosen per sample's
  instrument by default. Comparing across instruments shows a warning.
  Best-fit scoring uses the standards from the sample's own instrument.
- **Performance.** `fuel_fit.load_standards` is cached by (standards set,
  mtimes). Flags and best-fit are cached in `sample_cache`. The first
  sample list after migration is built from the store and reads no CDFs.

### Route migration (2A1)

Every route that identifies a sample by **file path** changes to use
**`sample_id`**:
- `/api/trace`
- `/api/distillation-curve`, which uses the revision's `blank_used` and
  `calibration_used`, not "the current blank" or "the last CSV row"
- `/api/metadata`
- the `sample_path` field in `/api/analysis` and in both report exports
- `/api/reprocess`, where a selection by lab-ID range must name an
  instrument
- `/api/export-lims`
- the QBench queue, whose items become `{sample_id}`; the server resolves
  the PDF and refuses anything not `final`
- `/api/files`, which becomes a store query

These are **removed**, since they have no meaning in hub mode: `/api/scan`,
`/api/stop-scan`, `/api/rebuild-db`, `/api/library/reindex-times` and
`/api/files/refresh`.

The UI is updated to match. 2A1's plan lists every route and its fate, and
a test checks the list against the routes the app actually registers.

### Security

- **Admin (D13).** On first use, `/admin/setup` sets the password. It is
  stored as a salted PBKDF2 hash in `settings_kv`. All admin-gated routes
  use it, and the hardcoded `"admin"` is removed.
- **Tokens.** "Download installer" on the Instruments page **mints a new
  token** and, after confirmation, revokes the previous one. It then
  embeds the token in that one download. Only the hash is stored. Update
  packages never contain a token.
- **Ingest.** `MAX_CONTENT_LENGTH` is 25 MB. The token is checked first,
  and an oversize `Content-Length` is refused before the body is read. The
  stored filename is built from the sanitised lab ID plus the sample ID,
  never from the header; `X-GC-Filename` is percent-encoded and informational
  only.
- **Unpacking.** Zip-slip is rejected (absolute paths, drive letters,
  `..`) in the agent's update unpacking and in the installer.
- **Traffic isn't activity.** `/api/ingest` and `/api/agent/*` are added to
  `_NON_ACTIVITY_PATHS`, so agents never count as users for idle
  restarts or `active_sessions`.
- **The phase 1 cross-site guard is unchanged.** Agent requests carry no
  `Origin` header.

### Ingest API (2B1)

- **`POST /api/ingest`**
  - Headers: `Authorization: Bearer`, `X-GC-SHA256`, `X-GC-Mtime` (ISO),
    `X-GC-Filename`. The body is the raw CDF.
  - Validation: token → instrument (enabled); the body's sha256 matches the
    header; the file parses as NetCDF with `ordinate_values` and sample
    metadata.
  - The injection time comes from the CDF. If the CDF has none, it falls
    back to `X-GC-Mtime` (recorded as `injection_dt_source='mtime'`).
  - The file is stored at `data/cdf/<inst>/<YYYY>/<MM>/<lab>_<sampleid>.CDF`
    (temp file, then replace), a job is queued, and the route returns
    `201 {sample_id, sha256, status}`.
  - `backfill = 1` when the injection is before `live_since`, or when
    `live_since` is unset.
- **Duplicates and conflicts**
  - The same sha256 on the same instrument returns `200 {sample_id,
    sha256, duplicate:true}`.
  - The same sha256 on **another** instrument returns `409 {error:
    "already received from GC-1"}`. The agent treats this as rejected, and
    it shows on both instruments' pages.
  - The same (instrument, lab ID, injection time) with a different sha256
    returns `202 {sha256, conflict_id}`. The file is stored in `conflicts`,
    a notification is raised, and the admin conflict screen can **Replace**
    (the stored file becomes the sample's CDF and is reprocessed as a new
    revision) or **Keep existing**.
- **Error responses:** 400 (bad CDF or sha), 401 (bad token), 403
  (instrument disabled), 413 (too large), 415 (not a CDF). Never a 5xx on
  bad input.
- **`POST /api/agent/heartbeat`** takes `{version, state, queue_size,
  rejected_count, last_file, last_error, host, agent_time, results_seq}`
  and returns `{command, agent_package_sha256, server_time}`. A mismatched
  sha256 makes the agent check for an update straight away.
- **`GET /api/agent/results?after=<seq>&limit=500`** returns
  `{rows:[{seq, line}], header}` for that token's instrument.
- **`GET /api/agent/package`** returns `{version, sha256}`, and
  **`GET /api/agent/package.zip`** returns the bytes. Both use token auth.
  The agent version is the hub's `VERSION`.

### Agent (2B2)

- **Location:** `%LOCALAPPDATA%\ASAPLabs\gc-agent\` containing
  `launcher.pyw`, `versions\<v>\`, `current.txt`, `agent.json`, `ledger.db`
  (stdlib sqlite3) and `agent.log` (rotating).
- **Launcher.** A resident supervisor, never self-updated.
  - It holds a single-instance named mutex (via `ctypes`).
  - It runs `versions\<current>\agent_main.py` as a child. Exit code 0
    means quit; 3 means restart, which re-reads `current.txt`; anything
    else is a crash, restarted with backoff.
  - Three crashes within 30 s of starting a **newly switched** version
    make it revert `current.txt` to the previous version and report that.
- **Autostart.** Registered at `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
  through `winreg`, pointing at the recorded full path of `pythonw.exe`
  (no elevation needed). Running only while a user is logged on is
  acceptable, because ChemStation needs a logged-on session to acquire.
- **Configuration** (`agent.json`): `hub_url`, `token`, `watch_dir`,
  `include_subdirs` (true), `poll_seconds` (5), `stable_seconds` (30),
  `results_mirror_path` ("" disables it), and `paused`, which persists
  across reboots.
- **Send loop**
  - Scan for `*.CDF` (case-insensitive). The ledger key is (path, size,
    mtime); a file is hashed only when it's new or has changed.
  - A file is ready once it's unchanged for `stable_seconds` **and**, on
    Windows, can be opened for exclusive access.
  - Send oldest first. On 200/201/202 with a matching sha256, mark the file
    sent.
  - 401, 403, 404, 405 and any unlisted 4xx: stop sending, turn the tray
    red, and retry every 5 minutes or as soon as the config changes. Files
    stay queued.
  - 400, 409, 413, 415: mark the file rejected with the reason. It shows as
    "N rejected" in the tray and on the Instruments page. The
    `retry-rejected` command and a tray menu item requeue them.
  - 429, 5xx or a network error: back off from 5 s up to 300 s.
  - Files on the GC PC are never modified or deleted.
- **Results mirror.** After each heartbeat, pull `results?after=<ledger
  results_seq>`, append the rows under the sidecar rules, then advance
  `results_seq`. If the mirror file was changed by someone else, stop
  mirroring and report the error; never rewrite the file.
- **Heartbeat** runs every 30 s and on state changes. It reports
  `agent_time`, and the Instruments page shows clock skew over 2 minutes.
- **Self-update**
  - Checked at start, hourly, and when the heartbeat reports a different
    **agent package sha256** (compared, not the hub version, so a hub PATCH
    doesn't restart every agent).
  - Steps: download, verify the sha256, safe-unzip to `versions\<v>\`, then
    a smoke test (`python.exe`, found next to the recorded `pythonw.exe`,
    `-c "import agent_main"` with cwd `versions\<v>` and a 30 s timeout).
  - Only then is `current.txt` switched atomically and the agent exits
    with code 3.
  - It keeps three versions.
- **Tray.** Green is healthy, amber means files are queued and the hub is
  unreachable, red is an auth or config error, grey is paused.
  - Menu: status lines (version, queued, rejected, last sent, mirror seq),
    Open hub, Open log, Settings… (tkinter), Pause/Resume, Retry rejected,
    Restart, Quit.
- **Installer.** `install.pyw`, double-clicked so it runs under the same
  interpreter the PC's `.pyw` association uses (which already has
  `pystray` and `Pillow`, because `run.pyw` uses them).
  - It checks that `pystray` and `PIL` import. If they don't, it says so
    and stops; there is no pip.
  - It records `sys.executable`, copies the files and writes `agent.json`
    with the hub URL and token.
  - It asks for `watch_dir` and `results_mirror_path` with pickers. Both
    default to the values in that PC's existing `~/.gc_viewer_settings.json`
    (`watch_dir`, `distill_output`) when that file exists.
  - **Adopting the mirror file.** For an existing mirror file it shows the
    size and last row and checks that the header equals `CSV_HEADER`. On
    confirmation it writes the sidecar. An `adopt-mirror` command exists
    for a mirror refused later.
  - It then registers autostart and starts the agent.
- **Compatibility.** The agent needs Python 3.9 or newer (the installer
  refuses older versions). CI runs the agent tests on 3.9 and 3.14 on Linux
  and on Windows.

### LEM corrections (2C)

- **Rules (matching LEM's own):** a 200 JSON response is authoritative.
  The machine counts as **known** if `methods` is non-empty. For each
  mapped test name:
  - listed in `corrections`: use its value;
  - in `methods` but not in `corrections`: record **0.0 explicitly**
    (LEM's rule is that a missing correction is zero);
  - in neither: a configuration error. The sample goes
    `pending_corrections` and the Instruments page shows red.
- **Units** must be `°C`, `C` or empty. Anything else is a configuration
  error.
- **Unreachable.** A non-JSON 200 (e.g. a sign-in page), a timeout, a
  connection error or a 5xx all count as unreachable.
- **Default map** covers all **11 cuts** from today's
  `_D86_CORRECTION_TEST_MAP`: IBP, 5, 10, 20, 30, 50, 70, 80, 90, 95 and
  FBP, using the current LEM test-name strings. It is editable on the
  Instruments page.
- **`LEM_URL`** is a global setting defaulting to LEM's local address on
  ASAPSV1, to be verified at deploy. The fetch timeout is 5 s.
- **Cache.** One entry per instrument, refreshed at most every 5 minutes or
  when someone presses "Refresh from LEM".
  - If LEM is unreachable and the cache is **≤ 24 h** old, use it and
    record `source:"cache"`. Otherwise the sample goes
    `pending_corrections`.
  - When a fresh fetch **differs** from the values a sample used, those
    samples are flagged "corrections changed since processing" and a
    notification is raised.
  - The rule is: **corrections are those in force at processing time.**
    Sample detail and the analysis report show the source; the customer
    PDF doesn't.
- **LEM repo change** (its own branch, tagged only with Ryan's say-so, per
  ASK-CLAUDE.md): a per-machine flag `corrections_applied_upstream`,
  honoured in `apply_row_corrections`, with tests. The cutover runbook
  turns it on **at the moment** the station's source switches to the
  hub-fed mirror.
- **VERIFY now (read-only):** whether Agilent GC 1 has non-zero
  `lem_correction_factors`. If it does, today's results are already
  corrected twice. Report that immediately; don't wait for cutover.

### History import (2D; its spike is part of 2A1)

- **Runs inside the hub.** It is an admin job (`/admin/import`) with SSE
  progress, pause/resume, and batched commits (500). The hub is the only
  writer.
- **Before importing, the runbook:**
  - robocopies the legacy processed folder and the results CSV to local disk
    on ASAPSV1;
  - freezes `processed_cdfs2`, which sits inside the webapp folder on the
    share and would be overwritten by a v1.x share update.
- **Key.** The sample key comes from each **CDF's own metadata** (sample
  name, injection time). Filenames aren't trusted, since two naming
  conventions exist.
- **CSV rows are imported as revisions.**
  - Rows are grouped by (lab ID, CSV `InjectionDateTime` exactly as written)
    and attached to a CDF only when that CDF's metadata matches (lab ID
    equal and time equal). Prefix-matched `Source File` values are not
    trusted.
  - Unmatched rows are imported as **result-only** samples: no CDF
    (`cdf_path` and `cdf_sha256` NULL), `status='final'`, `backfill=1`,
    `legacy_unverified=1`. They are searchable and viewable as numbers, and
    can't be reprocessed or traced.
  - Duplicate rows for the same sample become revisions in CSV order
    (`reason:'import'`), and the last one is current.
- **Numbers are copied verbatim.** Historical values are never recomputed,
  and `corrections_used = {source:'legacy'}`.
- **Orphan CDFs** (no CSV row) become `raw_only` with `backfill = 1`.
- **Every imported sample** is `backfill = 1` and is never exported
  automatically (D11).
- **Across instruments.** A sha256 already on another instrument is reported,
  not imported twice. A row whose CDF isn't in that instrument's folder is
  reported, not attached.
- **Re-runs and summary.** Re-runs are no-ops. The job prints a summary:
  imported, revisions, attached, result-only, orphans, cross-instrument,
  conflicts, errors.
- **Spike.** Early in 2A1, a read-only dry-run of the matcher runs against
  the real share (or the snapshot, while off-network). It measures match
  rates, duplicates and CDFs with no injection time (v1 used the file mtime,
  and a processed copy's mtime may differ from the original's), and checks
  whether the 5570 instance's CSV is mixed, before the schema is frozen.

### Notifications (using the existing notification store)

Raised for:
- an agent not seen for 15 minutes;
- LEM unreachable while samples are waiting on it;
- corrections changed compared with values samples used;
- a conflict received;
- an export or mirror append failing for more than 10 minutes, or refused
  (sidecar mismatch);
- samples waiting for calibration (once per instrument per hour);
- files rejected;
- the nightly backup failing.

### Cutover runbook (DEPLOY.md, 2B2)

1. Confirm ASAPSV1 runs v2.x and port 5560 is open inbound in the Windows
   firewall.
2. Set the admin password. Create GC-1 and GC-2 on the Instruments page.
3. Set each instrument's calibration and LEM mapping. Confirm the LEM
   corrections read green.
4. Copy the legacy folders locally, then run the import for both
   instruments. Review the summary.
5. **Per GC PC, in this exact order**, so that no rows flow between steps:
   1. quit `run.pyw` and remove its autostart;
   2. re-run the import for that instrument (it picks up only the delta;
      otherwise a no-op);
   3. set that instrument's `live_since` to now;
   4. in LEM, set `corrections_applied_upstream` for that machine;
   5. run `install.pyw` from the Instruments page download, adopt the
      mirror file (that PC's old `distill_output`), and start the agent.

   Files v1 already processed dedupe by sha256 against the imported copy.
   Anything else before `live_since` is backfill and is listed for review
   on the Instruments page.
6. Watch the first sync and the mirror's first appends. Reset LEM's tail
   offset only if the mirror path differs from what the station tails.

## Testing

- **Unit tests:**
  - store: migrations are additive, and older code starts on a newer
    `user_version`;
  - dedupe and conflicts;
  - the revisions, export ordering and sidecar rules;
  - the status machine;
  - the per-instrument blank ("at or before");
  - the calibration-usable rule;
  - corrections rules, covering the 11 cuts, explicit zero, unknown
    machine, units, non-JSON 200, the cache age and change detection;
  - the admin hash;
  - token mint and revoke;
  - the import matcher.
- **Golden:** `distill.compute` equals phase 1 byte for byte (2A0).
- **Fixtures:** synthetic CDFs from `netCDF4`: solvent, alkane ladder,
  sample, blank, a truncated CDF, and a CDF with no injection time. Snapshot
  CDFs add extra golden cases when present.
- **Boot tests (subprocess):**
  - every migrated route by `sample_id`, plus the route-fate table;
  - ingest (valid, duplicate, cross-instrument, conflict, too large, bad
    token);
  - the results pull;
  - a fake LEM server;
  - admin setup.
- **End to end:** a hub subprocess plus a real agent subprocess
  (`--no-tray`). A synthetic CDF dropped into the watch folder becomes a
  `final` sample, and its row appears in the agent's mirror CSV.
- **Agent:** the launcher's exit codes and revert, the mutex, the ledger,
  the stability check, the 401 hold vs 400 reject, backoff, update
  verify/smoke/switch, zip-slip, and the installer run as a dry run.
- **CI:** existing ubuntu 3.12/3.14 plus Windows 3.14. The agent suite
  also runs on 3.9 on both.

## Open items (must be verified on the network)

- The real LEM test names on the GC benches; whether GC-2 exists in LEM;
  `LEM_URL`; whether Agilent GC 1 has non-zero corrections today
  (double-correction check).
- Where each GC's LEM station module runs, and which file it tails. D9
  assumes it is the GC PC's `distill_output`.
- The GC PCs' Python version and its `.pyw` association.
- Whether the 5570 instance's CSV is mixed with other data (answered by the
  2A1 spike).
- ChemStation's CDF output folder on each PC (the installer asks for it).
- Whether each PC's `distill_output` is local or on UNC (D9 assumes the
  station tails a file the agent can append to).
- Whether the port 5570 (GC-2) instance applies the `Agilent GC` factors
  today (`correction_factors_json` isn't per-instance); the JSON file's
  modification time; whether its values match LEM's for Agilent GC 1.
