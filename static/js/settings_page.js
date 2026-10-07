/* settings_page.js (v5.0 lane R): the /settings page's DOM. The rules are
   settings_logic.js (GCSettings, node-tested): sections by audience, field
   validation, and a save split the way /api/settings splits it.

   * This browser: the theme (GCTheme when the shell provides it) and the
     D86 default the Results table starts with (localStorage 'gc.correctedD86').
   * Sample flags (anyone): the flagrules.js rule editor; saves
     sample_flag_rules only when the rules in effect change.
   * Best fit, Findings, Compare defaults, the LEM address (admin): saved
     with GCShell.adminPost, i.e. the page's one 15-minute unlock
     (GCAdminUnlock), never window.prompt.
   * Comparison standards (admin): tag with a GC, rename, delete, and add
     from a run's lab ID (GET /api/lab/<id> -> POST /api/comparison-standard).
   * QBench: the API key (admin; tested before it is saved) and the web
     login's status. Server paths: read-only, collapsed.
   Every string is set with textContent; answers go through GCSession.readJson. */
(function () {
    'use strict';
    const S = window.GCSettings;
    const $ = (id) => document.getElementById(id);
    const D86_KEY = 'gc.correctedD86';
    let settings = {};
    let instruments = [];

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = String(text);
        return e;
    }
    function toast(m, k) { window.GCShell.toast(m, k); }
    function msg(id, text, kind) {
        const m = $(id);
        if (!m) return;
        m.textContent = text || '';
        m.className = 'msg' + (kind ? ' ' + kind : '');
    }
    async function getJSON(path) { return window.GCShell.getJSON(path, false); }
    async function postJSON(path, body) {
        const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                                      body: JSON.stringify(body) });
        return window.GCSession.readJson(r);
    }
    function lsGet(k) { try { return window.localStorage.getItem(k); } catch (_e) { return null; } }
    function lsSet(k, v) { try { window.localStorage.setItem(k, v); } catch (_e) { /* private mode */ } }

    // A DELETE with the admin password in its JSON body (the standards route):
    // the page's one unlock, asked for in the shell's masked dialog.
    async function adminDelete(path, what) {
        const AU = window.GCAdminUnlock;
        for (let attempt = 0; attempt < 3; attempt++) {
            const pw = await AU.ask(what);
            if (!pw) return null;
            const r = await fetch(path, { method: 'DELETE', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                                          body: JSON.stringify({ password: pw }) });
            const res = await window.GCSession.readJson(r);
            if (r.status === 403) {
                AU.refused({ status: 403 });
                if (/incorrect password/i.test((res.body && res.body.error) || '')) continue;
                return res;
            }
            AU.accepted(pw);
            return res;
        }
        return null;
    }

    // ── sections, intro lines, the sub-nav ──────────────────────────────────
    function renderFrame() {
        const nav = $('set-nav');
        nav.replaceChildren(...S.SECTIONS.map((s) => {
            const a = el('a', null, s.title);
            a.href = '#' + s.id;
            a.dataset.section = s.id;
            if (s.audience === 'admin') {
                const lock = el('span', 'ico ico-lock lockmark');
                lock.setAttribute('aria-hidden', 'true');
                a.append(lock);
                a.title = s.title + ': needs the admin password';
            }
            return a;
        }));
        for (const s of S.SECTIONS) {
            const intro = document.querySelector('#' + s.id + ' .intro');
            if (intro) intro.textContent = s.intro;
        }
        // the sub-nav follows the scroll
        const secs = S.SECTIONS.map((s) => $(s.id)).filter(Boolean);
        const mark = () => {
            let cur = secs[0];
            for (const sec of secs) if (sec.getBoundingClientRect().top < 140) cur = sec;
            if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) cur = secs[secs.length - 1];
            nav.querySelectorAll('a').forEach((a) => a.classList.toggle('active', a.dataset.section === cur.id));
        };
        window.addEventListener('scroll', mark, { passive: true });
        mark();
    }

    // ── this browser ────────────────────────────────────────────────────────
    function themeChoice() {
        if (window.GCTheme && window.GCTheme.get) return window.GCTheme.get().choice;
        const v = lsGet('gc.theme');
        return v === 'dark' || v === 'system' ? v : 'light';
    }
    function syncTheme() {
        const c = themeChoice();
        document.querySelectorAll('#theme-choice [data-choice]').forEach((b) =>
            b.setAttribute('aria-checked', b.dataset.choice === c ? 'true' : 'false'));
    }
    function wireBrowser() {
        document.querySelectorAll('#theme-choice [data-choice]').forEach((b) => b.addEventListener('click', () => {
            if (window.GCTheme && window.GCTheme.set) window.GCTheme.set(b.dataset.choice);
            else { lsSet('gc.theme', b.dataset.choice); if (window.GCShell.applyTheme) window.GCShell.applyTheme(); }
            syncTheme();
        }));
        document.addEventListener('gc:theme', syncTheme);
        syncTheme();
        const d86 = $('d86-default');
        d86.checked = lsGet(D86_KEY) === '1';
        d86.addEventListener('change', () => {
            lsSet(D86_KEY, d86.checked ? '1' : '0');
            toast(d86.checked ? 'Results will show D86 with correction factors.' : 'Results will show D86 before correction factors.');
        });
    }

    // ── sample flags ────────────────────────────────────────────────────────
    function ruleRow(rule, i) {
        const row = el('div', 'rule');
        row.dataset.color = rule.color || '';
        row.setAttribute('data-testid', 'flag-rule');
        const lab = (text, input) => { const l = el('label', 'rf'); l.append(el('span', 'visually-hidden', text), input); return l; };
        const on = el('input'); on.type = 'checkbox'; on.className = 'r-on'; on.checked = !!rule.enabled;
        const sw = el('label', 'switch');
        sw.append(on, el('span', 'track'), el('span', 'visually-hidden', 'Rule ' + (i + 1) + ' on'));
        const name = el('input'); name.type = 'text'; name.className = 'r-name'; name.value = rule.name || ''; name.maxLength = 60;
        const cond = el('select', 'r-cond');
        for (const [v, t] of [['above', 'Above'], ['below', 'Below']]) { const o = el('option', null, t); o.value = v; cond.append(o); }
        cond.value = rule.condition === 'below' ? 'below' : 'above';
        const num = (cls, v, step, label) => { const n = el('input'); n.type = 'number'; n.className = cls; n.value = v; n.step = step; n.setAttribute('aria-label', label); return n; };
        const thr = num('r-thr', rule.threshold, '100', 'Rule ' + (i + 1) + ' threshold');
        const t0 = num('r-t0', rule.t_start, '0.1', 'Rule ' + (i + 1) + ' window start, min');
        const t1 = num('r-t1', rule.t_end, '0.1', 'Rule ' + (i + 1) + ' window end, min');
        const del = el('button', 'icon-btn');
        del.type = 'button';
        del.setAttribute('aria-label', 'Remove rule ' + (i + 1));
        del.title = 'Remove';
        del.append(el('span', 'ico ico-x'));
        del.addEventListener('click', () => { row.remove(); renumber(); });
        row.append(sw, lab('Rule ' + (i + 1) + ' name', name), lab('Rule ' + (i + 1) + ' condition', cond), thr,
            el('span', 'caption', 'from'), t0, el('span', 'caption', 'to'), t1, el('span', 'caption', 'min'), del);
        return row;
    }
    function renumber() {
        const rows = [...document.querySelectorAll('#rules .rule')];
        const rules = rows.map(readRule);
        $('rules').replaceChildren(...rules.map(ruleRow));
    }
    function readRule(row) {
        const q = (c) => row.querySelector(c);
        return { name: q('.r-name').value.trim(), condition: q('.r-cond').value, threshold: q('.r-thr').value,
                 t_start: q('.r-t0').value, t_end: q('.r-t1').value, color: row.dataset.color || '#e67e22',
                 enabled: q('.r-on').checked };
    }
    function renderRules() {
        const rules = window.effectiveFlagRules(settings);
        $('rules').replaceChildren(...rules.map(ruleRow));
    }
    async function saveFlags() {
        const rules = [...document.querySelectorAll('#rules .rule')].map(readRule);
        const plan = S.planSave(settings, {}, rules);
        if (plan.errors.sample_flag_rules) { msg('flags-msg', plan.errors.sample_flag_rules, 'err'); return; }
        const body = S.saveBody(plan, false);
        if (!body) { msg('flags-msg', 'Nothing changed.'); return; }
        msg('flags-msg', 'Saving…');
        const res = await postJSON('/api/settings', body);
        if (res.status !== 200) { msg('flags-msg', (res.body && res.body.error) || ('Not saved (HTTP ' + res.status + ').'), 'err'); return; }
        settings = res.body;
        renderRules();
        msg('flags-msg', 'Saved. Lists re-check their flags in the background.', 'ok');
        stamp();
    }

    // ── admin fields ────────────────────────────────────────────────────────
    function renderFields(section) {
        const box = $('fields-' + section);
        if (!box) return;
        box.replaceChildren(...S.fieldsFor(section).map((f) => {
            const wrap = el('div', 'field' + (f.kind === 'url' ? ' wide' : ''));
            const id = 'set-' + f.key;
            const value = settings[f.key] === undefined || settings[f.key] === null ? f.def : String(settings[f.key]);
            let input;
            if (f.kind === 'bool') {
                input = el('input'); input.type = 'checkbox'; input.checked = value.trim().toLowerCase() === 'true';
                const sw = el('label', 'switch'); sw.htmlFor = id;
                input.id = id; input.dataset.key = f.key;
                sw.append(input, el('span', 'track'), el('span', null, f.label));
                wrap.append(sw);
            } else {
                const l = el('label', null, f.label); l.htmlFor = id;
                input = el('input'); input.type = 'text'; input.id = id; input.value = value; input.dataset.key = f.key;
                input.inputMode = f.kind === 'number' ? 'decimal' : 'url';
                input.spellcheck = false;
                if (f.optional) input.placeholder = 'Moderate';
                wrap.append(l, input);
                if (f.unit) wrap.append(el('span', 'caption', f.unit));
            }
            const err = el('span', 'errline field-err');
            err.id = id + '-err';
            err.hidden = true;
            input.setAttribute('aria-describedby', err.id);
            wrap.append(err);
            input.addEventListener('input', () => { err.hidden = true; input.removeAttribute('aria-invalid'); });
            return wrap;
        }));
    }
    function readSection(section) {
        const out = {};
        document.querySelectorAll('#fields-' + section + ' [data-key]').forEach((i) => {
            out[i.dataset.key] = i.type === 'checkbox' ? (i.checked ? 'true' : 'false') : i.value;
        });
        return out;
    }
    async function saveSection(section) {
        const plan = S.planSave(settings, readSection(section));
        let bad = false;
        for (const [key, problem] of Object.entries(plan.errors)) {
            const input = $('set-' + key);
            const err = $('set-' + key + '-err');
            if (input && err) { err.textContent = problem; err.hidden = false; input.setAttribute('aria-invalid', 'true'); bad = true; }
        }
        if (bad) { msg(section + '-msg', 'Check the marked field.', 'err'); return; }
        const body = S.saveBody(plan, true);
        if (!body) { msg(section + '-msg', 'Nothing changed.'); return; }
        msg(section + '-msg', 'Saving…');
        const res = await window.GCShell.adminPost('/api/settings', body,
            { reason: 'Saving ' + S.section(section).title.toLowerCase() + ' needs the admin password. It stays unlocked in this tab for 15 minutes, never saved.', quiet: true });
        if (!res) { msg(section + '-msg', 'Not saved.'); return; }
        if (res.status !== 200) { msg(section + '-msg', (res.body && res.body.error) || ('Not saved (HTTP ' + res.status + ').'), 'err'); return; }
        settings = res.body;
        renderFields(section);
        msg(section + '-msg', 'Saved.', 'ok');
        stamp();
    }

    // ── comparison standards ────────────────────────────────────────────────
    let standards = [];
    async function loadStandards() {
        const res = await getJSON('/api/standards');
        standards = (res.status === 200 && res.body && Array.isArray(res.body.standards)) ? res.body.standards : [];
        renderStandards();
    }
    function renderStandards() {
        const tbody = $('std-rows');
        tbody.replaceChildren(...standards.map((s) => {
            const tr = el('tr');
            tr.setAttribute('data-testid', 'standard-row');
            const name = el('td', 'std-name');
            name.append(el('b', null, s.name));
            if (s.file_name) name.append(el('span', 'caption mono std-file', s.file_name));
            if (s.missing) name.append(el('span', 'errline', 'The file is missing from the standards folder.'));
            const tag = el('td');
            const sel = el('select');
            sel.setAttribute('aria-label', 'GC of ' + s.name);
            const none = el('option', null, 'Not tagged'); none.value = '';
            sel.append(none, ...instruments.map((i) => { const o = el('option', null, i.name || i.id); o.value = i.id; return o; }));
            if (s.instrument_id && !instruments.some((i) => i.id === s.instrument_id)) {
                const o = el('option', null, S.standardTagText(s)); o.value = s.instrument_id; sel.append(o);
            }
            sel.value = s.instrument_id || '';
            sel.addEventListener('change', () => tagStandard(s, sel));
            tag.append(sel);
            const act = el('td', 'std-act');
            const ren = el('button', 'btn btn-ghost btn-sm', 'Rename'); ren.type = 'button';
            const del = el('button', 'btn btn-ghost btn-sm btn-danger', 'Delete'); del.type = 'button';
            ren.addEventListener('click', () => renameForm(tr, s));
            del.addEventListener('click', () => deleteConfirm(tr, s));
            act.append(ren, del);
            tr.append(name, tag, act);
            return tr;
        }));
        $('std-empty').hidden = standards.length > 0;
    }
    async function tagStandard(s, sel) {
        const res = await window.GCShell.adminPost('/api/admin/standards/' + encodeURIComponent(s.id) + '/instrument',
            { instrument_id: sel.value || null }, { reason: 'Tagging a standard needs the admin password.', quiet: true });
        if (!res || res.status !== 200) {
            sel.value = s.instrument_id || '';
            if (res) msg('std-msg', (res.body && res.body.error) || ('Not saved (HTTP ' + res.status + ').'), 'err');
            return;
        }
        msg('std-msg', s.name + ': ' + (sel.value ? 'tagged ' + sel.options[sel.selectedIndex].textContent : 'not tagged') + '.', 'ok');
        loadStandards();
    }
    function inlineRow(tr, cells) {
        const row = el('tr', 'inline');
        const td = el('td');
        td.colSpan = 3;
        const box = el('div', 'row');
        box.append(...cells);
        td.append(box);
        row.append(td);
        const old = tr.nextElementSibling;
        if (old && old.classList.contains('inline')) old.remove();
        tr.after(row);
        return row;
    }
    function renameForm(tr, s) {
        const input = el('input'); input.type = 'text'; input.value = s.name; input.maxLength = 120;
        input.setAttribute('aria-label', 'New name for ' + s.name);
        const ok = el('button', 'btn btn-primary btn-sm', 'Rename'); ok.type = 'button';
        const cancel = el('button', 'btn btn-ghost btn-sm', 'Cancel'); cancel.type = 'button';
        const err = el('span', 'msg');
        const row = inlineRow(tr, [input, ok, cancel, err]);
        input.focus();
        input.select();
        cancel.addEventListener('click', () => row.remove());
        const go = async () => {
            const name = input.value.trim();
            const problem = S.standardNameProblem(name);
            if (problem) { err.textContent = problem; err.className = 'msg err'; return; }
            if (name === s.name) { row.remove(); return; }
            const res = await window.GCShell.adminPost('/api/comparison-standard/rename', { old_name: s.name, new_name: name },
                { reason: 'Renaming a standard needs the admin password.', quiet: true });
            if (!res) return;
            if (res.status !== 200) { err.textContent = (res.body && res.body.error) || ('Not renamed (HTTP ' + res.status + ').'); err.className = 'msg err'; return; }
            row.remove();
            msg('std-msg', 'Renamed to ' + name + '.', 'ok');
            loadStandards();
        };
        ok.addEventListener('click', go);
        input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); go(); } if (ev.key === 'Escape') row.remove(); });
    }
    function deleteConfirm(tr, s) {
        const text = el('span', 'warnline', 'Delete ' + s.name + '? Its file leaves the standards folder; reports already made keep their numbers.');
        const ok = el('button', 'btn btn-sm btn-danger', 'Delete ' + s.name); ok.type = 'button';
        const cancel = el('button', 'btn btn-ghost btn-sm', 'Cancel'); cancel.type = 'button';
        const row = inlineRow(tr, [text, ok, cancel]);
        cancel.focus();
        cancel.addEventListener('click', () => row.remove());
        ok.addEventListener('click', async () => {
            const res = await adminDelete('/api/comparison-standard/' + encodeURIComponent(s.name), 'delete ' + s.name);
            if (!res) return;
            if (res.status !== 200) { msg('std-msg', (res.body && res.body.error) || ('Not deleted (HTTP ' + res.status + ').'), 'err'); return; }
            row.remove();
            msg('std-msg', 'Deleted ' + s.name + '.', 'ok');
            loadStandards();
        });
    }
    async function addStandard(ev) {
        ev.preventDefault();
        const lab = $('add-lab').value.trim();
        const name = $('add-name').value.trim();
        const problem = S.standardNameProblem(name);
        if (!lab) { msg('std-msg', 'The lab ID of a run, please.', 'err'); return; }
        if (problem) { msg('std-msg', problem, 'err'); return; }
        const found = await getJSON('/api/lab/' + encodeURIComponent(lab));
        if (found.status !== 200 || !found.body || !found.body.sample_id) {
            msg('std-msg', 'No run with lab ID ' + lab + ' was found.', 'err');
            return;
        }
        const res = await window.GCShell.adminPost('/api/comparison-standard', { sample_id: found.body.sample_id, name },
            { reason: 'Adding a standard needs the admin password.', quiet: true });
        if (!res) return;
        if (res.status !== 200) { msg('std-msg', (res.body && res.body.error) || ('Not added (HTTP ' + res.status + ').'), 'err'); return; }
        $('add-lab').value = '';
        $('add-name').value = '';
        msg('std-msg', 'Added ' + name + ' from the newest run of ' + lab + '. Tag it with its GC.', 'ok');
        loadStandards();
    }

    // ── QBench ──────────────────────────────────────────────────────────────
    function renderQb(status) {
        $('qb-status').textContent = window.qbApiStatusText(status);
        $('qb-glyph').className = 'glyph ' + (status && status.configured ? 'final' : 'never');
        $('qb-store').textContent = status && status.store_path ? 'Saved on the server in ' + status.store_path + '.' : '';
    }
    async function loadQb() {
        const [api, web] = await Promise.all([getJSON('/api/qbench-api-credentials'), getJSON('/api/qbench-credentials')]);
        if (api.status === 200) renderQb(api.body);
        else $('qb-status').textContent = 'Unknown (' + ((api.body && api.body.error) || 'HTTP ' + api.status) + ')';
        $('qb-web').textContent = web.status === 200 ? S.webLoginText(web.body) : 'Unknown (HTTP ' + web.status + ')';
    }
    async function saveQb(ev) {
        ev.preventDefault();
        const id = $('qb-id').value.trim();
        const secret = $('qb-secret').value;
        if (!id || !secret) { msg('qb-msg', 'Both the client ID and the secret, please.', 'err'); return; }
        $('qb-save').disabled = true;
        msg('qb-msg', 'Checking with QBench…');
        try {
            const res = await window.GCShell.adminPost('/api/qbench-api-credentials', { client_id: id, client_secret: secret },
                { reason: 'Changing the QBench API key needs the admin password.', quiet: true });
            $('qb-secret').value = '';
            if (!res) { msg('qb-msg', 'Not saved.'); return; }
            if (res.status !== 200) { msg('qb-msg', (res.body && res.body.error) || ('Not saved (HTTP ' + res.status + ').'), 'err'); return; }
            $('qb-id').value = '';
            renderQb(res.body);
            msg('qb-msg', res.body.source === 'environment'
                ? 'Saved, but environment variables on the server override it.' : 'QBench accepted the key. Saved.', 'ok');
        } finally {
            $('qb-save').disabled = false;
        }
    }

    // ── server ──────────────────────────────────────────────────────────────
    function renderPaths() {
        const dl = $('path-list');
        const facts = S.serverPaths(settings);
        dl.replaceChildren(...facts.flatMap((f) => [el('div', 'k', f.label), el('div', 'v mono', f.value)]));
    }

    function stamp() {
        const d = new Date();
        $('saved-state').textContent = 'Saved at ' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    }

    async function loadSettings() {
        const res = await getJSON('/api/settings');
        if (res.status !== 200 || !res.body) {
            toast((res.body && res.body.error) || ('Settings could not be loaded (HTTP ' + res.status + ').'), 'err');
            return;
        }
        settings = res.body;
        renderRules();
        ['bestfit', 'findings', 'compare', 'lem'].forEach(renderFields);
        renderPaths();
    }

    document.addEventListener('DOMContentLoaded', () => {
        if (!$('set-nav')) return;
        renderFrame();
        wireBrowser();
        $('add-rule').addEventListener('click', () => {
            const rows = [...document.querySelectorAll('#rules .rule')];
            $('rules').append(ruleRow(window.newFlagRule(rows.length), rows.length));
        });
        $('save-flags').addEventListener('click', saveFlags);
        document.querySelectorAll('[data-save]').forEach((b) => b.addEventListener('click', () => saveSection(b.dataset.save)));
        $('add-std').addEventListener('submit', addStandard);
        $('qb-form').addEventListener('submit', saveQb);
        window.GCShell.onInstruments((body) => {
            instruments = (body && body.instruments) || [];
            renderStandards();
        });
        loadSettings();
        loadStandards();
        loadQb();
    });
})();
