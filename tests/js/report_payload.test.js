// Report payload helpers: custom regions must ride along with every
// export/queue payload so the final report can draw them.
const { rangesForPayload, buildReportItemPayload } =
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
        lab_id: 'AB123', sample_name: 'Doc', sample_path: '/p/s.CDF',
        standard_name: 'Diesel', bullets: 'b', conclusion: 'c',
        overlay_standards: ['x'],
        ranges: [{ label: 'Gas', c_start: 5, c_end: 11, color: '#f0a50044' }],
    };
    const payload = buildReportItemPayload(item);
    t.eq(payload.sample_path, '/p/s.CDF');
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
};
