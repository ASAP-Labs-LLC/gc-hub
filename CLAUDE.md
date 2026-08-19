# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Flask backend + single-page frontend that is a **1:1 web clone of the PyQt5 desktop "GC Viewer & Distillation Parser"** (documented in `../../CLAUDE.md`). It watches a folder for gas-chromatograph `.CDF` files, runs an ASTM D2887 simulated distillation, derives ASTM D86 temperatures, serves Plotly traces + a live results table, and can attach generated PDF reports to QBench samples. The REST API in `app.py` delegates all heavy lifting to the backend modules (`distill`, `looker`, `settings`, `qbench_pdf_uploader`).

## Deployment topology (important, not obvious from the code)

- **Production runs on Windows** (`\\ASAPServer`), launched by `run.pyw` (a `pystray` tray app using `ctypes.windll`, `taskkill`, `CREATE_NEW_CONSOLE`). It auto-restarts the Flask subprocess when any `.py` in `webapp/` changes (watchdog).
- **Development is on a Mac**, editing the same files over the shared drive (`\\ASAPServer\Labsharedrive` == `/Volumes/Labsharedrive`).
- **Windows-isms are intentional, not bugs:** `chromedriver.exe`, UNC credential paths (`\\ASAPServer\...\qbenchlogin.txt`), `os.environ["TEMP"]`, `run.pyw`'s win32 calls. Don't "port" them to POSIX.
- The canonical folder is **`GC 2026.5 WEB/`**. A sibling `GC 2026.5 WEB - Copy/` is a backup of an older divergent build — do not edit it as if it were live.

## Run / develop

```bash
# from webapp/
pip install -r requirements.txt        # Python 3.11+ (prod). netCDF4 needs HDF5 system libs.
python app.py                          # dev server on http://0.0.0.0:5560
```
On Windows the normal entry point is double-clicking `run.pyw` (tray icon → Restart / Open in Browser). First launch creates `~/.gc_viewer_settings.json`; configure paths via the in-app Settings, not by hand.

## Tests

```bash
# from webapp/
python -m pytest tests/ -v                       # needs requirements-dev.txt (pytest)
python -m unittest discover -s tests -v          # stdlib-only fallback
python -m pytest tests/test_distill.py -k d86    # single test / pattern
```
`tests/test_qbench_import.py` is deliberately **dep-free** (AST/source only) so it runs on a bare interpreter; tests needing numpy/netCDF4/flask import them and skip if absent. See `tests/README.md` for coverage. There is no lint config.

## Architecture

- **`app.py`** — the Flask app and all 37 routes. Module-level `_init_app()` (called on import, line ~3393) starts daemon threads: a Looker file-watcher and an auto-restart loop. **Importing `app` therefore has side effects** — tests assert the route surface via AST instead of importing it. Long-running work (scans, QBench uploads) runs in background threads and streams progress to the frontend over **Server-Sent Events** (`/api/*/stream`); `_publish*/_sse_stream` fan messages out to subscriber queues. The QBench upload queue is append-while-running and supports mid-run credential re-prompt and per-item skip.
- **`distill.py`** — the numeric core. `_cumulative_percent` (trapezoidal integration → 0–100%), `_interp_bp` (boiling-point interpolation at `PERCENT_LEVELS`), `_convert_to_d86` (ASTM D86 App. X4 polynomial via `_CONVERSION_COEFF`/`_CONVERSION_REL`), and `_upsert_csv_row` (replace-or-append keyed on `Lab ID` + `InjectionDateTime`). All CSV and NetCDF access is serialized through module locks (`_CSV_LOCK`, `_NETCDF_LOCK`). `CSV_HEADER` is 31 columns (D2887 + D86 cuts + `Best Fit`/`Fit Score`). **Approximation only — not ASTM-certified.**
- **`looker.py`** — `Looker` thread that polls `watch_dir` for new `.CDF`, copies/renames into the processed dir, and calls `distill.process_cdf()`; dedupes on `(Lab ID, InjectionDateTime)`.
- **`settings.py`** — JSON config at `~/.gc_viewer_settings.json`. `DEFAULTS` is the source of truth for keys; `load_settings()` merges the file over defaults and ensures the comparison-standards dir exists.
- **`qbench_pdf_uploader.py`** — headless-Chrome Selenium upload of a PDF to a QBench sample. `attach_pdf_to_sample()` does: API lookup (lab_id → sample_id) → login (raising `LoginFailedError` on bad creds/lockout) → attach → save → verify. `app.py` catches `LoginFailedError`, so keep it.
- **`qbench_client.py`** — thin QBench REST v2 client (JWT auth, rate limiting, retries). **Must stay vendored here in `webapp/`.**
- **`analysis_core.py`** — Analysis-tab numerics, importable without app.py's side effects. Two deviation channels: the rolling low-quantile *trend* difference (broad envelope deviations) and a *spike* channel on the raw sample−standard difference with a min-width guard (`analysis_spike_min_width_min`, default 0.02 min) — the trend's low quantile erases short tall spikes (gasoline adulteration) by construction, so never remove the spike channel. Also owns `resolve_report_ranges` (request ranges → saved `analysis_range_overlays` → legacy gas/oil) used by `/api/analysis` and both report-export routes via `_run_export_analysis`.
- **`sample_flags.py`** — operator-defined flag rules (JSON in `sample_flag_rules` setting): `{name, above|below, threshold, t_start, t_end, color, enabled}`. `above` = any point in window exceeds; `below` = all points stay under (no-signal case). Legacy `early_signal_*` settings auto-migrate. app.py caches per-file results keyed on a rules fingerprint (`.sample_flags_cache.json`); file entries expose `flags` + legacy `early_signal` bool.
- **`fuel_fit.py`** — fuel-type best-fit vs the comparison standards: baseline-subtracted, area-normalized envelopes (amplitude-invariant), small retention-shift search, cosine score; NNLS blend names two-component mixes (`Mix: A + B (80/20)`), else `Mix`. Tunables in settings (`bestfit_*`). Called from `distill.process_cdf` (CSV columns `Best Fit`/`Fit Score`), the `/api/best-fit` route, and file-list enrichment (`.bestfit_cache.json`). CDF reading is injected (`load_standards(dir, reader)`) to avoid import cycles.
- **Frontend pure-logic modules** (window globals + module.exports, node-tested via `node tests/js/run.js`): `selection.js`, `report_payload.js` (export payloads carry the operator's region overlays), `flagrules.js` (rule editor rows ↔ rules, legacy migration mirror).

## QBench gotcha (caused a production outage)

`qbench_pdf_uploader._api_lookup` does `from qbench_client import QBenchAPIClient`. It must resolve the **local** `webapp/qbench_client.py` (on `sys.path` because `run.pyw` runs `app.py` from `webapp/`). Do **not** reintroduce any `sys.path.insert` to an external share path (the old `…\COA Reviewer\V2\Past Data Manager\API`) — that dir is gone and importing from it took uploads down. `tests/test_qbench_import.py` guards this.
