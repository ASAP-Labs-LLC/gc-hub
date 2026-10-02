// samples_logic.js (v5.0.0 lane S): the Samples page's pure rules. Status
// text and the fix a held row links to, multi-select (click, shift-click,
// drag, "select all N matching"), bulk chunking run as a task, the
// confirmation that names the count and the filter, and the numbers.
const L = require('../../static/js/samples_logic.js');

const row = (id, over) => Object.assign({ sample_id: id, lab_id: String(40300 + id), display_name: String(40300 + id),
    instrument: 'gc1', status: 'final', error: null, backfill: false, released: false }, over || {});

module.exports = async (t) => {
    // ── a row's status: glyph + words, never colour alone ───────────────
    t.eq(L.rowStatus(row(1)), { group: 'final', glyph: 'final', text: 'Final', reason: '', fix: null });
    t.eq(L.rowStatus(row(2, { status: 'received' })).glyph, 'working');
    t.eq(L.rowStatus(row(2, { status: 'received' })).reason, 'Processing…');
    const cal = L.rowStatus(row(3, { status: 'awaiting_calibration', instrument: 'gc2' }));
    t.eq(cal.group, 'held');
    t.eq(cal.glyph, 'held');
    t.eq(cal.reason, 'Held · waiting for calibration');
    t.eq(cal.fix, { label: 'Calibration', href: '/calibration?instrument=gc2' });
    t.eq(L.rowStatus(row(3, { status: 'pending_corrections', instrument: 'gc 2' })).fix,
         { label: 'Corrections', href: '/instruments/gc%202#corrections' });
    const om = L.rowStatus(row(4, { status: 'other_method', method_name: 'D7096.M' }));
    t.eq(om.reason, 'Held · other method (D7096.M)');
    t.eq(om.fix, { label: 'Methods', href: '/instruments/gc1#methods' });
    t.eq(L.rowStatus(row(4, { status: 'review_method' })).fix.label, 'Methods');
    const err = L.rowStatus(row(5, { status: 'error', error: "Couldn't read the CDF file" }));
    t.eq(err, { group: 'error', glyph: 'error', text: 'Error', reason: "Couldn't read the CDF file",
                fix: { label: 'Re-process', action: 'reprocess' } });
    t.eq(L.rowStatus(row(5, { status: 'error' })).reason, 'Processing failed');
    t.eq(L.rowStatus(row(6, { status: 'raw_only' })).fix, { label: 'Re-process', action: 'reprocess' });
    // a final backfill run that isn't released: final, but it is never exported until released
    const bf = L.rowStatus(row(7, { backfill: true }));
    t.eq(bf.group, 'final');
    t.eq(bf.reason, 'Backfill · not released');
    t.eq(bf.fix, { label: 'Release backfill', href: '/instruments/gc1#backfill' });
    t.eq(L.rowStatus(row(7, { backfill: true, released: true })).fix, null);
    // an unknown status is held, with its name, never blank
    t.eq(L.rowStatus(row(8, { status: 'mystery' })).text, 'mystery');

    // the row's second line for a final run: the best fit, else the method
    t.eq(L.rowDetail(row(1, { best_fit: { label: 'Diesel ULSD Std', score: 0.9 } })), 'Diesel ULSD Std');
    t.eq(L.rowDetail(row(1, { method_name: 'SIMDISB.M' })), 'SIMDISB.M');
    t.eq(L.rowDetail(row(1)), '');
    t.eq(L.flagText(row(1, { flags: [{ name: 'Early high-signal' }, 'Late'] })), 'Early high-signal, Late');
    t.eq(L.flagText(row(1, { flags: [] })), '');
    // v5.1.0: a review note (a late blank, a Lab ID LEM will misread) shows
    // in the list row too, as "Review: <the note>", whole (the row's CSS
    // shortens it; the full text is the tooltip and the sample's header).
    t.eq(L.reviewText(row(1, { review_note: "Lab ID '40304, rerun' contains a comma; LEM will read this row's values one column off. Rename the sample in QBench/LEM by hand." })),
         "Review: Lab ID '40304, rerun' contains a comma; LEM will read this row's values one column off. Rename the sample in QBench/LEM by hand.");
    t.eq(L.reviewText(row(1, { review_note: '  ' })), '');
    t.eq(L.reviewText(row(1, { review_note: null })), '');
    t.eq(L.reviewText(row(1)), '');
    t.eq(L.reviewText(null), '');

    // ── the injection time in the header ────────────────────────────────
    t.eq(L.injectedText('2026-09-29 14:28:05'), 'Injected Tue, Sep 29 at 14:28');
    t.eq(L.injectedText('2026-09-29T09:05'), 'Injected Tue, Sep 29 at 09:05');
    t.eq(L.injectedText(null), 'No injection time');
    t.eq(L.injectedText('2026-09-29 14:28:05', 'mtime'), 'No injection time in the CDF (file time Sep 29 14:28)');

    // ── multi-select ─────────────────────────────────────────────────────
    let s = L.selection();
    t.eq(L.selectedIds(s), []);
    s = L.toggle(s, 3);
    s = L.toggle(s, 5);
    t.eq(L.selectedIds(s), [3, 5]);
    t.eq(s.anchor, 5);
    s = L.toggle(s, 3);
    t.eq(L.selectedIds(s), [5]);
    // shift-click: the range from the anchor, added to what is selected
    const order = [9, 8, 7, 6, 5, 4, 3];
    s = L.extend(L.toggle(L.selection(), 8), order, 5);
    t.eq(L.selectedIds(s).sort(), [5, 6, 7, 8]);
    s = L.extend(s, order, 9);                  // the other way from the same anchor
    t.eq(L.selectedIds(s).sort(), [5, 6, 7, 8, 9]);
    // drag: every row passed over takes the state of the first
    s = L.paint(L.selection(), [4, 3], true);
    t.eq(L.selectedIds(s).sort(), [3, 4]);
    s = L.paint(s, [4], false);
    t.eq(L.selectedIds(s), [3]);
    // select all shown, then "all N matching this filter"
    s = L.selectShown(L.selection(), order);
    t.eq(L.selectedIds(s).length, 7);
    t.eq(L.bulkBar(s, { shown: 7, total: 7 }), { count: 7, text: '7 selected', offerAll: null, all: false });
    t.eq(L.bulkBar(s, { shown: 7, total: 1234 }),
         { count: 7, text: 'All 7 shown are selected', offerAll: 'Select all 1,234 matching this filter', all: false });
    t.eq(L.bulkBar(L.toggle(s, 9), { shown: 7, total: 1234 }).offerAll, null);   // not every row shown
    const all = L.selectAllMatching(s, 1234);
    t.eq(all.all, true);
    t.eq(L.bulkBar(all, { shown: 7, total: 1234 }),
         { count: 1234, text: 'All 1,234 matching this filter are selected', offerAll: null, all: true });
    // live updates move the matching total: the bar names the current one (the action fetches it anyway)
    t.eq(L.bulkBar(all, { shown: 7, total: 1240 }),
         { count: 1240, text: 'All 1,240 matching this filter are selected', offerAll: null, all: true });
    t.eq(L.bulkBar(all, { shown: 0, total: 0 }).count, 0);
    // any change to a single row ends "all matching"
    t.eq(L.toggle(all, 9).all, false);
    t.eq(L.clear(all).all, false);
    t.eq(L.selectedIds(L.clear(all)), []);
    t.eq(L.bulkBar(L.selection(), { shown: 7, total: 7 }).count, 0);
    // rows that leave the list leave the selection
    t.eq(L.selectedIds(L.keepOnly(L.selectShown(L.selection(), [1, 2, 3]), [2, 3, 4])), [2, 3]);

    // ── bulk work, chunked, as a task ───────────────────────────────────
    t.eq(L.chunks([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]);
    t.eq(L.chunks([], 2), []);
    const seen = [];
    const progress = [];
    const res = await L.runChunks([1, 2, 3, 4, 5], 2, async (ids) => {
        seen.push(ids);
        return { done: ids.length - (ids.includes(4) ? 1 : 0), refused: ids.includes(4) ? [{ sample_id: 4, error: 'no' }] : [] };
    }, (p) => progress.push(p));
    t.eq(seen, [[1, 2], [3, 4], [5]]);
    t.eq(res, { done: 4, refused: [{ sample_id: 4, error: 'no' }], failed: 0, stopped: false, total: 5 });
    t.eq(progress.map(p => p.sent), [2, 4, 5]);
    // a chunk that throws counts as failed and the rest still run; stop ends it between chunks
    const r2 = await L.runChunks([1, 2, 3], 1, async (ids) => { if (ids[0] === 2) throw new Error('x'); return { done: 1 }; });
    t.eq(r2.failed, 1);
    t.eq(r2.done, 2);
    let stop = false;
    const r3 = await L.runChunks([1, 2, 3], 1, async () => { stop = true; return { done: 1 }; }, null, () => stop);
    t.eq(r3.stopped, true);
    t.eq(r3.done, 1);

    t.eq(L.CHUNK.reprocess, 200);
    t.eq(L.BULK_LIMIT.reports, 200);
    t.eq(L.outcomeText('Re-process', { done: 4, refused: [1], failed: 0, total: 5, stopped: false }),
         'Re-process: 4 of 5 done · 1 refused');
    t.eq(L.outcomeText('Export to LIMS', { done: 3, refused: [], failed: 2, total: 5, stopped: true }),
         'Export to LIMS: 3 of 5 done · 2 failed · stopped');

    // the confirmation names the count and the filter
    const names = { gc1: 'GC-1', gc2: 'GC-2' };
    t.eq(L.filterText({ instrument: ['gc1'], status: ['held', 'error'], q: '403', notsent: true }, names),
         'GC-1 · Held or Error · Not sent to QBench · lab ID contains “403”');
    t.eq(L.filterText({ instrument: [], status: [], q: '', notsent: false }, names), 'All samples');
    t.eq(L.confirmText('reprocess', 1234, 'GC-1 · Held'),
         { title: 'Re-process 1,234 samples?', body: 'Every sample matching this filter: GC-1 · Held. Each is queued again with its recorded blank and corrections.', ok: 'Re-process 1,234 samples' });
    t.eq(L.confirmText('lims', 1, null).ok, 'Export 1 sample to LIMS');
    t.eq(L.confirmText('queue', 2, null).ok, 'Add 2 samples to the report queue');
    t.eq(L.confirmText('reports', 3, null).ok, 'Download 3 reports');

    // ── counts line ──────────────────────────────────────────────────────
    t.eq(L.countsText({ total: 128, today: 9 }), '128 samples · 9 today');
    t.eq(L.countsText({ total: 1, today: 0 }), '1 sample');
    t.eq(L.number(1234567), '1,234,567');

    // ── the Results card and the Data table ─────────────────────────────
    const curve = {
        d2887: { '2887 IBP': 111.587, '2887 T50': 274.45, '2887 FBP': 390.62 },
        d86: { 'D86 IBP': 171.71, 'D86 T50': 270.35, 'D86 T40': 257.7 },
        d86_uncorrected: { 'D86 IBP': 183.79, 'D86 T50': 274.41 },
    };
    const rows = L.resultRows(curve, true);
    t.eq(rows.length, 13);
    t.eq(rows[0], { label: 'IBP', d86: 171.71, d2887: 111.59, note: null, key: true });
    t.eq(rows[6].label, '50%');
    t.eq(rows[6].key, true);
    t.eq(rows[1], { label: '5%', d86: null, d2887: null, note: 'Not stored for this result.', key: false });
    t.eq(rows[5].d86, 257.7);                         // 40%: the stored midpoint, with its note
    t.eq(/Midpoint/.test(rows[5].note), true);
    t.eq(L.resultRows(curve, false)[0].d86, 183.79);  // uncorrected
    t.eq(L.fmt(171.714, 1), '171.7');
    t.eq(L.fmt(null, 1), '—');
    t.eq(L.fmt(-12.08, 2), '−12.08');

    const data = L.dataRows(curve);
    t.eq(data[0], { label: 'IBP', d2887: 111.59, raw: 183.79, correction: -12.08, reported: 171.71 });
    t.eq(data[6], { label: '50%', d2887: 274.45, raw: 274.41, correction: -4.06, reported: 270.35 });
    t.eq(data[1].correction, null);
    t.eq(L.dataTableText(data.slice(0, 1)), 'Recovery\tD2887 °C\tD86 raw\tCorrection\tD86 reported\nIBP\t111.59\t183.79\t-12.08\t171.71');

    // revisions → the history list, newest first, plain words
    const hist = L.historyItems({
        revisions: [{ revision: 1, reason: 'processed', by: 'worker', processed_at: '2026-09-29 14:29:01' },
                    { revision: 2, reason: 'reprocess', by: 'Ryan C (10.0.0.5)', processed_at: '2026-09-29 15:02:00' }],
        qbench_uploaded_at: '2026-09-29 15:21:00', qbench_revision: 2,
        received_at: null, source_name: '40329.CDF',
    });
    t.eq(hist.map(h => h.title), ['Sent to QBench', 'Revision 2 — re-processed', 'Revision 1 — processed']);
    t.eq(hist[1].detail, '2026-09-29 15:02 · Ryan C');
    t.eq(hist[0].detail, '2026-09-29 15:21 · revision 2');

    // ── carbon ticks: monochrome, labels thinned to fit ──────────────────
    const ticks = L.carbonTicks([0.2, 0.3, 0.5, 0.8, 1.1], [6, 7, 8, 9, 10], { ink: '#111', axis: '#666' }, 3);
    t.eq(ticks.shapes.length, 5);
    t.eq(ticks.shapes[0].line.color, '#666');
    // labels at least span/maxLabels apart (the ladder's span unless the chart's is given)
    t.eq(ticks.annotations.map(a => a.text), ['C6', 'C8', 'C9', 'C10']);
    t.eq(L.carbonTicks([0.2, 0.3, 0.5, 0.8, 1.1], [6, 7, 8, 9, 10], { axis: '#666' }, 3, 1.2).annotations.map(a => a.text),
         ['C6', 'C9']);
    t.eq(L.carbonTicks([], [], {}, 3), { shapes: [], annotations: [] });

    // ── the Plotly template from the tokens ──────────────────────────────
    const lay = L.chartLayout({ ink: '#0f172a', axis: '#64748b', grid: '#eef0f3', bg: '#ffffff', font: 'X' });
    t.eq(lay.paper_bgcolor, '#ffffff');
    t.eq(lay.xaxis.gridcolor, '#eef0f3');
    t.eq(lay.dragmode, 'zoom');
    t.eq(L.CHART_CONFIG.displayModeBar, false);
    t.eq(L.CHART_CONFIG.doubleClick, 'reset');

    // ── keyboard: the next sample in the shown order ─────────────────────
    t.eq(L.step([5, 4, 3], 4, 1), 3);
    t.eq(L.step([5, 4, 3], 3, 1), 3);
    t.eq(L.step([5, 4, 3], 5, -1), 5);
    t.eq(L.step([5, 4, 3], null, 1), 5);
    t.eq(L.step([], null, 1), null);
    t.eq(L.step([5, 4, 3], 99, -1), 5);

    // ── a report item for the queue (lane C's GCReportQueue.add) ─────────
    // the report's name is 'GC Analysis' as on classic (it names the PDF and the QBench attachment)
    t.eq(L.queueItem(row(1, { display_name: '40301 (2)' }), 'Diesel'),
         { sample_id: 1, lab_id: '40301', sample_name: 'GC Analysis', instrument: 'gc1', standard_name: 'Diesel' });
    t.eq(L.queueItem(row(1), null).standard_name, '');

    // ── a result-only (v1-imported) run: its numbers from /api/table ─────
    const table = { columns: ['Lab ID', 'InjectionDateTime', '2887 IBP', '2887 T30', '2887 T50', '2887 T70', 'D86 IBP', 'D86 T50', 'D86 T40', 'Best Fit'],
                    rows: [['X', '2026-09-01 10:00', '100.5', '200', '250', '300', '150.25', '255', '', 'Diesel'],
                           ['Y', '2026-09-02 10:00', '1', '2', '3', '4', '5', '6', '7', '']],
                    sample_ids: [7, 8] };
    const tc = L.curveFromTable(table, 7);
    t.eq(tc.fromTable, true);
    t.eq(tc.d2887['2887 IBP'], 100.5);
    t.eq(tc.d86['D86 IBP'], 150.25);
    t.eq(tc.d86['D86 T40'], undefined);                 // an empty cell is no value
    t.eq(tc.d86_uncorrected, null);
    t.eq(L.curveFromTable(table, 99), null);
    t.eq(L.curveFromTable(null, 7), null);
    // corrected: the stored cells; uncorrected: the D2887 converted (X4), 40/60 the midpoints
    const conv = (d) => ({ '30%': d['30%'] + 1, '50%': d['50%'] + 1, '70%': d['70%'] + 1 });
    t.eq(L.resultRows(tc, true, conv)[0].d86, 150.25);
    const un = L.resultRows(tc, false, conv);
    t.eq(un[6].d86, 251);
    t.eq(un[5].d86, 226);                               // 40%: midpoint of 201 and 251
    t.eq(un[7].d86, 276);                               // 60%: midpoint of 251 and 301
    // the Data view: raw = the conversion when the result stores none
    const dr = L.dataRows(tc, conv);
    t.eq(dr[6], { label: '50%', d2887: 250, raw: 251, correction: 4, reported: 255 });
    t.eq(L.dataRows(tc)[6].raw, null);
};
