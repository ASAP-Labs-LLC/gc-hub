// The Instruments page (2A2). Every string from the server (instrument names,
// and the agent's host/state/last_file/last_error/version, which come from
// the GC PCs) is rendered as text: the DOM is built with createElement and
// textContent only (2B1 review M5). Pure logic is in instruments_logic.js.
(function () {
    'use strict';
    const L = window.InstrumentsLogic;
    const $ = (id) => document.getElementById(id);

    let STATE = { list: [], hubUrl: null, commands: [], hubMethods: [], selected: null, detail: null };

    // Every instrument switch bumps the generation; an answer that arrives for
    // an earlier one (a slow request after a click on another instrument) is
    // dropped instead of being drawn into the wrong instrument's page.
    let GENERATION = 0;
    const stale = (gen) => gen !== GENERATION;

    // ── DOM helpers (text only) ────────────────────────────────────────────
    function h(tag, props, ...children) {
        const el = document.createElement(tag);
        for (const [k, v] of Object.entries(props || {})) {
            if (v === null || v === undefined || v === false) continue;
            if (k === 'className') el.className = v;
            else if (k === 'text') el.textContent = String(v);
            else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
            else if (k === 'value') el.value = v;
            else if (k === 'checked') el.checked = !!v;
            else el.setAttribute(k, String(v));
        }
        for (const c of children.flat()) {
            if (c === null || c === undefined || c === false) continue;
            el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
        }
        return el;
    }
    const txt = (v) => (v === null || v === undefined || v === '' ? '—' : String(v));

    function flash(message, level) {
        const f = $('flash');
        f.textContent = message || '';
        f.className = 'flash' + (message ? ' show ' + (level || 'ok') : '');
    }

    // ── HTTP ───────────────────────────────────────────────────────────────
    async function getJSON(path) {
        const r = await fetch(path, { headers: { Accept: 'application/json' } });
        let body = null;
        try { body = await r.json(); } catch (_e) { body = null; }
        return { status: r.status, body };
    }

    function password() {
        const pw = $('admin-pw').value;
        if (!pw) { flash('Enter the admin password (top right) first.', 'err'); return null; }
        return pw;
    }

    async function adminPost(path, payload, opts) {
        const pw = password();
        if (pw === null) return null;
        const r = await fetch(path, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(Object.assign({}, payload || {}, { password: pw })),
        });
        if (opts && opts.raw) return r;
        let body = null;
        try { body = await r.json(); } catch (_e) { body = null; }
        if (r.status >= 400 && !(opts && opts.quiet)) {
            flash((body && body.error) || ('HTTP ' + r.status), 'err');
        }
        return { status: r.status, body };
    }

    const enc = encodeURIComponent;

    // ── the list ───────────────────────────────────────────────────────────
    async function loadList(keepSelection) {
        const { status, body } = await getJSON('/api/instruments');
        if (status !== 200) { flash((body && body.error) || 'Could not load instruments', 'err'); return; }
        STATE.list = body.instruments || [];
        STATE.hubUrl = body.hub_url || null;
        STATE.commands = body.agent_commands || [];
        STATE.hubMethods = body.hub_methods || [];
        const form = $('hub-url-form');
        const hubInput = form && form.elements.namedItem('hub_url');
        if (hubInput && document.activeElement !== hubInput) hubInput.value = STATE.hubUrl || '';
        renderList();
        const want = keepSelection ? STATE.selected : (new URLSearchParams(location.search).get('instrument')
            || (STATE.list[0] && STATE.list[0].id));
        if (want && STATE.list.some(i => i.id === want)) await select(want);
    }

    function renderList() {
        const ul = $('inst-list');
        ul.replaceChildren();
        const now = Date.now();
        for (const inst of STATE.list) {
            const cal = L.calibrationBadge(inst.calibration);
            const agent = L.agentHealth(inst.agent, now);
            const counts = inst.counts || {};
            const li = h('li', { className: inst.id === STATE.selected ? 'sel' : '',
                                 onclick: () => select(inst.id) },
                h('div', { className: 'nm' }, h('span', { className: 'dot ' + agent.level, title: agent.text }),
                    inst.name, h('span', { className: 'muted', text: '  ' + inst.id })),
                h('div', { className: 'sub' },
                    inst.enabled ? '' : 'disabled · ',
                    cal.level === 'ok' ? 'calibrated' : 'not calibrated',
                    ' · ', (counts.final || 0) + ' final',
                    inst.backfill_unreleased ? ' · ' + inst.backfill_unreleased + ' backfill' : '',
                    inst.open_conflicts ? ' · ' + inst.open_conflicts + ' conflict(s)' : '',
                    counts.pending_corrections ? ' · ' + counts.pending_corrections + ' waiting for corrections' : '',
                    counts.awaiting_calibration ? ' · ' + counts.awaiting_calibration + ' waiting for calibration' : ''));
            ul.appendChild(li);
        }
    }

    async function select(id) {
        const gen = ++GENERATION;
        STATE.selected = id;
        renderList();
        const { status, body } = await getJSON('/api/instruments/' + enc(id));
        if (stale(gen)) return;
        if (status !== 200) { flash((body && body.error) || 'Could not load ' + id, 'err'); return; }
        STATE.detail = body;
        renderDetail();
    }

    // ── the detail ─────────────────────────────────────────────────────────
    function renderDetail() {
        const d = STATE.detail;
        const main = $('detail');
        main.replaceChildren(
            settingsCard(d), agentCard(d), exportCard(d), calibrationCard(d), correctionsCard(d),
            methodsCard(d), backfillCard(d), conflictsCard(d), standardsCard(d));
        loadBackfill();
        loadConflicts();
        loadStandards();
    }

    function card(title, ...children) {
        return h('section', { className: 'card' }, h('h3', {}, ...[].concat(title)), ...children);
    }

    async function afterChange(message, level) {
        flash(message, level || 'ok');
        await loadList(true);
    }

    // ── LEM machine dropdown (D10) ─────────────────────────────────────────
    // One GET /api/lem/machines per page load (and per Refresh); the hub
    // fetches from LEM and caches it. Titles and uids from LEM are untrusted:
    // option texts are set with textContent (h's `text`).
    let LEM_ANSWER = null;
    function lemMachines(refresh) {
        if (!LEM_ANSWER || refresh) {
            LEM_ANSWER = getJSON('/api/lem/machines')
                .then(r => (r.status === 200 ? r.body : null))
                .catch(() => null);
        }
        return LEM_ANSWER;
    }

    // A <select> of LEM's machines for `saved` (a uid or ''), with "Other…"
    // revealing a text box. value() is the uid to save (the saved one until
    // the list has loaded).
    function lemPicker(saved) {
        const start = saved || '';
        const select = h('select', { disabled: true, 'aria-label': 'LEM machine' },
            h('option', { text: 'Loading LEM machines…' }));
        const other = h('input', { maxlength: 128, value: start, hidden: true,
                                   placeholder: 'LEM machine uid', 'aria-label': 'LEM machine uid' });
        const note = h('span', { className: 'hint', hidden: true });
        let options = null;
        const chosen = () => (options ? options[select.selectedIndex] : null);
        function sync() {
            const o = chosen();
            other.hidden = !(o && o.other);
            if (!other.hidden) other.focus();
        }
        select.addEventListener('change', sync);
        lemMachines().then(answer => {
            const r = L.lemMachineOptions(answer, start);
            options = r.options;
            select.replaceChildren(...options.map(o => h('option', { text: o.text })));
            select.selectedIndex = r.selected;
            select.disabled = false;
            note.textContent = r.note || '';
            note.hidden = !r.note;
            other.hidden = true;
        });
        return {
            el: h('span', { className: 'lem-picker' }, select, other, note),
            value: () => (options ? L.lemPickerValue(chosen(), other.value) : start),
            reset() {
                other.value = '';
                other.hidden = true;
                if (options) select.selectedIndex = 0;
            },
        };
    }

    // settings: name, enabled, method, live_since, lem_machine_uid
    function settingsCard(d) {
        const inst = d.instrument;
        const name = h('input', { value: inst.name, maxlength: 64 });
        const enabled = h('input', { type: 'checkbox', checked: !!inst.enabled });
        const method = h('select', {}, ...STATE.hubMethods.map(m => h('option', { value: m, text: m })));
        method.value = inst.method;
        const live = h('input', { type: 'datetime-local', step: 1, value: L.liveSinceInput(inst.live_since) });
        const uid = lemPicker(inst.lem_machine_uid || '');
        const warnings = h('div');
        for (const w of d.live_since_warnings || []) warnings.appendChild(h('p', { className: 'warnline', text: w }));
        async function save() {
            const ls = L.liveSinceValue(live.value);
            if (ls.error) { flash(ls.error, 'err'); return; }
            const payload = { name: name.value, enabled: enabled.checked, method: method.value,
                              lem_machine_uid: uid.value() };
            if (ls.value !== (inst.live_since || '')) {
                const ask = L.liveSinceConfirm(inst.live_since, ls.value, L.localNow());
                if (ask && !confirm(ask)) return;
                payload.live_since = ls.value;
            }
            const r = await adminPost('/api/admin/instruments/' + enc(inst.id), payload);
            if (!r || r.status !== 200) return;
            await afterChange('Saved.', (r.body.warnings || []).length ? 'warn' : 'ok');
            if ((r.body.warnings || []).length) flash('Saved. ' + r.body.warnings.join(' '), 'warn');
        }
        return card([inst.name, h('span', { className: 'badge', text: inst.id })],
            h('div', { className: 'grid2' },
                h('label', {}, 'Name', name),
                h('label', {}, 'Method', method),
                h('label', { title: "Injections before this (the GC's local clock) are backfill (D11)" },
                    'Live since (GC local time)', live),
                h('label', { title: 'Informational: LabStation routing only' }, 'LEM machine', uid.el),
                h('label', {}, enabled, ' Enabled (a disabled instrument\'s agent is refused)')),
            warnings,
            h('p', { className: 'hint', text: 'live_since applies to samples received from now on; ' +
                'samples already received keep their backfill flag. Empty = everything is backfill.' }),
            h('div', { className: 'row' }, h('button', { className: 'btn primary', onclick: save, text: 'Save settings' })));
    }

    // agent panel: status, clock skew, commands, installer, revoke
    function agentCard(d) {
        const inst = d.instrument;
        const a = inst.agent || {};
        const health = L.agentHealth(inst.agent, Date.now());
        const skewWarn = L.skewWarning(a.clock_skew_seconds);
        const rows = [
            ['Status', health.text], ['Host', a.host], ['Agent version', a.version], ['State', a.state],
            ['Queued files', a.queue_size], ['Rejected files', a.rejected_count], ['Last file', a.last_file],
            ['Last error', a.last_error], ['Last seen', a.last_seen],
            ['Clock skew', L.formatSkew(a.clock_skew_seconds)], ['Results seq', a.results_seq],
            ['Pending command', a.pending_command],
            ['Token', inst.has_token ? 'issued ' + txt(inst.token_issued_at) : 'none'],
        ];
        const table = h('table', {}, h('tbody', {}, ...rows.map(([k, v]) =>
            h('tr', {}, h('th', { text: k }), h('td', { className: k === 'Last file' || k === 'Last error' ? 'mono' : '', text: txt(v) })))));
        const cmds = STATE.commands.filter(c => c !== 'adopt-mirror').map(c =>
            h('button', { className: 'btn', text: c, onclick: () => command(inst.id, c) }));
        return card(['Agent', h('span', { className: 'badge ' + (health.level === 'ok' ? 'ok' : 'warn'), text: health.text })],
            table,
            skewWarn ? h('p', { className: 'warnline', text: skewWarn + ' Fix it before relying on live_since.' }) : null,
            h('div', { className: 'row' }, ...cmds),
            h('div', { className: 'row' },
                h('button', { className: 'btn primary', text: 'Download installer', onclick: () => installer(inst.id, false) }),
                h('button', { className: 'btn danger', text: 'Revoke token', disabled: !inst.has_token,
                              onclick: () => revoke(inst) })),
            h('p', { className: 'hint', text: 'Downloading an installer mints a new agent token; the old one stops working.' }));
    }

    async function command(id, cmd) {
        const r = await adminPost('/api/admin/instruments/' + enc(id) + '/agent-command', { command: cmd });
        if (r && r.status === 200) await afterChange('Command "' + cmd + '" queued; the agent takes it at its next heartbeat.');
    }

    async function revoke(inst) {
        if (!confirm('Revoke ' + inst.name + "'s agent token? Its agent stops sending until it gets a new installer.")) return;
        const r = await adminPost('/api/admin/instruments/' + enc(inst.id) + '/revoke-token', {});
        if (r && r.status === 200) await afterChange('Token revoked.');
    }

    async function installer(id, confirmRevoke) {
        const r = await adminPost('/api/admin/instruments/' + enc(id) + '/installer',
            { confirm_revoke: !!confirmRevoke }, { raw: true });
        if (!r) return;
        let body = null;
        if (r.status !== 200) { try { body = await r.json(); } catch (_e) { body = null; } }
        const out = L.installerOutcome(r.status, body);
        if (out.kind === 'download') {
            const blob = await r.blob();
            const url = URL.createObjectURL(blob);
            const a = h('a', { href: url, download: 'gc-agent-installer-' + id + '.zip' });
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 10000);
            await afterChange('Installer downloaded. It holds the new token: copy it to the GC PC and run install.pyw.');
        } else if (out.kind === 'confirm') {
            if (confirm(out.message)) await installer(id, true);
        } else if (out.kind === 'needs_hub_url') {
            flash(out.message + ' Set it under "Hub URL for installers" (left).', 'err');
            $('hub-url-box').open = true;
        } else {
            flash(out.message, 'err');
        }
    }

    // export path + adopt
    function exportCard(d) {
        const inst = d.instrument;
        const ex = d.export || {};
        const path = h('input', { value: ex.configured ? ex.path : '', placeholder: ex.path || '', size: 60 });
        path.style.width = '100%';
        async function setPath() {
            const r = await adminPost('/api/admin/instruments/' + enc(inst.id) + '/export-path', { path: path.value });
            if (r && r.status === 200) await afterChange('Export path set. If a file is already there, adopt it before the hub appends.');
        }
        async function adopt() {
            if (!confirm('Adopt ' + txt(ex.path) + ' as it is now? The hub will append after its current content.')) return;
            const r = await adminPost('/api/admin/instruments/' + enc(inst.id) + '/export-adopt', {});
            if (r && r.status === 200) await afterChange('Adopted.');
        }
        return card('Results export (append-only CSV)',
            h('table', {}, h('tbody', {},
                h('tr', {}, h('th', { text: 'File' }), h('td', { className: 'mono', text: txt(ex.path) })),
                h('tr', {}, h('th', { text: 'Pending rows' }), h('td', { text: txt(ex.pending) })),
                h('tr', {}, h('th', { text: 'Refused' }), h('td', { className: ex.refused ? 'errline' : '', text: ex.refused ? ex.refused + ': ' + txt(ex.refused_detail) : 'no' })),
                h('tr', {}, h('th', { text: 'Last error' }), h('td', { text: txt(ex.last_error || ex.error) })))),
            h('label', {}, 'Export to (absolute path; LEM tails this file)', path),
            h('div', { className: 'row' },
                h('button', { className: 'btn', text: 'Set path', onclick: setPath }),
                h('button', { className: 'btn', text: 'Adopt the file as it is', onclick: adopt })));
    }

    // calibration: CDF from own samples or path; link to the assignment page
    function calibrationCard(d) {
        const inst = d.instrument;
        const st = inst.calibration || {};
        const badge = L.calibrationBadge(st);
        const q = h('input', { placeholder: 'lab ID contains…' });
        const list = h('tbody');
        const path = h('input', { placeholder: 'or an absolute path to a CDF' });
        path.style.width = '100%';
        async function search() {
            const { status, body } = await getJSON('/api/instruments/' + enc(inst.id) +
                '/calibration-candidates?q=' + enc(q.value));
            list.replaceChildren();
            if (status !== 200) { flash((body && body.error) || 'Search failed', 'err'); return; }
            for (const c of body.candidates) {
                list.appendChild(h('tr', {}, h('td', { text: c.sample_id }), h('td', { text: c.lab_id }),
                    h('td', { text: c.injection_dt }), h('td', { text: c.method_name || '—' }), h('td', { text: c.status }),
                    h('td', {}, h('button', { className: 'btn', text: 'Use', onclick: () => useCdf({ sample_id: c.sample_id }) }))));
            }
            if (!body.candidates.length) list.appendChild(h('tr', {}, h('td', { colspan: 6, className: 'muted', text: 'No samples.' })));
        }
        async function useCdf(payload) {
            if (!confirm('Use this as ' + inst.name + "'s calibration CDF? Its saved peak assignments are cleared; assign the peaks next.")) return;
            const r = await adminPost('/api/admin/instruments/' + enc(inst.id) + '/calibration-cdf', payload);
            if (r && r.status === 200) await afterChange('Calibration CDF set. Now assign its peaks.');
        }
        return card(['Calibration', h('span', { className: 'badge ' + badge.level, text: badge.text })],
            h('p', {}, 'CDF: ', h('span', { className: 'mono', text: txt(st.calibration_cdf) }),
                ' · sensitivity ', txt(st.sensitivity)),
            h('div', { className: 'row' },
                h('a', { className: 'btn primary', href: '/calibration?instrument=' + enc(inst.id), text: 'Assign peaks…' })),
            h('div', { className: 'row' }, q, h('button', { className: 'btn', text: 'Find calibration runs', onclick: search })),
            h('div', { className: 'tablescroll' }, h('table', {},
                h('thead', {}, h('tr', {}, ...['Sample', 'Lab ID', 'Injected', 'Method', 'Status', ''].map(t => h('th', { text: t })))),
                list)),
            h('div', { className: 'row' }, path,
                h('button', { className: 'btn', text: 'Use path', onclick: () => useCdf({ path: path.value }) })));
    }

    // corrections editor (D4b)
    function correctionsCard(d) {
        const inst = d.instrument;
        const c = d.corrections;
        const inputs = {};
        const grid = h('div', { className: 'cuts' });
        for (const cut of c.cuts) {
            inputs[cut] = h('input', { inputmode: 'decimal',
                value: c.values && c.values[cut] !== undefined ? String(c.values[cut]) : '' });
            grid.appendChild(h('label', {}, cut + ' (°C)', inputs[cut]));
        }
        const reason = h('input', { placeholder: 'Reason (required, kept in the history)', maxlength: 500 });
        reason.style.width = '100%';
        const errs = h('div');
        async function save() {
            errs.replaceChildren();
            const raw = {};
            for (const cut of c.cuts) raw[cut] = inputs[cut].value;
            const parsed = L.parseCorrections(raw, c.cuts, c.max_abs);
            if (parsed.errors.length) { parsed.errors.forEach(e => errs.appendChild(h('p', { className: 'errline', text: e }))); return; }
            if (!reason.value.trim()) { errs.appendChild(h('p', { className: 'errline', text: 'A reason is required.' })); return; }
            const r = await adminPost('/api/admin/instruments/' + enc(inst.id) + '/corrections',
                { values: parsed.values, reason: reason.value });
            if (!r) return;
            if (r.status === 200) {
                await afterChange(r.body.changed + ' value(s) changed; ' + r.body.queued + ' sample(s) waiting for corrections queued.');
            } else if (r.body && r.body.errors) {
                r.body.errors.forEach(e => errs.appendChild(h('p', { className: 'errline', text: e })));
            }
        }
        async function seed() {
            if (!confirm('Seed GC-1\'s hub correction factors from the phase-1 file? This is done once; afterwards the file is never read again.')) return;
            const r = await adminPost('/api/admin/instruments/' + enc(inst.id) + '/corrections/seed', {});
            if (r && r.status === 200) await afterChange('Seeded from correction_factors.json.');
        }
        const source = c.source === 'hub' ? 'Hub values, last changed ' + txt(c.updated_at) + ' by ' + txt(c.updated_by)
            : c.source === 'file' ? 'Interim: read from the phase-1 file until seeded'
            : 'Not set: this instrument\'s samples wait (pending corrections) until all 11 are entered';
        const audit = h('tbody', {}, ...(c.audit || []).map(a => h('tr', {},
            h('td', { text: a.changed_at }), h('td', { text: a.cut }), h('td', { text: txt(a.old_value) }),
            h('td', { text: txt(a.new_value) }), h('td', { text: txt(a.changed_by) }), h('td', { text: a.reason }))));
        return card(['D86 correction factors', h('span', { className: 'badge ' + (c.source === 'hub' ? 'ok' : 'warn'), text: c.source || 'not set' })],
            h('p', { className: 'muted', text: source }),
            c.file_error ? h('p', { className: 'errline', text: 'Phase-1 file: ' + c.file_error }) : null,
            h('p', { className: 'hint', text: 'Added to the reported D86 temperatures. Never enter GC factors in LEM as well (they would be applied twice).' }),
            grid, h('div', { className: 'row' }, reason), errs,
            h('div', { className: 'row' },
                h('button', { className: 'btn primary', text: 'Save corrections', onclick: save }),
                c.can_seed ? h('button', { className: 'btn', text: 'Seed from correction_factors.json', onclick: seed }) : null),
            h('details', {}, h('summary', { text: 'History (' + (c.audit || []).length + ')' }),
                h('div', { className: 'tablescroll' }, h('table', {},
                    h('thead', {}, h('tr', {}, ...['When (UTC)', 'Cut', 'Old', 'New', 'By', 'Reason'].map(t => h('th', { text: t })))),
                    audit))));
    }

    // methods seen
    function methodsCard(d) {
        const inst = d.instrument;
        const m = d.methods;
        const rows = L.methodRows(m.seen);
        async function map(name, hubMethod) {
            const verb = hubMethod ? 'Map ' + name + ' to ' + hubMethod + '? Its held samples are queued for processing.'
                : 'Unmap ' + name + '? Existing results stay; new samples with it are held as another method.';
            if (!confirm(verb)) return;
            const r = await adminPost('/api/admin/instruments/' + enc(inst.id) + '/methods',
                { method_name: name, hub_method: hubMethod });
            if (r && r.status === 200) await afterChange(hubMethod ? r.body.queued + ' sample(s) queued.' : 'Unmapped.');
        }
        async function markOther() {
            if (!confirm('Mark every sample with no method name as "other method" (stored, never processed)?')) return;
            const r = await adminPost('/api/admin/instruments/' + enc(inst.id) + '/review-method', {});
            if (r && r.status === 200) await afterChange(r.body.marked + ' sample(s) marked other method.');
        }
        // The admin chooses which hub method a name maps to (default: the instrument's).
        function mapControl(r) {
            const pick = h('select', {}, ...STATE.hubMethods.map(x => h('option', { value: x, text: x })));
            if (STATE.hubMethods.includes(inst.method)) pick.value = inst.method;
            return h('span', {}, pick, ' ',
                h('button', { className: 'btn', text: 'Map', onclick: () => map(r.name, pick.value) }));
        }
        const body = h('tbody', {}, ...rows.map(r => h('tr', {},
            h('td', { className: 'mono', text: r.label }), h('td', { text: r.count }),
            h('td', { text: txt(r.first_seen) }), h('td', { text: txt(r.last_seen) }),
            h('td', { text: r.mapped_to || (r.action === 'review' ? 'held for review' : 'not processed') }),
            h('td', {}, r.action === 'map' ? mapControl(r)
                : r.action === 'unmap' ? h('button', { className: 'btn', text: 'Unmap', onclick: () => map(r.name, null) })
                : (m.review_count ? h('button', { className: 'btn', text: 'Mark ' + m.review_count + ' as other method', onclick: markOther }) : null)))));
        return card('Methods seen',
            h('p', { className: 'hint', text: 'Only mapped ChemStation methods are processed. A sample is never guessed into D2887.' }),
            h('div', { className: 'tablescroll' }, h('table', {},
                h('thead', {}, h('tr', {}, ...['Method', 'Samples', 'First', 'Last', 'Processed as', ''].map(t => h('th', { text: t })))),
                body)));
    }

    // backfill release (D11)
    let backfillEls = null;
    function backfillCard(d) {
        const inst = d.instrument;
        const q = h('input', { placeholder: 'lab ID contains…' });
        const status = h('select', {}, h('option', { value: '', text: 'any status' }),
            ...['final', 'raw_only', 'pending_corrections', 'awaiting_calibration', 'error', 'other_method', 'review_method']
                .map(s => h('option', { value: s, text: s })));
        const released = h('select', {}, h('option', { value: 'false', text: 'not released' }),
            h('option', { value: 'true', text: 'released' }), h('option', { value: '', text: 'all' }));
        const tbody = h('tbody');
        const total = h('span', { className: 'muted' });
        backfillEls = { inst, q, status, released, tbody, total };
        async function releaseSelected() {
            const ids = Array.from(tbody.querySelectorAll('input[type=checkbox]:checked')).map(x => Number(x.dataset.id));
            if (!ids.length) { flash('Select samples to release.', 'err'); return; }
            if (!confirm('Release ' + ids.length + ' backfill sample(s)? Each is appended to ' + inst.name +
                "'s export (and so reaches LEM). This can't be undone.")) return;
            const r = await adminPost('/api/admin/instruments/' + enc(inst.id) + '/backfill/release', { sample_ids: ids });
            if (r && r.status === 200) {
                const summary = L.releaseSummary(r.body.results);
                await afterChange(summary, r.body.results.every(x => x.ok) ? 'ok' : 'warn');
                flash(summary, r.body.results.every(x => x.ok) ? 'ok' : 'warn');
            }
        }
        return card('Backfill (not exported until released)',
            h('div', { className: 'row' }, q, status, released,
                h('button', { className: 'btn', text: 'Filter', onclick: loadBackfill }), total),
            h('div', { className: 'tablescroll' }, h('table', {},
                h('thead', {}, h('tr', {}, ...['', 'Sample', 'Lab ID', 'Injected', 'Status', 'Released'].map(t => h('th', { text: t })))),
                tbody)),
            h('div', { className: 'row' }, h('button', { className: 'btn primary', text: 'Release selected…', onclick: releaseSelected })));
    }

    async function loadBackfill() {
        const e = backfillEls;
        if (!e) return;
        const gen = GENERATION;
        const params = new URLSearchParams({ q: e.q.value, status: e.status.value, released: e.released.value, limit: '500' });
        const { status, body } = await getJSON('/api/instruments/' + enc(e.inst.id) + '/backfill?' + params);
        if (stale(gen) || e !== backfillEls) return;
        e.tbody.replaceChildren();
        if (status !== 200) { flash((body && body.error) || 'Could not load backfill', 'err'); return; }
        e.total.textContent = body.total + ' sample(s)' + (body.total > body.samples.length ? ', first ' + body.samples.length + ' shown' : '');
        for (const s of body.samples) {
            const box = h('input', { type: 'checkbox', disabled: s.status !== 'final' || !!s.released_at });
            box.dataset.id = String(s.sample_id);
            e.tbody.appendChild(h('tr', {}, h('td', {}, box), h('td', { text: s.sample_id }), h('td', { text: s.lab_id }),
                h('td', { text: s.injection_dt }), h('td', { text: s.status }),
                h('td', { text: s.released_at ? s.released_at + ' by ' + txt(s.released_by) : 'no' })));
        }
    }

    // conflicts
    let conflictsBody = null;
    function conflictsCard(d) {
        conflictsBody = h('div');
        return card('Conflicts (same sample, different file)',
            h('p', { className: 'hint', text: 'Replace makes the new file the sample\'s CDF and reprocesses it as a new revision; the old file is kept.' }),
            conflictsBody);
    }

    async function loadConflicts() {
        const inst = STATE.detail.instrument;
        const gen = GENERATION;
        const target = conflictsBody;
        const { status, body } = await getJSON('/api/conflicts?instrument=' + enc(inst.id));
        if (stale(gen) || target !== conflictsBody) return;
        conflictsBody.replaceChildren();
        if (status !== 200) { conflictsBody.appendChild(h('p', { className: 'errline', text: (body && body.error) || 'Could not load conflicts' })); return; }
        if (!body.conflicts.length) { conflictsBody.appendChild(h('p', { className: 'muted', text: 'No open conflicts.' })); return; }
        for (const c of body.conflicts) {
            const ex = c.existing || {};
            const rows = [['Lab ID', ex.lab_id, c.held.lab_id], ['Injected', ex.injection_dt, c.held.injection_dt],
                ['sha256', ex.cdf_sha256, c.held.cdf_sha256], ['File', ex.file_name, c.held.file_name],
                ['Received', ex.received_at, c.held.received_at], ['Source name', ex.source_name, ''],
                ['Status', ex.status + (ex.current_revision ? ' (revision ' + ex.current_revision + ')' : ''), c.held.size ? c.held.size + ' bytes' : '']];
            conflictsBody.appendChild(h('div', { className: 'card' },
                h('table', {}, h('thead', {}, h('tr', {}, h('th', { text: 'Conflict ' + c.id }), h('th', { text: 'Existing (sample ' + txt(ex.sample_id) + ')' }), h('th', { text: 'Received' }))),
                    h('tbody', {}, ...rows.map(([k, a, b]) => h('tr', {}, h('th', { text: k }), h('td', { className: 'mono', text: txt(a) }), h('td', { className: 'mono', text: txt(b) }))))),
                c.error ? h('p', { className: 'errline', text: c.error }) : null,
                c.replace_pending ? h('p', { className: 'warnline', text: 'Replace queued: waiting for the worker.' }) : null,
                h('div', { className: 'row' },
                    h('button', { className: 'btn', text: 'Keep existing', onclick: () => resolve(c, 'keep') }),
                    h('button', { className: 'btn danger', text: 'Replace', disabled: c.replace_pending, onclick: () => resolve(c, 'replace') }))));
        }
    }

    async function resolve(c, how) {
        const msg = how === 'keep' ? 'Keep the existing file for conflict ' + c.id + '? The received file stays on disk.'
            : 'Replace the existing sample\'s file with the received one (conflict ' + c.id + ')? It is reprocessed as a new revision.';
        if (!confirm(msg)) return;
        const r = await adminPost('/api/admin/conflicts/' + enc(c.id) + '/' + how, {});
        if (r && r.status === 200) await afterChange(how === 'keep' ? 'Kept the existing file.' : 'Replace queued.');
    }

    // standards (D12)
    let standardsBody = null;
    function standardsCard(d) {
        standardsBody = h('tbody');
        return card('Comparison standards (tagged by instrument)',
            h('p', { className: 'hint', text: 'The picker offers a sample\'s own instrument\'s standards first; comparing across instruments shows a warning.' }),
            h('div', { className: 'tablescroll' }, h('table', {},
                h('thead', {}, h('tr', {}, ...['Standard', 'File', 'Instrument'].map(t => h('th', { text: t })))),
                standardsBody)));
    }

    async function loadStandards() {
        const gen = GENERATION;
        const { status, body } = await getJSON('/api/standards');
        if (stale(gen)) return;
        standardsBody.replaceChildren();
        if (status !== 200) return;
        for (const s of body.standards) {
            const sel = h('select', {}, h('option', { value: '', text: '(not tagged)' }),
                ...STATE.list.map(i => h('option', { value: i.id, text: i.name })));
            sel.value = s.instrument_id || '';
            sel.addEventListener('change', async () => {
                const r = await adminPost('/api/admin/standards/' + enc(s.id) + '/instrument', { instrument_id: sel.value || null });
                if (r && r.status === 200) flash('Standard ' + s.name + ' tagged.', 'ok');
                else sel.value = s.instrument_id || '';
            });
            standardsBody.appendChild(h('tr', {}, h('td', { text: s.name }),
                h('td', { className: 'mono', text: s.file_name + (s.missing ? ' (missing)' : '') }), h('td', {}, sel)));
        }
    }

    // ── left column forms ──────────────────────────────────────────────────
    const ADD_LEM = lemPicker('');
    $('add-lem').appendChild(ADD_LEM.el);

    $('add-form').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const f = ev.target;
        const r = await adminPost('/api/admin/instruments', { id: f.elements.namedItem('id').value.trim(), name: f.elements.namedItem('name').value.trim(),
                                                              lem_machine_uid: ADD_LEM.value() });
        if (r && r.status === 201) {
            f.reset();
            ADD_LEM.reset();
            STATE.selected = r.body.instrument.id;
            await afterChange('Instrument ' + r.body.instrument.name + ' added. Set its calibration, corrections and live_since next.');
        }
    });

    $('hub-url-form').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const r = await adminPost('/api/admin/hub-url', { hub_url: ev.target.elements.namedItem('hub_url').value.trim() });
        if (r && r.status === 200) await afterChange(r.body.hub_url ? 'Hub URL set to ' + r.body.hub_url + '.' : 'Hub URL cleared.');
    });

    $('btn-refresh').addEventListener('click', () => { lemMachines(true); loadList(true); });

    loadList(false);
})();
