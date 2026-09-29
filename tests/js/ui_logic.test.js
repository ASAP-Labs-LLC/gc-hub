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

    // ── agent status: Live / last seen N min ago / Never, with a glyph
    t.eq(U.agentStatus(null, now), { glyph: 'never', label: 'Never checked in', since: null });
    t.eq(U.agentStatus({ last_seen: null }, now).glyph, 'never');
    const live = U.agentStatus({ last_seen: '2026-09-30T11:59:48+00:00', state: 'running' }, now);
    t.eq(live, { glyph: 'final', label: 'Live', since: 'checked in 12 s ago' });
    const late = U.agentStatus({ last_seen: '2026-09-30T11:50:00+00:00' }, now);
    t.eq(late, { glyph: 'held', label: 'Last seen 10 min ago', since: 'checked in 10 min ago' });
    t.eq(U.agentStatus({ last_seen: '2026-09-30T11:59:50+00:00', state: 'paused' }, now).label, 'Paused');
    t.eq(U.agentStatus({ last_seen: '2026-09-30T11:59:50+00:00', last_error: 'disk full' }, now),
        { glyph: 'error', label: 'Reporting an error', since: 'checked in 10 s ago' });

    // GCLive's agents[].status is the agent's own state (idle, sending, paused,
    // hub-unreachable, auth-error, config-error, unknown)
    const fresh = '2026-09-30T11:59:50+00:00';
    t.eq(U.agentStatus({ last_seen: fresh, status: 'sending' }, now).label, 'Live');
    t.eq(U.agentStatus({ last_seen: fresh, status: 'paused' }, now).label, 'Paused');
    t.eq(U.agentStatus({ last_seen: fresh, status: 'auth-error' }, now).glyph, 'error');
    t.eq(U.agentStatus({ last_seen: fresh, status: 'config-error' }, now).label, 'Reporting an error');
    t.eq(U.agentStatus({ last_seen: fresh, status: 'hub-unreachable' }, now).glyph, 'error');
    t.eq(U.agentStatus({ last_seen: fresh, state: 'idle', status: 'paused' }, now).label, 'Paused');

    // ── sample status: never by colour alone (glyph + text)
    t.eq(U.sampleStatus('final'), { glyph: 'final', text: 'Final' });
    t.eq(U.sampleStatus('received'), { glyph: 'working', text: 'Processing' });
    t.eq(U.sampleStatus('awaiting_calibration').glyph, 'held');
    t.eq(U.sampleStatus('pending_corrections').text, 'Waiting for correction factors');
    t.eq(U.sampleStatus('error'), { glyph: 'error', text: 'Error' });
    t.eq(U.sampleStatus('whatever').glyph, 'held');

    // ── setup labels
    t.eq(U.setupLabel({ total: 8, done: 3, step: 4, ready: false }), 'Step 4 of 8');
    t.eq(U.setupLabel({ total: 8, done: 8, step: null, ready: true }), 'Ready');
    t.eq(U.setupLabel(null), '');
    t.eq(U.STEP_SHORT.length, 8);
    t.eq(U.nextStepText({ total: 8, done: 3, step: 4, ready: false }), 'next: agent checks in');
    t.eq(U.nextStepText({ ready: true }), '');
    t.eq(U.stepBadge('done'), 'Done');
    t.eq(U.stepBadge('current'), 'Now');
    t.eq(U.stepBadge('waiting'), 'Waiting');
    t.eq(U.stepBadge('blocked'), 'Blocked');
    const insts = [
        { id: 'gc1', name: 'GC-1', enabled: 1, setup: { ready: true, step: null } },
        { id: 'gc3', name: 'GC-3', enabled: 0, setup: { ready: false, step: 2 } },
        { id: 'gc2', name: 'GC-2', enabled: 1, setup: { ready: false, step: 4 } },
    ];
    t.eq(U.setupNav(insts), { instrument_id: 'gc2', step: 4, text: 'Step 4' });
    t.eq(U.setupNav([insts[0]]), null);
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

    // ── theme: light by default
    t.eq(U.resolveTheme('dark', false), 'dark');
    t.eq(U.resolveTheme('light', true), 'light');
    t.eq(U.resolveTheme('system', true), 'dark');
    t.eq(U.resolveTheme('system', false), 'light');
    t.eq(U.resolveTheme(null, true), 'light');
    t.eq(U.resolveTheme('purple', true), 'light');

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

    // ── the LEM machine's title for a card (untrusted: shown as text)
    const lem = { source: 'live', machines: [{ uid: 'm1', title: 'GC-1 (Agilent 7890B)' }] };
    t.eq(U.lemTitle(lem, 'm1'), 'GC-1 (Agilent 7890B)');
    t.eq(U.lemTitle(lem, 'm9'), 'm9');
    t.eq(U.lemTitle(null, 'm1'), 'm1');
    t.eq(U.lemTitle(lem, ''), null);
};
