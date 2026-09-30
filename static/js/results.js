/* results.js (v5.0 lane R): the /results page's DOM. The logic is
   results_logic.js (GCResults, node-tested); this file fetches, renders with
   textContent only, and keeps the address bar current.

   * Filters (?instrument=&status=&q=&range=&flagged=) are in the URL: a
     change pushes a history entry (typing replaces it), Back/Forward restore
     the view, a reload keeps it.
   * Key points / All points and Corrected D86 are remembered in this browser
     (localStorage 'gc.results.points', 'gc.correctedD86'; the Settings page
     sets the D86 default too).
   * Each row links to /samples/<id>/data; ticking rows overlays their
     distillation curves (Plotly, the token-driven monochrome template,
     re-themed on 'gc:theme').
   * Live: a GCLive update naming samples (or a reset) reloads the rows in the
     background, at most every 5 s; the selection is kept. */
(function () {
    'use strict';
    const R = window.GCResults;
    const U = window.GCUi;
    const $ = (id) => document.getElementById(id);
    const POINTS_KEY = 'gc.results.points';
    const D86_KEY = 'gc.correctedD86';
    const PAGE = 300;

    function load(key) { try { return window.localStorage.getItem(key); } catch (_e) { return null; } }
    function save(key, v) { try { window.localStorage.setItem(key, v); } catch (_e) { /* private mode */ } }
    function toast(m, k) { if (window.GCShell) window.GCShell.toast(m, k); }
    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = String(text);
        return e;
    }

    const state = {
        filters: R.parseFilters(location.search),
        points: R.pointsMode(load(POINTS_KEY)),
        corrected: load(D86_KEY) === '1',
        table: null,            // /api/table
        files: [],              // /api/files samples
        total: 0,
        rows: [],
        shown: PAGE,
        sort: { key: null, dir: 'asc' },
        selected: [],           // sample ids, in the order ticked
        curves: new Map(),      // sample id -> {percent, temperature}
        names: {},              // instrument id -> name
        instruments: [],
        loadedOnce: false,
    };

    // ── data ────────────────────────────────────────────────────────────────
    async function getJSON(path, background) {
        return window.GCShell.getJSON(path, background);
    }
    function today() {
        const t = window.GCLive && window.GCLive.serverToday ? window.GCLive.serverToday() : null;
        if (t) return t;
        const d = new Date();
        return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    }

    let seq = 0;
    async function reload(background) {
        const mine = ++seq;
        if (!state.loadedOnce) $('count').textContent = 'Loading…';
        const [t, f] = await Promise.all([
            (background || !state.table) ? getJSON('/api/table', background) : Promise.resolve({ status: 200, body: state.table }),
            getJSON('/api/files?' + R.filesQuery(state.filters, today()), background),
        ]);
        if (mine !== seq) return;                         // a newer load won
        if (t.status !== 200 || !t.body || !Array.isArray(t.body.columns)) {
            showError((t.body && t.body.error) || ('The results could not be loaded (HTTP ' + t.status + ').'));
            return;
        }
        if (f.status !== 200 || !f.body || !Array.isArray(f.body.samples)) {
            showError((f.body && f.body.error) || ('The runs could not be loaded (HTTP ' + f.status + ').'));
            return;
        }
        state.table = t.body;
        state.files = f.body.samples;
        state.total = f.body.total || 0;
        state.loadedOnce = true;
        rebuild();
    }

    function rebuild() {
        const rows = R.buildRows(state.files, state.table, { corrected: state.corrected });
        state.rows = R.sortRows(R.applyClientFilters(rows, state.filters), state.sort.key, state.sort.dir);
        render();
        renderOverlay();
    }

    function showError(msg) {
        $('count').textContent = 'Results';
        $('empty').hidden = false;
        $('empty').textContent = msg;
        $('tbody').replaceChildren();
    }

    // ── the table ───────────────────────────────────────────────────────────
    function sortButton(label, key, numeric) {
        const b = el('button', 'th-sort' + (numeric ? ' num' : ''), label);
        b.type = 'button';
        b.dataset.sort = key;
        const on = state.sort.key === key;
        if (on) b.append(el('span', 'arrow', state.sort.dir === 'asc' ? ' ▲' : ' ▼'));
        b.addEventListener('click', () => {
            if (state.sort.key === key) state.sort.dir = state.sort.dir === 'asc' ? 'desc' : 'asc';
            else { state.sort.key = key; state.sort.dir = numeric ? 'desc' : 'asc'; }
            rebuild();
        });
        return b;
    }
    function th(cls, content, sortKey) {
        const c = el('th', cls);
        c.scope = 'col';
        if (content instanceof Node) c.append(content); else if (content) c.textContent = content;
        if (sortKey && state.sort.key === sortKey) c.setAttribute('aria-sort', state.sort.dir === 'asc' ? 'ascending' : 'descending');
        return c;
    }

    function render() {
        const g = R.groupColumns(state.table.columns, state.points);
        document.querySelectorAll('[data-points]').forEach((b) =>
            b.setAttribute('aria-checked', b.dataset.points === state.points ? 'true' : 'false'));
        $('results').classList.toggle('all-points', state.points === 'all');

        // two header rows: the method groups over their cuts
        const lead = 5;
        const top = el('tr', 'groups');
        const cap = el('th', 'lead', '');
        cap.colSpan = lead;
        cap.scope = 'colgroup';
        top.append(cap);
        g.groups.forEach((grp) => {
            const c = el('th', 'grp grp-' + grp.method, grp.title);
            c.colSpan = grp.cols.length;
            c.scope = 'colgroup';
            c.dataset.group = grp.method;
            top.append(c);
        });
        const tailCap = el('th', 'tail', '');
        tailCap.colSpan = Math.max(1, g.tail.length);
        top.append(tailCap);
        const head = el('tr', 'cols');
        const sel = th('c-sel', null);
        sel.append(el('span', 'visually-hidden', 'Overlay'));
        head.append(sel, th('c-st', el('span', 'visually-hidden', 'Status')),
            th('c-lab', sortButton('Lab ID', 'lab_id', false), 'lab_id'),
            th('c-gc', 'GC'),
            th('c-inj', sortButton('Injected', 'injection_dt', false), 'injection_dt'));
        g.groups.forEach((grp) => grp.cols.forEach((c, i) => {
            const cell = th('num' + (i === 0 ? ' first' : ''), sortButton(c.label, c.col, true), c.col);
            cell.dataset.col = c.col;
            cell.title = (grp.method === 'd86' ? 'D86 ' : 'D2887 ') + c.label + ' (°C)';
            head.append(cell);
        }));
        if (g.tail.length) g.tail.forEach((c, i) => head.append(th('tailc' + (i === 0 ? ' first' : ''), c.label)));
        else head.append(th('tailc first', ''));
        $('thead').replaceChildren(top, head);

        const rows = state.rows.slice(0, state.shown);
        const now = Date.now();
        $('tbody').replaceChildren(...rows.map((r) => rowEl(r, g, now)));

        const n = state.rows.length;
        $('count').textContent = n === 1 ? '1 run' : n.toLocaleString() + ' runs';
        const notes = [];
        if (state.total > state.files.length) notes.push('the newest ' + state.files.length.toLocaleString() + ' of ' + state.total.toLocaleString() + ' matching');
        notes.push(state.corrected ? 'D86 with correction factors' : 'D86 before correction factors');
        $('count-note').textContent = notes.join(' · ');
        $('empty').hidden = n > 0;
        if (!n) $('empty').textContent = emptyText();
        const more = $('more');
        more.hidden = n <= state.shown;
        more.textContent = 'Show ' + Math.min(PAGE, n - state.shown) + ' more of ' + (n - state.shown).toLocaleString();
    }

    function emptyText() {
        const f = state.filters;
        if (f.q || f.instrument.length || f.status !== 'any' || f.range !== 'all' || f.flagged) {
            return 'No runs match these filters. Clear a filter to see more.';
        }
        return 'No runs yet. Results appear here as soon as a GC sends its first run.';
    }

    function rowEl(r, g, now) {
        const tr = el('tr', r.hasResult ? '' : 'no-result');
        tr.dataset.sampleId = r.sample_id;
        if (r.backfill) tr.classList.add('backfill');
        if (state.selected.includes(r.sample_id)) tr.classList.add('is-picked');
        const st = U.sampleStatus(r.status);

        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = state.selected.includes(r.sample_id);
        cb.disabled = !r.hasResult;
        cb.setAttribute('aria-label', (r.hasResult ? 'Compare the curve of ' : 'No curve yet for ') + r.display_name);
        cb.addEventListener('change', () => toggle(r.sample_id, cb));
        const c0 = el('td', 'c-sel'); c0.append(cb);

        const c1 = el('td', 'c-st');
        const gl = el('span', 'glyph ' + st.glyph);
        gl.setAttribute('role', 'img');
        gl.setAttribute('aria-label', st.text);
        gl.title = st.text;
        c1.append(gl);

        const c2 = el('td', 'c-lab');
        const a = el('a', 'lab', r.display_name);
        a.href = '/samples/' + encodeURIComponent(r.sample_id) + '/data';
        a.title = 'Open ' + r.display_name + ': its numbers, calibration and history';
        c2.append(a);
        if (r.backfill) c2.append(el('span', 'tag', 'backfill'));
        if (r.flags && r.flags.length) {
            const fl = el('span', 'tag flag', r.flags.map((x) => x.name || x).join(', '));
            fl.title = 'Flagged: ' + fl.textContent;
            c2.append(fl);
        }
        const c3 = el('td', 'c-gc');
        c3.append(el('span', 'gc', state.names[r.instrument] || r.instrument || ''));
        const c4 = el('td', 'c-inj', r.injection_dt ? U.clockTime(r.injection_dt, now) : '—');
        if (r.injection_dt) c4.title = r.injection_dt.replace('T', ' ');
        tr.append(c0, c1, c2, c3, c4);

        g.groups.forEach((grp) => grp.cols.forEach((c, i) => {
            const v = r.cells[c.col];
            const td = el('td', 'num' + (i === 0 ? ' first' : '') + (v === null ? ' none' : ''), R.fmtCell(v));
            if (r.notes[c.col]) td.title = r.notes[c.col];
            else if (typeof v === 'number') td.title = v.toFixed(2) + ' °C';
            tr.append(td);
        }));
        const note = R.rowNote(r);
        if (g.tail.length) {
            g.tail.forEach((c, i) => {
                let text = R.fmtCell(r.cells[c.col]);
                let cls = 'tailc' + (i === 0 ? ' first' : '');
                if (i === 0 && note) { text = note; cls += ' why'; }
                else if (c.col === 'Fit Score' && typeof r.cells[c.col] === 'string') text = r.cells[c.col];
                const td = el('td', cls, text);
                if (i === 0 && note) td.title = note;
                tr.append(td);
            });
        } else {
            tr.append(el('td', 'tailc first' + (note ? ' why' : ''), note || ''));
        }
        return tr;
    }

    // ── overlay ─────────────────────────────────────────────────────────────
    async function toggle(id, cb) {
        const before = state.selected;
        const next = R.toggleSelected(before, id);
        if (next.length === before.length && !before.includes(id)) {
            cb.checked = false;
            toast('At most ' + R.MAX_OVERLAY + ' curves at once. Untick one first.');
            return;
        }
        state.selected = next;
        const tr = cb.closest('tr');
        if (tr) tr.classList.toggle('is-picked', next.includes(id));
        if (next.includes(id) && !state.curves.has(id)) {
            const res = await getJSON('/api/samples/' + encodeURIComponent(id) + '/distillation-curve', false);
            if (res.status !== 200 || !res.body || !Array.isArray(res.body.percent)) {
                state.selected = state.selected.filter((x) => x !== id);
                cb.checked = false;
                if (tr) tr.classList.remove('is-picked');
                toast((res.body && res.body.error) || ('No curve for that run (HTTP ' + res.status + ').'), 'err');
                renderOverlay();
                return;
            }
            state.curves.set(id, { percent: res.body.percent, temperature: res.body.temperature });
        }
        renderOverlay();
    }

    function colors() {
        const cs = getComputedStyle(document.documentElement);
        const v = (n, d) => (cs.getPropertyValue(n) || '').trim() || d;
        return { ink: v('--chart-ink', '#0f172a'), ref: v('--chart-ref', '#a8b0bc'), grid: v('--chart-grid', '#eef0f3'),
                 axis: v('--chart-axis', '#64748b'), bg: v('--bg-card', '#ffffff'), font: v('--font', 'system-ui') };
    }

    function labelOf(id) {
        const f = state.files.find((x) => x.sample_id === id);
        return f ? (f.display_name || f.lab_id) : String(id);
    }

    function renderOverlay() {
        const sec = $('overlay');
        const picked = state.selected.filter((id) => state.curves.has(id));
        sec.hidden = picked.length === 0;
        $('select-hint').textContent = picked.length
            ? picked.length + ' of ' + R.MAX_OVERLAY + ' on the chart'
            : 'Tick up to ' + R.MAX_OVERLAY + ' runs to compare their curves.';
        $('picked').replaceChildren(...picked.map((id, i) => {
            const st = R.overlayStyle(i);
            const li = el('li', 'pick');
            const sw = el('span', 'swatch dash-' + st.dash + ' shade-' + st.shade);
            sw.setAttribute('aria-hidden', 'true');
            const x = el('button', 'icon-btn');
            x.type = 'button';
            x.setAttribute('aria-label', 'Take ' + labelOf(id) + ' off the chart');
            x.append(el('span', 'ico ico-x'));
            x.addEventListener('click', () => {
                state.selected = state.selected.filter((s) => s !== id);
                const cb = document.querySelector('tr[data-sample-id="' + String(id).replace(/[^0-9]/g, '') + '"] input[type=checkbox]');
                if (cb) { cb.checked = false; cb.closest('tr').classList.remove('is-picked'); }
                renderOverlay();
            });
            li.append(sw, el('span', 'pick-name', labelOf(id)), x);
            return li;
        }));
        if (!picked.length) return;
        const note = $('chart-note');
        if (!window.Plotly) {
            note.hidden = false;
            note.textContent = 'The chart library did not load (no internet on this computer?). The numbers above are unaffected.';
            return;
        }
        note.hidden = true;
        const c = colors();
        const traces = R.overlayTraces(picked.map((id) => Object.assign({ sample_id: id, label: labelOf(id) }, state.curves.get(id))), c);
        const axis = (title) => ({ title: { text: title, font: { size: 12, color: c.axis } }, color: c.axis,
            gridcolor: c.grid, zeroline: false, linecolor: c.grid, tickfont: { size: 11, color: c.axis } });
        const layout = {
            margin: { l: 56, r: 16, t: 8, b: 44 }, height: 300, paper_bgcolor: c.bg, plot_bgcolor: c.bg,
            font: { family: c.font, color: c.axis }, showlegend: false, dragmode: 'zoom', hovermode: 'closest',
            xaxis: Object.assign(axis('% recovered'), { range: [0, 100] }), yaxis: axis('°C'),
        };
        window.Plotly.react($('chart'), traces, layout, { displayModeBar: false, responsive: true, doubleClick: 'reset' });
    }

    // ── filters and the address bar ─────────────────────────────────────────
    function syncControls() {
        const f = state.filters;
        $('f-q').value = f.q;
        $('f-range').value = f.range;
        $('f-status').value = f.status;
        $('f-flagged').setAttribute('aria-pressed', f.flagged ? 'true' : 'false');
        $('corrected').checked = state.corrected;
        renderInstrumentChips();
    }

    function renderInstrumentChips() {
        const box = $('f-inst');
        const f = state.filters;
        const chip = (id, text, on) => {
            const b = el('button', 'chip', text);
            b.type = 'button';
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
            b.dataset.inst = id;
            b.addEventListener('click', () => {
                let list = f.instrument.slice();
                if (!id) list = [];
                else if (list.includes(id)) list = list.filter((x) => x !== id);
                else list = list.concat([id]).sort();
                setFilters(Object.assign({}, f, { instrument: list }), true);
            });
            return b;
        };
        const ids = state.instruments.map((i) => i.id);
        f.instrument.forEach((id) => { if (!ids.includes(id)) ids.push(id); });
        box.replaceChildren(chip('', 'All instruments', f.instrument.length === 0),
            ...ids.map((id) => chip(id, state.names[id] || id, f.instrument.includes(id))));
    }

    function setFilters(next, push) {
        state.filters = R.parseFilters(R.filtersToQuery(next));   // one cleaning rule
        state.shown = PAGE;
        const url = location.pathname + R.filtersToQuery(state.filters) + location.hash;
        if (url !== location.pathname + location.search + location.hash) {
            if (push) history.pushState({ results: true }, '', url);
            else history.replaceState({ results: true }, '', url);
        }
        syncControls();
        reload(false);
    }

    let qTimer = null;
    let typing = false;
    function wire() {
        const range = $('f-range');
        range.replaceChildren(...Object.keys(R.RANGES).map((k) => { const o = el('option', null, R.RANGE_LABELS[k]); o.value = k; return o; }));
        const status = $('f-status');
        status.replaceChildren(...Object.keys(R.STATUS_LABELS).map((k) => { const o = el('option', null, R.STATUS_LABELS[k]); o.value = k; return o; }));
        $('filters').addEventListener('submit', (ev) => ev.preventDefault());
        $('f-q').addEventListener('input', () => {
            clearTimeout(qTimer);
            qTimer = setTimeout(() => {
                const push = !typing;               // the first keystroke pushes, the rest replace
                typing = true;
                setFilters(Object.assign({}, state.filters, { q: $('f-q').value }), push);
            }, 300);
        });
        $('f-q').addEventListener('blur', () => { typing = false; });
        range.addEventListener('change', () => setFilters(Object.assign({}, state.filters, { range: range.value }), true));
        status.addEventListener('change', () => setFilters(Object.assign({}, state.filters, { status: status.value }), true));
        $('f-flagged').addEventListener('click', () => setFilters(Object.assign({}, state.filters, { flagged: !state.filters.flagged }), true));
        $('corrected').addEventListener('change', () => {
            state.corrected = $('corrected').checked;
            save(D86_KEY, state.corrected ? '1' : '0');
            rebuild();
        });
        document.querySelectorAll('[data-points]').forEach((b) => b.addEventListener('click', () => {
            state.points = R.pointsMode(b.dataset.points);
            save(POINTS_KEY, state.points);
            render();
        }));
        $('more').addEventListener('click', () => { state.shown += PAGE; render(); });
        $('overlay-clear').addEventListener('click', () => {
            state.selected = [];
            document.querySelectorAll('#tbody input[type=checkbox]').forEach((cb) => { cb.checked = false; });
            document.querySelectorAll('#tbody tr.is-picked').forEach((tr) => tr.classList.remove('is-picked'));
            renderOverlay();
        });
        $('csv').addEventListener('click', downloadCsv);
        window.addEventListener('popstate', () => {
            state.filters = R.parseFilters(location.search);
            state.shown = PAGE;
            syncControls();
            reload(false);
        });
        // re-theme the chart when the theme changes (GCTheme's event; older
        // shells only change <html data-theme>, so watch that too)
        document.addEventListener('gc:theme', () => renderOverlay());
        new MutationObserver(() => renderOverlay()).observe(document.documentElement,
            { attributes: true, attributeFilter: ['data-theme'] });
    }

    function downloadCsv() {
        if (!state.table) return;
        const text = R.toCsv(state.rows, state.table.columns);
        const blob = new Blob(['﻿' + text], { type: 'text/csv;charset=utf-8' });
        const a = el('a');
        a.href = URL.createObjectURL(blob);
        a.download = R.csvName(today());
        document.body.append(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        toast(R.csvName(today()) + ' downloaded (' + state.rows.length + (state.rows.length === 1 ? ' run).' : ' runs).'));
    }

    // ── live ────────────────────────────────────────────────────────────────
    let liveTimer = null;
    let lastLive = 0;
    function onLive(u) {
        if (!state.loadedOnce || !u) return;
        if (!(u.reset || (u.samples && u.samples.length))) return;
        clearTimeout(liveTimer);
        const wait = Math.max(0, 5000 - (Date.now() - lastLive));
        liveTimer = setTimeout(() => { lastLive = Date.now(); reload(true); }, wait);
    }

    document.addEventListener('DOMContentLoaded', () => {
        if (!$('results')) return;
        wire();
        syncControls();
        window.GCShell.onInstruments((body) => {
            state.instruments = (body && body.instruments) || [];
            state.names = {};
            state.instruments.forEach((i) => { state.names[i.id] = i.name || i.id; });
            renderInstrumentChips();
            if (state.loadedOnce) render();
        });
        reload(false);
        if (window.GCLiveAdapter) window.GCLiveAdapter.subscribe(onLive);
    });

    window.GCResultsPage = { state, reload };
})();
