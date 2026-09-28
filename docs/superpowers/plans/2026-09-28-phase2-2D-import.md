# Phase 2 plan 2D: history import (plan)

Spec: `2026-09-28-phase2-multi-instrument-hub-design.md` ("History import
(2D)", D1/D2/D10/D11, "Injection time", "Method detection", "Data model",
"Status machine"), contracts §3 (`import_match`), and the 2D carry-overs in
`2026-09-28-phase2-2A1-store-pipeline.md` (stamp-less CDFs stored at the whole
second; whitespace-only sample names counted; `pipeline.cdf_problem` over the
real share).

Branch `d/import` (from `feat/phase2`). **New files only:**
`jobs/import_history.py`, `tools/import_history.py`,
`tests/import_history/test_import_history*.py` (no `__init__.py`, unique
basenames), this plan. No route wiring: T5/2A2 add the admin page and call
`import_history(...)` with a progress callback. Strict TDD: every task starts
with a failing test that is run and seen red.

## API

```python
# jobs/import_history.py
import_history(instrument_id, processed_dir, results_csv, *,
               instrument_folder_aliases, db, data_dir, progress=None,
               dry_run=False, conf=None, by="import", batch_size=500,
               examples=20) -> dict            # the summary
format_summary(summary, *, examples=5) -> str  # text form
```

`progress(event)` gets dicts (as `jobs.load_folder`): `{"phase": "scan"}`,
`{"phase": "match", "done", "total"}` (CDF metadata pass, every 500),
`{"phase": "import", "done", "total", "outcome", "lab_id"}` per sample,
`{"phase": "commit", "done", "total"}` per batch, and `{"phase": "done",
"summary"}`. An exception raised by the callback stops the import (the
admin page's pause/cancel); the batch in flight rolls back, earlier batches
stay, the exception carries `exc.import_summary`, and a re-run resumes.

## Design decisions (decided up front)

- **No recompute, no submit.** `pipeline.submit` queues a `process` job and
  the in-hub Worker would recompute the sample, while imported numbers must
  be verbatim. So the importer has its own store path in
  `jobs/import_history.py` that reuses pipeline's helpers
  (`cdf_problem`, `_existing_result`, `_genuine_blank`, `_safe_stem`, the
  `data/cdf/<inst>/<YYYY>/<MM>/<lab>_<id>.CDF` layout, the original mtime on
  the stored copy) and **never enqueues a job and never writes
  `export_rows`** (D11).
- **Verbatim revisions** through `store.add_revision` (no store change):
  `results` = JSON of the CSV row's `values` (every `CSV_HEADER` key, each
  cell the exact string v1 wrote, including v1's `InjectionDateTime` and
  `Source File`), `reason='import'`, `corrections_used={"source":"legacy"}`,
  `blank_used`/`calibration_used`/`d86_uncorrected`/`flags` NULL,
  `best_fit` = the `Best Fit` cell (NULL if empty), `fit_score` = the `Fit
  Score` cell as a float (NULL if empty or not a number), `by` = `by`,
  `notes={"import": {"csv": <path>, "line_no": n}}`, `cdf_sha256`/`cdf_path`
  the sample's (NULL for result-only). Rows in CSV order; the last is
  current. Because the values stay strings, `exports.format_line(results,
  src)` reproduces v1's line byte for byte except `Source File` (tested).
- **Statuses.** CDF-backed with rows: `final` if its method name maps in the
  instrument's `method_map`, `other_method` if not, `review_method` if the
  CDF has none (spec "Method detection": unmapped history is imported as
  `other_method` with its CSV rows kept as revisions). Orphan CDFs: `raw_only`
  under the same method rule (`other_method`/`review_method` otherwise).
  Result-only: `final`, `method_name` NULL, `legacy_unverified=1`,
  `time_unverifiable` from the matcher, `injection_dt_source='csv'`. Every
  imported sample has `backfill=1`.
- **Times.** `injection_dt` = the CDF's correct time; a stamp-less CDF's
  mtime is truncated to the whole second (the hub's rule, so a live copy of
  the same file collides). `legacy_injection_dt` = `CdfMeta.legacy_injection_dt`
  (v1's string, unrounded for mtime); `time_corrected` = the two differ.
- **`is_blank`** is decided as `submit` does (`_genuine_blank`: blank name +
  plausible signal), so live samples after cutover find history blanks. No
  late-blank review notes are written (imported results never used a hub
  blank).
- **Classes → actions.** Matcher `key_collisions` `(kept, other)`: `other`
  is stored as a conflict against `kept`'s sample (file under
  `cdf/<inst>/conflicts/...`, `store.conflicts.add`). An imported CDF whose
  key is already taken by a different file in the store is a conflict too
  (as `submit`). `ambiguous_rows` are removed from the chosen sample (not
  imported, listed). `held_rows`, `mixed_rows` and rows without a key are
  listed, never imported. A sha already on another instrument (sample or
  conflict) is reported, not imported. Truncated/empty CDFs
  (`pipeline.cdf_problem`) are not imported, and neither are their rows;
  they are listed so the file can be recopied and the import re-run.
- **Idempotent / resumable.** Per sample: sha already on this instrument →
  nothing new, except that CSV rows appended since the last run are added as
  further import revisions when the stored import revisions are exactly the
  first rows (a "delta"); if the sample's current revision is not an import
  (the hub has processed it since) the new rows are listed, not added.
  Result-only: an existing sample with the key (or a CDF sample whose correct
  or legacy time is the CSV time) is not duplicated. Conflicts are found by
  sha. A second run over the same inputs writes nothing.
- **Batches.** Copies go to `cdf/.incoming` outside any transaction; then one
  `write_txn` per `batch_size` samples (default 500), each sample in its own
  SAVEPOINT (a failure is recorded and the batch goes on); the stored file is
  moved into place as the last step of its savepoint. A failed batch
  commit removes the files it placed.
- **Dry run** (`dry_run=True`): the same classification with no writes. With
  a `db` it also says what already exists; without one (`db=None`) it assumes
  an empty store and `instruments.DEFAULT_METHOD_MAP`. It runs
  `pipeline.cdf_problem` on every CDF read.
- **Summary** (JSON-ready dict): `counts` per class, `method_names`
  histogram, `whitespace_only_names` (CDF sample name non-empty but blank:
  the hub uses the file stem, v1 wrote the spaces), `truncated`, v1 misparse
  counts, `examples` per class, the matcher's stats; `format_summary` renders
  text.
- **CLI** `tools/import_history.py` only does `--dry-run` (read-only; an
  optional `--db` is opened read-only). Without `--dry-run` it exits 2 and
  says the real import runs inside the hub (admin page), the single writer.

## Tasks

1. **Plan** (this file). Commit.
2. **Core import.** Red: synthetic hub (the pipeline tests' `hub` fixture) +
   processed folder + v1 CSV: attached sample (final, backfill, revisions in
   CSV order, last current, fields above, stored copy with the original name
   and mtime, no jobs, no export rows); orphan → raw_only; result-only →
   final/legacy_unverified/csv/time_unverifiable; stamp-less CDF at the whole
   second with `time_corrected`; v1 misparse → `time_corrected`, matched on
   the legacy string; method rule (other_method/review_method with
   revisions); genuine blank `is_blank`; `format_line` of each imported
   revision equals the v1 line except Source File. Green. Commit.
3. **Classes.** Red: key collision → conflict row + stored file; key taken in
   the store → conflict; cross-instrument sha → reported; ambiguous rows not
   imported; held/mixed/keyless listed; truncated CDF not imported, its rows
   listed. Green. Commit.
4. **Idempotence, delta, batches, progress.** Red: second run writes nothing
   (row counts and file tree unchanged); appended CSV rows become new import
   revisions; delta refused after a hub revision; batch_size=2 commits in
   batches; a progress callback raising mid-run keeps committed batches and
   a re-run completes; per-sample failure recorded, batch continues. Green.
   Commit.
5. **Dry run + summary + CLI.** Red: dry run writes nothing (db and data
   dir unchanged) and predicts the same counts as the real run; summary keys,
   whitespace-only names, truncated, misparse counts, method histogram; text
   summary mentions every class; CLI `--dry-run` prints and writes `--json`,
   exits 0; without `--dry-run` exits 2. Green. Commit.
6. **Snapshot + suite.** Dry run over
   `/Users/rynatical/Projects/gc-share-snapshot-2026-09-25`
   (`webapp-live/distill_results.csv`, `cdf/processed-sample`, alias
   `processed_cdfs2`), skipped when absent; a real import of the snapshot
   CSV into a temp hub reproduces every v1 line except Source File. Full
   suite. Push, report.
