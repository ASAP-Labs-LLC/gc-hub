/* results_logic.js (v5.0 lane R): the Results page's pure, DOM-free logic —
   window.GCResults in the browser, module.exports for node
   (tests/js/results_logic.test.js).

   * The grouped D2887 / D86 headers are built from /api/table's `columns`
     (distill.CSV_HEADER order), never a hard-coded list: the classic table
     once put every D86 header over a D2887 value.
   * Key points (IBP, 10 %, 50 %, 90 %, FBP) by default; All points shows all
     13 cuts per method plus the fit score and source file.
   * Filters live in the URL (?instrument=&status=&q=&range=&flagged=) and
     become the /api/files query; Flagged is filtered here.
   * "Corrected D86" on shows the stored (corrected) D86 cells; off, the
     uncorrected ASTM D86 App. X4 conversion of the stored D2887 cells, with
     40 %/60 % the midpoints (distill.x4_midpoints). The 40/60 tooltips are
     distill_view.js's wording.
   * Rows are /api/files' samples (newest first) joined by sample_id with
     /api/table's rows; a sample with no result yet shows dashes and why. */
(function (root) {
    'use strict';
    const req = (typeof require === 'function') ? require : null;
    const DV = (root && root.DistillView) || (req ? req('./distill_view.js') : null);
    const U = (root && root.GCUi) || (req ? req('./ui_logic.js') : null);

    const MAX_ROWS = 5000;               // /api/files' FILES_MAX_LIMIT
    const MAX_OVERLAY = 6;
    const KEY_CUTS = ['IBP', 'T10', 'T50', 'T90', 'FBP'];
    const META = ['Lab ID', 'InjectionDateTime'];
    const METHODS = { '2887': { method: 'd2887', title: 'D2887 (°C)' }, D86: { method: 'd86', title: 'D86 (°C)' } };
    const LABELS = { 'Best Fit': 'Best fit', 'Fit Score': 'Fit score', 'Source File': 'Source file',
        InjectionDateTime: 'Injected', 'Lab ID': 'Lab ID' };
    // the D86 cut names distill_view.js / distill.py use, per column suffix
    const CUT_LABEL = { IBP: 'IBP', FBP: 'FBP' };
    [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95].forEach((n) => { CUT_LABEL['T' + n] = n + '%'; });

    // ── columns ─────────────────────────────────────────────────────────────
    function parseColumn(col) {
        const m = /^(2887|D86) (IBP|FBP|T\d+)$/.exec(String(col || ''));
        if (m) return { col, method: METHODS[m[1]].method, cut: m[2], label: CUT_LABEL[m[2]] || m[2] };
        return { col, method: null, cut: null, label: LABELS[col] || String(col || '') };
    }

    function pointsMode(v) { return v === 'all' ? 'all' : 'key'; }

    /** {meta, groups:[{method, title, cols}], tail} from /api/table's columns,
        each column with its `index` in the table's rows. */
    function groupColumns(columns, points) {
        const out = { meta: [], groups: [], tail: [] };
        if (!Array.isArray(columns)) return out;
        const keyOnly = pointsMode(points) === 'key';
        const byMethod = {};
        columns.forEach((col, index) => {
            const p = Object.assign(parseColumn(col), { index });
            if (META.includes(col)) { out.meta.push(p); return; }
            if (p.method) {
                if (keyOnly && !KEY_CUTS.includes(p.cut)) return;
                if (!byMethod[p.method]) {
                    const title = p.method === 'd2887' ? METHODS['2887'].title : METHODS.D86.title;
                    byMethod[p.method] = { method: p.method, title, cols: [] };
                    out.groups.push(byMethod[p.method]);
                }
                byMethod[p.method].cols.push(p);
                return;
            }
            if (keyOnly && col !== 'Best Fit') return;
            out.tail.push(p);
        });
        return out;
    }

    // ── filters ─────────────────────────────────────────────────────────────
    const STATUS_GROUPS = {
        final: ['final'],
        held: ['awaiting_calibration', 'pending_corrections', 'other_method', 'review_method', 'raw_only'],
        error: ['error'],
        processing: ['received'],
    };
    const STATUS_LABELS = { any: 'Any status', final: 'Final', held: 'Held', error: 'Error', processing: 'Processing' };
    const RANGES = { all: null, today: 1, '7d': 7, '30d': 30, '90d': 90 };
    const RANGE_LABELS = { all: 'Any time', today: 'Today', '7d': 'Last 7 days', '30d': 'Last 30 days',
        '90d': 'Last 90 days' };
    const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

    function parseFilters(search) {
        const p = new URLSearchParams(String(search || '').replace(/^\?/, ''));
        const inst = [...new Set(String(p.get('instrument') || '').split(',').map((s) => s.trim())
            .filter((s) => ID_RE.test(s)))].sort();
        const status = STATUS_GROUPS[p.get('status')] ? p.get('status') : 'any';
        const range = Object.prototype.hasOwnProperty.call(RANGES, p.get('range')) ? p.get('range') : 'all';
        return {
            instrument: inst,
            status,
            q: String(p.get('q') || '').trim().slice(0, 64),
            range,
            flagged: p.get('flagged') === '1',
        };
    }

    function filtersToQuery(f) {
        const p = new URLSearchParams();
        if (f.instrument && f.instrument.length) p.set('instrument', f.instrument.join(','));
        if (f.status && f.status !== 'any') p.set('status', f.status);
        if (f.q) p.set('q', f.q);
        if (f.range && f.range !== 'all') p.set('range', f.range);
        if (f.flagged) p.set('flagged', '1');
        const s = p.toString();
        return s ? '?' + s : '';
    }

    const pad = (n) => String(n).padStart(2, '0');
    /** The first day a range covers, "YYYY-MM-DD", counted back from the
        hub's `today` (calendar arithmetic, no time zone involved). */
    function dateFrom(range, today) {
        const days = RANGES[range];
        const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(today || ''));
        if (!days || !m) return null;
        const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3] - (days - 1)));
        return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate());
    }

    function filesQuery(f, today) {
        const p = new URLSearchParams();
        if (f.instrument && f.instrument.length) p.set('instrument', f.instrument.join(','));
        if (STATUS_GROUPS[f.status]) p.set('status', STATUS_GROUPS[f.status].join(','));
        if (f.q) p.set('q', f.q);
        const from = dateFrom(f.range, today);
        if (from) p.set('date_from', from);
        p.set('limit', String(MAX_ROWS));
        return p.toString();
    }

    function applyClientFilters(rows, f) {
        return f && f.flagged ? rows.filter((r) => r.flags && r.flags.length) : rows;
    }

    // ── D86 before the correction factors (ASTM D86 App. X4) ───────────────
    // The same coefficients as distill._CONVERSION_COEFF / app.js; checked
    // against Python by tests/test_results_d86_js.py.
    const CONVERSION_COEFF = {
        IBP: [25.351, 0.32216, 0.71187, -0.04221],
        '5%': [18.822, 0.06602, 0.15803, 0.77898],
        '10%': [15.173, 0.20149, 0.30606, 0.48227],
        '20%': [13.141, 0.22677, 0.29042, 0.46023],
        '30%': [5.7766, 0.37218, 0.30313, 0.31118],
        '50%': [6.3753, 0.07763, 0.68984, 0.18302],
        '70%': [-2.8437, 0.16366, 0.42102, 0.38252],
        '80%': [-0.21536, 0.25614, 0.40925, 0.27995],
        '90%': [0.09966, 0.24335, 0.32051, 0.37357],
        '95%': [0.89880, -0.09790, 1.03816, -0.00894],
        FBP: [19.444, -0.38161, 1.08571, 0.17729],
    };
    const CONVERSION_REL = {
        IBP: ['IBP', '5%', '10%'], '5%': ['IBP', '5%', '10%'], '10%': ['5%', '10%', '20%'],
        '20%': ['10%', '20%', '30%'], '30%': ['20%', '30%', '50%'], '50%': ['30%', '50%', '70%'],
        '70%': ['50%', '70%', '80%'], '80%': ['70%', '80%', '90%'], '90%': ['80%', '90%', '95%'],
        '95%': ['90%', '95%', 'FBP'], FBP: ['90%', '95%', 'FBP'],
    };
    const pyRound2 = DV.pyRound2;

    /** {label: °C} D2887 → the uncorrected D86 conversion, 40/60 midpoints. */
    function convertToD86(d2887) {
        const out = {};
        for (const [cut, [a0, a1, a2, a3]] of Object.entries(CONVERSION_COEFF)) {
            const [p, c, n] = CONVERSION_REL[cut];
            const tp = d2887[p]; const tc = d2887[c]; const tn = d2887[n];
            if (![tp, tc, tn].every((v) => typeof v === 'number' && Number.isFinite(v))) continue;
            out[cut] = pyRound2(a0 + a1 * tp + a2 * tc + a3 * tn);
        }
        return DV.x4Midpoints(out);
    }

    // ── rows ────────────────────────────────────────────────────────────────
    function number(v) {
        if (v === null || v === undefined || String(v).trim() === '') return null;
        const n = Number(v);
        return Number.isFinite(n) ? n : null;
    }

    /** /api/files' samples (in their order) joined with /api/table by sample_id. */
    function buildRows(files, table, opts) {
        const corrected = !!(opts && opts.corrected);
        const columns = (table && table.columns) || [];
        const ids = (table && table.sample_ids) || [];
        const byId = new Map();
        ((table && table.rows) || []).forEach((row, i) => { if (ids[i] !== undefined) byId.set(ids[i], row); });
        const parsed = columns.map(parseColumn);
        return (files || []).map((f) => {
            const row = byId.get(f.sample_id);
            const cells = {};
            const notes = {};
            parsed.forEach((p, i) => {
                if (!row) { cells[p.col] = null; return; }
                cells[p.col] = p.method ? number(row[i]) : (row[i] === '' || row[i] == null ? null : String(row[i]));
            });
            if (row && !corrected) {
                const d2887 = {};
                parsed.forEach((p) => { if (p.method === 'd2887') d2887[p.label] = cells[p.col]; });
                const conv = convertToD86(d2887);
                parsed.forEach((p) => { if (p.method === 'd86') cells[p.col] = conv[p.label] === undefined ? null : conv[p.label]; });
            }
            parsed.forEach((p) => {
                if (p.method !== 'd86' || (p.label !== '40%' && p.label !== '60%') || !row) return;
                notes[p.col] = cells[p.col] === null ? DV.missingNote(p.label) : DV.midpointNote(p.label, corrected);
            });
            return {
                sample_id: f.sample_id,
                lab_id: f.lab_id,
                display_name: f.display_name || f.lab_id,
                instrument: f.instrument,
                injection_dt: f.injection_dt || null,
                status: f.status,
                error: f.error || null,
                review_note: f.review_note || null,
                backfill: !!f.backfill,
                flags: f.flags || [],
                best_fit: f.best_fit || null,
                hasResult: !!row,
                cells,
                notes,
            };
        });
    }

    /** Why a row is not final (shown in the row), or null for a final one. */
    function rowNote(row) {
        if (!row || row.status === 'final') return null;
        const text = U.sampleStatus(row.status).text;
        if (row.status === 'error' && row.error) return text + ': ' + row.error;
        if (row.review_note) return text + ': ' + row.review_note;
        return text;
    }

    /** A table cell: one decimal for temperatures, a dash for no value. */
    function fmtCell(v) {
        if (v === null || v === undefined || v === '') return '—';
        if (typeof v === 'number') return v.toFixed(1);
        return String(v);
    }

    const collator = (typeof Intl !== 'undefined') ? new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' }) : null;
    /** Rows sorted by a column (or lab_id / injection_dt); empty cells last. */
    function sortRows(rows, key, dir) {
        if (!key) return rows;
        const sign = dir === 'desc' ? -1 : 1;
        const val = (r) => (key === 'lab_id' ? r.lab_id : key === 'injection_dt' ? r.injection_dt : r.cells[key]);
        return rows.map((r, i) => [r, i]).sort((a, b) => {
            const va = val(a[0]); const vb = val(b[0]);
            const ea = va === null || va === undefined || va === ''; const eb = vb === null || vb === undefined || vb === '';
            if (ea || eb) return ea === eb ? a[1] - b[1] : (ea ? 1 : -1);
            let c;
            if (typeof va === 'number' && typeof vb === 'number') c = va - vb;
            else c = collator ? collator.compare(String(va), String(vb)) : String(va).localeCompare(String(vb));
            return c ? sign * c : a[1] - b[1];
        }).map((x) => x[0]);
    }

    // ── CSV ─────────────────────────────────────────────────────────────────
    function csvCell(v) {
        if (v === null || v === undefined) return '';
        let s = String(v);
        // a formula would run when the file is opened in Excel; a number stays a number
        if (/^[=+\-@\t\r]/.test(s) && !(typeof v === 'number' || /^-?\d+(\.\d+)?$/.test(s))) s = "'" + s;
        return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }

    /** Every column (all points), the rows shown, D86 as shown. */
    function toCsv(rows, columns) {
        const cols = (columns || []).filter((c) => !META.includes(c));
        const header = ['Lab ID', 'GC', 'InjectionDateTime', 'Status'].concat(cols);
        const lines = [header.map(csvCell).join(',')];
        for (const r of rows) {
            const vals = [r.lab_id, r.instrument, r.injection_dt, U.sampleStatus(r.status).text]
                .concat(cols.map((c) => r.cells[c]));
            lines.push(vals.map(csvCell).join(','));
        }
        return lines.join('\r\n') + '\r\n';
    }

    function csvName(today) {
        return 'gc-results-' + (/^\d{4}-\d{2}-\d{2}$/.test(String(today || '')) ? today : 'export') + '.csv';
    }

    // ── overlay curves ──────────────────────────────────────────────────────
    const DASHES = ['solid', 'dash', 'dot', 'dashdot', 'longdash', 'longdashdot'];
    function overlayStyle(i) {
        const n = ((i % DASHES.length) + DASHES.length) % DASHES.length;
        // ink, ink, grey, grey, … each with its own dash: never colour alone
        return { dash: DASHES[n], shade: Math.floor(n / 2) % 2 ? 'ref' : 'ink',
                 marker: n === 0 ? 'circle' : 'circle-open' };
    }

    function overlayTraces(curves, colors) {
        return (curves || []).map((c, i) => {
            const st = overlayStyle(i);
            return {
                x: c.percent, y: c.temperature, type: 'scatter', mode: 'lines', name: String(c.label || c.sample_id),
                line: { color: st.shade === 'ref' ? colors.ref : colors.ink, width: 1.5, dash: st.dash },
                hovertemplate: '%{x:.1f}% · %{y:.1f} °C<extra>' + String(c.label || '').replace(/[<>&]/g, '') + '</extra>',
            };
        });
    }

    function toggleSelected(list, id) {
        if (list.includes(id)) return list.filter((x) => x !== id);
        if (list.length >= MAX_OVERLAY) return list.slice();
        return list.concat([id]);
    }

    const api = {
        MAX_ROWS, MAX_OVERLAY, KEY_CUTS, STATUS_GROUPS, STATUS_LABELS, RANGES, RANGE_LABELS,
        parseColumn, pointsMode, groupColumns, parseFilters, filtersToQuery, dateFrom, filesQuery,
        applyClientFilters, convertToD86, pyRound2, buildRows, rowNote, fmtCell, sortRows,
        csvCell, toCsv, csvName, overlayStyle, overlayTraces, toggleSelected,
    };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (root) root.GCResults = api;
})(typeof window !== 'undefined' ? window : null);
