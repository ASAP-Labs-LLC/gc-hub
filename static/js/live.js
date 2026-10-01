/* live.js: the live-update client (v4.0; spec "Live updates (v4.0)").

   window.GCLive:
     start(opts?)          idempotent; returns a Promise that resolves after the
                           first answer (or first failure), so a page can take
                           the cursor first and then load its data. Polls
                           GET /api/live every 3 s while the tab is visible,
                           30 s while hidden (backing off on errors: at most
                           12 s visible, 60 s hidden), and at once on
                           visibilitychange/focus. Never two requests at once.
     subscribe(fn)         -> unsubscribe. fn(update) after every poll that
                           changed something, and once with {reset: true}
                           after (re)start or a boot_id change (a subscriber
                           added later gets its own {reset: true} at once).
                           update = {reset, samples: [ids], instruments: [ids],
                                     kinds: [event kinds], agents: [{instrument_id,
                                     name, enabled, last_seen, version, host, status,
                                     live, last_seen_age_s}],
                                     notifications_unread, hub: {state, staged_update,
                                     processing_paused, queue, exports_pending,
                                     paused_by, paused_since},
                                     version, version_changed, tasks, boot}
                           (v4.0 lane E: `live` is the server's one liveness
                           rule; an agent's age alone changing is not an
                           update; `tasks` is the running-now feed; `boot` the
                           hub process's boot id, from the cursor.)
                           On reset, reload your data: samples/instruments are
                           then empty (the ring could not say what changed).
     agents()              the latest agent snapshot (a copy).
     tasks(), hub()        the latest running-now tasks and hub state (copies).
                           agents() is refreshed on every poll, update or not:
                           each agent carries read_at (ms, this browser's clock,
                           when the answer arrived), so agentAge(agent, now) =
                           the hub's last_seen_age_s + the time since, and
                           agentLive(agent, now) applies the hub's 90 s rule to
                           it (v4.0 lane E review: a timer re-renders from these).
     serverToday()         the hub's own date ("YYYY-MM-DD"; day headings).
     status()              {connected, last_ok_at (ms), error}; statusText()
                           turns it into "Live · updated 12 s ago" /
                           "Reconnecting… · last update 40 s ago".
     bgFetch(url, opts)    fetch for a GET the page makes on its own (every
                           follow-up of a live update): it carries
                           X-GC-Background: 1, so the hub doesn't count it as
                           activity (the 3 AM restart, the idle deploy) or
                           keep the session alive. Refuses anything but GET.
   Pure (node-tested in tests/js/live.test.js and live_poller.test.js):
   nextDelay, initialState, applyResponse, pollUrl, statusText, agoText,
   createPoller(deps) (the poller with injectable timers/fetch/visibility).

   An agent's `status` is its reported heartbeat state (idle, sending,
   paused, hub-unreachable, auth-error, config-error, unknown; null = never
   heard from); "last seen N s ago" ticks on the client with agoText. */
(function (root) {
    'use strict';

    const VISIBLE_MS = 3000;
    const HIDDEN_MS = 30000;
    const VISIBLE_MAX_MS = 12000;
    const HIDDEN_MAX_MS = 60000;
    const BG_HEADER = 'X-GC-Background';
    const LIVE_SECONDS = 90;        // live.LIVE_SECONDS on the hub
    // a poll the hub accepted but never answered (hung, half-open connection) is
    // given up after this, so the status says so and the next poll can go out
    const POLL_TIMEOUT_MS = 15000;
    // no answer for longer than this is never "Live" (three hidden-tab polls)
    const STALE_MS = 3 * HIDDEN_MS;

    /** Milliseconds to the next poll. */
    function nextDelay(visible, failures) {
        const base = visible ? VISIBLE_MS : HIDDEN_MS;
        const cap = visible ? VISIBLE_MAX_MS : HIDDEN_MAX_MS;
        const f = Number.isFinite(failures) && failures > 0 ? Math.min(Math.floor(failures), 10) : 0;
        return Math.min(base * Math.pow(2, f), cap);
    }

    function initialState() {
        return { cursor: null, agents: [], notifications_unread: null, hub: null, version: null,
                 tasks: [], server_today: null, seen: false };
    }

    function _same(a, b) {
        return JSON.stringify(a) === JSON.stringify(b);
    }

    function _arr(v) {
        return Array.isArray(v) ? v.slice() : [];
    }

    function _copyAll(list) {
        return (list || []).map(a => Object.assign({}, a));
    }

    /** The agents without their age, which ticks on every poll (v4.0 lane E). */
    function _agentsKey(agents) {
        return JSON.stringify((agents || []).map(a => {
            const o = Object.assign({}, a);
            delete o.last_seen_age_s;
            delete o.read_at;
            return o;
        }));
    }

    /** Seconds since the agent's last check-in: the hub's age when read plus
        the time since it was read (null when the hub gave no age). */
    function agentAge(agent, nowMs) {
        const age = agent && agent.last_seen_age_s;
        if (typeof age !== 'number' || !isFinite(age)) return null;
        const at = agent.read_at;
        const extra = (typeof at === 'number' && isFinite(at) && typeof nowMs === 'number')
            ? Math.max(0, (nowMs - at) / 1000) : 0;
        return Math.round(age + extra);
    }

    /** The hub said live, and it is still within the hub's 90 s rule. */
    function agentLive(agent, nowMs) {
        if (!agent || agent.live !== true) return false;
        const age = agentAge(agent, nowMs);
        return age === null || age <= LIVE_SECONDS;
    }

    function _boot(cursor) {
        const i = typeof cursor === 'string' ? cursor.indexOf(':') : -1;
        return i > 0 ? cursor.slice(0, i) : null;
    }

    /** Fold one /api/live answer into the state: {state, update}; update is
        null when nothing changed. Pure: *state* is not modified. */
    function applyResponse(state, response) {
        if (!response || typeof response !== 'object' || typeof response.cursor !== 'string') {
            return { state, update: null };
        }
        // a field the answer lacks keeps its last value
        const agents = (Array.isArray(response.agents) ? response.agents : state.agents || [])
            .map(a => Object.assign({}, a));
        const unread = Number.isFinite(response.notifications_unread)
            ? response.notifications_unread
            : (state.notifications_unread == null ? 0 : state.notifications_unread);
        const h = response.hub && typeof response.hub === 'object' ? response.hub
            : (state.hub || {});
        const hub = Object.assign({}, h, { state: h.state == null ? null : h.state,
                                           staged_update: h.staged_update == null ? null : h.staged_update });
        const tasks = Array.isArray(response.tasks) ? _copyAll(response.tasks)
            : _copyAll(state.tasks);
        const today = typeof response.server_today === 'string' ? response.server_today
            : (state.server_today || null);
        const dayChanged = !!(state.server_today && today && today !== state.server_today);
        const version = typeof response.version === 'string' ? response.version : state.version;
        const versionChanged = !!(state.version && version && version !== state.version);
        const reset = !!response.reset || !state.seen;
        const samples = reset ? [] : _arr(response.samples);
        const instruments = reset ? [] : _arr(response.instruments);
        const kinds = _arr(response.kinds);
        const changed = reset || samples.length > 0 || instruments.length > 0 || kinds.length > 0
            || versionChanged || _agentsKey(state.agents) !== _agentsKey(agents)
            || state.notifications_unread !== unread || !_same(state.hub, hub)
            || !_same(state.tasks || [], tasks) || dayChanged;
        const next = { cursor: response.cursor, agents, notifications_unread: unread, hub,
                       version, tasks, server_today: today, seen: true };
        const update = changed
            ? { reset, samples, instruments, kinds, agents: agents.map(a => Object.assign({}, a)),
                notifications_unread: unread, hub: Object.assign({}, hub), version,
                version_changed: versionChanged, tasks: _copyAll(tasks),
                boot: _boot(response.cursor), server_today: today, day_changed: dayChanged }
            : null;
        return { state: next, update };
    }

    function pollUrl(cursor) {
        return cursor ? '/api/live?since=' + encodeURIComponent(cursor) : '/api/live';
    }

    function _toMs(t) {
        if (t == null) return null;
        if (typeof t === 'number') return Number.isFinite(t) ? t : null;
        const ms = Date.parse(t);
        return Number.isFinite(ms) ? ms : null;
    }

    /** "just now", "42 s ago", "5 min ago", "3 h ago", "2 d ago"; "never". */
    function agoText(t, now) {
        const ms = _toMs(t);
        if (ms == null) return 'never';
        const s = Math.max(0, Math.round(((now == null ? Date.now() : now) - ms) / 1000));
        if (s < 5) return 'just now';
        if (s < 60) return s + ' s ago';
        if (s < 3600) return Math.floor(s / 60) + ' min ago';
        if (s < 86400) return Math.floor(s / 3600) + ' h ago';
        return Math.floor(s / 86400) + ' d ago';
    }

    /** The bottom-bar indicator's text. */
    function statusText(st, now) {
        if (!st || !st.last_ok_at) return 'Connecting…';
        const stale = (now == null ? Date.now() : now) - st.last_ok_at > STALE_MS;
        if (!st.connected || stale) return 'Reconnecting… · last update ' + agoText(st.last_ok_at, now);
        return 'Live · updated ' + agoText(st.last_ok_at, now);
    }

    /** A background GET (see the header comment). */
    function bgFetch(url, opts, fetchFn) {
        const o = Object.assign({}, opts || {});
        const method = String(o.method || 'GET').toUpperCase();
        if (method !== 'GET') return Promise.reject(new Error('bgFetch is for GET requests only'));
        o.method = 'GET';
        o.credentials = o.credentials || 'same-origin';
        o.headers = Object.assign({}, o.headers || {}, { [BG_HEADER]: '1' });
        const f = fetchFn || root.fetch;
        return Promise.resolve().then(() => f(url, o));
    }

    /** The poller, over injectable deps: {fetch, setTimeout, clearTimeout,
        now, isVisible, on(event, fn), off(event, fn)}. */
    function createPoller(deps) {
        const d = deps;
        let state = initialState();
        let subs = [];
        let started = false;
        let timer = null;
        let inFlight = false;
        let again = false;
        let failures = 0;
        let status = { connected: false, last_ok_at: 0, error: null };
        let startPromise = null;
        let resolveStart = null;

        function emit(update) {
            for (const fn of subs.slice()) {
                try { fn(update); } catch (e) {
                    if (root.console) console.error('GCLive subscriber', e);
                }
            }
        }

        function schedule(ms) {
            if (timer != null) d.clearTimeout(timer);
            timer = d.setTimeout(() => { timer = null; pollNow(); }, ms);
        }

        function pollNow() {
            if (!started) return;
            if (inFlight) { again = true; return; }
            inFlight = true;
            if (timer != null) { d.clearTimeout(timer); timer = null; }
            const ctl = typeof AbortController === 'function' ? new AbortController() : null;
            let gaveUp = null;
            const hung = new Promise((_, reject) => {
                gaveUp = d.setTimeout(() => {
                    gaveUp = null;
                    if (ctl) { try { ctl.abort(); } catch (_e) { /* already done */ } }
                    reject(new Error('no answer from the hub in ' + Math.round(POLL_TIMEOUT_MS / 1000) + ' s'));
                }, POLL_TIMEOUT_MS);
            });
            hung.catch(() => {});
            const opts = { cache: 'no-store', headers: { Accept: 'application/json' } };
            if (ctl) opts.signal = ctl.signal;
            Promise.race([bgFetch(pollUrl(state.cursor), opts, d.fetch), hung])
                .then(r => Promise.race([Promise.resolve(r).then(r => {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    // GCSession.readJson where the page has it (a Cloudflare page is a readable error)
                    if (typeof GCSession !== 'undefined' && GCSession.readJson) {
                        return GCSession.readJson(r).then(res => {
                            if (res.body && res.body.error && res.body.cursor == null) throw new Error(res.body.error);
                            return res.body;
                        });
                    }
                    return r.text().then(t => JSON.parse(t));
                }), hung]))
                .then(body => {
                    const out = applyResponse(state, body);
                    if (out.state === state) throw new Error('bad /api/live answer');
                    state = out.state;
                    // every agent read now: its age ticks from here (v4.0 lane E)
                    const readAt = d.now();
                    for (const a of state.agents) a.read_at = readAt;
                    if (out.update) for (const a of out.update.agents) a.read_at = readAt;
                    failures = 0;
                    status = { connected: true, last_ok_at: d.now(), error: null };
                    if (out.update) emit(out.update);
                })
                .catch(err => {
                    failures++;
                    status = { connected: false, last_ok_at: status.last_ok_at,
                               error: String((err && err.message) || err) };
                })
                .then(() => {
                    if (gaveUp != null) { d.clearTimeout(gaveUp); gaveUp = null; }
                    inFlight = false;
                    if (resolveStart) { const r = resolveStart; resolveStart = null; r(); }
                    if (!started) return;
                    if (again) { again = false; pollNow(); return; }
                    schedule(nextDelay(d.isVisible(), failures));
                });
        }

        function onWake() {
            if (started && d.isVisible()) pollNow();
        }

        function start() {
            if (startPromise) return startPromise;
            started = true;
            state = initialState();        // the first answer is a reset
            failures = 0;
            startPromise = new Promise(r => { resolveStart = r; });
            d.on('visibilitychange', onWake);
            d.on('focus', onWake);
            pollNow();
            return startPromise;
        }

        function stop() {
            started = false;
            startPromise = null;
            if (timer != null) { d.clearTimeout(timer); timer = null; }
            d.off('visibilitychange', onWake);
            d.off('focus', onWake);
        }

        function subscribe(fn) {
            if (typeof fn !== 'function') return function () {};
            subs.push(fn);
            if (state.seen) {
                // joined after the start's reset: give it its own
                const snap = { reset: true, samples: [], instruments: [], kinds: [],
                               agents: state.agents.map(a => Object.assign({}, a)),
                               notifications_unread: state.notifications_unread,
                               hub: Object.assign({}, state.hub), version: state.version,
                               version_changed: false, tasks: _copyAll(state.tasks),
                               boot: _boot(state.cursor), server_today: state.server_today,
                               day_changed: false };
                Promise.resolve().then(() => {        // after subscribe() has returned
                    if (subs.includes(fn)) { try { fn(snap); } catch (_) { /* its problem */ } }
                });
            }
            return function unsubscribe() { subs = subs.filter(x => x !== fn); };
        }

        return {
            start, stop, subscribe, pollNow,
            agents: () => state.agents.map(a => Object.assign({}, a)),
            tasks: () => _copyAll(state.tasks),
            hub: () => (state.hub ? Object.assign({}, state.hub) : null),
            serverToday: () => state.server_today,
            status: () => {
                // backstop: whatever stalled the poll, an old answer is not "connected"
                const st = Object.assign({}, status);
                const limit = 3 * nextDelay(d.isVisible(), 0) + POLL_TIMEOUT_MS;
                if (st.connected && st.last_ok_at && d.now() - st.last_ok_at > limit) {
                    st.connected = false;
                    st.error = st.error || 'no answer from the hub';
                }
                return st;
            },
        };
    }

    // ── the page's poller ──────────────────────────────────────────────────
    const hasDom = typeof document !== 'undefined';
    const browserDeps = {
        fetch: (url, opts) => root.fetch(url, opts),
        setTimeout: (fn, ms) => setTimeout(fn, ms),
        clearTimeout: (id) => clearTimeout(id),
        now: () => Date.now(),
        isVisible: () => !hasDom || document.visibilityState !== 'hidden',
        on: (ev, fn) => {
            if (ev === 'visibilitychange') { if (hasDom) document.addEventListener(ev, fn); }
            else if (typeof root.addEventListener === 'function') root.addEventListener(ev, fn);
        },
        off: (ev, fn) => {
            if (ev === 'visibilitychange') { if (hasDom) document.removeEventListener(ev, fn); }
            else if (typeof root.removeEventListener === 'function') root.removeEventListener(ev, fn);
        },
    };
    const page = createPoller(browserDeps);

    const api = {
        start: () => page.start(), stop: () => page.stop(), subscribe: fn => page.subscribe(fn),
        agents: () => page.agents(), tasks: () => page.tasks(), hub: () => page.hub(),
        serverToday: () => page.serverToday(), agentAge, agentLive, LIVE_SECONDS,
        status: () => page.status(), pollNow: () => page.pollNow(),
        bgFetch, nextDelay, initialState, applyResponse, pollUrl, statusText, agoText, createPoller,
        POLL_TIMEOUT_MS, STALE_MS,
    };
    root.GCLive = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
