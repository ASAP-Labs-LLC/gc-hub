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
    // app.js convertToD86) with 40%/60% filled by distill.x4_midpoints' rule
    // (x4Midpoints). 40% and 60% have no X4 equation: the hub stores the
    // midpoint of the uncorrected neighbouring cuts, which no correction
    // factor touches, so even in "Corrected D86" mode they are uncorrected
    // midpoints; their tooltip says so (or, since v7.0.0, that one was held
    // at an earlier cut's corrected value: monotonicD86). With no value, null
    // and a note (the cell's tooltip). Display only.
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
    const HELD_NOTE = 'Held at an earlier corrected value: a corrected D86 temperature never falls ' +
        'below an earlier one.';
    const MIDPOINT_OF = { '40%': ['30%', '50%'], '60%': ['50%', '70%'] };

    function pair(label) {
        return MIDPOINT_OF[label].map((l) => l.replace('%', '')).join('/');
    }

    /** The tooltip of a 40%/60% cell that has a value. */
    function midpointNote(label, corrected) {
        if (corrected) {
            return 'Midpoint of the uncorrected ' + pair(label) + ' values; no correction factor applies.';
        }
        return 'Midpoint of the ' + pair(label) + ' values: ASTM D86 Appendix X4 has no ' + label +
            ' equation.';
    }

    /** Python's round(x, 2): the float's exact value, ties (x.xx5 exactly) to even.
     *  toFixed rounds the exact value too, but an exact tie away from zero. */
    function pyRound2(v) {
        const x = Number(v);
        if (!Number.isFinite(x)) return x;
        const a = Math.abs(x);
        let r = Number(a.toFixed(2));
        const eighths = a * 8;              // exact: a tie is an odd number of eighths
        if (Number.isInteger(eighths) && eighths % 2 === 1) {
            const cents = Math.round(r * 100);
            if (cents % 2 === 1) r = (cents - 1) / 100;
        }
        return x < 0 ? -r : r;
    }

    /** distill.x4_midpoints: a copy with 40%/60% the midpoints of the 30/50
     *  and 50/70 conversions, when both neighbours have a value. */
    function x4Midpoints(d86) {
        const out = Object.assign({}, d86);
        const has = (l) => out[l] !== null && out[l] !== undefined && Number.isFinite(Number(out[l]));
        for (const label of Object.keys(MIDPOINT_OF)) {
            const [lo, hi] = MIDPOINT_OF[label];
            if (has(lo) && has(hi)) out[label] = pyRound2((Number(out[lo]) + Number(out[hi])) / 2);
        }
        return out;
    }

    /** distill.monotonic_d86 (v7.0.0): a copy of a corrected D86 series
     *  ({label: number|null}) in which no cut is below an earlier one —
     *  walking IBP → FBP, each value is max(itself, the previous value).
     *  Missing values (null/undefined/'') are skipped and left as they are.
     *  Only the corrected (reported) series gets this; the uncorrected one
     *  keeps any dip. Checked against Python by tests/test_d86_monotonic.py. */
    function monotonicD86(d86) {
        const out = Object.assign({}, d86);
        let running = null;
        for (const l of D86_LABELS) {
            const v = out[l];
            if (v === null || v === undefined || v === '' || !Number.isFinite(Number(v))) continue;
            if (running !== null && Number(v) < running) out[l] = running;
            else running = Number(v);
        }
        return out;
    }

    /** The first label (IBP → FBP) whose value is below an earlier one, or
     *  null: a stored corrected series from before v7.0.0 can dip. */
    function firstDip(values) {
        let running = null;
        for (const l of D86_LABELS) {
            const v = values ? values[l] : null;
            if (v === null || v === undefined || v === '' || !Number.isFinite(Number(v))) continue;
            if (running !== null && Number(v) < running) return l;
            running = Number(v);
        }
        return null;
    }

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
            const conv = x4Midpoints(convert(byLabel) || {});
            source = (l) => num(conv[l]);
        } else {
            source = () => null;
        }
        const unc = corrected && hasValues(data.d86_uncorrected) ? data.d86_uncorrected : null;
        for (const l of D86_LABELS) {
            values[l] = source(l);
            if (values[l] === null) notes[l] = missingNote(l);
            else if (MIDPOINT_OF[l]) {
                // v7.0.0: a corrected midpoint may be held at the cut before it
                // (30% / 50%), above its own uncorrected midpoint.
                const mid = unc ? num(unc[D86_COLUMN[l]]) : null;
                const before = values[MIDPOINT_OF[l][0]];
                notes[l] = mid !== null && values[l] > mid && values[l] === before
                    ? HELD_NOTE : midpointNote(l, !!corrected);
            }
        }
        return { values, notes };
    }

    /** Why a D86 cell has no value (its tooltip). */
    function missingNote(label) {
        return (label === '40%' || label === '60%') ? NO_X4 : NOT_STORED;
    }

    const api = { columnClass, tableHeader, headerText, D86_LABELS, dashboardD86, missingNote,
        midpointNote, pyRound2, x4Midpoints, monotonicD86, firstDip, HELD_NOTE };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (root) root.DistillView = api;
})(typeof window !== 'undefined' ? window : null);
