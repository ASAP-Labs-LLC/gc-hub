# Releasing

How a version number becomes a running GC Hub on ASAPSV1, and what an agent
needs to know before cutting one. Adapted from `coa-reviewer/RELEASING.md`;
the updater is the same program, and gc is one more entry in its config.

**Read §5 before your first release.** Deploys are unattended: pushing a tag
ships to the lab without anyone clicking anything.

First-time server setup (PAT, `config.json` entry, credentials, settings) is
in [`DEPLOY.md`](DEPLOY.md).

---

## 1. The pipeline, end to end

```
you: git tag -a v1.2.3 && git push origin v1.2.3
        │
        ▼
.github/workflows/release.yml   (triggers on tags matching v*)
        │  job test = .github/workflows/ci.yml (also runs on every push/PR):
        │     Python 3.12 and 3.14 on Linux: pinned deps, both suites with
        │     GC_REQUIRE_DEPS=1 (a missing dep fails, never skips), identity
        │     checks against coa-reviewer's latest release, a dry-run package;
        │     plus an advisory Windows 3.14 install + /healthz boot
        │  job exports = .github/workflows/exports-ci.yml: the export tests
        │     on Windows (file locking and share semantics for real)
        │  job agent = .github/workflows/agent-ci.yml: the GC-PC agent on
        │     Python 3.9 and 3.14, Linux and Windows
        │  any of them red means no release
        │  job publish (the only job with a write token), from a fresh checkout,
        │  runs scripts/package_release.sh <tag> dist, which
        │     copies tracked source only (state and dev files excluded by name)
        │     writes the tag into VERSION
        │     refuses if state leaked in or a required file is missing
        │     builds gc-hub-<tag>.zip + gc-hub-<tag>.zip.sha256
        │  and creates the release; re-running it is safe (a complete release
        │  is left alone, a partial one is replaced, the tag is kept)
        ▼
GitHub Release  v1.2.3   ← this is now "latest"
        │
        ▼
C:\ASAPApps\updater\updater.py   (polls every 5 min, on ASAPSV1)
        │  compares latest tag with C:\ASAPApps\gc\current\VERSION
        │  downloads, VERIFIES THE CHECKSUM, unpacks to releases\v1.2.3\
        │  builds that release's .venv from its requirements.txt
        │  starts it on scratch port 15560 with an empty data dir and
        │  polls /healthz. If unhealthy: records the failure and stops.
        ▼
        │  if healthy: waits (auto_switch is false for gc, so it waits
        │  for someone to press Restart; see §4)
        ▼
   repoints the `current` junction, restarts, re-checks /healthz
        │  if unhealthy after the switch: ROLLS ITSELF BACK
        ▼
   live on port 5560
```

**gc-hub is a private repo** (unlike COA). The updater downloads with the
fine-grained PAT stored in Credential Manager as `asaplabs-github`, and that
PAT must include `ASAP-Labs-LLC/gc-hub` or the updater cannot see any release.

To build the same zip locally (it is what CI runs):

```bash
bash scripts/package_release.sh v0.0.0-local "$TMPDIR/gcpkg"
unzip -l "$TMPDIR/gcpkg/gc-hub-v0.0.0-local.zip"
```

In a git checkout the script packages **tracked files only** (`git ls-files`,
then the exclude rules on top), so an untracked scratch file does not ship,
but uncommitted edits to tracked files do: build from a clean tree if the zip
matters. Outside a git checkout (a plain copy) it packages the whole working
tree and says so. It needs `bash`, `rsync` and `zip`; it is tested with rsync
3.x (Homebrew `rsync` on the Mac, the runner's on CI). The stock macOS
`/usr/bin/rsync` (openrsync, or 2.6.9 on older systems) is untested, so use
Homebrew rsync or let CI build it.

## 2. Choosing the number

`MAJOR.MINOR.PATCH`, tag prefixed with `v`.

| Bump | When | Examples here |
|---|---|---|
| **PATCH** `v1.2.3 → v1.2.4` | A fix that changes nothing about how the app is used | A crash on an unreadable CDF, a typo in a label, a log message |
| **MINOR** `v1.2.3 → v1.3.0` | New behaviour, existing behaviour unchanged | A new settings field, a new API route, a new export option |
| **MAJOR** `v1.2.3 → v2.0.0` | Something a person or another program must know about | A results-CSV column change, a settings key renamed, an API route removed |

Two extra rules matter more than the letter of semver, because this app
deploys itself:

- **Bump MAJOR for anything an analyst would be surprised by**, even if it is
  technically a minor feature. The number is how a human decides whether to
  read the release notes before the lab hits it.
- **Anything that changes how results are calculated, recorded or displayed is
  MAJOR**, regardless of size: the D2887/D86 numbers, calibration and carbon
  labelling, the results CSV, report PDFs, what gets uploaded to QBench. Those
  are the changes a health check cannot catch (§5).

gc-hub versions independently of coa-reviewer and lab-equipment-manager.

**v1.x after v2 (`maint/v1`).** From v2.0.0 on, `main` is the hub. Fixes for
the v1.x share copies are made on the branch `maint/v1` (cut at v1.1.0) and
hand-copied (DEPLOY.md, "Updating the share copies"). **Never publish a v1.x
GitHub release as latest**: the updater installs whatever GitHub calls
latest (§6), so it would downgrade ASAPSV1 to v1. If a v1.x fix needs a tag,
create its release with `--latest=false` and confirm `gh release view` still
shows the v2 release as latest.

**Release notes.** `docs/release-notes/<tag>.md` heads the GitHub release. A
MAJOR release must list every change to how results are calculated, recorded
or displayed (v2.0.0's is the model).

## 3. Cutting a release

From a clean checkout on `main`, with both suites passing:

```bash
.venv/bin/python -m pytest tests/ -q
node tests/js/run.js
git push origin main                       # the commit first
git tag -a v1.2.3 -m "One line saying what changed and why"
git push origin v1.2.3
```

That is the whole thing. Do **not**:

- create a `VERSION` file by hand. CI writes it and `.gitignore` excludes it;
  a checkout reports `dev`.
- edit a release folder on the server. Releases are immutable; fix forward
  with a new tag.
- push the tag before the commit it points at.

Then confirm CI built it:

```bash
gh run list --workflow=release.yml --limit 1   # expect: completed  success
gh release view v1.2.3                         # expect: 2 assets, .zip and .zip.sha256
```

If the run failed after the release was created (or you are unsure), re-run
it: the publish step leaves a release that already has its `.sha256` asset
untouched and replaces a partial one, keeping the tag.

### Dependency pins

`requirements.txt` pins everything with `==`: the direct dependencies, and
every transitive one in a `# --- transitive` block frozen from a clean Python
3.14 venv built from the direct pins alone, plus a small platform block with
environment markers (`tzdata` on Windows, which a macOS freeze cannot show).
`tests/test_release_package.py` requires `==` on every line, and checks that
the hub neither pins nor imports the tray packages (`pystray`, `watchdog`:
v1's `run.pyw` only; the agent declares its own deps in
`agent/requirements-agent.txt`). `Pillow` stays pinned as a transitive
dependency of `reportlab`/`xhtml2pdf`.

The updater builds each release's venv with **its own interpreter**:
`build_venv` runs `sys.executable -m venv`, then `pip install --quiet -r
requirements.txt` with the pip bundled in that venv and no constraints file
(`coa-reviewer/deploy/updater/updater.py`). That interpreter was recorded as
**CPython 3.14.4 on ASAPSV1 on 2026-08-21** (the header of
`coa-reviewer/requirements.txt`), so the pins target Python 3.14 on 64-bit
Windows. CI tests them on Python 3.12 and 3.14 on Linux and installs them into
a fresh Windows 3.14 venv and boots the app (`.github/workflows/ci.yml`; the
Windows job is advisory for now). Every pinned version was also checked to
have a `cp314` `win_amd64` (or pure-Python) wheel on 2026-09-26.

Before the first release, and whenever the server's Python may have changed,
**confirm the updater's interpreter on ASAPSV1**:

```
schtasks /query /tn "<updater task name>" /v /fo list
```

Take the executable from "Task To Run", then run it:

```
"<that python.exe>" -c "import sys,struct;print(sys.version, sys.executable, struct.calcsize('P')*8)"
```

It must be **64-bit CPython** (there are no 32-bit Windows wheels for scipy),
and 3.12 or newer (`numpy==2.5.3` and `scipy==1.18.1` declare
`Requires-Python >=3.12`). A different minor version than 3.14 needs its own
wheel check. An interpreter that cannot build the venv is a safe failure (the
release is never deployed, and `updater.log` says why), but nothing ships
until it is fixed.

When bumping a pin:

- Refresh the transitive block from a clean 3.14 venv built from the direct
  pins only (not the dev tools), and re-check the platform block by hand from
  the packages' `Requires-Dist` markers.
- Check that a **Windows wheel** exists for Python 3.14 for anything compiled
  (`numpy`, `scipy`, `pandas`, `netCDF4`, `cftime`, `Pillow`, `lxml`,
  `cryptography`, ...). Without a wheel, pip tries to compile from source and
  fails on a server with no compiler. `pip download --no-deps --only-binary=:all:
  --platform win_amd64 --python-version 3.14 -r <file>` does this from a Mac.
- `kaleido` is pure Python but drives a Chrome install at runtime. The QBench
  PDF upload (`selenium`, `chromedriver-autoinstaller`) also needs Google
  Chrome installed on ASAPSV1.

## 4. Confirming it reached the lab

On ASAPSV1 (or via the **Lab Apps Status** desktop shortcut):

```
python C:\ASAPApps\updater\updater.py status --config C:\ASAPApps\updater\config.json
```

```
gc: SERVING on 5560  current=v1.2.3 junction->v1.2.3 staged=v1.2.3 healthy=True
```

`current` is what is running. `staged` ahead of `current` means it is built and
healthy and waiting. With `"auto_switch": false` (gc's setting for now), a
staged release goes live when someone presses **Settings > Restart**, whose
label becomes "Restart & install vX.Y.Z" once a release is staged. That button
asks the updater to switch (the `restart_update` handshake) and falls back to a
plain restart if the updater refuses or does not answer.
`C:\ASAPApps\updater\updater.log` gives the reason in plain words.

Before `auto_switch` can be turned on, `/healthz` must count a running QBench
upload or a queued reprocess as an active session (spec, Open items).

## 5. What automatic deployment does and does not protect you from

A release **is** blocked from going live if it fails to start, crashes on
import, has a bad checksum, cannot build its venv, or does not answer
`/healthz` with its own tag within 60 s. A release that goes live and then
fails `/healthz` is **rolled back automatically**.

A release that **starts perfectly and computes something wrong** is not caught
by anything. `/healthz` proves the app is alive, never that its boiling points
are right. The health check runs against an empty data dir (the hub creates a
store and `gc1` there, with no calibration or corrections), so it exercises
none of the numerics. For the hub, the v1 parity report (`tools/parity_report.py`,
the 2A1 plan's T6 runbook) is the check that the numbers are right.

So if a change could produce a wrong number on a report, test it properly
first, or publish it without deploying (below).

**Rolling back** (immediate, one command):

```
python C:\ASAPApps\updater\updater.py rollback --app gc
```

**Publishing without deploying**: a tag with a pre-release suffix
(`v1.2.3-rc.1`, anything containing `-`) is published as a **prerelease** by
`release.yml` automatically. `/releases/latest` skips prereleases, so the
updater never stages an rc. To do the same for an existing release, mark it as
a prerelease by hand:

```bash
gh release edit v1.2.3 --prerelease          # updater ignores it
gh release edit v1.2.3 --prerelease=false    # hand it over when ready
```

It takes ~20 seconds to take effect. Only use it on a release that is **not yet
deployed**: marking the running release as a prerelease moves "latest"
backwards, which the updater treats as a new release to stage. To pull a bad
release, use `rollback`.

**Stopping deploys** while you work:

```
python C:\ASAPApps\updater\updater.py pause  --app gc
python C:\ASAPApps\updater\updater.py resume --app gc
```

While the updater is paused it will not relaunch the app, so the app's own
Restart (and the 3 AM auto-restart) starts its own replacement instead of just
exiting (`restart_policy.should_respawn`).

## 6. Gotchas that will bite an agent

- **The updater tracks whatever GitHub calls *latest*, not the highest
  version.** GitHub's latest is the most recently created non-prerelease, so
  publishing `v1.0.9` after `v1.2.0` stages `v1.0.9`. That is how you
  re-release a known-good build, and it is also how a mistyped tag ships.
- **A re-released older build (or any tag with a suffix) can only be
  installed with the CLI `switch`, not the Restart button.** Restart asks for
  a switch only when `restart_update.is_upgrade` says the staged tag is a
  strictly newer plain `vX.Y.Z` than the running one; an older version, or a
  tag like `v1.2.3+build.1`, is never offered: the button stays "Restart" and
  restarts the same version. On ASAPSV1:
  `python C:\ASAPApps\updater\updater.py switch --app gc --tag v1.0.9`.
  (For an immediate undo, `rollback` is still the tool; see §5.)
- **Tags containing `-` are prereleases** (`v1.2.3-rc.1`): published, never
  staged. Hand one over with `gh release edit <tag> --prerelease=false`; it
  then has a suffix, so it too needs the CLI `switch` (above).
- **The tag becomes a folder name** (`releases\v1.2.3\`). `package_release.sh`
  accepts only `vMAJOR.MINOR.PATCH` with an optional `-prerelease`/`+build`
  suffix of letters, digits, dots and hyphens that does not end in `.` or `-`
  (`v1.2.3`, `v1.2.3-rc.1`); anything else fails the release job.
- **Only the tag decides the version.** `VERSION` is written from
  `github.ref_name`. A release created by hand in the GitHub UI has no
  `VERSION` and will not deploy correctly.
- **`/healthz` must keep reporting `status`, `version`, `active_sessions` and
  `idle_seconds`.** `tests/test_healthz.py` pins that contract.
- **`supervisor.py` and `restart_update.py` must stay byte-identical to
  coa-reviewer's.** The updater imports whichever app's copy it finds first.
  Their tests compare against `../coa-reviewer` when that checkout exists; CI
  clones coa-reviewer's latest release there, so a drift fails CI.
- **The health check passes `--no-tray`.** `app.py` accepts it and ignores it.
  Any other unknown flag is an error on purpose, so a typo in the updater
  config fails the health check instead of being ignored.
- **`GC_DATA_DIR` is required.** v2 exits with code 2 without it, so the
  updater's `data_env` must stay `GC_DATA_DIR` (DEPLOY.md Step 2).
- **A v1.x release published after v2 is a downgrade** (the updater follows
  "latest"). v1.x fixes live on `maint/v1` and are published, if at all, with
  `--latest=false` (§2).
- **A docs-only release still deploys** and restarts the app on the next
  Restart.
- **State is excluded from the zip by name** in `scripts/package_release.sh`.
  A new kind of runtime file written next to the code (rather than under
  `GC_DATA_DIR`) needs an exclusion there and in
  `tests/test_release_package.py`.

## 7. If something goes wrong

| Symptom | Where to look |
|---|---|
| Tag pushed, no release | `gh run list --workflow=release.yml`. The test job fails on a red suite or a missing dependency (`GC_REQUIRE_DEPS`); publish fails if `package_release.sh` refused the tag, found leaked state or a missing file. Re-running is safe |
| Release exists, never staged | `updater.log`: the PAT does not include gc-hub, a checksum mismatch, or the venv failed to build (see §3, Dependency pins) |
| Staged but never deployed | Expected until someone presses Restart (`auto_switch` is false). Otherwise `updater.py status`, then `updater.log` |
| Deployed and broken | `updater.py rollback --app gc`, then read `C:\ASAPApps\gc\data\app.log` |
| App keeps restarting | `updater.log`: after 3 starts in 15 min the supervisor gives up and logs CRITICAL |
