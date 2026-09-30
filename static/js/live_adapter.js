// Live updates for the v3.1 pages (GCLiveAdapter), coded against lane A's
// fixed GCLive contract:
//   GCLive.start(); GCLive.subscribe(fn) -> unsubscribe
//   fn(update): {reset, samples:[ids], instruments:[ids],
//                agents:[{instrument_id, last_seen, version, host, status}],
//                notifications_unread, hub:{state, staged_update}}
//   GCLive.agents(); GCLive.status() -> {connected, last_ok_at, error}
// When window.GCLive is absent (live.js not loaded), it degrades to a 5 s poll
// of the same data (30 s while the tab is hidden), marked X-GC-Background so
// an open tab never counts as activity. createFallback is pure enough for
// node (tests/js/live_adapter.test.js): fetchJSON and timers are injected.
(function (root) {
    'use strict';
    const U = root.GCUi || (typeof require !== 'undefined' ? require('./ui_logic.js') : null);

    const VISIBLE_MS = 5000;
    const HIDDEN_MS = 30000;

    function createFallback(deps) {
        const fetchJSON = deps.fetchJSON;
        const setT = deps.setTimeout || setTimeout;
        const clearT = deps.clearTimeout || clearTimeout;
        const now = deps.now || (() => Date.now());
        const hidden = deps.hidden || (() => false);
        const subs = new Set();
        let prev = null;
        let agents = [];
        let state = { connected: false, last_ok_at: null, error: null };
        let timer = null;
        let running = false;
        let inFlight = false;

        async function poll() {
            if (inFlight) return;
            inFlight = true;
            try {
                const [inst, ag, notes] = await Promise.all([
                    fetchJSON('/api/instruments'), fetchJSON('/api/agents'), fetchJSON('/api/notifications')]);
                if (!inst || !Array.isArray(inst.instruments)) throw new Error('no instruments answer');
                const d = U.diffSummaries(prev, inst.instruments);
                const update = {
                    reset: prev === null,
                    samples: [],
                    instruments: d.changed,
                    agents: U.agentsFromStatus(ag && ag.agents),
                    notifications_unread: Array.isArray(notes) ? notes.length : null,
                    hub: null,
                };
                prev = d.map;
                agents = update.agents;
                state = { connected: true, last_ok_at: now(), error: null };
                for (const fn of Array.from(subs)) {
                    try { fn(update); } catch (e) { if (typeof console !== 'undefined') console.error(e); }
                }
            } catch (e) {
                state = { connected: false, last_ok_at: state.last_ok_at, error: String(e && e.message || e) };
            } finally {
                inFlight = false;
            }
        }

        function schedule() {
            if (!running) return;
            clearT(timer);
            timer = setT(async () => { await poll(); schedule(); }, hidden() ? HIDDEN_MS : VISIBLE_MS);
        }

        return {
            start() {
                if (running) return;
                running = true;
                poll().then(schedule);
            },
            stop() { running = false; clearT(timer); },
            poke() { if (running) { poll().then(schedule); } },
            subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
            agents() { return agents.slice(); },
            status() { return Object.assign({}, state); },
            _poll: poll,
        };
    }

    const api = { createFallback, VISIBLE_MS, HIDDEN_MS };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (typeof window === 'undefined' || typeof document === 'undefined') return;

    let impl = null;
    function backend() {
        if (impl) return impl;
        if (window.GCLive && typeof window.GCLive.subscribe === 'function') {
            window.GCLive.start();                      // idempotent (contract)
            impl = window.GCLive;
        } else {
            const fb = createFallback({
                fetchJSON: (path) => fetch(path, { headers: { Accept: 'application/json', 'X-GC-Background': '1' },
                                                   cache: 'no-store' })
                    .then(r => (r.ok ? window.GCSession.readJson(r).then(x => x.body) : null)),
                hidden: () => document.visibilityState === 'hidden',
            });
            document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') fb.poke(); });
            window.addEventListener('focus', () => fb.poke());
            fb.start();
            impl = fb;
        }
        return impl;
    }

    root.GCLiveAdapter = {
        subscribe(fn) { return backend().subscribe(fn); },
        agents() { return backend().agents(); },
        status() { return impl ? impl.status() : { connected: false, last_ok_at: null, error: null }; },
        usingFallback() { return !!impl && impl !== window.GCLive; },
    };
})(typeof window !== 'undefined' ? window : globalThis);
