# Phase 1 — Deployment infrastructure (COA Reviewer parity) — Design

Date: 2026-09-25
Repo: `ASAP-Labs-LLC/gc-hub` (extracted from `gc-data` →
`GC2025/GC 2026.5 2887 Advanced analysis/webapp`, history preserved, QBench
literals scrubbed from history).

## Roadmap this phase belongs to

| Phase | What | Status |
|---|---|---|
| **1** | **Deploy like COA Reviewer: tag → release → updater on ASAPSV1; QBench credential UI; calibration crash fix** | **this doc** |
| 2 | One hub app on ASAPSV1, instruments as config (own calibration / blank / corrections / results), GC PCs **push** CDFs over HTTP with a small agent; `method` is a pluggable field (D2887 only today) | next |
| 3 | Deviation bullets derived from the difference graph (trend + range overlays), merged and capped | |
| 4 | Frequently-used comment presets | |
| 5 | UI rebuilt in the COA Reviewer style (monochrome, spacious) | |

Ryan ranked phase 1 as priority number one: "same infrastructure as the COA
reviewer (restart update detection from github, not editing the code
directly)".

## Goal

After this phase, a `vX.Y.Z` tag on `gc-hub` becomes a running, health-checked
app on ASAPSV1 with no hand-editing. The pipeline is the same one COA Reviewer
uses (`coa-reviewer/RELEASING.md` §1). The updater itself is unchanged; gc is
one more entry in its `config.json`.

## Contract the updater imposes (from `coa-reviewer/deploy/updater/updater.py`)

- The release zip has one top-level folder and contains `app.py`,
  `requirements.txt` (pinned), `VERSION`, `templates/` and `static/`.
- The app is launched as `current\.venv\Scripts\python.exe app.py`, with
  `cwd=current`. The environment carries `GC_DATA_DIR=<root>\data` (via
  `data_env`) and `PORT=<port>` (because `port_arg` is empty).
- Health check: `GET /healthz` on the scratch port must return HTTP 200 with
  `{"status":"ok","version":<tag>}` within 60 s. The check runs with
  `health_args` appended (`--no-tray`) and `GC_DATA_DIR=<root>\data\healthcheck`,
  which is an **empty** directory.
- Restart handshake: the app writes `switch-requested` in the data dir, and the
  updater renames it to `switch-accepted` or `switch-refused`. The app must not
  respawn itself while `switching` or `switch-accepted` exists.
- `supervisor.py` must be byte-identical to COA's copy. The updater imports
  whichever app's copy it finds first.

## Design

### 1. `paths.py` — the single source of on-disk locations (new)

A stdlib-only module with no side effects on import, in the same style as
`instance.py`.

- `DATA_DIR`: `Path(os.environ["GC_DATA_DIR"])` when that variable is set.
  When it is unset, **legacy mode**: behaviour is exactly as today (home-folder
  files, `cwd`-relative defaults). This keeps the copies still running from
  the share working unchanged until phase 2 retires them.
- Deployed mode puts every piece of state under `DATA_DIR`:

| State | Legacy location | Deployed location (`GC_DATA_DIR` set) |
|---|---|---|
| settings | `~/.gc_viewer_settings[-port].json` | `DATA_DIR/settings.json` |
| results CSV default | `cwd/distill_results.csv` | `DATA_DIR/distill_results.csv` |
| processed CDF dir default | `cwd/processed_cdf` | `DATA_DIR/processed_cdf` |
| exports default | `cwd/exports` | `DATA_DIR/exports` |
| comparison standards | `~/gc_comparison_standards` | `DATA_DIR/gc_comparison_standards` |
| dir cache | `~/.gc_viewer_dircache.json` | `DATA_DIR/dircache.json` |
| notifications | `~/.gc_viewer_notifications.json` | `DATA_DIR/notifications.json` |
| pidfile | `BASE/.gc_server[-port].pid` | not used (the updater supervises) |
| log | console only | `DATA_DIR/app.log` (rotating) plus console |

- Caches that already follow `processed_cdf_dir` (`.blank_cache.json`,
  `.processed_index.json`, `.sample_flags_cache.json`, `.bestfit_cache.json`)
  need no change. They move with that directory.
- A settings value the operator set explicitly (e.g. a `watch_dir`) always
  wins. `paths.py` only changes **defaults** and the settings file location.
- **Health-check safety:** with an empty data dir, startup must succeed. It
  creates the folders it needs and, where no settings file exists, runs
  unconfigured instead of crashing. The Looker is not started when `watch_dir`
  is unset or missing, and the reason is logged.

### 2. Port

The precedence becomes `PORT` env (from the updater) → `--port` → `GC_PORT` →
5560, still resolved in `instance.py`. The legacy multi-port launcher keeps
working but is not used in production. `app.py` accepts and ignores
`--no-tray` (the updater's `health_args`) and accepts `--dev`; any other
unknown flag is an error, so a typo in the updater config fails the health
check loudly.

### 3. Versioning and health

- `VERSION` file at the app root, first line; `"dev"` if missing. CI writes it
  and `.gitignore` excludes it.
- `GET /healthz`: no auth, no outbound calls, and requests to it do not count
  as activity. It returns `{status:"ok", version, pid, active_sessions,
  idle_seconds}`. `idle_seconds` comes from the existing `_last_activity`
  tracking. `active_sessions` is the number of distinct client IPs seen in the
  last 5 minutes. Both are provided so `auto_switch` can be enabled later.
- A version badge fixed to the bottom-right of the viewport, same markup and
  CSS as COA, and the same string `/healthz` reports.

### 4. Production hardening

- `app.run(debug=True)` becomes `debug=False` unless `--dev` is passed. The
  Werkzeug interactive debugger on `0.0.0.0` is a remote-code-execution hole.
- Self-respawn (the 3 AM auto-restart, and Restart when nothing is staged):
  when `GC_DATA_DIR` is set, the app **exits** and lets the updater's
  supervision loop restart it within ~20 s. It never spawns a copy of itself,
  so two processes cannot race for the port. Legacy mode keeps today's
  self-respawn.

### 5. Restart installs a staged update

- Port `restart_update.py` from COA verbatim, adding a test proving it is
  identical to the version the COA suite verifies.
- `POST /api/restart`: when `restart_update.staged_update(DATA_DIR, VERSION)`
  finds a newer healthy release, write the switch request, then poll
  `read_switch_outcome` until accepted, refused or timed out. It falls back to
  a plain restart (§4) on refusal or timeout. The response tells the UI which
  path it took.
- A Restart button in Settings, which shows "Restart & install vX.Y.Z" when an
  update is staged.
- On startup: `clear_switch_files` (as COA's `_tidy_after_last_run` does).

### 6. `supervisor.py`

Copy COA's file byte-for-byte. A test pins its sha256 against the copy in
`../coa-reviewer` when that checkout exists, and skips otherwise.

### 7. Release workflow

`.github/workflows/release.yml` is adapted from COA:
- Trigger: `v*` tags.
- rsync into `dist/gc-hub-<tag>/`, excluding `.git`, `.github`, `dist`,
  `__pycache__`, `.pytest_cache`, `.venv`, `tests/manual`, `processed_cdf*`,
  `*.csv`, `*.pid`, `.*_cache.json`, `*.log*`, `docs`, and the switch files.
- Write `VERSION`; assert that no state leaked in and that the required files
  are present; zip plus sha256; `gh release create`.
- `requirements.txt` is pinned to the versions the suite passes on.
  `selenium`/`webdriver-manager`/`chromedriver-autoinstaller` stay for the
  QBench PDF upload.

### 8. QBench API credentials in the UI (Ryan's request)

- **Lazy resolution.** `qbench_client.py` no longer reads the credentials at
  import (lines 21-22). They are resolved when a client is constructed, so the
  app boots with no credential store: the health check on a fresh box passes,
  and the UI is reachable to enter them.
- `qbench_secrets.save_default(client_id, client_secret)`: an atomic write
  (temp file + `os.replace`) to the existing store path. It merges into the
  existing JSON so other `profiles` are preserved. On POSIX the file is
  created `0600`; on Windows it inherits the `%APPDATA%` ACL.
- `GET /api/qbench-api-credentials` returns `{configured: bool, client_id_hint:
  "…abcd", store_path}`. **The secret is never returned.**
- `POST /api/qbench-api-credentials` takes `{client_id, client_secret, password}`
  (JSON only: any other content type is refused with 415). The older
  `/api/qbench-credentials` is the Selenium web-login route and is unchanged:
  - It is gated by the same admin password as "Set as Default". That password
    is currently a hardcoded `"admin"`; the design follows the existing gate
    and flags it (see Open items).
  - It **test-authenticates first** by requesting a token with the submitted
    pair, and saves only on success. Failure returns 400 with a generic
    reason (the HTTP status or "timed out", never QBench's response text,
    which could echo the assertion) and nothing is written. The probe is
    bounded: a (5 s connect, 10 s read) timeout, its own rate limiter, and
    exactly one token request (no clock-skew retry).
- UI: a "QBench API" section in Settings with Client ID, Client Secret
  (password input, never pre-filled), admin password, and a "Test & Save"
  button. When configured, the status line reads "Configured (…abcd)".
- The Selenium web-login file (`qbenchlogin.txt`, UNC) is unchanged in this
  phase. It already has its own in-app prompt.

### 9. Calibration crash fix

The bug report was confirmed in code. `app.py:~898-931` pairs auto-detected
calibration peak times (`distill.calibration_peak_times`) with the fixed
20-entry `N_ALKANE_CARBON` list by position. `analysis_core.py:348/360` slices
the carbons to `len(cal_times)`. With the 9/16 calibration file (24 detected
peaks: 20 alkanes, CS₂, 3 impurities), `np.interp` gets arrays of different
lengths, the handler returns a 500, and Analysis, Export Report and QBench
export all fail. With the old file it only "worked" because the solvent was
labelled C5, so every carbon range was shifted by one.

Fix:
- `distill.calibration_ladder(conf) -> list[tuple[float, int]]` returns
  (retention time, carbon number) pairs from the **saved assignments**. It is
  the same source `_build_calibration` uses for the distillation math, so
  ignored peaks are dropped. It falls back to sequential auto-detection only
  when no assignments exist. The result is sorted by time and contains no
  duplicate carbons.
- All four copy-pasted blocks (`api_analysis`, `_run_export_analysis`, the
  analysis-PDF generator, the QBench upload worker) call it. There is one code
  path.
- `analysis_core` asserts equal lengths at its boundary, so a future mismatch
  becomes a clear error rather than an `np.interp` failure.
- Tests: a failing test first, reproducing a 24-peak ladder with 4 ignored
  peaks, plus a test that the carbon labels match the assignments.

## Testing

- TDD for every behaviour change: the failing test is written first and seen
  to fail.
- A new `tests/test_paths.py` covers legacy vs deployed mode for every row in
  the table in §1.
- `tests/test_healthz.py` boots the real app **in a subprocess** with an empty
  `GC_DATA_DIR` and a scratch `PORT`, then polls `/healthz`. This is exactly
  what the updater does; tests elsewhere avoid importing `app` because of its
  side effects.
- A test drives the credential endpoints with a stubbed token call.
- A release-workflow test runs the rsync/zip step locally and asserts no state
  is included.
- A critic subagent reviews each task's diff against this spec before it is
  marked done.

## Out of scope for phase 1

Multiple instruments, the push agent, migrating data off the GC PCs, the
deviation bullets, comment presets, and the UI redesign.

## Manual steps for Ryan (cannot be done from here)

1. Add `ASAP-Labs-LLC/gc-hub` to the updater's fine-grained PAT (the repo is
   private, unlike COA). This is stored in Credential Manager under
   `asaplabs-github`.
2. Add the `gc` entry to `C:\ASAPApps\updater\config.json`:
   `{"name":"gc","repo":"ASAP-Labs-LLC/gc-hub","root":"C:\\ASAPApps\\gc","port":5560,"scratch_port":15560,"entry":"app.py","data_env":"GC_DATA_DIR","port_arg":"","health_args":["--no-tray"],"auto_switch":false}`,
   then restart the updater task. Port 5560 must be free on ASAPSV1.
3. Enter the QBench API credentials in the new Settings screen, **after
   rotating them**: the old values are in `gc-data`'s history on GitHub and
   hardcoded in the live `qbench_client.py` on the share. The store lands in
   the `%APPDATA%` of the account the updater's scheduled task runs as; for
   SYSTEM that is
   `C:\Windows\System32\config\systemprofile\AppData\Roaming\ASAPLabs\qbench.json`.
   Settings > QBench API shows the exact path.

## Open items

- State-changing `/api/` requests that a browser marks as cross-site (a
  foreign `Origin`, or `Sec-Fetch-Site` other than `same-origin`/`none`) are
  refused with 403, but DNS rebinding is not mitigated; a `Host` allowlist
  would be the fix.
- The admin gate is a hardcoded `"admin"`. It should become a setting (a
  hashed password in the data dir). That is small, but it changes who can do
  what, so it waits for Ryan's call.
- The GC PCs keep running the share copy until phase 2, so they **do not get
  the calibration fix from a release**. The fixed files can be copied onto the
  share as a stopgap, with Ryan's approval.
- Before enabling auto_switch, `/healthz` must count a live QBench upload or
  queued reprocess as an active session (reuse `_is_server_idle`'s checks).
