/* samples_page.js (v5.0.0 lane S): the Samples page's DOM and data.

   The address bar is the link: SamplesRouter parses location on load and on
   popstate, and every change of sample, view, filter, search or sort is
   written back with history.pushState (typing in the filter, keyboard steps
   and Compare's standard use replaceState). Rules live in the node-tested
   modules (samples_router.js, samples_logic.js, sample_order.js, samples.js,
   distill_view.js, ladder.js); this file only fetches (GCSession.readJson on
   every answer, GCLive.bgFetch for what the page fetches on its own) and
   builds the DOM with textContent.

   Lane C (compare_view.js, from _compare_assets.html): Compare mounts
   GCCompare.mount(el, {sample, standards, settings, onUrlChange}) ->
   {unmount(), setStandard(name), ...}; the header's Export report and Add to
   queue are GCCompare.openExportSheet / GCCompare.addToQueue (the mounted
   view's parameters when Compare is open, the saved defaults otherwise); the
   bulk "Add to report queue" is GCReportQueue.addMany with each run's
   GCCompare.defaultStandard; the gc:adjust event (the Adjust drawer) hides
   the list.
   Themes: GCTheme's gc:theme event (and any data-theme change) re-themes
   Plotly from the CSS tokens.
   v6: the chart draws or says why (#chrom-msg, Retry; the plot purged
   before any message), the row's checkbox only shows S.sel, and Add trace
   stacks other samples and comparison standards on the open sample's chart. */
(function () {
    'use strict';

    const R = window.SamplesRouter;
    const L = window.SamplesLogic;
    const O = window.SampleOrder;
    const SH = window.GCShell;
    const h = SH.h;
    const $ = (id) => document.getElementById(id);
    const PAGE = 500;
    const D86_KEY = 'gc.correctedD86';      // shared with Results, Settings and classic

    const S = {
        route: R.parse(location.pathname, location.search),
        files: [], total: 0, loading: false, listSeq: 0,
        instruments: [], names: {},
        standards: [], settings: null, standardsReady: null, table: null,
        sel: L.selection(), backfillInfo: {},
        overlay: null, ovTraces: new Map(),              // v6: the traces stacked on the chart (key -> {trace}|{error}|{loading})
        row: null, meta: null, cache: new Map(),       // sample id -> {meta, trace, curve, lab}
        detailSeq: 0, compare: null, compareFor: null, mountSeq: 0,
        corrected: loadBool(D86_KEY, false),
        counts: { held: 0, error: 0, today: 0 },
        bulkRunning: false, bulkStop: false,
    };

    function loadBool(key, dflt) {
        try { const v = localStorage.getItem(key); return v === null ? dflt : v === '1'; } catch (_e) { return dflt; }
    }
    function saveBool(key, v) {
        try { localStorage.setItem(key, v ? '1' : '0'); } catch (_e) { /* not remembered */ }
    }

    // ── fetching ────────────────────────────────────────────────────────
    // Never throws: a network failure (the hub restarting, the tunnel down) is
    // {ok: false, status: 0} with its words, so every caller shows it (v6:
    // a rejected fetch used to leave the chart on "Loading…" for good).
    async function getJSON(url, background) {
        const opts = { cache: 'no-store', headers: { Accept: 'application/json' } };
        try {
            const r = background && window.GCLive ? await window.GCLive.bgFetch(url, opts) : await fetch(url, opts);
            const res = await window.GCSession.readJson(r);
            return { ok: r.ok, status: r.status, body: res.body || {} };
        } catch (e) {
            return { ok: false, status: 0, body: { error: (e && e.message) || 'no answer' } };
        }
    }
    async function postJSON(url, body) {
        try {
            const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                                         body: JSON.stringify(body || {}) });
            const res = await window.GCSession.readJson(r);
            return { ok: r.ok, status: r.status, body: res.body || {} };
        } catch (e) {
            return { ok: false, status: 0, body: { error: (e && e.message) || 'no answer' } };
        }
    }
    function errText(res, what) {
        const err = res.body && res.body.error;
        if (!res.status) return what + ' failed: the hub did not answer' + (err ? ' (' + err + ').' : '.');
        return err || (what + ' failed (HTTP ' + res.status + ').');
    }

    // ── the URL ─────────────────────────────────────────────────────────
    function go(change, how) {
        const before = R.build(S.route);
        const filters = Object.assign({}, S.route.filters, change.filters || {});
        S.route = Object.assign({}, S.route, change, { filters, legacy: false });
        if (S.route.view !== 'compare') S.route.standard = null;
        const url = R.build(S.route);
        if (url !== before) {
            if (how === 'replace') history.replaceState(null, '', url);
            else history.pushState(null, '', url);
        }
    }

    // ── the list ────────────────────────────────────────────────────────
    function today() {
        return (window.GCLive && window.GCLive.serverToday && window.GCLive.serverToday()) || new Date();
    }
    function listQuery(extra) {
        const q = R.filesQuery(S.route.filters);
        return [q, extra].filter(Boolean).join('&');
    }

    async function loadList(opts) {
        const seq = ++S.listSeq;
        const more = opts && opts.more;
        S.loading = true;
        const offset = more ? S.files.length : 0;
        const res = await getJSON('/api/files?' + listQuery('limit=' + PAGE + '&offset=' + offset), opts && opts.background);
        if (seq !== S.listSeq) return;
        S.loading = false;
        if (!res.ok) { renderListError(errText(res, 'Loading the samples')); return; }
        S.files = more ? S.files.concat(res.body.samples || []) : (res.body.samples || []);
        S.total = Number(res.body.total) || 0;
        S.sel = L.keepOnly(S.sel, S.files.map(f => f.sample_id));
        renderList();
        loadCounts(opts && opts.background);
    }

    async function loadCounts(background) {
        const f = S.route.filters;
        const base = R.filesQuery({ instrument: f.instrument, q: f.q, status: [], notsent: false });
        const one = (q) => getJSON('/api/files?' + [base, q, 'limit=1'].filter(Boolean).join('&'), background);
        const day = today();
        const dayKey = typeof day === 'string' ? day : day.toISOString().slice(0, 10);
        const [held, error, todayRes] = await Promise.all([
            one('status=' + encodeURIComponent(R.statusList(['held']).join(','))),
            one('status=error'),
            getJSON('/api/files?' + [listQuery(), 'date_from=' + dayKey, 'limit=1'].filter(Boolean).join('&'), background),
        ]);
        S.counts = { held: held.ok ? held.body.total : 0, error: error.ok ? error.body.total : 0,
                     today: todayRes.ok ? todayRes.body.total : 0 };
        renderCounts();
        renderFilters();
    }

    function renderListError(msg) {
        $('rows').replaceChildren(h('p', { className: 'list-empty', role: 'alert', text: msg }));
    }

    function renderFilters(fromUrl) {
        const f = S.route.filters;
        const inst = $('inst-chips');
        const chips = [h('button', { type: 'button', className: 'chip', 'aria-pressed': f.instrument.length ? 'false' : 'true',
                                      'data-testid': 'chip-inst-all', text: 'All instruments',
                                      onclick: () => setFilters({ instrument: [] }) })];
        for (const i of S.instruments) {
            chips.push(h('button', { type: 'button', className: 'chip', 'data-testid': 'chip-inst-' + i.id,
                                     'aria-pressed': f.instrument.length === 1 && f.instrument[0] === i.id ? 'true' : 'false',
                                     text: i.name || i.id, onclick: () => setFilters({ instrument: [i.id] }) }));
        }
        inst.replaceChildren(...chips);
        const st = $('status-chips');
        const toggleStatus = (g) => {
            const cur = f.status.slice();
            const i = cur.indexOf(g);
            if (i >= 0) cur.splice(i, 1); else cur.push(g);
            setFilters({ status: cur });
        };
        const label = (g) => {
            const word = { final: 'Final', held: 'Held', error: 'Error', processing: 'Processing' }[g];
            const n = g === 'held' ? S.counts.held : g === 'error' ? S.counts.error : 0;
            return n ? word + ' · ' + L.number(n) : word;
        };
        const sc = ['held', 'error', 'processing', 'final'].map(g => h('button', {
            type: 'button', className: 'chip', 'data-testid': 'chip-status-' + g,
            'aria-pressed': f.status.includes(g) ? 'true' : 'false',
            title: g === 'held' || g === 'error' ? 'Show the ' + g + ' samples (the count ignores the status filter)' : null,
            text: label(g), onclick: () => toggleStatus(g) }));
        sc.push(h('button', { type: 'button', className: 'chip', 'data-testid': 'chip-notsent',
                              'aria-pressed': f.notsent ? 'true' : 'false', text: 'Not sent to QBench',
                              onclick: () => setFilters({ notsent: !S.route.filters.notsent }) }));
        st.replaceChildren(...sc);
        const q = $('filter-q');
        if ((fromUrl || document.activeElement !== q) && q.value !== f.q) q.value = f.q;
        document.querySelectorAll('#sort-switch [data-sort]').forEach(b =>
            b.setAttribute('aria-checked', b.dataset.sort === f.sort ? 'true' : 'false'));
    }

    function renderCounts() {
        $('list-count').textContent = L.countsText({ total: S.total, today: S.counts.today });
    }

    function setFilters(change, how) {
        go({ filters: change }, how);
        S.sel = L.clear();
        renderFilters();
        loadList();
    }

    function shownOrder() {
        const groups = O.groupSamples(S.files, S.route.filters.sort, today());
        return { groups, order: groups.flatMap(g => g.items.map(f => f.sample_id)) };
    }

    /** rowStatus, with the backfill reason (why it is backfill) once the
        instrument's live-since facts are in (v6). */
    function statusOf(f) {
        const st = L.rowStatus(f);
        if (f && f.backfill && !f.released && st.group === 'final') {
            const info = S.backfillInfo[f.instrument];
            st.reason = L.backfillReason(f, info ? Object.assign({}, info, { name: S.names[f.instrument] || f.instrument }) : null,
                                         window.GCBackfill ? window.GCBackfill.whyText : null);
        }
        return st;
    }

    function rowEl(f, underDay) {
        const st = statusOf(f);
        const id = f.sample_id;
        const checked = L.isChecked(S.sel, id);
        const time = O.rowTime(f, { underDay });
        // the box only shows the selection (S.sel): it takes no clicks of its
        // own (samples.css), the row's .lead handles the press (v6, bug 5)
        const box = h('input', { type: 'checkbox', 'aria-label': 'Select ' + (f.display_name || f.lab_id),
                                 'data-testid': 'row-check', tabindex: '-1' });
        box.checked = checked;
        box.defaultChecked = checked;
        const line2 = [];
        // the action first: a long review note or reason never pushes it out of the row (v5.1.0 follow-up)
        if (st.fix) line2.push(fixEl(st.fix, f));
        if (st.group === 'final' && !st.reason) {
            const det = L.rowDetail(f);
            if (det) line2.push(h('span', { className: 'detail', text: det }));
        } else {
            line2.push(h('span', { className: 'detail reason-' + (st.group === 'error' ? 'error' : st.group === 'held' ? 'held' : 'work'),
                                   title: st.reason, 'data-testid': 'row-reason', text: st.reason }));
        }
        const flags = L.flagText(f);
        if (flags) line2.push(h('span', { className: 'flag', text: '⚑ ' + flags }));
        const review = L.reviewText(f);
        if (review) line2.push(h('span', { className: 'review', title: review, 'data-testid': 'row-review', text: review }));
        const tags = [h('span', { className: 'tag', text: S.names[f.instrument] || f.instrument || '' })];
        if (f.time_corrected) tags.push(h('span', { className: 'tag', title: 'Injection time corrected', text: 'time fixed' }));
        const el = h('div', {
            className: 'srow' + (S.route.sampleId === id ? ' active' : '') + (checked ? ' checked' : ''),
            role: 'option', 'aria-selected': S.route.sampleId === id ? 'true' : 'false',
            'data-sample-id': String(id), 'data-testid': 'sample-row', 'data-status': st.group,
        },
        h('span', { className: 'lead' }, SH.glyph(st.glyph, st.text), box),
        h('div', { className: 'l1' }, h('span', { className: 'lab', text: f.display_name || f.lab_id || '#' + id }), ...tags),
        h('span', { className: 'when', title: time.title, text: time.text }),
        h('div', { className: 'l2' }, ...line2));
        return el;
    }

    function fixEl(fix, f) {
        if (fix.href) {
            return h('a', { className: 'fix', href: fix.href, 'data-testid': 'row-fix', text: fix.label + ' →',
                            onclick: (ev) => ev.stopPropagation() });
        }
        return h('button', { type: 'button', className: 'fix', 'data-testid': 'row-fix', text: fix.label,
                             onclick: (ev) => { ev.stopPropagation(); reprocess([f.sample_id]); } });
    }

    function renderList() {
        const rows = $('rows');
        const { groups } = shownOrder();
        const out = [];
        for (const g of groups) {
            if (g.label) out.push(h('h3', { className: 'day', text: g.label }));
            for (const f of g.items) out.push(rowEl(f, !!g.label));
        }
        if (!S.files.length) {
            out.push(h('p', { className: 'list-empty', 'data-testid': 'list-empty',
                              text: R.filtersActive(S.route.filters) ? 'No sample matches this filter.' : 'No samples yet. Runs appear here as the GCs send them.' }));
        }
        if (S.files.length < S.total) {
            out.push(h('div', { className: 'list-more' },
                h('button', { type: 'button', className: 'btn btn-sm', 'data-testid': 'load-more',
                              text: 'Show ' + L.number(Math.min(PAGE, S.total - S.files.length)) + ' more of ' + L.number(S.total - S.files.length),
                              onclick: () => loadList({ more: true }) })));
        }
        rows.replaceChildren(...out);
        rows.classList.toggle('selecting', S.sel.all || S.sel.ids.size > 0);
        renderCounts();
        renderBulk();
        loadBackfillInfo();
    }

    // why a row is backfill needs its instrument's live_since and when that
    // was set (GET …/backfill answers both); asked once per instrument
    function loadBackfillInfo() {
        const want = new Set(S.files.filter(f => f.backfill && !f.released && f.instrument
                                             && !(f.instrument in S.backfillInfo)).map(f => f.instrument));
        for (const inst of want) {
            S.backfillInfo[inst] = null;                  // asked; null until answered
            getJSON('/api/instruments/' + encodeURIComponent(inst) + '/backfill?limit=1&released=false', true).then((r) => {
                if (!r.ok) { delete S.backfillInfo[inst]; return; }
                S.backfillInfo[inst] = { live_since: r.body.live_since || null,
                                         live_since_set_at: r.body.live_since_set_at || null };
                renderList();
                if (S.row && S.row.instrument === inst) renderHeader(S.row, entry(S.row.sample_id).meta || {});
            });
        }
    }

    function refreshRowStates() {
        document.querySelectorAll('#rows .srow').forEach(el => {
            const id = Number(el.dataset.sampleId);
            const checked = L.isChecked(S.sel, id);
            el.classList.toggle('checked', checked);
            el.classList.toggle('active', S.route.sampleId === id);
            el.setAttribute('aria-selected', S.route.sampleId === id ? 'true' : 'false');
            const box = el.querySelector('input[type=checkbox]');
            if (box) box.checked = checked;
        });
        $('rows').classList.toggle('selecting', S.sel.all || S.sel.ids.size > 0);
        renderBulk();
    }

    // ── selection: click, ctrl/cmd-click, shift-click, drag, keyboard ───
    let drag = null;
    function wireRows() {
        const rows = $('rows');
        rows.addEventListener('mousedown', (ev) => {
            const box = ev.target.closest('.srow .lead');
            if (!box || ev.button !== 0) return;
            const row = box.closest('.srow');
            const id = Number(row.dataset.sampleId);
            ev.preventDefault();
            const press = L.pressBox(S.sel, shownOrder().order, id, ev.shiftKey);
            S.sel = press.sel;
            drag = press.drag ? { on: press.drag.on, seen: new Set([id]) } : null;
            refreshRowStates();
        });
        rows.addEventListener('mouseover', (ev) => {
            if (!drag) return;
            const row = ev.target.closest('.srow');
            if (!row) return;
            const id = Number(row.dataset.sampleId);
            if (drag.seen.has(id)) return;
            drag.seen.add(id);
            S.sel = L.paint(S.sel, [id], drag.on);
            refreshRowStates();
        });
        document.addEventListener('mouseup', () => { drag = null; });
        rows.addEventListener('click', (ev) => {
            // the press already changed the selection: the click must not
            // toggle the box again (v5's bug: it then showed the opposite)
            if (ev.target.closest('.lead')) { ev.preventDefault(); refreshRowStates(); return; }
            if (ev.target.closest('.fix')) return;
            const row = ev.target.closest('.srow');
            if (!row) return;
            const id = Number(row.dataset.sampleId);
            if (ev.shiftKey) { S.sel = L.extend(S.sel, shownOrder().order, id); refreshRowStates(); return; }
            if (ev.metaKey || ev.ctrlKey) { S.sel = L.toggle(S.sel, id); refreshRowStates(); return; }
            openSample(id, 'push');
        });
        $('select-shown').addEventListener('change', (ev) => {
            S.sel = ev.target.checked ? L.selectShown(L.clear(), shownOrder().order) : L.clear();
            refreshRowStates();
        });
    }

    function renderBulk() {
        const shown = S.files.length;
        const bar = L.bulkBar(S.sel, { shown, total: S.total });
        const box = $('bulk');
        box.hidden = bar.count === 0 && !S.bulkRunning;
        $('bulk-text').textContent = bar.text;
        const all = $('bulk-all');
        all.hidden = !bar.offerAll;
        all.textContent = bar.offerAll || '';
        const sa = $('select-shown');
        sa.checked = shown > 0 && (S.sel.all || S.sel.ids.size === shown);
        sa.indeterminate = !sa.checked && S.sel.ids.size > 0;
        document.querySelectorAll('#bulk-actions button').forEach(b => { b.disabled = S.bulkRunning; });
    }

    // ── bulk actions ────────────────────────────────────────────────────
    function askConfirm(text) {
        const dlg = $('bulk-confirm');
        $('bc-title').textContent = text.title;
        $('bc-body').textContent = text.body;
        $('bc-ok').textContent = text.ok;
        return new Promise((resolve) => {
            let done = false;
            const finish = (v) => {
                if (done) return;
                done = true;
                $('bc-cancel').removeEventListener('click', onCancel);
                dlg.removeEventListener('close', onClose);
                if (dlg.open) dlg.close();
                resolve(v);
            };
            const onCancel = () => finish(false);
            const onClose = () => finish(false);          // Esc, or closed any other way
            $('bc-cancel').addEventListener('click', onCancel);
            $('bc-ok').onclick = (ev) => { ev.preventDefault(); finish(true); };
            dlg.addEventListener('close', onClose);
            dlg.returnValue = '';
            dlg.showModal();
            $('bc-ok').focus();
        });
    }

    async function selectedTargets() {
        if (!S.sel.all) {
            const ids = L.selectedIds(S.sel);
            return { ids, rows: S.files.filter(f => S.sel.ids.has(f.sample_id)), capped: false };
        }
        const res = await getJSON('/api/files/ids?' + listQuery());
        if (!res.ok) throw new Error(errText(res, 'Finding the matching samples'));
        return { ids: res.body.ids || [], rows: null, capped: !!res.body.capped, total: res.body.total };
    }

    async function rowsFor(ids) {
        const have = new Map(S.files.map(f => [f.sample_id, f]));
        const missing = ids.filter(id => !have.has(id));
        for (const part of L.chunks(missing, 1000)) {
            const res = await getJSON('/api/files?limit=' + part.length + '&ids=' + part.join(','));
            if (res.ok) for (const f of res.body.samples || []) have.set(f.sample_id, f);
        }
        return ids.map(id => have.get(id)).filter(Boolean);
    }

    const BULK_WORDS = { reprocess: ['Re-process', 'Re-processing'], lims: ['Export to LIMS', 'Exporting to LIMS'],
                         queue: ['Add to report queue', 'Adding to the report queue'], reports: ['Download reports', 'Building reports'] };

    async function runBulk(kind) {
        if (S.bulkRunning) return;
        let t;
        try { t = await selectedTargets(); } catch (e) { SH.toast(e.message, 'err'); return; }
        let ids = t.ids;
        if (!ids.length) { SH.toast('Nothing is selected.'); return; }
        const limit = L.BULK_LIMIT[kind];
        if (limit && ids.length > limit) {
            SH.toast(BULK_WORDS[kind][0] + ' takes at most ' + L.number(limit) + ' samples at a time; narrow the filter.', 'err');
            return;
        }
        const filter = S.sel.all ? L.filterText(S.route.filters, S.names) : null;
        const needConfirm = S.sel.all || ((kind === 'reprocess' || kind === 'lims') && ids.length > 1);
        if (needConfirm && !(await askConfirm(L.confirmText(kind, ids.length, filter)))) return;
        if (t.capped) SH.toast('Only the newest ' + L.number(ids.length) + ' of ' + L.number(t.total) + ' matching samples are included.');
        S.bulkRunning = true;
        S.bulkStop = false;
        $('bulk-run').hidden = false;
        renderBulk();
        const words = BULK_WORDS[kind];
        const progress = (p) => {
            $('bulk-bar-fill').style.width = Math.round(100 * p.sent / Math.max(1, p.total)) + '%';
            $('bulk-run-text').textContent = words[1] + ' · ' + L.number(p.sent) + ' of ' + L.number(p.total);
        };
        progress({ sent: 0, total: ids.length });
        let result;
        try {
            if (kind === 'reprocess') {
                result = await L.runChunks(ids, L.CHUNK.reprocess, async (part) => {
                    const r = await postJSON('/api/reprocess', { sample_ids: part });
                    if (!r.ok) throw new Error(errText(r, 'Re-process'));
                    return { done: r.body.count || 0, refused: r.body.refused || [] };
                }, progress, () => S.bulkStop);
            } else if (kind === 'lims') {
                result = await L.runChunks(ids, L.CHUNK.lims, async (part) => {
                    const r = await postJSON('/api/export-lims', { sample_ids: part });
                    if (!r.ok && r.status !== 409) throw new Error(errText(r, 'Export to LIMS'));
                    return { done: (r.body.exported || []).length, refused: r.body.refused || [] };
                }, progress, () => S.bulkStop);
            } else if (kind === 'queue') {
                result = await queueRows(await rowsFor(ids), progress);
            } else {
                result = await downloadReports(await rowsFor(ids), progress);
            }
        } catch (e) {
            result = { done: 0, refused: [], failed: ids.length, total: ids.length, stopped: false };
            SH.toast(e.message, 'err');
        }
        S.bulkRunning = false;
        $('bulk-run').hidden = true;
        const line = L.outcomeText(words[0], result);
        SH.toast(line, result.failed || (result.refused && result.refused.length) ? 'err' : undefined);
        if (!result.failed) S.sel = L.clear();
        refreshRowStates();
    }

    function convert() {
        return window.GCResults && window.GCResults.convertToD86 ? window.GCResults.convertToD86 : null;
    }

    function defaultStandard(row) {
        const C = window.GCCompare;
        return C && C.defaultStandard ? C.defaultStandard(row, S.standards) : null;
    }

    async function queueRows(rows, progress) {
        const q = window.GCReportQueue;
        if (!q || typeof q.addMany !== 'function') throw new Error('The report queue is not available on this page.');
        await settingsAndStandards();
        const items = [];
        const refused = [];
        for (const f of rows) {
            const std = defaultStandard(f);
            if (std) items.push(L.queueItem(f, std));
            else refused.push({ sample_id: f.sample_id, error: 'no comparison standard' });
        }
        const n = items.length ? (q.addMany(items, { quiet: true }) || 0) : 0;
        progress({ sent: rows.length, total: rows.length });
        if (n && typeof q.openSheet === 'function') q.openSheet();
        return { done: n, refused, failed: items.length - n, stopped: false, total: rows.length };
    }

    async function downloadReports(rows, progress) {
        await settingsAndStandards();
        const items = [];
        const refused = [];
        for (const f of rows) {
            const it = L.queueItem(f, defaultStandard(f));
            if (!it.standard_name) { refused.push({ sample_id: f.sample_id, error: 'no comparison standard' }); continue; }
            items.push({ sample_id: it.sample_id, standard_name: it.standard_name, lab_id: it.lab_id,
                         doc_name: 'GC Analysis', sample_name: it.sample_name });
        }
        if (!items.length) return { done: 0, refused, failed: 0, stopped: false, total: rows.length };
        const start = await postJSON('/api/export-analysis-reports-zip', { items });
        if (start.status !== 202) throw new Error(errText(start, 'Download reports'));
        let job = start.body.job;
        while (job && job.state === 'running') {
            progress({ sent: job.done || 0, total: rows.length });
            await new Promise(r => setTimeout(r, 1500));
            const poll = await getJSON('/api/export-analysis-reports-zip/' + encodeURIComponent(job.id), true);
            if (!poll.ok) throw new Error(errText(poll, 'Download reports'));
            job = poll.body.job;
        }
        const view = window.zipJobView ? window.zipJobView(job) : { download: null, message: '' };
        if (!view.download) throw new Error(view.message || 'The report ZIP failed.');
        window.location.assign(view.download);
        const r = job.result || {};
        return { done: r.written || 0, refused: refused.concat(new Array(r.skipped || 0).fill({ error: 'skipped' })),
                 failed: 0, stopped: false, total: rows.length };
    }

    function wireBulk() {
        document.querySelectorAll('#bulk-actions [data-bulk]').forEach(b =>
            b.addEventListener('click', () => runBulk(b.dataset.bulk)));
        $('bulk-clear').addEventListener('click', () => { S.sel = L.clear(); refreshRowStates(); });
        $('bulk-all').addEventListener('click', () => { S.sel = L.selectAllMatching(S.sel, S.total); refreshRowStates(); });
        $('bulk-stop').addEventListener('click', () => { S.bulkStop = true; $('bulk-run-text').textContent = 'Stopping after this step…'; });
    }

    // ── one sample ──────────────────────────────────────────────────────
    async function loadCurve(id, row, background) {
        const r = await getJSON('/api/samples/' + id + '/distillation-curve', background);
        if (r.ok) return r.body;
        if (r.status === 404 && row && row.current_revision) {
            if (!S.table || Date.now() - S.table.at > 60000) {
                const t = await getJSON('/api/table', background);
                if (t.ok) S.table = { at: Date.now(), body: t.body };
            }
            const c = S.table ? L.curveFromTable(S.table.body, id) : null;
            if (c) return c;
        }
        return { error: errText(r, 'The results') };
    }

    function entry(id) {
        if (!S.cache.has(id)) S.cache.set(id, {});
        return S.cache.get(id);
    }

    /** A live change to a sample: its metadata, curve and runs are asked
        again; its trace stays drawn (marked stale) until the new one is in. */
    function invalidate(id) {
        const e = S.cache.get(id);
        if (!e) return;
        S.cache.set(id, e.trace ? { trace: e.trace, traceStale: true } : {});
    }

    /** showDetail, never an unhandled rejection: what failed is said. */
    function showDetailSafe(opts) {
        return showDetail(opts).catch((e) => {
            console.error('[samples] detail', e);
            SH.toast('The sample did not open: ' + ((e && e.message) || e), 'err');
        });
    }

    async function rowOf(id, background) {
        const inList = S.files.find(f => f.sample_id === id);
        if (inList) return inList;
        const res = await getJSON('/api/files?limit=1&ids=' + id, background);
        return res.ok && (res.body.samples || [])[0] || null;
    }

    function openSample(id, how) {
        const same = S.route.sampleId === id;
        go({ sampleId: id, view: S.route.view || 'overview', standard: same ? S.route.standard : null }, how);
        showDetailSafe();
    }

    function setView(view, how) {
        if (S.route.sampleId == null) return;
        go({ view }, how);
        showDetailSafe();
    }

    async function showDetail(opts) {
        const id = S.route.sampleId;
        refreshRowStates();
        const empty = $('detail-empty');
        const body = $('detail-body');
        if (id == null) {
            if (S.emptyNote) empty.replaceChildren(...S.emptyNote.map(n => n.cloneNode(true)));
            empty.hidden = false;
            body.hidden = true;
            unmountCompare();
            return;
        }
        const seq = ++S.detailSeq;
        const background = opts && opts.background;
        const e = entry(id);
        const [row, meta] = await Promise.all([
            rowOf(id, background),
            e.meta ? Promise.resolve({ ok: true, body: e.meta }) : getJSON('/api/samples/' + id + '/metadata', background),
        ]);
        if (seq !== S.detailSeq) return;
        if (!meta.ok) {
            // the error replaces "Pick a sample" until the list is back (kept to restore)
            if (!S.emptyNote) S.emptyNote = Array.from(empty.childNodes, n => n.cloneNode(true));
            empty.hidden = false;
            body.hidden = true;
            empty.replaceChildren(h('p', { role: 'alert', text: errText(meta, 'Loading sample #' + id) }));
            return;
        }
        e.meta = meta.body;
        S.row = row || metaAsRow(meta.body);
        empty.hidden = true;
        body.hidden = false;
        renderHeader(S.row, e.meta);
        renderViewSwitch();
        if (!background) {
            SH.addRecent({ href: '/samples/' + id, label: S.row.display_name || S.row.lab_id || ('#' + id) });
            const el = document.querySelector('#rows .srow.active');
            if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
        }
        const view = S.route.view;
        $('view-overview').hidden = view !== 'overview';
        $('view-compare').hidden = view !== 'compare';
        $('view-data').hidden = view !== 'data';
        $('btn-copy-table').hidden = view !== 'data';
        if (view !== 'compare') unmountCompare();
        if (view === 'overview') await renderOverview(id, seq, background);
        else if (view === 'data') await renderData(id, seq, background);
        else mountCompare();
    }

    function metaAsRow(m) {
        return { sample_id: m.sample_id, lab_id: m.lab_id, display_name: m.lab_id, instrument: m.instrument,
                 injection_dt: m.injection_datetime, injection_dt_source: m.injection_dt_source, status: m.status,
                 error: m.error, backfill: m.backfill, released: !!m.released_at, method_name: m.method_name,
                 current_revision: m.current_revision, time_corrected: m.time_corrected, flags: [], best_fit: null,
                 qbench_uploaded_at: m.qbench_uploaded_at };
    }

    function renderHeader(row, meta) {
        const st = statusOf(row);
        $('d-lab').textContent = row.display_name || row.lab_id || ('#' + row.sample_id);
        const pill = $('d-status');
        pill.className = 'pill ' + (st.group === 'processing' ? '' : st.group);
        pill.replaceChildren(SH.glyph(st.glyph), h('span', { text: st.text }));
        const parts = [];
        const add = (node) => { if (parts.length) parts.push(h('span', { className: 'sep', 'aria-hidden': 'true', text: '·' })); parts.push(node); };
        add(h('span', { className: 'tag', text: S.names[row.instrument] || row.instrument || '' }));
        add(h('span', { text: L.injectedText(row.injection_dt, row.injection_dt_source) }));
        const rev = meta.current_revision ? 'revision ' + meta.current_revision : 'no result yet';
        add(h('span', { text: (row.method_name || 'D2887') + ' · ' + rev }));
        if (row.backfill) add(h('span', { text: row.released ? 'Backfill · released' : 'Backfill' }));
        if (row.time_corrected) add(h('span', { title: 'v1 misread this CDF’s time stamp', text: 'Injection time corrected' }));
        const review = L.reviewText(meta);
        if (review) add(h('span', { className: 'd-review', 'data-testid': 'detail-review', text: review }));
        $('d-meta').replaceChildren(...parts);
        const reason = $('d-reason');
        if (st.reason) {
            reason.hidden = false;
            reason.className = 'd-reason ' + (st.group === 'error' ? 'error' : st.group === 'held' ? 'held' : '');
            reason.replaceChildren(h('span', { text: st.reason }), ...(st.fix ? [fixEl(st.fix, row)] : []));
        } else {
            reason.hidden = true;
            reason.replaceChildren();
        }
        const cdf = $('act-cdf');
        cdf.setAttribute('href', '/api/samples/' + row.sample_id + '/cdf');
        $('btn-add-queue').disabled = row.status !== 'final';
        $('btn-export-report').disabled = row.status !== 'final';
    }

    function renderViewSwitch() {
        const id = S.route.sampleId;
        document.querySelectorAll('.seg.views [data-view]').forEach(a => {
            const v = a.dataset.view;
            a.setAttribute('href', R.build(Object.assign({}, S.route, { sampleId: id, view: v, standard: v === 'compare' ? S.route.standard : null })));
            a.setAttribute('aria-selected', v === S.route.view ? 'true' : 'false');
            a.tabIndex = v === S.route.view ? 0 : -1;
        });
    }

    // ── Overview: the chromatogram (v6) ─────────────────────────────────
    // The chart always draws or says why in one line, with a Retry. #chrom
    // holds Plotly only: a message goes in #chrom-msg beside it, and the plot
    // is purged before any message (v5 replaced Plotly's DOM with "Loading…"
    // and then Plotly.react'ed the same div, which still held the old plot's
    // state, so the next sample drew into detached nodes: no chart at all).
    // Overlays: other samples and comparison standards on the same axes,
    // Overlay or Stacked, kept in sessionStorage (gc.samples.overlay).
    const CH = { for: null, plotted: false, pending: false, seq: 0 };
    const OVERLAY_KEY = 'gc.samples.overlay';

    function loadOverlay() {
        try { return L.overlayParse(sessionStorage.getItem(OVERLAY_KEY)); } catch (_e) { return L.overlayState(null); }
    }
    function saveOverlay() {
        try { sessionStorage.setItem(OVERLAY_KEY, L.overlayStringify(S.overlay)); } catch (_e) { /* not kept */ }
    }

    function tokens() {
        const cs = getComputedStyle(document.documentElement);
        const v = (n) => cs.getPropertyValue(n).trim();
        return { ink: v('--chart-ink'), axis: v('--chart-axis'), grid: v('--chart-grid'), bg: v('--bg-card'),
                 font: v('--font') || 'sans-serif', tick: v('--chart-tick'), ref: v('--chart-ref') };
    }

    function purgeChart() {
        const el = $('chrom');
        if (window.Plotly && (el.data || el._fullLayout)) {
            try { window.Plotly.purge(el); } catch (e) { console.error('[samples] purge', e); }
        }
        el.replaceChildren();
        CH.plotted = false;
        CH.for = null;
    }

    /** One line where the chart would be (and no chart): loading, or why it
        is not drawn, with Retry. */
    function chartMessage(text, opts) {
        purgeChart();
        CH.pending = false;
        const msg = $('chrom-msg');
        const retry = opts && opts.retry;
        msg.replaceChildren(h('span', { className: 'chart-empty', text }),
            ...(retry ? [h('button', { type: 'button', className: 'link-btn', 'data-testid': 'chart-retry', text: 'Retry', onclick: retry })] : []));
        msg.setAttribute('role', retry ? 'alert' : 'status');
        msg.hidden = false;
        $('chrom-note').textContent = '';
        renderLegend([], []);
    }

    /** The open sample's trace: one request at a time per sample; an answer
        is kept, a failure never is (the next visit or Retry asks again). A
        live update marks it stale: it stays drawn while it is asked again. */
    function sampleTrace(id, background) {
        const e = entry(id);
        if (e.trace && !e.traceStale) return Promise.resolve({ trace: e.trace });
        if (e.traceLoad) return e.traceLoad;
        const load = getJSON('/api/samples/' + id + '/trace', background).then((r) => {
            const cur = entry(id);                       // a live reset may have replaced the entry
            if (cur.traceLoad === load) cur.traceLoad = null;
            if (r.ok && Array.isArray(r.body.x)) {
                cur.trace = r.body;
                cur.traceStale = false;
                return { trace: r.body };
            }
            return cur.trace ? { trace: cur.trace } : { error: r };
        });
        e.traceLoad = load;
        return load;
    }

    async function renderChart(id, background) {
        const mine = ++CH.seq;
        if (entry(id).trace) drawChart();                 // what we have, at once (a stale one too)
        else if (!(CH.for === id && CH.plotted)) chartMessage('Loading the chromatogram…');
        const res = await sampleTrace(id, background);
        if (mine !== CH.seq || S.route.sampleId !== id || S.route.view !== 'overview') return;
        if (res.error) {
            chartMessage(L.chartReason(res.error), { retry: () => renderChart(id, false) });
            return;
        }
        drawChart();
        loadOverlayTraces();
    }

    function primaryLabel() {
        return S.row ? (S.row.display_name || S.row.lab_id || '#' + S.row.sample_id) : '';
    }

    function overlayTraceFor(it) {
        if (it.kind === 'sample') {
            const e = S.cache.get(it.id);
            if (e && e.trace) return { trace: e.trace };
        }
        return S.ovTraces.get(L.overlayKey(it)) || null;
    }

    function loadOverlayTraces() {
        for (const it of L.overlayShown(S.overlay, S.route.sampleId)) {
            const key = L.overlayKey(it);
            const have = overlayTraceFor(it);
            if (have && (have.trace || have.loading)) continue;       // a failure is asked again
            S.ovTraces.set(key, { loading: true });
            const url = it.kind === 'standard'
                ? '/api/comparison-standards/' + encodeURIComponent(it.name) + '/trace'
                : '/api/samples/' + it.id + '/trace';
            getJSON(url, true).then((r) => {
                S.ovTraces.set(key, r.ok && Array.isArray(r.body.x) ? { trace: r.body } : { error: r });
                drawChart();
            });
        }
    }

    function drawChart() {
        const id = S.route.sampleId;
        if (id == null || S.route.view !== 'overview') return;
        const e = entry(id);
        if (!e.trace) return;                             // renderChart says why
        const el = $('chrom');
        if (!window.Plotly) {
            chartMessage('The chart library did not load (no internet access to cdn.plot.ly?). The numbers are still shown.',
                         { retry: () => location.reload() });
            return;
        }
        // hidden or not laid out yet: Plotly would draw at a default size; the ResizeObserver draws it once it has one
        if (!el.clientWidth) { CH.pending = true; return; }
        CH.pending = false;
        const c = tokens();
        const list = [{ key: 's:' + id, label: primaryLabel(), sub: '', kind: 'sample', x: e.trace.x, y: e.trace.y }];
        const waiting = [];
        for (const it of L.overlayShown(S.overlay, id)) {
            const got = overlayTraceFor(it);
            const key = L.overlayKey(it);
            if (got && got.trace) list.push({ key, label: it.label, sub: it.sub || '', kind: it.kind, x: got.trace.x, y: got.trace.y });
            else waiting.push({ key, label: it.label, kind: it.kind, state: got && got.error ? L.chartReason(got.error) : 'Loading…' });
        }
        const built = L.overlayTraces(list, S.overlay.mode, c);
        const stacked = S.overlay.mode === 'stacked' && list.length > 1;
        const layout = L.chartLayout(c);
        const xs = e.trace.x || [];
        // carbon marks: the open sample's own ladder (ladder.js's pairs, monochrome)
        const ticks = L.carbonTicks(e.trace.cal_times, e.trace.cal_carbons, c, 12, xs.length ? xs[xs.length - 1] - xs[0] : 0);
        layout.shapes = ticks.shapes;
        layout.annotations = ticks.annotations;
        layout.margin.t = ticks.annotations.length ? 34 : 12;
        layout.uirevision = 's' + id + ':' + S.overlay.mode;   // adding a trace keeps the zoom; a new sample resets it
        if (stacked) {
            layout.yaxis.showticklabels = false;
            layout.yaxis.title = { text: 'Stacked: each trace lifted', font: { color: c.axis, size: 11 } };
        }
        if (el.data && !el.querySelector('.plot-container')) purgeChart();   // never react into a wiped div
        $('chrom-msg').hidden = true;
        window.Plotly.react(el, built.data, layout, L.CHART_CONFIG);
        CH.plotted = true;
        CH.for = id;
        $('chrom-note').textContent = !ticks.annotations.length ? 'No calibration ladder for this run'
            : list.length > 1 || waiting.length ? 'Carbon marks from ' + primaryLabel() + '’s calibration'
                : 'Carbon marks from this run’s calibration';
        renderLegend(built.legend, waiting);
    }

    // ── the legend: every trace named, remove one, clear all ────────────
    function swatch(color, dash) {
        const NS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(NS, 'svg');
        svg.setAttribute('viewBox', '0 0 28 8');
        svg.setAttribute('class', 'tl-swatch');
        svg.setAttribute('aria-hidden', 'true');
        const line = document.createElementNS(NS, 'line');
        line.setAttribute('x1', '1'); line.setAttribute('x2', '27'); line.setAttribute('y1', '4'); line.setAttribute('y2', '4');
        line.setAttribute('stroke', color);
        line.setAttribute('stroke-width', '2');
        const da = L.dashArray(dash);
        if (da) line.setAttribute('stroke-dasharray', da);
        svg.appendChild(line);
        return svg;
    }

    function renderLegend(items, waiting) {
        const box = $('chrom-legend');
        const extras = items.filter(it => !it.primary).length + waiting.length;
        $('chrom-mode').hidden = !extras;
        document.querySelectorAll('#chrom-mode [data-mode]').forEach(b =>
            b.setAttribute('aria-checked', b.dataset.mode === S.overlay.mode ? 'true' : 'false'));
        if (!extras) { box.hidden = true; box.replaceChildren(); return; }
        const remove = (key, label) => h('button', { type: 'button', className: 'tl-remove', 'data-testid': 'trace-remove',
            'aria-label': 'Remove ' + label + ' from the chart', title: 'Remove from the chart', text: '×',
            onclick: () => removeOverlay(key) });
        const lis = items.map(it => h('li', { className: 'tl-item' + (it.primary ? ' primary' : ''), 'data-key': it.key, 'data-testid': 'trace-item' },
            swatch(it.color, it.dash),
            h('span', { className: 'tl-name', text: it.label }),
            it.primary ? h('span', { className: 'tl-sub', text: 'open sample' })
                : h('span', { className: 'tl-sub', text: it.kind === 'standard' ? 'standard' : it.sub }),
            it.primary ? null : remove(it.key, it.label)));
        for (const w of waiting) {
            lis.push(h('li', { className: 'tl-item waiting', 'data-key': w.key, 'data-testid': 'trace-item' },
                h('span', { className: 'tl-swatch tl-none', 'aria-hidden': 'true' }),
                h('span', { className: 'tl-name', text: w.label }),
                h('span', { className: 'tl-sub' + (w.state === 'Loading…' ? '' : ' tl-err'), text: w.state }),
                remove(w.key, w.label)));
        }
        box.replaceChildren(h('ul', { className: 'tl-list', 'aria-label': 'Traces on the chart' }, ...lis),
            h('button', { type: 'button', className: 'link-btn tl-clear', 'data-testid': 'trace-clear', text: 'Clear all',
                          onclick: clearOverlay }));
        box.hidden = false;
    }

    // ── adding and removing traces ──────────────────────────────────────
    function overlayItemOf(f) {
        const when = O.rowTime(f, { underDay: false });
        const inst = S.names[f.instrument] || f.instrument || '';
        return { kind: 'sample', id: f.sample_id, label: f.display_name || f.lab_id || '#' + f.sample_id,
                 sub: [inst, when && when.text].filter(Boolean).join(' · ') };
    }

    function addOverlay(items) {
        const r = L.overlayAdd(S.overlay, items, S.route.sampleId);
        S.overlay = r.ov;
        saveOverlay();
        const text = L.overlayAddText(r);
        if (text) SH.toast(text, r.added ? undefined : 'err');
        loadOverlayTraces();
        drawChart();
        return r;
    }
    function removeOverlay(key) {
        S.overlay = L.overlayRemove(S.overlay, key);
        saveOverlay();
        drawChart();
        if (!L.overlayShown(S.overlay, S.route.sampleId).length) $('btn-add-trace').focus();
    }
    function clearOverlay() {
        S.overlay = L.overlayClear(S.overlay);
        saveOverlay();
        drawChart();
        $('btn-add-trace').focus();
    }
    function setOverlayMode(mode) {
        S.overlay = L.overlayMode(S.overlay, mode);
        saveOverlay();
        drawChart();
    }

    /** "Overlay selected": the ticked rows on the open sample's chart (with
        none open, the first ticked one opens and the rest go on its chart). */
    async function overlaySelected() {
        if (S.sel.all) { SH.toast('Tick the samples to overlay (at most ' + (L.OVERLAY_MAX + 1) + ' traces), not all matching.', 'err'); return; }
        const ids = shownOrder().order.filter(id => S.sel.ids.has(id));
        if (!ids.length) { SH.toast('Tick the samples to overlay first.', 'err'); return; }
        let rest = ids;
        if (S.route.sampleId == null) {
            go({ sampleId: ids[0], view: 'overview', standard: null }, 'push');
            rest = ids.slice(1);
        } else if (S.route.view !== 'overview') {
            go({ view: 'overview' }, 'push');
        }
        const rows = await rowsFor(rest);
        addOverlay(rows.map(overlayItemOf));
        await showDetailSafe();
    }

    async function addByLabId(text, out) {
        const lab = String(text || '').trim();
        if (!lab) { out.textContent = 'Type a lab ID.'; return false; }
        if (lab.includes('/')) { out.textContent = 'A lab ID with “/” can’t be looked up; tick its row in the list instead.'; return false; }
        out.textContent = 'Looking up ' + lab + '…';
        const r = await getJSON('/api/lab/' + encodeURIComponent(lab));
        if (!r.ok || r.body.sample_id == null) {
            out.textContent = r.status === 404 ? 'No GC run has lab ID ' + lab + '.' : errText(r, 'Looking up ' + lab);
            return false;
        }
        const row = await rowOf(Number(r.body.sample_id), true);
        const item = row ? overlayItemOf(row) : { kind: 'sample', id: Number(r.body.sample_id), label: lab };
        const res = addOverlay([item]);
        out.textContent = res.added ? '' : L.overlayAddText(res);
        return res.added > 0;
    }

    function buildTracePop() {
        const pop = $('add-trace-pop');
        const n = S.sel.all ? 0 : S.sel.ids.size;
        const out = h('p', { className: 'tp-out', role: 'status', 'data-testid': 'overlay-lab-out' });
        const input = h('input', { type: 'search', placeholder: 'Lab ID', autocomplete: 'off', spellcheck: 'false',
                                   'aria-label': 'Lab ID to add to the chart', 'data-testid': 'overlay-lab' });
        const form = h('form', { className: 'tp-lab', onsubmit: async (ev) => {
            ev.preventDefault();
            if (await addByLabId(input.value, out)) { input.value = ''; closeTracePop(); }
        } }, input, h('button', { type: 'submit', className: 'btn btn-sm', text: 'Add' }));
        const have = new Set(L.overlayState(S.overlay).items.map(L.overlayKey));
        const stds = S.standards.map(s => s && s.name).filter(Boolean);
        pop.replaceChildren(
            h('div', { className: 'tp-sec' },
                h('h3', { text: 'Samples ticked in the list' }),
                h('button', { type: 'button', className: 'menu-item', 'data-testid': 'overlay-selected', disabled: !n,
                              text: n ? 'Overlay ' + L.plural(n, 'ticked sample') : 'Tick samples in the list to overlay them',
                              onclick: () => { closeTracePop(); overlaySelected(); } })),
            h('div', { className: 'tp-sec' },
                h('h3', { text: 'Comparison standards' }),
                stds.length ? h('div', { className: 'tp-stds' }, ...stds.map(name => h('button', {
                    type: 'button', className: 'menu-item', 'data-testid': 'overlay-standard', 'data-name': name,
                    'aria-pressed': have.has('std:' + name) ? 'true' : 'false', text: name,
                    onclick: () => { addOverlay([{ kind: 'standard', name }]); closeTracePop(); } })))
                    : h('p', { className: 'tp-out', text: 'No comparison standards yet (Settings · Comparison standards).' })),
            h('div', { className: 'tp-sec' }, h('h3', { text: 'A sample by lab ID' }), form, out));
        return input;
    }
    async function openTracePop() {
        const pop = $('add-trace-pop');
        await settingsAndStandards();                     // loaded at start; the standards are listed
        const input = buildTracePop();
        pop.hidden = false;
        $('btn-add-trace').setAttribute('aria-expanded', 'true');
        const first = pop.querySelector('button:not([disabled])') || input;
        if (first) first.focus();
    }
    function closeTracePop() {
        $('add-trace-pop').hidden = true;
        $('btn-add-trace').setAttribute('aria-expanded', 'false');
    }
    function wireChart() {
        const btn = $('btn-add-trace');
        const pop = $('add-trace-pop');
        btn.addEventListener('click', (ev) => {
            ev.stopPropagation();
            if (pop.hidden) openTracePop(); else closeTracePop();
        });
        document.addEventListener('click', (ev) => { if (!pop.hidden && !pop.contains(ev.target) && ev.target !== btn) closeTracePop(); });
        pop.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { ev.stopPropagation(); closeTracePop(); btn.focus(); } });
        document.querySelectorAll('#chrom-mode [data-mode]').forEach(b => b.addEventListener('click', () => setOverlayMode(b.dataset.mode)));
        $('bulk-overlay').addEventListener('click', overlaySelected);
        const el = $('chrom');
        if (window.ResizeObserver) {
            new ResizeObserver(() => {
                if (CH.pending) drawChart();
                else if (CH.plotted && window.Plotly && el.clientWidth) window.Plotly.Plots.resize(el);
            }).observe(el);
        }
    }

    async function renderOverview(id, seq, background) {
        const row = S.row;
        renderChart(id, background);                      // draws or says why, on its own
        let curve = entry(id).curve || null;
        let lab = entry(id).lab || null;
        const wants = [
            curve || !row.current_revision ? null : loadCurve(id, row, background).then((c) => {
                curve = c;
                if (!c.error) entry(id).curve = c;          // a failure is not kept: the next visit asks again
            }),
            lab || !row.lab_id || String(row.lab_id).includes('/') ? null : getJSON('/api/lab/' + encodeURIComponent(row.lab_id), true).then((r) => {
                lab = r.ok ? r.body : { runs: [] };
                if (r.ok) entry(id).lab = lab;
            }),
        ].filter(Boolean);
        if (wants.length && !curve && !background) renderResults(row, null);   // a live refresh keeps what is shown
        await Promise.all(wants);
        if (seq !== S.detailSeq) return;
        renderResults(row, curve);
        renderRuns(row, lab);
    }

    function renderResults(row, curve) {
        const body = $('results-body');
        if (!row.current_revision) {
            const st = L.rowStatus(row);
            body.replaceChildren(h('p', { className: 'empty-note held-note', text: 'No result yet: ' + (st.reason || st.text) + '.' }));
            return;
        }
        if (!curve) { body.replaceChildren(h('p', { className: 'side-note', text: 'Loading…' })); return; }
        if (curve.error) { body.replaceChildren(h('p', { className: 'side-note', role: 'alert', text: curve.error })); return; }
        const rows = L.resultRows(curve, S.corrected, convert());
        const tbl = h('table', { className: 'res-tbl', 'data-testid': 'results-table' },
            h('thead', {}, h('tr', {}, h('th', { scope: 'col', text: 'Recovery' }), h('th', { scope: 'col', text: 'D86' }), h('th', { scope: 'col', text: 'D2887' }))),
            h('tbody', {}, ...rows.map(r => h('tr', { className: r.key ? 'key' : '' },
                h('td', { text: r.label }),
                h('td', { className: r.d86 === null ? 'none' : '', title: r.note || null, text: L.fmt(r.d86, 1) }),
                h('td', { className: r.d2887 === null ? 'none' : '', text: L.fmt(r.d2887, 1) })))));
        const sw = h('button', { type: 'button', className: 'switch', role: 'switch', 'aria-checked': S.corrected ? 'true' : 'false',
                                 'aria-label': 'D86 corrected', 'data-testid': 'corrected-toggle',
                                 onclick: () => { S.corrected = !S.corrected; saveBool(D86_KEY, S.corrected); renderResults(S.row, entry(S.row.sample_id).curve); } });
        const inst = S.names[row.instrument] || row.instrument;
        const best = row.best_fit;
        const flags = L.flagText(row);
        body.replaceChildren(tbl,
            h('div', { className: 'switch-row' }, h('span', { text: S.corrected ? 'D86 corrected (' + inst + ' factors)' : 'D86 uncorrected' }), sw),
            // (replaceChildren would print a null as the text "null": v5 showed it under the switch)
            ...(curve.fromTable ? [h('p', { className: 'side-note', 'data-testid': 'results-from-table', text: 'Imported result: the numbers as stored (no curve for this run).' })] : []),
            h('div', { className: 'res-foot' },
                h('div', { className: 'line' }, h('span', { className: 'k', text: 'Best fit' }),
                    h('span', { className: 'v' }, best && best.label ? best.label : '—',
                        best && best.score != null ? h('small', { text: Number(best.score).toFixed(2) + ' match' }) : null)),
                h('div', { className: 'line' }, h('span', { className: 'k', text: 'Flags' }), h('span', { className: 'v', text: flags || 'None' }))));
    }

    function renderRuns(row, lab) {
        const box = $('other-runs');
        const others = window.DeepLink ? window.DeepLink.otherRuns(lab) : [];
        const list = others.filter(r => r.sample_id !== row.sample_id);
        box.hidden = !list.length;
        if (!list.length) return;
        $('runs-lab').textContent = row.lab_id;
        $('runs-list').replaceChildren(...list.map(r => {
            const st = L.rowStatus({ status: r.status });
            return h('li', {}, SH.glyph(st.glyph, st.text),
                h('a', { href: R.build({ sampleId: r.sample_id, view: 'overview', filters: S.route.filters }), text: r.label,
                         onclick: (ev) => { if (ev.metaKey || ev.ctrlKey || ev.shiftKey) return; ev.preventDefault(); openSample(r.sample_id, 'push'); } }),
                h('span', { className: 'caption', text: st.text }));
        }));
    }

    // ── Data ────────────────────────────────────────────────────────────
    async function renderData(id, seq, background) {
        const e = entry(id);
        const row = S.row;
        if (!e.curve && row.current_revision) {
            e.curve = await loadCurve(id, row, background);
        }
        if (seq !== S.detailSeq) return;
        const curve = e.curve;
        const tbl = $('data-table');
        if (!row.current_revision || !curve || curve.error) {
            const st = L.rowStatus(row);
            tbl.replaceChildren(h('tbody', {}, h('tr', {}, h('td', { text: curve && curve.error ? curve.error : 'No result yet: ' + (st.reason || st.text) + '.' }))));
        } else {
            const rows = L.dataRows(curve, convert());
            const inst = S.names[row.instrument] || row.instrument;
            tbl.replaceChildren(
                h('thead', {}, h('tr', {}, ...['Recovery', 'D2887 °C', 'D86 raw', inst + ' correction', 'D86 reported'].map(t => h('th', { scope: 'col', text: t })))),
                h('tbody', {}, ...rows.map(r => h('tr', {},
                    h('td', { text: r.label }), h('td', { text: L.fmt(r.d2887, 2) }), h('td', { text: L.fmt(r.raw, 2) }),
                    h('td', { text: r.correction === null ? '—' : (r.correction === 0 ? '0' : L.fmt(r.correction, 2)) }),
                    h('td', { className: 'rep', text: L.fmt(r.reported, 2) })))));
        }
        const meta = e.meta || {};
        const fact = (k, v) => h('div', { className: 'fact' }, h('span', { className: 'k', text: k }), v instanceof Node ? h('span', { className: 'v' }, v) : h('span', { className: 'v', text: v }));
        const blank = curve && !curve.error ? curve.blank_used : null;
        $('data-facts').replaceChildren(
            fact('Blank used', blank ? h('a', { href: '/samples/' + blank, text: 'Sample #' + blank }) : 'None'),
            fact('Method', meta.method_name || '—'),
            fact('CDF', meta.source_name || '—'),
            fact('QBench', meta.qbench_uploaded_at ? 'Sent ' + String(meta.qbench_uploaded_at).replace('T', ' ').slice(0, 16) : 'Not sent'));
        const cal = curve && !curve.error ? (curve.calibration || {}) : null;
        const calBody = $('cal-body');
        if (!cal) {
            calBody.replaceChildren(h('p', { className: 'side-note', text: 'No calibration recorded: this run has no result yet.' }));
        } else {
            const anchors = Array.isArray(cal.anchors) ? cal.anchors : [];
            const file = String(cal.cdf || '—').split(/[\\/]/).pop();
            calBody.replaceChildren(
                h('div', { className: 'kv' },
                    h('div', { className: 'k', text: 'File' }), h('div', { text: file }),
                    h('div', { className: 'k', text: 'Anchors' }),
                    h('div', { text: anchors.length ? anchors.length + ' peaks' : (cal.source === 'instrument' ? 'the instrument’s current calibration (this result records none)' : 'none') })),
                h('div', { className: 'anchors' }, ...anchors.map(a => h('span', { text: 'C' + a[1] + ' · ' + Number(a[0]).toFixed(3) }))));
        }
        const hist = L.historyItems(meta);
        $('hist-list').replaceChildren(...(hist.length ? hist.map(it => h('li', {}, h('div', { className: 't', text: it.title }), h('div', { className: 'd', text: it.detail })))
            : [h('li', {}, h('div', { className: 'd', text: 'No revisions yet.' }))]));
    }

    async function copyTable() {
        const e = entry(S.route.sampleId);
        if (!e.curve || e.curve.error) return;
        const ok = await copyText(L.dataTableText(L.dataRows(e.curve, convert())));
        SH.toast(ok ? 'Table copied' : 'Could not copy the table', ok ? undefined : 'err');
    }

    // ── Compare (lane C) ────────────────────────────────────────────────
    function settingsAndStandards() {
        if (!S.standardsReady) {
            S.standardsReady = Promise.all([
                getJSON('/api/comparison-standards', true).then(r => { S.standards = r.ok && Array.isArray(r.body) ? r.body : []; }),
                S.settings ? null : getJSON('/api/settings', true).then(r => { S.settings = r.ok ? r.body : {}; }),
            ]);
        }
        return S.standardsReady;
    }

    async function mountCompare() {
        const id = S.route.sampleId;
        if (S.compare && S.compareFor === id) {
            if (S.route.standard && S.compare.setStandard) S.compare.setStandard(S.route.standard);
            return;
        }
        unmountCompare();
        const mine = ++S.mountSeq;                // a second call while this one waits wins
        const el = $('view-compare');
        if (!window.GCCompare || typeof window.GCCompare.mount !== 'function') {
            el.replaceChildren(h('p', { className: 'errline', role: 'alert', text: 'Compare did not load (compare_view.js). Reload the page.' }));
            return;
        }
        el.replaceChildren(h('p', { className: 'side-note', text: 'Loading Compare…' }));
        await settingsAndStandards();
        if (mine !== S.mountSeq || S.route.sampleId !== id || S.route.view !== 'compare') return;
        el.replaceChildren();
        try {
            S.compare = window.GCCompare.mount(el, {
                sample: S.row, standards: S.standards, settings: S.settings,
                onUrlChange: (change) => {
                    if (!change || S.route.sampleId !== id || S.route.view !== 'compare') return;
                    if ('standard' in change) {
                        // a pick is a step Back can undo; the default resolving is not
                        go({ standard: change.standard || null }, change.source === 'pick' ? 'push' : 'replace');
                        renderViewSwitch();
                    }
                },
            });
            S.compareFor = id;
            if (S.route.standard && S.compare && S.compare.setStandard
                    && S.compare.setStandard(S.route.standard) === false) {
                SH.toast('No comparison standard named ' + S.route.standard + '.', 'err');
            }
        } catch (e) {
            el.replaceChildren(h('p', { className: 'errline', role: 'alert', text: 'Compare could not open: ' + e.message }));
        }
    }

    function unmountCompare() {
        if (S.compare && typeof S.compare.unmount === 'function') {
            try { S.compare.unmount(); } catch (e) { console.error(e); }
        }
        S.compare = null;
        S.compareFor = null;
        const main = $('main');
        if (main) main.classList.remove('adjusting');
    }

    // ── header actions ──────────────────────────────────────────────────
    async function copyText(text) {
        try {
            if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
        } catch (_e) { /* fall back */ }
        const ta = h('textarea', { readonly: true });
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        let ok = false;
        try { ok = document.execCommand('copy'); } catch (_e) { ok = false; }
        ta.remove();
        return ok;
    }

    async function copyLink() {
        const id = S.route.sampleId;
        const session = window.GCSession && window.GCSession.whoami ? await window.GCSession.whoami() : null;
        const url = window.DeepLink.linkFromSession(session, id);
        const ok = await copyText(url);
        window.__lastCopiedLink = url;
        SH.toast(ok ? 'Link copied' : 'Copy this link: ' + url, ok ? undefined : 'err');
    }

    async function reprocess(ids) {
        const r = await postJSON('/api/reprocess', { sample_ids: ids });
        if (!r.ok) { SH.toast(errText(r, 'Re-process'), 'err'); return; }
        const refused = (r.body.refused || []).length;
        SH.toast(r.body.count ? 'Re-processing ' + L.plural(r.body.count, 'sample') + (refused ? ' · ' + refused + ' refused' : '') : 'Nothing was queued' + (refused ? ': ' + r.body.refused[0].error : ''),
                 r.body.count ? undefined : 'err');
    }

    async function exportLims(ids) {
        const r = await postJSON('/api/export-lims', { sample_ids: ids });
        if (r.ok) SH.toast('Exported to LIMS (written to the results file)');
        else SH.toast(r.body.refused && r.body.refused.length ? 'Not exported: ' + r.body.refused[0].error : errText(r, 'Export to LIMS'), 'err');
    }

    async function addToQueue() {
        const C = window.GCCompare;
        if (!C || typeof C.addToQueue !== 'function' || !window.GCReportQueue) {
            SH.toast('The report queue is not available on this page.', 'err');
            return;
        }
        await settingsAndStandards();
        C.addToQueue({ sample: S.row, standards: S.standards, settings: S.settings });   // it says what it did
    }

    async function exportReport() {
        const C = window.GCCompare;
        if (!C || typeof C.openExportSheet !== 'function') { SH.toast('Export report did not load. Reload the page.', 'err'); return; }
        await settingsAndStandards();
        C.openExportSheet({ sample: S.row, standards: S.standards, settings: S.settings });
    }

    function wireHeader() {
        const btn = $('btn-more');
        const menu = $('more-menu');
        const close = () => { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
        btn.addEventListener('click', (ev) => {
            ev.stopPropagation();
            menu.hidden = !menu.hidden;
            btn.setAttribute('aria-expanded', menu.hidden ? 'false' : 'true');
            if (!menu.hidden) { const first = menu.querySelector('[role=menuitem]'); if (first) first.focus(); }
        });
        document.addEventListener('click', (ev) => { if (!menu.hidden && !menu.contains(ev.target)) close(); });
        menu.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { close(); btn.focus(); } });
        menu.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', () => {
            close();
            const id = S.route.sampleId;
            if (b.dataset.act === 'reprocess') reprocess([id]);
            else if (b.dataset.act === 'lims') exportLims([id]);
            else if (b.dataset.act === 'copy') copyLink();
        }));
        $('act-cdf').addEventListener('click', () => close());
        $('btn-add-queue').addEventListener('click', addToQueue);
        $('btn-export-report').addEventListener('click', exportReport);
        $('btn-copy-table').addEventListener('click', copyTable);
        document.querySelectorAll('.seg.views [data-view]').forEach(a => a.addEventListener('click', (ev) => {
            if (ev.metaKey || ev.ctrlKey || ev.shiftKey) return;
            ev.preventDefault();
            setView(a.dataset.view, 'push');
        }));
    }

    // ── keyboard ────────────────────────────────────────────────────────
    function wireKeys() {
        document.addEventListener('keydown', (ev) => {
            const k = ev.key;
            if ((ev.ctrlKey || ev.metaKey) && (k === 'k' || k === 'K')) {
                ev.preventDefault();
                const q = $('filter-q');
                q.focus();
                q.select();
                return;
            }
            const t = ev.target;
            const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
            if (typing) {
                if (k === 'Escape' && t.id === 'filter-q') { t.blur(); $('rows').focus(); }
                if ((k === 'ArrowDown' || k === 'Enter') && t.id === 'filter-q') { ev.preventDefault(); $('rows').focus(); stepTo(1, false); }
                return;
            }
            if (document.querySelector('dialog[open]') || ev.altKey || ev.ctrlKey || ev.metaKey) return;
            if (!$('view-compare').hidden && $('view-compare').contains(t)) return;       // lane C's own keys
            if ($('add-trace-pop').contains(t)) return;                                   // the Add trace menu's buttons
            if (k === 'ArrowDown' || k === 'j' || k === 'J') { ev.preventDefault(); stepTo(1, ev.shiftKey); }
            else if (k === 'ArrowUp' || k === 'k' || k === 'K') { ev.preventDefault(); stepTo(-1, ev.shiftKey); }
            else if ((k === 'x' || k === ' ') && S.route.sampleId != null && t && (t.id === 'rows' || t === document.body)) {
                ev.preventDefault();
                S.sel = L.toggle(S.sel, S.route.sampleId);
                refreshRowStates();
            } else if (k === 'Escape' && (S.sel.all || S.sel.ids.size)) {
                S.sel = L.clear();
                refreshRowStates();
            }
        });
    }

    function stepTo(dir, extend) {
        const { order } = shownOrder();
        const next = L.step(order, S.route.sampleId, dir);
        if (next == null) return;
        if (extend) {
            if (S.sel.anchor == null && S.route.sampleId != null) S.sel = L.toggle(S.sel, S.route.sampleId);
            S.sel = L.paint(S.sel, [next], true);
        }
        openSample(next, 'replace');
    }

    // ── filters wiring ──────────────────────────────────────────────────
    function wireFilters() {
        let timer = null;
        $('filter-q').addEventListener('input', (ev) => {
            clearTimeout(timer);
            const v = ev.target.value;
            timer = setTimeout(() => setFilters({ q: v.trim() }, 'replace'), 250);
        });
        document.querySelectorAll('#sort-switch [data-sort]').forEach(b => b.addEventListener('click', () => {
            O.saveSortMode(b.dataset.sort);
            go({ filters: { sort: b.dataset.sort } }, 'push');
            renderFilters();
            renderList();
        }));
    }

    // ── live updates ────────────────────────────────────────────────────
    let firstLive = true;
    async function onLive(update) {
        if (update.reset) {
            if (firstLive) { firstLive = false; return; }
            for (const id of Array.from(S.cache.keys())) invalidate(id);
            S.ovTraces.clear();
            await loadList({ background: true });
            if (S.route.sampleId != null) showDetailSafe({ background: true });
            return;
        }
        firstLive = false;
        if (update.day_changed) renderList();
        const changed = (update.samples || []).map(Number).filter(Boolean);
        if (!changed.length) return;
        for (const id of changed) invalidate(id);
        const query = listQuery();
        const parts = L.chunks(changed, 900);
        const rows = [];
        for (const part of parts) {
            const res = await getJSON('/api/files?' + listQuery('limit=' + part.length + '&ids=' + part.join(',')), true);
            if (res.ok) rows.push(...(res.body.samples || []));
        }
        // the rows match the filter of that moment: if it changed meanwhile, the reload it started is newer
        if (query === listQuery()) {
            const merged = window.mergeChangedRows(S.files, changed, rows, { pageFull: S.files.length < S.total });
            S.files = merged.files;
            S.total = Math.max(0, S.total + merged.added - merged.removed);
            renderList();
            loadCounts(true);
        }
        if (S.route.sampleId != null && changed.includes(S.route.sampleId)) showDetailSafe({ background: true });
    }

    // ── themes ──────────────────────────────────────────────────────────
    let themeFrame = 0;
    function rethemeChart() {
        cancelAnimationFrame(themeFrame);
        themeFrame = requestAnimationFrame(() => {
            drawChart();                      // the open sample's chart, if it is drawn, in the new tokens
        });
    }

    // ── start ───────────────────────────────────────────────────────────
    async function start() {
        if (!$('main') || !R || !L) return;
        S.overlay = loadOverlay();
        if (S.route.legacy) history.replaceState(null, '', R.build(S.route));
        // the remembered order, unless the link says one
        if (!/[?&]sort=/.test(location.search)) S.route.filters.sort = O.loadSortMode();
        renderFilters();
        wireRows();
        wireBulk();
        wireHeader();
        wireKeys();
        wireFilters();
        wireChart();
        SH.onInstruments((body) => {
            S.instruments = (body.instruments || []).map(i => ({ id: i.id, name: i.name || i.id }));
            S.names = {};
            for (const i of S.instruments) S.names[i.id] = i.name;
            renderFilters();
            renderList();
            if (S.row) renderHeader(S.row, entry(S.row.sample_id).meta || {});
        });
        settingsAndStandards();
        window.addEventListener('popstate', () => {
            const before = S.route;
            S.route = R.parse(location.pathname, location.search);
            renderFilters(true);
            if (R.filesQuery(before.filters) !== R.filesQuery(S.route.filters)) { S.sel = L.clear(); loadList(); }
            else if (before.filters.sort !== S.route.filters.sort) renderList();
            if (before.sampleId !== S.route.sampleId || before.view !== S.route.view) showDetailSafe();
            else if (S.route.view === 'compare' && S.compare && S.compare.setStandard && S.route.standard && S.route.standard !== before.standard) {
                S.compare.setStandard(S.route.standard);
            }
        });
        document.addEventListener('gc:adjust', (ev) => {
            const open = !!(ev.detail && (ev.detail.open === undefined ? true : ev.detail.open));
            $('main').classList.toggle('adjusting', open && S.route.view === 'compare');
        });
        window.addEventListener('gc:theme', rethemeChart);
        document.addEventListener('gc:theme', rethemeChart);
        new MutationObserver(rethemeChart).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        window.addEventListener('resize', () => { const el = $('chrom'); if (window.Plotly && CH.plotted && el.clientWidth) window.Plotly.Plots.resize(el); });
        await loadList();
        showDetailSafe();
        if (window.GCLive && typeof window.GCLive.subscribe === 'function') {
            window.GCLive.start();
            window.GCLive.subscribe((u) => { onLive(u).catch(e => console.error('[samples] live', e)); });
        }
        window.GCSamples = { state: S, openSample, setView, setFilters, onLive, addOverlay, ready: true };
    }

    document.addEventListener('DOMContentLoaded', () => { start().catch(e => { console.error(e); SH.toast('The Samples page failed to start: ' + e.message, 'err'); }); });
})();
