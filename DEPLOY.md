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
installer", set the address the GC PCs use to reach the hub: Instruments
page > *Hub URL for installers*, `http://asapsv1:5560` (or
`POST /api/admin/hub-url {password, hub_url}`). A download from `localhost`
or `127.0.0.1` on the server itself is refused until it is set.

## Upgrading to v2 (the hub)

v2.0.0 is the hub: it no longer watches a folder. CDFs arrive from the GC-PC
agents (installed per PC at cutover, below), from the v1 history import, or
from **Load CDFs from a folder** on the Hub admin page, and each final result
is appended to the instrument's results CSV. Read the v2.0.0 release notes
(the GitHub release page for the tag) before installing; several numbers can
differ from v1's for documented reasons.

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
  and export folders are fixed under `data\`), the corrections path and
  `blank_max_intensity_pa` are changed by editing `settings.json` on the
  server (pause / stop / edit / resume, Step 4). The calibration is each
  instrument's own, set on the Instruments and Calibration pages (admin
  password); `settings.json`'s calibration keys only create gc1's once.
  Saving the calibration page and adding, renaming or deleting comparison
  standards need the admin password; standards are added from a sample
  ("Set as comparison standard"), never from a server path.
- An **admin setup code** is written (see "Admin password" below). Set the
  password first: every admin action needs it.
- The nightly backup (from 1 AM, `data\backups\`, 14 kept, plus a copy of
  `settings.json`) and the job-table prune run inside the hub. A failed backup
  raises an error notification and is retried hourly.

Then continue with "Before cutover" below. What the admin pages do:

- **Hub admin** (`http://asapsv1:5560/admin/hub`, admin password):
  - **Import v1 history**: one instrument's v1 processed CDFs and results
    CSV, read-only on both. *Dry run* only classifies and writes nothing;
    *Start real import* runs it as a job (one admin job at a time),
    resumable, stoppable between batches (*Stop*); *Load last run* shows the
    previous run and fills the form. Every imported sample is backfill with
    v1's numbers stored verbatim (never recomputed, never exported
    automatically). This is how production gets its history.
  - **Load CDFs from a folder**: one-shot and read-only on the folder,
    resumable, in injection-time order; the hub **computes** what it loads.
    It is not the way to bring in history (the import is), and the parity
    check runs it on a scratch folder, never here (below). **Before a GC is
    cut over, never load CDFs of that GC injected after its `live_since`**
    without *force backfill*: they would be appended to the export while v1
    on the share still writes the same results to LEM's CSV, so LEM would
    get them twice.
  - **Exports**: each instrument's CSV, pending rows and refusals, with
    *Adopt*, *New path* and *Write fresh*. gc1 appends to
    `data\results\gc1_results.csv` until it is pointed at the share CSV LEM
    tails at cutover (below). *Write fresh* starts a new file with every
    exportable result; never point LEM at one without setting its tail
    offset to the end.
- **Instruments** (`http://asapsv1:5560/instruments`): add an instrument;
  per instrument its name, `live_since` and enabled flag, the results export
  path and *Adopt*, the calibration (a CDF from its own samples or an
  absolute path, then *Assign peaks…*), the 11 D86 correction factors (with
  a required reason and their history; GC-1's seed), methods seen and their
  mapping, the **backfill list** (*Release selected…* exports backfill
  samples), **conflicts** (*Keep existing* / *Replace*), the comparison
  standards per instrument, and the agent panel (status, clock skew, queue,
  last error, commands, *Download installer*, *Revoke token*).

## Upgrading to v3 (deviation bullets and comments)

v3.0.0 changes how analysis reports read (one bullet per deviating range,
sharp-peak rules, comments); no D2887/D86 number or results CSV changes. Read
the v3.0.0 release notes first. Nothing needs editing on the server:

- On the first v3 start the database is migrated to schema v2 (three new
  tables, nothing existing changed) after a copy to
  `data\backups\pre-migrate-1-<time>.db`; `app.log` says so.
- The four comment presets are seeded once; review or reword them on
  **Hub admin > Comment presets** (admin password) before operators use
  them, since QBench PDFs may reach customers.
- The deviation-bullet thresholds were tuned on synthetic data. Before
  relying on them, run the Analysis tab on a few real runs whose answer is
  known and adjust Settings > **Deviation Bullets** (admin password) if
  needed.
- **Rollback** (`updater.py rollback --app gc`) to v2.0.0 is safe: it starts
  on the migrated database, but its reports omit comments and use v2's
  bullets, and it writes no `report_log` rows. Comments made under v3 are
  kept and reappear after upgrading again.

### Behind the Cloudflare tunnel (https://gc.asaplabs.net)

From v3.0.0 the hub also works at https://gc.asaplabs.net: `cloudflared` on
ASAPSV1 forwards that name to `http://localhost:5560`. The hub treats only
**loopback** as a trusted proxy (`netctx.py`): from 127.0.0.1 it believes
`CF-Connecting-IP` (the real client, used for the admin-password throttle and
logs) and `X-Forwarded-Proto: https` (so the browser's `https://gc.asaplabs.net`
origin passes the cross-site guard); from any other address those headers are
ignored. Setup accepts `Host: gc.asaplabs.net` only over https, a request
carrying any forwarding header is never "the server's own console" (it spends
the hub-wide password budget, and the hub tray's pause/stop routes always
answer 403 through the tunnel), and an installer downloaded through the tunnel
with no hub URL set points agents at `https://gc.asaplabs.net`. **There is no
sign-in yet** (v3.1): anyone who reaches the address can use the operator
actions (admin actions still need the admin password). Agents send
`User-Agent: gc-agent/<version>` because Cloudflare refuses urllib's default
with 403 "error code: 1010".

## Before cutover (once, for both GCs)

The spec's "Cutover runbook", steps 1 to 4, plus the parity check. Until a GC
PC is cut over, its v1 copy on the share keeps processing and writing LEM's
CSV; the hub holds that GC's history only as backfill.

1. **v2 is running and the GC PCs can reach it.** `updater.py status` shows
   `current=v2.x`. Open port 5560 inbound in the Windows firewall on
   ASAPSV1 (elevated Command Prompt):

   ```
   netsh advfirewall firewall add rule name="GC Hub 5560" dir=in action=allow protocol=TCP localport=5560 profile=domain,private
   ```

   Check from each GC PC: `curl http://asapsv1:5560/healthz` answers
   `{"status": "ok", ...}`.
2. **Set the admin password** with the setup code ("Admin password"
   above). Then on the Instruments page set *Hub URL for installers* to
   `http://asapsv1:5560`, and **create GC-2**: *Add an instrument*, id
   `gc2`, name `GC-2`. Leave its `live_since` empty: until it is set,
   everything GC-2 sends is backfill.
3. **Set each instrument's calibration and corrections.**
   - Calibration (Instruments page, the instrument's calibration panel):
     GC-1's came from `settings.json`; check it shows as usable. For GC-2,
     copy the calibration CDF its v1 copy uses (`calibration_cdf` in that
     PC's `%USERPROFILE%\.gc_viewer_settings.json`) to
     `C:\ASAPApps\gc\data\calibration\gc2\`, enter that absolute path, *Use
     path*, then *Assign peaks…* the way GC-2's v1 Calibration page has them.
   - Corrections: check GC-1's seeded values against its
     `correction_factors.json` (the info notification from the first start
     names the file). Enter GC-2's 11 values (from GC-2's v1
     `correction_factors_json`) with a reason, and *Save corrections*.
     GC-2's samples wait as `pending_corrections` until then.
   - **Confirm that LEM holds no GC correction factors.** The hub's rows are
     already corrected; factors in LEM would be applied twice.
4. **Copy each GC's legacy folders to ASAPSV1** (read-only on the source).
   Take the paths from that PC's v1 settings
   (`%USERPROFILE%\.gc_viewer_settings.json`: `processed_cdf_dir`,
   `distill_output`, `correction_factors_json`). Copy the CDFs **first**,
   then the CSV, so every copied CDF that v1 processed has its row. Keep
   these folders: the cutover re-copies into them, and the history import
   matches rows by CSV path. In a Command Prompt (cmd.exe) on ASAPSV1:

   ```
   robocopy "<GC-1 processed_cdf_dir>" C:\ASAPApps\gc\legacy\gc1\processed_cdf /E /COPY:DAT /DCOPY:T /R:2 /W:5
   robocopy "<folder of GC-1 distill_output>" C:\ASAPApps\gc\legacy\gc1 "<its file name>" /COPY:DAT /R:2 /W:5
   robocopy "<GC-2 processed_cdf_dir>" C:\ASAPApps\gc\legacy\gc2\processed_cdf /E /COPY:DAT /DCOPY:T /R:2 /W:5
   robocopy "<folder of GC-2 distill_output>" C:\ASAPApps\gc\legacy\gc2 "<its file name>" /COPY:DAT /R:2 /W:5
   ```

   `/COPY:DAT /DCOPY:T` keeps file and folder times (a CDF without an
   injection stamp is timed by its file time). Robocopy exit codes 0 to 7
   are success; 8 or more means some files failed: read its output and
   re-run. The account the app runs as must be able to read
   `C:\ASAPApps\gc\legacy`.
5. **Parity check, GC-1 then GC-2** (next section). The go-live gate:
   PASS, with no `unexplained` difference.
6. **First full history import, both GCs** (Hub admin > *Import v1
   history*). For each instrument: pick it; *Processed CDF folder*
   `C:\ASAPApps\gc\legacy\gc1\processed_cdf`; *Results CSV*
   `C:\ASAPApps\gc\legacy\gc1\<its file name>`; *Folder aliases* the
   processed folder(s) v1 wrote into the CSV's `Source File` column, as they
   appear there, comma-separated. Press **Dry run** first and review it
   (rows not imported, conflicts, rejected and truncated files, time
   corrections); then **Start real import** and wait for the job to finish.
   Repeat for GC-2 with its folders. Imported samples are backfill: nothing
   is exported, and LEM sees nothing. Conflicts it reports are resolved on
   the Instruments page (*Keep existing* / *Replace*).

### Parity check before cutover

`tools/parity_report.py` compares the hub's numbers with v1's, row by row
(31 columns, exact strings), and names the reason for every difference. It
runs on a **scratch data folder, never on production**: the loader computes
the history there, which in production would collide with the history import.
`--copy-instrument-from` gives the scratch store production's instrument (its
calibration CDF and assignments, method map and hub correction factors, read
from production's `gc.db` read-only), plus production's `settings.json` and
comparison standards. The live hub keeps running and plays no part.

Do this after "Before cutover" steps 3 and 4, in a Command Prompt (cmd.exe,
**not** PowerShell: its `>` writes UTF-16, which the report can't read) on
ASAPSV1:

```
cd /d C:\ASAPApps\gc\current
set PY=C:\ASAPApps\gc\current\.venv\Scripts\python.exe
set P=C:\ASAPApps\gc\parity

rem GC-1: load and compute the copy in a new scratch folder (the loader creates it)
%PY% tools\load_folder.py --data-dir %P%\gc1 --copy-instrument-from C:\ASAPApps\gc\data --process --json gc1 C:\ASAPApps\gc\legacy\gc1\processed_cdf > %P%-gc1-load.json
echo %ERRORLEVEL%

rem GC-1: the report, scoped to exactly what was loaded
%PY% tools\parity_report.py --data-dir %P%\gc1 --v1-corrections "<GC-1 correction_factors_json>" --sample-ids @%P%-gc1-load.json gc1 "C:\ASAPApps\gc\legacy\gc1\<its file name>"
echo %ERRORLEVEL%
```

GC-2 is the same with `gc2` and its folders (`--data-dir %P%\gc2`,
`%P%-gc2-load.json`, GC-2's v1 `correction_factors_json`, its CSV). The copy
creates GC-2 in the scratch store, so GC-2 must exist in production with its
calibration and corrections set (step 3).

**The loader** exits 0 (loaded), 1 (loaded, but some files were rejected:
see `rejected_files` in the JSON; truncated CDFs are refused on purpose) or 2
(nothing loaded: the message says why, e.g. the instrument doesn't exist in
production or its calibration CDF is missing). It refuses `--process` on a
folder a hub has served and refuses to copy an instrument into one: the
scratch folder must be new. If a crash leaves `%P%\gc1\.gc-load-folder.lock`
behind, delete it by hand once no loader is running.

**The report** prints a verdict line, a line per tag, and the CSV and HTML it
wrote (in `%P%\gc1\reports\`), and exits 0 for PASS, 1 for FAIL. Read:

- `PASS: every difference is explained` or `FAIL: <reasons>`, then *rows
  numerically verified* (must be more than 0) and *v1 rows outside the
  scope* (rows whose CDFs were not in the copy; information only).
- `[info]` tags are expected, documented differences (see the release
  notes): `source-file` (the hub records its relative CDF path),
  `injection-time-fix` (v1's misparsed stamp), `blank-rule`,
  `auto-detect-off` and `corrections` (each claimed only when recomputing
  the way v1 did reproduces v1's row exactly; the HTML shows the largest
  absolute difference for each), `method-excluded` (an accepted non-D2887
  method), `not-in-hub`, `v1-short-row`.
- `[FAIL]` tags, and what to do:
  - `method-not-accepted`: method names the hub doesn't process (listed
    under *methods not processed*) that haven't been accepted. Only if they
    are confirmed gasoline (D7096) runs, re-run the report with
    `--accept-excluded-method <name>` for each (e.g.
    `--accept-excluded-method D7096.M`; repeatable, any case). A D2887 name
    (`SIMDISB.M`, `SIMDISTB.M`, or one mapped to D2887) can never be
    accepted: fix the method mapping in production instead, then redo the
    check in a new scratch folder.
  - `not-processed`: a sample has no result. `--process` empties the queue,
    so the sample is held (e.g. `awaiting_calibration`): fix production's
    calibration and redo the check in a new scratch folder.
  - `review-method`: a CDF with no method name. No flag accepts it; look at
    the file and decide with Ryan.
  - `no-v1-row` / `lab-id-other-time`: a loaded CDF with no v1 row. If the
    CSV was copied before the CDFs, re-copy the CSV and re-run the report.
    If v1 really never wrote a row for it (an orphan CDF; the history import
    counts these as `orphans` too), there is nothing to compare: note the
    file for sign-off and re-run the report with `--sample-ids` listing the
    JSON's `sample_ids` without that sample's id (e.g. `--sample-ids
    1,2,3,4`).
  - `ambiguous-match`, `not-verified`: see those rows in the CSV/HTML.
  - `unexplained`: a real difference. **Stop: do not cut over.** Keep the
    report's CSV and HTML and send them to Ryan.
- The HTML also lists the instrument's open conflicts; resolve those in
  production before sign-off.

After sign-off, keep the reports (copy `%P%\gc1\reports` and
`%P%\gc2\reports` somewhere safe), then delete the scratch folders
(`rmdir /s /q C:\ASAPApps\gc\parity` and the `parity-*-load.json` files).
Keep `C:\ASAPApps\gc\legacy` until both GCs are cut over.

## Cutover runbook (per GC PC)

Cut over one PC at a time, **in this exact order**, so no row is lost or
written twice between steps (spec, "Cutover runbook"). The order is: stop v1
writing, adopt, then `live_since`.

1. **Stop v1 on that PC.** Quit `run.pyw` (tray > Quit) and remove its
   autostart (Startup folder shortcut or scheduled task). Check nothing still
   listens on its port (`netstat -ano | findstr :5560`, `taskkill /F /T /PID
   <pid>`). From now on nothing processes that GC's new runs until step 6,
   and nothing writes LEM's CSV.
2. **Re-copy and re-import the delta.** Run that GC's two robocopy commands
   from "Before cutover" step 4 again (CDFs first, then the CSV, into the
   same folders), then Hub admin > *Import v1 history* for that instrument
   with the same folders and CSV path (*Load last run* fills them): *Dry
   run*, then *Start real import*. It picks up only what v1 processed since
   the first import; otherwise it is a no-op. Review the summary.
3. **Point the export at the CSV LEM tails, and adopt it** (spec D9b: the
   hub, not the agent, feeds LEM). Instruments page > that instrument >
   *Results export*: enter the share CSV that v1 wrote and LEM tails (that
   PC's `distill_output`, as a UNC path), *Set path*, then *Adopt the file
   as it is*. The hub appends after its current content, so LEM's read
   position carries on. The account the app runs as (for SYSTEM, the
   computer account `ASAPSV1$` on the share) needs write access to the file
   **and** create/rename/delete rights in its folder, for the sidecar
   (`<file>.gchub.json`), its temp file and the lock file; *Adopt* refuses
   with that reason otherwise. Rows the hub appended to its own
   `data\results\<id>_results.csv` before the switch stay there; only
   pending rows follow the adopt. Adopt only after step 1: a file that
   changes behind the hub's back is refused until it is adopted again.
4. **Check the GC PC's clock.** On the PC:
   `w32tm /stripchart /computer:asapsv1 /samples:3 /dataonly`. `live_since`
   is compared with the GC's own clock; fix an offset of more than a minute
   or two before the next step.
5. **Set that instrument's `live_since` to now** (the GC's local time) on
   the Instruments page, and *Save settings*. Anything injected before it
   that arrives later is backfill: it is listed in the backfill list and
   never exported until released.
6. **Install the agent.** Instruments page > that instrument > *Download
   installer* (it mints a new token; downloading again revokes the
   previous one). Copy the unzipped folder to the PC and **double-click
   `install.pyw`**, so it runs with the Python that runs `run.pyw` (which
   has `pystray` and `Pillow`). It takes `watch_dir` from that PC's v1
   settings, registers autostart and starts the agent. It does **not** ask
   for a results mirror: the mirror is optional and off. Only if LEM tails a
   CSV local to that PC, which the hub can't write, run the installer from a
   Command Prompt with that Python and `--mirror-path "<csv>"` (it adopts
   the existing file). Delete `install.json` afterwards when asked (it holds
   the token). Files v1 already processed dedupe by sha256 against the
   imported copy.

Then watch the first sync on the Instruments page: the agent panel turns
healthy (clock skew under 2 minutes, queued files draining, no rejected
files), the first live sample is final, the *Results export* shows no
pending rows or refusal, and LEM picks up the hub's first appended row. Agent
commands (*restart*, *pause*, *resume*, *retry-rejected*) are taken at the
agent's next heartbeat. Only after the last GC is cut over is v1 retired.

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

## Hub tray on ASAPSV1

The hub runs in the background under the updater (a scheduled task, launched
with `DETACHED_PROCESS`, as SYSTEM or the updater's service account), so it
has no window and no icon: a tray icon inside it would sit in session 0,
where nobody sees it. The **hub tray** (`tray\hub_tray.pyw`, shipped in every
release) is a separate small program for the admin logged on to ASAPSV1. It
shows how the hub is doing and lets you pause it or stop it if it is slowing
the server down. It talks to the hub at `http://127.0.0.1:5560` only.

**Install (once, as the admin who uses ASAPSV1).** Log on to ASAPSV1 (RDP),
open a command prompt (not elevated) and run, via the `current` junction so
autostart follows every update:

```
C:\ASAPApps\gc\current\.venv\Scripts\pythonw.exe C:\ASAPApps\gc\current\tray\hub_tray.pyw --install
```

This registers the tray to start at **your** logon
(`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, value
`ASAPLabs GC Hub Tray`) and starts it now. Only one tray runs per logon
session (a `Local\` mutex: a second start in the same session exits; two
RDP sessions each get their own). `--uninstall` removes the autostart (a running tray
stays until *Exit tray*). The tray runs with the release's own `.venv`:
`requirements.txt` pins `pystray` (Windows only) and `psutil`, next to the
`Pillow` and `six` pins that were already there, so nothing else is
installed. When the updater switches to a new release, the tray notices the
new version and restarts itself from `current` (so the old release folder is
not held open when the updater prunes it). Its log is
`%LOCALAPPDATA%\ASAPLabs\gc-hub-tray.log`. Settings are optional: copy
`tray\tray.example.json` to `%APPDATA%\ASAPLabs\gc-hub-tray.json` and keep
only what you change (port, the updater's paths, the amber thresholds).
Without a `port` there, the tray uses the `gc` entry's port from the
updater's `config.json` when it can read it, else 5560.

**The icon.** Green: running. Amber: busy (the hub process has used more
than half a CPU core for a whole minute, or more than 25 jobs are due) or
processing is paused. Red: stopped or not answering. It polls
`GET /api/hub/status` every 5 s, and less often (up to once a minute) while
the hub does not answer. The status is cheap and never waits on the
database: its queue numbers come from a cache the hub refreshes every 5 s
(`stale: true` if that could not be read for 15 s), so a busy or locked
store can never slow the updater's `/healthz`, which carries the same numbers
under `hub`. Polling it is not activity, so it never blocks the 3 AM
restart (nor does a paused hub's waiting queue).

**The menu (right-click).**

| Item | What it does |
|---|---|
| Status line | Version · running/processing paused/stopped · CPU (share of one core) · RAM · jobs queued |
| Open in browser | `http://localhost:5560` |
| Pause processing / Resume processing | Stops (starts) the hub's background work: the Worker, the export to the results CSVs and the nightly maintenance (a job or an append in progress finishes first; the icon changes when it has). The web app keeps serving and the agents keep sending: new samples wait as *received* and are processed on Resume. It survives a restart (stored in `gc.db`) and a notification stays up in the app while paused. Admin password |
| Restart (Restart & install vX) | Same as Settings > Restart: a plain restart, or installs the staged release |
| Stop hub | Stops the hub completely (below). If it is in the middle of something (a QBench upload, a processing job, an export append, an admin job or a diagnostics build), the tray names it and asks whether to **stop anyway**. Admin password |
| Start hub | Runs the updater's `resume` (below) |
| Exit tray | Closes the icon only; the hub is not touched |

The admin password is asked for when an action needs it and kept **in
memory** until it has not been used for 15 minutes (never on disk; set
`remember_password` to false to be asked every time). Pause, Resume and Stop
are refused from any other machine: they answer only on loopback
(`127.0.0.1`/`::1`) with a loopback `Host` (so a web page cannot reach them
by DNS rebinding), with the admin password, as JSON. The pause notice and
the `paused` file name the Windows user who did it, e.g.
`paused by ryan (127.0.0.1)`.

**Why Stop pauses the updater.** The updater restarts any app that stops
serving within ~20 s (`supervise()`), so simply exiting would be undone. Stop
therefore writes the updater's own hold, `C:\ASAPApps\gc\data\paused` (the
file `updater.py pause --app gc` writes; the text says who stopped it and
when), then stops the hub's threads and exits. A paused updater normally
makes the hub start its own replacement when it restarts; an explicit Stop
never does. While the file exists, the updater leaves gc alone (it still
stages new releases, but does not switch or start them).

Not handled, because they are narrow and a person is at the keyboard: a Stop
in the seconds while the updater itself is switching releases or answering a
*Restart & install* (the updater's `switching` hold and the new release's
start can race the exit; if the hub comes back, stop it again), and a Stop
while Settings > Restart is already restarting the hub (the restart never
respawns after a Stop, but the updater may already have been asked to
switch).

**How Start works.** *Start hub* runs
`updater.py resume --app gc --config C:\ASAPApps\updater\config.json` (with
the release's Python; the updater needs nothing else), which deletes
`paused`; the updater's next supervision pass (within ~20 s) starts the hub,
and the icon turns green once it answers. The data folder and the updater
are Administrators-only, so from an unelevated tray this usually needs
elevation: the tray tries the command, then removing the file itself, and
then offers to run the command as an administrator (a UAC prompt). By hand,
from an elevated prompt:

```
"C:\Program Files\Python314\python.exe" C:\ASAPApps\updater\updater.py resume --app gc --config C:\ASAPApps\updater\config.json
```

(use the updater's own Python, see *Before you start*). The same caveat means
an unelevated tray may show a stopped hub as "not responding" rather than
"stopped (updater paused)": it cannot see the `paused` file; the menu offers
Start hub either way, and then only says the hub *should* start within
~20 s if the updater is running (if it stays red, check the updater's
scheduled task and `updater.log`).

## After setup

- Everyday releases: [`RELEASING.md`](RELEASING.md).
- Logs: `C:\ASAPApps\gc\data\app.log` for the app, `C:\ASAPApps\updater\updater.log`
  for staging, health checks, switches and rollbacks.
- Back up `C:\ASAPApps\gc\data\`. The hub makes its own nightly copy of
  `gc.db` and `settings.json` in `data\backups\`, but on the same disk; the
  store, the CDFs under `data\cdf\` and the results files are the only copy
  of that state on this server.
