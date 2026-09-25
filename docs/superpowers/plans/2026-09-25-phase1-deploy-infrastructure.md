# Phase 1 — Deploy Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `gc-hub` deployable by the COA Reviewer updater on ASAPSV1 (tag → release zip → health-checked, self-installing app), add a QBench API credential screen, and fix the calibration/analysis crash.

**Architecture:** A new side-effect-free `paths.py` decides where all state lives: under `GC_DATA_DIR` when the updater sets it, and exactly where it lives today when it doesn't (legacy mode). `app.py` gains `/healthz`, a `VERSION` stamp, COA's `restart_update.py` handshake and `supervisor.py`, and production-safe startup (no debug, no self-respawn under the updater). A single `distill.calibration_ladder()` replaces four copy-pasted blocks that paired peak times with carbon numbers by position.

**Tech Stack:** Python 3.11+, Flask 3.1, numpy/scipy/netCDF4, pytest, node for JS unit tests, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-25-phase1-deploy-infrastructure-design.md`

## Conventions for every task

- Run all commands from the repo root `/Users/rynatical/Projects/gc-hub`. Python is `.venv/bin/python`.
- Full suite: `.venv/bin/python -m pytest tests/ -q` (214 pass at start) and `node tests/js/run.js`.
- **Never `import app` in a test.** Import-time side effects start threads. Test routes through a subprocess boot (Task 4 helper) or via AST, as `tests/test_app_routes.py` does.
- TDD: write the test, run it and **see it fail for the expected reason**, implement, and see it pass. Then run the full suite.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Windows-isms (`taskkill`, UNC paths, `CREATE_NEW_CONSOLE`) are intentional. Do not port them to POSIX.

## File map

| File | Status | Responsibility |
|---|---|---|
| `distill.py` | modify | add `calibration_ladder()` |
| `analysis_core.py` | modify | reject mismatched ladders at its boundary |
| `app.py` | modify | use the ladder (4 sites); `/healthz`; `/api/restart`; `/api/qbench-credentials`; argv; debug off; supervised restart; file logging |
| `paths.py` | **create** | every on-disk state location, legacy vs deployed |
| `settings.py`, `notifications.py`, `instance.py` | modify | take their locations from `paths.py`; `PORT` env precedence |
| `version.py` | **create** | read `VERSION` |
| `supervisor.py`, `restart_update.py` | **create** (copied from `../coa-reviewer`) | process/port primitives; restart→switch handshake |
| `qbench_client.py`, `qbench_secrets.py` | modify | lazy credential resolution; `save_default()` |
| `templates/index.html`, `static/js/app.js`, `static/css/style.css` | modify | version badge; Restart button; QBench API settings section |
| `requirements.txt` | modify | pin versions |
| `.github/workflows/release.yml` | **create** | tag → zip + sha256 release |
| `RELEASING.md`, `CLAUDE.md`, `DEPLOY.md` | create/modify | docs |
| `tests/test_calibration_ladder.py`, `tests/test_paths.py`, `tests/test_healthz.py`, `tests/test_restart_update.py`, `tests/test_qbench_credentials.py`, `tests/test_release_package.py`, `tests/bootapp.py` | **create** | tests + subprocess boot helper |

---

### Task 1: Calibration ladder (fixes the Analysis / Export / QBench crash)

**Files:**
- Modify: `distill.py` (add after `anchors_for`, ~line 348)
- Modify: `analysis_core.py:345-365` (`carbon_to_time`, `segment_carbon_range`)
- Modify: `app.py` at the four sites: `~896-903` (`_generate_analysis_report_pdf`), `~2708-2714` (`api_analysis`), `~2942-2948` (`_run_export_analysis`), `~3314-3320` (QBench upload worker)
- Test: `tests/test_calibration_ladder.py`

- [ ] **Step 1: Write the failing tests**

```python
"""calibration_ladder: (time, carbon) pairs for Analysis / reports.

Regression for the 2026-09-16 crash: auto-detect found 24 peaks on the new
350 °C calibration (20 alkanes + CS2 + 3 impurities) and they were paired by
position with the fixed 20-entry N_ALKANE_CARBON list -> np.interp raised
'fp and xp are not of the same length' and every Analysis/Export 500'd.
"""
import json
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import analysis_core
import distill


def _assignments(cal_path, entries):
    return json.dumps({distill._cal_key(cal_path): entries})


class CalibrationLadderTests(unittest.TestCase):
    def setUp(self):
        self.cal = Path("/tmp/fake_cal.CDF")
        # 24 peaks: CS2 solvent, C5..C24, and 3 impurities, as in 002F0301.CDF
        self.entries = [{"rt": 0.30, "ignore": True}]  # CS2
        self.entries += [{"rt": 0.50 + 0.25 * i, "carbon": 5 + i} for i in range(20)]
        self.entries += [{"rt": 1.13, "ignore": True},
                         {"rt": 2.61, "ignore": True},
                         {"rt": 4.07, "ignore": True}]

    def test_uses_saved_assignments_and_drops_ignored_peaks(self):
        conf = {"calibration_cdf": str(self.cal),
                "calibration_assignments": _assignments(self.cal, self.entries)}
        times, carbons = distill.calibration_ladder(conf)
        self.assertEqual(len(times), len(carbons))
        self.assertEqual(carbons, list(range(5, 25)))
        self.assertAlmostEqual(times[0], 0.50)
        self.assertEqual(times, sorted(times))

    def test_ladder_feeds_segment_carbon_range_without_error(self):
        conf = {"calibration_cdf": str(self.cal),
                "calibration_assignments": _assignments(self.cal, self.entries)}
        times, carbons = distill.calibration_ladder(conf)
        # 0.50 min is C5 exactly, 0.75 min is C6: the solvent is NOT C5
        self.assertEqual(analysis_core.segment_carbon_range(0.50, 0.75, times, carbons), "C5-C6")

    def test_falls_back_to_auto_detect_truncated_to_known_carbons(self):
        conf = {"calibration_cdf": str(self.cal), "calibration_assignments": ""}
        peaks = [0.1 * i for i in range(1, 25)]  # 24 detected peaks
        with mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(distill, "calibration_peak_times", return_value=peaks):
            times, carbons = distill.calibration_ladder(conf)
        self.assertEqual(len(times), len(carbons))
        self.assertEqual(len(carbons), len(distill.N_ALKANE_CARBON))

    def test_no_calibration_configured_returns_empty(self):
        self.assertEqual(distill.calibration_ladder({"calibration_cdf": ""}), ([], []))

    def test_unreadable_calibration_returns_empty_not_raise(self):
        conf = {"calibration_cdf": str(self.cal), "calibration_assignments": ""}
        with mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(distill, "calibration_peak_times", side_effect=OSError("bad")):
            self.assertEqual(distill.calibration_ladder(conf), ([], []))


class AnalysisCoreBoundaryTests(unittest.TestCase):
    def test_mismatched_ladder_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "calibration ladder"):
            analysis_core.segment_carbon_range(1.0, 2.0, [0.5, 1.0, 1.5], [5, 6])
        with self.assertRaisesRegex(ValueError, "calibration ladder"):
            analysis_core.carbon_to_time(6.0, [0.5, 1.0, 1.5], [5, 6])


class AppUsesLadderTests(unittest.TestCase):
    """AST/source guard: the positional pairing must not come back."""
    def test_app_has_no_positional_n_alkane_pairing(self):
        src = Path(__file__).resolve().parent.parent.joinpath("app.py").read_text(encoding="utf-8")
        self.assertNotIn("cal_carbons: list[int] = list(distill.N_ALKANE_CARBON)", src)
        self.assertGreaterEqual(src.count("distill.calibration_ladder("), 4)
```

- [ ] **Step 2: Run and see it fail**

Run: `.venv/bin/python -m pytest tests/test_calibration_ladder.py -v`
Expected: FAIL with `AttributeError: module 'distill' has no attribute 'calibration_ladder'`. The boundary tests also fail, because `analysis_core` currently truncates silently.

- [ ] **Step 3: Implement `distill.calibration_ladder`** (insert after `anchors_for`)

```python
def calibration_ladder(conf: Dict[str, str],
                       sensitivity: float | None = None) -> Tuple[list, list]:
    """Return ``(times, carbons)`` for labelling carbon ranges on a trace.

    Uses the operator's saved peak->carbon assignments for the configured
    calibration CDF (the same source the distillation math uses), so ignored
    peaks (CS2, impurities) never get a carbon number. Falls back to
    sequential auto-detection only when no assignments are saved, truncated
    to the known n-alkane ladder so the two lists are always the same length.
    Returns ``([], [])`` when no calibration is configured or it can't be read.
    """
    cal_cdf = (conf.get("calibration_cdf") or "").strip()
    if not cal_cdf:
        return [], []
    cal_path = Path(cal_cdf)
    amap = parse_assignment_map(conf.get("calibration_assignments", ""))
    entries = amap.get(_cal_key(cal_path)) or amap.get(str(cal_path)) or []
    pairs = []
    seen = set()
    for e in entries:
        if not isinstance(e, dict) or e.get("ignore"):
            continue
        if e.get("carbon") is None or e.get("rt") is None:
            continue
        c = int(e["carbon"])
        if c in seen:
            continue
        seen.add(c)
        pairs.append((float(e["rt"]), c))
    if len(pairs) >= 2:
        pairs.sort()
        return [p[0] for p in pairs], [p[1] for p in pairs]
    if not cal_path.is_file():
        return [], []
    try:
        sens = float(sensitivity if sensitivity is not None
                     else conf.get("calibration_sensitivity", 50) or 50)
        peaks = calibration_peak_times(cal_path, sens)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Calibration ladder: could not read %s: %s", cal_path, exc)
        return [], []
    n = min(len(peaks), len(N_ALKANE_CARBON))
    return [float(t) for t in peaks[:n]], list(N_ALKANE_CARBON[:n])
```

- [ ] **Step 4: Replace the silent truncation in `analysis_core.py`**

```python
def _ladder(cal_times, cal_carbons):
    if len(cal_times) != len(cal_carbons):
        raise ValueError(
            f"calibration ladder mismatch: {len(cal_times)} peak times vs "
            f"{len(cal_carbons)} carbon numbers - use distill.calibration_ladder()")
    return np.array(cal_times, dtype=float), np.array(cal_carbons, dtype=float)


def carbon_to_time(carbon_number, cal_times, cal_carbons) -> float:
    ct, cn = _ladder(cal_times, cal_carbons)
    return float(np.interp(carbon_number, cn, ct))


def segment_carbon_range(t_start, t_end, cal_times, cal_carbons) -> str:
    ct, cn = _ladder(cal_times, cal_carbons)
    c_start = np.interp(t_start, ct, cn)
    c_end = np.interp(t_end, ct, cn)
    return f"C{int(round(c_start))}-C{int(round(c_end))}"
```

Keep the existing type hints (`list[float]`, `list[int]`) on the two public functions.

- [ ] **Step 5: Replace the four app.py blocks.** Each site currently looks like this:

```python
cal_cdf = conf.get("calibration_cdf", "").strip()
cal_times: list[float] = []
cal_carbons: list[int] = list(distill.N_ALKANE_CARBON)
if cal_cdf and Path(cal_cdf).is_file():
    try:
        cal_times = distill.calibration_peak_times(Path(cal_cdf))
    except Exception as exc:
        LOGGER.warning(...)
```

Replace each with:

```python
cal_times, cal_carbons = distill.calibration_ladder(conf)
```

Keep any later uses of `cal_cdf` at a site (grep that site's function body first). In the PDF generator, the label loop's `if ci < len(cal_carbons)` fallback may stay; it is now always true.

- [ ] **Step 6: Run the tests.** `.venv/bin/python -m pytest tests/test_calibration_ladder.py -v` should PASS, then run the full suite.

- [ ] **Step 7: Real-data check.** If `/Volumes/Labsharedrive` is mounted, run `calibration_ladder` against the real settings and CDF. Take the settings file from the share user's home, or build `conf` from `calibration_cdf` = the path of `002F0301.CDF` with empty assignments. Print the lengths and confirm they are equal. Report the result; skip this step if the share is not mounted.

- [ ] **Step 8: Commit**

```bash
git add distill.py analysis_core.py app.py tests/test_calibration_ladder.py
git commit -m "Label carbon ranges from saved calibration assignments

Analysis, Export Report and Export to QBench 500'd since the 9/16 calibration
file: auto-detect found 24 peaks and paired them by position with the
20-entry n-alkane list. One calibration_ladder() now serves all four call
sites, uses the operator's saved assignments (ignored peaks dropped), and
analysis_core rejects a mismatched ladder instead of silently truncating."
```

---

### Task 2: `paths.py` and relocating state

**Files:**
- Create: `paths.py`
- Modify: `settings.py:22` (CONFIG_PATH), `settings.py:27-43` (DEFAULTS paths), `settings.py:128-131` (load_settings fallbacks)
- Modify: `notifications.py:23` (`DEFAULT_PATH`)
- Modify: `app.py:1772` (`_DIR_CACHE_PATH`)
- Modify: `instance.py` `resolve_port`: `PORT` env comes first
- Test: `tests/test_paths.py`, plus a new case in `tests/test_instance.py`

- [ ] **Step 1: Write failing tests**

```python
"""paths.py: every on-disk state location, legacy vs deployed (GC_DATA_DIR)."""
import importlib
import os
import unittest
from pathlib import Path
from unittest import mock

import paths


class LegacyModeTests(unittest.TestCase):
    def test_no_env_is_legacy(self):
        self.assertIsNone(paths.data_dir(env={}))

    def test_legacy_settings_path_is_home_file(self):
        self.assertEqual(paths.settings_file(env={}),
                         Path.home() / ".gc_viewer_settings.json")

    def test_legacy_settings_path_is_per_port(self):
        self.assertEqual(paths.settings_file(env={"GC_PORT": "5570"}),
                         Path.home() / ".gc_viewer_settings-5570.json")

    def test_legacy_defaults_are_cwd_relative(self):
        self.assertEqual(paths.default_results_csv(env={}), Path.cwd() / "distill_results.csv")
        self.assertEqual(paths.default_processed_dir(env={}), Path.cwd() / "processed_cdf")
        self.assertEqual(paths.default_export_dir(env={}), Path.cwd() / "exports")

    def test_legacy_home_files(self):
        self.assertEqual(paths.dir_cache_file(env={}), Path.home() / ".gc_viewer_dircache.json")
        self.assertEqual(paths.notifications_file(env={}), Path.home() / ".gc_viewer_notifications.json")
        self.assertEqual(paths.standards_dir(env={}), Path.home() / "gc_comparison_standards")
        self.assertIsNone(paths.log_file(env={}))


class DeployedModeTests(unittest.TestCase):
    def setUp(self):
        self.d = Path("/srv/gcdata")
        self.env = {"GC_DATA_DIR": str(self.d), "GC_PORT": "5570"}

    def test_everything_under_data_dir(self):
        e = self.env
        self.assertEqual(paths.data_dir(env=e), self.d)
        self.assertEqual(paths.settings_file(env=e), self.d / "settings.json")  # port ignored
        self.assertEqual(paths.default_results_csv(env=e), self.d / "distill_results.csv")
        self.assertEqual(paths.default_processed_dir(env=e), self.d / "processed_cdf")
        self.assertEqual(paths.default_export_dir(env=e), self.d / "exports")
        self.assertEqual(paths.dir_cache_file(env=e), self.d / "dircache.json")
        self.assertEqual(paths.notifications_file(env=e), self.d / "notifications.json")
        self.assertEqual(paths.standards_dir(env=e), self.d / "gc_comparison_standards")
        self.assertEqual(paths.log_file(env=e), self.d / "app.log")

    def test_blank_env_value_is_legacy(self):
        self.assertIsNone(paths.data_dir(env={"GC_DATA_DIR": "  "}))


class SettingsUsesPathsTests(unittest.TestCase):
    def test_settings_module_follows_gc_data_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"GC_DATA_DIR": tmp}):
            import settings
            s = importlib.reload(settings)
            try:
                self.assertEqual(s.CONFIG_PATH, Path(tmp) / "settings.json")
                self.assertEqual(s.DEFAULTS["distill_output"], str(Path(tmp) / "distill_results.csv"))
                self.assertEqual(s.DEFAULTS["processed_cdf_dir"], str(Path(tmp) / "processed_cdf"))
                self.assertEqual(s.DEFAULTS["export_folder"], str(Path(tmp) / "exports"))
                self.assertEqual(s.DEFAULTS["comparison_defaults_dir"],
                                 str(Path(tmp) / "gc_comparison_standards"))
            finally:
                os.environ.pop("GC_DATA_DIR", None)
                importlib.reload(settings)
```

Add to `tests/test_instance.py`:

```python
    def test_port_env_from_updater_wins(self):
        self.assertEqual(instance.resolve_port(argv=["--port", "5561"],
                                               env={"PORT": "5580", "GC_PORT": "5562"}), 5580)
```

(Place it in the existing resolve_port test class and match its style.)

- [ ] **Step 2: Run and see them fail.** `ModuleNotFoundError: paths`, and 5561 != 5580.

- [ ] **Step 3: Create `paths.py`**

```python
"""paths.py — where every piece of runtime state lives.

Two modes, decided by ``GC_DATA_DIR``:

* **deployed** (set, e.g. by the ASAPSV1 updater to ``C:\\ASAPApps\\gc\\data``):
  all state under that one folder, so a release directory stays immutable and
  a deploy can never delete results.
* **legacy** (unset): exactly the historic locations — home-dir settings per
  port, cwd-relative results — so copies still running from the share are
  unaffected.

Stdlib only, no import-time side effects; every function takes ``env`` so
tests don't touch ``os.environ``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

import instance

DATA_ENV = "GC_DATA_DIR"


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def data_dir(env=None) -> Optional[Path]:
    raw = (_env(env).get(DATA_ENV) or "").strip()
    return Path(raw) if raw else None


def settings_file(env=None) -> Path:
    d = data_dir(env)
    return d / "settings.json" if d else instance.settings_path(env=_env(env))


def default_results_csv(env=None) -> Path:
    d = data_dir(env)
    return (d if d else Path.cwd()) / "distill_results.csv"


def default_processed_dir(env=None) -> Path:
    d = data_dir(env)
    return (d if d else Path.cwd()) / "processed_cdf"


def default_export_dir(env=None) -> Path:
    d = data_dir(env)
    return (d if d else Path.cwd()) / "exports"


def standards_dir(env=None) -> Path:
    d = data_dir(env)
    return (d if d else Path.home()) / "gc_comparison_standards"


def dir_cache_file(env=None) -> Path:
    d = data_dir(env)
    return d / "dircache.json" if d else Path.home() / ".gc_viewer_dircache.json"


def notifications_file(env=None) -> Path:
    d = data_dir(env)
    return d / "notifications.json" if d else Path.home() / ".gc_viewer_notifications.json"


def log_file(env=None) -> Optional[Path]:
    d = data_dir(env)
    return d / "app.log" if d else None
```

Note: `instance.settings_path(env=...)` already accepts `env`. Check its signature. If it doesn't, add an `env=None` passthrough with a test.

- [ ] **Step 4: Wire the modules.**
  - `settings.py`: `import paths`; `CONFIG_PATH: Path = paths.settings_file()`.
  - In `DEFAULTS`: `"watch_dir": ""` in deployed mode, keeping `str(Path.cwd())` in legacy mode. Write it as `str(Path.cwd()) if paths.data_dir() is None else ""`. **Why:** in deployed mode, cwd is the release folder. Watching it would treat the app's own files as instrument data, and an empty `watch_dir` is what keeps the Looker off (Task 3).
  - `"processed_cdf_dir": str(paths.default_processed_dir())`, `"distill_output": str(paths.default_results_csv())`, `"blank_cache_file": str(paths.default_processed_dir() / ".blank_cache.json")`, `"export_folder": str(paths.default_export_dir())`, `"comparison_defaults_dir": str(paths.standards_dir())`.
  - In `load_settings()`, replace `Path.cwd()` in the `processed_cdf_dir` and `export_folder` fallbacks with the `paths.` equivalents.
  - `_seed_new_instance()`: return immediately when `paths.data_dir()` is set. There are no per-port files in deployed mode.
  - `notifications.py`: `DEFAULT_PATH = paths.notifications_file()`.
  - `app.py:1772`: `_DIR_CACHE_PATH = paths.dir_cache_file()`, with `import paths` added near `import instance`.
  - `instance.resolve_port`: before the argv loop, add `raw = (env.get("PORT") or "").strip()` and `if raw: return validate_port(raw)`. Update its docstring: `PORT` (set by the updater) → `--port` → `GC_PORT` → default.

- [ ] **Step 5: Run the tests.** The new tests pass. Run the full suite; `tests/test_settings.py` must still pass (it redirects `CONFIG_PATH`). Also run `grep -rn "Path.home()\|Path.cwd()" *.py` and check that every remaining hit is either in `paths.py`/`instance.py` or has a comment explaining why it stays.

- [ ] **Step 6: Commit** — "Put all runtime state under GC_DATA_DIR when deployed"

---

### Task 3: Production-safe startup (argv, debug off, file log, unconfigured boot)

**Files:**
- Modify: `app.py` (top, near `GC_PORT = instance.resolve_port()`; logging setup ~line 103; `_init_app` ~3688; `__main__` ~3726; `_get_looker`/`_start_watcher`)
- Create: `tests/bootapp.py` (subprocess boot helper, reused by later tasks)
- Test: `tests/test_startup.py`

- [ ] **Step 1: Write the helper and failing tests**

`tests/bootapp.py`:

```python
"""Boot the real app.py in a subprocess, the way the ASAPSV1 updater does.

app.py cannot be imported in-process (import starts threads), so route-level
tests launch it: cwd = repo root, PORT + GC_DATA_DIR in env, HOME redirected
so nothing leaks into the real home directory.
"""
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port, path, timeout=3.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def post(port, path, body, timeout=10.0):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


@contextlib.contextmanager
def booted(tmp: Path, *, args=("--no-tray",), extra_env=None, wait=60.0):
    data = tmp / "data"
    home = tmp / "home"
    data.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    port = free_port()
    env = dict(os.environ)
    env.update({"GC_DATA_DIR": str(data), "PORT": str(port), "HOME": str(home),
                "USERPROFILE": str(home), "APPDATA": str(home / "AppData"),
                "QBENCH_STORE_PATH": str(home / "qbench.json")})
    env.pop("GC_PORT", None)
    env.pop("QBENCH_CLIENT_ID", None)
    env.pop("QBENCH_CLIENT_SECRET", None)
    env.update(extra_env or {})
    log = open(tmp / "boot.log", "w")
    proc = subprocess.Popen([sys.executable, "app.py", *args], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + wait
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"app exited {proc.returncode}: {(tmp / 'boot.log').read_text()[-3000:]}")
            try:
                code, _ = get(port, "/healthz", timeout=1.0)
                if code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError(f"no /healthz within {wait}s: {(tmp / 'boot.log').read_text()[-3000:]}")
        yield port, proc, data, home
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
```

Note: `booted()` waits for `/healthz`, which Task 4 adds. For Task 3, write a minimal `/healthz` returning `{"status": "ok"}` in this task. Task 4 extends it under its own test.

`tests/test_startup.py`:

```python
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.bootapp import ROOT, booted, get

try:
    import flask, netCDF4  # noqa: F401
    HAVE_DEPS = True
except Exception:
    HAVE_DEPS = False


@unittest.skipUnless(HAVE_DEPS, "needs flask + netCDF4")
class StartupTests(unittest.TestCase):
    def test_boots_with_empty_data_dir_and_writes_nothing_to_home(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                code, body = get(port, "/healthz")
                self.assertEqual(code, 200)
                self.assertEqual(body["status"], "ok")
                self.assertTrue((data / "app.log").is_file())
                self.assertTrue((data / "settings.json").is_file() or True)
            leaked = [p.relative_to(home) for p in home.rglob("*") if p.is_file()]
            self.assertEqual(leaked, [], f"state leaked into HOME: {leaked}")

    def test_debugger_is_off_by_default(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                pass
            log = (Path(t) / "boot.log").read_text()
            self.assertNotIn("Debugger is active", log)

    def test_unknown_flag_fails_fast(self):
        env = dict(os.environ, GC_DATA_DIR=tempfile.mkdtemp(), PORT="1")
        r = subprocess.run([sys.executable, "app.py", "--no-such-flag"], cwd=ROOT,
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--no-such-flag", r.stderr + r.stdout)
```

The `or True` in the first test marks settings creation as optional. Remove that line if you don't create `settings.json` on boot. `tests/__init__.py` may be needed for `from tests.bootapp import`. Check how the existing tests import modules (they probably add the repo root to `sys.path` via a conftest). If no `tests/__init__.py` exists, use `from bootapp import ...` and add `tests/` to `sys.path` in the test file.

- [ ] **Step 2: Run and see them fail.** Expected failures: no `/healthz` (timeout), no `app.log`, "Debugger is active" in the log.

- [ ] **Step 3: Implement in `app.py`**
  - **Argument parsing, before `instance.resolve_port()`:**

    ```python
    import argparse
    _ap = argparse.ArgumentParser(add_help=True)
    _ap.add_argument("--port", type=int)
    _ap.add_argument("--dev", action="store_true", help="Flask debug mode (never in production)")
    _ap.add_argument("--no-tray", action="store_true", help="accepted for updater compatibility")
    ARGS, _unknown = _ap.parse_known_args()
    ```

    **Only when `__name__ == "__main__"`**, error on `_unknown`: `_ap.error(f"unrecognized arguments: {' '.join(_unknown)}")`. The legacy `run.pyw` bootstrap runs `app.py` via `runpy` with `-c`, and its `sys.argv` must not be rejected. Check what argv looks like under `run.pyw` (`runpy.run_path(..., run_name="__main__")` with `sys.argv` = `['-c']`) and make sure it passes: only reject arguments that start with `--`.
  - **Logging:** after `basicConfig`, if `paths.log_file()` is set, create the parent folder and add a `logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")` with the same format to the root logger.
  - **Watcher guard:** in `_init_app._bg_init`, before `_get_looker()`, read the settings. If `watch_dir` is empty or not a directory, `LOGGER.warning("Watcher not started: watch_dir %r is not set or missing - configure it in Settings", wd)` and skip the Looker and watcher. Find where settings are saved (`/api/settings` POST). If a valid `watch_dir` is saved later, call the existing Looker start/refresh path so no restart is needed. Grep for how `/api/settings` currently re-applies the watch dir and follow it.
  - **Boot directories:** in deployed mode, `mkdir(parents=True, exist_ok=True)` the processed and export defaults at startup.
  - **`__main__`:** `app.run(host="0.0.0.0", port=GC_PORT, debug=ARGS.dev, threaded=True, use_reloader=False)`.
  - **Minimal `/healthz`:**

    ```python
    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok"})
    ```

- [ ] **Step 4: Run the tests.** The new tests pass; run the full suite. Then run it by hand: `GC_DATA_DIR=/tmp/gcd PORT=5599 .venv/bin/python app.py --no-tray`, check that `curl localhost:5599/` serves the page, and Ctrl-C.

- [ ] **Step 5: Commit** — "Boot safely under the updater: no debugger, file log, unconfigured start"

---

### Task 4: VERSION, `/healthz` contract, version badge

**Files:**
- Create: `version.py`
- Modify: `app.py` (`/healthz`; exclude it from activity tracking; pass the version to the template)
- Modify: `templates/index.html` and `templates/calibration.html` (badge), `static/css/style.css`
- Modify: `.gitignore` (add `VERSION`)
- Test: `tests/test_healthz.py`

- [ ] **Step 1: Failing tests**

```python
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import version
from tests.bootapp import ROOT, booted, get


class VersionFileTests(unittest.TestCase):
    def test_reads_first_line(self):
        with tempfile.TemporaryDirectory() as t:
            Path(t, "VERSION").write_text("v1.2.3\nbuilt by CI\n")
            self.assertEqual(version.read_version(Path(t)), "v1.2.3")

    def test_missing_is_dev(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(version.read_version(Path(t)), "dev")


class HealthzTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                code, body = get(port, "/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["version"], version.read_version(ROOT))
        for k in ("pid", "active_sessions", "idle_seconds"):
            self.assertIn(k, body)
        self.assertIsInstance(body["idle_seconds"], (int, float))

    def test_healthz_does_not_count_as_activity(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                import time
                time.sleep(2.2)
                get(port, "/healthz")
                _, body = get(port, "/healthz")
        self.assertGreaterEqual(body["idle_seconds"], 2)
        self.assertEqual(body["active_sessions"], 0)

    def test_page_shows_version_badge(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
        self.assertIn('id="app-version"', html)
        self.assertIn(version.read_version(ROOT), html)
```

- [ ] **Step 2: See them fail.**

- [ ] **Step 3: Implement**
  - `version.py`:

    ```python
    """The running release tag. CI writes VERSION into the release zip; a
    checkout has none and reports "dev" (correct, and never blank)."""
    from pathlib import Path

    APP_DIR = Path(__file__).resolve().parent


    def read_version(directory: Path = APP_DIR) -> str:
        try:
            line = (Path(directory) / "VERSION").read_text(encoding="utf-8").splitlines()[0].strip()
            return line or "dev"
        except (OSError, IndexError):
            return "dev"


    APP_VERSION = read_version()
    ```

  - Activity tracking in `app.py`. Add `_NON_ACTIVITY_PATHS = {"/healthz"}` and `_recent_clients: dict[str, float] = {}`. In `_track_activity`, return early when `request.path in _NON_ACTIVITY_PATHS` or the path starts with `/static/` or ends in `/stream` (SSE keep-alives are not a person). Otherwise update `_last_activity` and set `_recent_clients[request.remote_addr] = now`, all under `_last_activity_lock`.
  - `/healthz`:

    ```python
    @app.route("/healthz")
    def healthz():
        """Updater health contract (see coa-reviewer/RELEASING.md): 200 + status
        ok + version == tag. No auth, no outbound calls, not activity."""
        now = time.time()
        with _last_activity_lock:
            idle = now - _last_activity
            active = sum(1 for t in _recent_clients.values() if now - t < 300)
        return jsonify({"status": "ok", "version": version.APP_VERSION, "pid": os.getpid(),
                        "active_sessions": active, "idle_seconds": round(idle, 1)})
    ```

  - Badge: find how `/` renders `index.html` (`render_template` or `send_file`). If it uses `send_file`/static, switch to `render_template("index.html", app_version=version.APP_VERSION)` and confirm the template has no Jinja-conflicting `{{` (grep first; wrap conflicting blocks in `{% raw %}`). Before `</body>`, add `<div id="app-version" aria-hidden="true">{{ app_version }}</div>`. Do the same in `calibration.html`.
  - CSS, copied from COA (`coa-reviewer/static/css/app.css:2673-2693`), adapted to this stylesheet's variables:

    ```css
    #app-version {
      position: fixed; bottom: 6px; right: 10px; z-index: 9999;
      font-size: 10px; opacity: .55; pointer-events: none; user-select: none;
      font-variant-numeric: tabular-nums;
    }
    @media print { #app-version { display: none; } }
    ```

  - `.gitignore`: add the line `VERSION`.

- [ ] **Step 4: Tests pass.** Remove Task 3's minimal `/healthz` (replaced). Full suite.

- [ ] **Step 5: Commit** — "Add /healthz contract, VERSION and bottom-right version badge"

---

### Task 5: supervisor.py, restart_update.py and Restart-installs-update

**Files:**
- Create: `supervisor.py`, `restart_update.py`, both copied **byte-for-byte** from `/Users/rynatical/Projects/coa-reviewer/`
- Modify: `app.py` (`_do_restart`, `/api/restart`, startup tidy)
- Modify: `templates/index.html`, `static/js/app.js` (Restart button in the Settings modal)
- Test: `tests/test_restart_update.py`

First read these in `coa-reviewer`: `restart_update.py` (whole file), `app.py` around `request_restart` (~2759), `_await_switch` (~2865), `_may_respawn` (~3011), `_tidy_after_last_run` (~1082) and `/api/restart` (~4077), and `tests/` for any restart_update tests. Port the behaviour, not COA's tray or session code.

- [ ] **Step 1: Failing tests**

```python
import hashlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COA = ROOT.parent / "coa-reviewer"


class SharedModulesTests(unittest.TestCase):
    """The updater imports supervisor.py from whichever app it finds first,
    so every app must ship the identical file."""
    @unittest.skipUnless((COA / "supervisor.py").is_file(), "coa-reviewer checkout not beside gc-hub")
    def test_supervisor_identical_to_coa(self):
        for name in ("supervisor.py", "restart_update.py"):
            ours = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            theirs = hashlib.sha256((COA / name).read_bytes()).hexdigest()
            self.assertEqual(ours, theirs, f"{name} drifted from coa-reviewer")


class RestartDecisionTests(unittest.TestCase):
    def test_supervised_restart_exits_without_spawning(self):
        import restart_policy
        self.assertEqual(restart_policy.restart_mode(env={"GC_DATA_DIR": "C:/x"}), "exit")
        self.assertEqual(restart_policy.restart_mode(env={}), "respawn")

    def test_respawn_blocked_while_switching(self):
        import tempfile, restart_policy
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(restart_policy.may_respawn(Path(t)))
            Path(t, "switching").write_text("")
            self.assertFalse(restart_policy.may_respawn(Path(t)))
            Path(t, "switching").unlink()
            Path(t, "switch-accepted").write_text("{}")
            self.assertFalse(restart_policy.may_respawn(Path(t)))
```

Add a subprocess test using `booted()`: write `staged.json` in the data dir with `{"tag": "v9.9.9", "healthy": true, ...}`, in the exact shape `restart_update.staged_update` reads (copy the shape from COA's tests). Then `POST /api/restart` with `{"dry_run": true}` and assert the response is `{"mode": "switch", "tag": "v9.9.9"}`. Without `staged.json`, assert `{"mode": "restart"}`. `dry_run` only reports the decision, so the test process isn't killed. Keep `dry_run` in the route; it is also useful to the UI for labelling the button.

- [ ] **Step 2: See them fail.**

- [ ] **Step 3: Implement**
  - `cp ../coa-reviewer/supervisor.py ../coa-reviewer/restart_update.py .`
  - `restart_policy.py`: small, stdlib-only, testable.

    ```python
    """How this process may restart itself.

    Under the updater (GC_DATA_DIR set) the updater supervises the port and
    relaunches within ~20 s, so the app must only EXIT — spawning its own
    replacement would race the updater for the port. Legacy (share) mode
    keeps the historic spawn-then-exit.
    """
    import os
    from pathlib import Path

    BLOCKING = ("switching", "switch-accepted")


    def restart_mode(env=None) -> str:
        env = os.environ if env is None else env
        return "exit" if (env.get("GC_DATA_DIR") or "").strip() else "respawn"


    def may_respawn(data_dir: Path) -> bool:
        return not any((Path(data_dir) / n).exists() for n in BLOCKING)
    ```

  - `app.py` `_do_restart`: if `restart_policy.restart_mode() == "exit"`, log and `os._exit(0)` after the 1-second grace period. Otherwise, only spawn if `restart_policy.may_respawn(paths.data_dir() or BASE_DIR)`.
  - `app.py` startup, in deployed mode: `restart_update.clear_switch_files(paths.data_dir())`, wrapped in try/except with a logged warning.
  - `POST /api/restart`, body `{"dry_run": bool}`:
    - `staged = restart_update.staged_update(data_dir, version.APP_VERSION)`. Match COA's exact signature and return shape.
    - If `staged` is newer and healthy: `mode="switch"`. Unless `dry_run`, call `write_switch_request(data_dir, staged["tag"], by=request.remote_addr, now=time.time())`, then start a thread that polls `read_switch_outcome` for up to `PICKUP_SECONDS`. On `accepted`, do nothing: the updater stops this process. On `refused` or timeout, call `withdraw_switch_request`, then `_do_restart()`.
    - Otherwise `mode="restart"`, and unless `dry_run` start the thread `_do_restart()`.
    - Return `{"mode": mode, "tag": staged tag or None}`.
    - In legacy mode (no data dir), always return `{"mode": "restart"}`.
  - UI: add to the Settings modal footer `<button class="btn" id="btn-restart">Restart</button>`. On opening Settings, `POST /api/restart {"dry_run": true}`, and if `mode == "switch"` set the label to `Restart & install ${tag}`. On click, `confirm()`, then POST without `dry_run` and show "Restarting… this page will reconnect". Poll `/healthz` every 2 s and reload when it answers with a different `pid`.

- [ ] **Step 4: Tests pass**, full suite.

- [ ] **Step 5: Commit** — "Restart installs a staged update via the updater handshake"

---

### Task 6: QBench API credentials in the UI

**Files:**
- Modify: `qbench_client.py:21-22,60-70` (lazy), `qbench_secrets.py` (add `save_default`, `describe`)
- Modify: `app.py` (two routes, with a shared `_check_admin`)
- Modify: `templates/index.html` (Settings section), `static/js/app.js`
- Test: `tests/test_qbench_credentials.py`

- [ ] **Step 1: Failing tests**

```python
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import qbench_secrets


class SaveDefaultTests(unittest.TestCase):
    def test_writes_and_preserves_profiles(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t, "sub", "qbench.json")
            p.parent.mkdir()
            p.write_text(json.dumps({"client_id": "old", "client_secret": "old",
                                     "profiles": {"legacy": {"client_id": "L", "client_secret": "LS"}}}))
            with mock.patch.dict(os.environ, {"QBENCH_STORE_PATH": str(p)}):
                qbench_secrets.save_default("new-id-1234", "new-secret")
                data = json.loads(p.read_text())
                self.assertEqual(data["client_id"], "new-id-1234")
                self.assertEqual(data["client_secret"], "new-secret")
                self.assertEqual(data["profiles"]["legacy"]["client_id"], "L")
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_creates_missing_store(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t, "new", "qbench.json")
            with mock.patch.dict(os.environ, {"QBENCH_STORE_PATH": str(p)}):
                qbench_secrets.save_default("id-abcd", "s")
                self.assertTrue(p.is_file())

    def test_describe_never_returns_secret(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t, "qbench.json")
            with mock.patch.dict(os.environ, {"QBENCH_STORE_PATH": str(p)}):
                self.assertEqual(qbench_secrets.describe()["configured"], False)
                qbench_secrets.save_default("id-abcd", "TOPSECRET")
                d = qbench_secrets.describe()
                self.assertEqual(d["configured"], True)
                self.assertEqual(d["client_id_hint"], "…abcd")
                self.assertNotIn("TOPSECRET", json.dumps(d))

    def test_rejects_blank(self):
        with self.assertRaises(ValueError):
            qbench_secrets.save_default("", "x")
        with self.assertRaises(ValueError):
            qbench_secrets.save_default("x", "  ")


class LazyClientTests(unittest.TestCase):
    def test_importing_client_does_not_need_credentials(self):
        import importlib, sys
        with tempfile.TemporaryDirectory() as t, \
             mock.patch.dict(os.environ, {"QBENCH_STORE_PATH": str(Path(t, "none.json"))}):
            os.environ.pop("QBENCH_CLIENT_ID", None)
            os.environ.pop("QBENCH_CLIENT_SECRET", None)
            sys.modules.pop("qbench_client", None)
            mod = importlib.import_module("qbench_client")  # must not raise
            with self.assertRaises(qbench_secrets.QBenchSecretMissing):
                mod.QBenchAPIClient()
```

Also add route tests with `booted()`, using an isolated `QBENCH_STORE_PATH` (already set in `bootapp`):
- `GET /api/qbench-credentials` returns `configured: false`.
- A POST with the wrong password returns 403 and writes no file.
- A POST with the right password, when the token check fails, returns 400 and writes no file. To make the token call deterministic, the route reads `QBENCH_TOKEN_URL` from the env: point it at `http://127.0.0.1:1/` (connection refused) in the test.
- Stubbing a success needs a fake token server. Start a tiny `http.server` thread in the test that returns `{"access_token": "x", "expires_in": 3600}` on POST, and pass its URL as `QBENCH_TOKEN_URL` via `extra_env`. Then assert 200, that the store is written, that `GET` shows `configured: true`, and that no response body contains the secret.

- [ ] **Step 2: See them fail.**

- [ ] **Step 3: Implement**
  - `qbench_client.py`: delete the module-level `CLIENT_ID = ...` and `CLIENT_SECRET = ...` lines. Make `TOKEN_URL = os.getenv("QBENCH_TOKEN_URL", "https://asaplabs.qbench.net/qbench/oauth2/v1/token")`. In `__init__`, change the defaults to `client_id: Optional[str] = None, client_secret: Optional[str] = None`, then `self.client_id = client_id or qbench_secrets.get_client_id()` and the same for the secret. Grep the repo for `qbench_client.CLIENT_ID` and `CLIENT_SECRET` uses and fix them. `tests/test_no_hardcoded_credentials.py` and `tests/test_qbench_import.py` must still pass.
  - `qbench_secrets.py`:

    ```python
    def save_default(client_id, client_secret):
        """Write the default pair into the local store atomically, keeping any
        other keys/profiles. Never logs or returns the secret."""
        client_id = (client_id or "").strip()
        client_secret = (client_secret or "").strip()
        if not client_id or not client_secret:
            raise ValueError("Client ID and Client Secret are both required")
        path = _store_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            data = _load_store()
        except Exception:  # corrupt store: replace rather than refuse
            data = {}
        data["client_id"] = client_id
        data["client_secret"] = client_secret
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
        if os.name != "nt":
            os.chmod(path, 0o600)


    def describe():
        """Safe status for the UI: configured? + last 4 of the id. No secret."""
        try:
            data = _load_store()
        except Exception:
            data = {}
        cid = os.environ.get("QBENCH_CLIENT_ID") or data.get("client_id") or ""
        sec = os.environ.get("QBENCH_CLIENT_SECRET") or data.get("client_secret") or ""
        return {"configured": bool(cid and sec),
                "client_id_hint": ("…" + cid[-4:]) if cid else "",
                "store_path": _store_path()}
    ```

    Add both functions to `__all__`.
  - `app.py`: extract `def _check_admin(body) -> bool: return (body or {}).get("password") == "admin"` and use it at the existing `/api/save-analysis-defaults` site (same behaviour). Add a comment marking it as the spec's open item.

    ```python
    @app.route("/api/qbench-credentials", methods=["GET"])
    def api_qbench_credentials_get():
        return jsonify(qbench_secrets.describe())


    @app.route("/api/qbench-credentials", methods=["POST"])
    def api_qbench_credentials_post():
        body = request.get_json(force=True, silent=True) or {}
        if not _check_admin(body):
            return _error("Incorrect password", 403)
        cid = (body.get("client_id") or "").strip()
        sec = (body.get("client_secret") or "").strip()
        if not cid or not sec:
            return _error("Client ID and Client Secret are both required", 400)
        try:
            qbench_client.QBenchAPIClient(client_id=cid, client_secret=sec).get_access_token(force=True)
        except Exception as exc:
            LOGGER.warning("QBench credential test failed: %s", type(exc).__name__)
            return _error(f"QBench rejected these credentials: {exc}", 400)
        qbench_secrets.save_default(cid, sec)
        LOGGER.info("QBench API credentials updated from %s", request.remote_addr)
        return jsonify(qbench_secrets.describe())
    ```

    Import `qbench_secrets` and `qbench_client` at the top of app.py (both are now safe at import). Check that `get_access_token`'s exception message can't echo the secret: read its code. If it includes the request body, replace `{exc}` with a generic message plus the status code.
  - UI: add a "QBench API" fieldset to the Settings modal (`templates/index.html` ~781-888) with a status line `#qb-api-status`, `#qb-api-client-id` (text), `#qb-api-client-secret` (`type=password`, `autocomplete=new-password`, never pre-filled), `#qb-api-admin` (password), and the button `#btn-qb-api-save` "Test & Save". In `app.js`, GET on modal open fills the status line ("Configured (…abcd)" or "Not configured"). The POST shows the error text or success, and always clears the secret and password inputs afterwards. Follow the existing Settings modal's markup and JS patterns (grep `btn-settings-save`).

- [ ] **Step 4: Tests pass**, full suite, plus `node tests/js/run.js`.

- [ ] **Step 5: Commit** — "Enter QBench API credentials from Settings (tested before saved)"

---

### Task 7: Release workflow, pinned requirements, docs

**Files:**
- Create: `.github/workflows/release.yml`, `scripts/package_release.sh`, `RELEASING.md`, `DEPLOY.md`
- Modify: `requirements.txt` (pins), `CLAUDE.md`
- Test: `tests/test_release_package.py`

- [ ] **Step 1: Failing test.** The package script is the single source of truth, and the workflow calls it.

```python
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("bash") and shutil.which("rsync") and shutil.which("zip"),
                     "needs bash, rsync, zip")
class PackageTests(unittest.TestCase):
    def test_package_contains_code_and_no_state(self):
        with tempfile.TemporaryDirectory() as t:
            # plant state that must never ship
            planted = [ROOT / "distill_results.csv", ROOT / "processed_cdf" / "x.CDF",
                       ROOT / ".gc_server.pid", ROOT / "app.log", ROOT / "switch-requested"]
            made = []
            for p in planted:
                if not p.exists():
                    p.parent.mkdir(exist_ok=True)
                    p.write_text("x")
                    made.append(p)
            try:
                r = subprocess.run(["bash", "scripts/package_release.sh", "v0.0.0-test", t],
                                   cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                z = zipfile.ZipFile(Path(t, "gc-hub-v0.0.0-test.zip"))
                names = z.namelist()
                top = {n.split("/")[0] for n in names}
                self.assertEqual(top, {"gc-hub-v0.0.0-test"})
                rel = {n.split("/", 1)[1] for n in names}
                for must in ("app.py", "requirements.txt", "VERSION", "paths.py",
                             "supervisor.py", "restart_update.py", "templates/index.html"):
                    self.assertIn(must, rel)
                self.assertEqual(z.read("gc-hub-v0.0.0-test/VERSION").decode().strip(), "v0.0.0-test")
                bad = [n for n in rel if n.endswith((".csv", ".pid", ".log", ".CDF"))
                       or n.startswith(("processed_cdf", "tests/", ".git", "docs/", ".venv"))
                       or n in ("switch-requested",)]
                self.assertEqual(bad, [])
                self.assertTrue(Path(t, "gc-hub-v0.0.0-test.zip.sha256").is_file())
            finally:
                for p in made:
                    p.unlink()
                pc = ROOT / "processed_cdf"
                if pc.is_dir() and not any(pc.iterdir()):
                    pc.rmdir()
```

- [ ] **Step 2: See it fail** (no script).

- [ ] **Step 3: Implement**
  - `scripts/package_release.sh`:

    ```bash
    #!/usr/bin/env bash
    # Build dist zip for a tag: package_release.sh <tag> <outdir>
    set -euo pipefail
    TAG="$1"; OUT="$2"
    NAME="gc-hub-${TAG}"
    STAGE="$(mktemp -d)/${NAME}"
    mkdir -p "$STAGE" "$OUT"
    rsync -a \
      --exclude='.git' --exclude='.github' --exclude='dist' --exclude='.venv' --exclude='venv' \
      --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
      --exclude='tests' --exclude='docs' --exclude='scripts' \
      --exclude='processed_cdf*' --exclude='exports' --exclude='*.csv' --exclude='*.csv.bak' \
      --exclude='*.pid' --exclude='*.log' --exclude='*.log.*' --exclude='.*_cache.json' \
      --exclude='[Cc][Dd][Ff]' --exclude='*.[Cc][Dd][Ff]' --exclude='*.bak_*' \
      --exclude='switch-requested' --exclude='switch-accepted' --exclude='switch-refused' \
      --exclude='switching' --exclude='staged.json' --exclude='held-tags.json' --exclude='paused' \
      --exclude='VERSION' --exclude='.DS_Store' \
      ./ "$STAGE/"
    printf '%s\n' "$TAG" > "$STAGE/VERSION"
    for f in app.py requirements.txt VERSION paths.py supervisor.py restart_update.py templates static; do
      [ -e "$STAGE/$f" ] || { echo "missing $f in package" >&2; exit 1; }
    done
    if find "$STAGE" -iname '*.csv' -o -iname '*.pid' -o -iname '*.cdf' | grep -q .; then
      echo "state leaked into package" >&2; exit 1
    fi
    ( cd "$(dirname "$STAGE")" && zip -qr "$OLDPWD/$OUT/${NAME}.zip" "$NAME" ) 2>/dev/null \
      || ( cd "$(dirname "$STAGE")" && zip -qr "${OUT}/${NAME}.zip" "$NAME" )
    ( cd "$OUT" && sha256sum "${NAME}.zip" > "${NAME}.zip.sha256" 2>/dev/null \
      || shasum -a 256 "${NAME}.zip" > "${NAME}.zip.sha256" )
    ```

    Simplify the `zip` line so `$OUT` is resolved to an absolute path first (`OUT="$(cd "$OUT" && pwd)"` after `mkdir`). The fallback above is only illustrative. The sha256 file format must match what `updater.parse_sha256_file` expects: `<hex>  <filename>`. `shasum -a 256` prints the same format.
  - `.github/workflows/release.yml`: copy `../coa-reviewer/.github/workflows/release.yml` and replace its rsync/VERSION/zip steps with `bash scripts/package_release.sh "$TAG" dist`. Keep COA's trigger (`v*`), permissions, release-notes and `gh release create` steps, with the asset names changed to `gc-hub-<tag>.zip(.sha256)`. Before packaging, add a `pytest` step on `ubuntu-latest` with Python 3.12 and `pip install -r requirements.txt -r requirements-dev.txt`, so a red suite never releases.
  - `requirements.txt`: pin each line to the versions in `.venv` (`pip freeze`): Flask==3.1.3, numpy==2.5.3, scipy==1.18.1, pandas==3.0.6, netCDF4==1.7.4, plotly==7.1.0, kaleido==1.4.0, xhtml2pdf==0.2.20, pystray==0.19.5, Pillow==12.3.0, watchdog==6.0.0, selenium==4.49.0, webdriver-manager==4.1.2, chromedriver-autoinstaller==0.6.4, PyJWT==2.15.0, requests==2.34.2. Keep the comments.
  - `RELEASING.md`: adapt COA's (§1 pipeline diagram, §2 bump rules including "anything that changes how results are calculated, recorded or displayed is MAJOR", §3 cutting a release). Change `coa` to `gc` and the port to 5560. Note that the repo is **private**, so the updater's PAT must include it.
  - `DEPLOY.md`: the three manual steps from the spec's "Manual steps for Ryan", the exact `config.json` entry, and how to move existing settings into `C:\ASAPApps\gc\data\settings.json` (copy `~/.gc_viewer_settings.json` and adjust the paths).
  - `CLAUDE.md`: add a "Deployment" section (deployed vs legacy mode, `GC_DATA_DIR`, `/healthz`, never edit a release folder, tag to ship; see RELEASING.md). Fix the "Deployment topology" paragraph: production is moving to ASAPSV1 under the updater, while the share copy is legacy until phase 2. Mention `paths.py`, `version.py`, `restart_policy.py` and `calibration_ladder` in Architecture.

- [ ] **Step 4: Tests pass.** Full suite. Also run `bash scripts/package_release.sh v0.0.0-local /tmp/gcpkg && unzip -l /tmp/gcpkg/gc-hub-v0.0.0-local.zip | head -50` and eyeball the listing.

- [ ] **Step 5: Commit** — "Release workflow: tag -> tested, state-free zip + sha256"

---

### Task 8: End-to-end release smoke test (controller, not a subagent)

- [ ] Build the package locally, unzip it into a temp folder, and create a fresh venv from its pinned `requirements.txt`. Run it the way the updater does: `cwd=<unzipped>`, `GC_DATA_DIR=<tmp>/data`, `PORT=<free>`, argument `--no-tray`. Poll `/healthz` and assert `version == "v0.0.0-local"`. Then stop it.
- [ ] Push `main`. Open a PR? No: Ryan's repos release from `main` directly. Push the commits, tag `v1.0.0` (the first deployable release), push the tag, and watch `gh run list --workflow=release.yml` until it completes. `gh release view v1.0.0` must show the zip and sha256.
- [ ] Report the remaining manual steps (PAT scope, config entry, rotating credentials, the share stopgap for the calibration fix).
