// /instruments/<id> (v4.0): the setup checklist, then Agent, Calibration,
// Correction factors, Results file, Methods, Backfill and Conflicts. The same
// operations as the classic page, over the same routes (GET /api/instruments
// /<id>[/…], /api/conflicts, and the admin POSTs through GCShell.adminPost).
// Live: the page reloads when this instrument changes; the agent's status
// ticks every second; the checklist refreshes when the agent checks in.
// Every string from the server or an agent is set with textContent (M5).
(function () {
    'use strict';
    const U = window.GCUi;
    const L = window.InstrumentsLogic;
    const S = window.GCShell;
    const A = window.GCActions;
    const h = S.h;
    const $ = (id) => document.getElementById(id);
    const enc = encodeURIComponent;
    const txt = (v) => (v === null || v === undefined || v === '' ? '—' : String(v));
    const IID = document.getElementById('main').dataset.instrument;

    let D = null;                   // GET /api/instruments/<id>
    let SETUP = null;               // GET /api/instruments/<id>/setup
    let LIST = { hub_methods: [], agent_commands: [] };
    let LEM = null;
    let agent = null;               // the live agent row
    let finderOpen = false;

    const SECTION_FOR = { create: '#main', corrections: '#corrections', installer: '#agent', checkin: '#agent',
                          calibration: '#calibration', method: '#methods', go_live: '#export', first_result: '#export' };

    // ── load ────────────────────────────────────────────────────────────────
    // `background`: a reload caused by a live update (X-GC-Background: 1), not a click.
    let bgRender = false;
    let LOADED_AT = Date.now();
    async function load(background) {
        const bg = background === true;
        const [d, st] = await Promise.all([S.getJSON('/api/instruments/' + enc(IID), bg),
                                           S.getJSON('/api/instruments/' + enc(IID) + '/setup', bg)]);
        bgRender = bg;
        LOADED_AT = Date.now();
        if (d.status !== 200) { S.toast((d.body && d.body.error) || 'Could not load ' + IID, 'err'); return; }
        D = d.body;
        if (st.status === 200) SETUP = st.body;
        // v4.0 lane E: the hub's age was read now (read_at), so it ticks from here;
        // a newer live answer keeps its own last_seen, live flag and age
        const server = Object.assign({}, D.instrument.agent || {}, { read_at: Date.now() });
        const liveNewer = agent && agent.last_seen && (!server.last_seen || agent.last_seen > server.last_seen);
        agent = Object.assign({}, server, liveNewer ? { last_seen: agent.last_seen, live: agent.live,
            last_seen_age_s: agent.last_seen_age_s, read_at: agent.read_at } : {});
        render(bg);
    }

    async function loadSetup() {
        const st = await S.getJSON('/api/instruments/' + enc(IID) + '/setup', true);
        if (st.status === 200) { SETUP = st.body; renderHead(); renderChecklist(); }
    }

    function stepOf(key) {
        return SETUP ? SETUP.steps.find(s => s.key === key) : null;
    }
    function doneBy(key) {
        const s = stepOf(key);
        if (!s || !s.done_at) return null;
        return U.clockTime(s.done_at, Date.now()) + (s.done_by ? ' by ' + U.actorName(s.done_by) : '');
    }

    // ── head + checklist ────────────────────────────────────────────────────
    function renderHead() {
        const inst = D.instrument;
        document.title = inst.name + ' · GC Hub';
        $('inst-name').textContent = inst.name;
        $('crumb-name').textContent = inst.name;
        const st = U.agentStatus(agent, Date.now());
        const lemTitle = U.lemTitle(LEM, inst.lem_machine_uid);
        const sep = () => h('span', { className: 'sep', 'aria-hidden': 'true', text: '·' });
        $('inst-meta').replaceChildren(
            h('span', { className: 'pill ' + (st.glyph === 'never' ? '' : st.glyph), id: 'head-pill' }, S.glyph(st.glyph), h('span', { text: st.label })),
            h('span', { text: 'LEM machine ' + (lemTitle || 'not chosen') }), sep(),
            h('span', { text: inst.method }), sep(),
            h('span', { text: inst.enabled ? 'Enabled' : 'Disabled' }), sep(),
            h('span', { className: 'mono', text: inst.id }));
    }

    let checklistOpen = false;
    function renderChecklist() {
        if (!SETUP) return;
        const sm = SETUP.summary;
        const box = $('checklist');
        box.dataset.ready = sm.ready ? 'true' : 'false';
        $('checklist-title').textContent = sm.ready ? 'Setup complete' : 'Setup checklist';
        $('checklist-count').textContent = sm.ready ? 'Ready · all 8 steps done' : U.setupLabel(sm);
        const toggle = $('checklist-toggle');
        toggle.hidden = !sm.ready;
        toggle.textContent = checklistOpen ? 'Hide the steps' : 'Show the steps';
        toggle.setAttribute('aria-expanded', checklistOpen ? 'true' : 'false');
        const tiles = $('tiles');
        tiles.hidden = sm.ready && !checklistOpen;
        tiles.replaceChildren(...SETUP.steps.map(s => h('a', {
            className: 'tile ' + s.status, href: s.status === 'done' ? SECTION_FOR[s.key] : '/setup?instrument=' + enc(IID) + '#step-' + s.key,
            'data-testid': 'checklist-step', 'data-status': s.status, title: s.blocker || s.detail },
        h('span', { className: 'num', 'aria-hidden': 'true' }, s.status === 'done' ? S.icon('check') : String(s.n)),
        h('span', { className: 't', text: s.title }),
        h('span', { className: 's', text: U.stepBadge(s.status) }))));
    }

    // ── sections ────────────────────────────────────────────────────────────
    function kv(rows) {
        return h('div', { className: 'kv' }, ...rows.filter(Boolean).flatMap(([k, v, cls]) => [
            h('div', { className: 'k', text: k }),
            v instanceof Node ? h('div', { className: cls || '' }, v) : h('div', { className: cls || '', text: txt(v) })]));
    }

    // "Live · checked in 12 s ago"; the other labels already say when.
    function statusText(st) {
        return st.label === 'Live' && st.since ? st.label + ' · ' + st.since
            : st.glyph === 'never' ? 'Waiting for the first check-in' : st.label;
    }

    function agentSection() {
        const inst = D.instrument;
        const a = agent || {};
        const st = U.agentStatus(a, Date.now());
        const seen = !!a.last_seen;
        const skew = L.skewWarning(a.clock_skew_seconds);
        const installer = inst.has_token
            ? 'Downloaded' + (doneBy('installer') ? ' ' + doneBy('installer') : ' ' + U.clockTime(inst.token_issued_at, Date.now())) + ' · key issued'
            : 'No key issued yet';
        const cmds = (LIST.agent_commands || []).filter(c => c !== 'adopt-mirror');
        return [
            kv([
                ['Status', h('span', { className: 'row', id: 'agent-status' }, S.glyph(st.glyph), h('span', { text: statusText(st) }))],
                ['Installer', installer],
                ['Computer', seen ? txt(a.host) : '— reported on first check-in'],
                ['Agent version', seen ? txt(a.version) : '—'],
                ['Clock', seen ? L.formatSkew(a.clock_skew_seconds) : '— checked on first check-in'],
                seen ? ['State', txt(a.state || a.status)] : null,
                seen ? ['Queue', (a.queue_size || 0) + ' waiting · ' + (a.rejected_count || 0) + ' rejected'] : null,
                seen ? ['Results seq', txt(a.results_seq)] : null,
                ['Pending command', a.pending_command ? a.pending_command + ' (taken at the next check-in)' : 'none'],
                ['Hub address', h('span', { className: 'row' },
                    h('span', { className: 'mono', text: LIST.hub_url_effective || '—' }),
                    h('button', { type: 'button', className: 'btn btn-ghost btn-sm', text: 'Change…', 'data-testid': 'hub-url',
                                  onclick: async () => { if (await A.setHubUrl(LIST.hub_url, LIST.hub_url_effective)) S.loadInstruments(false); } }))],
                seen && a.last_file ? ['Last file', a.last_file, 'mono'] : null,
                seen && a.last_error ? ['Last error', a.last_error, 'errline'] : null,
            ]),
            skew ? h('p', { className: 'warnline', text: skew + ' Fix it before relying on live since.' }) : null,
            inst.enabled ? null : h('p', { className: 'warnline', text: 'This GC is disabled: its runs are refused. Enable it in Edit details.' }),
            h('div', { className: 'row' },
                h('button', { type: 'button', className: 'btn' + (inst.has_token ? '' : ' btn-primary'), 'data-testid': 'download-installer',
                              onclick: async () => { if (await A.downloadInstaller(inst)) load(); } },
                S.icon('download'), inst.has_token ? 'Download installer again' : 'Download installer'),
                h('button', { type: 'button', className: 'btn btn-ghost btn-danger', text: 'Revoke key', disabled: !inst.has_token,
                              onclick: async () => { if (await A.revokeKey(inst)) load(); } }),
                seen ? null : h('span', { className: 'caption end', text: 'Remote control appears once the agent has checked in' })),
            seen && cmds.length ? h('div', { className: 'row' }, h('span', { className: 'caption', text: 'Send to the agent:' }),
                ...cmds.map(c => h('button', { type: 'button', className: 'btn btn-sm', text: c, onclick: () => A.agentCommand(inst, c) }))) : null,
        ];
    }

    function calibrationSection() {
        const inst = D.instrument;
        const cal = inst.calibration || {};
        const file = cal.calibration_cdf ? cal.calibration_cdf.split(/[\\/]/).pop() : null;
        const pill = cal.usable
            ? h('span', { className: 'pill final' }, S.glyph('final'), h('span', { text: 'Usable · ' + (cal.assigned || 0) + ' peaks' }))
            : h('span', { className: 'pill held' }, S.glyph('held'), h('span', { text: 'Not usable' }));
        const saved = doneBy('calibration');
        const out = [
            h('div', { className: 'row' }, pill,
                h('span', { className: 'caption', text: [file, cal.sensitivity !== undefined ? 'sensitivity ' + cal.sensitivity : null, saved ? 'saved ' + saved : null].filter(Boolean).join(' · ') })),
            cal.usable ? null : h('p', { className: 'warnline', text: cal.problem || 'Choose the n-alkane run, then assign its peaks.' }),
            h('div', { className: 'row' },
                cal.calibration_cdf ? h('a', { className: 'btn', href: '/calibration?instrument=' + enc(inst.id), 'data-testid': 'assign-peaks', text: 'Assign peaks…' }) : null,
                h('button', { type: 'button', className: 'btn btn-ghost', 'aria-expanded': finderOpen ? 'true' : 'false',
                              text: cal.calibration_cdf ? 'Choose another calibration run' : 'Choose the calibration run',
                              onclick: () => { finderOpen = !finderOpen; renderSection('calibration'); } })),
        ];
        if (finderOpen) out.push(calibrationFinder(inst));
        // not ported yet: comparison standards are tagged by instrument on the classic page
        out.push(h('p', { className: 'caption' }, 'Comparison standards are tagged by instrument on the ',
            h('a', { className: 'link', href: '/instruments/classic?instrument=' + enc(inst.id), text: 'classic Instruments page' }), '.'));
        return out;
    }

    function calibrationFinder(inst) {
        const q = h('input', { type: 'text', placeholder: 'Lab ID contains…', 'aria-label': 'Lab ID contains' });
        const tbody = h('tbody');
        const path = h('input', { type: 'text', className: 'grow', placeholder: 'or an absolute path to a CDF on the server', 'aria-label': 'CDF path' });
        async function search() {
            const r = await S.getJSON('/api/instruments/' + enc(inst.id) + '/calibration-candidates?q=' + enc(q.value));
            tbody.replaceChildren();
            if (r.status !== 200) { S.toast((r.body && r.body.error) || 'Search failed', 'err'); return; }
            for (const c of r.body.candidates) {
                const st = U.sampleStatus(c.status);
                tbody.appendChild(h('tr', {}, h('td', { text: c.lab_id }), h('td', { text: c.injection_dt }),
                    h('td', { className: 'mono', text: c.method_name || '—' }),
                    h('td', {}, h('span', { className: 'row' }, S.glyph(st.glyph), h('span', { text: st.text }))),
                    h('td', {}, h('button', { type: 'button', className: 'btn btn-sm', text: 'Use', onclick: () => use({ sample_id: c.sample_id }) }))));
            }
            if (!r.body.candidates.length) tbody.appendChild(h('tr', {}, h('td', { colspan: 5, className: 'muted', text: 'No runs from this GC yet.' })));
        }
        async function use(payload) {
            if (!window.confirm('Use this run as ' + inst.name + "'s calibration? Its saved peak assignments are cleared; assign the peaks next.")) return;
            const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/calibration-cdf', payload);
            if (r && r.status === 200) { S.toast('Calibration run set. Now assign its peaks.'); finderOpen = false; load(); }
        }
        search();
        return h('div', { className: 'card', style: 'padding:16px;display:grid;gap:12px' },
            h('div', { className: 'row' }, q, h('button', { type: 'button', className: 'btn btn-sm', text: 'Search', onclick: search })),
            h('div', { className: 'tablewrap' }, h('table', { className: 'tbl' },
                h('thead', {}, h('tr', {}, ...['Lab ID', 'Injected', 'Method', 'Status', ''].map(t => h('th', { text: t })))), tbody)),
            h('div', { className: 'row' }, path, h('button', { type: 'button', className: 'btn btn-sm', text: 'Use path', onclick: () => use({ path: path.value }) })));
    }

    function correctionsSection() {
        const inst = D.instrument;
        const c = D.corrections;
        const inputs = {};
        const grid = h('div', { className: 'cuts' });
        for (const cut of c.cuts) {
            inputs[cut] = h('input', { type: 'text', inputmode: 'decimal', 'aria-label': cut + ' correction (°C)',
                value: c.values && c.values[cut] !== undefined ? String(c.values[cut]) : '' });
            grid.appendChild(h('label', { className: 'field' }, h('span', { text: cut }), inputs[cut]));
        }
        const reason = h('input', { type: 'text', className: 'grow', placeholder: 'Reason for the change (kept in the history)', maxlength: 500, 'aria-label': 'Reason' });
        const errs = h('div');
        async function save() {
            errs.replaceChildren();
            const raw = {};
            for (const cut of c.cuts) raw[cut] = inputs[cut].value;
            const parsed = L.parseCorrections(raw, c.cuts, c.max_abs);
            if (parsed.errors.length) { parsed.errors.forEach(e => errs.appendChild(h('p', { className: 'errline', text: e }))); return; }
            if (!reason.value.trim()) { errs.appendChild(h('p', { className: 'errline', text: 'A reason is required.' })); return; }
            const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/corrections', { values: parsed.values, reason: reason.value });
            if (!r) return;
            if (r.status === 200) { S.toast(r.body.changed + ' value(s) changed; ' + r.body.queued + ' waiting run(s) queued.'); load(); }
            else if (r.body && r.body.errors) r.body.errors.forEach(e => errs.appendChild(h('p', { className: 'errline', text: e })));
        }
        async function seed() {
            if (!window.confirm("Seed GC-1's correction factors from the phase-1 file? This is done once.")) return;
            const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/corrections/seed', {});
            if (r && r.status === 200) { S.toast('Seeded from correction_factors.json.'); load(); }
        }
        const last = c.source === 'hub' ? 'Last changed ' + U.clockTime(c.updated_at, Date.now()) + ' by ' + (U.actorName(c.updated_by) || '—')
            : c.source === 'file' ? 'Read from the phase-1 file until seeded'
            : 'Not set: this GC\'s runs wait (pending corrections) until all 11 are saved.';
        const audit = c.audit || [];
        return [
            grid,
            h('div', { className: 'row' }, reason, h('button', { type: 'button', className: 'btn btn-primary', text: 'Save', 'data-testid': 'save-corrections', onclick: save }),
                c.can_seed ? h('button', { type: 'button', className: 'btn', text: 'Seed from correction_factors.json', onclick: seed }) : null),
            errs,
            c.file_error ? h('p', { className: 'errline', text: 'Phase-1 file: ' + c.file_error }) : null,
            h('p', { className: 'caption', text: last }),
            audit.length ? h('details', { className: 'more' }, h('summary', { text: 'History (' + audit.length + ')' }),
                h('div', { className: 'tablewrap' }, h('table', { className: 'tbl' },
                    h('thead', {}, h('tr', {}, ...['When', 'Cut', 'Old', 'New', 'By', 'Reason'].map(t => h('th', { text: t })))),
                    h('tbody', {}, ...audit.map(a => h('tr', {}, h('td', { text: U.clockTime(a.changed_at, Date.now()) }), h('td', { text: a.cut }),
                        h('td', { text: txt(a.old_value) }), h('td', { text: txt(a.new_value) }), h('td', { text: U.actorName(a.changed_by) || '—' }),
                        h('td', { text: a.reason }))))))) : null,
        ];
    }

    function clock() {
        return { hub_local_now: D.hub_local_now, loaded_at: LOADED_AT,
                 skew_seconds: (agent || {}).clock_skew_seconds,
                 results_file_configured: !!(D.export || {}).configured };
    }

    function exportSection() {
        const inst = D.instrument;
        const ex = D.export || {};
        const step = stepOf('go_live') || {};
        const hubOnly = !ex.configured && !step.needs_results_file;
        const path = h('input', { type: 'text', className: 'grow mono', value: ex.configured ? ex.path : '',
                                  placeholder: 'The file LEM reads, e.g. \\\\asapserver\\Labsharedrive\\…\\results.csv',
                                  'aria-label': 'Results file path', 'data-testid': 'results-path' });
        const state = ex.refused ? 'Refused (' + ex.refused + '): ' + txt(ex.refused_detail) : ex.last_error || ex.error ? txt(ex.last_error || ex.error) : 'OK';
        const ls = U.liveSinceState(inst.live_since, D.hub_local_now);
        const liveText = inst.live_since ? ls.text + " (the GC's clock)"
            : 'Not live: every run is backfill and is never written to a results file unless released';
        async function adopt() {
            if (!window.confirm('Adopt ' + txt(ex.path) + ' as it is now? The hub appends after its current content.')) return;
            const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/export-adopt', {});
            if (r && r.status === 200) { S.toast('Adopted.'); load(); }
        }
        const fileText = ex.configured ? ex.path + ' (LEM reads it)'
            : ex.path + (hubOnly ? " (the hub's own file, kept on purpose: LEM won't see these results)"
                                 : " (the hub's own file: LEM does not read it)");
        const out = [
            kv([
                ['File', fileText, 'mono'],
                ['Waiting to write', (ex.pending || 0) + ' row(s)'],
                ['State', state, ex.refused ? 'errline' : ''],
                ['Live since', liveText],
                ['Backfill', (inst.backfill_unreleased || 0) + ' run(s) held back'],
            ]),
            // 1. the results file
            h('h3', { className: 'caption', text: '1 · The results file' }),
            ex.configured ? null : h('p', { className: 'warnline', text:
                "Rows written to the hub's own file never reach LEM, even if you choose LEM's file later: choose it before going live." }),
            h('div', { className: 'row' }, path,
                h('button', { type: 'button', className: 'btn' + (step.needs_results_file ? ' btn-primary' : ''), text: 'Set results file',
                              'data-testid': 'set-results-file',
                              onclick: async () => { if (await A.setResultsFile(inst, path.value)) { clean('export'); load(); } } }),
                ex.configured || hubOnly ? null : h('button', { type: 'button', className: 'btn btn-ghost', 'data-testid': 'keep-hub-only',
                    text: "Keep the hub-only file (LEM won't see these results)",
                    onclick: async () => { if (await A.keepHubOnly(inst, ex.path)) load(); } }),
                ex.configured ? h('button', { type: 'button', className: 'btn btn-ghost', text: 'Adopt the file as it is', onclick: adopt }) : null),
            // 2. go live
            h('h3', { className: 'caption', text: '2 · Go live' }),
            ...(inst.live_since ? (D.live_since_warnings || []) : []).map(w => h('p', { className: 'warnline', text: w })),
        ];
        if (!inst.live_since && step.needs_results_file) {
            out.push(h('p', { className: 'caption', text: 'Choose the results file first (step 1 above).' }));
        } else {
            out.push(h('div', { className: 'row' },
                inst.live_since ? null : h('button', { type: 'button', className: 'btn btn-primary', 'data-testid': 'go-live', text: 'Go live now',
                                                       onclick: async () => { if (await A.goLiveNow(inst, clock())) load(); } }),
                h('button', { type: 'button', className: 'btn btn-ghost', text: inst.live_since ? 'Change live since…' : 'Choose a time…',
                              onclick: () => A.editDetails(inst, LIST.hub_methods || [], load) })));
        }
        return out;
    }

    function methodsSection() {
        const inst = D.instrument;
        const m = D.methods;
        const rows = L.methodRows(m.seen);
        async function markOther() {
            if (!window.confirm('Mark every run with no method name as "another method" (stored, never processed)?')) return;
            const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/review-method', {});
            if (r && r.status === 200) { S.toast(r.body.marked + ' run(s) marked as another method.'); load(); }
        }
        function mapControl(r) {
            const pick = h('select', { 'aria-label': 'Hub method for ' + r.label }, ...(LIST.hub_methods || [inst.method]).map(x => h('option', { value: x, text: x })));
            pick.value = inst.method;
            return h('span', { className: 'row' }, pick, h('button', { type: 'button', className: 'btn btn-sm', text: 'Map',
                onclick: async () => { if (await A.mapMethod(inst, r.name, pick.value)) load(); } }));
        }
        const body = rows.length ? rows.map(r => h('tr', {},
            h('td', { className: 'mono', text: r.label }), h('td', { text: r.count }),
            h('td', { text: r.last_seen ? String(r.last_seen).slice(0, 16) : '—' }),
            h('td', {}, r.mapped_to ? h('span', { className: 'row' }, S.glyph('final'), h('span', { text: r.mapped_to }))
                : h('span', { className: 'row' }, S.glyph('held'), h('span', { text: r.action === 'review' ? 'Held for review' : 'Not processed' }))),
            h('td', {}, r.action === 'map' ? mapControl(r)
                : r.action === 'unmap' ? h('button', { type: 'button', className: 'btn btn-ghost btn-sm', text: 'Unmap',
                    onclick: async () => { if (await A.mapMethod(inst, r.name, null)) load(); } })
                : (m.review_count ? h('button', { type: 'button', className: 'btn btn-sm', text: 'Mark ' + m.review_count + ' as another method', onclick: markOther }) : null))))
            : [h('tr', {}, h('td', { colspan: 5, className: 'muted', text: 'No runs yet.' }))];
        return [h('div', { className: 'tablewrap' }, h('table', { className: 'tbl' },
            h('thead', {}, h('tr', {}, ...['Method', 'Runs', 'Last run', 'Processed as', ''].map(t => h('th', { text: t })))),
            h('tbody', {}, ...body)))];
    }

    // ── backfill (v4.0.1: select many) ──────────────────────────────────────
    // The selection is kept by sample id, outside the DOM, so the list can be
    // redrawn by live updates without losing it; rows that are released or
    // change drop out (GCBackfill.prune). A live reload is held back while a
    // drag or a release is under way and runs when it ends. Select all, a
    // shift-click range, a drag down the checkboxes or the rows (pointer
    // events: mouse and touch; on touch, the checkbox column), Space and
    // Shift+Space. Releases go GCBackfill.RELEASE_MAX ids per call.
    const BF = window.GCBackfill;
    const bf = { selected: new Set(), order: [], selectable: new Set(), rows: new Map(), anchor: null,
                 gesture: null, keyed: null, releasing: false, pending: false, seq: 0,
                 q: '', released: 'false', info: null };
    let backfillEls = null;
    const bfHold = () => !!(bf.gesture || bf.releasing);

    function bfSync() {
        const e = backfillEls;
        if (!e) return;
        for (const [id, r] of bf.rows) {
            const on = bf.selected.has(id);
            r.box.checked = on;
            r.tr.classList.toggle('is-sel', on);
        }
        const state = BF.headerState(bf.selected, bf.selectable);
        e.all.checked = state === 'all';
        e.all.indeterminate = state === 'some';
        e.all.disabled = !bf.selectable.size || bf.releasing;
        const n = bf.selected.size;
        const t = BF.barText(n);
        e.count.textContent = t.count;
        e.bar.dataset.some = n ? 'true' : 'false';
        e.release.textContent = t.action;
        e.release.disabled = !n || bf.releasing;
    }

    function bfProgress(done, total) {
        const e = backfillEls;
        if (!e) return;
        e.progress.hidden = done === null;
        if (done === null) return;
        e.progressText.textContent = BF.progressText(done, total);
        e.progressFill.style.width = (total ? Math.round(100 * done / total) : 0) + '%';
    }

    const rowOf = (el) => (el && el.closest ? el.closest('#backfill-body tr[data-id]') : null);

    function bfPointerDown(ev) {
        if (bf.releasing || bf.gesture || (ev.pointerType === 'mouse' && ev.button !== 0)) return;
        const tr = rowOf(ev.target);
        if (!tr) return;
        const id = Number(tr.dataset.id);
        if (!bf.selectable.has(id)) return;
        const r = bf.rows.get(id);
        if (ev.pointerType === 'mouse') {
            ev.preventDefault();                 // no text selection while dragging
            r.box.focus({ preventScroll: true });
        }
        const state = !bf.selected.has(id);      // the first row's new state
        if (ev.shiftKey && bf.anchor !== null) {
            bf.selected = BF.setRange(bf.selected, bf.order, bf.anchor, id, state, bf.selectable);
            bf.anchor = id;
            bfSync();
            return;
        }
        bf.gesture = { pointerId: ev.pointerId, start: id, over: id, state, base: new Set(bf.selected) };
        bf.selected = BF.setRange(bf.gesture.base, bf.order, id, id, state, bf.selectable);
        backfillEls.table.classList.add('dragging');
        document.addEventListener('pointermove', bfPointerMove);
        document.addEventListener('pointerup', bfPointerEnd);
        document.addEventListener('pointercancel', bfPointerEnd);
        bfSync();
    }
    function bfPointerMove(ev) {
        const g = bf.gesture;
        if (!g || ev.pointerId !== g.pointerId) return;
        const tr = rowOf(document.elementFromPoint(ev.clientX, ev.clientY));
        if (!tr) return;
        const id = Number(tr.dataset.id);
        if (id === g.over || !bf.rows.has(id)) return;
        g.over = id;
        bf.selected = BF.setRange(g.base, bf.order, g.start, id, g.state, bf.selectable);
        bfSync();
    }
    function bfPointerEnd(ev) {
        const g = bf.gesture;
        if (!g || ev.pointerId !== g.pointerId) return;
        // a touch that turned into a scroll is not a selection
        if (ev.type === 'pointercancel') bf.selected = g.base;
        else bf.anchor = g.over;
        bf.gesture = null;
        document.removeEventListener('pointermove', bfPointerMove);
        document.removeEventListener('pointerup', bfPointerEnd);
        document.removeEventListener('pointercancel', bfPointerEnd);
        if (backfillEls) backfillEls.table.classList.remove('dragging');
        bfSync();
        bfRelease();
    }
    // A click from the mouse or a finger (detail > 0) was handled on
    // pointerdown: keep the box as the selection says. A click with no pointer
    // (a script, or Space where the key handler didn't run) toggles.
    function bfClick(ev) {
        const box = ev.target;
        if (!box.dataset || !box.dataset.sel) return;
        const id = Number(box.dataset.id);
        if (ev.detail === 0 && bf.keyed !== id && bf.selectable.has(id) && !bf.releasing) {
            bf.selected = BF.setRange(bf.selected, bf.order, ev.shiftKey && bf.anchor !== null ? bf.anchor : id, id,
                                      !bf.selected.has(id), bf.selectable);
            bf.anchor = id;
        }
        bfSync();
    }
    function bfKey(ev) {
        const box = ev.target;
        if (ev.key !== ' ' || !box.dataset || !box.dataset.sel) return;
        ev.preventDefault();
        if (ev.type === 'keyup') { setTimeout(() => { bf.keyed = null; }, 0); return; }
        const id = Number(box.dataset.id);
        if (ev.repeat || bf.releasing || !bf.selectable.has(id)) return;
        bf.keyed = id;
        bf.selected = BF.setRange(bf.selected, bf.order, ev.shiftKey && bf.anchor !== null ? bf.anchor : id, id,
                                  !bf.selected.has(id), bf.selectable);
        bf.anchor = id;
        bfSync();
    }

    function backfillSection() {
        const inst = D.instrument;
        const q = h('input', { type: 'text', placeholder: 'Lab ID contains…', 'aria-label': 'Lab ID contains', value: bf.q });
        const released = h('select', { 'aria-label': 'Released' }, h('option', { value: 'false', text: 'Not released' }),
            h('option', { value: 'true', text: 'Released' }), h('option', { value: '', text: 'All' }));
        released.value = bf.released;
        const filter = () => { bf.q = q.value; bf.released = released.value; loadBackfill(); };
        q.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') filter(); });
        const tbody = h('tbody');
        const total = h('span', { className: 'caption' });
        const all = h('input', { type: 'checkbox', 'aria-label': 'Select all shown', 'data-testid': 'bf-all',
                                 onclick: () => { bf.selected = BF.toggleAll(bf.selected, bf.selectable); bfSync(); } });
        const count = h('span', { className: 'count', 'data-testid': 'bf-count', 'aria-live': 'polite' });
        const release = h('button', { type: 'button', className: 'btn btn-primary btn-sm', 'data-testid': 'bf-release',
                                      onclick: releaseSelected });
        const progressText = h('span', { className: 'caption' });
        const progressFill = h('span');
        const progress = h('span', { className: 'bf-progress', 'data-testid': 'bf-progress', role: 'status', hidden: true },
            h('span', { className: 'progress', 'aria-hidden': 'true' }, progressFill), progressText);
        const bar = h('div', { className: 'bf-bar', 'data-testid': 'bf-bar' }, count, progress, h('span', { className: 'spacer' }), release);
        const table = h('table', { className: 'tbl bf' },
            h('thead', {}, h('tr', {}, h('th', { className: 'sel' }, all),
                ...['Lab ID', 'Status', 'Released'].map(t => h('th', { text: t })))), tbody);
        tbody.addEventListener('pointerdown', bfPointerDown);
        tbody.addEventListener('click', bfClick);
        tbody.addEventListener('keydown', bfKey);
        tbody.addEventListener('keyup', bfKey);
        backfillEls = { inst, q, released, tbody, total, all, count, release, bar, table, progress, progressText, progressFill };

        async function releaseSelected() {
            const ids = bf.order.filter(id => bf.selected.has(id));
            if (!ids.length || bf.releasing) return;
            if (!window.confirm('Release ' + ids.length.toLocaleString('en-US') + ' run(s)? Each is written to ' +
                                inst.name + "'s results file (which LEM reads). This can't be undone.")) return;
            bf.releasing = true;
            bfSync();
            const results = [];
            let done = 0;
            bfProgress(0, ids.length);
            try {
                for (const part of BF.chunk(ids, BF.RELEASE_MAX)) {
                    const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/backfill/release', { sample_ids: part });
                    if (!r || r.status !== 200) break;
                    for (const x of r.body.results) {
                        results.push(x);
                        if (x.ok) bf.selected.delete(x.sample_id);
                    }
                    done += part.length;
                    bfProgress(done, ids.length);
                }
            } finally {
                bf.releasing = false;
                bfProgress(null);
                bfSync();
            }
            if (results.length) S.toast(L.releaseSummary(results), results.every(x => x.ok) ? null : 'err');
            load();
        }
        return [
            h('div', { className: 'row' }, q, released, h('button', { type: 'button', className: 'btn btn-sm', text: 'Filter', onclick: filter }), total),
            bar,
            h('div', { className: 'tablewrap' }, table),
        ];
    }
    // A reload held back by a drag or a release runs when it ends.
    function bfRelease() {
        if (bf.pending && !bfHold()) { bf.pending = false; loadBackfill(true); }
    }
    async function loadBackfill(background) {
        const e = backfillEls;
        if (!e) return;
        if (background === true && bfHold()) { bf.pending = true; return; }
        const seq = ++bf.seq;
        const params = new URLSearchParams({ q: bf.q, released: bf.released, limit: '1000' });
        const r = await S.getJSON('/api/instruments/' + enc(e.inst.id) + '/backfill?' + params, background === true);
        if (e !== backfillEls || seq !== bf.seq) return;
        if (background === true && bfHold()) { bf.pending = true; return; }
        if (r.status !== 200) { S.toast((r.body && r.body.error) || 'Could not load backfill', 'err'); return; }
        const act = document.activeElement;
        const focusId = act && act.dataset && act.dataset.sel ? Number(act.dataset.id) : null;
        const info = { name: e.inst.name, live_since: r.body.live_since, live_since_set_at: r.body.live_since_set_at };
        bf.order = r.body.samples.map(s => s.sample_id);
        bf.selectable = new Set(r.body.samples.filter(s => s.status === 'final' && !s.released_at).map(s => s.sample_id));
        bf.selected = BF.prune(bf.selected, bf.selectable);
        if (bf.anchor !== null && !bf.selectable.has(bf.anchor)) bf.anchor = null;
        bf.rows = new Map();
        e.total.textContent = r.body.total + ' run(s)' + (r.body.total > r.body.samples.length ? ', first ' + r.body.samples.length + ' shown' : '');
        const rows = r.body.samples.map(s => {
            const can = bf.selectable.has(s.sample_id);
            const box = h('input', { type: 'checkbox', disabled: !can, 'aria-label': 'Select ' + s.lab_id,
                                     'data-sel': '1', 'data-id': String(s.sample_id) });
            const st = U.sampleStatus(s.status);
            const tr = h('tr', { 'data-id': String(s.sample_id), className: can ? 'can' : null },
                h('td', { className: 'sel' }, box),
                h('td', {}, h('span', { className: 'lab', text: s.lab_id }),
                    h('span', { className: 'why', 'data-testid': 'bf-why', text: BF.whyText(s, info) })),
                h('td', {}, h('span', { className: 'row' }, S.glyph(st.glyph), h('span', { text: st.text }))),
                h('td', { text: s.released_at ? U.clockTime(s.released_at, Date.now()) + ' by ' + (U.actorName(s.released_by) || '—') : 'No' }));
            bf.rows.set(s.sample_id, { tr, box });
            return tr;
        });
        e.tbody.replaceChildren(...rows);
        if (!rows.length) e.tbody.appendChild(h('tr', {}, h('td', { colspan: 4, className: 'muted', text: 'No backfill runs.' })));
        if (focusId !== null && bf.rows.has(focusId)) bf.rows.get(focusId).box.focus({ preventScroll: true });
        bfSync();
    }

    let conflictsBox = null;
    function conflictsSection() {
        conflictsBox = h('div', { className: 'body', style: 'gap:12px' });
        return [conflictsBox];
    }
    async function loadConflicts(background) {
        const box = conflictsBox;
        const r = await S.getJSON('/api/conflicts?instrument=' + enc(IID), background === true);
        if (box !== conflictsBox) return;
        box.replaceChildren();
        if (r.status !== 200) { box.appendChild(h('p', { className: 'errline', text: (r.body && r.body.error) || 'Could not load conflicts' })); return; }
        if (!r.body.conflicts.length) { box.appendChild(h('div', { className: 'empty-note' }, S.icon('check'), h('span', { text: 'No open conflicts.' }))); return; }
        for (const c of r.body.conflicts) {
            const ex = c.existing || {};
            const rows = [['Lab ID', ex.lab_id, c.held.lab_id], ['Injected', ex.injection_dt, c.held.injection_dt],
                ['File', ex.file_name, c.held.file_name], ['sha256', ex.cdf_sha256, c.held.cdf_sha256],
                ['Received', ex.received_at, c.held.received_at]];
            box.appendChild(h('div', { className: 'card', style: 'padding:16px;display:grid;gap:12px' },
                h('div', { className: 'tablewrap' }, h('table', { className: 'tbl' },
                    h('thead', {}, h('tr', {}, h('th', { text: 'Conflict ' + c.id }), h('th', { text: 'Kept (sample ' + txt(ex.sample_id) + ')' }), h('th', { text: 'Arrived' }))),
                    h('tbody', {}, ...rows.map(([k, a, b]) => h('tr', {}, h('td', { className: 'muted', text: k }), h('td', { className: 'mono', text: txt(a) }), h('td', { className: 'mono', text: txt(b) })))))),
                c.error ? h('p', { className: 'errline', text: c.error }) : null,
                c.replace_pending ? h('p', { className: 'warnline', text: 'Replace queued: waiting for the worker.' }) : null,
                h('div', { className: 'row' },
                    h('button', { type: 'button', className: 'btn btn-sm', text: 'Keep the existing file', onclick: () => resolve(c, 'keep') }),
                    h('button', { type: 'button', className: 'btn btn-sm btn-danger', text: 'Replace', disabled: c.replace_pending, onclick: () => resolve(c, 'replace') }))));
        }
    }
    async function resolve(c, how) {
        const msg = how === 'keep' ? 'Keep the existing file for conflict ' + c.id + '? The arrived file stays on disk.'
            : "Replace the sample's file with the one that arrived (conflict " + c.id + ')? It is reprocessed as a new revision; the old file is kept.';
        if (!window.confirm(msg)) return;
        const r = await S.adminPost('/api/admin/conflicts/' + enc(c.id) + '/' + how, {});
        if (r && r.status === 200) { S.toast(how === 'keep' ? 'Kept the existing file.' : 'Replace queued.'); loadConflicts(); }
    }

    const SECTIONS = { agent: agentSection, calibration: calibrationSection, corrections: correctionsSection,
                       export: exportSection, methods: methodsSection, backfill: backfillSection, conflicts: conflictsSection };

    // A section someone is editing (typed, ticked, picked, or focused) is left
    // alone by live updates; it is redrawn after its own save, or the next
    // time it is neither dirty nor focused.
    const DIRTY = new Set();
    function clean(key) { DIRTY.delete(key); }
    for (const key of Object.keys(SECTIONS)) {
        const body = $(key + '-body');
        const mark = (ev) => { if (ev.isTrusted) DIRTY.add(key); };
        body.addEventListener('input', mark);
        body.addEventListener('change', mark);
    }
    function busy(key) {
        const body = $(key + '-body');
        return DIRTY.has(key) || (document.activeElement && document.activeElement !== document.body &&
                                  body.contains(document.activeElement));
    }
    function renderSection(key, background) {
        if (background && busy(key)) return;
        DIRTY.delete(key);
        $(key + '-body').replaceChildren(...SECTIONS[key]().filter(Boolean));
    }

    function render(background) {
        renderHead();
        renderChecklist();
        for (const key of Object.keys(SECTIONS)) {
            // v4.0.1: live updates redraw only the Backfill rows, never its
            // controls, so the filter and the selection stay as they are
            if (key === 'backfill' && background && backfillEls) continue;
            renderSection(key, background);
        }
        const typing = backfillEls && [backfillEls.q, backfillEls.released].includes(document.activeElement);
        if (!(background && typing)) loadBackfill(background);
        if (!(background && busy('conflicts'))) loadConflicts(background);
        S.addRecent({ href: U.instrumentHref(IID), label: D.instrument.name });
    }

    // ── live ────────────────────────────────────────────────────────────────
    function tick() {
        if (!D) return;
        const st = U.agentStatus(agent, Date.now());
        const pill = $('head-pill');
        if (pill) {
            pill.className = 'pill ' + (st.glyph === 'never' ? '' : st.glyph);
            pill.firstChild.className = 'glyph ' + st.glyph;
            pill.lastChild.textContent = st.label;
        }
        const s = $('agent-status');
        if (s) {
            s.firstChild.className = 'glyph ' + st.glyph;
            s.lastChild.textContent = statusText(st);
        }
    }

    let reloadTimer = null;
    function reloadSoon() { clearTimeout(reloadTimer); reloadTimer = setTimeout(() => load(true), 300); }
    function onLive(update) {
        const mine = (update.agents || []).find(a => a.instrument_id === IID);
        if (mine) {
            const first = !(agent && agent.last_seen) && mine.last_seen;
            const changed = mine.last_seen && (!agent || mine.last_seen !== agent.last_seen);
            agent = Object.assign({}, agent || {}, mine, { state: mine.status !== undefined ? mine.status : (agent || {}).state });
            if (first) reloadSoon();
            else if (changed && SETUP && !SETUP.summary.ready) loadSetup();
        }
        if (update.reset || (update.instruments || []).includes(IID)) reloadSoon();
        tick();
    }

    $('edit-details').addEventListener('click', () => { if (D) A.editDetails(D.instrument, LIST.hub_methods || [], load); });
    $('checklist-toggle').addEventListener('click', () => { checklistOpen = !checklistOpen; renderChecklist(); });
    S.onInstruments((body) => { LIST = body; if (D) renderSection('agent', true); });
    A.lemMachines().then(ans => { LEM = ans; if (D) renderHead(); });
    load().then(() => {
        if (new URLSearchParams(location.search).get('edit') === '1' && D) A.editDetails(D.instrument, LIST.hub_methods || [], load);
    });
    if (window.GCLiveAdapter) window.GCLiveAdapter.subscribe(onLive);
    setInterval(tick, 1000);
})();
