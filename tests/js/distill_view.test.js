// distill_view.js: the Distillation Data table's header is built from the
// columns /api/table returns (distill.CSV_HEADER order: Lab ID,
// InjectionDateTime, 2887 IBP…FBP, D86 IBP…FBP, Best Fit, Fit Score, Source
// File), so every header sits over its own value. Before v3.1.0 the template
// hard-coded D86 first and then 2887: every D86 header was over a D2887 value.
const fs = require('fs');
const path = require('path');
const V = require('../../static/js/distill_view.js');

// The real column order, read from distill.py (what /api/table serves).
function csvHeader() {
    const src = fs.readFileSync(path.join(__dirname, '..', '..', 'distill.py'), 'utf8');
    const block = /CSV_HEADER: list\[str\] = \[([\s\S]*?)\n\]/.exec(src)[1];
    return [...block.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

module.exports = (t) => {
    const columns = csvHeader();
    t.eq(columns.slice(0, 4), ['Lab ID', 'InjectionDateTime', '2887 IBP', '2887 T5']);
    t.eq(columns.indexOf('D86 IBP'), 15);

    // a row as /api/table sends it: one cell per column, in column order
    const row = columns.map((c) => 'value of ' + c);
    const head = V.tableHeader(columns);
    t.eq(head.length, row.length);
    head.forEach((h, i) => {
        t.eq(h.col, columns[i]);
        t.eq(row[i], 'value of ' + h.col);           // the header names the value under it
        t.eq(h.cls, V.columnClass(columns[i]));       // same colour group as its cells
    });
    const at = (name) => head.findIndex((h) => h.col === name);
    t.eq(row[at('D86 T40')], 'value of D86 T40');
    t.eq(row[at('2887 T40')], 'value of 2887 T40');
    t.eq(head[at('D86 T50')].label, 'D86 T50');
    t.eq(head[at('2887 FBP')].cls, 'col-d2887');
    t.eq(head[at('D86 IBP')].cls, 'col-d86');
    t.eq(head[at('InjectionDateTime')], { col: 'InjectionDateTime', label: 'Injection Date', cls: 'col-meta' });
    t.eq(head[at('Lab ID')].cls, 'col-meta');
    t.eq(head[at('Best Fit')].cls, '');

    // the sort arrow goes on the sorted column only
    t.eq(V.headerText(head[3], 3, 3, true), '2887 T5 ▲');
    t.eq(V.headerText(head[3], 3, 3, false), '2887 T5 ▼');
    t.eq(V.headerText(head[3], 3, -1, true), '2887 T5');

    t.eq(V.tableHeader(null), []);

    dashboardD86Tests(t);
};

// The Dashboard's D86 table (v3.1.0): "Corrected D86" on = the revision's
// stored (corrected) D86 cells; off = its stored uncorrected conversion
// (d86_uncorrected), or, for a revision that stores none (a v1 import), the
// X4 conversion of its stored D2887, 40%/60% filled by distill.x4_midpoints'
// rule (tests/test_d86_midpoints_js.py checks the numbers against Python).
// 40% and 60% are shown whenever a value exists in that mode (the hub stores
// the midpoint of the uncorrected neighbouring cuts, which no correction
// factor touches: a tooltip says so); with none, "—" and a tooltip saying
// why. Nothing is recalculated for a value that is stored.
function dashboardD86Tests(t) {
    const LABELS = ['IBP', '5%', '10%', '20%', '30%', '40%', '50%', '60%', '70%', '80%', '90%', '95%', 'FBP'];
    const COLS = ['D86 IBP', 'D86 T5', 'D86 T10', 'D86 T20', 'D86 T30', 'D86 T40', 'D86 T50', 'D86 T60',
        'D86 T70', 'D86 T80', 'D86 T90', 'D86 T95', 'D86 FBP'];
    const stored = {}, unc = {};
    COLS.forEach((c, i) => { stored[c] = 100 + i + 0.004; unc[c] = 90 + i; });
    const d2887 = { '2887 IBP': 50, '2887 T50': 150 };
    let converted = 0;
    const convert = (byLabel) => {
        converted++;
        const out = {};
        for (const l of LABELS) out[l] = (l === '40%' || l === '60%') ? null : 7;
        out.seen = byLabel;
        return out;
    };
    t.eq(V.D86_LABELS, LABELS);

    const CORRECTED_40 = 'Midpoint of the uncorrected 30/50 values; no correction factor applies.';
    const CORRECTED_60 = 'Midpoint of the uncorrected 50/70 values; no correction factor applies.';

    // corrected on: the stored cells, 40% and 60% included, rounded to 2 places.
    // 40%/60% are the uncorrected midpoints (corrections never touch them): the
    // tooltip says so
    let r = V.dashboardD86({ d86: stored, d86_uncorrected: unc, d2887 }, true, convert);
    t.eq(r.values['40%'], 105);
    t.eq(r.values['60%'], 107);
    t.eq(r.values.IBP, 100);
    t.eq(r.values.FBP, 112);
    t.eq(r.notes, { '40%': CORRECTED_40, '60%': CORRECTED_60 });
    t.eq(converted, 0);

    // corrected off: the stored uncorrected conversion, 40% and 60% included
    r = V.dashboardD86({ d86: stored, d86_uncorrected: unc, d2887 }, false, convert);
    t.eq(LABELS.map((l) => r.values[l]), COLS.map((_c, i) => 90 + i));
    t.eq(Object.keys(r.notes).sort(), ['40%', '60%']);
    t.eq(r.notes['40%'], 'Midpoint of the 30/50 values: ASTM D86 Appendix X4 has no 40% equation.');
    t.eq(r.notes['60%'], 'Midpoint of the 50/70 values: ASTM D86 Appendix X4 has no 60% equation.');
    t.eq(converted, 0);

    // corrected off, a revision with no stored uncorrected values (v1 import):
    // the X4 conversion of its stored D2887, 40%/60% by distill.compute's
    // midpoint rule (not "—")
    const conv2 = (byLabel) => {
        converted++;
        const out = {};
        LABELS.forEach((l, i) => { out[l] = (l === '40%' || l === '60%') ? null : 200 + i * 10; });
        out['30%'] = 100.01; out['50%'] = 100.02; out['70%'] = 404.23;
        out.seen = byLabel;
        return out;
    };
    r = V.dashboardD86({ d86: stored, d86_uncorrected: {}, d2887 }, false, conv2);
    t.eq(converted, 1);
    t.eq(r.values.IBP, 200);
    t.eq(r.values['40%'], 100.02);          // Python: round((100.01 + 100.02) / 2, 2)
    t.eq(r.values['60%'], 252.12);          // Python: round(252.125, 2), an exact tie: to even
    t.eq(r.values['50%'], 100.02);
    t.eq(Object.keys(r.notes).sort(), ['40%', '60%']);
    t.eq(/^Midpoint of the 30\/50 values/.test(r.notes['40%']), true);
    // a missing neighbour leaves 40% empty, with the "no X4" note
    r = V.dashboardD86({ d86: stored, d86_uncorrected: {}, d2887 }, false, convert);
    t.eq(r.values['40%'], 7);
    const noThirty = (b) => Object.assign(convert(b), { '30%': null });
    r = V.dashboardD86({ d86: stored, d86_uncorrected: {}, d2887 }, false, noThirty);
    t.eq(r.values['40%'], null);
    t.eq(r.values['60%'], 7);
    t.eq(/Appendix X4 has no 40%\/60% conversion/.test(r.notes['40%']), true);

    // pyRound2 rounds like Python's round(x, 2): the exact binary value, ties to even
    const PY = [[0.125, 0.12], [0.375, 0.38], [0.625, 0.62], [0.875, 0.88], [-0.125, -0.12],
        [2.675, 2.67], [1.005, 1.0], [100.015, 100.02], [(404.21 + 404.22) / 2, 404.22],
        [123.456, 123.46], [-2.675, -2.67], [5, 5], [252.125, 252.12]];
    for (const [v, want] of PY) t.eq([v, V.pyRound2(v)], [v, want]);
    t.eq(V.x4Midpoints({ '30%': 1, '50%': 2, '70%': null }), { '30%': 1, '50%': 2, '70%': null, '40%': 1.5 });

    // corrected on, a revision whose 40/60 cells are empty
    const partial = Object.assign({}, stored);
    delete partial['D86 T40'];
    delete partial['D86 T60'];
    delete partial['D86 FBP'];
    r = V.dashboardD86({ d86: partial, d86_uncorrected: unc, d2887 }, true, convert);
    t.eq(r.values['40%'], null);
    t.eq(r.values.FBP, null);
    t.eq(Object.keys(r.notes).sort(), ['40%', '60%', 'FBP']);
    t.eq(/Appendix X4/.test(r.notes['60%']), true);
    t.eq(r.notes.FBP, 'Not stored for this result.');

    // nothing at all
    r = V.dashboardD86({}, true, convert);
    t.eq(LABELS.every((l) => r.values[l] === null), true);
}
