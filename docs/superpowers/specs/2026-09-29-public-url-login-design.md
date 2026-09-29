# gc.asaplabs.net and sign-in (v3.1.0)

Status: design, **rev 2** (2026-09-29). Decided by Ryan: "The ping for ASAPv1 should be gc.asaplabs.net and route all traffic through that"; access control = "App login like COA". Rev 2 folds in the spec critic's 22 amendments (with the coordinator's adjustment to setup, item 12 below) and the release split: the minimal tunnel fixes ship in v3.0.0 (`tunnel/fix`), sign-in ships as **v3.1.0**.

## Findings that drive this

- `https://gc.asaplabs.net` already reaches the v2.0.0 hub on ASAPSV1 through a Cloudflare tunnel (same edge as `coa.asaplabs.net`). The hub has no login, so results, settings, notifications (with share paths), agents and instruments can be read by anyone with the URL. Operator actions (restart, reprocess, Export to LIMS, QBench upload) accept a request that has no `Origin` header, such as one sent with curl.
- In a browser, every POST through gc.asaplabs.net is refused. The cross-site guard compares `Origin: https://gc.asaplabs.net` (port 443) with `request.scheme` = `http` (cloudflared → `http://localhost:5560`), so the site is read-only in practice (verified: POST to a missing route with a same-origin `Origin` → 403; PC 2 confirmed every admin action 403s).
- Behind the tunnel, `request.remote_addr` is cloudflared's address (loopback when cloudflared runs on ASAPSV1). The code treated loopback as the trusted local admin in several places: `admin_auth` (throttle exemption, one throttle key for the internet), `hub_control` (pause/stop; saved only by the loopback-`Host` check), `ingest_api` (hub-URL trust), `comments` (`author_ip`).
- Verified live: Cloudflare answers `403 "error code: 1010"` to any request with `User-Agent: Python-urllib/*` (gc, lem and labvision alike), so every server-side and agent HTTP call sends its own User-Agent. `http://gc.asaplabs.net` is served with no redirect.

## Decisions

### D1. Identity: a LabLink account, as in COA Reviewer
- Sign-in uses the endpoint COA uses: `POST {labcore_url}/api/login {username, password}` → `{username}`. `labcore_url` defaults to `https://labvision.asaplabs.net`, overridden by `LABCORE_URL`. A keycard is its own route, `POST /api/login/card {code}`, and the code is sent as both username and password (as `coa-reviewer/labcore_client.py`). The login page focuses the card field.
- The session takes the canonical account name LabCore returns.
- `labcore_auth.py` (stdlib urllib, 10 s timeout, `User-Agent: gc-hub/<version>`, `Accept: application/json`). **Only a JSON 4xx (not 429) is a failed sign-in** (None). `LabCoreUnavailable` on a network/TLS error or timeout, a 5xx, a 429, a `cf-mitigated` header, a 403 with a non-JSON body (e.g. `error code: 1010`), or a 200 that isn't JSON or has no usable `username`. No new dependency.
- **Break-glass.** `POST /api/login/admin {password}` via `admin_auth.check(password, client)` and its throttle, **only when the request did not come through Cloudflare** (LAN or local); through the tunnel only LabLink is offered. Session name `Admin (break-glass)`, method `admin`. Changing the admin password revokes every `admin` session.
- **Admin setup** (adjusted item 12): not through Cloudflare and no password yet → `/admin/setup` is open without a session (as before). Through Cloudflare → it needs a valid LabLink session **and** the setup code; its `Host` check accepts the hub URL's host only then (https and a session). Once a password is set, setup is closed everywhere.

### D2. Sessions: server-side, in SQLite, surviving restarts and updates
- One additive migration step, `user_version` 2 → 3 (the v2 step is not edited): table `web_sessions(id INTEGER PRIMARY KEY, token_hash TEXT UNIQUE (nullable, so the diagnostics copy can null it), name, method (password|card|admin), created_at, last_seen, expires_at, ip, user_agent ≤200, revoked_at)`, plus the nullable columns `sample_comments.author_name`, `deleted_by_name` and `report_log.user_name`. Older code still starts on it.
- The cookie value is `secrets.token_urlsafe(32)`; only its sha256 is stored. A **new token on every sign-in**, and the session of any cookie the browser presented is revoked.
- **Cookie:** `__Host-gc_session` on https (Secure, `Path=/`, no Domain), `gc_session` on the LAN over http; `HttpOnly`, `SameSite=Lax`.
- **Expiry:** idle 12 h; absolute 14 days, **7 days for a session created through Cloudflare**. Validated sessions are cached in memory for 30 s; revoking clears the cache. `last_seen` is written by a background refresher (at most once a minute per session), never on the request path and never for non-activity paths.
- A store error while checking a session is **503, not 401**. Before the store exists, `/login` still renders and sign-in answers 503.
- **Sign out** is `POST /api/logout` only. The admin page lists active sessions (name, method, IP, last seen) with Revoke and **Revoke all for this name**. Maintenance prunes sessions that ended more than 30 days ago.

### D3. What needs a session
- The gate (a `before_request` after the cross-site guard) classifies every route (`web_auth.route_class`):
  - **open**: `/healthz`; `/login`; `/api/login`, `/api/login/card`, `/api/login/admin` (listed explicitly, no prefix globs); `/api/logout`; `/static/*`, `/favicon.ico`; the agent paths **exactly** `/api/ingest`, `/api/agent/heartbeat`, `/api/agent/results`, `/api/agent/package`, `/api/agent/package.zip` (`/api/agents` stays gated).
  - **local** (the hub tray; item 1): `GET /api/hub/status`, `POST /api/admin/hub/pause-processing|resume-processing|stop` and `POST /api/restart` pass without a session when `is_local()`; every other check of theirs stays. Not local: `/api/admin/hub/*` is 403 regardless of session; `/api/restart` and the status need a session.
  - **setup**: `/admin/setup`, `POST /api/admin/setup`, per D1.
  - everything else: **session**.
- **Refusals:** a page gets 302 `/login?next=<path>`; `next` must match `^/(?![/\\])[^\\\x00-\x1f]*$` and not start with `/api/` or `/login`, else `/` (the login page's JS re-validates it). An `/api/` request gets 401 `{error: "Sign in required", login_required: true}` with `X-GC-Login-Required: 1`.
- `GET /api/session` → `{name, method}` or 401. The front end handles 401 centrally: `static/js/session.js`, loaded first on every page, wraps `fetch` (so app.js `api()` and its other calls, comments.js, instruments.js, hub_admin.js, the inline scripts of calibration.html and admin_setup.html are all covered) and sends the page to `/login?next=<page>` on the gate's 401; the SSE `onerror` asks `/api/session`.
- Admin actions keep requiring the admin password on top of the session. The diagnostics routes, including the one-time `GET …/download/<token>`, need a session. The LAN address needs sign-in too: one rule everywhere.
- An AST test classifies every registered route; the route-fate table has an **Auth** column pinned to `route_class`.

### D4. The tunnel: client address, scheme, and "local"
- `netctx.py` is the only place these are decided:
  - `client_ip()`: `CF-Connecting-IP` (a valid IP) when `remote_addr` is a trusted proxy, else `remote_addr`. Trusted proxies: loopback, plus `settings.json` `trusted_proxies` (IPs/CIDRs) for a cloudflared elsewhere. A spoofed `CF-Connecting-IP` from anyone else is ignored.
  - `is_https()`: `request.is_secure`, or `X-Forwarded-Proto: https` from a trusted proxy.
  - `is_proxied()` ("through Cloudflare"): a trusted-proxy peer that sent any of `CF-Connecting-IP`, `CF-Ray`, `X-Forwarded-For`, `Forwarded`, `X-Forwarded-Proto`.
  - `is_local()`: loopback peer AND none of those headers AND a loopback `Host`. Anything through Cloudflare is never local.
- Routed through it (item 5): `admin_auth` (throttle keys, the local exemption, `_cross_site`, the setup Host check and its log, setup/change clients), `hub_control._guard`, `ingest_api` (bad-token log, the installer hub URL — never derived from the request — and its log), `instruments_api._by`, `hub_admin._who`, `comments_api` (`author_ip`), `app` (`_who`, the guard and its log, activity, restart, the QBench credentials log), and the sessions.
- **https only through Cloudflare** (item 4): a request through Cloudflare that isn't `is_https()` gets a 308 to https, before any gate, for every method. `/api/login*` refuses non-https unless the request came directly from the LAN. https responses carry HSTS `max-age=31536000`.
- **The cross-site guard** builds "my origin" from `https` when `is_https()`, else `request.scheme`, plus `request.host`; `admin_auth` uses the same function. There is no "Origin equals the hub URL on another Host" exception (dropped).
- **`/healthz` through Cloudflare** returns only `{status, version, pid}`; local requests are unchanged (the updater's contract).
- **Activity** (item 6): recorded only for requests with a valid session on non-excluded paths; `/healthz` `active_sessions` = distinct sessions seen in 5 minutes. Unauthenticated scanners never block the idle deploy or the 3 AM restart.

### D5. gc.asaplabs.net is the hub's address (one setting, item 22)
- The existing `settings_kv["hub_url"]` is the hub address; unset, the effective value is `https://gc.asaplabs.net`. It must be https unless its host is a LAN name or IP. Editable on `/admin/hub` (and the Instruments page).
- **Installers:** `hub_url` = the effective hub URL; `needs_hub_url` is gone. `install.json` also carries `lan_url` (`http://<this machine>:<port>`, or null when the hub URL already is a LAN address).
- **Agent** (items 3, 16): `User-Agent: gc-agent/<version>` (the installer too); TLS verified with the system store; connect 10 s, reads 30 s, ingest 95 s; a 524 is retried (ingest is deduplicated by sha). A response is **blocked by Cloudflare** when it has `cf-mitigated`, or is a 403/503 with `cf-ray` whose body is HTML or matches `^error code: \d+`; the agent logs and shows "Cloudflare blocked the agent: add the WAF skip rule in DEPLOY.md" and never rejects the file over it. `lan_url` is used only after a network/TLS error against `hub_url`, never after an HTTP status. The 25 MB ingest cap is under Cloudflare's 100 MB.
- **Hub tray:** "Open in browser" opens the effective `hub_url` from `GET /api/hub/status`, falling back to the tray config's `hub_url`, then `http://localhost:<port>`. Its control and status calls stay on `127.0.0.1`.
- **Updater:** unchanged (`http://localhost:<port>/healthz`).

### D6. Attribution (items 7, 8)
- **Every stored `by` and every `_who()`/`_by()`** is `'<session name> (<client_ip()>)'` (`web_auth.actor()`): revisions, releases, conflict decisions, admin jobs, instruments, presets, the diagnostics manifest and log.
- **Comments:** POST and delete ignore any `initials` in the body. `author_initials`/`deleted_by_initials` are derived from the session name (split on non-letters, first letter of each word, A–Z, at most 4, else `X`); `author_name`/`deleted_by_name` are set. `comments.js` drops the initials box and shows "Commenting as <name>" from `GET /api/session`. Reports keep printing initials; `for_report` also returns `author_name`, never an IP.
- **report_log** gets `user_name` and initials derived from it; the QBench queue captures the name at enqueue next to the address (the upload thread has no request).
- **Operator actions are logged with the name**: reprocess, Export to LIMS, restart, QBench upload, Settings and analysis defaults, calibration, admin actions, diagnostics builds and downloads.
- **Every page** shows "Signed in as <name> · Sign out" next to the version badge. The redesign is phase 5.

### D7. Diagnostics and secrets (item 17)
- `diag/bundle` merged first: its DB copy nulls every `token_hash` column (so `web_sessions` keeps name, method, IP and times with `token_hash` null; tested with the v3 table), and `ENV_NAMES` has `LABCORE_URL`/`LEM_URL`.
- The session cookie value is never logged; the hub stores no LabCore password; the login password is never stored or logged.

### D8. Login throttling (item 9)
- Per `(client_ip, username)`: 5 failures in 10 minutes → refused for 60 s.
- Per `client_ip`: 30 failures in 10 minutes → 60 s.
- Hub-wide: 100 in 10 minutes, counted and applied **only through Cloudflare**; the LAN and the console are never refused by it.
- Maps capped at 10 000 keys; IPv6 keyed by /64; card attempts keyed `(client_ip, "card")`. In memory; a restart resets it.
- A failed sign-in logs the IP, the method and `sha256(username)[:8]`, never the raw username, a card code or a password.
- LabCore unreachable is not a failure; the page shows "LabVision can't be reached. Try again, or sign in with the admin password." (the second half only where the break-glass exists). Do not probe LabCore's own lockout; it is shared with COA (DEPLOY note).

### D9. Deployment notes (DEPLOY.md)
- The cloudflared ingress for `gc.asaplabs.net` points at `http://localhost:5560`; cloudflared elsewhere → `trusted_proxies`.
- Cloudflare: **Always Use HTTPS**; a WAF custom rule **Skip** for host `gc.asaplabs.net` and paths starting `/api/ingest` or `/api/agent/`, also skipping Browser Integrity Check, Security Level and Super Bot Fight Mode (the paths stay bearer-token protected).
- **Rollback warning:** a release before v3.1.0 has no login; pause the tunnel hostname before rolling back.
- Existing browser sessions: none; everyone signs in once after v3.1.0.

### D10. LEM machine: a dropdown, not a typed uid
Another lane (`lem/dropdown`). Rev 2: its route sits behind the session gate (class `session`) and its server-side call to LEM sends its own User-Agent (Cloudflare refuses `Python-urllib`). Otherwise unchanged from rev 1:

Ryan asked for this: "trying for the user to remember the LEM UID is insane, please just pull a list from LEM and have it be a drop down".

- **Where the list comes from.** LEM already serves its machines without a session at `GET https://lem.asaplabs.net/api/machines`. It answers `{machines: [{machine_uid, title, status, closed_reason, …}], labcore_online, stale}`. It is read-only and has 17 machines today, including `bf8e64b59f12` "Agilent GC 1" and `3afa991a66e9` "Agilent GC 2".
- **Settings.** A new setting `lem_url` (default `https://lem.asaplabs.net`, override `LEM_URL`), validated like `public_url`.
- **The hub route.** The browser never calls LEM directly (no CORS). A new hub route, `GET /api/lem/machines` (signed in), fetches the list server-side:
  - timeout 8 s;
  - the answer is cached for 60 s, and the last good answer is served stale when LEM fails;
  - it returns `{machines: [{uid, title, status, closed}], source: "live"|"cached"|"unavailable", age_seconds, error?}`, sorted by title, with only those fields.
  - Titles and uids are untrusted, so they are length-capped and inserted with textContent.
- **The Instruments page** replaces the `LEM machine uid` text box, in both the edit and create forms, with a `<select>`:
  - "— none —";
  - one entry per machine, "Agilent GC 1 (bf8e64b59f12)", with closed machines marked;
  - "Other…", which reveals the old text box.

  The current value is pre-selected. A saved uid that is not in the list shows as "Unknown machine (uid)" so it is never silently dropped. When LEM is unreachable, the select shows the saved value plus "Other…" and a line "LEM can't be reached; showing the saved value."
- **Saving.** The saved value is still the uid string in `instruments.lem_machine_uid`, so the storage and API are unchanged. The server check stays as it is: a string of at most 128 characters. The hub does not refuse a uid LEM doesn't know, because LEM can be down.
- **Ownership.** LEM remains read-only from the hub. Never write to LEM.

## Out of scope
- COA-style UI (phase 5).
- Per-user roles. Everyone signed in may do operator actions; the admin password gates admin actions.
- Cloudflare Access.
