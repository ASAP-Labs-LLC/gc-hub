// static/js/live.js (v4.0 lane E): /api/live's tasks, the full hub state and
// the server's agent liveness flow through applyResponse.
const L = require('../../static/js/live.js');

const AG = (over) => Object.assign({ instrument_id: 'gc1', name: 'GC-1', enabled: true,
    last_seen: '2026-09-30T10:00:00+00:00', version: '1.0', host: 'PC', status: 'idle',
    live: true, last_seen_age_s: 12 }, over || {});
const HUB = (over) => Object.assign({ state: 'running', staged_update: null,
    processing_paused: false, queue: { waiting: 0, running: 0 }, exports_pending: 0,
    paused_by: null, paused_since: null }, over || {});
const TASK = (over) => Object.assign({ id: 'reprocess:1', kind: 'reprocess',
    title: 'Re-process · 2 samples', state: 'running', progress: { done: 0, total: 2, text: null },
    by: 'Ryan C', started_at: '2026-09-30T10:00:00+00:00', ended_at: null, open_url: null,
    download_url: null, mine: true, instrument: null }, over || {});
const RESP = (over) => Object.assign({ cursor: 'b00t:1', reset: false, samples: [],
    instruments: [], kinds: [], agents: [AG()], notifications_unread: 0, hub: HUB(),
    version: 'v4.0.0', tasks: [] }, over || {});

module.exports = (t) => {
    let r = L.applyResponse(L.initialState(), RESP({ tasks: [TASK()] }));
    t.eq(r.update.tasks, [TASK()]);
    t.eq(r.update.boot, 'b00t');
    t.eq(r.update.hub, HUB());
    const s1 = r.state;
    t.eq(L.applyResponse(s1, RESP({ tasks: [TASK()] })).update, null);

    // progress moving is an update; so is a task ending or going away
    r = L.applyResponse(s1, RESP({ tasks: [TASK({ progress: { done: 1, total: 2, text: null } })] }));
    t.eq(r.update.tasks[0].progress.done, 1);
    t.eq(L.applyResponse(s1, RESP({ tasks: [] })).update.tasks, []);

    // an answer without tasks (an older hub) keeps the last list
    r = L.applyResponse(s1, RESP({ tasks: undefined }));
    t.eq(r.update, null);
    t.eq(r.state.tasks, [TASK()]);

    // the agent's age ticks every poll: not an update by itself, but kept
    r = L.applyResponse(s1, RESP({ tasks: [TASK()], agents: [AG({ last_seen_age_s: 15 })] }));
    t.eq(r.update, null);
    t.eq(r.state.agents[0].last_seen_age_s, 15);
    // the server's live flag flipping is
    r = L.applyResponse(s1, RESP({ tasks: [TASK()], agents: [AG({ live: false, last_seen_age_s: 95 })] }));
    t.eq(r.update.agents[0].live, false);

    // the hub's pause and queue counts are updates
    r = L.applyResponse(s1, RESP({ tasks: [TASK()], hub: HUB({ processing_paused: true,
                                                               state: 'processing-paused' }) }));
    t.eq(r.update.hub.processing_paused, true);
    r = L.applyResponse(s1, RESP({ tasks: [TASK()], hub: HUB({ queue: { waiting: 4, running: 1 } }) }));
    t.eq(r.update.hub.queue, { waiting: 4, running: 1 });

    // the poller exposes the latest tasks and hub
    const p = L.createPoller({ fetch: () => new Promise(() => {}), setTimeout: () => 1,
        clearTimeout: () => {}, now: () => 0, isVisible: () => true, on: () => {}, off: () => {} });
    t.eq(p.tasks(), []);
    t.eq(p.hub(), null);

    // ── v4.0 lane E review: an agent's age keeps ticking between updates ──
    // agentAge = the hub's age when read + the time since (never the browser's
    // clock against last_seen); agentLive applies the hub's 90 s rule to it.
    const A = { last_seen: '2026-09-30T10:00:00+00:00', live: true, last_seen_age_s: 30, read_at: 1000 };
    t.eq(L.agentAge(A, 1000), 30);
    t.eq(L.agentAge(A, 31000), 60);
    t.eq(L.agentAge(A, 500), 30);                           // never younger than read
    t.eq(L.agentAge({ last_seen_age_s: 12 }, 99999), 12);   // no read_at: as read
    t.eq(L.agentAge({ last_seen_age_s: null }, 1000), null);
    t.eq(L.agentAge(null, 1000), null);
    t.eq(L.agentLive(A, 31000), true);
    t.eq(L.agentLive(A, 62000), false);                     // 91 s: the hub's rule
    t.eq(L.agentLive(Object.assign({}, A, { live: false }), 1000), false);
    t.eq(L.agentLive({ last_seen: null, live: undefined }, 1000), false);
    t.eq(L.LIVE_SECONDS, 90);

    // the poller stamps read_at on every answer, and agents() carries it, so a
    // page's timer sees the latest ages even when no update was emitted
    let clock = 5000;
    const answers = [RESP({ reset: true, agents: [AG({ last_seen_age_s: 3 })] }),
                     RESP({ agents: [AG({ last_seen_age_s: 6 })] })];
    const timers = [];
    const seen = [];
    const q = L.createPoller({
        fetch: () => Promise.resolve({ ok: true, status: 200,
            text: () => Promise.resolve(JSON.stringify(answers.shift())) }),
        setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout: () => {},
        now: () => clock, isVisible: () => true, on: () => {}, off: () => {} });
    q.subscribe(u => seen.push(u));
    return q.start().then(() => {
        t.eq(q.agents()[0].read_at, 5000);
        t.eq(seen.length, 1);
        t.eq(seen[0].agents[0].read_at, 5000);
        clock = 8000;
        q.pollNow();
        return new Promise(r => setTimeout(r, 20));
    }).then(() => {
        t.eq(seen.length, 1);                               // age alone: no update …
        t.eq(q.agents()[0].last_seen_age_s, 6);             // … but the latest is there
        t.eq(q.agents()[0].read_at, 8000);
        t.eq(L.agentAge(q.agents()[0], 10000), 8);

        // the hub's date rides along; a new day is an update
        let s = L.applyResponse(L.initialState(), RESP({ server_today: '2026-09-30' })).state;
        t.eq(s.server_today, '2026-09-30');
        t.eq(L.applyResponse(s, RESP({ server_today: '2026-09-30' })).update, null);
        const u = L.applyResponse(s, RESP({ server_today: '2026-10-01' })).update;
        t.eq(u.server_today, '2026-10-01');
        t.eq(u.day_changed, true);
    });
};
