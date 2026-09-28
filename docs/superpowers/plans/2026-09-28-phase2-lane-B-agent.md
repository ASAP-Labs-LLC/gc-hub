# Phase 2, Lane B: the GC-PC push agent (2B2 package), TDD plan

Spec: `docs/superpowers/specs/2026-09-28-phase2-multi-instrument-hub-design.md`
("Agent (2B2)", "Security", "Ingest API (2B1)"). Contract: `…-phase2-contracts.md` §1,
which this lane builds the **client** side of, exactly.

Branch `lane/agent`. New files only (plus `.github/workflows/agent-ci.yml`). No hub
module is imported by the agent; `CSV_HEADER` is copied as a literal.

## Layout

```
agent/
  agent_main.py            entry; import has no side effects (the update smoke test imports it)
  requirements-agent.txt   informational: pystray, Pillow
  launcher.pyw             resident supervisor + stdlib helpers the installer reuses
  install.pyw              installer (interactive; --dry-run --root --yes for tests)
  gc_agent/
    __init__.py
    util.py        atomic writes, local ISO time, sha256 helpers
    config.py      agent.json defaults / validate / load / save (atomic)
    ledger.py      sqlite3 ledger.db: files(path,size,mtime_ns) + kv
    scanner.py     recursive *.CDF (any case), stability + exclusive-open check
    winutil.py     exclusive-open probe (ctypes CreateFileW share=0; POSIX: always True)
    client.py      urllib client for §1 (HubResponse / NetworkError)
    sender.py      oldest-first send; sent / rejected / hold / backoff (5→300 s)
    mirror.py      results mirror: sidecar rules, pending roll-forward, adopt()
    updater.py     package check / verify / safe unzip / smoke / switch / keep 3
    core.py        Agent: loop, state, heartbeat, commands, actions queue
    tray.py        pystray + Pillow (optional); colours; menu; status lines
    settings_ui.py tkinter Settings dialog (runs as `agent_main.py --settings`)
agent/build_package.py      deterministic package zip; prints sha256 (moved from scripts/ in review)
tests/agent/               fakehub.py (http.server, all §1 routes), tests per module
```

Root on the GC PC (`--root`, default `%LOCALAPPDATA%\ASAPLabs\gc-agent`, or derived from
`versions/<v>/agent_main.py`): `launcher.pyw`, `versions/<v>/` (each with
`PACKAGE_SHA256` written by the installer/updater), `current.txt`, `previous.txt`,
`agent.json`, `ledger.db`, `agent.log`, `launcher.log`, `revert.json` (launcher → next
heartbeat), `bad_packages.json` (shas the launcher reverted from; the updater skips them).

## Decisions beyond the contract (reported to the integration critic)

1. sha check applies to 201, 200 **and** 202 (all three return `sha256`). A mismatch marks the
   file **rejected** (`hub acknowledged sha … for …`), never sent.
2. A file over 25 MB is rejected locally (`too large to send (N bytes > 25 MB)`) without
   uploading.
3. A 2xx with a non-JSON / malformed body is treated like a 5xx (backoff).
4. The launcher's revert is reported in the heartbeat's `last_error`
   (`reverted from <v> to <v>: 3 crashes …`) until one heartbeat succeeds.
5. The mirror sidecar may carry an extra `"pending"` object during an append (crash-safe
   roll-forward); readers of the four contract keys are unaffected.
6. `results` pages are fetched until `more` is false (max 20 pages per cycle);
   rows whose `header` differs from the literal `CSV_HEADER` stop mirroring with an error.
7. Test-only env `GC_AGENT_HEARTBEAT_SECONDS` shortens the heartbeat interval (the loop waits
   `min(1 s, poll_seconds)`).
8. Installer download layout proposed to 2B1: `install.pyw`, `launcher.pyw`, `install.json`
   (`{"hub_url","token"}`) and `agent-package.zip` (+ optional `agent-package.json`
   `{"version","sha256"}`). Without the zip the installer downloads it from the hub.

9. **Spec change D9b (mid-lane, from the coordinator):** the hub appends to the share CSVs LEM
   tails, so the agent's results mirror is optional and **off by default**. `results_mirror_path`
   defaults to `""`; the installer never asks for it or defaults it from v1's `distill_output`;
   only an explicit `--mirror-path` sets it (adopting an existing file). A reinstall keeps a path
   already in agent.json. `adopt-mirror` is a no-op when mirroring is off. The mirror code and
   its tests stay (done before the change).
10. **Critic review fixes:** a scan pass hashes at most 200 files or 2 s and queues them in one
   transaction; polls walk with `os.scandir` against an in-memory key set (no per-file SQL);
   `poll_seconds` is floored at 1 s unless `GC_AGENT_FAST_POLL=1` (tests); ledger keys compare
   `normcase(normpath(path))` and `watch_dir` is normalised; bytes already sent are marked sent
   without uploading (index on sha256); packages that fail to unpack/smoke are kept in
   `failed_packages.json` and retried at most once per 24 h; the smoke test imports
   `agent_main, gc_agent.tray`; only the five §1 commands are obeyed; the launcher waits at
   least 1 s between exit-3 relaunches and backs off after 5 quick ones; the builder moved to
   `agent/build_package.py` (the hub release drops `scripts/`); the installer updates a running
   agent's `hub_url`/`token` in place and offers to delete `install.json`.
11. **Second review:** the scan's time budget starts at the first *unknown* file (walking known
   files is free) and every pass queues at least one ready file before a cap can stop it;
   `FALLING_BEHIND_PASSES` (10) capped passes with no progress set `last_error`
   ("scan falling behind: …"). The ledger's in-memory key set changes only after a commit;
   sqlite errors in the scan/send path are logged and reported, and the loop carries on. The
   ledger has `PRAGMA user_version` (2) and migrates the first layout (no `pkey`). An `OSError`
   while unpacking an update is recorded in `failed_packages.json`. The installer's in-place
   update re-reads, merges and verifies (retrying) so it never clobbers the agent's own
   `paused` write; the launcher's atomic write retries sharing violations.
   *Possible optimisation (not done):* each poll still walks the whole watch folder; with a very
   large history it could skip folders whose mtime has not changed since the last pass (NTFS
   updates a folder's mtime when entries are added or removed).

## Tasks (strict TDD: write the test, run it red, implement, run green, commit)

### T1 fake hub + util + config
- `tests/agent/fakehub.py`: ThreadingHTTPServer on 127.0.0.1:0; records requests; auth
  check (401 on wrong token); `/api/ingest` scripted responses or default (201 new sha,
  200 duplicate); heartbeat with command queue; results with `after`/`limit`/`more`;
  package + package.zip.
- test_config: defaults filled; unknown keys kept; invalid hub_url / token / numbers →
  `ConfigError` listing the field; save is atomic (no temp left, content round-trips);
  `paused` round-trips.
- util: `local_iso(ts)` format `YYYY-MM-DDTHH:MM:SS`; `atomic_write_bytes` replaces.

### T2 ledger
- new DB has tables; `known()` false→true after `add_queued`; key is (path,size,mtime):
  same path with new size is unknown; `next_queued` oldest mtime first; `mark_sent`,
  `mark_rejected(reason)`; `requeue_rejected()` count; `counts()`; kv round-trip
  (`results_seq` default 0); `drop_queued_for_path` except current key; reopen persists.

### T3 scanner
- finds `a.CDF`, `b.cdf`, `c.Cdf`, ignores `x.txt`; recursion on/off; a file younger than
  `stable_seconds` whose size changes is not ready; ready after stable time (fake clock);
  an old mtime is ready at once; exclusive-open probe false → not ready; files already in
  ledger are skipped (no re-hash: hash function counted); changed file → new key queued
  and stale queued row dropped. Windows probe (`winutil.can_open_exclusively`) returns
  True on POSIX; on Windows false while another handle holds the file (skip on POSIX).

### T4 client
- ingest request carries exactly: Bearer, `X-GC-SHA256` lower hex, `X-GC-Mtime`
  `YYYY-MM-DDTHH:MM:SS`, `X-GC-Filename` = `quote(basename)` (test with `Sé #1.CDF`),
  `Content-Type: application/octet-stream`, body bytes; HTTP errors return
  `HubResponse(status, json)`; connection refused → `NetworkError`; non-JSON body →
  `json=None`; heartbeat/results/package/package.zip paths and query.

### T5 sender state machine (against fakehub)
- 201 match → sent (+sample_id); 200 duplicate → sent; 202 → sent;
- 201/200/202 with other sha → rejected, reason names both;
- 400, 409, 413, 415 → rejected with the hub's error, next file still sent;
- 401, 403, 404, 405, 418 → hold: file stays queued, state `auth-error`, no further
  request until +300 s (fake clock) or `config_changed()`; retry succeeds → clears;
- 429, 500, 503, network error → backoff 5,10,20,…,300 cap, state `hub-unreachable`;
  success resets to 5; file stays queued;
- oldest-first order; paused sends nothing; file >25 MB rejected locally;
- file changed between scan and send → row dropped, not sent;
- retry-rejected requeues and they send.

### T6 mirror
- no file, no sidecar → creates with header + rows, sidecar {size,sha256,seq,adopted_at};
  `line` bytes appended verbatim (incl. `\r\n`, quotes, non-ASCII);
- existing file without sidecar → refused, file untouched;
- file changed (same size, other bytes) / grown / shrunk since sidecar → refused;
- sidecar but file missing → refused;
- pending roll-forward: crash after append before commit → accepted;
- rows with seq ≤ current skipped; seq advanced in ledger kv; `more` paging;
- response header ≠ CSV_HEADER → refused;
- `adopt()`: header ok → sidecar written, returns size + last row; bad header →
  `MirrorError`, no sidecar; missing file → clears sidecar; adoption after refusal
  lets the next append proceed.

### T7 core: heartbeat + commands + pause
- payload has exactly the §1 keys and types; state values;
- command `pause` → paused persisted in agent.json, next heartbeat `paused`, no sends;
  `resume` → persisted false; `retry-rejected` requeues; `adopt-mirror` adopts;
  `restart` → run() returns 3; unknown command ignored (logged);
- heartbeat sent again immediately on state change; mirror pulled after heartbeat;
- revert.json is reported in last_error and removed after a successful heartbeat;
- agent.json edited on disk → reloaded, hold cleared;
- heartbeat `agent_package_sha256` ≠ own sha → update check requested.

### T8 updater + build_agent_zip
- build zip: root has exactly agent_main.py, gc_agent/…, VERSION, requirements-agent.txt;
  no pyc/`__pycache__`, no json; deterministic (two builds same sha); prints sha.
- `safe_extract` rejects `/abs`, `C:x`, `C:\x`, `..\x`, `a/../../x`, backslash `..`;
  nothing is written when rejected.
- check: same sha → no-op; sha mismatch after download → refuse, nothing switched;
  bad version string → refuse; smoke failure (package whose agent_main raises) → refuse,
  directory removed, current.txt unchanged; success → versions/<v>/ with
  PACKAGE_SHA256, previous.txt = old, current.txt = new, returns "switched"
  (agent exits 3); keep-3 prunes oldest but never current/previous; sha in
  bad_packages.json is skipped; `smoke_python()` picks python.exe beside pythonw.exe.

### T9 launcher
- child exit 0 → launcher returns 0; exit 3 → restarted, re-reads current.txt;
  other → restarted with backoff (sleep injected);
- newly switched version crashing 3× within 30 s → current.txt = previous.txt,
  revert.json + bad_packages.json written, then runs previous;
- crashes of a non-new version never revert;
- single instance: second `acquire_single_instance(root)` fails while first held
  (POSIX fcntl path); released on close; `launcher.pyw` subprocess exits 0 at once
  when another holds the lock.

### T10 installer dry-run
- `install.pyw --dry-run --root R --yes` with install.json + agent-package.zip beside:
  files copied, versions/<v>/ + PACKAGE_SHA256, current.txt, agent.json has hub_url,
  token, python = sys.executable, watch_dir/mirror defaulted from
  `~/.gc_viewer_settings.json`; mirror with correct header adopted (sidecar);
  wrong header → exit non-zero with message, no sidecar; no autostart, no launch in
  dry-run (the plan printed); missing install.json → error; python < 3.9 check is a
  function tested with a fake version; missing pystray → message + non-zero;
  zip-slip package refused.

### T11 tray + settings (pure parts)
- `tray_colour(state)`: idle/sending green, hub-unreachable amber, auth-error /
  config-error red, paused grey; `status_lines(snapshot)` has version, queued,
  rejected, last sent, mirror seq; `apply_settings(cfg, form)` validates, blank token
  keeps the old one.

### T12 end to end
- subprocess `agent_main.py --root R --no-tray` (layout without versions/, updates off),
  fake hub with one results row; drop `S1.CDF` in the watch dir → fake hub receives the
  exact bytes with correct headers; mirror file gets header + the row; fake hub then
  sends `restart` → process exits 3.

### T13 compat + CI + full suite
- every agent source parses with `ast.parse(..., feature_version=(3, 9))`, no `match`;
  run tests/agent under a local 3.9 venv; `.github/workflows/agent-ci.yml`
  (ubuntu-24.04 3.9 + 3.14, windows-latest 3.14; contents: read;
  persist-credentials: false; timeout 20); `.venv/bin/python -m pytest tests/ -q`.
