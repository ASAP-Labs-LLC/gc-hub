// settings_logic.js (v5.0 lane R): /settings groups what the classic Settings
// modal mixed together (analyst preferences, admin tuning, server facts) and
// saves only what changed, split by who may change it (/api/settings:
// OPERATOR_KEYS freely, ADMIN_KEYS with the admin password, anything else 400).
const S = require('../../static/js/settings_logic.js');

module.exports = async (t) => {
    // ── the sections, in order, each with its audience ───────────────────
    t.eq(S.SECTIONS.map((s) => s.id),
        ['browser', 'flags', 'bestfit', 'findings', 'compare', 'standards', 'qbench', 'lem', 'server']);
    t.eq(S.SECTIONS.filter((s) => s.audience === 'admin').map((s) => s.id),
        ['bestfit', 'findings', 'compare', 'standards', 'lem']);
    t.eq(S.section('server').audience, 'readonly');
    t.eq(S.section('nope'), null);

    // every field is a key /api/settings lets its audience change
    for (const f of S.FIELDS) {
        const sec = S.section(f.section);
        t.eq(!!sec, true);
        t.eq(S.audienceOf(f.key), sec.audience === 'admin' ? 'admin' : 'operator');
    }
    t.eq(S.audienceOf('sample_flag_rules'), 'operator');
    t.eq(S.audienceOf('bestfit_threshold'), 'admin');
    t.eq(S.audienceOf('watch_dir'), null);                  // a path: never saved from here
    t.eq(S.fieldsFor('bestfit').map((f) => f.key),
        ['bestfit_enabled', 'bestfit_threshold', 'bestfit_shift_tolerance_min', 'bestfit_mix_min_frac']);
    t.eq(S.fieldsFor('findings').map((f) => f.key), ['analysis_min_width_min', 'analysis_merge_gap_min',
        'analysis_spike_min_width_min', 'analysis_spike_max_fwhm_min', 'analysis_spike_min_dominance',
        'analysis_spike_report_threshold']);

    // ── validation, next to the field ────────────────────────────────────
    const thr = S.field('bestfit_threshold');
    t.eq(S.validateField(thr, '0.93'), null);
    t.eq(S.validateField(thr, '1.5'), 'Between 0 and 1.');
    t.eq(S.validateField(thr, 'abc'), 'A number, please.');
    t.eq(S.validateField(thr, ''), 'Required.');
    t.eq(S.validateField(S.field('analysis_spike_report_threshold'), ''), null);   // empty = moderate
    t.eq(S.validateField(S.field('analysis_min_width_min'), '-1'), 'Zero or more.');
    t.eq(S.validateField(S.field('analysis_window'), '30.5'), 'A whole number, please.');
    t.eq(S.validateField(S.field('bestfit_enabled'), 'true'), null);
    t.eq(S.validateField(S.field('lem_url'), 'https://lem.asaplabs.net'), null);
    t.eq(S.validateField(S.field('lem_url'), 'https://lem.asaplabs.net/api'), 'http(s)://host or http(s)://host:port, no path.');
    t.eq(S.validateField(S.field('lem_url'), 'ftp://x'), 'http(s)://host or http(s)://host:port, no path.');

    // ── what a save sends ────────────────────────────────────────────────
    const current = { bestfit_enabled: 'true', bestfit_threshold: '0.93', analysis_min_width_min: '0.05',
        sample_flag_rules: '', early_signal_enabled: 'true', watch_dir: 'C:\\x', lem_url: 'https://lem.asaplabs.net' };
    const same = S.planSave(current, { bestfit_threshold: '0.93', analysis_min_width_min: '0.050' });
    t.eq(same.admin, { analysis_min_width_min: '0.050' });    // compared as the server does: as text
    t.eq(S.planSave(current, { bestfit_threshold: '0.93' }), { operator: {}, admin: {}, errors: {} });
    const plan = S.planSave(current, { bestfit_threshold: '0.9', bestfit_enabled: 'false', watch_dir: 'D:\\y' });
    t.eq(plan.admin, { bestfit_threshold: '0.9', bestfit_enabled: 'false' });
    t.eq(plan.operator, {});
    t.eq(plan.errors, { watch_dir: 'Set on the server, not here.' });        // never posted
    t.eq(S.planSave(current, { bestfit_threshold: '9' }).errors, { bestfit_threshold: 'Between 0 and 1.' });
    t.eq(S.planSave(current, { bestfit_threshold: '9' }).admin, {});
    // missing keys fall back to the server's defaults (as settings.DEFAULTS)
    t.eq(S.planSave({}, { bestfit_threshold: '0.93' }).admin, {});

    // flag rules: operator, saved only when they differ from what is in effect
    const rules = [{ name: 'Early', condition: 'above', threshold: 7500, t_start: 0, t_end: 0.5, color: '#e67e22', enabled: true }];
    const fp = S.planSave(current, {}, rules);
    t.eq(Object.keys(fp.operator), ['sample_flag_rules']);
    t.eq(JSON.parse(fp.operator.sample_flag_rules), rules);
    const saved = Object.assign({}, current, { sample_flag_rules: JSON.stringify(rules) });
    t.eq(S.planSave(saved, {}, rules).operator, {});
    t.eq(S.planSave(saved, {}, [{ name: '', condition: 'sideways' }]).errors,
        { sample_flag_rules: 'Rule 1: needs a name, above or below, a threshold and a window that ends after it starts.' });
    t.eq(S.checkRules([]), null);

    // ── the body a save posts (admin keys need the password added by the unlock) ──
    t.eq(S.saveBody(plan, false), null);                        // admin changes need the unlock
    t.eq(S.saveBody(plan, true), { bestfit_threshold: '0.9', bestfit_enabled: 'false' });
    t.eq(S.saveBody({ operator: { series_colors: '#000' }, admin: {}, errors: {} }, false), { series_colors: '#000' });
    t.eq(S.saveBody({ operator: {}, admin: {}, errors: {} }, false), null);

    // ── server paths: read-only facts, the v1 leftovers dropped ──────────
    const facts = S.serverPaths({ watch_dir: 'C:\\w', processed_cdf_dir: 'C:\\p', distill_output: 'C:\\d',
        export_folder: 'C:\\data\\exports', comparison_defaults_dir: 'C:\\data\\std', analysis_report_logo: '',
        correction_factors_json: '\\\\srv\\cf.json' });
    t.eq(facts.map((f) => f.label), ['Report PDFs', 'Comparison standards', 'Report logo',
        'First correction factors (GC-1, seed only)']);
    t.eq(facts[2].value, 'None: reports use the built-in header');
    t.eq(facts.some((f) => /C:\\[wpd]$/.test(f.value)), false);

    // ── standards: tag, rename, delete ───────────────────────────────────
    t.eq(S.standardNameProblem(''), 'A name, please.');
    t.eq(S.standardNameProblem('Diesel #2'), null);
    t.eq(S.standardNameProblem('a/b'), 'No slashes, no dot at the start, none of : * ? " < > |.');
    t.eq(S.standardNameProblem('..x'), 'No slashes, no dot at the start, none of : * ? " < > |.');
    t.eq(S.standardNameProblem('x'.repeat(121)), 'At most 120 characters.');
    t.eq(S.standardTagText({ instrument_id: null }), 'Not tagged');
    t.eq(S.standardTagText({ instrument_id: 'gc2', instrument_name: 'GC-2' }), 'GC-2');
    t.eq(S.standardTagText({ instrument_id: 'gc2', missing: true }), 'gc2 · file missing');

    // ── standards: add from a run (v8.1) ─────────────────────────────────
    // the Samples list labels re-runs "<lab ID> (N)"; Add takes that label too
    t.eq(S.parseRunLabel('DIESEL STD (30)'), { lab: 'DIESEL STD', run: 30 });
    t.eq(S.parseRunLabel('40304'), { lab: '40304', run: null });
    t.eq(S.parseRunLabel('40304(2)'), { lab: '40304', run: 2 });
    t.eq(S.parseRunLabel(' 40304 (2) '), { lab: '40304', run: 2 });
    t.eq(S.parseRunLabel('A (1) (12)'), { lab: 'A (1)', run: 12 });
    t.eq(S.parseRunLabel('(2)'), { lab: '(2)', run: null });              // no lab ID before it
    t.eq(S.parseRunLabel('40304 (0)'), { lab: '40304 (0)', run: null });  // runs count from 1
    t.eq(S.parseRunLabel('40304 (x)'), { lab: '40304 (x)', run: null });
    t.eq(S.parseRunLabel(''), { lab: '', run: null });

    // /api/lab runs, newest first, with the list's run_no / display_name
    const run = (sid, inst, no, dt, status) => ({ sample_id: sid, lab_id: 'DIESEL STD', instrument: inst,
        instrument_name: inst.toUpperCase().replace('GC', 'GC-'), injection_dt: dt, status: status || 'final',
        run_no: no, display_name: no > 1 ? 'DIESEL STD (' + no + ')' : 'DIESEL STD' });
    const r30 = run(31, 'gc1', 30, '2026-10-03 10:15:00');
    const b30 = run(32, 'gc2', 30, '2026-10-04 08:05:00', 'awaiting_calibration');
    const r29 = run(29, 'gc1', 29, '2026-10-02 10:15:00');
    const lab = { lab_id: 'DIESEL STD', sample_id: 31, runs: [b30, r30, r29] };
    const one = { lab_id: '40304', sample_id: 7, runs: [{ sample_id: 7, lab_id: '40304', instrument: 'gc1',
        instrument_name: 'GC-1', injection_dt: '2026-09-25 14:23:00', status: 'final', run_no: 1, display_name: '40304' }] };

    // one run: added at once, as before
    t.eq(S.addStandardRuns(one, null), { runs: one.runs, chosen: one.runs[0], selected: 7, prompt: null, error: null });
    // several, no "(N)": pick one, the resolver's own run preselected
    const many = S.addStandardRuns(lab, null);
    t.eq([many.chosen, many.selected, many.error], [null, 31, null]);
    t.eq(many.runs.map((r) => r.sample_id), [32, 31, 29]);
    t.eq(many.prompt, 'DIESEL STD has 3 runs. Pick the one to add.');
    // "(30)" names one run per GC: here two, so pick; the resolver's run first
    const two = S.addStandardRuns(lab, 30);
    t.eq([two.runs.map((r) => r.sample_id), two.chosen, two.selected], [[32, 31], null, 31]);
    t.eq(two.prompt, 'DIESEL STD (30) is on 2 GCs. Pick the one to add.');
    t.eq(S.addStandardRuns(lab, 29).chosen, r29);
    // the preselected run is the first when the resolver's own is not among them
    t.eq(S.addStandardRuns({ lab_id: 'DIESEL STD', sample_id: 29, runs: [b30, r30, r29] }, 30).selected, 32);
    t.eq(S.addStandardRuns(lab, 31), { runs: [], chosen: null, selected: null, prompt: null,
        error: 'No run 31 of DIESEL STD was found.' });

    // the lookup: the whole text first (a lab ID may hold "(2)"), then the label's lab ID
    const asked = [];
    const hub = { 'DIESEL STD': lab, '40304 (2)': one, '40304': one };
    const fetchLab = async (id) => { asked.push(id); return hub[id] || null; };
    t.eq((await S.findAddRuns(' DIESEL STD (30) ', fetchLab)).runs.map((r) => r.sample_id), [32, 31]);
    t.eq(asked, ['DIESEL STD (30)', 'DIESEL STD']);
    asked.length = 0;
    t.eq((await S.findAddRuns('40304 (2)', fetchLab)).chosen, one.runs[0]);    // literally that lab ID
    t.eq(asked, ['40304 (2)']);
    asked.length = 0;
    t.eq((await S.findAddRuns('DIESEL STD (31)', fetchLab)).error, 'No run 31 of DIESEL STD was found.');
    t.eq((await S.findAddRuns('99999', fetchLab)).error, 'No run with lab ID 99999 was found.');
    t.eq((await S.findAddRuns('99999 (2)', fetchLab)).error, 'No run with lab ID 99999 (2) was found.');
    t.eq(asked, ['DIESEL STD (31)', 'DIESEL STD', '99999', '99999 (2)', '99999']);

    // the picker's options
    t.eq(S.runChoiceLabel(r30), 'DIESEL STD (30) · GC-1 · Oct 3 10:15 · Final');
    t.eq(S.runChoiceLabel(b30), 'DIESEL STD (30) · GC-2 · Oct 4 08:05 · Held');
    t.eq(S.runChoiceLabel({ lab_id: 'X', instrument: 'gc3', injection_dt: '', status: 'error' }), 'X · gc3 · Error');

    // the GC the standard is tagged with as it is added
    t.eq(S.addGcOptions([{ id: 'gc1', name: 'GC-1' }, { id: 'gc2' }]),
        [{ value: '', text: 'GC of the run' }, { value: 'gc1', text: 'GC-1' }, { value: 'gc2', text: 'gc2' },
         { value: S.GC_NONE, text: 'Not tagged' }]);
    t.eq(S.addGcOptions(null).map((o) => o.value), [S.GC_OF_RUN, S.GC_NONE]);
    t.eq(S.addStandardBody(31, 'Diesel ULSD Std', S.GC_OF_RUN), { sample_id: 31, name: 'Diesel ULSD Std' });
    t.eq(S.addStandardBody(31, 'D', 'gc2'), { sample_id: 31, name: 'D', instrument_id: 'gc2' });
    t.eq(S.addStandardBody(31, 'D', S.GC_NONE), { sample_id: 31, name: 'D', instrument_id: null });

    // the line after it is added names the run used
    t.eq(S.addedText('Diesel ULSD Std', r30, { instrument_id: 'gc1', instrument_name: 'GC-1' }),
        'Added Diesel ULSD Std from DIESEL STD (30) on GC-1.');
    t.eq(S.addedText('D', r30, { instrument_id: 'gc2', instrument_name: 'GC-2' }),
        'Added D from DIESEL STD (30) on GC-1, tagged GC-2.');
    t.eq(S.addedText('D', r30, { instrument_id: null }), 'Added D from DIESEL STD (30) on GC-1, not tagged with a GC.');

    // ── QBench ───────────────────────────────────────────────────────────
    t.eq(S.webLoginText({ username: 'lab@asap', has_password: true }), 'Uploads sign in to QBench as lab@asap.');
    t.eq(S.webLoginText({ username: '', has_password: false }),
        'No QBench web login is saved. The upload asks for one the first time.');
    t.eq(S.webLoginText(null), 'No QBench web login is saved. The upload asks for one the first time.');
};
