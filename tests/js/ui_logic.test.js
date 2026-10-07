// The v3.1 pages' pure logic (static/js/ui_logic.js): status glyphs, relative
// times, the Activity feed's text and merge, the setup labels, the admin
// password closure, recents, theme and the fallback poll's diff.
const U = require('../../static/js/ui_logic.js');

module.exports = (t) => {
    const now = Date.parse('2026-09-30T12:00:00Z');

    // ── relative time, ticking on the client
    t.eq(U.relTime('2026-09-30T11:59:58+00:00', now), 'just now');
    t.eq(U.relTime('2026-09-30T11:59:48+00:00', now), '12 s ago');
    t.eq(U.relTime('2026-09-30T11:56:00+00:00', now), '4 min ago');
    t.eq(U.relTime('2026-09-30T09:00:00+00:00', now), '3 h ago');
    t.eq(U.relTime('2026-09-28T12:00:00+00:00', now), '2 d ago');
    t.eq(U.relTime('2026-09-30T12:00:30+00:00', now), 'just now');     // clock a hair ahead
    t.eq(U.relTime(null, now), null);
    t.eq(U.relTime('garbage', now), null);

    // ── "done by Ryan C": web_auth.actor() is '<name> (<address>)'
    t.eq(U.actorName('Ryan C (10.0.0.5)'), 'Ryan C');
    t.eq(U.actorName('Admin (break-glass) (127.0.0.1)'), 'Admin (break-glass)');
    t.eq(U.actorName('10.0.0.5'), '10.0.0.5');
    t.eq(U.actorName(null), null);
    t.eq(U.actorName(''), null);
    t.eq(U.initials('Ryan Cunningham'), 'RC');
    t.eq(U.initials('ann'), 'A');
    t.eq(U.initials(''), '?');

    // ── agent status: Live / last seen N min ago / Never, with a glyph.
    //    v4.0 lane E: live is the hub's one rule (last_seen at most 90 s old on
    //    the hub's clock: `live`, `last_seen_age_s`); the age ticks from when it
    //    was read (`read_at`); the browser's clock is never compared with last_seen.
    const L = (over) => Object.assign({ last_seen: '2026-09-30T11:59:48+00:00', live: true,
                                        last_seen_age_s: 12, read_at: now }, over || {});
    t.eq(U.agentStatus(null, now), { glyph: 'never', label: 'Never checked in', since: null });
    t.eq(U.agentStatus({ last_seen: null }, now).glyph, 'never');
    t.eq(U.agentStatus(L({ state: 'running' }), now),
         { glyph: 'final', label: 'Live', since: 'checked in 12 s ago' });
    t.eq(U.agentStatus(L({ live: false, last_seen_age_s: 600 }), now),
         { glyph: 'held', label: 'Last seen 10 min ago', since: 'checked in 10 min ago' });
    // live when read, 2 minutes on without an answer: no longer live
    t.eq(U.agentStatus(L(), now + 120000).label, 'Last seen 2 min ago');
    // the hub's clock decides, not the browser's: an old-looking stamp the hub calls live is live
    t.eq(U.agentStatus(L({ last_seen: '2026-09-30T08:00:00+00:00' }), now).label, 'Live');
    // an older hub without `live`: never guessed live
    t.eq(U.agentStatus({ last_seen: '2026-09-30T11:59:48+00:00' }, now).glyph, 'held');
    t.eq(U.agentStatus(L({ state: 'paused' }), now).label, 'Paused');
    t.eq(U.agentStatus(L({ last_seen_age_s: 10, last_error: 'disk full' }), now),
        { glyph: 'error', label: 'Reporting an error', since: 'checked in 10 s ago' });
    t.eq(U.LIVE_SECONDS, 90);

    // GCLive's agents[].status is the agent's own state (idle, sending, paused,
    // hub-unreachable, auth-error, config-error, unknown)
    t.eq(U.agentStatus(L({ status: 'sending' }), now).label, 'Live');
    t.eq(U.agentStatus(L({ status: 'paused' }), now).label, 'Paused');
    t.eq(U.agentStatus(L({ status: 'auth-error' }), now).glyph, 'error');
    t.eq(U.agentStatus(L({ status: 'config-error' }), now).label, 'Reporting an error');
    t.eq(U.agentStatus(L({ status: 'hub-unreachable' }), now).glyph, 'error');
    t.eq(U.agentStatus(L({ state: 'idle', status: 'paused' }), now).label, 'Paused');

    // ── sample status: never by colour alone (glyph + text)
    t.eq(U.sampleStatus('final'), { glyph: 'final', text: 'Final' });
    t.eq(U.sampleStatus('received'), { glyph: 'working', text: 'Processing' });
    t.eq(U.sampleStatus('awaiting_calibration').glyph, 'held');
    t.eq(U.sampleStatus('pending_corrections').text, 'Waiting for correction factors');
    t.eq(U.sampleStatus('error'), { glyph: 'error', text: 'Error' });
    t.eq(U.sampleStatus('whatever').glyph, 'held');

    // ── setup labels
    // one wording everywhere: "5 of 8 done · Next: <step>"; the sidebar says "Setup · 5/8"
    t.eq(U.setupLabel({ total: 8, done: 5, step: 4, ready: false, next_title: 'Wait for the agent to check in' }),
        '5 of 8 done · Next: Wait for the agent to check in');
    t.eq(U.setupLabel({ total: 8, done: 3, step: 4, ready: false }), '3 of 8 done');
    t.eq(U.setupLabel({ total: 8, done: 8, step: null, ready: true }), 'Ready');
    t.eq(U.setupLabel(null), '');
    t.eq(U.stepBadge('done'), 'Done');
    t.eq(U.stepBadge('current'), 'Now');
    t.eq(U.stepBadge('waiting'), 'Waiting');
    t.eq(U.stepBadge('blocked'), 'Blocked');
    const insts = [
        { id: 'gc1', name: 'GC-1', enabled: 1, setup: { ready: true, step: null } },
        { id: 'gc3', name: 'GC-3', enabled: 0, setup: { ready: false, step: 2, done: 1, total: 8 } },
        { id: 'gc2', name: 'GC-2', enabled: 1, setup: { ready: false, step: 4, done: 5, total: 8 } },
    ];
    // the sidebar names the GC (v4.0 lane E2 review)
    t.eq(U.setupNav(insts), { instrument_id: 'gc2', step: 4, text: 'GC-2 · 5/8', title: 'Setup: GC-2 · 5 of 8 done' });
    t.eq(U.setupNav([insts[0]]), null);
    // several unfinished: the one being viewed first, then the furthest along; all listed
    const many = [
        { id: 'gc1', name: 'GC-1', enabled: 1, setup: { ready: false, step: 3, done: 2, total: 8 } },
        { id: 'gc2', name: 'GC-2', enabled: 1, setup: { ready: false, step: 4, done: 5, total: 8 } },
        { id: 'gc4', name: 'GC-4', enabled: 1, setup: { ready: false, step: 2, done: 1, total: 8 } },
    ];
    t.eq(U.setupNav(many).instrument_id, 'gc2');
    t.eq(U.setupNav(many).text, 'GC-2 · 5/8, GC-1 · 2/8, GC-4 · 1/8');
    t.eq(U.setupNav(many, 'gc1').instrument_id, 'gc1');
    t.eq(U.setupNav(many, 'gc1').text, 'GC-1 · 2/8, GC-2 · 5/8, GC-4 · 1/8');
    t.eq(U.setupNav(many, 'nope').instrument_id, 'gc2');
    t.eq(U.setupNav([{ id: 'gc9', enabled: 1, setup: { ready: false, step: 1, done: 0, total: 8 } }]).text, 'gc9 · 0/8');
    t.eq(U.setupNav([{ id: 'x', enabled: 1, setup: null }]), null);

    // ── the Activity feed: text segments (rendered with textContent) ...
    const seg = (e) => U.activityText(e).map(s => (s.strong ? '*' + s.text + '*' : s.text)).join('');
    const base = { instrument_id: 'gc2', instrument_name: 'GC-2', by: 'Ryan C (10.0.0.5)' };
    t.eq(seg(Object.assign({ kind: 'installer' }, base)), 'Ryan C downloaded the *GC-2* installer');
    t.eq(seg(Object.assign({ kind: 'calibration_saved', detail: { assigned: 13 } }, base)),
        'Ryan C saved the *GC-2* calibration · 13 peaks');
    t.eq(seg(Object.assign({ kind: 'corrections_saved', detail: { changed: 11 } }, base)),
        'Ryan C saved *GC-2* correction factors (11 changed)');
    t.eq(seg(Object.assign({ kind: 'sample_received', lab_id: '40331' }, base, { by: null })),
        '*GC-2* sent *40331*');
    t.eq(seg(Object.assign({ kind: 'export_written', lab_id: '40330' }, base, { by: null })),
        '*40330* · Written to results CSV (*GC-2*)');
    t.eq(seg(Object.assign({ kind: 'agent_seen' }, base, { by: null })), '*GC-2* agent checked in');
    t.eq(seg(Object.assign({ kind: 'live_since', detail: { live_since: '2026-10-01 08:00:00' } }, base)),
        'Ryan C set *GC-2* live from 2026-10-01 08:00');
    t.eq(seg(Object.assign({ kind: 'live_since', detail: { live_since: null } }, base)),
        'Ryan C took *GC-2* off live');
    t.eq(seg(Object.assign({ kind: 'method_mapped', detail: { method: 'X.M', hub_method: 'D2887' } }, base)),
        'Ryan C mapped *X.M* on *GC-2* to D2887');
    t.eq(seg(Object.assign({ kind: 'report', lab_id: '40329', detail: { report_kind: 'qbench' } }, base, { by: 'Ann B' })),
        'Ann B uploaded the *40329* report to QBench');
    t.eq(seg(Object.assign({ kind: 'token_revoked' }, base, { by: null })), 'The hub revoked the *GC-2* agent key');
    t.eq(seg(Object.assign({ kind: 'nonsense' }, base)), '*GC-2* · nonsense');
    // never "Sent to LEM": the hub writes the results CSV (LEM reads it)
    t.eq(seg({ kind: 'purge', by: 'Ryan C (10.0.0.9)', instrument_id: 'gc1', instrument_name: 'GC-1',
        detail: { scope: 'backfill', samples: 12 } }), 'Ryan C purged *12* samples of *GC-1* (imported history)');
    t.eq(U.ACTIVITY_KINDS.every(k => !seg(Object.assign({ kind: k, detail: {} }, base)).includes('LEM')), true);

    // ... and the merge that prepends new entries
    const a1 = { key: 'a', at: '2026-09-30T11:00:00+00:00' };
    const b1 = { key: 'b', at: '2026-09-30T11:30:00+00:00' };
    let m = U.mergeActivity([], [b1, a1], 10);
    t.eq(m.list.map(e => e.key), ['b', 'a']);
    t.eq(m.added, ['b', 'a']);
    const ag = { key: 'agent:gc2', at: '2026-09-30T11:45:00+00:00' };
    m = U.mergeActivity(m.list, [ag, b1, a1], 10);
    t.eq(m.list.map(e => e.key), ['agent:gc2', 'b', 'a']);
    t.eq(m.added, ['agent:gc2']);
    const ag2 = { key: 'agent:gc2', at: '2026-09-30T11:59:00+00:00' };
    m = U.mergeActivity(m.list, [ag2], 10);
    t.eq(m.list[0].at, ag2.at);
    t.eq(m.added, ['agent:gc2']);                  // moved up: shown as new
    t.eq(U.mergeActivity(m.list, [], 2).list.length, 2);

    // ── the admin password: a closure for 15 minutes, never storage
    let clock = 0;
    const gate = U.makeAdminGate({ ttlMs: 15 * 60 * 1000, now: () => clock });
    t.eq(gate.get(), null);
    gate.set('pw');
    t.eq(gate.get(), 'pw');
    clock = 14 * 60 * 1000;
    t.eq(gate.get(), 'pw');
    t.eq(gate.remainingMs(), 60 * 1000);
    clock = 15 * 60 * 1000 + 1;
    t.eq(gate.get(), null);
    gate.set('pw2');
    gate.clear();
    t.eq(gate.get(), null);
    t.eq(gate.remainingMs(), 0);
    t.eq(JSON.stringify(gate).includes('pw'), false);

    // ── recents on this computer
    let rec = U.recentAdd([], { href: '/instruments/gc1', label: 'GC-1' }, 3, 1000);
    rec = U.recentAdd(rec, { href: '/instruments/gc2', label: 'GC-2' }, 3, 2000);
    rec = U.recentAdd(rec, { href: '/instruments/gc1', label: 'GC-1' }, 3, 3000);
    t.eq(rec.map(r => r.href), ['/instruments/gc1', '/instruments/gc2']);
    rec = U.recentAdd(rec, { href: '/a', label: 'a' }, 3, 4000);
    rec = U.recentAdd(rec, { href: '/b', label: 'b' }, 3, 5000);
    t.eq(rec.map(r => r.href), ['/b', '/a', '/instruments/gc1']);
    t.eq(U.recentClean([{ href: 'javascript:alert(1)', label: 'x' }, { href: '//evil', label: 'y' },
                        { href: '/ok', label: 'ok', at: 1 }, null, 'junk']).map(r => r.href), ['/ok']);

    // ── theme (v5.0): System, Light or Dark; System (follow the OS) unless chosen
    t.eq(U.THEME_CHOICES, ['system', 'light', 'dark']);
    t.eq(U.themeChoice('dark'), 'dark');
    t.eq(U.themeChoice('light'), 'light');
    t.eq(U.themeChoice('system'), 'system');
    t.eq(U.themeChoice(null), 'system');              // never chosen
    t.eq(U.themeChoice(''), 'system');
    t.eq(U.themeChoice('purple'), 'system');          // junk in storage
    t.eq(U.resolveTheme('dark', false), 'dark');      // a choice wins over the OS
    t.eq(U.resolveTheme('dark', true), 'dark');
    t.eq(U.resolveTheme('light', true), 'light');
    t.eq(U.resolveTheme('light', false), 'light');
    t.eq(U.resolveTheme('system', true), 'dark');     // System follows the OS
    t.eq(U.resolveTheme('system', false), 'light');
    t.eq(U.resolveTheme(null, true), 'dark');         // nothing stored: System
    t.eq(U.resolveTheme(null, false), 'light');
    t.eq(U.resolveTheme('purple', true), 'dark');

    // ── the fallback poll (no live.js): which instruments changed
    const s1 = [{ id: 'gc1', name: 'GC-1', agent: { last_seen: 'x' }, counts: { final: 1 } }];
    let d = U.diffSummaries(null, s1);
    t.eq(d.changed, ['gc1']);
    t.eq(U.diffSummaries(d.map, s1).changed, []);
    // the agent's heartbeat alone is an agent update, not an instrument change
    t.eq(U.diffSummaries(d.map, [Object.assign({}, s1[0], { agent: { last_seen: 'y' } })]).changed, []);
    t.eq(U.diffSummaries(d.map, [Object.assign({}, s1[0], { counts: { final: 2 } })]).changed, ['gc1']);
    t.eq(U.agentsFromStatus([{ instrument_id: 'gc1', last_seen: 't', version: '2', host: 'h', state: 'running', x: 1 }]),
        [{ instrument_id: 'gc1', last_seen: 't', version: '2', host: 'h', status: 'running' }]);
    t.eq(U.agentsFromStatus(null), []);

    // ── "Go live now" uses the hub's clock (and the GC's, when its skew is known),
    //    never the browser's
    const loaded = Date.parse('2026-09-30T12:00:00Z');
    // the browser runs 5 min fast; the hub said 14:00:00 when the page loaded
    let g = U.goLiveTime('2026-09-30 14:00:00', loaded, loaded + 90 * 1000, null);
    t.eq(g.value, '2026-09-30 14:01:30');
    t.eq(g.basis, 'hub');
    t.eq(g.text.includes("hub's clock"), true);
    g = U.goLiveTime('2026-09-30 14:00:00', loaded, loaded + 90 * 1000, -600);    // GC 10 min behind
    t.eq(g.value, '2026-09-30 13:51:30');
    t.eq(g.basis, 'gc');
    t.eq(g.text.includes("GC's clock"), true);
    t.eq(U.goLiveTime('2026-12-31 23:59:59', loaded, loaded + 2000, 0).value, '2027-01-01 00:00:01');
    t.eq(U.goLiveTime(null, loaded, loaded, null), null);
    t.eq(U.goLiveTime('garbage', loaded, loaded, null), null);

    // ── live_since in the future, by the hub's clock
    t.eq(U.liveSinceState('2026-10-01 08:00:00', '2026-09-30 12:00:00'),
        { live: false, text: 'Waiting until 2026-10-01 08:00' });
    t.eq(U.liveSinceState('2026-09-01 08:00:00', '2026-09-30 12:00:00'),
        { live: true, text: 'Live since 2026-09-01 08:00' });
    t.eq(U.liveSinceState(null, '2026-09-30 12:00:00'), { live: false, text: 'Not live' });

    // ── where an instrument's page is (a reserved id can't use /instruments/<id>)
    t.eq(U.instrumentHref('gc2'), '/instruments/gc2');
    t.eq(U.instrumentHref('classic'), '/instruments');
    t.eq(U.instrumentHref('activity'), '/instruments');
    t.eq(U.instrumentHref('a b'), '/instruments/a%20b');
    t.eq(U.RESERVED_IDS.includes('setup'), true);

    // ── the Activity feed: the sample's link and injection time
    t.eq(U.activitySample({ sample_id: 7, lab_id: '40330', injection_dt: '2026-09-30 07:00:00' }),
        { href: '/samples/7', injected: 'injected 2026-09-30 07:00' });
    t.eq(U.activitySample({ sample_id: null }), null);
    t.eq(U.activitySample({ sample_id: 3, injection_dt: null }), { href: '/samples/3', injected: null });

    // ── the LEM machine's title for a card (untrusted: shown as text)
    const lem = { source: 'live', machines: [{ uid: 'm1', title: 'GC-1 (Agilent 7890B)' }] };
    t.eq(U.lemTitle(lem, 'm1'), 'GC-1 (Agilent 7890B)');
    t.eq(U.lemTitle(lem, 'm9'), 'm9');
    t.eq(U.lemTitle(null, 'm1'), 'm1');
    t.eq(U.lemTitle(lem, ''), null);
};
