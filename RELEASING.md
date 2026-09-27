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
        │  installs requirements.txt + requirements-dev.txt on Python 3.12
        │  runs the Python suite and the JS suite: red means no release
        │  runs scripts/package_release.sh <tag> dist, which
        │     copies source only (state and dev files excluded by name)
        │     writes the tag into VERSION
        │     refuses if state leaked in or a required file is missing
        │     builds gc-hub-<tag>.zip + gc-hub-<tag>.zip.sha256
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

### Dependency pins

`requirements.txt` pins every direct dependency with `==`. The updater builds
each release's venv with `python -m venv` using **the updater's own Python**
(`sys.executable` in `build_venv`) and then `pip install --quiet -r
requirements.txt`, on Windows. Transitive dependencies are not pinned, so
pip resolves them at build time.

The pins were validated on **Python 3.12 on macOS only** (the dev `.venv`),
and CI checks them on Python 3.12 on Linux. Neither proves they build on
ASAPSV1. Before the first release, and whenever you bump a compiled package:

- **Confirm the updater's Python version on ASAPSV1** (`C:\ASAPApps\updater\.venv\Scripts\python.exe --version`,
  or whichever interpreter runs the updater's scheduled task). It has not been
  checked from here. `numpy==2.5.3` and `scipy==1.18.1` declare
  `Requires-Python >=3.12`, so an older interpreter cannot build the venv. That
  failure is safe (the release is never deployed, and `updater.log` says why),
  but nothing ships until it is fixed.
- Check that a **Windows wheel** exists for that Python version for the
  compiled packages: `numpy`, `scipy`, `pandas`, `netCDF4` (and its `cftime`
  dependency), `Pillow`, `watchdog`. Without a wheel, pip tries to compile from
  source and fails on a server with no compiler. `kaleido` is pure Python but
  drives a Chrome install at runtime.
- The QBench PDF upload (`selenium`, `chromedriver-autoinstaller`) needs Google
  Chrome installed on ASAPSV1.

## 4. Confirming it reached the lab

On ASAPSV1 (or via the **Lab Apps Status** desktop shortcut):

```
python C:\ASAPApps\updater\updater.py status --config C:\ASAPApps\updater\config.json
```

```
gc: SERVING on 5560  current=v1.2.3  junction->v1.2.3  staged=v1.2.3 healthy=True
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
are right. The health check runs against an empty data dir with no watch
folder and no calibration, so it exercises none of the numerics.

So if a change could produce a wrong number on a report, test it properly
first, or publish it without deploying (below).

**Rolling back** (immediate, one command):

```
python C:\ASAPApps\updater\updater.py rollback --app gc
```

**Publishing without deploying**: mark the GitHub release as a prerelease.
`/releases/latest` skips prereleases, so the updater never sees it:

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
  publishing `v1.0.9` after `v1.2.0` deploys `v1.0.9`. That is how you
  re-release a known-good build, and it is also how a mistyped tag ships.
- **The tag becomes a folder name** (`releases\v1.2.3\`). Keep tags to letters,
  digits, dots and hyphens. `package_release.sh` refuses a tag containing `/`.
- **Only the tag decides the version.** `VERSION` is written from
  `github.ref_name`. A release created by hand in the GitHub UI has no
  `VERSION` and will not deploy correctly.
- **`/healthz` must keep reporting `status`, `version`, `active_sessions` and
  `idle_seconds`.** `tests/test_healthz.py` pins that contract.
- **`supervisor.py` and `restart_update.py` must stay byte-identical to
  coa-reviewer's.** The updater imports whichever app's copy it finds first.
  Their tests compare against `../coa-reviewer` when that checkout exists (they
  skip on CI).
- **The health check passes `--no-tray`.** `app.py` accepts it and ignores it.
  Any other unknown flag is an error on purpose, so a typo in the updater
  config fails the health check instead of being ignored.
- **A docs-only release still deploys** and restarts the app on the next
  Restart.
- **State is excluded from the zip by name** in `scripts/package_release.sh`.
  A new kind of runtime file written next to the code (rather than under
  `GC_DATA_DIR`) needs an exclusion there and in
  `tests/test_release_package.py`.

## 7. If something goes wrong

| Symptom | Where to look |
|---|---|
| Tag pushed, no release | `gh run list --workflow=release.yml`. The job fails on a red suite, or if `package_release.sh` found leaked state or a missing file |
| Release exists, never staged | `updater.log`: the PAT does not include gc-hub, a checksum mismatch, or the venv failed to build (see §3, Dependency pins) |
| Staged but never deployed | Expected until someone presses Restart (`auto_switch` is false). Otherwise `updater.py status`, then `updater.log` |
| Deployed and broken | `updater.py rollback --app gc`, then read `C:\ASAPApps\gc\data\app.log` |
| App keeps restarting | `updater.log`: after 3 starts in 15 min the supervisor gives up and logs CRITICAL |
