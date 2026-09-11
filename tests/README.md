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
| `test_app_routes.py` | All 36 `@app.route` endpoints the frontend calls are still registered (AST scan — never imports app.py, so no watcher threads spawn). |
| `test_distill.py` | Core science: cumulative-area→percent, boiling-point interpolation, the ASTM D86 X4 polynomial, and the CSV upsert/dedup. |
| `test_settings.py` | `DEFAULTS` surface (incl. the `early_signal_*` keys) and a save/load round-trip, isolated from the real `~/.gc_viewer_settings.json`. Also the port-aware `CONFIG_PATH` and the new-instance seeding rules (`PER_INSTANCE_KEYS` reset to defaults). |
| `test_instance.py` | `instance.py` — port resolution precedence (`--port` > `GC_PORT` > 5560), validation, per-port settings/pidfile paths, the remembered-ports list, and the port-in-use probe. Stdlib-only. |

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
