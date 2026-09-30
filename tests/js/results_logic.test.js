// results_logic.js (v5.0 lane R): the Results page's pure logic.
// * the grouped D2887 / D86 headers come from /api/table's `columns`, never a
//   hard-coded order (the classic table once put every D86 header over a
//   D2887 value);
// * Key points (IBP, 10, 50, 90, FBP) by default, All points on a toggle;
// * the filters live in the URL, and turn into the /api/files query;
// * "Corrected D86" off shows the uncorrected X4 conversion (40/60 midpoints);
// * the CSV is safe to open in Excel.
const fs = require('fs');
const path = require('path');
const R = require('../../static/js/results_logic.js');

function csvHeader() {
    const src = fs.readFileSync(path.join(__dirname, '..', '..', 'distill.py'), 'utf8');
    const block = /CSV_HEADER: list\[str\] = \[([\s\S]*?)\n\]/.exec(src)[1];
    return [...block.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

module.exports = (t) => {
    const columns = csvHeader();

    // ── columns → groups ────────────────────────────────────────────────
    t.eq(R.parseColumn('2887 T10'), { col: '2887 T10', method: 'd2887', cut: 'T10', label: '10%' });
    t.eq(R.parseColumn('D86 IBP'), { col: 'D86 IBP', method: 'd86', cut: 'IBP', label: 'IBP' });
    t.eq(R.parseColumn('Best Fit'), { col: 'Best Fit', method: null, cut: null, label: 'Best fit' });
    t.eq(R.parseColumn('InjectionDateTime').label, 'Injected');

    const key = R.groupColumns(columns, 'key');
    t.eq(key.groups.map((g) => g.method), ['d2887', 'd86']);            // CSV order: D2887 first
    t.eq(key.groups.map((g) => g.title), ['D2887 (°C)', 'D86 (°C)']);
    t.eq(key.groups[0].cols.map((c) => c.label), ['IBP', '10%', '50%', '90%', 'FBP']);
    t.eq(key.groups[1].cols.map((c) => c.col), ['D86 IBP', 'D86 T10', 'D86 T50', 'D86 T90', 'D86 FBP']);
    // every column's index is its place in /api/table's rows
    for (const g of key.groups) for (const c of g.cols) t.eq(columns[c.index], c.col);
    t.eq(key.tail.map((c) => c.col), ['Best Fit']);
    t.eq(key.meta.map((c) => c.col), ['Lab ID', 'InjectionDateTime']);

    const all = R.groupColumns(columns, 'all');
    t.eq(all.groups[0].cols.length, 13);
    t.eq(all.groups[1].cols.length, 13);
    t.eq(all.groups[1].cols.map((c) => c.label)[5], '40%');
    t.eq(all.tail.map((c) => c.col), ['Best Fit', 'Fit Score', 'Source File']);

    // the order follows the columns, whatever it is (the old bug: swapped headers)
    const swapped = ['Lab ID', 'D86 IBP', 'D86 T50', '2887 IBP', '2887 T50', 'Best Fit'];
    const sg = R.groupColumns(swapped, 'all');
    t.eq(sg.groups.map((g) => g.method), ['d86', 'd2887']);
    t.eq(sg.groups[0].cols.map((c) => c.index), [1, 2]);
    t.eq(sg.groups[1].cols.map((c) => c.index), [3, 4]);
    t.eq(R.groupColumns(null, 'key'), { meta: [], groups: [], tail: [] });
    t.eq(R.pointsMode('all'), 'all');
    t.eq(R.pointsMode('nonsense'), 'key');
    t.eq(R.pointsMode(null), 'key');

    // ── filters ↔ URL ↔ /api/files ──────────────────────────────────────
    t.eq(R.parseFilters(''), { instrument: [], status: 'any', q: '', range: 'all', flagged: false });
    const f = R.parseFilters('?instrument=gc2,gc1,gc2&status=held&q=+403+&range=7d&flagged=1&junk=1');
    t.eq(f, { instrument: ['gc1', 'gc2'], status: 'held', q: '403', range: '7d', flagged: true });
    t.eq(R.parseFilters('?status=nope&range=forever&instrument=<b>,ok_1').status, 'any');
    t.eq(R.parseFilters('?status=nope&range=forever&instrument=<b>,ok_1').range, 'all');
    t.eq(R.parseFilters('?instrument=<b>,ok_1').instrument, ['ok_1']);
    t.eq(R.parseFilters('?q=' + 'x'.repeat(200)).q.length, 64);
    t.eq(R.filtersToQuery(f), '?instrument=gc1%2Cgc2&status=held&q=403&range=7d&flagged=1');
    t.eq(R.filtersToQuery(R.parseFilters('')), '');
    t.eq(R.parseFilters(R.filtersToQuery(f)), f);                         // round trip

    t.eq(R.dateFrom('all', '2026-09-30'), null);
    t.eq(R.dateFrom('today', '2026-09-30'), '2026-09-30');
    t.eq(R.dateFrom('7d', '2026-09-30'), '2026-09-24');
    t.eq(R.dateFrom('30d', '2026-03-01'), '2026-01-31');
    t.eq(R.dateFrom('7d', 'garbage'), null);

    const q = new URLSearchParams(R.filesQuery(f, '2026-09-30'));
    t.eq(q.get('instrument'), 'gc1,gc2');
    t.eq(q.get('status'), 'awaiting_calibration,pending_corrections,other_method,review_method,raw_only');
    t.eq(q.get('q'), '403');
    t.eq(q.get('date_from'), '2026-09-24');
    t.eq(q.get('limit'), String(R.MAX_ROWS));
    t.eq(q.has('flagged'), false);                                          // client-side
    const q0 = new URLSearchParams(R.filesQuery(R.parseFilters(''), '2026-09-30'));
    t.eq([...q0.keys()], ['limit']);
    t.eq(new URLSearchParams(R.filesQuery(R.parseFilters('?status=processing'), null)).get('status'), 'received');

    // ── rows: files joined with the table ───────────────────────────────
    const table = {
        columns,
        sample_ids: [7, 9],
        rows: [
            columns.map((c) => (c === 'Lab ID' ? '40304' : c.startsWith('2887') ? '200.004'
                : c.startsWith('D86') ? '210.5' : c === 'Best Fit' ? 'Diesel' : '')),
            columns.map(() => ''),
        ],
    };
    const files = [
        { sample_id: 9, lab_id: 'B', display_name: 'B', instrument: 'gc1', status: 'error', error: 'Bad CDF', flags: [], backfill: false },
        { sample_id: 7, lab_id: '40304', display_name: '40304 (2)', instrument: 'gc1', status: 'final', flags: [{ name: 'Early' }], backfill: true, injection_dt: '2026-09-25T14:23:00' },
        { sample_id: 3, lab_id: 'C', display_name: 'C', instrument: 'gc2', status: 'awaiting_calibration', flags: [] },
    ];
    const rows = R.buildRows(files, table, { corrected: true });
    t.eq(rows.map((r) => r.sample_id), [9, 7, 3]);                        // the files' order
    t.eq(rows[1].cells['D86 T10'], 210.5);
    t.eq(rows[1].cells['2887 T10'], 200.004);
    t.eq(rows[1].cells['Best Fit'], 'Diesel');
    t.eq(rows[1].hasResult, true);
    t.eq(rows[1].backfill, true);
    t.eq(rows[2].hasResult, false);
    t.eq(rows[0].cells['D86 T10'], null);
    t.eq(R.rowNote(rows[0]), 'Error: Bad CDF');
    t.eq(R.rowNote(rows[2]), 'Waiting for calibration');
    t.eq(R.rowNote(rows[1]), null);
    t.eq(R.fmtCell(200.004), '200.0');
    t.eq(R.fmtCell(null), '—');
    t.eq(R.fmtCell('Diesel'), 'Diesel');
    // the 40/60 tooltips, the same wording as the Dashboard
    t.eq(rows[1].notes['D86 T40'], 'Midpoint of the uncorrected 30/50 values; no correction factor applies.');
    t.eq(rows[1].notes['D86 T50'], undefined);

    // Flagged is a client-side filter
    t.eq(R.applyClientFilters(rows, { flagged: true }).map((r) => r.sample_id), [7]);
    t.eq(R.applyClientFilters(rows, { flagged: false }).length, 3);

    // "Corrected D86" off: the uncorrected X4 conversion of the stored D2887
    const d2887 = { IBP: 100, '5%': 150, '10%': 170, '20%': 190, '30%': 210, '40%': 230, '50%': 250,
        '60%': 270, '70%': 290, '80%': 310, '90%': 330, '95%': 350, FBP: 400 };
    const conv = R.convertToD86(d2887);
    t.eq(conv.IBP, R.pyRound2(25.351 + 0.32216 * 100 + 0.71187 * 150 - 0.04221 * 170));
    t.eq(conv['40%'], R.pyRound2((conv['30%'] + conv['50%']) / 2));
    const off = R.buildRows(files, table, { corrected: false });
    const exp = R.convertToD86(Object.fromEntries(Object.keys(d2887).map((k) => [k, 200.004])));
    t.eq(off[1].cells['D86 IBP'], exp.IBP);
    t.eq(off[1].cells['D86 T40'], exp['40%']);
    t.eq(off[1].notes['D86 T40'], 'Midpoint of the 30/50 values: ASTM D86 Appendix X4 has no 40% equation.');
    t.eq(off[1].cells['2887 T10'], 200.004);                              // D2887 is never corrected

    // ── sorting ─────────────────────────────────────────────────────────
    const s = R.sortRows(rows, 'D86 T10', 'asc');
    t.eq(s.map((r) => r.sample_id), [7, 9, 3]);                            // no value sorts last
    t.eq(R.sortRows(rows, 'D86 T10', 'desc').map((r) => r.sample_id), [7, 9, 3]);
    t.eq(R.sortRows(rows, 'lab_id', 'asc').map((r) => r.lab_id), ['40304', 'B', 'C']);
    t.eq(R.sortRows(rows, null, 'asc'), rows);

    // ── CSV ─────────────────────────────────────────────────────────────
    t.eq(R.csvCell('a,b'), '"a,b"');
    t.eq(R.csvCell('say "hi"'), '"say ""hi"""');
    t.eq(R.csvCell('=cmd()'), "'=cmd()");
    t.eq(R.csvCell('+1+2'), "'+1+2");
    t.eq(R.csvCell('-12.5'), '-12.5');                                      // a number stays a number
    t.eq(R.csvCell(-3), '-3');
    t.eq(R.csvCell(null), '');
    const csv = R.toCsv(rows, columns, { corrected: true });
    const lines = csv.trim().split('\r\n');
    t.eq(lines.length, 4);
    t.eq(lines[0].split(',').slice(0, 5), ['Lab ID', 'GC', 'InjectionDateTime', 'Status', '2887 IBP']);
    t.eq(lines[0].split(',').length, columns.length + 2);
    t.eq(lines[2].split(',').slice(0, 4), ['40304', 'gc1', '2026-09-25T14:23:00', 'Final']);
    t.eq(R.csvName('2026-09-30'), 'gc-results-2026-09-30.csv');

    // ── overlay curves: monochrome, told apart by dash, never colour alone ─
    t.eq(R.overlayStyle(0), { dash: 'solid', shade: 'ink', marker: 'circle' });
    t.eq(R.overlayStyle(1).dash, 'dash');
    t.eq(R.overlayStyle(2).shade, 'ref');
    t.eq(R.overlayStyle(7).dash, R.overlayStyle(1).dash);
    const traces = R.overlayTraces([
        { sample_id: 7, label: '40304', percent: [0, 50, 100], temperature: [100, 250, 400] },
    ], { ink: '#000', ref: '#999' });
    t.eq(traces.length, 1);
    t.eq(traces[0].line, { color: '#000', width: 1.5, dash: 'solid' });
    t.eq(traces[0].name, '40304');
    t.eq(R.MAX_OVERLAY, 6);
    t.eq(R.toggleSelected([1, 2], 2), [1]);
    t.eq(R.toggleSelected([1, 2], 3), [1, 2, 3]);
    t.eq(R.toggleSelected([1, 2, 3, 4, 5, 6], 7), [1, 2, 3, 4, 5, 6]);      // at most six
};
