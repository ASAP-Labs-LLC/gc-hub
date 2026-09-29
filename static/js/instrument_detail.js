// /instruments/<id> (v3.1): the setup checklist, then Agent, Calibration,
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
    async function load(background) {
        const bg = background === true;
        const [d, st] = await Promise.all([S.getJSON('/api/instruments/' + enc(IID), bg),
                                           S.getJSON('/api/instruments/' + enc(IID) + '/setup', bg)]);
        bgRender = bg;
        if (d.status !== 200) { S.toast((d.body && d.body.error) || 'Could not load ' + IID, 'err'); return; }
        D = d.body;
        if (st.status === 200) SETUP = st.body;
        const server = D.instrument.agent || {};
        const liveNewer = agent && agent.last_seen && (!server.last_seen || agent.last_seen > server.last_seen);
        agent = Object.assign({}, server, liveNewer ? { last_seen: agent.last_seen } : {});
        render();
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
        $('checklist-count').textContent = sm.ready ? 'Ready · all 8 steps done'
            : sm.done + ' of ' + sm.total + ' done · step ' + sm.step + ' is next';
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
                seen ? ['Queue', (a.queue_size || 0) + ' waiting · ' + (a.rejected_count || 0) + ' rejected'] : null,
                seen && a.last_file ? ['Last file', a.last_file, 'mono'] : null,
                seen && a.last_error ? ['Last error', a.last_error, 'errline'] : null,
            ]),
            skew ? h('p', { className: 'warnline', text: skew + ' Fix it before relying on live since.' }) : null,
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

    function exportSection() {
        const inst = D.instrument;
        const ex = D.export || {};
        const path = h('input', { type: 'text', className: 'grow mono', value: ex.configured ? ex.path : '', placeholder: ex.path || 'An absolute path to a .csv file', 'aria-label': 'Results file path' });
        const state = ex.refused ? 'Refused (' + ex.refused + '): ' + txt(ex.refused_detail) : ex.last_error || ex.error ? txt(ex.last_error || ex.error) : 'OK';
        const liveText = inst.live_since ? inst.live_since.slice(0, 16) + " (the GC's clock)"
            : 'Not live: every run is backfill and is never written to this file unless released';
        async function setPath() {
            const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/export-path', { path: path.value });
            if (r && r.status === 200) { S.toast('Results file set. If a file is already there, adopt it before the hub appends.'); load(); }
        }
        async function adopt() {
            if (!window.confirm('Adopt ' + txt(ex.path) + ' as it is now? The hub appends after its current content.')) return;
            const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/export-adopt', {});
            if (r && r.status === 200) { S.toast('Adopted.'); load(); }
        }
        return [
            kv([
                ['File', ex.path + (ex.configured ? '' : ' (the default)'), 'mono'],
                ['Waiting to write', (ex.pending || 0) + ' row(s)'],
                ['State', state, ex.refused ? 'errline' : ''],
                ['Live since', liveText],
                ['Backfill', (inst.backfill_unreleased || 0) + ' run(s) held back'],
            ]),
            ...(inst.live_since ? (D.live_since_warnings || []) : []).map(w => h('p', { className: 'warnline', text: w })),
            h('div', { className: 'row' },
                inst.live_since ? null : h('button', { type: 'button', className: 'btn btn-primary', 'data-testid': 'go-live', text: 'Go live now',
                                                       onclick: async () => { if (await A.goLiveNow(inst)) load(); } }),
                h('button', { type: 'button', className: 'btn btn-ghost', text: inst.live_since ? 'Change live since…' : 'Choose a time…',
                              onclick: () => A.editDetails(inst, LIST.hub_methods || [], load) })),
            h('div', { className: 'row' }, path, h('button', { type: 'button', className: 'btn', text: 'Set results file', onclick: setPath }),
                h('button', { type: 'button', className: 'btn btn-ghost', text: 'Adopt the file as it is', onclick: adopt })),
        ];
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

    let backfillEls = null;
    function backfillSection() {
        const inst = D.instrument;
        const q = h('input', { type: 'text', placeholder: 'Lab ID contains…', 'aria-label': 'Lab ID contains' });
        const released = h('select', { 'aria-label': 'Released' }, h('option', { value: 'false', text: 'Not released' }),
            h('option', { value: 'true', text: 'Released' }), h('option', { value: '', text: 'All' }));
        const tbody = h('tbody');
        const total = h('span', { className: 'caption' });
        backfillEls = { inst, q, released, tbody, total };
        async function releaseSelected() {
            const ids = Array.from(tbody.querySelectorAll('input[type=checkbox]:checked')).map(x => Number(x.dataset.id));
            if (!ids.length) { S.toast('Select runs to release.', 'err'); return; }
            if (!window.confirm('Release ' + ids.length + ' run(s)? Each is written to ' + inst.name + "'s results file (which LEM reads). This can't be undone.")) return;
            const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/backfill/release', { sample_ids: ids });
            if (r && r.status === 200) { S.toast(L.releaseSummary(r.body.results), r.body.results.every(x => x.ok) ? null : 'err'); load(); }
        }
        return [
            h('div', { className: 'row' }, q, released, h('button', { type: 'button', className: 'btn btn-sm', text: 'Filter', onclick: loadBackfill }), total),
            h('div', { className: 'tablewrap' }, h('table', { className: 'tbl' },
                h('thead', {}, h('tr', {}, ...['', 'Lab ID', 'Injected', 'Status', 'Released'].map(t => h('th', { text: t })))), tbody)),
            h('div', { className: 'row' }, h('button', { type: 'button', className: 'btn', text: 'Release selected…', onclick: releaseSelected })),
        ];
    }
    async function loadBackfill(background) {
        const e = backfillEls;
        if (!e) return;
        const params = new URLSearchParams({ q: e.q.value, released: e.released.value, limit: '200' });
        const r = await S.getJSON('/api/instruments/' + enc(e.inst.id) + '/backfill?' + params, background === true);
        if (e !== backfillEls) return;
        e.tbody.replaceChildren();
        if (r.status !== 200) { S.toast((r.body && r.body.error) || 'Could not load backfill', 'err'); return; }
        e.total.textContent = r.body.total + ' run(s)' + (r.body.total > r.body.samples.length ? ', first ' + r.body.samples.length + ' shown' : '');
        for (const s of r.body.samples) {
            const box = h('input', { type: 'checkbox', disabled: s.status !== 'final' || !!s.released_at, 'aria-label': 'Release ' + s.lab_id });
            box.dataset.id = String(s.sample_id);
            const st = U.sampleStatus(s.status);
            e.tbody.appendChild(h('tr', {}, h('td', {}, box), h('td', { text: s.lab_id }), h('td', { text: s.injection_dt }),
                h('td', {}, h('span', { className: 'row' }, S.glyph(st.glyph), h('span', { text: st.text }))),
                h('td', { text: s.released_at ? U.clockTime(s.released_at, Date.now()) + ' by ' + (U.actorName(s.released_by) || '—') : 'No' })));
        }
        if (!r.body.samples.length) e.tbody.appendChild(h('tr', {}, h('td', { colspan: 5, className: 'muted', text: 'No backfill runs.' })));
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
    function renderSection(key) {
        const body = $(key + '-body');
        const focused = document.activeElement && body.contains(document.activeElement) && document.activeElement.tagName === 'INPUT';
        if (focused && key === 'corrections') return;          // never wipe a half-typed value
        body.replaceChildren(...SECTIONS[key]().filter(Boolean));
    }

    function render() {
        renderHead();
        renderChecklist();
        for (const key of Object.keys(SECTIONS)) renderSection(key);
        loadBackfill(bgRender);
        loadConflicts(bgRender);
        S.addRecent({ href: '/instruments/' + IID, label: D.instrument.name });
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
    S.onInstruments((body) => { LIST = body; if (D) renderSection('agent'); });
    A.lemMachines().then(ans => { LEM = ans; if (D) renderHead(); });
    load().then(() => {
        if (new URLSearchParams(location.search).get('edit') === '1' && D) A.editDetails(D.instrument, LIST.hub_methods || [], load);
    });
    if (window.GCLiveAdapter) window.GCLiveAdapter.subscribe(onLive);
    setInterval(tick, 1000);
})();
