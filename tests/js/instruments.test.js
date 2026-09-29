// The Instruments page's pure logic (static/js/instruments_logic.js).
const L = require('../../static/js/instruments_logic.js');

module.exports = (t) => {
    // ── clock skew (agent heartbeat)
    t.eq(L.formatSkew(null), 'unknown');
    t.eq(L.formatSkew(0), 'in step');
    t.eq(L.formatSkew(4), 'in step');
    t.eq(L.formatSkew(75), '1 min 15 s ahead');
    t.eq(L.formatSkew(-600), '10 min behind');
    t.eq(L.formatSkew(30), '30 s ahead');
    t.eq(L.skewWarning(119), null);
    t.eq(L.skewWarning(-121).includes('behind'), true);
    t.eq(L.skewWarning(null), null);
    t.eq(L.skewWarning(200, 300), null);

    // ── agent health: never / stale (15 min) / paused / error / ok
    const now = Date.parse('2026-09-28T12:00:00Z');
    t.eq(L.agentHealth(null, now).level, 'never');
    t.eq(L.agentHealth({ last_seen: null }, now).level, 'never');
    t.eq(L.agentHealth({ last_seen: '2026-09-28T11:40:00+00:00', state: 'idle' }, now).level, 'stale');
    t.eq(L.agentHealth({ last_seen: '2026-09-28T11:59:00+00:00', state: 'paused' }, now).level, 'paused');
    t.eq(L.agentHealth({ last_seen: '2026-09-28T11:59:00+00:00', state: 'idle', last_error: 'x' }, now).level, 'error');
    t.eq(L.agentHealth({ last_seen: '2026-09-28T11:59:30+00:00', state: 'idle' }, now).level, 'ok');
    t.eq(L.agentHealth({ last_seen: 'garbage' }, now).level, 'never');

    // ── corrections inputs (mirrors corrections.validate_values)
    const cuts = ['IBP', '5%', '10%', '20%', '30%', '50%', '70%', '80%', '90%', '95%', 'FBP'];
    const inputs = {};
    cuts.forEach((c, i) => { inputs[c] = String(i - 5); });
    let r = L.parseCorrections(inputs, cuts, 50);
    t.eq(r.errors, []);
    t.eq(r.values.IBP, -5);
    t.eq(r.values.FBP, 5);
    r = L.parseCorrections(Object.assign({}, inputs, { IBP: '' }), cuts, 50);
    t.eq(r.errors.length, 1);
    t.eq(r.errors[0].includes('IBP'), true);
    t.eq(r.values, null);
    r = L.parseCorrections(Object.assign({}, inputs, { '5%': 'abc', '10%': '51', '20%': 'Infinity' }), cuts, 50);
    t.eq(r.errors.length, 3);
    r = L.parseCorrections(Object.assign({}, inputs, { '5%': ' -3.5 ' }), cuts, 50);
    t.eq(r.values['5%'], -3.5);
    r = L.parseCorrections(Object.assign({}, inputs, { '5%': '1e1' }), cuts, 50);
    t.eq(r.values['5%'], 10);
    r = L.parseCorrections(Object.assign({}, inputs, { '5%': '0x10' }), cuts, 50);
    t.eq(r.errors.length, 1);

    // ── live_since from <input type="datetime-local">
    t.eq(L.liveSinceValue('2026-10-01T08:00'), { value: '2026-10-01 08:00:00', error: null });
    t.eq(L.liveSinceValue('2026-10-01T08:00:30'), { value: '2026-10-01 08:00:30', error: null });
    t.eq(L.liveSinceValue(''), { value: '', error: null });
    t.eq(L.liveSinceValue('2026-10-01T08:00Z').error !== null, true);
    t.eq(L.liveSinceValue('yesterday').error !== null, true);
    t.eq(L.liveSinceInput('2026-10-01 08:00:00'), '2026-10-01T08:00:00');
    t.eq(L.liveSinceInput(null), '');

    // ── review I1: confirm clearing live_since, moving it later, or into the future
    const nowL = '2026-09-28 12:00:00';
    t.eq(L.liveSinceConfirm('2026-01-01 00:00:00', '', nowL).includes('stops'), true);
    t.eq(L.liveSinceConfirm(null, '', nowL), null);
    t.eq(L.liveSinceConfirm('2026-01-01 00:00:00', '2026-02-01 00:00:00', nowL).includes('later'), true);
    t.eq(L.liveSinceConfirm('2026-02-01 00:00:00', '2026-01-01 00:00:00', nowL), null);
    t.eq(L.liveSinceConfirm(null, '2026-10-01 00:00:00', nowL).includes('future'), true);
    t.eq(L.liveSinceConfirm('2026-01-01 00:00:00', '2026-01-01 00:00:00', nowL), null);
    t.eq(L.localNow(new Date(2026, 8, 28, 7, 5, 9)), '2026-09-28 07:05:09');

    // ── installer download outcomes
    t.eq(L.installerOutcome(200, null).kind, 'download');
    const c = L.installerOutcome(409, { needs_confirm: true, error: 'has a token', hub_url: 'http://sv1:5560' });
    t.eq(c.kind, 'confirm');
    t.eq(c.message.includes('http://sv1:5560'), true);
    // rev 2: the installer always has a hub URL (the admin-set one, else https://gc.asaplabs.net)
    t.eq(L.installerOutcome(409, { needs_hub_url: true, error: 'set it' }).kind, 'error');
    t.eq(L.installerOutcome(403, { error: 'Incorrect password' }), { kind: 'error', message: 'Incorrect password' });
    t.eq(L.installerOutcome(500, null).kind, 'error');

    // ── methods seen rows
    const rows = L.methodRows([
        { method_name: 'SIMDISB.M', count: 10, mapped_to: 'D2887' },
        { method_name: '', count: 2, mapped_to: null },
        { method_name: 'D7096.M', count: 3, mapped_to: null },
    ]);
    t.eq(rows.map(x => x.label), ['D7096.M', 'SIMDISB.M', '(no method name)']);
    t.eq(rows.map(x => x.action), ['map', 'unmap', 'review']);

    // ── standards picker (D12): own first, then others with the warning
    const picked = L.orderStandards([
        { name: 'B', instrument_id: 'gc1', warning: 'w1' },
        { name: 'A', instrument_id: 'gc2', warning: null },
        { name: 'C', instrument_id: null, warning: 'w2' },
    ], 'gc2');
    t.eq(picked.map(s => s.name), ['A', 'B', 'C']);
    t.eq(picked.map(s => s.cross), [false, true, true]);

    // ── release summary
    t.eq(L.releaseSummary([{ sample_id: 1, ok: true, seq: 4 }, { sample_id: 2, ok: false, error: 'x' }]),
        '1 released, 1 refused (2: x)');
    t.eq(L.releaseSummary([]), 'Nothing released.');

    // ── calibration badge
    t.eq(L.calibrationBadge({ usable: true, assigned: 12 }), { text: 'Calibration usable (12 anchors)', level: 'ok' });
    t.eq(L.calibrationBadge({ usable: false, problem: 'No CDF' }), { text: 'No CDF', level: 'bad' });
    t.eq(L.calibrationBadge(null), { text: 'Calibration unknown', level: 'bad' });
};
