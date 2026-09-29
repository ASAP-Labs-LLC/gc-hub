# Tests

Regression suite for the GC Viewer webapp. Built while consolidating the two
divergent copies (`GC 2026.5 WEB` = canonical, `GC 2026.5 WEB - Copy` = backup)
and fixing the QBench upload outage.

## Run

```bash
# from webapp/
python -m pytest tests/ -v          # with pytest installed
python -m unittest discover -s tests -v   # stdlib only, no pytest
```

The structural QBench guards (`test_qbench_import.py`) run on a bare
interpreter with **no third-party deps** — by design, so they catch the
"module not importable on the server" class of bug in the same constrained
conditions where it bites. Tests that need numpy / netCDF4 / flask import those
modules and are skipped automatically if the dep is absent.

## Coverage

| File | What it locks in |
|------|------------------|
| `test_qbench_import.py` | The fix: `qbench_client.py` is vendored in `webapp/` and imports locally; **no** code depends on the dead `…\COA Reviewer\V2\Past Data Manager\API` share path; the uploader keeps `LoginFailedError` + post-upload verification that `app.py` relies on. |
| `test_app_routes.py` | The `@app.route` endpoints the frontend calls are still registered (AST scan — never imports app.py, which starts the hub's threads). `test_route_fates.py` pins every registered route against the T4 plan's route-fate table. |
| `test_hub_start.py` | `hub.start`: migrate, gc1 bootstrap, gc1 corrections seeded once (never zeros), the Worker (hub-owned corrections by default), the exporter woken on each final, the nightly backup and job prune; a booted hub turns a submitted CDF into an appended export row with no worker in the test; no `GC_DATA_DIR`, no start. |
| `test_hub_admin.py` | The admin folder-loader job and the export actions (adopt, new path, write fresh), booted, admin-gated. |
| `store/test_store_v2.py`, `test_comments.py`, `test_comments_api.py`, `test_ui_comments.py` | Phase 4: schema v2 is additive, seeds the presets exactly once, and v2.0.0's store.py (frozen in `store/v2_0_0/`) starts on it; the comment/preset rules (initials, limits, preset text copied, soft delete), `comments.for_report`'s shape and `comments.log_report`; the routes booted (JSON only, 64 KiB, cross-site guard, admin gate); in headless Chrome, escaping, annotation → one comment, shapes redrawn from GET, Clear Annotations, the presets admin panel. |
| `test_distill.py` | Core science: cumulative-area→percent, boiling-point interpolation, the ASTM D86 X4 polynomial, and the CSV upsert/dedup. |
| `test_settings.py` | `DEFAULTS` surface (incl. the `early_signal_*` keys) and a save/load round-trip in a temp folder; v1's per-port settings and instance seeding are gone. |
| `test_instance.py` | `instance.py` — port resolution precedence (`PORT` > `--port` > `GC_PORT` > 5560), validation and the port-in-use probe. Stdlib-only. |
| `test_paths.py` | Every state location under `GC_DATA_DIR`; without it every location refuses (`DataDirMissing`). |
| `test_analysis_bullets.py` | Phase 3 deviation bullets: `range_windows` (one carbon↔time conversion, extrapolation, clipping, not-evaluable), `report_params`, spikes, every rule of `build_deviation_report` and a golden string for every `render_bullets` template, the conclusion, the ranges + 1 bound. |
| `test_bullets_regression.py` | `fixtures/diesel_pair.npz` (synthetic diesel pair on the real calibration axis, built by `fixtures/make_diesel_pair.py`): the old per-excursion text gave 29 bullets, the range-driven report ≤ ranges + 1; the same-product pair and its retention-shift/±50% variants report no deviation, diesel + 1/2/5% gasoline flags Gas only. |
| `test_analysis_settings_routes.py` | Deviation-bullet admin settings are validated when saved (400, nothing written); Set as Default keeps the saved overlays unless sent. |
| `test_report_content.py` | `app._report_content` is identical across `/api/analysis`, the direct export, the ZIP and the QBench PDF (`report_harness.py` drives all four in one app process in a subprocess, with fake `comments` and QBench uploader); PDFs print the computed bullets, never client ones; escaping, footer, `report_log` rows. |
| `test_ui_analysis_smoke.py` | Headless Chrome: range boxes from the analysis windows, threshold lines and spike markers, read-only bullets, queue items capture params and carry no bullets. |

## Not covered here

The live Selenium upload to QBench (real Chrome, real credentials, the live
QBench DOM) cannot be exercised from a dev box — it needs the production
Windows host. These tests prove the upload no longer fails at the
import/API-lookup step; the browser-automation steps still need a manual
smoke test against a real sample.

## Frontend (JS) pure-logic tests

Pure, DOM-free helpers in `static/js/selection.js` (multi-select range +
selection-to-files resolution) are unit-tested under Node:

```bash
node tests/js/run.js        # requires Node; tests static/js/selection.js
```

The runner (`tests/js/run.js`) is zero-dependency (uses Node's `assert`). Keep
the helpers DOM/fetch-free so they `require` cleanly outside the browser.
