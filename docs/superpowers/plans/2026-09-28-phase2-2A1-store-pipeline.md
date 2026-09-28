# Phase 2 / 2A1: Store, pipeline, append-only exports, route migration (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Tasks marked **∥** run in parallel worktrees once their dependency is merged. Steps use checkbox syntax.

**Goal:** the hub becomes store-driven, with one implicit instrument `gc1`.
CDFs enter through `pipeline.submit()`. The worker classifies each by
method, computes the result with `distill.compute`, and writes a revision,
the `export_rows` entry and `current_revision` in one transaction. Exports
are append-only. The UI addresses samples by `sample_id`. Legacy share mode
is removed.

**Spec:** `docs/superpowers/specs/2026-09-28-phase2-multi-instrument-hub-design.md`
(rev 3 plus the "Injection time" and "Method detection" additions).
**Contracts:** `docs/superpowers/specs/2026-09-28-phase2-contracts.md`
(§2 corrections, §3 import matcher).

**Prerequisites** (merged into `feat/phase2` before Task 2 starts):
- 2A0, approved.
- Lane C's `corrections.py`, for `FileProvider`.
- Lane D's `import_match.py`, used for the folder loader and the parity
  report.

## Conventions

- Branch `feat/phase2`. Parallel tasks use worktree branches
  `a1/<task>`, merged back by the controller.
- Run tests with `.venv/bin/python -m pytest tests/ -q` and
  `node tests/js/run.js`.
- `distill.py`/`looker.py` are CRLF; preserve it.
- TDD: red first, and report the red output.
- Never `import app` in new tests. Route tests boot the app through
  `tests/bootapp.py`.
- Commits end with the Co-Authored-By line.

## Task graph

```
T1 store ──► T2 pipeline+parser+methods ─┬─► T4 routes+UI ──► T5 legacy removal+packaging+docs
         └─► T3 exports ∥ ───────────────┘        │
                                        T6 folder loader + parity ∥ (after T2, T3; own module + CLI; route wired in T5)
```

---

### T1: `store.py`, schema v1, migrations, backups (sequential; everything depends on it)

**Files:** `store.py`, `tests/store/test_store.py`.

- **API.** Short-lived connections, and every function takes `db_path` or
  uses `paths.data_dir()/gc.db`:
  - `open_db(path) -> sqlite3.Connection`. Sets WAL,
    `busy_timeout=10000`, `synchronous=NORMAL`, `foreign_keys=ON`, and
    `row_factory=sqlite3.Row`.
  - `migrate(path)`. Additive only, driven by `PRAGMA user_version`. The
    db file is copied to `data/backups/pre-migrate-<v>-<ts>.db` before
    migrating. If `user_version` is **higher** than known, it logs and
    carries on, never refusing.
  - `write_txn(conn)`, a context manager for `BEGIN IMMEDIATE`.
  - Typed helpers:
    - `instruments`: get, list, upsert
    - `samples`: `insert_received`, `get`, `find_by_sha`,
      `find_by_key`, `set_status`, `search(q, instrument, date_from,
      date_to, status, limit, offset)`
    - `add_revision(conn, sample_id, results…, reason, by)`. Returns the
      revision and bumps `current_revision`.
    - `export_rows`: `append_pending(conn, instrument, sample_id,
      revision, line)`, `pending_hub_appends(instrument)`,
      `mark_hub_appended(seq)`, `rows_after(instrument, seq, limit)`
    - `jobs`: `enqueue(kind, payload, not_before=None)`,
      `claim_next(now)`, `complete`, `fail(retry_at)`,
      `requeue_stale_running()`
    - `sample_cache` get/put
    - `settings_kv` get/set
    - `conflicts` add/list/resolve
  - `backup_nightly(path, keep=14)`: `VACUUM INTO`, plus a copy of
    `settings.json`.
- **Schema:** exactly the spec's data model, including `method_name`,
  `legacy_injection_dt`, `time_corrected`, `released_*`, `qbench_*`,
  `method_map`, `legacy_unverified` and the conflict audit columns.
- **Tests:**
  - schema created, re-running `migrate` is a no-op, and a
    newer `user_version` is tolerated;
  - the pre-migrate backup is written;
  - the unique keys (sha256 across instruments; instrument, lab ID and
    injection time);
  - `add_revision` increments and sets current;
  - `claim_next` is atomic across 2 threads (no double claim);
  - `requeue_stale_running`;
  - `VACUUM INTO` produces an openable copy, and pruning keeps 14;
  - short-lived connections leak no handles (open and close 1,000 times).

### T2: Pipeline, parser fix, method detection, file corrections (after T1; sequential)

**Files:** `pipeline.py`, `methods/__init__.py`, `methods/d2887.py`,
`instruments.py` (a minimal `gc1` context), `distill.py` (parser fix
only), `tests/pipeline/`.

- **Parser fix (MAJOR).** In `distill.parse_injection_datetime`, the ANDI
  compact regex `^(\d{14})(?:\s*[+-]\d{2}:?\d{2})?$` runs **first**. ISO
  is attempted only if the text contains `-` or `/` date separators. The
  other formats stay in order.
  - A golden table test covers every stamp form: compact with and
    without a zone, ISO with `T` and with a space, `DD-Mon-YYYY`, US,
    garbage. It includes `20260925002450+0000` → 00:24:50.
  - Add `distill.v1_parse_injection_datetime(raw)`, a frozen copy of
    v1's order so the legacy string can be computed. Its test proves it
    reproduces 02:45:00 on Python ≥ 3.11.
  - Update `distill.cdf_metadata` to use the fixed parser and return
    `(sample, dt, dt_source, method_name)` through a **new** function
    `cdf_identity(path)`. The old signature is kept.
- **`methods` registry.** `get("D2887")` returns an object with
  `compute(cdf_path, ctx) -> dict`, delegating to `distill.compute` with
  `corrections` passed explicitly. It also has `normalise_method_name()`:
  trim, basename, upper-case.
- **`instruments.context(instrument_row, global_conf) -> dict`.** The merged
  conf, including `calibration_cdf`, `calibration_assignments` and
  `calibration_sensitivity`. `GC_CAL_CDF` is ignored.
  - "Calibration usable" means the CDF exists and there are at least
    2 usable assignment pairs.
  - In 2A1 the `gc1` row is created on first start from `settings.json`
    (calibration CDF and assignments copied) with the default
    `method_map`. This is the runbook replacement for auto-migration.
- **`pipeline`:**
  - `submit(instrument_id, cdf_bytes|path, mtime, source_name) -> SubmitResult`.
    It computes the sha, dedupes (same instrument → duplicate, other
    instrument → cross-instrument), finds conflicts on the (lab ID,
    injection time) key, stores the file at
    `data/cdf/<inst>/<YYYY>/<MM>/<lab>_<id>.CDF` (temp file, then
    replace), inserts `received`, and enqueues `process`.
  - **Store review notes for T2:**
    - `is_blank` is decided **in `submit`**, from the CDF bytes (name
      rule plus `is_plausible_blank`), so blank choice never depends on
      processing order.
    - Pass `db=conn` everywhere inside `write_txn`. Opening a new
      connection there raises.
    - Do the corrections read and `compute` **before** opening the
      transaction.
    - The `gc1` bootstrap must set `live_since`. `is_backfill(None, …)`
      is True, so without it nothing would ever export.
    - Reprocess requests enqueue with their own payload (last intent
      wins on dedupe).
  - `Worker` is one thread. It claims jobs and handles them with this
    status machine:
    1. method unmapped → `other_method`; missing → `review_method`;
    2. calibration unusable → `awaiting_calibration`;
    3. **blank:** the latest genuine blank on the same instrument whose
       `injection_dt` is at or before the sample's, and whose method maps
       to D2887. Genuine means `is_blank` (the case-insensitive name set
       `blank`, `(blank)`, `[b] blank`, `blank2`…, plus
       `is_plausible_blank`).
    4. corrections from `corrections.FileProvider` (`gc1` only);
       `CorrectionsUnavailable` → `pending_corrections` with a retry job
       in 5 minutes;
    5. otherwise `compute`, then **in one `write_txn`**: `add_revision`,
       `export_rows.append_pending` (only if the gate passes:
       `backfill=0` or released), and `status='final'`;
    6. an exception → `error` with the message.
  - On start: `requeue_stale_running()`, and requeue `received` and
    `pending_corrections`.
  - After a calibration save:
    `enqueue_for_status(instrument, 'awaiting_calibration')`.
  - Mapping a method: `enqueue_for_status(instrument, 'other_method',
    method_name=…)`.
- **Tests** (synthetic CDFs from `tests/cdf_fixtures.py`, whose
  `write_cdf` must gain a `method_name` parameter):
  - every status branch;
  - blank at-or-before, including an out-of-order arrival and a blank
    from another method being ignored;
  - revisions on reprocess;
  - the gate with backfill;
  - the transaction is atomic: an exception injected after
    `add_revision` leaves no revision, no export row, and the status
    unchanged;
  - restart requeue;
  - duplicate, cross-instrument and conflict;
  - parity of results with 2A0's golden rows when given the same conf
    and corrections (the numbers must not change except via the parser
    fix, which the fixtures don't hit).

### T3 ∥: `exports.py`, append-only CSV (after T1; parallel with T2)

**Files:** `exports.py`, `tests/exports/`.

- `format_line(results_json, source_file) -> str`: `csv.writer` output
  with a `\r\n` terminator, in the 31-column `CSV_HEADER` order. It must
  be **byte-identical** to v1's append for the same values; a golden test
  compares it with v1 `_append_csv_row` output.
- `HubExporter.flush(instrument)` appends pending `export_rows` in `seq`
  order to `export_path` (default `<data>/results/<inst>_results.csv`).
  It writes the header if the file is new, checks and updates the
  `<file>.gchub.json` sidecar (`{size, sha256, seq}`), and marks rows
  appended.
  - **It refuses** (raising `ExportRefused`, then notification plus
    admin choices) when the file exists without a sidecar, or its size or
    sha differs from the sidecar.
  - `adopt(instrument)` checks that the header equals `CSV_HEADER`, then
    writes the sidecar at the current size.
  - `new_path(instrument, path)` switches the export to a new file.
- `write_fresh(instrument, path)` is the admin action. It refuses if
  `path` exists.
- **Retry.** A background job flushes every 60 s. If rows have been
  pending for more than 10 minutes, a notification is raised.
- **Tests:**
  - byte identity with v1;
  - ordering;
  - header once;
  - refusal cases (no sidecar, grown by someone else, shrunk, sha
    changed);
  - adopt;
  - a lock-held append (simulated `PermissionError`) leaves rows pending
    and is retried;
  - never truncates: property-style, any sequence of operations leaves
    the file a prefix-extension of its previous content.

### T4: Route migration and UI to `sample_id` (after T2 and T3)

**Files:** `app.py`, `static/js/app.js`, `templates/index.html`,
`tests/test_route_fates.py`, plus boot tests.

- **Route fates** (the test asserts this table equals the app's registered
  routes, via AST):

| Route | Fate |
|---|---|
| `/api/files` | store query: `{samples:[{sample_id, instrument, lab_id, injection_dt, status, flags, best_fit, backfill, time_corrected, method_name}]}`, with paging and filters |
| `/api/metadata/<path>` | becomes `/api/samples/<id>/metadata` |
| `/api/trace?path` | becomes `/api/samples/<id>/trace` |
| `/api/distillation-curve?path` | becomes `/api/samples/<id>/distillation-curve` (the revision's `blank_used` and `calibration_used`) |
| `/api/table` | store query of current revisions (same columns as today) |
| `/api/analysis`, `/api/export-analysis-report`, `/api/export-analysis-reports-zip`, `/api/export-pdf`, `/api/export-comparison`, `/api/best-fit` | `sample_path` becomes `sample_id`; the numbers come from the stored revision |
| `/api/reprocess`, `/api/reprocess/preview`, `/api/reprocess/status` | by `sample_ids`; a lab-ID range needs `instrument` |
| `/api/export-lims` | by `sample_ids`; the gate is enforced; writes a revision (`reason:'export-lims'`) plus an export row |
| `/api/qbench-upload` | queue items `{sample_id}`; the server resolves the PDF; not final or gated → refused; records `qbench_revision` |
| `/api/scan`, `/api/scan/status`, `/api/scan/stream`, `/api/stop-scan`, `/api/rebuild-db`, `/api/library/reindex-times`, `/api/files/refresh` | **removed** (the UI controls go too) |
| `/api/calibration*` | unchanged API shape, but reads and writes the `gc1` instrument row; a save enqueues `awaiting_calibration` |
| everything else | unchanged |

- **Flags and best-fit** move to `sample_cache`. Nothing reads the JSON
  cache files any more. The file list never reads CDFs.
- **UI:**
  - the file list uses `sample_id`;
  - status badges for non-final samples;
  - a `time_corrected` marker;
  - the scan and rebuild controls are removed;
  - the node tests are updated.
- **Boot tests:** every migrated route by `sample_id`, 404 for an unknown
  id, the gate refusals, and the removed routes returning 404.

### T5: Legacy removal, packaging, docs, admin wiring (after T4)

- `app.py` refuses to start without `GC_DATA_DIR`, with a clear message.
- Delete `looker.py`, `run.pyw`, `instance.py`'s per-port seeding,
  `PER_INSTANCE_KEYS`, the legacy branches in `paths.py`,
  `test_run_pyw.py`, and the legacy tests.
- `instance.py` keeps port resolution; `--no-tray` stays accepted.
- Update `scripts/package_release.sh` (the required files, plus
  `pipeline.py store.py exports.py corrections.py import_match.py
  methods/ tools/`) and `tests/test_release_package.py`.
- Drop `pystray`/`Pillow` from `requirements.txt` if nothing imports
  them, and re-freeze the transitive pins.
- **Admin routes** (behind the existing `_check_admin`; D13's real
  password arrives in 2B1):
  - `/admin/exports`: pending, refused, Adopt, new path, write fresh;
  - `/admin/load-folder`: the T6 job;
  - `/admin/methods`: methods seen and mapping.
- Docs: CLAUDE.md (architecture), DEPLOY.md (v2 notes, `maint/v1`
  rule), RELEASING.md, and `docs/release-notes/v2.0.0.md` listing every
  result-affecting change:
  - blank at-or-before;
  - auto-detect off;
  - stricter file corrections;
  - the injection-time parse fix;
  - D7096-style runs no longer processed.

### T6 ∥: Folder loader and parity report

**Pipeline review notes for T6 (must do):**
- Read each file's time with `distill.cdf_identity` and submit in
  **injection-time order**, so history blanks never trigger late-blank
  flags.
- Pass `notifier=None` and report a summary line instead, including the
  late-blank `review_note` count.
- Pass the original file name as `source_name` and the original mtime as
  `mtime`.
- Expect `duplicate` and `conflict` outcomes and count them.
- Never release backfill samples.
- Call `instruments.startup()` (or `bootstrap_gc1`) first, so gc1 has
  `live_since` and the default method map. (after T2 and T3; own module)

**Files:** `jobs/load_folder.py`, `tools/parity_report.py`,
`tests/jobs/`.

- `load_folder(instrument, folder, *, backfill: bool, progress)`. It
  streams CDFs through `pipeline.submit` (read-only on the source) and is
  resumable, since submit dedupes. It runs as a job inside the hub (SSE
  progress); T5 wires the route.
- `parity_report(instrument, v1_results_csv) -> report` uses
  `import_match`. For every v1 row it finds the hub sample by the legacy
  string, then compares all 31 columns, with numeric tolerance 0 (exact
  strings). It explains each difference with a tag: `blank-rule`,
  `auto-detect-off`, `corrections`, `injection-time-fix`,
  `method-excluded`, or `unexplained`. It writes CSV and HTML to
  `data/reports/`.
- The acceptance gate on ASAPSV1 is **zero `unexplained`** on a robocopied
  sample of GC-1.
- **Tests:** synthetic fixtures where each tag is produced deliberately;
  `unexplained` is produced when a number is altered.

## Integration checkpoints

- After T3 and after T6, the controller runs the **integration critic**
  over the merged branch. It checks the seams:
  - store ↔ pipeline ↔ exports ↔ routes;
  - corrections §2 and the matcher §3 used as contracted;
  - the agent contract §1, still honoured by the `export_rows` line
    format.
- 2A1 ships as the next MAJOR only after the whole suite, the node tests
  and CI pass, and the integration critic approves. The parity gate runs on
  ASAPSV1 after deploy, before GC PCs cut over.
