/* live.js: the live-update client (v3.1; spec "Live updates (v3.1)").

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
                                     last_seen, version, host, status}],
                                     notifications_unread, hub: {state, staged_update},
                                     version, version_changed}
                           On reset, reload your data: samples/instruments are
                           then empty (the ring could not say what changed).
     agents()              the latest agent snapshot (a copy).
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

    /** Milliseconds to the next poll. */
    function nextDelay(visible, failures) {
        const base = visible ? VISIBLE_MS : HIDDEN_MS;
        const cap = visible ? VISIBLE_MAX_MS : HIDDEN_MAX_MS;
        const f = Number.isFinite(failures) && failures > 0 ? Math.min(Math.floor(failures), 10) : 0;
        return Math.min(base * Math.pow(2, f), cap);
    }

    function initialState() {
        return { cursor: null, agents: [], notifications_unread: null, hub: null, version: null,
                 seen: false };
    }

    function _same(a, b) {
        return JSON.stringify(a) === JSON.stringify(b);
    }

    function _arr(v) {
        return Array.isArray(v) ? v.slice() : [];
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
        const hub = { state: h.state == null ? null : h.state,
                      staged_update: h.staged_update == null ? null : h.staged_update };
        const version = typeof response.version === 'string' ? response.version : state.version;
        const versionChanged = !!(state.version && version && version !== state.version);
        const reset = !!response.reset || !state.seen;
        const samples = reset ? [] : _arr(response.samples);
        const instruments = reset ? [] : _arr(response.instruments);
        const kinds = _arr(response.kinds);
        const changed = reset || samples.length > 0 || instruments.length > 0 || kinds.length > 0
            || versionChanged || !_same(state.agents, agents)
            || state.notifications_unread !== unread || !_same(state.hub, hub);
        const next = { cursor: response.cursor, agents, notifications_unread: unread, hub,
                       version, seen: true };
        const update = changed
            ? { reset, samples, instruments, kinds, agents: agents.map(a => Object.assign({}, a)),
                notifications_unread: unread, hub: Object.assign({}, hub), version,
                version_changed: versionChanged }
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
        if (!st.connected) return 'Reconnecting… · last update ' + agoText(st.last_ok_at, now);
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
            bgFetch(pollUrl(state.cursor), { cache: 'no-store',
                                             headers: { Accept: 'application/json' } }, d.fetch)
                .then(r => {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.json();
                })
                .then(body => {
                    const out = applyResponse(state, body);
                    if (out.state === state) throw new Error('bad /api/live answer');
                    state = out.state;
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
                               version_changed: false };
                Promise.resolve().then(() => {        // after subscribe() has returned
                    if (subs.includes(fn)) { try { fn(snap); } catch (_) { /* its problem */ } }
                });
            }
            return function unsubscribe() { subs = subs.filter(x => x !== fn); };
        }

        return {
            start, stop, subscribe, pollNow,
            agents: () => state.agents.map(a => Object.assign({}, a)),
            status: () => Object.assign({}, status),
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
        agents: () => page.agents(), status: () => page.status(), pollNow: () => page.pollNow(),
        bgFetch, nextDelay, initialState, applyResponse, pollUrl, statusText, agoText, createPoller,
    };
    root.GCLive = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
