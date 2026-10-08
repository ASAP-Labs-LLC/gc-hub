// Report payload helpers: custom regions must ride along with every
// export/queue payload so the final report can draw them.
const { rangesForPayload, buildReportItemPayload, captureReportParams,
        overlaysFromSettings, rangesText, reportDiffers, refreshItem } =
    require('../../static/js/report_payload.js');

module.exports = (t) => {
    // rangesForPayload strips UI-only fields and keeps label/c bounds/color
    const overlays = [
        { id: 1, label: 'Gas', c_start: 5, c_end: 11, color: '#f0a50044' },
        { id: 2, label: 'Jet', c_start: 9, c_end: 16, color: '#3498db44' },
    ];
    t.eq(rangesForPayload(overlays), [
        { label: 'Gas', c_start: 5, c_end: 11, color: '#f0a50044' },
        { label: 'Jet', c_start: 9, c_end: 16, color: '#3498db44' },
    ]);

    // empty/missing input → empty list (backend falls back to saved defaults)
    t.eq(rangesForPayload([]), []);
    t.eq(rangesForPayload(null), []);

    // numeric strings from DOM inputs are coerced
    t.eq(rangesForPayload([{ label: 'X', c_start: '7', c_end: '12', color: '#fff' }]),
        [{ label: 'X', c_start: 7, c_end: 12, color: '#fff' }]);

    // buildReportItemPayload: queue item + its captured ranges
    const item = {
        lab_id: 'AB123', sample_name: 'Doc', sample_id: 42,
        standard_name: 'Diesel', bullets: 'b', conclusion: 'c',
        overlay_standards: ['x'],
        ranges: [{ label: 'Gas', c_start: 5, c_end: 11, color: '#f0a50044' }],
    };
    const payload = buildReportItemPayload(item);
    t.eq(payload.sample_id, 42);
    t.eq('sample_path' in payload, false);   // samples are addressed by id (phase 2 T4)
    t.eq(payload.standard_name, 'Diesel');
    t.eq(payload.doc_name, 'Doc');
    t.eq(payload.lab_id, 'AB123');
    t.eq(payload.overlay_standards, ['x']);
    t.eq(payload.ranges, [{ label: 'Gas', c_start: 5, c_end: 11, color: '#f0a50044' }]);

    // items queued before this feature (no ranges) → fallback ranges argument
    const legacyItem = { ...item };
    delete legacyItem.ranges;
    const p2 = buildReportItemPayload(legacyItem, overlays);
    t.eq(p2.ranges.map(r => r.label), ['Gas', 'Jet']);

    // no ranges anywhere → omit key entirely (backend uses saved defaults)
    const p3 = buildReportItemPayload(legacyItem);
    t.eq('ranges' in p3, false);

    // ── Phase 3: bullets are computed on the server, never sent ──────────
    t.eq('bullets' in payload, false);
    t.eq('bullets' in p2, false);

    // an item queued with every range removed sends `ranges: []` (= no
    // ranges), not "missing" (= saved defaults) and not the fallback
    const noRanges = buildReportItemPayload({ ...item, ranges: [] }, overlays);
    t.eq(noRanges.ranges, []);

    // captureReportParams: the operator's trend parameters, thresholds and
    // graph limit, as numbers; nothing else
    const live = { quantile: '0.25', window: 251, sigma: 20, thresh_marginal: 120,
        thresh_moderate: 450, thresh_significant: 1800, x_max_min: 6.5, other: 'x' };
    const captured = captureReportParams(live);
    t.eq(captured, { quantile: 0.25, window: 251, sigma: 20, thresh_marginal: 120,
        thresh_moderate: 450, thresh_significant: 1800, x_max_min: 6.5 });
    t.eq(captureReportParams(null), {});
    t.eq(captureReportParams({ window: 'abc', sigma: 3 }), { sigma: 3 });

    // params captured at queue time ride along (flattened for the server)…
    const queued = { ...item, params: captured };
    const p4 = buildReportItemPayload(queued, overlays, { ...live, window: 999 });
    t.eq(p4.window, 251);
    t.eq(p4.thresh_marginal, 120);
    t.eq(p4.x_max_min, 6.5);
    t.eq('params' in p4, false);
    // …and items queued before params were captured use the current ones
    const p5 = buildReportItemPayload(item, overlays, live);
    t.eq(p5.window, 251);
    t.eq(p5.quantile, 0.25);
    // no params anywhere → none sent (server uses the saved defaults)
    const p6 = buildReportItemPayload(item);
    t.eq('window' in p6, false);

    // ── overlaysFromSettings mirrors resolve_report_ranges' saved/legacy
    // order: a saved list is final, even an empty one ("[]" = no ranges)
    const savedTwo = { analysis_range_overlays: JSON.stringify([
        { label: 'Gas', c_start: 5, c_end: 11, color: '#f0a50044' },
        { label: 'Kero', c_start: 12, c_end: 18 }]) };
    t.eq(overlaysFromSettings(savedTwo), [
        { id: 1, label: 'Gas', c_start: 5, c_end: 11, color: '#f0a50044' },
        { id: 2, label: 'Kero', c_start: 12, c_end: 18, color: '#3fb95044' }]);
    t.eq(overlaysFromSettings({ analysis_range_overlays: '[]' }), []);
    t.eq(overlaysFromSettings({ analysis_range_overlays: [] }), []);
    // nothing saved, or unreadable → the legacy Gas/Oil keys
    const legacy = overlaysFromSettings({ analysis_range_overlays: '',
        analysis_gas_c_start: '6', analysis_gas_c_end: '12',
        analysis_oil_c_start: '22', analysis_oil_c_end: '40' });
    t.eq(legacy.map(r => [r.label, r.c_start, r.c_end]), [['Gas', 6, 12], ['Oil', 22, 40]]);
    t.eq(overlaysFromSettings({ analysis_range_overlays: '{bad' }).map(r => [r.label, r.c_start, r.c_end]),
        [['Gas', 5, 11], ['Oil', 20, 44]]);

    // ── v7: which ranges a queue item prints (the report footer's words) ──
    t.eq(rangesText([{ label: 'Gas', c_start: 5, c_end: 11 }, { label: ' Jet  fuel ', c_start: '18', c_end: '12' }]),
        'Gas C5–C11, Jet fuel C12–C18');
    t.eq(rangesText([]), '');                      // an explicit [] = no ranges
    t.eq(rangesText(undefined), null);             // never captured: the hub's saved defaults
    t.eq(rangesText([{ label: '', c_start: 7, c_end: 7 }]), 'Range C7–C7');
    const forty = 'A forty character range label, exactly!';
    t.eq(rangesText([{ label: forty, c_start: 60, c_end: 80 }]), forty + ' C60–C80');

    // reportDiffers: would the open view build another report than the item?
    const queuedItem = { standard_name: 'Diesel', conclusion: '', params: captured,
        ranges: [{ label: 'Gas', c_start: 5, c_end: 11, color: '#f0a50044' }] };
    const view = JSON.parse(JSON.stringify(queuedItem));
    t.eq(reportDiffers(queuedItem, view), false);
    t.eq(reportDiffers(queuedItem, { ...view, ranges: [{ id: 1, label: 'Gas', c_start: '5', c_end: '11', color: '#000' }] }),
        false);                                   // UI ids, DOM strings and colours don't count
    t.eq(reportDiffers(queuedItem, { ...view, ranges: [] }), true);
    t.eq(reportDiffers(queuedItem, { ...view, ranges: [...view.ranges, { label: 'Jet', c_start: 9, c_end: 16 }] }), true);
    t.eq(reportDiffers(queuedItem, { ...view, ranges: [{ label: 'Gasoline', c_start: 5, c_end: 11 }] }), true);
    t.eq(reportDiffers(queuedItem, { ...view, standard_name: 'Red' }), true);
    t.eq(reportDiffers(queuedItem, { ...view, params: { ...captured, thresh_marginal: 150 } }), true);
    t.eq(reportDiffers(queuedItem, { ...view, conclusion: 'Edited on screen.' }), true);
    t.eq(reportDiffers({ ...queuedItem, conclusion: 'Typed in the export sheet.' }, view), false);
    const reordered = { ...queuedItem, ranges: [{ label: 'A', c_start: 1, c_end: 2 }, { label: 'B', c_start: 3, c_end: 4 }] };
    t.eq(reportDiffers(reordered, { ...reordered, ranges: reordered.ranges.slice().reverse() }), true);
    t.eq(reportDiffers({ ...queuedItem, ranges: undefined }, view), true);   // saved defaults vs explicit
    t.eq(reportDiffers(queuedItem, null), false);

    // refreshItem: the view's standard, parameters, ranges and edited
    // conclusion; the item's title and other standards kept
    const fromSheet = { ...queuedItem, sample_id: 42, lab_id: 'AB123', sample_name: 'Custom title',
        overlay_standards: ['x'], conclusion: 'Sheet text' };
    const now = { sample_id: 42, lab_id: 'AB123', sample_name: 'GC Analysis', standard_name: 'Diesel',
        conclusion: '', params: { ...captured, sigma: 50 }, overlay_standards: [],
        ranges: [{ id: 3, label: 'Jet', c_start: 9, c_end: 16, color: '#fff' }] };
    const r1 = refreshItem(fromSheet, now);
    t.eq([r1.sample_name, r1.overlay_standards, r1.conclusion, r1.params.sigma], ['Custom title', ['x'], 'Sheet text', 50]);
    t.eq(r1.ranges.map(r => r.label), ['Jet']);
    t.eq(refreshItem(fromSheet, { ...now, conclusion: 'On screen' }).conclusion, 'On screen');
    t.eq(refreshItem(fromSheet, { ...now, standard_name: 'Red' }).conclusion, '');   // written for another standard
    t.eq(refreshItem(fromSheet, { ...now, ranges: [] }).ranges, []);
};
