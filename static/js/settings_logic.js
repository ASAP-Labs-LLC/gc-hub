/* settings_logic.js (v5.0 lane R): the /settings page's pure logic —
   window.GCSettings in the browser, module.exports for node
   (tests/js/settings_logic.test.js).

   The classic Settings modal mixed three audiences in about 30 fields. Here
   they are sections: this browser (theme, the D86 default), sample flags
   (anyone), admin tuning behind the one unlock (best fit, findings, Compare
   defaults, comparison standards, the LEM address), QBench, and the server's
   paths (read-only, collapsed). A save sends only the changed keys, split as
   /api/settings splits them: OPERATOR_KEYS freely, ADMIN_KEYS with the admin
   password; any other key is never posted (the server would answer 400).
   OPERATOR_KEYS / ADMIN_KEYS mirror settings.py (tests/test_settings_page.py
   checks they are equal).

   v8.1: a comparison standard is added from a run's lab ID or from the
   Samples list's re-run label ("DIESEL STD (30)": findAddRuns tries the
   whole text first, a lab ID may hold "(2)"), with a pick when several runs
   fit, and tagged with a GC as it is added (the run's own by default). */
(function (root) {
    'use strict';
    const req = (typeof require === 'function') ? require : null;
    const FR = (root && root.effectiveFlagRules) ? root : (req ? req('./flagrules.js') : null);

    const OPERATOR_KEYS = ['sample_flag_rules', 'early_signal_enabled', 'early_signal_time_min',
        'early_signal_intensity_threshold', 'series_colors'];
    const ADMIN_KEYS = ['bestfit_enabled', 'bestfit_threshold', 'bestfit_shift_tolerance_min',
        'bestfit_mix_min_frac', 'analysis_quantile', 'analysis_window', 'analysis_sigma',
        'analysis_thresh_marginal', 'analysis_thresh_moderate', 'analysis_thresh_significant',
        'analysis_gas_c_start', 'analysis_gas_c_end', 'analysis_oil_c_start', 'analysis_oil_c_end',
        'analysis_x_max_min', 'analysis_spike_min_width_min', 'analysis_range_overlays',
        'analysis_min_width_min', 'analysis_merge_gap_min', 'analysis_spike_report_threshold',
        'analysis_spike_max_fwhm_min', 'analysis_spike_min_dominance', 'lem_url'];

    const SECTIONS = [
        { id: 'browser', title: 'This browser', audience: 'you',
          intro: 'Just for this computer: how pages look and which D86 numbers they show first.' },
        { id: 'flags', title: 'Sample flags', audience: 'operator',
          intro: '"Above" flags a run when any point in the window passes the threshold; "Below" when the ' +
                 'signal stays under it the whole window. Flags show as text in every list.' },
        { id: 'bestfit', title: 'Best fit', audience: 'admin',
          intro: 'Each run is compared with its GC\'s standards. At or above the threshold it names the ' +
                 'fuel; otherwise it tries a two-part mix. The name is written with every result.' },
        { id: 'findings', title: 'Findings', audience: 'admin',
          intro: 'How deviations from the standard become findings on Compare and in reports.' },
        { id: 'compare', title: 'Compare defaults', audience: 'admin',
          intro: 'What Compare and new reports start with. Adjust changes them for one sample; these are ' +
                 'the saved defaults.' },
        { id: 'standards', title: 'Comparison standards', audience: 'admin',
          intro: 'The picker offers a run\'s own GC\'s standards first. Add one from a sample.' },
        { id: 'qbench', title: 'QBench', audience: 'operator',
          intro: 'The API key finds samples in QBench; the web login attaches report PDFs.' },
        { id: 'lem', title: 'LEM address', audience: 'admin',
          intro: 'Where the hub reads LEM\'s machine list for the Instruments pages. Read-only on LEM\'s side.' },
        { id: 'server', title: 'Server', audience: 'readonly',
          intro: 'Where the hub keeps things on ASAPSV1. Set on the server; shown here for reference.' },
    ];

    const FIELDS = [
        { key: 'bestfit_enabled', section: 'bestfit', kind: 'bool', label: 'Best fit on', def: 'true' },
        { key: 'bestfit_threshold', section: 'bestfit', kind: 'number', label: 'Match threshold', unit: '0 to 1', min: 0, max: 1, def: '0.93' },
        { key: 'bestfit_shift_tolerance_min', section: 'bestfit', kind: 'number', label: 'Retention shift', unit: 'min', min: 0, def: '0.05' },
        { key: 'bestfit_mix_min_frac', section: 'bestfit', kind: 'number', label: 'Mix minimum', unit: 'fraction, 0 to 1', min: 0, max: 1, def: '0.10' },

        { key: 'analysis_min_width_min', section: 'findings', kind: 'number', label: 'Minimum run width', unit: 'min', min: 0, def: '0.05' },
        { key: 'analysis_merge_gap_min', section: 'findings', kind: 'number', label: 'Merge gap outside ranges', unit: 'min', min: 0, def: '0.10' },
        { key: 'analysis_spike_min_width_min', section: 'findings', kind: 'number', label: 'Spike minimum width', unit: 'min', min: 0, def: '0.02' },
        { key: 'analysis_spike_max_fwhm_min', section: 'findings', kind: 'number', label: 'Spike max width at half height', unit: 'min', min: 0, def: '0.20' },
        { key: 'analysis_spike_min_dominance', section: 'findings', kind: 'number', label: 'Spike minimum dominance', unit: '0 to 1', min: 0, max: 1, def: '0.6' },
        { key: 'analysis_spike_report_threshold', section: 'findings', kind: 'number', label: 'Spike report threshold', unit: 'empty = the moderate threshold', min: 0, optional: true, def: '' },

        { key: 'analysis_quantile', section: 'compare', kind: 'number', label: 'Trend quantile', unit: '0 to 1', min: 0, max: 1, def: '0.20' },
        { key: 'analysis_window', section: 'compare', kind: 'number', label: 'Trend window', unit: 'points', min: 1, integer: true, def: '301' },
        { key: 'analysis_sigma', section: 'compare', kind: 'number', label: 'Smoothing', unit: 'sigma', min: 0, def: '34.0' },
        { key: 'analysis_thresh_marginal', section: 'compare', kind: 'number', label: 'Marginal threshold', unit: 'pA', min: 0, def: '100' },
        { key: 'analysis_thresh_moderate', section: 'compare', kind: 'number', label: 'Moderate threshold', unit: 'pA', min: 0, def: '500' },
        { key: 'analysis_thresh_significant', section: 'compare', kind: 'number', label: 'Significant threshold', unit: 'pA', min: 0, def: '2000' },
        { key: 'analysis_x_max_min', section: 'compare', kind: 'number', label: 'Chart ends at', unit: 'min', min: 0, def: '7.0' },

        { key: 'lem_url', section: 'lem', kind: 'url', label: 'LEM address', unit: 'http(s)://host[:port]', def: 'https://lem.asaplabs.net' },
    ];

    function section(id) { return SECTIONS.find((s) => s.id === id) || null; }
    function field(key) { return FIELDS.find((f) => f.key === key) || null; }
    function fieldsFor(id) { return FIELDS.filter((f) => f.section === id); }
    function audienceOf(key) {
        if (OPERATOR_KEYS.includes(key)) return 'operator';
        if (ADMIN_KEYS.includes(key)) return 'admin';
        return null;
    }

    const URL_RE = /^https?:\/\/(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:\d{1,5})?\/?$/;
    function validateField(f, raw) {
        const v = String(raw === null || raw === undefined ? '' : raw).trim();
        if (!f) return null;
        if (f.kind === 'bool') return v === 'true' || v === 'false' ? null : 'On or off.';
        if (f.kind === 'url') return URL_RE.test(v) ? null : 'http(s)://host or http(s)://host:port, no path.';
        if (v === '') return f.optional ? null : 'Required.';
        const n = Number(v);
        if (!Number.isFinite(n)) return 'A number, please.';
        if (f.integer && !Number.isInteger(n)) return 'A whole number, please.';
        if (f.max !== undefined && (n < f.min || n > f.max)) return 'Between ' + f.min + ' and ' + f.max + '.';
        if (f.min !== undefined && n < f.min) return f.min === 0 ? 'Zero or more.' : f.min + ' or more.';
        return null;
    }

    const RULES_PROBLEM = 'needs a name, above or below, a threshold and a window that ends after it starts.';
    /** Why a rule list can't be saved (the first bad row), or null. */
    function checkRules(rules) {
        for (let i = 0; i < (rules || []).length; i++) {
            const r = rules[i] || {};
            if (!String(r.name || '').trim() || !FR.cleanFlagRule(r)) return 'Rule ' + (i + 1) + ': ' + RULES_PROBLEM;
        }
        return null;
    }

    function current(settings, key) {
        const f = field(key);
        const v = settings ? settings[key] : undefined;
        if (v === undefined || v === null) return f ? f.def : '';
        return f && f.kind === 'bool' ? String(v).trim().toLowerCase() : String(v);
    }

    /** {operator, admin, errors}: the changed keys by who may change them.
        `edits` is {key: text}; `rules` (optional) the flag-rule editor's rows. */
    function planSave(settings, edits, rules) {
        const out = { operator: {}, admin: {}, errors: {} };
        for (const [key, raw] of Object.entries(edits || {})) {
            const value = String(raw === null || raw === undefined ? '' : raw).trim();
            const who = audienceOf(key);
            if (!who) { out.errors[key] = 'Set on the server, not here.'; continue; }
            if (value === current(settings, key)) continue;
            const problem = validateField(field(key), value);
            if (problem) { out.errors[key] = problem; continue; }
            out[who][key] = value;
        }
        if (Array.isArray(rules)) {
            const problem = checkRules(rules);
            if (problem) out.errors.sample_flag_rules = problem;
            else {
                const clean = rules.map(FR.cleanFlagRule);
                const inEffect = FR.effectiveFlagRules(settings || {});
                if (JSON.stringify(clean) !== JSON.stringify(inEffect)) out.operator.sample_flag_rules = JSON.stringify(clean);
            }
        }
        return out;
    }

    /** The JSON body for POST /api/settings, or null when there is nothing to
        send (or admin changes and no unlock: the page asks first). */
    function saveBody(plan, unlocked) {
        const hasAdmin = Object.keys(plan.admin).length > 0;
        if (hasAdmin && !unlocked) return null;
        const body = Object.assign({}, plan.operator, hasAdmin ? plan.admin : {});
        return Object.keys(body).length ? body : null;
    }

    // v1's watch folder, processed-CDF folder and results CSV are not used by
    // the hub; the calibration is per instrument (Instruments pages).
    const SERVER_PATHS = [
        { key: 'export_folder', label: 'Report PDFs' },
        { key: 'comparison_defaults_dir', label: 'Comparison standards' },
        { key: 'analysis_report_logo', label: 'Report logo', empty: 'None: reports use the built-in header' },
        { key: 'correction_factors_json', label: 'First correction factors (GC-1, seed only)' },
    ];
    function serverPaths(settings) {
        return SERVER_PATHS.map((p) => {
            const v = settings && settings[p.key] ? String(settings[p.key]) : '';
            if (!v && !p.empty) return null;
            return { key: p.key, label: p.label, value: v || p.empty };
        }).filter(Boolean);
    }

    const BAD_NAME = /[\\/:*?"<>|\x00-\x1f]/;
    function standardNameProblem(name) {
        const n = String(name || '').trim();
        if (!n) return 'A name, please.';
        if (BAD_NAME.test(n) || n.startsWith('.')) return 'No slashes, no dot at the start, none of : * ? " < > |.';
        if (n.length > 120) return 'At most 120 characters.';
        return null;
    }
    function standardTagText(s) {
        if (!s || !s.instrument_id) return 'Not tagged';
        const name = s.instrument_name || s.instrument_id;
        return s.missing ? name + ' · file missing' : name;
    }

    // ── adding a standard from a run (v8.1) ─────────────────────────────
    const RUN_LABEL = /^(.*?)\s*\((\d{1,6})\)$/;
    /** "DIESEL STD (30)" → {lab: 'DIESEL STD', run: 30}: the Samples list's
        label of a lab ID's 30th run on its GC. Anything else → {lab: the
        text, run: null}. */
    function parseRunLabel(text) {
        const t = String(text || '').trim();
        const m = RUN_LABEL.exec(t);
        const run = m ? Number(m[2]) : 0;
        if (!m || !m[1].trim() || run < 1) return { lab: t, run: null };
        return { lab: m[1].trim(), run };
    }

    /** What Add does with a lab ID's runs (a GET /api/lab body) and the run
        number a "(N)" label named (null: none): {runs, chosen, selected,
        prompt, error}. One run fits → chosen (added at once); several (a
        lab ID with re-runs, or "(N)" on two GCs) → the page asks which, with
        `selected` (a sample_id) preselected: the resolver's own run when it
        fits, else the first; none → error. */
    function addStandardRuns(resolved, run) {
        const all = resolved && Array.isArray(resolved.runs) ? resolved.runs : [];
        const lab = (resolved && resolved.lab_id) || '';
        const runs = run ? all.filter((r) => r.run_no === run) : all;
        if (!runs.length) {
            return { runs: [], chosen: null, selected: null, prompt: null,
                     error: run ? 'No run ' + run + ' of ' + lab + ' was found.' : 'No run with lab ID ' + lab + ' was found.' };
        }
        const own = runs.some((r) => r.sample_id === resolved.sample_id);
        const several = runs.length > 1;
        return {
            runs, chosen: several ? null : runs[0], selected: own ? resolved.sample_id : runs[0].sample_id,
            prompt: !several ? null : run ? lab + ' (' + run + ') is on ' + runs.length + ' GCs. Pick the one to add.'
                : lab + ' has ' + runs.length + ' runs. Pick the one to add.',
            error: null,
        };
    }

    /** The plan for the text typed in "Lab ID of the run" (addStandardRuns'
        shape). `fetchLab(id)` resolves a lab ID (the GET /api/lab body, or
        null). The whole text first, since a lab ID may itself end in "(2)";
        then, for a "(N)" label, its lab ID's run N. */
    async function findAddRuns(text, fetchLab) {
        const t = String(text || '').trim();
        const whole = await fetchLab(t);
        if (whole) return addStandardRuns(whole, null);
        const label = parseRunLabel(t);
        const base = label.run ? await fetchLab(label.lab) : null;
        if (base) return addStandardRuns(base, label.run);
        return { runs: [], chosen: null, selected: null, prompt: null, error: 'No run with lab ID ' + t + ' was found.' };
    }

    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    function runWhen(dt) {
        const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(dt || ''));
        return m ? (MONTHS[Number(m[2]) - 1] || m[2]) + ' ' + Number(m[3]) + ' ' + m[4] + ':' + m[5] : '';
    }
    function runStatusText(status) {
        if (status === 'final') return 'Final';
        if (status === 'error') return 'Error';
        if (status === 'received') return 'Processing';
        return 'Held';
    }
    /** A run in the picker: "DIESEL STD (30) · GC-1 · Oct 3 10:15 · Final". */
    function runChoiceLabel(r) {
        return [r.display_name || r.lab_id, r.instrument_name || r.instrument, runWhen(r.injection_dt),
            runStatusText(r.status)].filter(Boolean).join(' · ');
    }

    // The add form's GC choice: '' = the run's own GC (instrument_id left
    // out: the server reads it from the run), GC_NONE = untagged (null).
    // Never an instrument id (they start with a letter).
    const GC_OF_RUN = '';
    const GC_NONE = '-';
    function addGcOptions(instruments) {
        return [{ value: GC_OF_RUN, text: 'GC of the run' },
            ...(instruments || []).map((i) => ({ value: i.id, text: i.name || i.id })),
            { value: GC_NONE, text: 'Not tagged' }];
    }
    /** The POST /api/comparison-standard body (the page adds the password). */
    function addStandardBody(sampleId, name, gc) {
        const body = { sample_id: sampleId, name };
        if (gc === GC_NONE) body.instrument_id = null;
        else if (gc) body.instrument_id = gc;
        return body;
    }
    /** The line once added: the run used, and its tag when not the run's GC. */
    function addedText(name, run, standard) {
        const line = 'Added ' + name + ' from ' + (run.display_name || run.lab_id) + ' on ' +
            (run.instrument_name || run.instrument);
        const tag = standard ? standard.instrument_id : run.instrument;
        if (!tag) return line + ', not tagged with a GC.';
        if (tag !== run.instrument) return line + ', tagged ' + ((standard && standard.instrument_name) || tag) + '.';
        return line + '.';
    }

    function webLoginText(c) {
        if (c && c.username) return 'Uploads sign in to QBench as ' + c.username + '.';
        return 'No QBench web login is saved. The upload asks for one the first time.';
    }

    const api = {
        OPERATOR_KEYS, ADMIN_KEYS, SECTIONS, FIELDS, SERVER_PATHS,
        section, field, fieldsFor, audienceOf, validateField, checkRules, planSave, saveBody,
        serverPaths, standardNameProblem, standardTagText, webLoginText,
        GC_OF_RUN, GC_NONE, parseRunLabel, addStandardRuns, findAddRuns, runChoiceLabel, addGcOptions,
        addStandardBody, addedText,
    };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (root) root.GCSettings = api;
})(typeof window !== 'undefined' ? window : null);
