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

    // ── v6: the row's checkbox shows the selection, and only it ─────────
    // (v5: the press toggled the selection, then the browser's click toggled
    // the box back, so the box showed the opposite of the selection)
    const order6 = [1, 2, 3, 4, 5];
    let p6 = L.pressBox(L.clear(), order6, 2, false);
    t.eq(L.selectedIds(p6.sel), [2]);
    t.eq(p6.sel.anchor, 2);
    t.eq(p6.drag, { on: true });
    t.eq(L.isChecked(p6.sel, 2), true);
    t.eq(L.isChecked(p6.sel, 3), false);
    p6 = L.pressBox(p6.sel, order6, 4, true);             // shift: from the anchor
    t.eq(L.selectedIds(p6.sel).sort(), [2, 3, 4]);
    t.eq(p6.drag, null);
    p6 = L.pressBox(p6.sel, order6, 3, false);            // press a checked one: off, and a drag paints off
    t.eq(L.selectedIds(p6.sel).sort(), [2, 4]);
    t.eq(p6.drag, { on: false });
    // "all matching" → the rows shown, less the one pressed
    const all6 = L.selectAllMatching(L.clear(), 900);
    t.eq(L.isChecked(all6, 77), true);
    p6 = L.pressBox(all6, order6, 5, false);
    t.eq(p6.sel.all, false);
    t.eq(L.selectedIds(p6.sel).sort(), [1, 2, 3, 4]);
    t.eq(L.isChecked(null, 1), false);
    // the press never mutates the selection it was given
    const before6 = L.toggle(L.clear(), 1);
    L.pressBox(before6, order6, 2, false);
    t.eq(L.selectedIds(before6), [1]);

    // ── v6: why the chart is not drawn, in one line ─────────────────────
    t.eq(L.chartReason({ status: 404, body: { error: 'Sample 9 has no stored CDF (result-only import)' } }),
         'No chromatogram: this result was imported from v1 without its CDF.');
    t.eq(L.chartReason({ status: 404, body: { error: "Sample 9's CDF is missing: cdf/gc1/x.CDF" } }),
         'No chromatogram: the stored CDF file is missing on the hub.');
    t.eq(L.chartReason({ status: 0, body: { error: 'Failed to fetch' } }),
         'The chromatogram did not load: the hub did not answer (Failed to fetch).');
    t.eq(L.chartReason({ status: 500, body: { error: 'Internal error (ref abc).' } }),
         'The chromatogram did not load: Internal error (ref abc).');
    t.eq(L.chartReason({ status: 502, body: {} }), 'The chromatogram did not load: HTTP 502.');
    t.eq(L.chartReason({ status: 404, body: { error: 'Standard not found: R99' } }), 'Standard not found: R99');

    // ── v6: the backfill reason on the row ──────────────────────────────
    const why = (r, info) => 'injected ' + r.injection_dt.slice(5, 10) + ' · before ' + info.name + ' went live';
    t.eq(L.backfillReason(row(7, { backfill: true }), null, why), 'Backfill · not released');
    t.eq(L.backfillReason(row(7, { backfill: true, injection_dt: '2026-09-20 13:30:00' }), { name: 'GC-1' }, why),
         'Backfill · not released · injected 09-20 · before GC-1 went live');
    t.eq(L.backfillReason(row(7), { name: 'GC-1' }, null), 'Backfill · not released');
    // with the real whyText
    const BF = require('../../static/js/backfill_logic.js');
    t.eq(L.backfillReason({ injection_dt: '2026-09-20 13:30:00' }, { name: 'GC-1', live_since: '2026-09-22 00:00:00' }, BF.whyText),
         'Backfill · not released · injected Sep 20 13:30 · before GC-1 went live (Sep 22 00:00)');

    // ── v6: stacking chromatograms ──────────────────────────────────────
    const s1 = { kind: 'sample', id: 11, label: '40304', sub: 'GC-1 · Sep 25 14:23' };
    const s2 = { kind: 'sample', id: 12, label: '40304 (2)' };
    const d2 = { kind: 'standard', name: 'Diesel #2' };
    const r99 = { kind: 'standard', name: 'R99' };
    t.eq(L.overlayKey(s1), 's:11');
    t.eq(L.overlayKey(d2), 'std:Diesel #2');
    let ov = L.overlayState(null);
    t.eq(ov, { mode: 'overlay', items: [] });
    let add = L.overlayAdd(ov, [s1, d2, s1, { kind: 'sample', id: 5 }], 5);   // 5 is the open sample
    t.eq(add.added, 2);
    t.eq(add.ov.items.map(L.overlayKey), ['s:11', 'std:Diesel #2']);
    t.eq(add.ov.items[1].label, 'Diesel #2');                    // a standard is named by its name
    t.eq(add.skipped.map(s => s.why), ['already', 'open']);
    t.eq(L.overlayAddText(add), 'Added 2 traces to the chart');
    t.eq(L.overlayAddText(L.overlayAdd(add.ov, [s1], 5)), 'Already on the chart');
    t.eq(L.overlayAddText(L.overlayAdd(add.ov, [{ kind: 'sample', id: 5 }], 5)), 'That is the sample open now');
    ov = add.ov;
    // the cap: the open sample plus OVERLAY_MAX others
    const many = [];
    for (let i = 0; i < 10; i++) many.push({ kind: 'sample', id: 100 + i, label: 'S' + i });
    const full = L.overlayAdd(ov, many, 5);
    t.eq(full.ov.items.length, L.OVERLAY_MAX);
    t.eq(full.added, L.OVERLAY_MAX - 2);
    t.eq(L.overlayAddText(full), 'Added 5 traces to the chart · 5 not added: the chart holds 8 traces');
    // remove one, clear all (the mode stays), switch mode
    t.eq(L.overlayRemove(ov, 's:11').items.map(L.overlayKey), ['std:Diesel #2']);
    t.eq(L.overlayMode(ov, 'stacked').mode, 'stacked');
    t.eq(L.overlayMode(ov, 'sideways').mode, 'overlay');
    t.eq(L.overlayClear(L.overlayMode(ov, 'stacked')), { mode: 'stacked', items: [] });
    // the open sample is never drawn twice when it is also in the set
    t.eq(L.overlayShown({ items: [s1, s2, d2] }, 12).map(L.overlayKey), ['s:11', 'std:Diesel #2']);
    // sessionStorage round trip; junk is an empty overlay, never a throw
    t.eq(L.overlayParse(L.overlayStringify(L.overlayMode(ov, 'stacked'))), L.overlayMode(ov, 'stacked'));
    t.eq(L.overlayParse('not json'), { mode: 'overlay', items: [] });
    t.eq(L.overlayParse('{"mode":"stacked","items":[{"kind":"sample","id":"x"},{"kind":"standard","name":" "},{"kind":"sample","id":3}]}').items,
         [{ kind: 'sample', id: 3, label: '#3', sub: '' }]);

    // styles: the open sample solid ink; samples step through ink/axis × dash; standards the reference grey
    const col = { ink: 'INK', axis: 'AXIS', ref: 'REF' };
    const st6 = L.traceStyles(['sample', 'sample', 'standard', 'sample', 'standard'], col);
    t.eq(st6, [{ color: 'INK', dash: 'solid' }, { color: 'INK', dash: 'dash' }, { color: 'REF', dash: 'solid' },
               { color: 'AXIS', dash: 'solid' }, { color: 'REF', dash: 'dash' }]);
    // every pair told apart (no two the same colour and dash) up to the cap
    const kinds8 = ['sample', 'sample', 'sample', 'sample', 'standard', 'standard', 'standard', 'standard'];
    const pairs = L.traceStyles(kinds8, col).map(s => s.color + s.dash);
    t.eq(new Set(pairs).size, 8);
    const allSamples = L.traceStyles(new Array(8).fill('sample'), col).map(s => s.color + s.dash);
    t.eq(new Set(allSamples).size, 8);

    // overlay keeps the numbers; stacked lifts each baseline by 1.08 × the tallest trace
    const tr = [{ key: 's:1', label: '<b>40304</b>', kind: 'sample', x: [0, 1, 2], y: [10, 110, 10] },
                { key: 'std:R99', label: 'R99', kind: 'standard', x: [0, 1, 2], y: [5, 55, 5] },
                { key: 's:2', label: '40305', kind: 'sample', x: [0, 1], y: [null, 20] }];
    const o1 = L.overlayTraces(tr, 'overlay', col);
    t.eq(o1.step, 0);
    t.eq(o1.data[1].y, [5, 55, 5]);
    t.eq(o1.data[0].name, '&lt;b&gt;40304&lt;/b&gt;');          // Plotly gets text, never markup
    t.eq(o1.data[0].type, 'scatter');                          // SVG, never WebGL (scattergl)
    t.eq(o1.data[1].line, { color: 'REF', width: 1.25, dash: 'solid' });
    t.eq(o1.legend.map(l => [l.key, l.label, l.primary, l.dash]),
         [['s:1', '<b>40304</b>', true, 'solid'], ['std:R99', 'R99', false, 'solid'], ['s:2', '40305', false, 'dash']]);
    const o2 = L.overlayTraces(tr, 'stacked', col);
    t.eq(o2.step, 108);
    t.eq(o2.data[0].y, [0, 100, 0]);
    t.eq(o2.data[1].y, [108, 158, 108]);
    t.eq(o2.data[2].y, [null, 216]);
    // one trace stacked is just the trace
    t.eq(L.overlayTraces(tr.slice(0, 1), 'stacked', col).data[0].y, [10, 110, 10]);
    // a trace without numbers is left out, not drawn as an empty line
    t.eq(L.overlayTraces([tr[0], { key: 'x', label: 'x' }], 'overlay', col).data.length, 1);
    t.eq(L.dashArray('dash'), '6 4');
    t.eq(L.dashArray('solid'), '');
};
