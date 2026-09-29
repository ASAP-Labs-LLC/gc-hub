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
};
