# Phase 2 Lane D: history-import matcher and dry run (plan)

Spec: `docs/superpowers/specs/2026-09-28-phase2-multi-instrument-hub-design.md`
("History import", "Data model", "Status machine", D1/D2/D10/D11) and
`2026-09-28-phase2-contracts.md` §3, which this lane implements exactly.

Branch `lane/import`. **New files only:** `import_match.py`,
`tools/import_dry_run.py`, `tests/import_match/test_import_match.py`, this
plan. No edits to `app.py`, `distill.py`, `settings.py`, `paths.py`,
`looker.py` or existing tests. Strict TDD: each task starts with a failing
test that is run and seen red.

## Design notes (decided up front)

- **Pure.** `import_match` imports `distill` only for `_read_text`,
  `parse_injection_datetime`, `CSV_HEADER` and `_NETCDF_LOCK`. No DB, no
  Flask, no settings. It never writes to the source.
- **`read_cdf_meta`** replicates `distill.cdf_metadata` (same lookup chain:
  `sample_name`; `injection_date_time_stamp` → `injection_date` →
  `injection_time`; variables before global attributes) so the lab ID and the
  canonical time equal what v1 wrote to the CSV, but it knows whether it fell
  back to the file mtime (`dt_source='mtime'`). `injection_dt` is
  `dt.isoformat(sep=" ")` (an mtime keeps its microseconds, exactly as v1
  wrote it). `sha256` is streamed in 1 MiB chunks; no chromatogram arrays are
  read.
- **`read_results_csv`** is header-driven: a record whose first cell (BOM
  stripped) is `Lab ID` and that contains `InjectionDateTime` is a header and
  (re)defines the columns from there on (handles old headers without
  `Best Fit`/`Fit Score`/`Source File`/T40/T60, and repeated headers
  mid-file). A data record with exactly `len(CSV_HEADER)` cells under an
  older, shorter header is read with `CSV_HEADER` (v1 appended new-format
  rows under an unmigrated header). Blank and all-empty records are skipped.
  `values` holds every `CSV_HEADER` name (missing → `""`), cells verbatim.
  `line_no` is the 1-based physical line the record starts on. Anomalies
  (short/long records, headerless start) are collected by
  `read_results_csv_ex` for the report.
- **`match`** (deterministic regardless of input order):
  1. CDFs sorted by path; identical sha256 → the first path is kept, the
     rest go to `dup_sha` as `(kept, dup)` and are not samples.
  2. CDF key `(lab_id.strip(), injection_dt)`. Two different-byte CDFs with
     the same key: the first path is kept, the other is reported in
     `stats['key_collisions']` and is not a sample (it would violate
     `UNIQUE(instrument_id, lab_id, injection_dt)`; the importer treats it as
     a conflict).
  3. Rows whose `Source File` is non-empty and not under
     `instrument_folder` (compared case-insensitively with `\` and `/`
     equivalent) → `mixed_rows`; never attached, not result-only.
  4. Remaining rows grouped by `(lab_id, injection_dt_raw)` in CSV order and
     attached to the CDF with the same key (all rows of the group, CSV order:
     revisions; last is current). Groups with no CDF → one result-only sample
     each (`cdf=None`) and their rows in `unmatched_rows`. Rows with an empty
     lab ID or time can't form a sample key: they are listed in
     `unmatched_rows` and counted in `stats['rows_missing_key']`, but not
     made samples.
  5. CDFs with no rows → orphan samples (`rows=[]`). CDFs whose
     `dt_source=='mtime'` → `no_injection_time` (they may still match).
  6. `samples` sorted by `(injection_dt, lab_id, path or "")`.
- **`dry_run`** walks `processed_dir` recursively (sorted, `*.cdf` any case),
  `results_csv` optional (CDF-only mode). Unreadable CDFs go to
  `stats['cdf_errors']`. Stats include source-folder counts of the CSV
  (answers "is the 5570 CSV mixed") and diagnostics for unmatched rows
  (same lab ID on a CDF at another time; Source File inside the folder but
  no match).
- **CLI** `tools/import_dry_run.py --processed-dir D [--results-csv F]
  --instrument-folder P [--json OUT] [--examples N] [--processed-index J]`
  prints a summary plus the first N examples of each problem class. Read
  only. `--processed-index` summarises v1's `.processed_index.json`.

## Tasks

1. **Fixtures + `read_cdf_meta`.** Test helper writes small NETCDF3 CDFs
   with `netCDF4` (`ordinate_values`, `actual_sampling_interval`,
   global `sample_name` / `injection_date_time_stamp`, or as char
   variables). Red: exact meta (compact stamp with `+0000` →
   `"2026-09-24 15:30:27"`, `dt_source='cdf'`, sha equals hashlib of bytes);
   variable-form metadata; missing stamp → mtime with microseconds and
   `dt_source='mtime'`; unparsable stamp → mtime; lab ID equals
   `distill.cdf_metadata`'s. Green: implement. Commit.
2. **`read_results_csv`.** Red: current header; old header without Best
   Fit/Fit Score/Source File (and without T40/T60) → those values `""`; new
   31-cell rows appended under an old header; BOM; blank lines; duplicate
   header mid-file (not a row); headerless file; line numbers; verbatim
   values. Green. Commit.
3. **`match`.** Red: exact match; revisions in CSV order; unmatched rows →
   result-only + `unmatched_rows`; orphans; dup bytes; mtime fallback listed;
   prefix trap (row `123` with Source File `.../1234_...CDF` stays
   unmatched, CDF `1234` is an orphan); same lab ID different time; mixed
   rows (UNC, case-insensitive, forward slashes); rows missing a key; key
   collision; determinism under shuffled inputs. Green. Commit.
4. **`dry_run` + summary + processed-index summary.** Red: end to end on a
   temp folder (+ CDF-only mode), recursion, unreadable CDF reported,
   source files never modified (bytes and mtime unchanged),
   `format_summary` mentions each class, `summarize_processed_index`
   counts/duplicates/canonical/microseconds/`(Blank)`-like names. Green.
   Commit.
5. **CLI.** Red: subprocess run prints summary, writes `--json`, exit 0;
   missing folder → exit 2. Green. Commit.
6. **Snapshot run + full suite.** Run the CLI against
   `/Users/rynatical/Projects/gc-share-snapshot-2026-09-25` (CDF-only on
   `cdf/`, plus the `webapp-live/distill_results.csv` found there, plus the
   processed index). Run the full suite. Report numbers and
   recommendations.

## Review fixes (critic, 2026-09-28)

7. **Injection time (C1).** Red: literal expectations for v1's
   fromisoformat-first misparse (`_v1_parse`) and the fixed parse
   (`_correct_parse`) over compact, `Z`, date-only and ISO stamps;
   `CdfMeta.legacy_injection_dt` / `raw_stamp` / `v1_injection_dts`;
   `method_name`; `normalise_lab_id`. Green. Commit.
8. **Match (I1, I3–I6).** Red: rows match any v1 form; `key_collisions`
   and `held_rows` report fields; Source File basename tie-breaks;
   folder aliases; mixed rows whose key matches here; non-canonical times;
   time zone; near misses; `no_injection_time` for kept CDFs only; dup
   bytes keep the path a row names; method-name histogram; result-only
   samples carry `dt_source='csv'`. Green. Commit.
9. **Reader, dry run, summary, CLI (minors).** Red: last-wins duplicate
   columns, header on `Lab ID` alone with missing columns reported, strict
   UTF-8 then cp1252; dry run holds bad-layout rows, per-folder counts and
   method names; summary labels and the mixed-folder warning; read-only
   test on a chmod'ed tree; `--json` guard by samefile/normcase; repeatable
   `--instrument-folder`. Green. Commit.
