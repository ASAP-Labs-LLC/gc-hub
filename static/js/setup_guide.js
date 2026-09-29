// /setup (v3.1): the GC setup guide. One instrument's eight steps (GET
// /api/instruments/<id>/setup, derived by setup_state on the server) as
// plain-language cards with each step's explanation, primary button and
// blocker; "Add a new GC" (?new=1) starts at step 1 with the create form. The
// steps refresh by themselves when the agent checks in or the instrument
// changes (GCLiveAdapter). Text only.
(function () {
    'use strict';
    const U = window.GCUi;
    const S = window.GCShell;
    const A = window.GCActions;
    const h = S.h;
    const $ = (id) => document.getElementById(id);
    const enc = encodeURIComponent;
    const NEW = '__new';
    const params = new URLSearchParams(location.search);

    let LIST = null;
    let inst = null;
    let SETUP = null;
    let agentSeen = null;

    // ── the words ───────────────────────────────────────────────────────────
    function guide(key, n) {
        const name = n || 'this GC';
        const pc = n ? 'the ' + n + ' computer' : "the GC's computer";
        switch (key) {
            case 'create': return {
                title: 'Create the instrument and pick its LEM machine',
                why: 'Give the GC a name, and pick its LEM machine so LabStation knows where its results belong.' };
            case 'corrections': return {
                title: 'Enter the 11 correction factors',
                why: 'The D86 correction factors for ' + name + ' (from its EQM study). They are added to every D86 result it reports, so its numbers match LEM.' };
            case 'installer': return {
                title: 'Install the agent on ' + pc,
                why: "The agent is a small program on the GC's computer that sends every finished run to the hub by itself.",
                how: [
                    [{ t: 'Click ' }, { b: 'Download installer' }, { t: '. It saves a file called ' }, { c: 'gc-agent-installer-' + (inst ? inst.id : 'gc') + '.zip' }, { t: '.' }],
                    [{ t: 'Copy that file to the ' + name + ' computer: a USB stick works, or a shared folder.' }],
                    [{ t: 'On the ' + name + ' computer, open the file and double-click ' }, { b: 'Install' }, { t: '. If Windows asks "Do you want to allow…", click ' }, { b: 'Yes' }, { t: '.' }],
                    [{ t: 'When it says ' }, { b: 'Connected' }, { t: ", you're done. Leave that computer on: this page moves to the next step by itself." }],
                ] };
            case 'checkin': return {
                title: 'Wait for the agent to check in',
                why: 'Leave ' + pc + ' on and connected to the network. If nothing happens within a few minutes, run Install again on that computer.' };
            case 'calibration': return {
                title: 'Set the calibration',
                why: 'The hub needs to know which peak is which carbon number before any result is final.',
                how: [
                    [{ t: 'Run the n-alkane calibration standard on ' + name + ' as usual.' }],
                    [{ t: 'When the run shows up here, choose it as the calibration run.' }],
                    [{ t: 'Assign its peaks to carbon numbers and save.' }],
                ] };
            case 'method': return {
                title: 'Confirm the GC method',
                why: "The hub only processes runs whose ChemStation method it knows; it never guesses. Runs with a method it doesn't know wait until you map it." };
            case 'go_live': return {
                title: 'Choose the results file and go live',
                why: 'Going live means results from now on are written to the results file LEM reads. Before that, runs count as backfill: they are kept and processed, but never sent on unless you release them.' };
            case 'first_result': return {
                title: 'The first result',
                why: 'Run any sample on ' + name + '. It shows up in Samples within a minute and is processed like the others.' };
            default: return { title: key, why: '' };
        }
    }

    function richLine(parts) {
        return parts.map(p => (p.b ? h('b', { text: p.b }) : p.c ? h('code', { text: p.c }) : document.createTextNode(p.t)));
    }

    // ── actions per step ────────────────────────────────────────────────────
    function actions(step) {
        const id = enc(inst.id);
        const primary = step.status === 'current' ? 'btn btn-primary' : 'btn';
        const out = [];
        switch (step.key) {
            case 'create':
                out.push(h('a', { className: 'btn', href: '/instruments/' + id + '?edit=1', text: 'Change details' }));
                break;
            case 'corrections':
                out.push(h('a', { className: primary, href: '/instruments/' + id + '#corrections', text: 'Enter correction factors' }));
                break;
            case 'installer':
                out.push(h('button', { type: 'button', className: primary, 'data-testid': 'guide-installer',
                                       onclick: async () => { if (await A.downloadInstaller(inst)) refresh(); } },
                    S.icon('download'), 'Download installer for ' + inst.name));
                break;
            case 'calibration':
                out.push(h('a', { className: primary, href: '/instruments/' + id + '#calibration', text: 'Choose the calibration run' }));
                if (inst.calibration && inst.calibration.calibration_cdf) {
                    out.push(h('a', { className: 'btn btn-ghost', href: '/calibration?instrument=' + id, text: 'Assign peaks' }));
                }
                break;
            case 'method':
                for (const name of step.unmapped || []) {
                    out.push(h('button', { type: 'button', className: primary,
                                           onclick: async () => { if (await A.mapMethod(inst, name, inst.method)) refresh(); } },
                        'Process ' + name + ' as ' + inst.method));
                }
                out.push(h('a', { className: 'btn btn-ghost', href: '/instruments/' + id + '#methods', text: 'See methods' }));
                break;
            case 'go_live':
                if (step.status !== 'done') {
                    out.push(h('button', { type: 'button', className: primary, 'data-testid': 'guide-go-live',
                                           onclick: async () => { if (await A.goLiveNow(inst)) refresh(); } }, 'Go live now'));
                }
                out.push(h('a', { className: 'btn btn-ghost', href: '/instruments/' + id + '#export', text: 'Change the results file' }));
                break;
            case 'first_result':
                out.push(h('a', { className: step.status === 'current' ? 'btn btn-primary' : 'btn', href: '/', text: 'Open Samples' }));
                break;
            default:
                break;
        }
        return out;
    }

    // ── cards ───────────────────────────────────────────────────────────────
    function stepCard(step, currentN) {
        const g = guide(step.key, inst.name);
        const open = step.status === 'current';
        const by = step.status === 'done' && (step.done_at || step.done_by)
            ? [step.done_at ? U.clockTime(step.done_at, Date.now()) : null, U.actorName(step.done_by)].filter(Boolean).join(' · ')
            : '';
        const badge = step.status === 'done' ? null : h('span', { className: 'badge', text: U.stepBadge(step.status) });
        const body = [
            h('div', { className: 'top' }, h('h2', { text: g.title }), by ? h('span', { className: 'by', text: by }) : null, badge),
            h('p', { className: 'detail', text: step.detail }),
        ];
        if (open) {
            body.push(h('p', { className: 'why', text: g.why }));
            if (g.how) body.push(h('ol', { className: 'how' }, ...g.how.map(line => h('li', {}, h('span', {}, ...richLine(line))))));
            if (step.key === 'checkin') {
                body.push(h('p', { className: 'blocker' }, S.glyph('working'), h('span', { text: 'Listening for ' + inst.name + '…' })));
            }
        }
        const acts = step.status === 'blocked' ? [] : actions(step);
        if (acts.length && (open || step.status === 'waiting' || step.key === 'create')) body.push(h('div', { className: 'actions' }, ...acts));
        if (step.status === 'blocked') {
            body.push(h('p', { className: 'blocker' }, S.icon('lock'), h('span', { text: step.blocker })));
        } else if (step.status === 'waiting' && currentN) {
            body.push(h('p', { className: 'blocker' }, S.icon('info'),
                h('span', { text: 'Step ' + currentN + ' comes first, but you can do this now: runs that arrive early wait and are processed automatically.' })));
        } else if (open && step.key === 'installer') {
            body.push(h('p', { className: 'blocker' }, S.icon('info'), h('span', { text: 'A new download replaces the key in any older ' + inst.name + ' installer.' })));
        }
        return h('li', { className: 'card gstep ' + step.status, id: 'step-' + step.key, 'data-testid': 'step-' + step.key,
                         'data-status': step.status, 'aria-current': open ? 'step' : null },
            h('span', { className: 'num', 'aria-hidden': 'true' }, step.status === 'done' ? S.icon('check') : String(step.n)),
            h('div', {}, ...body));
    }

    function renderInstrument() {
        const sm = SETUP.summary;
        $('guide-title').textContent = 'Set up ' + inst.name;
        $('guide-sub').textContent = sm.ready
            ? inst.name + ' is set up. Every step is done.'
            : 'Eight steps. You can stop at any point: the hub remembers where you got to.';
        $('guide-bar').style.width = Math.round(100 * sm.done / sm.total) + '%';
        $('guide-step').textContent = U.setupLabel(sm);
        $('steps').replaceChildren(...SETUP.steps.map(s => stepCard(s, sm.step)));
        document.title = 'Setup guide · ' + inst.name + ' · GC Hub';
    }

    // ── "Add a new GC": step 1's form ───────────────────────────────────────
    function slug(name) {
        const s = String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '').slice(0, 32);
        return /^[a-z]/.test(s) ? s : (s ? 'gc' + s : '');
    }

    function renderNew() {
        $('guide-title').textContent = 'Add a new GC';
        $('guide-sub').textContent = 'Eight steps, about 20 minutes. You can stop at any point: the hub remembers where you got to.';
        $('guide-bar').style.width = '0%';
        $('guide-step').textContent = 'Step 1 of 8';
        const name = h('input', { type: 'text', maxlength: 64, placeholder: 'GC-3', required: true, 'data-testid': 'new-name', 'aria-label': 'Name' });
        const id = h('input', { type: 'text', maxlength: 32, placeholder: 'gc3', required: true, pattern: '[a-z][a-z0-9_\\-]{0,31}',
                                'data-testid': 'new-id', 'aria-label': 'Id' });
        let idTouched = false;
        id.addEventListener('input', () => { idTouched = true; });
        name.addEventListener('input', () => { if (!idTouched) id.value = slug(name.value); });
        const lem = A.lemPicker('');
        const err = h('p', { className: 'errline', role: 'alert', hidden: true });
        const form = h('form', { className: 'form', 'data-testid': 'new-form' },
            h('label', { className: 'field' }, h('span', { text: 'Name (as people call it)' }), name),
            h('label', { className: 'field' }, h('span', { text: "Id (lower case, can't change later)" }), id),
            h('label', { className: 'field wide' }, h('span', { text: 'LEM machine (optional, but LabStation routes results by it)' }), lem.el),
            err,
            h('div', { className: 'actions wide' }, h('button', { type: 'submit', className: 'btn btn-primary', text: 'Create the instrument' })));
        form.addEventListener('submit', async (ev) => {
            ev.preventDefault();
            err.hidden = true;
            const r = await S.adminPost('/api/admin/instruments', { id: id.value.trim(), name: name.value.trim(), lem_machine_uid: lem.value() },
                { quiet: true, reason: 'Adding a GC needs the admin password.' });
            if (!r) return;
            if (r.status === 201) {
                S.toast(r.body.instrument.name + ' created.');
                location.assign('/setup?instrument=' + enc(r.body.instrument.id));
                return;
            }
            err.textContent = (r.body && r.body.error) || 'HTTP ' + r.status;
            err.hidden = false;
        });
        const first = h('li', { className: 'card gstep current', id: 'step-create', 'data-testid': 'step-create', 'data-status': 'current', 'aria-current': 'step' },
            h('span', { className: 'num', 'aria-hidden': 'true', text: '1' }),
            h('div', {}, h('div', { className: 'top' }, h('h2', { text: guide('create').title }), h('span', { className: 'badge', text: 'Now' })),
                h('p', { className: 'why', text: guide('create').why }), form));
        const rest = ['corrections', 'installer', 'checkin', 'calibration', 'method', 'go_live', 'first_result'].map((key, i) =>
            h('li', { className: 'card gstep waiting', 'data-testid': 'step-' + key, 'data-status': 'waiting' },
                h('span', { className: 'num', 'aria-hidden': 'true', text: String(i + 2) }),
                h('div', {}, h('div', { className: 'top' }, h('h2', { text: guide(key).title }), h('span', { className: 'badge', text: 'Waiting' })),
                    h('p', { className: 'detail', text: guide(key).why }),
                    h('p', { className: 'blocker' }, S.icon('lock'), h('span', { text: 'Starts after step 1.' })))));
        $('steps').replaceChildren(first, ...rest);
        name.focus();
    }

    // ── loading, the picker, live ───────────────────────────────────────────
    function chosen() {
        if (params.get('new') === '1') return NEW;
        const want = params.get('instrument');
        if (want) return want;
        const nav = LIST ? U.setupNav(LIST.instruments || []) : null;
        return nav ? nav.instrument_id : NEW;
    }

    function renderPicker(current) {
        const pick = $('picker');
        const opts = (LIST.instruments || []).map(i => h('option', { value: i.id, text: i.name + (i.setup && !i.setup.ready ? ' · ' + U.setupLabel(i.setup) : '') }));
        opts.push(h('option', { value: NEW, text: 'Add a new GC' }));
        pick.replaceChildren(...opts);
        pick.value = current;
    }

    async function refresh() {
        const cur = chosen();
        if (cur === NEW || !inst) return;
        const r = await S.getJSON('/api/instruments/' + enc(inst.id) + '/setup', true);
        if (r.status === 200) { SETUP = r.body; renderInstrument(); }
        S.loadInstruments(true);
    }

    let refreshTimer = null;
    function refreshSoon() { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 300); }

    function onLive(update) {
        if (!inst) return;
        const mine = (update.agents || []).find(a => a.instrument_id === inst.id);
        const seenChanged = mine && mine.last_seen && mine.last_seen !== agentSeen;
        if (mine) agentSeen = mine.last_seen;
        if (update.reset || (update.instruments || []).includes(inst.id) || (seenChanged && SETUP && !SETUP.summary.ready)) refreshSoon();
    }

    let started = false;
    S.onInstruments(async (body) => {
        LIST = body;
        const cur = chosen();
        renderPicker(cur);
        if (started) {
            inst = (LIST.instruments || []).find(i => i.id === cur) || inst;
            return;
        }
        started = true;
        if (cur === NEW) { renderNew(); return; }
        inst = (LIST.instruments || []).find(i => i.id === cur) || null;
        if (!inst) {
            S.toast('No instrument "' + cur + '". Pick one, or add a new GC.', 'err');
            renderNew();
            $('picker').value = NEW;
            return;
        }
        agentSeen = inst.agent ? inst.agent.last_seen : null;
        const r = await S.getJSON('/api/instruments/' + enc(inst.id) + '/setup');
        if (r.status !== 200) { S.toast((r.body && r.body.error) || 'Could not load the setup steps', 'err'); return; }
        SETUP = r.body;
        renderInstrument();
        if (location.hash) {
            const el = document.getElementById(location.hash.slice(1));
            if (el) el.scrollIntoView({ block: 'center' });
        }
    });

    $('picker').addEventListener('change', (ev) => {
        const v = ev.target.value;
        location.assign(v === NEW ? '/setup?new=1' : '/setup?instrument=' + enc(v));
    });
    if (window.GCLiveAdapter) window.GCLiveAdapter.subscribe(onLive);
})();
