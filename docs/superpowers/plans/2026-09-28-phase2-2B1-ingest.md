# Phase 2, plan 2B1: ingest API, agent tokens, admin password

Spec: `docs/superpowers/specs/2026-09-28-phase2-multi-instrument-hub-design.md`
(Security, Ingest API (2B1), D11, D13, Status machine). Contract:
`docs/superpowers/specs/2026-09-28-phase2-contracts.md` §1 (the agent in
`agent/` is already built against it). Carry-overs: plan 2A1,
"Integration-critic carry-overs", 2B1 items.

Branch `b1/ingest` off `feat/phase2`. Runs in parallel with 2A1 T4 (routes
and UI in `app.py` / `static/js/app.js`) and T6 (folder loader), so:

- all new code is in **new modules**: `admin_auth.py` (password, setup page,
  admin gate) and `ingest_api.py` (a Flask Blueprint: agent API, tokens,
  installer download, agent commands);
- `app.py` changes by a few lines only: register the blueprints, make
  `_check_admin` call `admin_auth`, add the agent paths to the activity
  exclusion;
- `store.py` gains one column, `agents.package_sha256`, in schema v1
  (unreleased, so allowed) and in `REQUIRED_COLUMNS`;
- no edits to `static/js/app.js`; one new template, `templates/admin_setup.html`.

Strict TDD: every task starts with a failing test (red), then the code
(green). Tests never `import app`; routes are tested in a booted subprocess
(`tests/bootapp.py`), modules in-process against a temp store.

## Decisions (where the spec leaves a choice)

- **Password hash.** `pbkdf2_hmac("sha256", pw, salt, 310_000)`, 16-byte
  random salt per install, stored in `settings_kv["admin_password"]` as
  `pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>` (the iteration count is
  read back, so it can be raised later). Minimum length 8.
- **Before a password is set** every admin-gated action is refused (403) with
  a message pointing to `/admin/setup`. The hardcoded `"admin"` is gone.
  `app._check_admin(body)` keeps its signature and returns a bool; the
  refusal text for "not set up" / "too many attempts" replaces the route's own
  403 message through an `after_app_request` hook (so every current and
  future admin route gets it without editing each route).
- **Setup.** `GET /admin/setup` (page, only useful while unset) and
  `POST /api/admin/setup` `{password}` (JSON only; the cross-site guard
  applies because it is under `/api/`; also refuses a request whose
  `Sec-Fetch-Site`/`Origin` is present and not same-origin, even outside
  `/api/`). Once a password exists it answers 409. Changing it:
  `POST /api/admin/password` `{password, new_password}`.
- **Rate limit.** In memory, per client address: after 5 consecutive
  failures, each further attempt is refused for `2**(n-5)` s (capped at
  300 s) without checking the password; a success resets. No store writes.
- **Tokens.** `secrets.token_urlsafe(32)`; `instruments.token_hash` =
  sha256 hex of the token, `token_issued_at` = `store.now_iso()`. Lookup by
  hash, then `hmac.compare_digest`. One token per instrument: minting a new
  one revokes the previous one.
- **Installer.** `POST /api/admin/instruments/<id>/installer`
  `{password, confirm_revoke}` → `application/zip` with `install.pyw`,
  `launcher.pyw`, `install.json` `{hub_url, token}`, `agent-package.zip`,
  `agent-package.json` `{version, sha256}` at the zip root. If the
  instrument already has a token and `confirm_revoke` is not `true`: 409
  `{error, needs_confirm: true}` and nothing changes. `hub_url` is
  `settings_kv["hub_url"]` if set, else `request.host_url`
  (`POST /api/admin/hub-url` `{password, hub_url}` sets or clears it).
- **Agent commands.** `POST /api/admin/instruments/<id>/agent-command`
  `{password, command}` stores `agents.pending_command`; the next heartbeat
  delivers it once and clears it (one write transaction).
  `GET /api/agents` lists agent rows with `clock_skew_seconds`
  (`agent_time` minus `last_seen` converted to hub local time, M4).
- **Ingest.** Order: token (401) → `Content-Length` > 25 MB (413, body
  unread) → headers (`X-GC-SHA256` 64 hex, `X-GC-Mtime`
  `YYYY-MM-DDTHH:MM:SS`; 400) → body read with a 25 MB cap (413) → sha
  (400) → magic bytes (NetCDF classic `CDF\x01|\x02|\x05` or HDF5; else
  415) → `pipeline.submit`. `InstrumentDisabled` → 403 (caught before
  `SubmitRejected` → 400); `UnknownInstrument` → 401. Unexpected server
  errors (store, disk) stay 500, logged: the agent backs off and retries,
  which is right for a hub fault.
- **Heartbeat for a disabled instrument** is accepted (the Instruments
  page still sees the agent); only ingest refuses with 403.
- **Package.** Built from `agent/` with `agent/build_package.py`
  (`build(agent_dir, version, out)`), version = hub `VERSION`, cached in
  memory per version (the build is deterministic).
- **Store.** The hub start-up (T4/T5, `instruments.startup`) migrates the
  store. Until that is wired, and for the health check's empty data folder,
  both modules call `store.migrate()` once, lazily, before first use
  (idempotent).

## Tasks

### T1: store column
- Red: `tests/ingest/test_store_agents_column.py`: a fresh v1 database has
  `agents.package_sha256`; `REQUIRED_COLUMNS["agents"]` includes it.
- Green: add the column in MIGRATIONS[0] and REQUIRED_COLUMNS.

### T2: `admin_auth` core
- Red: `tests/ingest/test_admin_auth.py`: `is_set()` false on a new store;
  `set_password` stores `pbkdf2_sha256$...` with ≥ 200k iterations and a
  random salt (two stores → different salts; the password never appears);
  `check("right")` true, `check("wrong")`/non-str false; `set_password`
  refuses < 8 chars; `setup()` refuses when a password exists;
  `change(old, new)`; backoff: 5 failures then refusal until the delay
  passes (injected clock), success resets; the check uses
  `hmac.compare_digest`.
- Green: `admin_auth.py`.

### T3: tokens
- Red: `tests/ingest/test_tokens.py`: `mint(inst)` returns a token, stores
  only its sha256 and `token_issued_at`; `verify(token)` → instrument row;
  wrong / empty / revoked (after a second mint) → None; the token never
  appears in the store.
- Green: in `ingest_api.py`.

### T4: ingest handler mapping (booted)
- Red: `tests/ingest/test_ingest_boot.py` with a store prepared before boot
  (gc1 + gc2, tokens minted in-process): 201 created; 200 duplicate; 409
  cross-instrument; 202 conflict (sha = received body's); 401 no/bad token
  (body unread); 403 disabled instrument; 413 oversize `Content-Length`
  (body unread, via `http.client`); 400 sha mismatch; 400 bad
  `X-GC-SHA256`/`X-GC-Mtime`; 415 non-CDF bytes; 400 truncated CDF;
  percent-encoded filename arrives unquoted as `source_name`; a request with
  a foreign `Origin` is still refused by the phase 1 guard, one without
  `Origin` passes; ingest does not count as activity.
- Green: Blueprint `ingest_api.bp`, registered in `app.py`.

### T5: heartbeat, results, package
- Red (booted): heartbeat upserts the agents row (incl. `package_sha256`),
  returns `agent_package_sha256` equal to `/api/agent/package`'s sha and to
  the sha of `package.zip`'s bytes, `server_time` local
  `YYYY-MM-DDTHH:MM:SS`; a pending command is delivered once; bad JSON 400;
  results: header = `distill.CSV_HEADER`, rows ascending for the token's
  instrument only, `more` via `limit+1`, `after`/`limit` validation;
  package zip contains `agent_main.py`, `gc_agent/`, `VERSION` = hub
  version, no token; `/api/agents` shows the skew.
- Green: the routes.

### T6: admin gate, setup page, installer
- Red (booted): before setup an admin route (`/api/save-analysis-defaults`)
  answers 403 naming `/admin/setup`; `GET /admin/setup` 200 while unset;
  `POST /api/admin/setup` sets it (then 409); cross-site setup refused; the
  admin route now works with the new password and refuses `"admin"`;
  repeated failures are throttled; installer download without password 403,
  first mint 200 zip with the contract layout, second without confirm 409,
  with confirm 200 and the old token now 401. Existing booted tests that
  used `"admin"` set a password first (`bootapp.setup_admin`).
- Green: `admin_auth` blueprint + template, `_check_admin` rewired,
  installer route.

### T7: end to end
- `tests/ingest/test_ingest_e2e.py`: a store with gc1 (calibration,
  corrections, `live_since` in the past), the hub booted, password set and
  token minted through the admin API (installer zip), the **real** agent
  (`agent/agent_main.py --no-tray`) watching a folder; a synthetic CDF
  dropped there is received, processed by a `pipeline.Worker` on the same
  store (the hub's own worker is wired by T4/T5; both can run safely), becomes
  `final`, and its row is served by `GET /api/agent/results` and mirrored by
  the agent.

### T8: docs and full suite
- CLAUDE.md: admin password, agent API, activity exclusion.
- Run the whole suite.

## Deferred to 2A2 (the Instruments page) — review I6

2B1 ships the APIs; the screens that use them move to 2A2, whose
Instruments page owns every per-instrument control:
- editing `live_since` (the D11 backfill boundary);
- the **backfill release** screen (`pipeline.release_backfill` already exists);
- the **conflicts** screen, Replace / Keep existing
  (`pipeline.resolve_conflict_replace` and `store.conflicts.resolve` already exist);
- "Download installer" (with the revoke confirmation, showing `hub_url` from
  the 409 body), "Revoke token", the hub URL setting, agent commands, and the
  agent status table from `GET /api/agents`.

**M5 (must):** the Instruments page renders agent-supplied strings
(`host`, `state`, `last_file`, `last_error`, `version`) and instrument names
with `textContent` / escaped templates, never `innerHTML`. They come from
the GC PCs and are not trusted.

## Security review 1 (2026-09-28): fixes, each a test first

- **C1** First-use setup needs a one-time code (`admin-setup-code.txt` in the
  data folder, 0600, logged at WARNING; deleted on success; regenerated after
  a reset). The setup route also refuses a `Host` that isn't an IP literal,
  `localhost`, the machine's own names or the configured `hub_url` host (DNS
  rebinding).
- **I1** Each password/code attempt is reserved under a lock before hashing:
  one in flight per client, counted as a failure up front and refunded on
  success; a hub-wide budget of 30 failures per 10 minutes; at most two
  PBKDF2 computations at once.
- **I2** Heartbeat integers and `after`/`limit` are bounded (0 ≤ v < 2⁶³ for
  counts); deeply nested JSON is a 400.
- **I3/I4** `request.max_content_length` = 64 KiB for the heartbeat and every
  admin/setup JSON body (`app._admin_json_body` too), so a chunked or
  unauthenticated body can't be large. 413s under `/api/` are JSON.
- **I5** The installer needs a trustworthy hub URL: the configured one, or
  the request's host only when it is a non-loopback IP or one of the
  machine's names; otherwise 409 `needs_hub_url`. The 409 `needs_confirm`
  body carries `hub_url`. `hub_url` must be `http(s)://host[:port]`.
- **M1** `POST /api/admin/instruments/<id>/revoke-token`. A disabled
  instrument's token gets 403 on results and package; its heartbeat is
  accepted but gets `command: null` (the command stays queued).
- **M2/M3** `mint_token(require_no_token=)` checks inside the write; the zip
  is built with a pre-generated token before its hash is stored.
- **M4** 400 messages carry file base names, never server paths.
- **M6/M7** DEPLOY.md: setup, reset procedure, legacy mode has no admin.
- **M8** `change()` is a compare-and-set in one transaction.
- **M10** The 403 hook overrides only `error` (and adds `setup_url`).
