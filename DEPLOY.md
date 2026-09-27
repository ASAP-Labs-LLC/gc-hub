# Deploying GC Hub on ASAPSV1 (one-time setup)

GC Hub is deployed by the same updater as COA Reviewer
(`C:\ASAPApps\updater\updater.py`, from `coa-reviewer/deploy/updater/`). The
updater is unchanged; gc is one more entry in its `config.json`. Everyday
releases are in [`RELEASING.md`](RELEASING.md). This file covers the steps
that have to be done by hand on the server, once.

Resulting layout:

```
C:\ASAPApps\gc\
  releases\v1.2.3\      unpacked release (immutable), with its own .venv
  current               junction -> the live release
  data\                 GC_DATA_DIR: all state, never touched by a deploy
    settings.json
    distill_results.csv
    processed_cdf\       (+ .blank_cache.json, .processed_index.json, ...)
    exports\
    gc_comparison_standards\
    notifications.json, dircache.json
    app.log              (rotating, 5 MB x 3)
    healthcheck\         the updater's scratch data dir for health checks
```

## Before you start

- **Port 5560 must be free on ASAPSV1** (and 15560, the scratch port for health
  checks). Check with `netstat -ano | findstr :5560`. The app refuses to start
  while its port has a listener, so a leftover share copy on 5560 blocks it.
- **Confirm the Python that runs the updater is 3.12 or newer**
  (`numpy==2.5.3` and `scipy==1.18.1` require it), and that Windows wheels exist
  for it for numpy, scipy, pandas, netCDF4/cftime, Pillow and watchdog. The pins
  were validated on Python 3.12 on macOS only; see RELEASING.md §3,
  "Dependency pins". This has not been checked on the server.
- **Google Chrome must be installed** for the QBench PDF upload (Selenium) and
  for kaleido's static chart export.

## Step 1: give the updater's PAT access to gc-hub

`ASAP-Labs-LLC/gc-hub` is **private**, unlike COA. Edit the fine-grained PAT
the updater uses (stored in Windows Credential Manager under
`asaplabs-github`) and add `gc-hub` to its repository access with read access
to Contents. Without it the updater cannot see gc-hub's releases at all.

## Step 2: add the `gc` entry to the updater config

Add this object to the apps list in `C:\ASAPApps\updater\config.json`:

```json
{"name": "gc", "repo": "ASAP-Labs-LLC/gc-hub", "root": "C:\\ASAPApps\\gc", "port": 5560, "scratch_port": 15560, "entry": "app.py", "data_env": "GC_DATA_DIR", "port_arg": "", "health_args": ["--no-tray"], "auto_switch": false}
```

What each field does for gc:

| Field | Effect |
|---|---|
| `data_env: "GC_DATA_DIR"` | The updater sets `GC_DATA_DIR=C:\ASAPApps\gc\data`. That switches the app to deployed mode (`paths.py`): all state under that folder, logging to `data\app.log`, and restarts that exit and let the updater relaunch |
| `port_arg: ""` | No `--port` flag; the port arrives as `PORT`, which wins in deployed mode (`instance.resolve_port`) |
| `health_args: ["--no-tray"]` | Accepted and ignored by `app.py`. Any other unknown flag makes the app exit with an error, so a typo here fails the health check loudly |
| `auto_switch: false` | A healthy staged release waits until someone presses Settings > Restart ("Restart & install vX.Y.Z"). Leave it false until `/healthz` counts a running QBench upload or reprocess as activity (spec, Open items) |

Then restart the updater's scheduled task so it reads the new config. The
first poll (within ~5 minutes) stages and health-checks the latest release.
Check with:

```
python C:\ASAPApps\updater\updater.py status --config C:\ASAPApps\updater\config.json
```

## Step 3: move existing settings into `C:\ASAPApps\gc\data\settings.json`

A fresh deploy starts **unconfigured**: no watch folder (so the Looker does not
start, and the log says why), no calibration, default analysis settings. You
can configure it from Settings in the browser, or carry over an existing
install's settings:

1. Stop the app, or do this before its first start.
2. Copy the settings file of the copy you are replacing. On the machine and
   account that ran it, that is `%USERPROFILE%\.gc_viewer_settings.json` for
   port 5560, or `%USERPROFILE%\.gc_viewer_settings-<port>.json` for another
   port. Save it as `C:\ASAPApps\gc\data\settings.json`.
3. Edit the paths in it. Every one must be reachable **from ASAPSV1, by the
   account the updater's task runs as**:

   | Key | Set it to |
   |---|---|
   | `distill_output` | `C:\\ASAPApps\\gc\\data\\distill_results.csv`, or delete the key to take that default |
   | `processed_cdf_dir` | `C:\\ASAPApps\\gc\\data\\processed_cdf`, or delete the key |
   | `export_folder` | `C:\\ASAPApps\\gc\\data\\exports`, or delete the key |
   | `blank_cache_file` | delete it; it is re-derived from `processed_cdf_dir` on every load |
   | `comparison_defaults_dir` | `C:\\ASAPApps\\gc\\data\\gc_comparison_standards` (delete the key for that default), then copy the standard CDFs from the old folder into it |
   | `watch_dir` | the GC workstation's CDF folder as a **UNC path** (`\\\\host\\share\\...`) |
   | `calibration_cdf` | the calibration CDF as a UNC path, or a copy under `data\` |
   | `correction_factors_json`, `comparison_export_template`, `analysis_report_logo` | UNC paths if set |

   Keep `calibration_assignments`, `calibration_labels`, the `analysis_*`,
   `bestfit_*` and `sample_flag_rules` values as they are. They are the
   operator's work, and the calibration assignments are what the carbon
   labels come from (`distill.calibration_ladder`).
4. Start the app (press Restart, or let the updater start it) and check
   Settings shows the paths you set.

Things to know about paths when the updater runs as **SYSTEM**:

- Mapped drive letters do not exist for SYSTEM. Use UNC paths.
- SYSTEM reaches network shares as the computer account (`ASAPSV1$`), so the
  share must grant that account access. The Selenium login file
  `\\ASAPServer\Labsharedrive\ASAP Lab Results\qbenchlogin.txt` is read the same
  way.
- Do not point an ASAPSV1 instance and a share copy at the same `watch_dir` or
  `distill_output`. Each would process every new file, and their writes to one
  results CSV are not coordinated across processes. Until phase 2 retires the
  share copies, retire a share copy before its folder moves to ASAPSV1.

## Step 4: enter the QBench API credentials (after rotating them)

**Rotate the QBench API client secret first.** The old values are in the
`gc-data` repository's history on GitHub and hardcoded in the `qbench_client.py`
still running from the share, so treat them as leaked. Create a new client
pair in QBench, and retire the old one once nothing uses it (the share copies
keep using the hardcoded pair until they are replaced or patched).

Then, in the deployed app: **Settings > QBench API**, enter Client ID, Client
Secret and the admin password, and press **Test & Save**. The app requests a
token with the pair and saves it only if QBench accepts it. The secret is never
shown again; the status line reads "Configured (…abcd)".

Where the store lands: `%APPDATA%\ASAPLabs\qbench.json` of **the account the
updater's scheduled task runs as**, which is the account the app runs as. For
SYSTEM that is:

```
C:\Windows\System32\config\systemprofile\AppData\Roaming\ASAPLabs\qbench.json
```

Settings > QBench API shows the exact path the running app uses. The file
holds the default pair plus any `profiles`; saving from Settings replaces only
the default pair and keeps the profiles. `QBENCH_STORE_PATH`,
`QBENCH_CLIENT_ID` and `QBENCH_CLIENT_SECRET` in the app's environment
override the store, but the updater does not set them, so the store is what
counts.

The app starts without credentials (the health check runs with none). Only
QBench lookups and uploads fail until they are entered.

## After setup

- Everyday releases: [`RELEASING.md`](RELEASING.md).
- Logs: `C:\ASAPApps\gc\data\app.log` for the app, `C:\ASAPApps\updater\updater.log`
  for staging, health checks, switches and rollbacks.
- Back up `C:\ASAPApps\gc\data\`. The results CSV, processed CDFs and settings
  there are the only copy of that state on this server.
