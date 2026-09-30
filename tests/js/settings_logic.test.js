// settings_logic.js (v5.0 lane R): /settings groups what the classic Settings
// modal mixed together (analyst preferences, admin tuning, server facts) and
// saves only what changed, split by who may change it (/api/settings:
// OPERATOR_KEYS freely, ADMIN_KEYS with the admin password, anything else 400).
const S = require('../../static/js/settings_logic.js');

module.exports = (t) => {
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

    // ── QBench ───────────────────────────────────────────────────────────
    t.eq(S.webLoginText({ username: 'lab@asap', has_password: true }), 'Uploads sign in to QBench as lab@asap.');
    t.eq(S.webLoginText({ username: '', has_password: false }),
        'No QBench web login is saved. The upload asks for one the first time.');
    t.eq(S.webLoginText(null), 'No QBench web login is saved. The upload asks for one the first time.');
};
