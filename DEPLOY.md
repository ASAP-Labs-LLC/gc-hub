# Deploying GC Hub on ASAPSV1 (one-time setup)

GC Hub is deployed by the same updater as COA Reviewer
(`C:\ASAPApps\updater\updater.py`, from `coa-reviewer/deploy/updater/`). The
updater is unchanged; gc is one more entry in its `config.json`. Everyday
releases are in [`RELEASING.md`](RELEASING.md). This file covers the steps
that have to be done by hand on the server, once.

Resulting layout (v2, the hub):

```
C:\ASAPApps\gc\
  releases\v2.0.0\      unpacked release (immutable), with its own .venv
  current               junction -> the live release
  data\                 GC_DATA_DIR: all state, never touched by a deploy
    gc.db                the hub store (samples, revisions, jobs, instruments, ...)
    backups\             gc-<date>.db + settings-<date>.json, nightly, 14 kept
    cdf\gc1\2026\09\    every received CDF, kept forever
    results\             gc1_results.csv (+ .gchub.json sidecar), unless an
                         instrument's export path points at the share CSV LEM tails
    settings.json
    exports\             report PDFs
    gc_comparison_standards\
    notifications.json
    admin-setup-code.txt until the admin password is set
    app.log              (rotating, 5 MB x 3)
    healthcheck\         the updater's scratch data dir for health checks
```

A v1.x install also left `distill_results.csv`, `processed_cdf\` and
`dircache.json` here. v2 neither reads nor deletes them.

## Before you start

- **Port 5560 must be free on ASAPSV1** (and 15560, the scratch port for health
  checks). Check with `netstat -ano | findstr :5560`. The app refuses to start
  while its port has a listener, so a leftover share copy on 5560 blocks it.
- **Confirm the Python that runs the updater.** It builds every release venv
  with its own interpreter (`sys.executable`), recorded as CPython 3.14.4 on
  ASAPSV1 on 2026-08-21 (coa-reviewer/requirements.txt); the pins target that
  and every pin has a Python 3.14 Windows wheel. Check it is still so:
  `schtasks /query /tn "<updater task name>" /v /fo list`, take the exe from
  "Task To Run", and run it with
  `-c "import sys,struct;print(sys.version, sys.executable, struct.calcsize('P')*8)"`.
  It must be 64-bit CPython (no 32-bit scipy wheels), 3.12 or newer; see
  RELEASING.md §3, "Dependency pins".
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
| `data_env: "GC_DATA_DIR"` | The updater sets `GC_DATA_DIR=C:\ASAPApps\gc\data`: all state under that folder (`paths.py`), logging to `data\app.log`, and restarts that exit and let the updater relaunch. v2 **refuses to start** without it (exit code 2, the message names this file) |
| `port_arg: ""` | No `--port` flag; the port arrives as `PORT`, which always wins (`instance.resolve_port`) |
| `health_args: ["--no-tray"]` | Accepted and ignored by `app.py`. Any other unknown flag makes the app exit with an error, so a typo here fails the health check loudly |
| `auto_switch: false` | A healthy staged release waits until someone presses Settings > Restart ("Restart & install vX.Y.Z"). Leave it false until `/healthz` counts a running QBench upload or reprocess as activity (spec, Open items) |

Then restart the updater's scheduled task so it reads the new config
(`schtasks /end /tn "<updater task name>"`, then
`schtasks /run /tn "<updater task name>"`). The first poll (within ~5 minutes)
stages and health-checks the latest release.

## Step 3: first install (switch to v1.0.0 by hand)

On a fresh box there is no `current` junction and nothing is running, so there
is no Settings > Restart button to press, and the updater's supervision loop
cannot start the app on its own. **Until the first switch, `updater.log`
fills with failed starts; this is expected.** Every ~20 s it logs
`[gc] not serving on port 5560 — starting it` and then `[gc] failed to start`,
and after 3 starts in 15 minutes it logs CRITICAL
`[gc] has failed to stay up after 3 starts in 15 minutes — not restarting
again` on every pass until those starts are 15 minutes old.

1. Wait until the release is staged and healthy:

   ```
   python C:\ASAPApps\updater\updater.py status --config C:\ASAPApps\updater\config.json
   ```

   It is ready when it prints

   ```
   gc: DOWN (port 5560)  current=dev junction->None staged=v1.0.0 healthy=True
   ```

   (plus a `notes:` line from the health check). `healthy=False` means the
   staging failed: the `notes:` line and `C:\ASAPApps\updater\updater.log` say
   why. `staged=None` means it has not been polled yet, or the PAT cannot
   see gc-hub (Step 1).

2. Do the first install:

   ```
   python C:\ASAPApps\updater\updater.py switch --app gc --tag v1.0.0 --config C:\ASAPApps\updater\config.json
   ```

   This creates the `current` junction, starts the app and waits up to 60 s
   for `/healthz` to report `v1.0.0`. It exits 0 on success. There is no
   previous release to roll back to, so a failure here leaves the app down;
   `updater.log` gives the reason.

3. Hand the app to the updater service. `switch` started the app itself, as
   **the account that ran the command** (so with that account's `%APPDATA%`,
   where the QBench store lives, and it stops when that account logs off).
   - Restart the updater's scheduled task (`schtasks /end` and `/run` as
     above). This also clears the failed-start history, which the service
     keeps only in memory, so it is no longer "giving up".
   - Stop the copy `switch` started: `netstat -ano | findstr :5560`, then
     `taskkill /F /T /PID <pid>` for the LISTENING line. Within ~20 s the
     service starts it again as the task's account.
   - `updater.py status` should now show
     `gc: SERVING on 5560  current=v1.0.0 junction->v1.0.0 staged=v1.0.0 healthy=True`.

From here on, a new release is installed with Settings > Restart (see
`RELEASING.md` §4).

## Step 4: move existing settings into `C:\ASAPApps\gc\data\settings.json`

(v1.0.0 setup. For v2 the keys that matter are the calibration and the
corrections file, which seed `gc1` once at its first start, plus the analysis
and best-fit settings; see "Upgrading to v2" below.)

A fresh deploy starts **unconfigured**: no calibration, default analysis
settings. You can configure it from Settings in the browser, or carry over an
existing install's settings:

1. Stop the app. Pausing it first keeps the updater from starting it again
   while you edit:

   ```
   python C:\ASAPApps\updater\updater.py pause --app gc --config C:\ASAPApps\updater\config.json
   ```

   Pause does not stop a running app ("Stop it yourself if it is still
   running"), so then `netstat -ano | findstr :5560` and
   `taskkill /F /T /PID <pid>` for the LISTENING line. (Settings > Restart
   is not a stop: while paused, the app starts its own replacement.)
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
4. Resume, and the updater starts the app within ~20 s:

   ```
   python C:\ASAPApps\updater\updater.py resume --app gc --config C:\ASAPApps\updater\config.json
   ```

   Check Settings shows the paths you set. Use the same pause / stop / edit /
   resume sequence for any later hand edit of `settings.json`; the app
   rewrites the file whenever settings are saved from the browser.

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

## Step 5: enter the QBench API credentials (after rotating them)

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

## Admin password (v2, 2B1)

There is no default admin password. Until one is set, every admin action
(Set as Default, QBench API credentials, agent installers, and so on) is
refused with a message pointing to `/admin/setup`.

1. On ASAPSV1, open `C:\ASAPApps\gc\data\admin-setup-code.txt`. The same
   one-time code is in `C:\ASAPApps\gc\data\app.log`, in a WARNING line that
   starts "No admin password is set. Admin setup code: ...".
2. In a browser, open `http://asapsv1:5560/admin/setup` (the hub's own name
   or IP address; setup is refused under any other host name). Enter the code
   and the new password (at least 8 characters). The code file is deleted
   once the password is set.

**Forgotten password (reset).** Stop nothing; on ASAPSV1 run, from the
current release folder with its venv's Python:

```
python -c "import store; store.settings_kv.delete('admin_password', db=r'C:\ASAPApps\gc\data\gc.db')"
```

Then open `/admin/setup` (or restart the app): a **new** setup code is
written to `admin-setup-code.txt` and logged in `app.log`. Follow the steps
above. Agent tokens are not affected by a reset.

Wrong passwords are throttled per address (after 5 in a row, a growing
wait) and hub-wide (30 in 10 minutes); a restart clears the counters. The
hub-wide limit never applies on the server itself: if someone on the LAN has
exhausted it, RDP to ASAPSV1 and use `http://localhost:5560`.

**Protect the data folder.** On Windows the setup code file's 0600 mode is
ignored; the folder's ACL is what protects it. `C:\ASAPApps\gc\data` must be
on a **local disk** of ASAPSV1 (never a network share), readable only by
Administrators and the account the updater runs the app as. It holds the
setup code, `app.log` (which also contains the code while no password is
set), the store with the admin hash and agent-token hashes, and every CDF.
Check with `icacls C:\ASAPApps\gc\data` and remove `Users` / `Everyone`
entries if present.

**The share copies are v1.x.** They have no hub store and keep their own
behaviour until cutover; the hub's admin password does not apply to them.

**Agent installers need the hub URL.** Before the first "Download
installer", set the address the GC PCs use to reach the hub
(`POST /api/admin/hub-url {password, hub_url: "http://asapsv1:5560"}`, the
Instruments page from 2A2). A download from `localhost` or `127.0.0.1` on the
server itself is refused until it is set.

## Upgrading to v2 (the hub)

v2.0.0 is the hub: it no longer watches a folder. CDFs arrive from the GC-PC
agents (phase 2B2) or from **Load CDFs from a folder** on the Hub admin page,
and each final result is appended to the instrument's results CSV. Read
`docs/release-notes/v2.0.0.md` before installing; several numbers can differ
from v1's for documented reasons.

Before pressing Settings > Restart ("Restart & install v2.0.0"):

1. **Check v1 is not watching a live folder on ASAPSV1**: `updater.py status`,
   and `watch_dir` in `C:\ASAPApps\gc\data\settings.json`. v2 ignores it,
   so anything that folder still receives would stop being processed on the
   server.
2. **Make sure `settings.json` has what gc1 is created from**: the
   calibration (`calibration_cdf`, `calibration_assignments`,
   `calibration_sensitivity`) and `correction_factors_json` (the phase-1
   corrections file, reachable by the account the updater runs the app as).

On the first v2 start (all automatic, visible in `app.log` and the
notifications):

- `gc.db` is created and the instrument **gc1** is made from `settings.json`,
  with `live_since` = that moment: only injections from then on export
  automatically; anything older loaded later is **backfill** and needs an
  admin release.
- **gc1's correction factors are seeded once** from `correction_factors_json`
  into the hub (an info notification says so; check the values). If the file
  is missing or invalid (e.g. the share is unreachable at that moment), an
  error notification says so and gc1's samples wait as `pending_corrections`
  (never corrected with zeros); the hub retries the seed every 10 minutes, or
  enter the values on the Instruments page. From then on the factors are the
  hub's; **never enter GC factors in LEM** (they would be applied twice).
  Editing `correction_factors.json` later changes nothing in the hub.
- Settings in the browser can change only the flag rules and series colours
  freely, and the best-fit settings with the admin password. Paths (standards
  and export folders are fixed under `data\`), the calibration, the
  corrections path and `blank_max_intensity_pa` are changed by editing
  `settings.json` on the server (pause / stop / edit / resume, Step 4).
  Saving the calibration page and adding, renaming or deleting comparison
  standards need the admin password; standards are added from a sample
  ("Set as comparison standard"), never from a server path.
- An **admin setup code** is written (see "Admin password" below). Set the
  password first: every admin action needs it.
- The nightly backup (from 1 AM, `data\backups\`, 14 kept, plus a copy of
  `settings.json`) and the job-table prune run inside the hub. A failed backup
  raises an error notification and is retried hourly.

Then, as needed, on **Hub admin** (`http://asapsv1:5560/admin/hub`, admin
password):

- **Load CDFs from a folder**: one-shot and read-only on the folder (copy the
  GC's processed folder locally first, e.g. with robocopy), resumable, in
  injection-time order; the hub processes what it loads. Check what was
  loaded with `tools/parity_report.py` (the 2A1 plan's T6 runbook) before
  sign-off. **Before a GC is cut over, never load CDFs of that GC injected
  after the hub's first start** without *force backfill*: they are live
  (after gc1's `live_since`) and would be appended to the export, while v1 on
  the share still writes the same results to LEM's CSV, so LEM would get them
  twice. History belongs to the import (below), which keeps it as backfill.
- **Exports**: gc1 appends to `data\results\gc1_results.csv` by default. To
  keep feeding the CSV LEM tails on the share: **stop the v1 writer for that
  GC first**, then *New path* to that CSV and *Adopt* it (the hub appends
  after its current content, so LEM's read position carries on). The
  account the app runs as needs write access to the file and
  create/rename/delete rights in its folder (sidecar and lock file). A file
  that changes behind the hub's back is refused (error notification) until
  it is adopted again or moved. *Write fresh* starts a new file with every
  exportable result; never point LEM at one without setting its tail offset to
  the end.

## Cutover runbook (per GC PC)

Until a GC PC is cut over, its v1 copy on the share keeps processing and
writing LEM's CSV; the hub holds that GC's history only as backfill. Cut over
one PC at a time, **in this exact order**, so no row is lost or written twice
between steps (spec, "Cutover runbook"):

1. **Quit `run.pyw`** on that PC (tray > Quit) and remove its autostart
   (Startup folder shortcut or scheduled task). Check nothing still listens on
   its port (`netstat -ano | findstr :5560`, `taskkill /F /T /PID <pid>`).
   From now on nothing processes that GC's new runs until step 4.
2. **Re-run the history import for that instrument** (Hub admin: the history
   import page lands with branch `t5/import-route`; until then the 2D CLI,
   `tools/import_history.py`). It picks up only the delta since the last
   import (files v1 processed after it); otherwise it is a no-op. Review the
   summary (conflicts, rejected and truncated files).
3. **Set that instrument's `live_since` to now** on the Instruments page.
   Anything injected before it that arrives later is backfill and is listed
   for review, never exported automatically.
4. **Install the agent**: on the Instruments page, *Download installer* for
   that instrument, run `install.pyw` on the PC, adopt the mirror file if
   LEM tails a local CSV on that PC, and start the agent. Files v1 already
   processed dedupe by sha256 against the imported copy.

Then watch the first sync on the Instruments page and the hub's first appends
to the adopted share CSV (Hub admin > Exports); LEM's read position carries
on unchanged. Only after the last GC is cut over is v1 retired.

## Updating the share copies (v1.x only, from `maint/v1`)

The GC PCs keep running v1.x from `\\ASAPServer\Labsharedrive` with `run.pyw`
until each is cut over to the agent. **v2 cannot run there** (no
`GC_DATA_DIR`, no `run.pyw`): never copy a v2 release onto the share.

Share fixes come from the branch **`maint/v1`**, cut at v1.1.0. They are
**never published as GitHub releases**: the updater installs whatever GitHub
calls "latest", so a v1.x release published after v2 would downgrade ASAPSV1.
If a fix needs a tag at all, create the release with `make_latest=false`
(`gh release create v1.1.1 --latest=false ...`) and check the latest release
is still v2 afterwards (`gh release view`). Build the tree to copy locally
from the branch (`git checkout maint/v1 && bash scripts/package_release.sh
v1.1.1 "$TMPDIR/v1pkg"`).

If Ryan approves copying a fix onto a share copy:

1. **Copy the whole tree, never individual files.** `app.py` imports
   `paths`, `restart_policy`, `restart_update`, `supervisor`, `version` and
   `qbench_secrets`, so an `app.py` copied on its own does not start. Use the
   contents of the `maint/v1` package (which also carries `VERSION`) and copy
   them over the share folder. The zip holds no state, so
   the folder's results CSV, `processed_cdf` and caches are not touched.
   Quit `run.pyw` (tray > Quit) first: it restarts the server whenever a
   `.py` file changes, which mid-copy means starting a half-copied tree.
2. **Stop any orphaned server still holding the port.** A restart from inside
   the app (Settings > Restart, or the 3 AM auto-restart) starts a new server
   process that `run.pyw` does not track, so quitting `run.pyw` can leave it
   running with the old code. Check with `netstat -ano | findstr :5560` (or
   that copy's port) and `taskkill /F /T /PID <pid>` for the LISTENING line.
   Then relaunch `run.pyw`.
3. **Enter the QBench API pair once per Windows account.** The old share
   `qbench_client.py` had the pair hardcoded. This tree reads
   `QBENCH_CLIENT_ID`/`QBENCH_CLIENT_SECRET` from the environment or
   `%APPDATA%\ASAPLabs\qbench.json`, so on each GC PC, as the Windows account
   that runs `run.pyw`, open **Settings > QBench API** and press **Test &
   Save** (Step 5). Until then QBench lookups and uploads fail.
4. **Scan, Reprocess, Export to LIMS and Rebuild DB return 409** ("Watch
   folder is not configured") while the instrument folder (`watch_dir`) is
   unreachable, for example when the share or the GC PC's folder is offline.
   They work again once the folder is reachable; nothing needs restarting.

## After setup

- Everyday releases: [`RELEASING.md`](RELEASING.md).
- Logs: `C:\ASAPApps\gc\data\app.log` for the app, `C:\ASAPApps\updater\updater.log`
  for staging, health checks, switches and rollbacks.
- Back up `C:\ASAPApps\gc\data\`. The hub makes its own nightly copy of
  `gc.db` and `settings.json` in `data\backups\`, but on the same disk; the
  store, the CDFs under `data\cdf\` and the results files are the only copy
  of that state on this server.
