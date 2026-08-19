# Background Scan Responsiveness + Batch Reprocess + LIMS Export — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the sample library usable immediately during background scanning, make Stop/Refresh reliable, add shift/ctrl-click multi-select with batch reprocess, and add a batch "Export to LIMS" CSV-append action.

**Architecture:** Backend (`app.py`) gets a throttled cache-rebuild helper and a *sticky* stop using a `_suppressed_paths` set so the watcher abandons the stopped backlog but still processes genuinely-new files. A new dep-free `lims_export.py` module holds the LIMS row-matching logic behind a new `/api/export-lims` route. Frontend (`app.js`) gains a multi-selection model (shift = range, ctrl/cmd = toggle) used by two context-menu actions: batch reprocess (existing route) and Export to LIMS (new route).

**Tech Stack:** Python 3.11 / Flask (Werkzeug dev server, `threaded=True`), vanilla JS SPA, pytest + unittest. Node-based JS unit tests (`node tests/js/run.js`).

> **SUPERSEDED (2026-06-16):** Tasks 3–4 below originally implemented Export-to-LIMS as a *read-the-CSV-and-rewrite* route + `lims_export.py`. Live testing proved that could never compute+append a result (see the spec's "Addendum — 2026-06-16 debugging"). The shipped implementation instead routes Export-to-LIMS through the **reprocess tunnel** (`_enqueue_reprocess` → `process_cdf` → `distill._append_csv_row`), and reprocess/export now **append a new row each run** (no upsert). `lims_export.py` was removed. The spec addendum is authoritative for Feature 4.

---

## File structure

- `webapp/lims_export.py` — **new**, dep-free: `collect_export_rows(csv_rows, requests)`.
- `webapp/settings.py` — add `lims_export_csv` default.
- `webapp/app.py` — throttled rebuild helper, `_suppressed_paths` + sticky stop, watcher candidate filter, `/api/scan` clears suppression, `/api/export-lims` route.
- `webapp/static/js/app.js` — multi-selection state + handlers, context-menu batch reprocess + export-lims.
- `webapp/templates/index.html` — add `#ctx-export-lims` menu item.
- `webapp/static/css/style.css` — `.selected-multi` style.
- `webapp/tests/test_cache_rebuild_throttle.py`, `test_scan_stop_sticky.py`, `test_lims_export.py`, `test_export_lims_route.py` — **new**.
- `webapp/tests/test_app_routes.py` — add `/api/export-lims`.

---

## Task 1: Throttled file-cache rebuild

**Files:**
- Modify: `webapp/app.py` (near `_rebuild_files_cache`, ~line 158; watcher call ~line 2163)
- Test: `webapp/tests/test_cache_rebuild_throttle.py`

- [ ] **Step 1: Write the failing test**

```python
"""Unit test for the throttled file-cache rebuild helper.

Pure logic — monkeypatches app's clock and the real rebuild so no time
passes and no CSV/CDF I/O happens. Imports app (flask-gated); the watcher is
stopped in setUp so it cannot rebuild the cache mid-test.
"""
from __future__ import annotations

import unittest

try:
    import flask  # noqa: F401
    _HAS_FLASK = True
except Exception:  # pragma: no cover
    _HAS_FLASK = False


@unittest.skipUnless(_HAS_FLASK, "flask not installed")
class CacheRebuildThrottleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app = app_module

    def setUp(self):
        self.app._watcher_stop.set()
        self.calls = []
        self._orig_rebuild = self.app._rebuild_files_cache
        self._orig_clock = self.app._monotonic
        self.app._rebuild_files_cache = lambda: self.calls.append("rebuild")
        self._now = [1000.0]
        self.app._monotonic = lambda: self._now[0]
        self.app._last_cache_rebuild = 0.0

    def tearDown(self):
        self.app._rebuild_files_cache = self._orig_rebuild
        self.app._monotonic = self._orig_clock
        self.app._watcher_stop.clear()

    def test_first_call_rebuilds(self):
        self.app._maybe_rebuild_files_cache()
        self.assertEqual(self.calls, ["rebuild"])

    def test_second_call_within_window_skips(self):
        self.app._maybe_rebuild_files_cache()
        self._now[0] += 1.0  # < CACHE_REBUILD_MIN_INTERVAL
        self.app._maybe_rebuild_files_cache()
        self.assertEqual(self.calls, ["rebuild"])

    def test_call_after_window_rebuilds(self):
        self.app._maybe_rebuild_files_cache()
        self._now[0] += self.app.CACHE_REBUILD_MIN_INTERVAL + 0.1
        self.app._maybe_rebuild_files_cache()
        self.assertEqual(self.calls, ["rebuild", "rebuild"])

    def test_force_bypasses_throttle(self):
        self.app._maybe_rebuild_files_cache()
        self._now[0] += 0.1
        self.app._maybe_rebuild_files_cache(force=True)
        self.assertEqual(self.calls, ["rebuild", "rebuild"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cache_rebuild_throttle.py -v`
Expected: FAIL — `AttributeError: module 'app' has no attribute '_maybe_rebuild_files_cache'` (or `_monotonic`/`CACHE_REBUILD_MIN_INTERVAL`).

- [ ] **Step 3: Add the helper + constant + clock seam**

In `app.py`, near the file-cache section (after `_rebuild_files_cache`, ~line 285), add:

```python
import time as _time  # if `time` not already imported at top; app.py already `import time`

CACHE_REBUILD_MIN_INTERVAL = 10.0  # seconds — throttle mid-scan full rebuilds
_last_cache_rebuild: float = 0.0
_cache_rebuild_lock = threading.Lock()


def _monotonic() -> float:
    """Indirection seam so tests can freeze time."""
    return time.monotonic()


def _maybe_rebuild_files_cache(force: bool = False) -> None:
    """Rebuild the file cache, but at most once per CACHE_REBUILD_MIN_INTERVAL
    unless ``force`` is set. Removes the per-batch full-CSV reads that starve
    /api/files during a long scan."""
    global _last_cache_rebuild
    with _cache_rebuild_lock:
        now = _monotonic()
        if not force and (now - _last_cache_rebuild) < CACHE_REBUILD_MIN_INTERVAL:
            return
        _last_cache_rebuild = now
    _rebuild_files_cache()
```

- [ ] **Step 4: Swap the watcher's per-batch rebuild to throttled**

In `_watcher_loop`, the per-batch block (~line 2161-2165):

```python
                # Refresh in-memory file list after each batch so the UI
                # picks up newly processed samples mid-scan (throttled so the
                # full-CSV read doesn't starve request handling).
                if processed > 0:
                    try:
                        _maybe_rebuild_files_cache()
                    except Exception:
                        pass
```

And the end-of-scan rebuild (~line 2185-2190) becomes a forced rebuild:

```python
            if processed > 0:
                try:
                    _maybe_rebuild_files_cache(force=True)
                except Exception as fc_exc:
                    print(f"[WATCHER] File cache refresh failed: {fc_exc}",
                          flush=True)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_cache_rebuild_throttle.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add webapp/app.py webapp/tests/test_cache_rebuild_throttle.py
git commit -m "Throttle mid-scan file-cache rebuilds so /api/files stays responsive"
```

---

## Task 2: Sticky stop via `_suppressed_paths`

**Files:**
- Modify: `webapp/app.py` (`_watcher_loop` ~2049, `api_scan` ~2205, `api_stop_scan` ~2238)
- Test: `webapp/tests/test_scan_stop_sticky.py`

The key change is extracting the candidate filter and the halt-record step into
pure helpers so they're testable without running rglob.

- [ ] **Step 1: Write the failing test**

```python
"""Sticky-stop behaviour: a stopped backlog must not auto-resume, but
genuinely new files still get processed. Pure-helper + route tests."""
from __future__ import annotations

import unittest

try:
    import flask  # noqa: F401
    _HAS_FLASK = True
except Exception:  # pragma: no cover
    _HAS_FLASK = False


@unittest.skipUnless(_HAS_FLASK, "flask not installed")
class ScanStopStickyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app = app_module

    def setUp(self):
        self.app._watcher_stop.set()
        with self.app._suppressed_lock:
            self.app._suppressed_paths.clear()
        self.app._scan_halt.clear()

    def tearDown(self):
        with self.app._suppressed_lock:
            self.app._suppressed_paths.clear()
        self.app._scan_halt.clear()
        self.app._watcher_stop.clear()

    def test_filter_excludes_seen_and_suppressed(self):
        seen = {"a.cdf"}
        with self.app._suppressed_lock:
            self.app._suppressed_paths.update({"b.cdf"})
        out = self.app._filter_candidates(["a.cdf", "b.cdf", "c.cdf"], seen)
        self.assertEqual(out, ["c.cdf"])  # a seen, b suppressed, c is new

    def test_suppress_backlog_records_unprocessed(self):
        self.app._suppress_backlog(["x.cdf", "y.cdf"])
        with self.app._suppressed_lock:
            self.assertEqual(self.app._suppressed_paths, {"x.cdf", "y.cdf"})

    def test_api_scan_clears_suppression_and_halt(self):
        with self.app._suppressed_lock:
            self.app._suppressed_paths.update({"z.cdf"})
        self.app._scan_halt.set()
        client = self.app.app.test_client()
        resp = client.post("/api/scan")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.app._scan_halt.is_set())
        with self.app._suppressed_lock:
            self.assertEqual(self.app._suppressed_paths, set())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scan_stop_sticky.py -v`
Expected: FAIL — `AttributeError: module 'app' has no attribute '_suppressed_paths'` / `_filter_candidates` / `_suppress_backlog`.

- [ ] **Step 3: Add suppressed-paths state + helpers**

In `app.py`, near the scan globals (~line 138, after `_scan_halt`):

```python
_suppressed_paths: set[str] = set()   # backlog abandoned by a user Stop
_suppressed_lock = threading.Lock()


def _filter_candidates(all_cdfs, seen) -> list:
    """Discovered CDFs minus already-seen and user-suppressed paths.
    ``all_cdfs`` items may be Path or str; ``seen`` holds the same type the
    Looker stores. Suppression compares on str(path)."""
    with _suppressed_lock:
        suppressed = set(_suppressed_paths)
    return [fp for fp in all_cdfs if fp not in seen and str(fp) not in suppressed]


def _suppress_backlog(paths) -> None:
    """Record un-processed backlog paths so the watcher won't auto-resume them."""
    with _suppressed_lock:
        _suppressed_paths.update(str(p) for p in paths)
```

- [ ] **Step 4: Use the filter + remove the auto-clear in `_watcher_loop`**

Remove the unconditional `_scan_halt.clear()` at the top of the loop (currently ~line 2060). Keep `lk._stop_event.clear()` there ONLY if no stop is pending; simplest: clear `lk._stop_event` at the top but never `_scan_halt`. Replace the candidate line (~line 2080):

```python
            candidates = _filter_candidates(all_cdfs, lk._seen)
```

When a stop is detected mid-batch (the `if _scan_halt.is_set():` block that sets `stopped = True`, ~line 2138), record the not-yet-processed candidates before breaking. Compute them as the slice not yet started plus the cancelled remainder:

```python
                if _scan_halt.is_set():
                    for f in remaining:
                        f.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    stopped = True
                    # Abandon the rest of the backlog so it won't auto-resume.
                    not_started = candidates[batch_start + len(batch):]
                    cancelled = [futs[f] for f in remaining]
                    _suppress_backlog([*not_started, *cancelled])
```

(Top of loop now reads:)

```python
        try:
            lk = _get_looker()
            lk._stop_event.clear()
            _scan_status["phase"] = "scanning"
```

- [ ] **Step 5: `/api/scan` clears suppression + halt; `/api/stop-scan` unchanged behaviour**

`api_scan` (~line 2205) gains, before `_start_watcher()`:

```python
    _scan_halt.clear()
    with _suppressed_lock:
        _suppressed_paths.clear()
    _start_watcher()
    _scan_stop.set()
    return jsonify({"status": "started"})
```

`api_stop_scan` stays as-is (sets `_scan_halt`, signals Looker, invalidates dir cache, phase=stopped). The watcher does the suppression recording.

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_scan_stop_sticky.py -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add webapp/app.py webapp/tests/test_scan_stop_sticky.py
git commit -m "Make Stop Scan sticky: abandon stopped backlog, keep watching new files"
```

---

## Task 3: LIMS row-matching helper (pure module)

**Files:**
- Create: `webapp/lims_export.py`
- Test: `webapp/tests/test_lims_export.py`

- [ ] **Step 1: Write the failing test**

```python
"""Pure tests for LIMS export row matching — no flask, no real CSV."""
from __future__ import annotations

import unittest

import lims_export


ROWS = [
    {"Lab ID": "100", "InjectionDateTime": "2026-01-01 08:00:00", "2887 IBP": "40"},
    {"Lab ID": "100", "InjectionDateTime": "2026-02-01 09:00:00", "2887 IBP": "41"},
    {"Lab ID": "200", "InjectionDateTime": "2026-01-15 10:00:00", "2887 IBP": "55"},
]


class CollectExportRowsTests(unittest.TestCase):
    def test_exact_match_on_lab_id_and_inj_dt(self):
        found, missing = lims_export.collect_export_rows(
            ROWS, [{"lab_id": "100", "inj_dt": "2026-02-01 09:00:00"}])
        self.assertEqual(missing, [])
        self.assertEqual(found[0]["2887 IBP"], "41")

    def test_blank_inj_dt_falls_back_to_newest(self):
        found, missing = lims_export.collect_export_rows(
            ROWS, [{"lab_id": "100", "inj_dt": ""}])
        self.assertEqual(missing, [])
        # newest = lexicographically/chronologically latest InjectionDateTime
        self.assertEqual(found[0]["2887 IBP"], "41")

    def test_unmatched_lab_id_reported_missing(self):
        found, missing = lims_export.collect_export_rows(
            ROWS, [{"lab_id": "999", "inj_dt": ""}])
        self.assertEqual(found, [])
        self.assertEqual(missing, ["999"])

    def test_order_preserved(self):
        found, missing = lims_export.collect_export_rows(
            ROWS, [{"lab_id": "200", "inj_dt": ""}, {"lab_id": "100", "inj_dt": ""}])
        self.assertEqual([r["Lab ID"] for r in found], ["200", "100"])
        self.assertEqual(missing, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_lims_export.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lims_export'`.

- [ ] **Step 3: Create `webapp/lims_export.py`**

```python
"""lims_export.py — pure LIMS-export row matching (no flask / no I/O).

Given the rows parsed from the main distillation results CSV and a list of
export requests ({"lab_id", "inj_dt"}), pick the matching result row for each
request. Kept dependency-free so it unit-tests on a bare interpreter.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple


def collect_export_rows(
    csv_rows: List[Dict[str, str]],
    requests: List[Dict[str, str]],
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Return (found_rows, missing_lab_ids), preserving request order.

    Matching: exact ``(Lab ID, InjectionDateTime)`` when ``inj_dt`` is given and
    present; otherwise the newest row (max InjectionDateTime) for that Lab ID.
    A request whose Lab ID has no row at all is reported in ``missing``.
    """
    by_lab: Dict[str, List[Dict[str, str]]] = {}
    for row in csv_rows:
        lab = (row.get("Lab ID") or "").strip()
        if lab:
            by_lab.setdefault(lab, []).append(row)

    found: List[Dict[str, str]] = []
    missing: List[str] = []
    for req in requests:
        lab = str(req.get("lab_id", "")).strip()
        inj = str(req.get("inj_dt", "")).strip()
        rows = by_lab.get(lab)
        if not rows:
            missing.append(lab)
            continue
        match = None
        if inj:
            for row in rows:
                if (row.get("InjectionDateTime") or "").strip() == inj:
                    match = row
                    break
        if match is None:
            match = max(rows, key=lambda r: (r.get("InjectionDateTime") or ""))
        found.append(match)
    return found, missing
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_lims_export.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add webapp/lims_export.py webapp/tests/test_lims_export.py
git commit -m "Add pure LIMS-export row-matching helper"
```

---

## Task 4: `/api/export-lims` route (reuse the existing upsert tunnel)

**Decision:** No new CSV file, no new setting, no new lock. Re-send each selected
row through `distill._upsert_csv_row` into `distill_output` (idempotent upsert).

**Files:**
- Modify: `webapp/app.py` (new route, `import lims_export`)
- Test: `webapp/tests/test_export_lims_route.py`, `webapp/tests/test_app_routes.py`

- [ ] **Step 1: Add the route to the surface guard (failing-test-first)**

In `tests/test_app_routes.py`, add `"/api/export-lims",` to the `EXPECTED_ROUTES`
set (alphabetically near the other `/api/export-*`).

- [ ] **Step 2: Write the route integration test**

```python
"""Integration guard for /api/export-lims. Imports app (flask-gated); watcher
stopped in setUp. Uses a temp distill_output so no real data is touched.

Export reuses distill._upsert_csv_row into the SAME results CSV — so it is
idempotent: re-exporting an existing row refreshes it (no duplicate)."""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

try:
    import flask  # noqa: F401
    _HAS_FLASK = True
except Exception:  # pragma: no cover
    _HAS_FLASK = False


@unittest.skipUnless(_HAS_FLASK, "flask not installed")
class ExportLimsRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        import distill as distill_module
        cls.app = app_module
        cls.distill = distill_module

    def setUp(self):
        self.app._watcher_stop.set()
        self.tmp = tempfile.TemporaryDirectory()
        self.results_csv = Path(self.tmp.name) / "results.csv"
        with self.results_csv.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=self.distill.CSV_HEADER)
            w.writeheader()
            row = {k: "" for k in self.distill.CSV_HEADER}
            row.update({"Lab ID": "100", "InjectionDateTime": "2026-02-01 09:00:00",
                        "2887 IBP": "41"})
            w.writerow(row)
        self._orig = self.app.settings_mod.load_settings

        def fake_load():
            conf = self._orig()
            conf["distill_output"] = str(self.results_csv)
            return conf

        self.app.settings_mod.load_settings = fake_load

    def tearDown(self):
        self.app.settings_mod.load_settings = self._orig
        self.tmp.cleanup()
        self.app._watcher_stop.clear()

    def _rows(self):
        with self.results_csv.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_export_reports_exported_and_missing(self):
        client = self.app.app.test_client()
        resp = client.post("/api/export-lims", json={
            "rows": [{"lab_id": "100", "inj_dt": "2026-02-01 09:00:00"},
                     {"lab_id": "999", "inj_dt": ""}]})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["exported"], 1)
        self.assertEqual(data["missing"], ["999"])

    def test_export_is_idempotent_no_duplicate_row(self):
        client = self.app.app.test_client()
        body = {"rows": [{"lab_id": "100", "inj_dt": "2026-02-01 09:00:00"}]}
        client.post("/api/export-lims", json=body)
        client.post("/api/export-lims", json=body)
        rows = self._rows()
        matches = [r for r in rows if r["Lab ID"] == "100"
                   and r["InjectionDateTime"] == "2026-02-01 09:00:00"]
        self.assertEqual(len(matches), 1)  # upsert, not append-duplicate
        self.assertEqual(matches[0]["2887 IBP"], "41")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `~/.gc_consolidation_venv/bin/python -m pytest tests/test_export_lims_route.py tests/test_app_routes.py -v`
Expected: FAIL — route test 404s, and the surface guard fails until the route exists.

- [ ] **Step 4: Add the route**

In `app.py`, add `import lims_export` next to `import library_view`. Add the route
near the other export routes (~line 2769):

```python
@app.route("/api/export-lims", methods=["POST"])
def api_export_lims():
    """Re-send selected samples' result rows through the existing CSV upsert
    tunnel (distill._upsert_csv_row) into the results CSV. Idempotent."""
    body = request.get_json(force=True) or {}
    rows_req = body.get("rows", [])
    if not rows_req:
        return _error("No rows provided")

    conf = settings_mod.load_settings()
    dest_csv = Path(conf.get("distill_output", "distill_results.csv"))

    try:
        with distill._CSV_LOCK:
            if not dest_csv.is_file():
                return _error("Results CSV not found")
            with dest_csv.open("r", encoding="utf-8", newline="") as fh:
                csv_rows = list(csv.DictReader(fh))
    except Exception as exc:
        return _error(f"Could not read results CSV: {exc}")

    found, missing = lims_export.collect_export_rows(csv_rows, rows_req)

    for row in found:
        # Order the dict back into CSV_HEADER order for the positional upsert.
        row_list = [row.get(col, "") for col in distill.CSV_HEADER]
        try:
            distill._upsert_csv_row(dest_csv, row_list)
        except Exception as exc:
            LOGGER.warning("LIMS export upsert failed for %s: %s",
                           row.get("Lab ID"), exc)

    if missing:
        preview = ", ".join(missing[:50]) + ("…" if len(missing) > 50 else "")
        notifications_mod.get_store().add(
            "warning",
            f"Export to LIMS: {len(missing)} Lab ID(s) had no result row: {preview}",
        )

    return jsonify({"status": "ok", "exported": len(found), "missing": missing})
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `~/.gc_consolidation_venv/bin/python -m pytest tests/test_export_lims_route.py tests/test_app_routes.py tests/test_lims_export.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add webapp/app.py webapp/tests/test_export_lims_route.py webapp/tests/test_app_routes.py
git commit -m "Add /api/export-lims: re-send selected results through the CSV upsert tunnel"
```

---

## Task 5: JS test harness + pure selection helpers (real frontend TDD)

Node is installed (`/opt/homebrew/bin/node`). Pure helpers live in a DOM-free
module that exports both as a browser global and via `module.exports`, so the same
file ships to the browser and `require`s under Node.

**Files:**
- Create: `webapp/static/js/selection.js`
- Create: `webapp/tests/js/run.js`, `webapp/tests/js/selection.test.js`
- Modify: `webapp/tests/README.md` (document the JS test command)

- [ ] **Step 1: Write the failing JS test**

`tests/js/selection.test.js`:

```javascript
const { computeRangeSelection, selectionFilesOr } = require('../../static/js/selection.js');

module.exports = function (t) {
    // computeRangeSelection — forward range
    t.eq(computeRangeSelection(['a','b','c','d'], 'b', 'd'), ['b','c','d']);
    // backward range (anchor after target) is normalised
    t.eq(computeRangeSelection(['a','b','c','d'], 'd', 'b'), ['b','c','d']);
    // single item / anchor == target
    t.eq(computeRangeSelection(['a','b','c'], 'b', 'b'), ['b']);
    // unknown anchor falls back to [target]
    t.eq(computeRangeSelection(['a','b','c'], 'zz', 'c'), ['c']);

    // selectionFilesOr — >1 selected returns the picked files in file order
    const files = [{uid:'1'},{uid:'2'},{uid:'3'}];
    t.eq(selectionFilesOr(files, new Set(['1','3']), files[1]), [files[0], files[2]]);
    // <=1 selected falls back to [file]
    t.eq(selectionFilesOr(files, new Set(['2']), files[1]), [files[1]]);
    t.eq(selectionFilesOr(files, new Set(), files[2]), [files[2]]);
};
```

`tests/js/run.js` (zero-dependency runner):

```javascript
const assert = require('assert');
const t = {
    eq(a, b) { assert.deepStrictEqual(a, b); },
};
const tests = ['./selection.test.js'];
let failed = 0;
for (const f of tests) {
    try { require(f)(t); console.log('PASS', f); }
    catch (e) { failed++; console.error('FAIL', f, '\n', e.message); }
}
process.exit(failed ? 1 : 0);
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node tests/js/run.js`
Expected: FAIL — `Cannot find module '../../static/js/selection.js'`.

- [ ] **Step 3: Create `static/js/selection.js`**

```javascript
/* Pure, DOM-free selection helpers — shared by the browser (window globals)
   and Node tests (module.exports). No document/fetch references. */
(function (root) {
    /** Inclusive uid range between anchor and target in an ordered uid list. */
    function computeRangeSelection(orderedUids, anchorUid, targetUid) {
        const a = orderedUids.indexOf(anchorUid);
        const b = orderedUids.indexOf(targetUid);
        if (a === -1 || b === -1) return targetUid != null ? [targetUid] : [];
        const [lo, hi] = a <= b ? [a, b] : [b, a];
        return orderedUids.slice(lo, hi + 1);
    }

    /** Files for the current selection: the multi-selection (in `files` order)
        when >1 uid is selected, else just [file]. */
    function selectionFilesOr(files, selectedUids, file) {
        if (selectedUids && selectedUids.size > 1) {
            const picked = files.filter(f => selectedUids.has(f.uid || f.path));
            if (picked.length) return picked;
        }
        return [file];
    }

    root.computeRangeSelection = computeRangeSelection;
    root.selectionFilesOr = selectionFilesOr;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { computeRangeSelection, selectionFilesOr };
    }
})(typeof window !== 'undefined' ? window : globalThis);
```

- [ ] **Step 4: Run it to verify it passes**

Run: `node tests/js/run.js`
Expected: `PASS ./selection.test.js`, exit 0.

- [ ] **Step 5: Document the command + commit**

Append to `tests/README.md`: "Frontend pure-logic tests: `node tests/js/run.js`
(requires Node; tests `static/js/selection.js`)."

```bash
git add webapp/static/js/selection.js webapp/tests/js/ webapp/tests/README.md
git commit -m "Add Node JS test harness + pure selection helpers (selection.js)"
```

---

## Task 6: Wire multi-selection into app.js + CSS

**Files:**
- Modify: `webapp/templates/index.html` (load `selection.js` before `app.js`)
- Modify: `webapp/static/js/app.js` (`state` ~49, `renderFileList` ~464, `onFileClick` ~548)
- Modify: `webapp/static/css/style.css` (add `.selected-multi`)

Verify via the manual protocol in the final step. `computeRangeSelection` /
`selectionFilesOr` come from `selection.js` (Task 5); `orderedUidsForContainer`
is DOM-bound and stays in `app.js`.

- [ ] **Step 1: Load selection.js before app.js**

In `templates/index.html`, just before the `app.js` script tag (~line 1069):

```html
<script src="{{ url_for('static', filename='js/selection.js') }}?v=1"></script>
```

- [ ] **Step 2: Add selection state**

In `state` (`app.js` ~49) add:

```javascript
    selectedUids: new Set(),   // multi-selection (shift/ctrl)
    selectionAnchor: null,     // last plainly-clicked uid (range anchor)
```

- [ ] **Step 2b: Add the DOM-bound ordered-uid accessor**

Above `onFileClick` (~line 547) add:

```javascript
/** The uids of the list that was clicked, in current visual order. */
function orderedUidsForContainer(containerEl) {
    return Array.from(containerEl.querySelectorAll('li[data-uid]'))
        .map(li => li.dataset.uid);
}
```

- [ ] **Step 3: Rewire click handling for modifiers**

Change the item click wiring in `renderFileList` (~line 513) from:

```javascript
        item.addEventListener('click', () => onFileClick(file, mode, item));
```

to:

```javascript
        item.addEventListener('click', (ev) => onFileClick(file, mode, item, ev));
```

Rewrite `onFileClick` (~line 548) to:

```javascript
function onFileClick(file, mode, itemEl, ev) {
    const uid = file.uid || file.path;
    const container = itemEl.parentElement;

    if (ev && (ev.metaKey || ev.ctrlKey)) {
        // Toggle one item; no per-mode load action.
        ev.preventDefault();
        if (state.selectedUids.has(uid)) state.selectedUids.delete(uid);
        else state.selectedUids.add(uid);
        state.selectionAnchor = uid;
        renderAllFileLists();
        return;
    }
    if (ev && ev.shiftKey && state.selectionAnchor) {
        // Contiguous range from anchor; no per-mode load action.
        ev.preventDefault();
        const ordered = orderedUidsForContainer(container);
        const range = computeRangeSelection(ordered, state.selectionAnchor, uid);
        state.selectedUids = new Set(range);
        renderAllFileLists();
        return;
    }

    // Plain click — single active selection (existing behaviour).
    state.selectedUids = new Set([uid]);
    state.selectionAnchor = uid;
    state.selectedFile = file;
    state.selectedSample = file;

    const label = document.getElementById('analysis-sample-label');
    if (label) label.textContent = file.name;

    renderAllFileLists();

    if (mode === 'dashboard') {
        loadDashboardData(file);
    } else if (mode === 'analysis') {
        _clearQueueSelection();
        updateAnalysisOverlay();
        maybeAutoRunAnalysis();
    }
}
```

- [ ] **Step 4: Reflect multi-selection in rendering**

In `renderFileList` (~line 504, where `selected` is added) add after that block:

```javascript
        if (state.selectedUids && state.selectedUids.has(fileUid)) {
            item.classList.add('selected-multi');
        }
```

In `style.css`, add:

```css
.file-list li.selected-multi { background: #1f6feb33; outline: 1px solid #1f6feb88; }
```

- [ ] **Step 5: Manual verification protocol**

Run the app (`python app.py`), open `http://localhost:5560`. In the Dashboard sample list:
1. Plain-click a sample → only it highlights; chart loads. PASS if single-select still works.
2. Ctrl/Cmd-click three non-adjacent samples → all three show `.selected-multi`; no chart reload. PASS.
3. Click one sample, then Shift-click another 5 rows down → the inclusive range highlights. PASS.
4. Repeat (2)+(3) on the Chromatogram, Distillation-Curve, and Analysis lists → behaviour identical. PASS.

- [ ] **Step 6: Commit**

```bash
git add webapp/static/js/app.js webapp/static/css/style.css
git commit -m "Add shift/ctrl multi-select to the sample library (all panes)"
```

---

## Task 7: Context-menu batch actions (reprocess + export-to-LIMS)

**Files:**
- Modify: `webapp/templates/index.html` (add `#ctx-export-lims`, bump app.js `?v=`)
- Modify: `webapp/static/js/app.js` (`showContextMenu` ~597)

`selectionFilesOr` is provided by `selection.js` (Task 5) with signature
`selectionFilesOr(files, selectedUids, file)` — do NOT redefine it here; call it
as `selectionFilesOr(state.files, state.selectedUids, file)`.

- [ ] **Step 1: Add the menu item to HTML**

In `templates/index.html` (~line 1058-1060), inside `#context-menu`, after the reprocess item:

```html
  <div class="ctx-item" id="ctx-export-lims">Export to LIMS</div>
```

Bump the `app.js` cache-buster at the bottom (`app.js?v=38` → `?v=39`).

- [ ] **Step 2: Make the reprocess menu item batch-aware**

In `showContextMenu`, replace the reprocess click handler body (~line 622-645) with:

```javascript
        newReproc.addEventListener('click', async () => {
            removeContextMenu();
            const files = selectionFilesOr(state.files, state.selectedUids, file);
            const paths = [];
            const samples = [];
            for (const f of files) {
                if (f.path && f.path !== f.name) paths.push(f.path);
                else {
                    const sid = (f.name || '').replace(/\.CDF$/i, '').trim();
                    if (sid) samples.push(sid);
                }
            }
            if (!paths.length && !samples.length) {
                showNotification('No sample ID available', 'error');
                return;
            }
            try {
                const result = await apiPost('/api/reprocess', { paths, samples });
                _showReprocessToast(files.length, result.pending || 0);
                _pollReprocessStatus();
            } catch (err) {
                showNotification('Reprocess failed: ' + err.message, 'error');
            }
        });
```

Update the menu label before showing (in `showContextMenu`, where reproc is set up):

```javascript
        const reCount = (state.selectedUids && state.selectedUids.size > 1)
            ? state.selectedUids.size : 0;
        newReproc.textContent = reCount > 1 ? `Reprocess ${reCount} selected`
                                            : 'Reprocess sample';
```

- [ ] **Step 3: Wire the Export-to-LIMS menu item**

In `showContextMenu`, after the reprocess wiring, add:

```javascript
    const limsItem = document.getElementById('ctx-export-lims');
    if (limsItem) {
        limsItem.style.display = '';
        const newLims = limsItem.cloneNode(true);
        limsItem.replaceWith(newLims);
        newLims.id = 'ctx-export-lims';
        const limsCount = (state.selectedUids && state.selectedUids.size > 1)
            ? state.selectedUids.size : 0;
        newLims.textContent = limsCount > 1 ? `Export ${limsCount} to LIMS`
                                            : 'Export to LIMS';
        newLims.addEventListener('click', async () => {
            removeContextMenu();
            const files = selectionFilesOr(state.files, state.selectedUids, file);
            const rows = files.map(f => ({
                lab_id: (f.name || '').replace(/\.CDF$/i, '').trim(),
                inj_dt: f.inj_dt || '',
            })).filter(r => r.lab_id);
            if (!rows.length) {
                showNotification('No sample ID available', 'error');
                return;
            }
            try {
                const result = await apiPost('/api/export-lims', { rows });
                let msg = `Exported ${result.exported} to LIMS`;
                if (result.missing && result.missing.length) {
                    msg += ` (${result.missing.length} had no results)`;
                }
                showNotification(msg, result.exported ? 'success' : 'warning');
            } catch (err) {
                showNotification('Export to LIMS failed: ' + err.message, 'error');
            }
        });
    }
```

- [ ] **Step 4: Manual verification protocol**

Run the app. In the Dashboard list:
1. Right-click one sample → menu shows "Reprocess sample" and "Export to LIMS". Click Export to LIMS → toast "Exported 1 to LIMS"; confirm its row is (re)written in the results CSV.
2. Shift-select 3 samples, right-click → menu reads "Reprocess 3 selected" and "Export 3 to LIMS". Click Export → toast "Exported 3 to LIMS".
3. Select a sample with no results row (freshly listed, never parsed) and Export → notification reports it as missing; no crash.

- [ ] **Step 5: Commit**

```bash
git add webapp/templates/index.html webapp/static/js/app.js
git commit -m "Add batch reprocess + Export to LIMS to the library context menu"
```

---

## Task 8: Full regression run

- [ ] **Step 1: Run the whole Python suite**

Run: `~/.gc_consolidation_venv/bin/python -m pytest tests/ -v`
Expected: all tests pass (new + existing). Deps are present in this venv.

- [ ] **Step 2: Run the JS tests**

Run: `node tests/js/run.js`
Expected: `PASS ./selection.test.js`, exit 0.

- [ ] **Step 3: Final commit if anything was adjusted**

```bash
git add -A webapp/
git commit -m "Background-scan responsiveness, batch reprocess, LIMS export — complete"
```

---

## Self-review notes

- **Spec coverage:** F1 → Task 1; F2 → Task 2; F3 → Tasks 5,6,7; F4 → Tasks 3,4,7. All four features covered.
- **Type consistency:** `_filter_candidates`, `_suppress_backlog`, `_suppressed_paths`, `_maybe_rebuild_files_cache`, `_monotonic`, `CACHE_REBUILD_MIN_INTERVAL`, `collect_export_rows`, `selectionFilesOr(files, selectedUids, file)`, `computeRangeSelection` used identically across tasks.
- **LIMS semantics:** reuses `distill._upsert_csv_row` into `distill_output` — idempotent (replace-or-append on Lab ID + InjectionDateTime), one writer.
