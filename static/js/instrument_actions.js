// Admin actions shared by the v3.1 instrument page and the setup guide
// (GCActions): the LEM machine dropdown, Edit details, Download installer
// (with the confirm-revoke step), Revoke key, Go live now and method mapping.
// They call the EXISTING /api/admin/... routes through GCShell.adminPost (the
// admin password lives in a closure for 15 minutes). DOM is text only.
(function () {
    'use strict';
    const L = window.InstrumentsLogic;
    const S = window.GCShell;
    const h = S.h;
    const enc = encodeURIComponent;

    // ── the LEM machine dropdown (D10), one GET per page load once it answered ──
    let LEM_ANSWER = null;
    function lemMachines(refresh) {
        if (!LEM_ANSWER || refresh) {
            const pending = S.getJSON('/api/lem/machines')
                .then(r => (r.status === 200 ? r.body : null))
                .catch(() => null)
                .then(answer => {
                    if (!L.lemAnswerCacheable(answer) && LEM_ANSWER === pending) LEM_ANSWER = null;
                    return answer;
                });
            LEM_ANSWER = pending;
        }
        return LEM_ANSWER;
    }

    function lemPicker(saved) {
        const start = saved || '';
        const select = h('select', { disabled: true, 'aria-label': 'LEM machine', 'data-testid': 'lem-select' },
            h('option', { text: 'Loading LEM machines…' }));
        const other = h('input', { type: 'text', maxlength: 128, value: start, hidden: true,
                                   placeholder: 'LEM machine uid', 'aria-label': 'LEM machine uid' });
        const note = h('span', { className: 'caption', hidden: true });
        let options = null;
        const chosen = () => (options ? options[select.selectedIndex] : null);
        select.addEventListener('change', () => {
            const o = chosen();
            other.hidden = !(o && o.other);
            if (!other.hidden) other.focus();
        });
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
        };
    }

    // ── Edit details (a sheet) ──────────────────────────────────────────────
    function editDetails(inst, hubMethods, onSaved) {
        const name = h('input', { type: 'text', value: inst.name, maxlength: 64, 'data-testid': 'edit-name' });
        const method = h('select', {}, ...(hubMethods.length ? hubMethods : [inst.method]).map(m => h('option', { value: m, text: m })));
        method.value = inst.method;
        const enabled = h('input', { type: 'checkbox', checked: !!inst.enabled });
        const live = h('input', { type: 'datetime-local', step: 1, value: L.liveSinceInput(inst.live_since) });
        const lem = lemPicker(inst.lem_machine_uid || '');
        const err = h('p', { className: 'errline', role: 'alert', hidden: true });
        const dlg = h('dialog', { className: 'sheet', 'data-testid': 'edit-dialog' });
        const cancel = h('button', { type: 'button', className: 'btn', text: 'Cancel', onclick: () => dlg.close() });
        const saveBtn = h('button', { type: 'submit', className: 'btn btn-primary', text: 'Save' });
        const form = h('form', { className: 'form' },
            h('h2', { text: 'Edit ' + inst.name }),
            h('label', { className: 'field' }, h('span', { text: 'Name' }), name),
            h('label', { className: 'field' }, h('span', { text: 'Method' }), method),
            h('label', { className: 'field' }, h('span', { text: 'LEM machine (optional; LabStation routes results by it)' }), lem.el),
            h('label', { className: 'field' }, h('span', { text: "Live since (the GC's own clock). Empty: everything is backfill" }), live),
            h('label', { className: 'check' }, enabled, h('span', { text: "Enabled (a disabled instrument's agent is refused)" })),
            err,
            h('div', { className: 'actions' }, cancel, saveBtn));
        form.addEventListener('submit', async (ev) => {
            ev.preventDefault();
            err.hidden = true;
            const ls = L.liveSinceValue(live.value);
            if (ls.error) { err.textContent = ls.error; err.hidden = false; return; }
            const payload = { name: name.value, method: method.value, enabled: enabled.checked,
                              lem_machine_uid: lem.value() };
            if (ls.value !== (inst.live_since || '')) {
                const ask = L.liveSinceConfirm(inst.live_since, ls.value, L.localNow());
                if (ask && !window.confirm(ask)) return;
                payload.live_since = ls.value;
            }
            const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id), payload);
            if (!r || r.status !== 200) return;
            dlg.close();
            const warn = (r.body.warnings || []);
            S.toast(warn.length ? 'Saved. ' + warn.join(' ') : 'Saved.');
            if (onSaved) onSaved(r.body.instrument);
        });
        dlg.appendChild(form);
        dlg.addEventListener('close', () => dlg.remove());
        document.body.appendChild(dlg);
        dlg.showModal();
        name.focus();
    }

    // ── installer / key ─────────────────────────────────────────────────────
    async function downloadInstaller(inst, confirmRevoke) {
        const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/installer',
            { confirm_revoke: !!confirmRevoke }, { raw: true, reason: 'Downloading the installer creates ' + inst.name + "'s agent key, so it needs the admin password." });
        if (!r || r.status === 403) return false;
        let body = null;
        if (r.status !== 200) { try { body = await r.json(); } catch (_e) { body = null; } }
        const out = L.installerOutcome(r.status, body);
        if (out.kind === 'download') {
            const blob = await r.blob();
            const url = URL.createObjectURL(blob);
            const a = h('a', { href: url, download: 'gc-agent-installer-' + inst.id + '.zip' });
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 10000);
            S.toast('Installer downloaded: gc-agent-installer-' + inst.id + '.zip. Copy it to the ' + inst.name + ' computer and run Install.');
            return true;
        }
        if (out.kind === 'confirm') {
            if (window.confirm(out.message)) return downloadInstaller(inst, true);
            return false;
        }
        S.toast(out.message, 'err');
        return false;
    }

    async function revokeKey(inst) {
        if (!window.confirm('Revoke ' + inst.name + "'s agent key? Its agent stops sending until it gets a new installer.")) return false;
        const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/revoke-token', {});
        if (r && r.status === 200) { S.toast('Key revoked.'); return true; }
        return false;
    }

    async function agentCommand(inst, cmd) {
        const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/agent-command', { command: cmd });
        if (r && r.status === 200) { S.toast('"' + cmd + '" sent: the agent takes it at its next check-in.'); return true; }
        return false;
    }

    // ── go live ─────────────────────────────────────────────────────────────
    async function goLiveNow(inst) {
        const now = L.localNow();
        const text = 'Go live now? Runs ' + inst.name + ' injects from ' + now.slice(0, 16) +
            " (by the GC's clock) are written to the results file LEM reads. Runs before that stay backfill.";
        if (!window.confirm(text)) return false;
        const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id), { live_since: now });
        if (!r || r.status !== 200) return false;
        const warn = (r.body.warnings || []).filter(w => !/applies to samples received from now on/.test(w));
        S.toast(warn.length ? 'Live. ' + warn.join(' ') : inst.name + ' is live.');
        return true;
    }

    async function mapMethod(inst, name, hubMethod) {
        const verb = hubMethod ? 'Process runs with ' + name + ' as ' + hubMethod + '? The runs waiting with it are processed now.'
            : 'Stop processing ' + name + '? Existing results stay; new runs with it wait as another method.';
        if (!window.confirm(verb)) return false;
        const r = await S.adminPost('/api/admin/instruments/' + enc(inst.id) + '/methods',
            { method_name: name, hub_method: hubMethod });
        if (r && r.status === 200) { S.toast(hubMethod ? (r.body.queued || 0) + ' run(s) queued.' : 'Unmapped.'); return true; }
        return false;
    }

    window.GCActions = { lemMachines, lemPicker, editDetails, downloadInstaller, revokeKey, agentCommand, goLiveNow, mapMethod };
})();
