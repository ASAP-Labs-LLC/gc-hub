# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Flask backend + single-page frontend that is a **1:1 web clone of the PyQt5 desktop "GC Viewer & Distillation Parser"** (documented in `../../CLAUDE.md`). It watches a folder for gas-chromatograph `.CDF` files, runs an ASTM D2887 simulated distillation, derives ASTM D86 temperatures, serves Plotly traces + a live results table, and can attach generated PDF reports to QBench samples. The REST API in `app.py` delegates all heavy lifting to the backend modules (`distill`, `looker`, `settings`, `qbench_pdf_uploader`).

## Deployment topology (important, not obvious from the code)

- **Production runs on Windows** (`\\ASAPServer`), launched by `run.pyw` (a `pystray` tray app using `ctypes.windll`, `taskkill`, `CREATE_NEW_CONSOLE`). It auto-restarts the Flask subprocess when any `.py` in `webapp/` changes (watchdog).
- **Development is on a Mac**, editing the same files over the shared drive (`\\ASAPServer\Labsharedrive` == `/Volumes/Labsharedrive`).
- **Windows-isms are intentional, not bugs:** `chromedriver.exe`, UNC credential paths (`\\ASAPServer\...\qbenchlogin.txt`), `os.environ["TEMP"]`, `run.pyw`'s win32 calls. Don't "port" them to POSIX.
- **The canonical D2887 folder is this one** — `GC 2026.5 2887 Advanced analysis/`. (An earlier copy of this file claimed `GC 2026.5 WEB/`; that tree stopped at 2026-06-16, has never been launched, and lacks `analysis_core.py`, `fuel_fit.py` and `sample_flags.py`.) `GC 2027/webapp` is the same 2026-06-16 build with D7096 *gasoline* modules bolted on — it is the gasoline fork, not newer diesel work.

## Run / develop

```bash
# from webapp/
pip install -r requirements.txt        # Python 3.11+ (prod). netCDF4 needs HDF5 system libs.
python app.py                          # dev server on http://0.0.0.0:5560
python app.py --port 5561              # or GC_PORT=5561 python app.py
```
On Windows the normal entry point is double-clicking `run.pyw` (tray icon → Restart / Open in Browser). First launch creates `~/.gc_viewer_settings.json`; configure paths via the in-app Settings, not by hand.

## Running more than one instance

Several copies can run on one machine — one per GC workstation folder. Double-clicking `run.pyw` opens a port dialog (default 5560, dropdown of previously used ports) before the server starts; it refuses a port that is already bound. Pass `--port N` or set `GC_PORT` to skip the dialog for an auto-started instance.

`instance.py` derives everything that must differ from the port alone:

| | port 5560 | port 5561 |
|---|---|---|
| Settings | `~/.gc_viewer_settings.json` | `~/.gc_viewer_settings-5561.json` |
| Pidfile | `.gc_server.pid` | `.gc_server-5561.pid` |

Port 5560 keeps the historic filenames, so the existing install needed no migration. Remembered ports live in `~/.gc_launcher_ports.json` (shared by all instances — it is a list of choices, not config).

A new instance's settings are seeded from the 5560 file, **except** `settings.PER_INSTANCE_KEYS` (`watch_dir`, `processed_cdf_dir`, `distill_output`), which reset to defaults so the operator must choose them. Two instances sharing `distill_output` would both append to one results CSV.

**Give each instance its own watch folder.** The Looker dedupes on `(Lab ID, InjectionDateTime)` within one CSV, not across instances, so two instances watching one folder both process every file.

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
- **`instance.py`** — per-instance identity, so several copies can run at once. Owns port resolution (`--port` → `GC_PORT` → 5560), validation, the port-in-use probe, and the per-port settings/pidfile paths. Stdlib-only and side-effect-free at import, which is what lets it be unit-tested and imported by both `app.py` and `run.pyw`.
- **`settings.py`** — JSON config at `~/.gc_viewer_settings.json` (or `~/.gc_viewer_settings-<port>.json` off the default port — see *Running more than one instance*). `DEFAULTS` is the source of truth for keys; `load_settings()` merges the file over defaults and ensures the comparison-standards dir exists. `PER_INSTANCE_KEYS` are the paths a second instance must not inherit.
- **`qbench_pdf_uploader.py`** — headless-Chrome Selenium upload of a PDF to a QBench sample. `attach_pdf_to_sample()` does: API lookup (lab_id → sample_id) → login (raising `LoginFailedError` on bad creds/lockout) → attach → save → verify. `app.py` catches `LoginFailedError`, so keep it.
- **`qbench_client.py`** — thin QBench REST v2 client (JWT auth, rate limiting, retries). **Must stay vendored here in `webapp/`.**
- **`analysis_core.py`** — Analysis-tab numerics, importable without app.py's side effects. Two deviation channels: the rolling low-quantile *trend* difference (broad envelope deviations) and a *spike* channel on the raw sample−standard difference with a min-width guard (`analysis_spike_min_width_min`, default 0.02 min) — the trend's low quantile erases short tall spikes (gasoline adulteration) by construction, so never remove the spike channel. Also owns `resolve_report_ranges` (request ranges → saved `analysis_range_overlays` → legacy gas/oil) used by `/api/analysis` and both report-export routes via `_run_export_analysis`.
- **`sample_flags.py`** — operator-defined flag rules (JSON in `sample_flag_rules` setting): `{name, above|below, threshold, t_start, t_end, color, enabled}`. `above` = any point in window exceeds; `below` = all points stay under (no-signal case). Legacy `early_signal_*` settings auto-migrate. app.py caches per-file results keyed on a rules fingerprint (`.sample_flags_cache.json`); file entries expose `flags` + legacy `early_signal` bool.
- **`fuel_fit.py`** — fuel-type best-fit vs the comparison standards: baseline-subtracted, area-normalized envelopes (amplitude-invariant), small retention-shift search, cosine score; NNLS blend names two-component mixes (`Mix: A + B (80/20)`), else `Mix`. Tunables in settings (`bestfit_*`). Called from `distill.process_cdf` (CSV columns `Best Fit`/`Fit Score`), the `/api/best-fit` route, and file-list enrichment (`.bestfit_cache.json`). CDF reading is injected (`load_standards(dir, reader)`) to avoid import cycles.
- **Frontend pure-logic modules** (window globals + module.exports, node-tested via `node tests/js/run.js`): `selection.js`, `report_payload.js` (export payloads carry the operator's region overlays), `flagrules.js` (rule editor rows ↔ rules, legacy migration mirror).

## QBench gotcha (caused a production outage)

`qbench_pdf_uploader._api_lookup` does `from qbench_client import QBenchAPIClient`. It must resolve the **local** `webapp/qbench_client.py` (on `sys.path` because `run.pyw` runs `app.py` from `webapp/`). Do **not** reintroduce any `sys.path.insert` to an external share path (the old `…\COA Reviewer\V2\Past Data Manager\API`) — that dir is gone and importing from it took uploads down. `tests/test_qbench_import.py` guards this.
