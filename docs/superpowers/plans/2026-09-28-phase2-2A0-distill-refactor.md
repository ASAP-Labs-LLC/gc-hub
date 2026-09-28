# Phase 2 / 2A0: `distill` takes conf explicitly + `compute()` (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** make every calibration path in `distill.py` take its settings
(`conf`) explicitly, and add `distill.compute()`. It returns one sample's
results (row values, uncorrected D86, anchors used) without writing any file.
**No behaviour change**, proven by golden tests pinned to today's output.

**Architecture:** `process_cdf` becomes a thin wrapper:
`compute()` + today's CSV append + CDF move. The hub pipeline (2A1) will call
`compute()` with a per-instrument `conf`. The calibration cache key gains the
assignment signature for the passed `conf`, so two confs can never share a
cached calibration function.

**Tech stack:** Python 3.12/3.14, numpy/scipy/netCDF4, pytest.

**Spec:** `docs/superpowers/specs/2026-09-28-phase2-multi-instrument-hub-design.md`
(Delivery table, row 2A0; "`distill` refactor (2A0)").

## Conventions

- Repo `/Users/rynatical/Projects/gc-hub`, branch `feat/phase2`. Python is
  `.venv/bin/python`. Suite: `.venv/bin/python -m pytest tests/ -q`
  (427 at the start) plus `node tests/js/run.js`.
- `distill.py` and `looker.py` are **CRLF** (pinned `-text`). Preserve the
  line endings exactly: check with `file distill.py` before and after every
  edit.
- TDD: write the test, see it fail for the right reason, implement, see it
  pass, run the full suite, commit. Commit messages end with
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Never `import app` in new tests.

## File map

| File | Change |
|---|---|
| `tests/cdf_fixtures.py` | **new**: writes synthetic ANDI-style CDFs with netCDF4 (calibration ladder, sample, blank). |
| `tests/golden/d2887_rows.json` | **new**: rows produced by the **pre-refactor** code for the fixtures, committed before any refactor. |
| `tests/test_distill_golden.py` | **new**: golden plus `compute()` tests. |
| `distill.py` | modify: conf-explicit calibration functions, `calibration_anchors()`, `compute()`, `process_cdf` wrapper. |
| `docs/release-notes/v1.1.0.md` | **new** |

---

### Task 1: Synthetic CDF fixtures

**Files:** create `tests/cdf_fixtures.py` and `tests/test_cdf_fixtures.py`.

- [ ] **Step 1: Read how `distill` reads a CDF** (`_read_cdf_unlocked` ~169,
  `cdf_metadata` ~882, `parse_injection_datetime` ~842). List exactly which
  variables and attributes it needs: `ordinate_values`,
  `actual_sampling_interval`, `actual_delay_time`, and the global attributes
  `sample_name` and `injection_date_time_stamp`, or whatever the code really
  reads. The fixtures must satisfy exactly those.
- [ ] **Step 2: Write `tests/test_cdf_fixtures.py`** (failing first). It
  checks that:
  - `write_cdf(path, times, signal, sample_name, injected)` produces a file
    for which `distill.gc_xy_from_cdf` returns the same arrays;
  - `distill.cdf_metadata` returns `(sample_name, injected)`.
- [ ] **Step 3: Implement `tests/cdf_fixtures.py`.**

  ```python
  """Synthetic ANDI/netCDF chromatograms for tests. Deterministic: same args,
  same bytes-level data (no randomness)."""
  from datetime import datetime
  from pathlib import Path
  import numpy as np
  import netCDF4

  DT_MIN = 0.001            # 0.06 s sampling
  RUN_MIN = 8.0

  def gaussian(t, centre, height, width):
      return height * np.exp(-0.5 * ((t - centre) / width) ** 2)

  def ladder_times(n=20, first=0.50, step=0.30):
      return [round(first + i * step, 4) for i in range(n)]

  def write_cdf(path, times, signal, sample_name, injected: datetime):
      """Write the variables/attributes distill reads (see Step 1)."""
      ...  # implement against Step 1's list; keep float64 signal

  def calibration_cdf(path, injected=datetime(2026, 9, 16, 9, 0, 0)):
      t = np.arange(0, RUN_MIN, DT_MIN)
      y = gaussian(t, 0.30, 50000, 0.01)                   # CS2 solvent
      for i, c in enumerate(ladder_times()):
          y += gaussian(t, c, 8000 - 150 * i, 0.012)       # C5.. ladder
      y += gaussian(t, 1.13, 900, 0.01)                    # impurity
      write_cdf(path, t, y, "CAL 9.16", injected); return path

  def sample_cdf(path, name="40304", injected=datetime(2026, 9, 25, 0, 23, 0)):
      t = np.arange(0, RUN_MIN, DT_MIN)
      y = gaussian(t, 0.30, 50000, 0.01) + gaussian(t, 3.2, 1200, 0.9) + gaussian(t, 4.6, 600, 0.6)
      y += 40 + 5 * t                                        # bleed ramp
      write_cdf(path, t, y, name, injected); return path

  def blank_cdf(path, injected=datetime(2026, 9, 24, 15, 30, 27)):
      t = np.arange(0, RUN_MIN, DT_MIN)
      y = gaussian(t, 0.30, 50000, 0.01) + 40 + 5 * t
      write_cdf(path, t, y, "Blank", injected); return path
  ```

- [ ] **Step 4:** The tests pass. Commit: "Add synthetic CDF fixtures for distill tests".

### Task 2: Pin today's output (golden file) **before** refactoring

**Files:** create `tests/golden/make_golden.py`, `tests/golden/d2887_rows.json`,
and `tests/test_distill_golden.py`.

- [ ] **Step 1: Write `tests/golden/make_golden.py`** (a script, not a test).
  - In a temp dir, it writes `calibration_cdf`, `blank_cdf` and three
    samples: `40304`, `40305` (a different envelope: shift the centres +0.4
    min) and `Blank2`.
  - It writes a `settings.json` with:
    - `calibration_cdf` set to the fixture;
    - `calibration_assignments` produced by `distill.upsert_assignments`,
      with the CS₂ and the impurity marked `ignore` and the ladder assigned
      `N_ALKANE_CARBON[:20]`;
    - `correction_factors_json` pointing at a temp copy of the phase 1
      shape (`{"Agilent GC": {"IBP - D86": {"correction_value": -12.08}, …}}`,
      with the 11 cuts and the values from
      `~/Projects/gc-share-snapshot-2026-09-25/ref/correction_factors.json`
      if present, else the literal values in this plan: IBP −12.08,
      10% −5.25, 50% −4.06, 90% −3.46, FBP −5.57, others 0);
    - `distill_output` and `processed_cdf_dir` in the temp dir;
    - `bestfit_enabled` set to "false", so the output doesn't depend on
      standards.
  - It points `settings.CONFIG_PATH` at it, calls `distill.process_cdf` for
    each sample (with and without the blank), reads the CSV rows back, and
    writes `tests/golden/d2887_rows.json` as
    `{case_name: {header: value, …}}`.
  - It excludes `Source File`, which contains temp paths.
  - Cases: `sample_40304_noblank`, `sample_40304_blank`,
    `sample_40305_blank`, `auto_detect_no_assignments` (the same sample with
    `calibration_assignments` empty, to pin the auto-detect path too), and
    `no_corrections` (`correction_factors_json` = "").
- [ ] **Step 2:** Run it **on the unmodified code** and commit the JSON.
  Check the numbers are plausible (IBP < T50 < FBP, D86 values finite). If
  any case raises, fix the fixture, not the code.
- [ ] **Step 3: Write `tests/test_distill_golden.py::ProcessCdfGoldenTests`.**
  It re-runs the same cases through `distill.process_cdf` and asserts each
  row equals the golden value exactly, string for string, as read from the
  CSV. It must pass now, on unmodified code.
- [ ] **Step 4:** Commit: "Pin distill output with golden rows before refactor".

### Task 3: Calibration functions take conf explicitly

**Files:** modify `distill.py`
(`_build_calibration` ~555, `_assignment_signature` ~593,
`_calibration_function` ~607, `distillation_curve_from_cdf` ~923, plus
callers inside distill), `app.py` and `looker.py` callers only if their
signatures change. Tests go in `tests/test_distill_golden.py`.

- [ ] **Step 1: Failing tests.**
  1. Build **two** confs pointing at the same calibration CDF path but with
     different `calibration_assignments` (one shifted by one carbon).
     `distill._calibration_function(cal, conf_a)(t)` must differ from
     `_calibration_function(cal, conf_b)(t)` when called in either order.
     This fails today, because the global `_get_settings()` is used.
  2. `distill.distillation_curve_from_cdf(path, blank_path=None, conf=conf_a)`
     uses `conf_a`'s calibration, even when `settings.CONFIG_PATH` points at
     a file with a different calibration.
- [ ] **Step 2: Implement.**
  - `_build_calibration(cal_cdf, conf)` and `_assignment_signature(cal_path, conf)`
    read `conf` and never call `_get_settings()`.
  - `_calibration_function(cal_cdf, conf)`: the cache key stays the resolved
    path, and the cached tuple is `(mtime, sig, func)` where `sig` comes from
    the passed `conf`. The cache is still correct when two confs share a path
    with different assignments, because `sig` differs and the entry is
    rebuilt. Make it hold **several** signatures per path, keyed by
    `(path, sig)`, so two instruments sharing a calibration file don't evict
    each other on every sample.
  - `distillation_curve_from_cdf(path, *, blank_path=None, conf=None)`:
    `conf = conf if conf is not None else _get_settings()`.
  - Grep for every caller of the three functions (`app.py`, `looker.py`,
    tests) and pass `conf` through; `app.py` passes `_get_settings()`-
    equivalent conf where it has one.
- [ ] **Step 3:** The new tests pass, the golden tests still pass, and the
  full suite passes. Commit: "Calibration functions take conf explicitly".

### Task 4: `calibration_anchors()` and `compute()`

**Files:** modify `distill.py`; tests in `tests/test_distill_golden.py`.

- [ ] **Step 1: Failing tests.**
  - `distill.calibration_anchors(cal_cdf, conf)` returns
    `{"source": "assignments"|"auto", "anchors": [[rt, carbon], …]}`,
    matching what `_build_calibration` uses, including the auto-detect
    fallback case.
  - `distill.compute(cdf_path, conf, blank_path=None, corrections=None)`
    returns a dict with:
    - `row`: a dict keyed by `CSV_HEADER` names; `Source File` = "" and
      values exactly as `process_cdf` would write them (same rounding,
      same `""` for missing D86 keys);
    - `d2887`, `d86_uncorrected` and `d86` (corrected);
    - `lab_id` and `injection_dt` (a `datetime`);
    - `calibration`: `{cdf, anchors_source, anchors}`.

    Golden: for each golden case, `compute(...)["row"]` minus `Source File`
    equals the golden row when `corrections` is loaded the way `process_cdf`
    loads them.
  - `compute` with `corrections={}` gives D86 equal to
    `d86_uncorrected`.
  - `compute` never writes a file: the temp dir listing is unchanged, and
    the CSV and processed dir are untouched.
  - `compute` raises `FileNotFoundError` when the calibration is missing,
    with the same message as today.
- [ ] **Step 2: Implement.**
  - Extract the numeric body of `process_cdf` (steps 1 to 6) into
    `compute`. It takes `corrections: dict | None`; `None` means load them
    from `conf["correction_factors_json"]` exactly as today, so the wrapper
    is unchanged.
  - Best fit stays inside `compute`, with the same failure-safe behaviour.
  - `calibration_anchors` shares code with `_build_calibration`. Refactor
    so both call one private helper that returns
    `(rt, bp, carbons, source)`; `_build_calibration` builds the function
    from it.
- [ ] **Step 3: Make `process_cdf` a wrapper.**
  - It calls `result = compute(path, conf, blank_path)`, builds `row_data`
    from `result["row"]` in `CSV_HEADER` order with `source_file` computed
    as today, then runs today's append, dedupe and move code **unchanged**.
- [ ] **Step 4:** The golden tests (`process_cdf`) and the `compute` tests
  pass, and so does the full suite. Commit: "Add distill.compute() and calibration_anchors()".

### Task 5: Release notes

- [ ] Write `docs/release-notes/v1.1.0.md`: "Internal refactor in preparation
  for the multi-instrument hub. No change to results, reports or behaviour:
  golden tests pin every output value to v1.0.0." Commit it.

## Done when

- The golden file was created from v1.0.0 code and is unchanged at the end.
- All of the new tests pass, the full suite passes, and so do the node
  tests.
- `file distill.py` still reports CRLF.
