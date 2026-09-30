/* samples_logic.js (v5.0.0 lane S): the Samples page's pure rules.

   Pure and DOM-free: window.SamplesLogic in the browser, module.exports for
   tests/js/samples_logic.test.js. What is here:
     - a row's status (glyph + words, never colour alone) and the fix a held
       or failed row links to (Calibration / Corrections / Methods /
       Re-process / Release backfill);
     - multi-select: click, shift-click, drag ("paint"), select all shown,
       then "Select all N matching this filter" (the server-side filter);
     - bulk work in chunks through the existing endpoints, as one task with
       progress and Stop, and the confirmation naming the count and filter;
     - the Results card (Recovery | D86 | D2887), the Data table, the
       revision history, carbon ticks and the monochrome Plotly template. */
(function (root) {
    'use strict';

    const DV = (typeof module !== 'undefined' && module.exports)
        ? require('./distill_view.js') : root.DistillView;

    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const INJ_RE = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/;

    // ── numbers and words ───────────────────────────────────────────────
    function number(n) {
        const v = Math.round(Number(n) || 0);
        return String(Math.abs(v)).replace(/\B(?=(\d{3})+(?!\d))/g, ',').replace(/^/, v < 0 ? '−' : '');
    }
    function plural(n, one, many) {
        return number(n) + ' ' + (Number(n) === 1 ? one : (many || one + 's'));
    }
    /** A temperature with `digits` decimals, a real minus sign; '—' for none. */
    function fmt(v, digits) {
        if (v === null || v === undefined || v === '' || !Number.isFinite(Number(v))) return '—';
        const s = Number(v).toFixed(digits);
        return s.startsWith('-') ? '−' + s.slice(1) : s;
    }
    function round2(v) {
        if (v === null || v === undefined || v === '' || !Number.isFinite(Number(v))) return null;
        return Math.round(Number(v) * 100) / 100;
    }

    // ── status and the fix ──────────────────────────────────────────────
    function _inst(r) { return encodeURIComponent(String((r && r.instrument) || '')); }

    /** {group, glyph, text, reason, fix} of a list row / sample. ``fix`` is
        {label, href} (a page that fixes it) or {label, action} (a button). */
    function rowStatus(r) {
        const s = (r && r.status) || '';
        const inst = _inst(r);
        const err = r && r.error ? String(r.error) : '';
        switch (s) {
        case 'final':
            if (r.backfill && !r.released) {
                return { group: 'final', glyph: 'final', text: 'Final', reason: 'Backfill · not released',
                         fix: { label: 'Release backfill', href: '/instruments/' + inst + '#backfill' } };
            }
            return { group: 'final', glyph: 'final', text: 'Final', reason: '', fix: null };
        case 'received':
            return { group: 'processing', glyph: 'working', text: 'Processing', reason: 'Processing…', fix: null };
        case 'awaiting_calibration':
            return { group: 'held', glyph: 'held', text: 'Held', reason: 'Held · waiting for calibration',
                     fix: { label: 'Calibration', href: '/calibration?instrument=' + inst } };
        case 'pending_corrections':
            return { group: 'held', glyph: 'held', text: 'Held', reason: 'Held · waiting for correction factors',
                     fix: { label: 'Corrections', href: '/instruments/' + inst + '#corrections' } };
        case 'other_method':
            return { group: 'held', glyph: 'held', text: 'Held',
                     reason: 'Held · other method' + (r.method_name ? ' (' + r.method_name + ')' : ''),
                     fix: { label: 'Methods', href: '/instruments/' + inst + '#methods' } };
        case 'review_method':
            return { group: 'held', glyph: 'held', text: 'Held', reason: 'Held · the method needs review',
                     fix: { label: 'Methods', href: '/instruments/' + inst + '#methods' } };
        case 'raw_only':
            return { group: 'held', glyph: 'held', text: 'Held', reason: 'Held · stored without a result',
                     fix: { label: 'Re-process', action: 'reprocess' } };
        case 'error':
            return { group: 'error', glyph: 'error', text: 'Error', reason: err || 'Processing failed',
                     fix: { label: 'Re-process', action: 'reprocess' } };
        default:
            return { group: 'held', glyph: 'held', text: s || 'Unknown', reason: err || ('Status: ' + (s || 'unknown')),
                     fix: null };
        }
    }

    /** A final row's second line: its best fit, else its method. */
    function rowDetail(r) {
        if (r && r.best_fit && r.best_fit.label) return String(r.best_fit.label);
        return r && r.method_name ? String(r.method_name) : '';
    }
    function flagText(r) {
        const flags = (r && Array.isArray(r.flags)) ? r.flags : [];
        return flags.map(f => (f && typeof f === 'object') ? String(f.name || f.label || '') : String(f))
            .filter(Boolean).join(', ');
    }

    /** "Injected Tue, Sep 29 at 14:28" (the hub's local clock, as stored). */
    function injectedText(dt, source) {
        const m = INJ_RE.exec(String(dt || ''));
        if (!m) return 'No injection time';
        const [, y, mo, d, hh, mm] = m;
        if (source === 'mtime') return 'No injection time in the CDF (file time ' + MONTHS[Number(mo) - 1] + ' ' + Number(d) + ' ' + hh + ':' + mm + ')';
        const day = DAYS[new Date(Date.UTC(Number(y), Number(mo) - 1, Number(d))).getUTCDay()];
        return 'Injected ' + day + ', ' + MONTHS[Number(mo) - 1] + ' ' + Number(d) + ' at ' + hh + ':' + mm;
    }

    // ── multi-select ────────────────────────────────────────────────────
    // {ids: Set<number>, anchor: number|null, all: bool, total: number}
    function selection() { return { ids: new Set(), anchor: null, all: false, total: 0 }; }
    function _copy(s) { return { ids: new Set(s.ids), anchor: s.anchor, all: false, total: 0 }; }
    function selectedIds(s) { return Array.from(s.ids); }
    function toggle(s, id) {
        const n = _copy(s);
        if (n.ids.has(id)) n.ids.delete(id); else n.ids.add(id);
        n.anchor = id;
        return n;
    }
    /** Shift-click: the rows from the anchor to *id* (in the shown order) are added. */
    function extend(s, order, id) {
        const n = _copy(s);
        const a = order.indexOf(s.anchor);
        const b = order.indexOf(id);
        if (a < 0 || b < 0) { n.ids.add(id); n.anchor = id; return n; }
        const [lo, hi] = a <= b ? [a, b] : [b, a];
        for (let i = lo; i <= hi; i++) n.ids.add(order[i]);
        return n;
    }
    /** Drag across checkboxes: every row passed over takes one state. */
    function paint(s, ids, on) {
        const n = _copy(s);
        for (const id of ids) { if (on) n.ids.add(id); else n.ids.delete(id); }
        return n;
    }
    function selectShown(s, order) { return paint(s, order, true); }
    function selectAllMatching(s, total) {
        const n = _copy(s);
        n.all = true;
        n.total = Number(total) || 0;
        return n;
    }
    function clear() { return selection(); }
    /** Rows that left the list leave the selection (unless all matching). */
    function keepOnly(s, ids) {
        if (s.all) return s;
        const keep = new Set(ids);
        const n = _copy(s);
        for (const id of s.ids) if (!keep.has(id)) n.ids.delete(id);
        return n;
    }
    /** What the bulk bar says, and whether it offers "Select all N matching". */
    function bulkBar(s, counts) {
        const shown = Number(counts && counts.shown) || 0;
        const total = Number(counts && counts.total) || 0;
        if (s.all) {
            return { count: s.total, text: 'All ' + number(s.total) + ' matching this filter are selected',
                     offerAll: null, all: true };
        }
        const count = s.ids.size;
        const allShown = count > 0 && count === shown;
        if (allShown && total > shown) {
            return { count, text: 'All ' + number(count) + ' shown are selected',
                     offerAll: 'Select all ' + number(total) + ' matching this filter', all: false };
        }
        return { count, text: number(count) + ' selected', offerAll: null, all: false };
    }

    // ── bulk work ───────────────────────────────────────────────────────
    const CHUNK = { reprocess: 200, lims: 100 };
    // what one bulk action may carry at most (report PDFs take seconds each)
    const BULK_LIMIT = { reports: 200, queue: 200, ids: 20000 };

    function chunks(ids, size) {
        const out = [];
        for (let i = 0; i < (ids || []).length; i += size) out.push(ids.slice(i, i + size));
        return out;
    }

    /** Run fn(chunk) → {done, refused?} over the chunks in turn. A chunk that
        throws is counted as failed and the rest still run; shouldStop() is
        asked between chunks. → {done, refused, failed, stopped, total}. */
    async function runChunks(ids, size, fn, onProgress, shouldStop) {
        const out = { done: 0, refused: [], failed: 0, stopped: false, total: (ids || []).length };
        let sent = 0;
        for (const part of chunks(ids || [], size)) {
            if (shouldStop && shouldStop()) { out.stopped = true; break; }
            try {
                const r = (await fn(part)) || {};
                out.done += Number(r.done) || 0;
                if (Array.isArray(r.refused)) out.refused.push(...r.refused);
            } catch (_e) {
                out.failed += part.length;
            }
            sent += part.length;
            if (onProgress) onProgress({ sent, total: out.total, done: out.done });
        }
        if (!out.stopped && shouldStop && shouldStop() && sent < out.total) out.stopped = true;
        return out;
    }

    function outcomeText(what, r) {
        const parts = [what + ': ' + number(r.done) + ' of ' + number(r.total) + ' done'];
        if (r.refused && r.refused.length) parts.push(number(r.refused.length) + ' refused');
        if (r.failed) parts.push(number(r.failed) + ' failed');
        if (r.stopped) parts.push('stopped');
        return parts.join(' · ');
    }

    const STATUS_WORDS = { final: 'Final', held: 'Held', error: 'Error', processing: 'Processing' };
    /** "GC-1 · Held or Error · Not sent to QBench · lab ID contains “403”". */
    function filterText(f, names) {
        const parts = [];
        const inst = (f && f.instrument) || [];
        if (inst.length) parts.push(inst.map(i => (names && names[i]) || i).join(' or '));
        const st = (f && f.status) || [];
        if (st.length) parts.push(st.map(s => STATUS_WORDS[s] || s).join(' or '));
        if (f && f.notsent) parts.push('Not sent to QBench');
        if (f && String(f.q || '').trim()) parts.push('lab ID contains “' + String(f.q).trim() + '”');
        return parts.length ? parts.join(' · ') : 'All samples';
    }

    /** {title, body, ok} of a bulk action's confirmation. */
    function confirmText(kind, count, filter) {
        const n = plural(count, 'sample');
        const scope = filter ? 'Every sample matching this filter: ' + filter + '. ' : '';
        switch (kind) {
        case 'reprocess':
            return { title: 'Re-process ' + n + '?',
                     body: scope + 'Each is queued again with its recorded blank and corrections.',
                     ok: 'Re-process ' + n };
        case 'lims':
            return { title: 'Export ' + n + ' to LIMS?',
                     body: scope + 'Final, gated results are written to the results file again; others are refused and listed.',
                     ok: 'Export ' + n + ' to LIMS' };
        case 'queue':
            return { title: 'Add ' + n + ' to the report queue?',
                     body: scope + 'Each uses its best-fit standard; change it in Compare before sending.',
                     ok: 'Add ' + n + ' to the report queue' };
        default:
            return { title: 'Download ' + plural(count, 'report') + '?',
                     body: scope + 'One PDF per sample against its best-fit standard, built on the hub as a ZIP.',
                     ok: 'Download ' + plural(count, 'report') };
        }
    }

    function countsText(c) {
        const base = plural(c.total, 'sample');
        return c.today ? base + ' · ' + number(c.today) + ' today' : base;
    }

    // ── the numbers ─────────────────────────────────────────────────────
    const LABELS = DV.D86_LABELS;
    const KEY = ['IBP', '50%', 'FBP'];
    function _col(prefix, label) {
        if (label === 'IBP' || label === 'FBP') return prefix + ' ' + label;
        return prefix + ' T' + label.replace('%', '');
    }

    /** The Results card: [{label, d86, d2887, note, key}] (D86 per the toggle). */
    function resultRows(curve, corrected) {
        const c = curve || {};
        const view = DV.dashboardD86({ d86: c.d86 || {}, d86_uncorrected: c.d86_uncorrected, d2887: c.d2887 || {} },
            !!corrected, null);
        return LABELS.map(l => ({
            label: l,
            d86: view.values[l],
            d2887: round2((c.d2887 || {})[_col('2887', l)]),
            note: view.notes[l] || null,
            key: KEY.includes(l),
        }));
    }

    /** The Data table: D2887, D86 before and after the correction, the correction. */
    function dataRows(curve) {
        const c = curve || {};
        return LABELS.map(l => {
            const raw = round2((c.d86_uncorrected || {})[_col('D86', l)]);
            const reported = round2((c.d86 || {})[_col('D86', l)]);
            return {
                label: l,
                d2887: round2((c.d2887 || {})[_col('2887', l)]),
                raw,
                correction: raw !== null && reported !== null ? round2(reported - raw) : null,
                reported,
            };
        });
    }

    /** The Data table as tab-separated text (Copy table). */
    function dataTableText(rows) {
        const cell = (v) => (v === null || v === undefined ? '' : Number(v).toFixed(2));
        const lines = ['Recovery\tD2887 °C\tD86 raw\tCorrection\tD86 reported'];
        for (const r of rows) lines.push([r.label, cell(r.d2887), cell(r.raw), cell(r.correction), cell(r.reported)].join('\t'));
        return lines.join('\n');
    }

    const REASON_WORDS = {
        processed: 'processed', reprocess: 're-processed', import: 'imported from v1',
        'export-lims': 'exported to LIMS', 'corrections-released': 'correction factors changed',
        replace: 'replaced by a new file',
    };
    function _when(dt) { return String(dt || '').replace('T', ' ').slice(0, 16); }
    function _who(by) {
        const s = String(by || '').replace(/\s*\([^)]*\)\s*$/, '').trim();
        return s && s !== 'worker' ? s : 'automatic';
    }
    /** The Data view's History: [{title, detail, at}], newest first. */
    function historyItems(meta) {
        const m = meta || {};
        const out = [];
        for (const r of (m.revisions || [])) {
            out.push({ at: String(r.processed_at || ''), title: 'Revision ' + r.revision + ' — ' + (REASON_WORDS[r.reason] || r.reason || 'processed'),
                       detail: _when(r.processed_at) + ' · ' + _who(r.by) });
        }
        if (m.qbench_uploaded_at) {
            out.push({ at: String(m.qbench_uploaded_at), title: 'Sent to QBench',
                       detail: _when(m.qbench_uploaded_at) + (m.qbench_revision ? ' · revision ' + m.qbench_revision : '') });
        }
        out.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0));
        return out;
    }

    // ── the chart ───────────────────────────────────────────────────────
    /** Carbon ticks along the top: short hairlines for every carbon of the
        sample's own ladder, labels thinned to at most maxLabels. */
    function carbonTicks(times, carbons, colours, maxLabels, xSpan) {
        const n = Math.min((times || []).length, (carbons || []).length);
        const shapes = [];
        const annotations = [];
        if (!n) return { shapes, annotations };
        const xs = times.slice(0, n).map(Number);
        const span = xSpan > 0 ? xSpan : (Math.max(...xs) - Math.min(...xs));
        const gap = span / Math.max(1, maxLabels || 14) - 1e-9;
        let last = -Infinity;
        for (let i = 0; i < n; i++) {
            const x = xs[i];
            shapes.push({ type: 'line', x0: x, x1: x, y0: 1.0, y1: 1.03, yref: 'paper',
                          line: { color: colours.axis, width: 1 } });
            if (x - last >= gap) {
                last = x;
                annotations.push({ x, y: 1.035, yref: 'paper', text: 'C' + carbons[i], showarrow: false,
                                   yanchor: 'bottom', font: { color: colours.axis, size: 10 } });
            }
        }
        return { shapes, annotations };
    }

    const CHART_CONFIG = { displayModeBar: false, responsive: true, doubleClick: 'reset', scrollZoom: false };

    /** The monochrome Plotly layout from the page's tokens. */
    function chartLayout(c) {
        const axis = { color: c.axis, gridcolor: c.grid, zeroline: false, linecolor: c.grid,
                       tickfont: { color: c.axis, size: 11 }, title: { font: { color: c.axis, size: 11 } } };
        return {
            paper_bgcolor: c.bg, plot_bgcolor: c.bg, font: { family: c.font, color: c.axis, size: 11 },
            margin: { l: 48, r: 16, t: 30, b: 40 }, showlegend: false, dragmode: 'zoom', hovermode: 'closest',
            xaxis: Object.assign({}, axis, { title: { text: 'Retention time (min)', font: { color: c.axis, size: 11 } } }),
            yaxis: Object.assign({}, axis),
        };
    }

    // ── keyboard ────────────────────────────────────────────────────────
    function step(order, current, dir) {
        if (!order || !order.length) return null;
        const i = order.indexOf(current);
        if (i < 0) return order[0];
        return order[Math.max(0, Math.min(order.length - 1, i + dir))];
    }

    // ── the report queue item (lane C's GCReportQueue.add) ──────────────
    function queueItem(r, standardNames, picked) {
        const names = standardNames || [];
        const best = r && r.best_fit && r.best_fit.label ? String(r.best_fit.label) : '';
        const std = picked || (names.includes(best) ? best : '');
        return { sample_id: r.sample_id, lab_id: r.lab_id, sample_name: r.display_name || r.lab_id,
                 instrument: r.instrument, standard_name: std };
    }

    const api = {
        number, plural, fmt, rowStatus, rowDetail, flagText, injectedText,
        selection, selectedIds, toggle, extend, paint, selectShown, selectAllMatching, clear, keepOnly, bulkBar,
        CHUNK, BULK_LIMIT, chunks, runChunks, outcomeText, filterText, confirmText, countsText,
        resultRows, dataRows, dataTableText, historyItems, carbonTicks, CHART_CONFIG, chartLayout, step, queueItem,
    };
    root.SamplesLogic = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
