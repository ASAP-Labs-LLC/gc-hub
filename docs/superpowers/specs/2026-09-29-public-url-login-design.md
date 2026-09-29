# gc.asaplabs.net and sign-in (v3.0.0)

Status: design, rev 1 (2026-09-29). Decided by Ryan: "The ping for ASAPv1 should be gc.asaplabs.net and route all traffic through that"; access control = "App login like COA".

## Findings that drive this

- `https://gc.asaplabs.net` already reaches the v2.0.0 hub on ASAPSV1 through a Cloudflare tunnel (same edge as `coa.asaplabs.net`). The hub has no login, so results, settings, notifications (with share paths), agents and instruments can be read by anyone with the URL. Operator actions (restart, reprocess, Export to LIMS, QBench upload) accept a request that has no `Origin` header, such as one sent with curl.
- In a browser, every POST through gc.asaplabs.net is refused. The cross-site guard compares `Origin: https://gc.asaplabs.net` (port 443) with `request.scheme` = `http` (cloudflared → `http://localhost:5560`), so the site is read-only in practice (verified: POST to a missing route with a same-origin `Origin` → 403).
- Behind the tunnel, `request.remote_addr` is cloudflared's address (loopback when cloudflared runs on ASAPSV1). The code currently treats loopback as the trusted local admin in several places:
  - `admin_auth` (exempt from the global throttle, and every internet client shares one throttle key)
  - `hub_control` (pause/stop; saved today only by the loopback-`Host` check)
  - `ingest_api` (hub-URL trust)
  - `comments` (`author_ip`)

## Decisions

### D1. Identity: a LabLink account, as in COA Reviewer
- Sign-in uses the same LabCore endpoint COA uses: `POST {labcore_url}/api/login {username, password}` → `{username}`. `labcore_url` defaults to `https://labvision.asaplabs.net` and is overridden by `LABCORE_URL`. A keycard scan is sent as both username and password, as `coa-reviewer/labcore_client.py` does.
- The session takes the canonical account name LabCore returns.
- New stdlib module `labcore_auth.py` (urllib, 10 s timeout, raises `LabCoreUnavailable` on network/5xx, returns None on any other non-200). No new dependency.
- **Break-glass sign-in.** "Sign in with the admin password" is always offered on the login page and is the only option when LabCore is unreachable. The session name is `Admin`. It goes through `admin_auth.verify` and its throttle.
- **Before admin setup**, `/admin/setup` stays reachable without a session; it is protected by the one-time setup code.

### D2. Sessions: server-side, in SQLite, surviving restarts and updates
- Store migration adds the table `web_sessions` with these columns:
  - `token_hash` TEXT PK: the sha256 of a 32-byte `secrets.token_urlsafe` cookie value
  - `name`
  - `method` (`password` | `card` | `admin`)
  - `created_at`
  - `last_seen`
  - `ip`
  - `user_agent` (≤200)
  - `revoked_at`
- Only the hash is stored.
- **Cookie:** `gc_session`, `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` when the request is https (D4).
- **Expiry:** idle 12 h, absolute 14 days. `last_seen` is written at most once a minute per session, and never for non-activity paths.
- **Sign out** revokes the session. The admin page lists active sessions (name, method, IP, last seen) with Revoke. Maintenance prunes sessions that expired more than 30 days ago.
- Additive migration only; an older release must still start on the newer DB.

### D3. What needs a session
- **A `before_request` gate** (registered after the cross-site guard) requires a valid session for every route except:
  - `/healthz`
  - `/login` and `/api/login*`
  - `/api/logout`
  - `/static/*` and `/favicon.ico`
  - `/admin/setup` and `POST /api/admin/setup`, only while no admin password is set
  - the agent paths `/api/ingest` and `/api/agent/*` (bearer token, unchanged)
  - the tray paths under `/api/hub/*`: loopback-only and admin password as today; `GET /api/hub/status` without a session only when local (D4)
- **Refusals:**
  - An unauthenticated page request gets a 302 to `/login?next=<path>`. `next` must be a relative path starting with a single `/`; anything else becomes `/`.
  - An unauthenticated `/api/` request gets 401 `{error: "Sign in required", login_required: true}`.
  - The front end handles 401s centrally: every `fetch` wrapper, the SSE stream and plain `fetch` calls send the user to `/login?next=<current page>`.
- **Admin actions** keep requiring the admin password on top of the session.
- **The diagnostics download link** (`GET …/download/<token>`) now also needs a session.
- The LAN address (`http://ASAPSV1:5560`) needs sign-in too: one rule everywhere.

### D4. The tunnel: client address, scheme, and "local"
- A new module `netctx.py` is the only place these three things are decided:
  - `client_ip()`:
    - If `remote_addr` is a trusted proxy and `CF-Connecting-IP` is a valid IP, the result is that IP.
    - Otherwise it is `remote_addr`.
    - Trusted proxies are loopback by default. `settings.trusted_proxies` (a list of IPs) adds a cloudflared on another machine.
  - `is_https()`: true when `request.is_secure`, or when `X-Forwarded-Proto: https` comes from a trusted proxy.
  - `is_local()`: `remote_addr` is loopback AND none of `CF-Connecting-IP`, `CF-Ray`, `X-Forwarded-For`, `Forwarded` or `X-Forwarded-Proto` is present AND the `Host` is a loopback name. Anything that came through Cloudflare is never local.
- **Every current use of `request.remote_addr` for a decision or for a record** goes through these helpers:
  - `admin_auth`: the throttle keys, the loopback exemption and the setup Host check
  - `hub_control`
  - `ingest_api`: hub-URL trust
  - `comments_api`: `author_ip`
  - log lines
  - the new sessions
- **The cross-site guard** builds "my origin" as scheme `https` if `is_https()`, else `request.scheme`, plus `request.host`. It also accepts an `Origin` equal to `public_url`. A test proves a browser POST through the tunnel (`Host: gc.asaplabs.net`, `X-Forwarded-Proto: https`, `CF-Connecting-IP`, `Origin: https://gc.asaplabs.net`) passes, and that `Origin: https://evil.example` is still refused.
- **`/healthz` through the tunnel** drops the `hub` field; local requests keep it. The updater's contract keys are unchanged.

### D5. gc.asaplabs.net is the hub's address
- **Setting:** `public_url`, default `https://gc.asaplabs.net`, admin-editable on `/admin/hub`, validated as a bare `https?://host[:port]`.
- **Agent installers:** `hub_url` = the admin-set hub URL if one was set, else `public_url`. The `needs_hub_url` 409 goes away. So every agent heartbeat (the "ping"), ingest, results poll and self-update goes to `https://gc.asaplabs.net`.
- **Agent over the internet:**
  - It sends `User-Agent: gc-agent/<version>`, and verifies TLS with the system store (default `ssl` context).
  - Its timeouts suit an internet hop (connect 10 s, ingest read 120 s).
  - When a response carries `cf-mitigated` or is a Cloudflare challenge page (HTML with `cf-ray`), it logs and shows in the tray: "Cloudflare blocked the agent: add the WAF skip rule in DEPLOY.md".
  - The 25 MB ingest cap is under Cloudflare's 100 MB request limit.
- **Hub tray:** "Open in browser" opens `public_url`, read from `GET /api/hub/status`, which now carries it, falling back to the tray config and then `http://localhost:<port>`. The tray's control and status calls stay on `localhost`; the pause/stop controls must never be reachable through the tunnel.
- **Updater:** it keeps health-checking `http://localhost:<port>/healthz`; nothing changes there.

### D6. Attribution
- **The signed-in name replaces typed initials.** The store migration adds `sample_comments.author_name`, `deleted_by_name` and `report_log.user_name`.
- **Comments:** the server sets `author_name` from the session and derives `author_initials` from it: the first letter of each word, upper-cased, ≤4 letters, `A–Z` only, and `X` if nothing is left. A client-sent `author_initials` is ignored. The UI drops the initials box and shows "Commenting as <name>". Reports keep printing initials; `for_report` also returns `author_name`, never an IP.
- **Operator actions are logged in `app.log` with the name:** reprocess, Export to LIMS, restart, QBench upload, Settings save, admin actions, and diagnostics builds. Admin notifications that say "by …" use the name.
- **Every page** shows the signed-in name and a Sign out control next to the version badge (bottom-right). The page stays as it is; the redesign is phase 5.

### D7. Diagnostics and secrets
- The bundle's DB copy keeps `web_sessions` rows (name, method, IP, times) with `token_hash` nulled.
- The session cookie value is never logged. `known_secrets` adds nothing new: the hub stores no LabCore password, and the login password is never stored or logged. Redact it from the request like the admin password.
- The login route logs only the name and method; a failed login logs the IP and the username attempted, never the password.

### D8. Login throttling
- **Per client IP (`client_ip()`):** 10 failures in 10 minutes means 60 s refusals.
- **Per username:** 10 failures in 10 minutes means 60 s refusals.
- **Hub-wide:** 100 in 10 minutes.
- In memory only; a restart resets it.
- **Card codes:** throttled as passwords, and never logged.
- **LabCore unreachable** is not a failure; the page shows "LabVision can't be reached. Try again, or sign in with the admin password."

### D9. Deployment notes (DEPLOY.md)
- The cloudflared ingress for `gc.asaplabs.net` points at `http://localhost:5560`. If cloudflared runs on another machine, set `trusted_proxies`.
- **If agents get a Cloudflare challenge:** add a WAF custom rule "Skip" for the host `gc.asaplabs.net` and URI paths starting with `/api/ingest` or `/api/agent/`. Those paths are bearer-token protected.
- Existing browser sessions: none; everyone signs in once after v3.0.0.

### D10. LEM machine: a dropdown, not a typed uid
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
- Per-user roles. Everyone signed in may do operator actions, and the admin password gates admin actions.
- Cloudflare Access.
