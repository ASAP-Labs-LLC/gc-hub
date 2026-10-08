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
       revision history, carbon ticks and the monochrome Plotly template;
     - v6: the checkbox press (pressBox/isChecked), why a chart is not drawn
       (chartReason), the backfill reason, and stacking chromatograms (the
       overlay set, its styles and the Overlay/Stacked traces). */
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
    /** "Review: <note>" for a sample with a review note (a late blank, a Lab ID
        LEM will misread, v5.1.0), else ''. Text only: shown with textContent. */
    function reviewText(r) {
        const note = r && r.review_note != null ? String(r.review_note).trim() : '';
        return note ? 'Review: ' + note : '';
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
            // the list's total now (live updates move it), else the one it had when chosen
            const n = counts && counts.total != null ? total : (Number(s.total) || 0);
            return { count: n, text: 'All ' + number(n) + ' matching this filter are selected',
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
    function resultRows(curve, corrected, convert) {
        const c = curve || {};
        const view = DV.dashboardD86({ d86: c.d86 || {}, d86_uncorrected: c.d86_uncorrected, d2887: c.d2887 || {} },
            !!corrected, convert || null);
        return LABELS.map(l => ({
            label: l,
            d86: view.values[l],
            d2887: round2((c.d2887 || {})[_col('2887', l)]),
            note: view.notes[l] || null,
            key: KEY.includes(l),
        }));
    }

    /** The Data table: D2887, D86 before and after the correction, the correction. */
    function dataRows(curve, convert) {
        const c = curve || {};
        let converted = null;
        if (!c.d86_uncorrected && typeof convert === 'function') {
            const byLabel = {};
            LABELS.forEach((l) => { byLabel[l] = round2((c.d2887 || {})[_col('2887', l)]); });
            converted = DV.x4Midpoints(convert(byLabel) || {});
        }
        return LABELS.map(l => {
            const raw = converted ? round2(converted[l]) : round2((c.d86_uncorrected || {})[_col('D86', l)]);
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
    function queueItem(r, standard) {
        return { sample_id: r.sample_id, lab_id: r.lab_id, sample_name: 'GC Analysis',
                 instrument: r.instrument, standard_name: standard || '' };
    }

    /** A result-only (v1-imported) run has no CDF, so no distillation curve:
        its numbers come from its /api/table row, shaped like the curve's
        (d2887/d86 by CSV column; no stored uncorrected D86). null: no row. */
    function curveFromTable(table, sampleId) {
        if (!table || !Array.isArray(table.rows)) return null;
        const i = (table.sample_ids || []).indexOf(sampleId);
        if (i < 0) return null;
        const row = table.rows[i] || [];
        const out = { d2887: {}, d86: {}, d86_uncorrected: null, fromTable: true };
        (table.columns || []).forEach((col, j) => {
            const v = row[j];
            if (v === null || v === undefined || String(v).trim() === '' || !Number.isFinite(Number(v))) return;
            if (/^2887 /.test(col)) out.d2887[col] = Number(v);
            else if (/^D86 /.test(col)) out.d86[col] = Number(v);
        });
        return out;
    }

    // ── v6: the row's checkbox ──────────────────────────────────────────
    /** Is *id* selected? The one rule every render (row, box, bulk bar) uses. */
    function isChecked(s, id) { return !!(s && (s.all || s.ids.has(id))); }

    /** A press (mousedown) on a row's checkbox → {sel, drag}. Shift extends
        from the anchor (no drag); otherwise the row flips, "all matching"
        first becoming the rows shown, and a drag from here paints that state
        (drag.on). The box itself never toggles: it only shows isChecked. */
    function pressBox(s, order, id, shift) {
        if (shift) return { sel: extend(s, order, id), drag: null };
        const on = !isChecked(s, id);
        const n = paint(s.all ? selectShown(clear(), order) : s, [id], on);
        n.anchor = id;
        return { sel: n, drag: { on } };
    }

    // ── v6: why the chart is not drawn ──────────────────────────────────
    /** One line for a trace request that failed ({status, body}): a
        result-only import (no CDF), a missing file, the hub out of reach,
        else the server's own words. */
    function chartReason(res) {
        const status = Number(res && res.status) || 0;
        const err = res && res.body && res.body.error ? String(res.body.error) : '';
        if (status === 404 && /result-only/i.test(err)) return 'No chromatogram: this result was imported from v1 without its CDF.';
        if (status === 404 && /CDF is missing/i.test(err)) return 'No chromatogram: the stored CDF file is missing on the hub.';
        if (status === 404 && /standard/i.test(err)) return err;
        if (!status) return 'The chromatogram did not load: the hub did not answer' + (err ? ' (' + err + ')' : '') + '.';
        return 'The chromatogram did not load: ' + (err || 'HTTP ' + status) + (/[.!?]$/.test(err) ? '' : '.');
    }

    /** The row's backfill reason: "Backfill · not released", plus why it is
        backfill (backfill_logic.whyText) once the instrument's live-since
        facts are known. ``why`` is GCBackfill.whyText (injected for tests). */
    function backfillReason(r, info, why) {
        const base = 'Backfill · not released';
        if (!info || typeof why !== 'function') return base;
        const w = why(r, info);
        return w ? base + ' · ' + w : base;
    }

    // ── v6: stacking chromatograms (the classic overlay, on the new page) ─
    const OVERLAY_MAX = 7;                      // traces besides the open sample
    const OVERLAY_MODES = ['overlay', 'stacked'];

    function overlayKey(item) {
        if (!item) return '';
        return item.kind === 'standard' ? 'std:' + item.name : 's:' + Number(item.id);
    }
    function _cleanItem(it) {
        if (!it || typeof it !== 'object') return null;
        if (it.kind === 'standard') {
            const name = String(it.name || '').trim();
            return name ? { kind: 'standard', name, label: String(it.label || name) } : null;
        }
        const id = Number(it.id);
        if (!Number.isInteger(id) || id <= 0) return null;
        return { kind: 'sample', id, label: String(it.label || ('#' + id)), sub: it.sub ? String(it.sub) : '' };
    }
    /** {mode, items} from anything (sessionStorage junk → empty overlay). */
    function overlayState(raw) {
        const r = raw && typeof raw === 'object' ? raw : {};
        const mode = OVERLAY_MODES.includes(r.mode) ? r.mode : 'overlay';
        const items = [];
        const seen = new Set();
        for (const it of (Array.isArray(r.items) ? r.items : [])) {
            const c = _cleanItem(it);
            if (!c || seen.has(overlayKey(c)) || items.length >= OVERLAY_MAX) continue;
            seen.add(overlayKey(c));
            items.push(c);
        }
        return { mode, items };
    }
    function overlayParse(text) {
        try { return overlayState(JSON.parse(String(text || ''))); } catch (_e) { return overlayState(null); }
    }
    function overlayStringify(ov) { return JSON.stringify(overlayState(ov)); }

    /** Add items (in order) → {ov, added, skipped: [{item, why}]}: never the
        open sample, never twice, at most OVERLAY_MAX. */
    function overlayAdd(ov, items, primaryId) {
        const cur = overlayState(ov);
        const out = { mode: cur.mode, items: cur.items.slice() };
        const have = new Set(out.items.map(overlayKey));
        const skipped = [];
        let added = 0;
        for (const raw of (items || [])) {
            const it = _cleanItem(raw);
            if (!it) continue;
            const key = overlayKey(it);
            if (it.kind === 'sample' && primaryId != null && it.id === Number(primaryId)) { skipped.push({ item: it, why: 'open' }); continue; }
            if (have.has(key)) { skipped.push({ item: it, why: 'already' }); continue; }
            if (out.items.length >= OVERLAY_MAX) { skipped.push({ item: it, why: 'full' }); continue; }
            have.add(key);
            out.items.push(it);
            added++;
        }
        return { ov: out, added, skipped };
    }
    function overlayRemove(ov, key) {
        const cur = overlayState(ov);
        return { mode: cur.mode, items: cur.items.filter(it => overlayKey(it) !== key) };
    }
    function overlayClear(ov) { return { mode: overlayState(ov).mode, items: [] }; }
    function overlayMode(ov, mode) {
        const cur = overlayState(ov);
        return { mode: OVERLAY_MODES.includes(mode) ? mode : cur.mode, items: cur.items };
    }
    /** The items drawn beside the open sample (the open one itself left out). */
    function overlayShown(ov, primaryId) {
        return overlayState(ov).items.filter(it => !(it.kind === 'sample' && it.id === Number(primaryId)));
    }
    /** What a toast says after adding. */
    function overlayAddText(r) {
        const parts = [];
        if (r.added) parts.push('Added ' + plural(r.added, 'trace') + ' to the chart');
        const full = r.skipped.filter(s => s.why === 'full').length;
        const dup = r.skipped.filter(s => s.why === 'already').length;
        if (full) parts.push(number(full) + ' not added: the chart holds ' + (OVERLAY_MAX + 1) + ' traces');
        if (dup && !r.added && !full) parts.push(dup === 1 ? 'Already on the chart' : 'Already on the chart: ' + number(dup));
        if (!parts.length && r.skipped.some(s => s.why === 'open')) parts.push('That is the sample open now');
        return parts.join(' · ');
    }

    /** Line styles, monochrome first: the open sample is solid ink; other
        samples step through ink/axis × dash; standards are the reference
        grey (as in Compare) with their own dashes. */
    const SAMPLE_STYLES = [['ink', 'solid'], ['ink', 'dash'], ['axis', 'solid'], ['ink', 'dot'], ['axis', 'dash'],
                           ['ink', 'dashdot'], ['axis', 'dot'], ['ink', 'longdash']];
    const STANDARD_STYLES = [['ref', 'solid'], ['ref', 'dash'], ['ref', 'dot'], ['ref', 'dashdot'], ['ref', 'longdash']];
    function traceStyles(kinds, colours) {
        let s = 0;
        let d = 0;
        return (kinds || []).map((k) => {
            const [tok, dash] = k === 'standard' ? STANDARD_STYLES[d++ % STANDARD_STYLES.length]
                : SAMPLE_STYLES[s++ % SAMPLE_STYLES.length];
            return { color: (colours && colours[tok]) || (colours && colours.ink) || '#000', dash };
        });
    }

    /** Plotly reads trace names as its own HTML subset: show them as text. */
    function plotlyText(s) {
        return String(s === null || s === undefined ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function _range(ys) {
        let lo = Infinity;
        let hi = -Infinity;
        for (const v of (ys || [])) {
            if (v === null || v === undefined || v === '') continue;
            const n = Number(v);
            if (!Number.isFinite(n)) continue;
            if (n < lo) lo = n;
            if (n > hi) hi = n;
        }
        return lo === Infinity ? { lo: 0, hi: 0 } : { lo, hi };
    }

    /** The chart's traces: list = [{key, label, kind, x, y}] (the open sample
        first). Overlay: as measured. Stacked: each trace's baseline (its
        lowest point) lifted by a step of 1.08 × the tallest trace's height,
        the open sample at the bottom. → {data, legend, step}. */
    function overlayTraces(list, mode, colours) {
        const items = (list || []).filter(t => t && Array.isArray(t.x) && Array.isArray(t.y));
        const styles = traceStyles(items.map(t => t.kind === 'standard' ? 'standard' : 'sample'), colours);
        const stacked = mode === 'stacked' && items.length > 1;
        const ranges = items.map(t => _range(t.y));
        const tallest = Math.max(0, ...ranges.map(r => r.hi - r.lo));
        const step = stacked ? (tallest > 0 ? tallest * 1.08 : 1) : 0;
        const data = items.map((t, i) => {
            const name = plotlyText(t.label);
            const y = stacked ? t.y.map(v => (v === null || v === undefined ? v : Number(v) - ranges[i].lo + i * step)) : t.y;
            return {
                x: t.x, y, type: 'scatter', mode: 'lines', name,
                line: { color: styles[i].color, width: i === 0 ? 1.5 : 1.25, dash: styles[i].dash },
                hovertemplate: stacked ? '%{x:.3f} min<extra>' + name + '</extra>'
                    : '%{x:.3f} min · %{y:.4~g}<extra>' + name + '</extra>',
            };
        });
        const legend = items.map((t, i) => ({ key: t.key, label: t.label, sub: t.sub || '', kind: t.kind,
                                              primary: i === 0, color: styles[i].color, dash: styles[i].dash }));
        return { data, legend, step };
    }

    /** SVG stroke-dasharray for a Plotly dash name (the legend's swatch). */
    function dashArray(dash) {
        return { solid: '', dash: '6 4', dot: '1.5 3', dashdot: '6 3 1.5 3', longdash: '11 4', longdashdot: '11 3 1.5 3' }[dash] || '';
    }

    // ── v7: the distillation curve on Overview ──────────────────────────
    // It draws the Results card's own rows (resultRows: D86 per the Corrected
    // D86 toggle, 40%/60% midpoints included), never numbers of its own, so
    // a dot and its Results cell always agree.

    /** % recovered of a Results label: IBP 0, FBP 100, '50%' 50. */
    function curvePct(label) {
        if (label === 'IBP') return 0;
        if (label === 'FBP') return 100;
        const n = parseFloat(String(label));
        return Number.isFinite(n) ? n : null;
    }

    /** The curve's two series from the Results rows: D86 (named as the
        toggle names it) and D2887, each [{label, pct, t, note}] with only the
        points that have a temperature. */
    const CURVE_MIDPOINT = { '40%': '30% and 50%', '60%': '50% and 70%' };
    function curveSeries(rows, corrected) {
        // the Results cell's tooltip, short enough for the callout: a 40/60 midpoint says so
        const noteOf = (r) => (!r.note || r.d86 === null ? null : CURVE_MIDPOINT[r.label]
            ? 'Midpoint of the ' + (corrected ? 'uncorrected ' : '') + CURVE_MIDPOINT[r.label] + ' values' : r.note);
        const pts = (field, withNote) => (rows || [])
            .map(r => ({ label: r.label, pct: curvePct(r.label), t: round2(r[field]), note: withNote ? noteOf(r) : null }))
            .filter(p => p.pct !== null && p.t !== null);
        return [
            { id: 'd86', name: corrected ? 'D86 corrected' : 'D86 uncorrected', short: 'D86', points: pts('d86', true) },
            { id: 'd2887', name: 'D2887', short: 'D2887', points: pts('d2887', false) },
        ];
    }

    /** A temperature as the Results card prints it, with its unit. */
    function tempText(t) {
        return t === null || t === undefined || !Number.isFinite(Number(t)) ? '—' : fmt(t, 1) + ' °C';
    }

    /** A dot's accessible name: "50%: 285.1 °C, D86 uncorrected". */
    function curvePointLabel(series, p) {
        return p.label + ': ' + tempText(p.t) + ', ' + series.name;
    }

    /** Round axis ticks covering [lo, hi]: at most about maxTicks steps of
        1, 2, 2.5 or 5 × 10ⁿ. */
    function niceTicks(lo, hi, maxTicks) {
        let a = Number(lo);
        let b = Number(hi);
        if (!Number.isFinite(a) || !Number.isFinite(b)) return { lo: 0, hi: 1, step: 1, ticks: [0, 1] };
        if (a > b) [a, b] = [b, a];
        if (a === b) { a -= 5; b += 5; }
        const raw = (b - a) / Math.max(1, maxTicks || 5);
        const mag = Math.pow(10, Math.floor(Math.log10(raw)));
        const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw - 1e-12);
        const nlo = Math.floor(a / step + 1e-9) * step;
        const nhi = Math.ceil(b / step - 1e-9) * step;
        const ticks = [];
        for (let v = nlo; v <= nhi + step / 2; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
        return { lo: nlo, hi: nhi, step, ticks };
    }

    /** The x labels for a plot this many pixels wide: IBP and FBP at the
        ends, the tens between (every 20 when narrow). Every point keeps its
        tick mark (marks), labelled or not. */
    function curveXTicks(plotWidth) {
        const every = plotWidth < 300 ? 20 : 10;
        const labels = [{ pct: 0, text: 'IBP' }];
        for (let p = every; p < 100; p += every) labels.push({ pct: p, text: String(p) });
        labels.push({ pct: 100, text: 'FBP' });
        const marks = LABELS.map(curvePct);
        return { labels, marks };
    }

    /** Pixel geometry for the series in a width × height box. → {plot:
        {left, top, right, bottom}, x(pct), y(t), yTicks, xTicks} (y grows
        downward, as in SVG). */
    function curveScale(series, width, height, margin) {
        const m = Object.assign({ l: 44, r: 16, t: 14, b: 30 }, margin || {});
        const ts = [];
        for (const s of (series || [])) for (const p of s.points) ts.push(p.t);
        const lo = ts.length ? Math.min(...ts) : 0;
        const hi = ts.length ? Math.max(...ts) : 100;
        const plotH = Math.max(40, height - m.t - m.b);
        const ny = niceTicks(lo, hi, Math.max(2, Math.min(7, Math.floor(plotH / 34))));
        const plot = { left: m.l, top: m.t, right: Math.max(m.l + 40, width - m.r), bottom: m.t + plotH };
        const x = (pct) => plot.left + (Number(pct) / 100) * (plot.right - plot.left);
        const y = (t) => plot.bottom - ((Number(t) - ny.lo) / ((ny.hi - ny.lo) || 1)) * (plot.bottom - plot.top);
        return { plot, x, y, yTicks: ny.ticks, xTicks: curveXTicks(plot.right - plot.left) };
    }

    /** The point under the pointer: the % column nearest in x (columns are
        unevenly spaced: 0, 5, 10 … 90, 95, 100), then of the series that
        have that point, the one nearest in y. null outside the plot (with
        `slack` pixels to spare) or when nothing is drawn. → {series, label}. */
    function curveHit(series, scale, px, py, slack) {
        const sl = slack === undefined ? 12 : slack;
        const p = scale.plot;
        if (px < p.left - sl || px > p.right + sl || py < p.top - sl || py > p.bottom + sl) return null;
        let best = null;
        for (const s of (series || [])) {
            for (const pt of s.points) {
                const dx = Math.abs(scale.x(pt.pct) - px);
                const dy = Math.abs(scale.y(pt.t) - py);
                if (!best || dx < best.dx - 0.5 || (Math.abs(dx - best.dx) <= 0.5 && dy < best.dy)) {
                    best = { series: s.id, label: pt.label, dx, dy };
                }
            }
        }
        return best ? { series: best.series, label: best.label } : null;
    }

    /** Find a point: → {series, point} or null. */
    function curveFind(series, sel) {
        if (!sel) return null;
        const s = (series || []).find(x => x.id === sel.series);
        const p = s && s.points.find(x => x.label === sel.label);
        return p ? { series: s, point: p } : null;
    }

    /** The keyboard path through the dots: ←/→ the previous/next point of
        the same series, ↑/↓ the other series at the same % (else its
        nearest), Home/End its first/last. → the new {series, label}, or the
        current one when the key goes nowhere; the first D86 point (else the
        first point) when nothing is chosen yet. */
    function curveStep(series, cur, key) {
        const list = (series || []).filter(s => s.points.length);
        if (!list.length) return null;
        const found = curveFind(list, cur);
        if (!found) return { series: list[0].id, label: list[0].points[0].label };
        const pts = found.series.points;
        const i = pts.indexOf(found.point);
        const at = (s, j) => ({ series: s.id, label: s.points[j].label });
        if (key === 'ArrowRight') return at(found.series, Math.min(pts.length - 1, i + 1));
        if (key === 'ArrowLeft') return at(found.series, Math.max(0, i - 1));
        if (key === 'Home') return at(found.series, 0);
        if (key === 'End') return at(found.series, pts.length - 1);
        if (key === 'ArrowUp' || key === 'ArrowDown') {
            const k = list.indexOf(found.series);
            const other = list[(k + (key === 'ArrowDown' ? 1 : list.length - 1)) % list.length];
            if (other === found.series) return at(found.series, i);
            let j = 0;
            other.points.forEach((p, n) => {
                if (Math.abs(p.pct - found.point.pct) < Math.abs(other.points[j].pct - found.point.pct)) j = n;
            });
            return at(other, j);
        }
        return { series: found.series.id, label: found.point.label };
    }

    /** Where the callout box (w × h) goes for a dot at (px, py) in a
        boxW × boxH box. A distillation curve only rises, so the space up and
        to the left of a dot is empty: the callout goes there, and down and
        to the right (also empty) when there is no room on the left; never
        past an edge. → {left, top, side: 'upper-left'|'lower-right'}. */
    function calloutPlace(px, py, w, h, boxW, boxH, gap) {
        const g = gap === undefined ? 10 : gap;
        const clamp = (v, hi) => Math.round(Math.max(0, Math.min(Math.max(0, hi), v)));
        if (px - g - w >= 0) {
            return { left: Math.round(px - g - w), top: clamp(py - g - h, boxH - h), side: 'upper-left' };
        }
        return { left: clamp(px + g, boxW - w), top: clamp(py + g, boxH - h), side: 'lower-right' };
    }

    const api = {
        number, plural, fmt, rowStatus, rowDetail, flagText, reviewText, injectedText,
        selection, selectedIds, toggle, extend, paint, selectShown, selectAllMatching, clear, keepOnly, bulkBar,
        CHUNK, BULK_LIMIT, chunks, runChunks, outcomeText, filterText, confirmText, countsText,
        resultRows, dataRows, dataTableText, curveFromTable, historyItems, carbonTicks, CHART_CONFIG, chartLayout, step, queueItem,
        isChecked, pressBox, chartReason, backfillReason,
        OVERLAY_MAX, overlayKey, overlayState, overlayParse, overlayStringify, overlayAdd, overlayRemove, overlayClear,
        overlayMode, overlayShown, overlayAddText, traceStyles, plotlyText, overlayTraces, dashArray,
        curvePct, curveSeries, tempText, curvePointLabel, niceTicks, curveXTicks, curveScale, curveHit, curveFind,
        curveStep, calloutPlace,
    };
    root.SamplesLogic = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
