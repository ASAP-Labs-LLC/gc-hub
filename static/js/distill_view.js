/* distill_view.js: pure, DOM-free helpers for showing distillation numbers —
   shared by the browser (window.DistillView) and Node tests (module.exports).

   The Distillation Data table's header is built from the columns /api/table
   returns (distill.CSV_HEADER order), so every header sits over its own
   value; each column keeps its colour group (col-meta / col-d86 / col-d2887). */
(function (root) {
    'use strict';

    const META_COLUMNS = ['Lab ID', 'InjectionDateTime'];
    const LABELS = { InjectionDateTime: 'Injection Date' };

    /** The colour-group class of a column (as its cells get it). */
    function columnClass(colName) {
        const c = String(colName || '');
        if (META_COLUMNS.includes(c)) return 'col-meta';
        if (c.includes('D86')) return 'col-d86';
        if (c.includes('2887')) return 'col-d2887';
        return '';
    }

    /** One header cell per column, in the columns' order: {col, label, cls}. */
    function tableHeader(columns) {
        if (!Array.isArray(columns)) return [];
        return columns.map((col) => ({ col, label: LABELS[col] || col, cls: columnClass(col) }));
    }

    /** A header cell's text with the sort arrow when it is the sorted column. */
    function headerText(cell, index, sortCol, sortAsc) {
        const arrow = index === sortCol ? (sortAsc ? ' ▲' : ' ▼') : '';
        return cell.label + arrow;
    }

    // ── the Dashboard's D86 table ─────────────────────────────────────────
    // "Corrected D86" on: the revision's stored (corrected) D86 cells. Off:
    // its stored uncorrected conversion (d86_uncorrected), or, when it stores
    // none (a v1 import), the X4 conversion of its stored D2887 (`convert`,
    // app.js convertToD86). 40% and 60% have no X4 equation: the hub stores
    // the midpoint of the neighbouring cuts, shown whenever it exists; with
    // none, null and a note (the cell's tooltip). Display only.
    const D86_LABELS = ['IBP', '5%', '10%', '20%', '30%', '40%', '50%', '60%', '70%', '80%', '90%',
        '95%', 'FBP'];
    const D86_COLUMN = {
        IBP: 'D86 IBP', '5%': 'D86 T5', '10%': 'D86 T10', '20%': 'D86 T20', '30%': 'D86 T30',
        '40%': 'D86 T40', '50%': 'D86 T50', '60%': 'D86 T60', '70%': 'D86 T70', '80%': 'D86 T80',
        '90%': 'D86 T90', '95%': 'D86 T95', FBP: 'D86 FBP',
    };
    const D2887_COLUMN = {};
    D86_LABELS.forEach((l) => { D2887_COLUMN[l] = D86_COLUMN[l].replace('D86', '2887'); });
    const NO_X4 = 'ASTM D86 Appendix X4 has no 40%/60% conversion, and this result stores no ' +
        'value for it.';
    const NOT_STORED = 'Not stored for this result.';

    function num(v) {
        if (v === null || v === undefined || v === '') return null;
        const n = Number(v);
        return Number.isFinite(n) ? Math.round(n * 100) / 100 : null;
    }

    function hasValues(obj) {
        return !!obj && typeof obj === 'object' && Object.keys(obj).some((k) => num(obj[k]) !== null);
    }

    /** {values: {label: number|null}, notes: {label: text}} for the D86 table. */
    function dashboardD86(dcData, corrected, convert) {
        const data = dcData || {};
        const values = {};
        const notes = {};
        let source = null;       // label -> value
        if (corrected) {
            source = (l) => num((data.d86 || {})[D86_COLUMN[l]]);
        } else if (hasValues(data.d86_uncorrected)) {
            source = (l) => num(data.d86_uncorrected[D86_COLUMN[l]]);
        } else if (hasValues(data.d2887) && typeof convert === 'function') {
            const byLabel = {};
            D86_LABELS.forEach((l) => { byLabel[l] = num(data.d2887[D2887_COLUMN[l]]); });
            const conv = convert(byLabel) || {};
            source = (l) => num(conv[l]);
        } else {
            source = () => null;
        }
        for (const l of D86_LABELS) {
            values[l] = source(l);
            if (values[l] === null) notes[l] = missingNote(l);
        }
        return { values, notes };
    }

    /** Why a D86 cell has no value (its tooltip). */
    function missingNote(label) {
        return (label === '40%' || label === '60%') ? NO_X4 : NOT_STORED;
    }

    const api = { columnClass, tableHeader, headerText, D86_LABELS, dashboardD86, missingNote };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (root) root.DistillView = api;
})(typeof window !== 'undefined' ? window : null);
