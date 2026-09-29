// Report payload helpers: custom regions must ride along with every
// export/queue payload so the final report can draw them.
const { rangesForPayload, buildReportItemPayload, captureReportParams,
        overlaysFromSettings } =
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
};
