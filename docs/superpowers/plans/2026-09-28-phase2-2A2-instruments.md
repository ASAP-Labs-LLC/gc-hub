# Phase 2, plan 2A2: the Instruments page and per-instrument configuration

Spec: `docs/superpowers/specs/2026-09-28-phase2-multi-instrument-hub-design.md`
(Delivery row 2A2, Processing per instrument, Corrections (2C), D11, D12,
D13, Method detection, Status machine, Security, Notifications). Carry-overs:
plan 2A1 ("Integration-critic carry-overs", 2C: `StoreProvider` read_fn with
an explicit db) and plan 2B1 ("Deferred to 2A2": `live_since` editing, the
backfill release screen, the conflicts screen, the agent panel; **M5**: agent
strings and instrument names are rendered with `textContent`, never
`innerHTML`).

Branch `a2/instruments` off `feat/phase2`. Runs in parallel with 2A1 T4
(`app.py`, `static/js/app.js`, `templates/index.html`) and T5 (start-up
ownership), so:

- all new code is in **new files**: `instrument_admin.py` (the operations,
  tested in-process against a temp store), `standards.py` (D12),
  `instruments_api.py` (a Flask Blueprint: the page and its JSON routes),
  `templates/instruments.html`, `static/js/instruments.js` (DOM),
  `static/js/instruments_logic.js` (pure logic, node-tested),
  `static/css/instruments.css`;
- `app.py` changes by two lines (import + `register_blueprint`);
  `templates/index.html` by one nav button;
- `instruments.py` gains the corrections-provider factory and `startup`
  uses it (small, self-contained; T5 keeps calling `startup`);
- `templates/calibration.html` gets an instrument selector and uses the
  instrument-scoped routes (T4 leaves this file alone);
- `tests/js/run.js` gains one entry.

Strict TDD: every task starts with a failing test (red), then the code
(green). Tests never `import app`; routes are tested in a booted subprocess
(`tests/bootapp.py`, `setup_admin`), operations in-process.

## Decisions (where the spec leaves a choice)

- **Routes.** Reads are `GET /api/instruments...` (open, like `/api/agents`);
  every state change is `POST /api/admin/...` with `{password, ...}`,
  `Content-Type: application/json`, a 64 KiB body cap and the phase-1
  cross-site guard (`ingest_api._admin_body`, reused). `by` is
  `admin@<remote address>` (there are no user accounts).
- **No secrets out.** Instrument rows are served without `token_hash`
  (`token_issued_at` only, as "has a token").
- **Instrument ids** match `^[a-z][a-z0-9_-]{0,31}$` and never change. A new
  instrument gets the default `method_map`, method `D2887`, `enabled=1` and
  **no** `live_since` (so everything it sends is backfill until an admin sets
  it, D11).
- **`live_since`** is naive local (`YYYY-MM-DD HH:MM[:SS]`, `T` accepted,
  offsets refused), empty clears it. The response carries a `warning` when the
  agent's clock skew is over 120 s (or unknown because it never reported), and
  always notes that it applies to samples received from now on (the backfill
  flag is decided at submit).
- **Export path** goes through `exports.HubExporter.new_path` (absolute
  paths only) and `adopt`; `ExportRefused` → 409 `{error, reason, detail}`.
  The Blueprint uses the exporter T5 registers (`instruments_api.set_exporter`)
  and otherwise its own `HubExporter(db)`.
- **Calibration.** The CDF is picked from the instrument's own received
  samples (`sample_id` → its data-relative `cdf_path`) or an absolute path to
  an existing file. Changing the CDF clears the assignments (they belong to
  one file). Saving assignments reuses the Calibration page's validation
  (carbons increase with retention time) and, in the same transaction,
  `pipeline.on_calibration_saved(instrument)`. `calibration_problem` is shown
  as "Calibration usable" / the reason. `/calibration?instrument=<id>`
  (default `gc1`) edits any instrument.
- **Corrections (D4b/2C).** `validate_values` (all 11, finite, ±50 °C);
  reason required; `store.corrections.set_all` and
  `enqueue_for_status(inst, 'pending_corrections')` in **one** `write_txn`.
  Seeding: `POST /api/admin/instruments/gc1/corrections/seed` reads
  `correction_factors_json` with `corrections.seed_from_file`, refuses (409)
  once gc1 has hub corrections, audit reason
  `seeded from correction_factors.json`.
- **Provider wiring.** `instruments.corrections_provider(db)` returns the
  Worker's `(conf) -> provider` factory: `StoreProvider` (read with the
  explicit db, 2A1 carry-over) for any instrument with saved corrections;
  for `gc1` with none, the phase-1 `FileProvider` (the interim source until
  seeded); every other instrument raises `CorrectionsUnavailable` →
  `pending_corrections`. `startup` passes it unless the caller gives one.
- **Methods seen.** `store.samples.methods_seen` plus `mapped_to`. Map/unmap
  rewrites `method_map` in one transaction; mapping queues that name's
  `other_method` samples (`pipeline.on_method_mapped`, same transaction).
  Unmapping never touches results. The empty name (`review_method`) can't be
  mapped: "Mark as other method" sets those samples `other_method`.
- **Backfill release.** List backfill samples (filters: `q`, `status`,
  `released`), release selected `final` ones one by one via
  `pipeline.release_backfill`; each id reports `{ok, seq}` or `{error}`.
- **Conflicts.** List unresolved (or all) with both files' identity (held:
  lab ID, injection time, sha256, stored name, received; existing sample: id,
  sha256, stored name, source name, status, revision), `error`, and whether a
  Replace is pending. Keep existing → `store.conflicts.resolve`; Replace →
  `pipeline.resolve_conflict_replace` (queues; the worker resolves).
- **Standards (D12).** `standards.py` keeps the `standards` table in step
  with the comparison-standards folder: the first time it is used with `gc1`
  present, every existing file is registered as `gc1` (flag
  `settings_kv["standards_migrated"]`); a file added later through the old
  routes is registered untagged. `for_sample(instrument)` orders the sample's
  instrument's standards first and marks the others `cross_instrument` with a
  warning text. The app.js picker, the PDF warning and per-instrument
  best-fit are T5's (carry-over): the API and data are here.

## Tasks

### T1 — provider factory (`instruments.corrections_provider`)
- Red `tests/instruments_admin/test_provider.py`: gc1 without store rows →
  `source 'file'` values from the JSON; gc1 with store rows → `'hub'`; gc2
  without → `CorrectionsUnavailable`; gc2 with → `'hub'`; reads use the given
  db; `startup` wires it (a Worker processing gc2 with hub corrections goes
  `final`, without → `pending_corrections`).
- Green: `instruments.corrections_provider`, `startup` default.

### T2 — instrument CRUD operations (`instrument_admin`)
- Red: create (id rule, duplicate, defaults, no `live_since`), update (name,
  enabled, method must be registered, `live_since` normalised / cleared /
  offset refused, `lem_machine_uid`), unknown fields refused, `public_row`
  drops `token_hash`, skew warning (>120 s, never reported).

### T3 — export path and adopt
- Red: `set_export_path` refuses relative, surfaces `in-use` with reason;
  `adopt_export` surfaces `missing`/`no-sidecar` adoption; status.

### T4 — calibration per instrument
- Red: candidates are only this instrument's samples; pick by sample sets
  the data-relative path and clears assignments; absolute path must exist;
  another instrument's sample refused; save assignments validates, stores the
  list + sensitivity, queues only this instrument's `awaiting_calibration`;
  status `usable`/`problem`.

### T5 — corrections editor and seeding
- Red: invalid values/missing reason refused and nothing written; valid →
  table + audit + only this instrument's `pending_corrections` queued, in one
  transaction; seed gc1 from file (audit reason), refused on non-gc1 and
  once seeded; missing file → error.

### T6 — methods seen, review_method
- Red: aggregate + mapped flag; map queues `other_method` of that name only;
  unmap keeps results; empty name refused; mark review → other_method.

### T7 — backfill release and conflicts
- Red: list filters; release ok/errors per id (not final, not backfill,
  already released); conflicts identity; keep; replace queues; missing id.

### T8 — standards (D12)
- Red: first use migrates folder files to gc1 once; later files untagged;
  retag; `for_sample` ordering and warnings; missing file flagged.

### T9 — the Blueprint (booted)
- Red `tests/instruments_admin/test_instruments_boot.py`: every admin route
  403 without the password, 415 non-JSON, 413 over 64 KiB, cross-site 403;
  the page renders; happy paths for create/update/live_since warning/export
  path/calibration/corrections/seed/methods/backfill/conflicts/standards;
  `token_hash` never in any response.
- Green: `instruments_api.py`, two lines in `app.py`.

### T10 — the page
- Red `tests/js/instruments.test.js`: skew text/warning, agent health,
  correction-input parsing (mirrors `validate_values`), `live_since` from a
  `datetime-local` value, installer outcome (download / confirm /
  needs_hub_url / error), method rows, standards ordering + warning, release
  summary.
- Green: `instruments_logic.js`; then `instruments.html` + `instruments.js`
  (DOM built with `textContent` only; a test greps the page script for
  `innerHTML` with interpolated data) + css; the nav button; the calibration
  page's instrument selector.

### T11 — docs, full suite
- CLAUDE.md (Instruments page, routes, provider), this plan's carry-overs;
  `pytest tests`, `node tests/js/run.js`.

## Carry-overs (for T5 and later)

- **T5, start-up:** call `instruments_api.set_exporter(exporter)` with the
  running `HubExporter`, so an Adopt/new path on the page clears the refusal
  that exporter holds in memory at once (until then the page uses its own
  exporter on the same store, and the running one clears at its next verified
  flush). `instruments.startup` already passes
  `corrections_provider(db)`; keep calling it.
- **T5, D12 in app.js/PDF:** the comparison picker should call
  `GET /api/standards?for_instrument=<sample's instrument>` (own instrument's
  standards first; others carry `cross_instrument` and `warning`) and show
  the warning; the analysis PDF prints
  `standards.cross_instrument_warning(...)`; best-fit scores against
  `standards.paths_for(comp_dir, sample's instrument)` (needs a paths-based
  variant of `fuel_fit.load_standards`). The old
  `/api/comparison-standard(s)` add/delete/rename routes still work on the
  folder only: an added file shows up untagged, and a rename shows the old
  row as missing plus a new untagged one. Move them onto the table.
- **T4/T5, `/api/calibration`:** the Calibration page now uses the
  instrument-scoped routes (`?instrument=`, default gc1; saving needs the admin
  password). T4's gc1 `POST /api/calibration` is not admin-gated; gate it or
  remove it (nothing in the page uses it any more). Settings' "Calibration
  CDF" field still mirrors into gc1 (T4); the Instruments page is the place
  for every instrument.
- **Notifications** (spec): "agent not seen for 15 minutes", "samples
  waiting for corrections", "samples waiting for calibration (once per
  instrument per hour)" are not raised yet; the page shows the stale badge and
  the counts. A background check belongs with T5's start-up owner.
- **Main UI:** the instrument filter/search in the sample list and the
  instrument in reports (Delivery row 2A2) are app.js/report work (T4/T5).
- **review_method:** only "mark as other method" exists. Processing a
  nameless CDF as D2887 anyway needs a per-sample override in the worker
  (it re-checks the method name every time).
- **raw_only backfill** (2D's orphans) is listed but can't be released until
  processed; add a "Process" action (`pipeline.request_reprocess`) with 2D.
- **2C leftovers:** "corrections changed since processing" on sample detail
  (`corrections.values_differ`); whether gc1 is seeded automatically at first
  start (spec) or only by the admin button (built here; until then gc1 keeps
  reading the phase-1 file, as in 2A1).
- `instrument_admin.conflicts_list` uses `pipeline._replace_job`; make it
  public if another caller appears.
- The page's GETs are open on the LAN, like `/api/agents` (correction values,
  audit, conflict hashes and file names). Only changes are gated. The
  calibration GET (peak detection) runs one at a time, 429 when busy.

## Critic review 1 (2026-09-28): fixes, each a test first

- **C1** The 24 Blueprint routes are in T4's route-fate table as `new`.
- **I1** Clearing `live_since`, moving it later or into the future returns a
  warning (exports stop or are held back) and the page asks for confirmation
  (`liveSinceConfirm`). `live_since` must be `YYYY-MM-DD HH:MM[:SS]` (space
  or `T`).
- **I2** `standards_migrated` is set only once `sync` has seen the folder with
  at least one CDF; an unreachable or empty folder at first use migrates later.
- Minors: a calibration CDF is parsed before it is accepted (assignments kept
  on refusal); stale answers after switching instrument are dropped; the
  methods card offers every hub method; the hub-URL box is found by id; the
  open GETs are documented in `instruments_api`.
- Left to T5 (coordinator): C2 (ungated legacy `POST /api/calibration` and the
  settings calibration/correction keys), I3, I4.
