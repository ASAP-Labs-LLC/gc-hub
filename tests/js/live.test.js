// static/js/live.js (v3.1 live updates): the pure cursor/reducer logic and the
// poll cadence. The poller itself is a thin wrapper around these.
const L = require('../../static/js/live.js');

const AG = (over) => Object.assign({ instrument_id: 'gc1', last_seen: '2026-09-30T10:00:00+00:00',
    version: '1.0', host: 'PC', status: 'idle' }, over || {});
const RESP = (over) => Object.assign({ cursor: 'b:1', reset: false, samples: [], instruments: [],
    kinds: [], agents: [AG()], notifications_unread: 0,
    hub: { state: 'running', staged_update: null }, version: 'v3.1.0' }, over || {});

module.exports = (t) => {
    // ── nextDelay: 3 s visible, 30 s hidden, backing off on failures ──
    t.eq(L.nextDelay(true, 0), 3000);
    t.eq(L.nextDelay(false, 0), 30000);
    t.eq(L.nextDelay(true, 1), 6000);
    t.eq(L.nextDelay(true, 2), 12000);
    t.eq(L.nextDelay(true, 3), 12000);           // a visible tab retries at least every 12 s
    t.eq(L.nextDelay(true, 10), 12000);
    t.eq(L.nextDelay(false, 1), 60000);
    t.eq(L.nextDelay(false, 9), 60000);
    t.eq(L.nextDelay(true, -1), 3000);           // nonsense counts as none
    t.eq(L.nextDelay(true, NaN), 3000);

    // ── applyResponse: the first answer is a reset carrying the snapshot ──
    const s0 = L.initialState();
    t.eq(s0.cursor, null);
    let r = L.applyResponse(s0, RESP({ reset: true }));
    t.eq(r.state.cursor, 'b:1');
    t.eq(r.update, { reset: true, samples: [], instruments: [], kinds: [], agents: [AG()],
                     notifications_unread: 0, hub: { state: 'running', staged_update: null },
                     version: 'v3.1.0', version_changed: false, tasks: [], boot: 'b',
                     server_today: null, day_changed: false });
    // the first answer is a reset even if the server did not say so
    t.eq(L.applyResponse(s0, RESP()).update.reset, true);

    // nothing changed: no update, cursor kept
    let s1 = r.state;
    r = L.applyResponse(s1, RESP());
    t.eq(r.update, null);
    t.eq(r.state.cursor, 'b:1');

    // changed samples / instruments
    r = L.applyResponse(s1, RESP({ cursor: 'b:4', samples: [3, 9], instruments: ['gc2'] }));
    t.eq(r.update.reset, false);
    t.eq(r.update.samples, [3, 9]);
    t.eq(r.update.instruments, ['gc2']);
    t.eq(r.state.cursor, 'b:4');

    // an agent heartbeat (the snapshot changed) is an update; same snapshot is not
    r = L.applyResponse(s1, RESP({ agents: [AG({ last_seen: '2026-09-30T10:00:30+00:00' })] }));
    t.eq(r.update.samples, []);
    t.eq(r.update.agents[0].last_seen, '2026-09-30T10:00:30+00:00');
    t.eq(r.state.agents[0].last_seen, '2026-09-30T10:00:30+00:00');
    t.eq(L.applyResponse(s1, RESP({ agents: [AG()] })).update, null);

    // the event kinds pass through; a notification event is an update even
    // when the count is unchanged (one dismissed, one added)
    r = L.applyResponse(s1, RESP({ cursor: 'b:5', kinds: ['notification'] }));
    t.eq(r.update.kinds, ['notification']);
    t.eq(r.update.notifications_unread, 0);

    // a new hub version (the updater installed a release): flagged, once
    r = L.applyResponse(s1, RESP({ version: 'v3.1.1' }));
    t.eq(r.update.version, 'v3.1.1');
    t.eq(r.update.version_changed, true);
    t.eq(L.applyResponse(r.state, RESP({ version: 'v3.1.1' })).update, null);
    t.eq(L.applyResponse(s1, RESP({ version: 'v3.1.1', reset: true, cursor: 'z:0' }))
        .update.version_changed, true);

    // the notification count and the hub state
    t.eq(L.applyResponse(s1, RESP({ notifications_unread: 2 })).update.notifications_unread, 2);
    t.eq(L.applyResponse(s1, RESP({ hub: { state: 'processing-paused', staged_update: null } }))
        .update.hub.state, 'processing-paused');
    t.eq(L.applyResponse(s1, RESP({ hub: { state: 'running', staged_update: 'v3.2.0' } }))
        .update.hub.staged_update, 'v3.2.0');

    // a later reset (boot_id changed, cursor overflow)
    r = L.applyResponse(s1, RESP({ cursor: 'c:0', reset: true }));
    t.eq(r.update.reset, true);
    t.eq(r.state.cursor, 'c:0');

    // a garbled answer changes nothing
    t.eq(L.applyResponse(s1, null), { state: s1, update: null });
    t.eq(L.applyResponse(s1, { reset: false }), { state: s1, update: null });
    t.eq(L.applyResponse(s1, 'x'), { state: s1, update: null });
    // a field the answer lacks keeps its last value
    r = L.applyResponse(s1, { cursor: 'b:2', reset: false });
    t.eq(r.update, null);
    t.eq(r.state.cursor, 'b:2');

    // applyResponse is pure: the old state is untouched
    t.eq(s1.cursor, 'b:1');
    t.eq(s1.agents, [AG()]);

    // ── pollUrl ──
    t.eq(L.pollUrl(null), '/api/live');
    t.eq(L.pollUrl('b:1'), '/api/live?since=b%3A1');

    // ── the "Live · updated Ns ago" text ──
    const now = 1_000_000;
    t.eq(L.statusText({ connected: true, last_ok_at: now - 800, error: null }, now),
         'Live · updated just now');
    t.eq(L.statusText({ connected: true, last_ok_at: now - 12_000, error: null }, now),
         'Live · updated 12 s ago');
    t.eq(L.statusText({ connected: true, last_ok_at: now - 85_000, error: null }, now),
         'Live · updated 1 min ago');
    // no answer for longer than three hidden-tab polls is not "Live", whatever the flag says
    t.eq(L.statusText({ connected: true, last_ok_at: now - 125_000, error: null }, now),
         'Reconnecting… · last update 2 min ago');
    t.eq(L.statusText({ connected: false, last_ok_at: now - 40_000, error: 'HTTP 503' }, now),
         'Reconnecting… · last update 40 s ago');
    t.eq(L.statusText({ connected: false, last_ok_at: 0, error: null }, now), 'Connecting…');
    t.eq(L.statusText(null, now), 'Connecting…');

    // ── agoText: "Checked in 12 s ago" ticks on the client from last_seen ──
    t.eq(L.agoText(null, now), 'never');
    t.eq(L.agoText(now - 3_000, now), 'just now');
    t.eq(L.agoText(now - 42_000, now), '42 s ago');
    t.eq(L.agoText(now - 60_000 * 5, now), '5 min ago');
    t.eq(L.agoText(now - 3600_000 * 3, now), '3 h ago');
    t.eq(L.agoText(now - 86400_000 * 2, now), '2 d ago');
    t.eq(L.agoText(now + 5_000, now), 'just now');            // a clock a little ahead
    t.eq(L.agoText(new Date(now - 42_000).toISOString(), now), '42 s ago');   // last_seen as sent
    t.eq(L.agoText('not a date', now), 'never');
};
