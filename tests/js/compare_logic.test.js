// v5.0.0 lane C: the Compare view's pure logic (static/js/compare_logic.js):
// parameters and the URL, the standard pick, the findings view model and the
// Adjust drawer's validation.
const L = require('../../static/js/compare_logic.js');

module.exports = (t) => {
    // ── the trend sliders mirror the classic Analysis tab (-1..+1 <-> real)
    t.eq(L.sliderToReal('baseline', 0), 0.20);
    t.eq(L.sliderReal('baseline', -1), 0.05);
    t.eq(L.sliderToReal('baseline', 1), 0.50);
    t.eq(L.sliderToReal('detail', 0), 301);
    t.eq(L.sliderToReal('detail', 1), 51);          // inverted: more detail, smaller window
    t.eq(L.sliderToReal('detail', -1), 2001);
    t.eq(L.sliderReal('smoothing', 1), 200);
    t.eq(L.realToSlider('baseline', 0.20), 0);
    t.eq(L.realToSlider('detail', 51), 1);
    t.eq(Math.round(L.realToSlider('smoothing', 117) * 100) / 100, 0.5);
    // the real value the server gets: window odd, the others to 2 places
    t.eq(L.sliderReal('detail', 0.1), 277);
    t.eq(L.sliderReal('detail', 0.1) % 2, 1);
    t.eq(L.sliderReal('baseline', 0.333), 0.30);
    t.eq(L.sliderLabel(0.25), '+0.25');
    t.eq(L.sliderLabel(-0.5), '-0.50');
    t.eq(L.sliderLabel(0), '+0.00');

    // ── defaults from /api/settings, as the classic loadSettings reads them
    const d0 = L.defaultParams({});
    t.eq(d0, { quantile: 0.2, window: 301, sigma: 34, thresh_marginal: 100,
               thresh_moderate: 500, thresh_significant: 2000, x_max_min: 7 });
    const d1 = L.defaultParams({ analysis_quantile: '0.3', analysis_window: '501',
        analysis_thresh_marginal: '150', analysis_x_max_min: '9.5', analysis_sigma: '' });
    t.eq([d1.quantile, d1.window, d1.thresh_marginal, d1.x_max_min, d1.sigma], [0.3, 501, 150, 9.5, 34]);

    // ── the /api/analysis body: the captured parameters and the ranges ([] = none)
    const ranges = [{ id: 1, label: 'Gas', c_start: '5', c_end: 11, color: '#3fb95044' }];
    t.eq(L.analysisBody(7, 'Diesel', d0, ranges), {
        sample_id: 7, standard_name: 'Diesel', quantile: 0.2, window: 301, sigma: 34,
        thresh_marginal: 100, thresh_moderate: 500, thresh_significant: 2000, x_max_min: 7,
        ranges: [{ label: 'Gas', c_start: 5, c_end: 11, color: '#3fb95044' }] });
    t.eq(L.analysisBody(7, 'Diesel', d0, []).ranges, []);

    // ── the URL: ?standard= in and out (lane S keeps it current)
    t.eq(L.standardFromSearch('?standard=Diesel%20ULSD%20Std'), 'Diesel ULSD Std');
    t.eq(L.standardFromSearch('?x=1&standard=A%2BB'), 'A+B');
    t.eq(L.standardFromSearch(''), null);
    t.eq(L.standardFromSearch('?standard='), null);
    t.eq(L.comparePath(40, 'Diesel ULSD Std'), '/samples/40/compare?standard=Diesel%20ULSD%20Std');
    t.eq(L.comparePath(40, null), '/samples/40/compare');
    t.eq(L.comparePath(40, 'a/b&c'), '/samples/40/compare?standard=a%2Fb%26c');

    // ── the standard: explicit (URL) > remembered pick > best fit > the list's best-fit label > first
    const stds = [{ name: 'A' }, { name: 'B' }, { name: 'C' }];
    t.eq(L.pickStandard({ standards: stds }), { name: 'A', source: 'first' });
    t.eq(L.pickStandard({ standards: stds, sampleBestFit: 'C' }), { name: 'C', source: 'best' });
    t.eq(L.pickStandard({ standards: stds, sampleBestFit: 'Mix: A + B (80/20)' }), { name: 'A', source: 'first' });
    t.eq(L.pickStandard({ standards: stds, best: 'B', sampleBestFit: 'C' }), { name: 'B', source: 'best' });
    t.eq(L.pickStandard({ standards: stds, best: 'B', remembered: 'C' }), { name: 'C', source: 'remembered' });
    t.eq(L.pickStandard({ standards: stds, explicit: 'A', remembered: 'C' }), { name: 'A', source: 'explicit' });
    t.eq(L.pickStandard({ standards: stds, explicit: 'Gone', remembered: 'Gone too', best: 'B' }),
        { name: 'B', source: 'best' });
    t.eq(L.pickStandard({ standards: [] }), null);

    // ── a manual pick is remembered per sample (most recent last, capped)
    let picks = [];
    picks = L.rememberPick(picks, 5, 'A');
    picks = L.rememberPick(picks, 6, 'B');
    picks = L.rememberPick(picks, 5, 'C');
    t.eq(picks, [['6', 'B'], ['5', 'C']]);
    t.eq(L.recallPick(picks, 5), 'C');
    t.eq(L.recallPick(picks, '6'), 'B');
    t.eq(L.recallPick(picks, 9), null);
    t.eq(L.recallPick('garbage', 9), null);
    let many = [];
    for (let i = 0; i < 305; i++) many = L.rememberPick(many, i, 'S' + i, 300);
    t.eq(many.length, 300);
    t.eq(L.recallPick(many, 0), null);
    t.eq(L.recallPick(many, 304), 'S304');
    t.eq(L.parsePicks('{"not":"a list"}'), []);
    t.eq(L.parsePicks('[["1","A"],[2],"x"]'), [['1', 'A']]);
    t.eq(L.parsePicks(null), []);

    // ── findings: one row per server item, the sentence is the server's line
    const result = {
        items: [
            { kind: 'range', label: 'Gas', c_start: 5, c_end: 11, severity: 'significant',
              direction: 'higher', verdict: 'higher', t0: 0.2, t1: 1.4, mixed: false },
            { kind: 'not-evaluated', label: 'Heavy', c_start: 40, c_end: 60 },
            { kind: 'outside', label: 'Outside the defined ranges', severity: 'marginal',
              direction: 'lower', verdict: 'lower', spans: [[4.2, 5.0]] },
        ],
        text: [
            '• Gas (C5–C11, partly checked): higher than Std — significant, plus 1 sharp peak',
            '• Heavy (C40–C60): not checked, outside this run',
            '• Outside the defined ranges: lower than Std — marginal',
        ].join('\n'),
    };
    const v = L.findingsView(result);
    t.eq(v.rows.length, 3);
    t.eq(v.rows[0].heading, 'Gas (C5–C11, partly checked)');
    t.eq(v.rows[0].text, 'higher than Std — significant, plus 1 sharp peak');
    t.eq(v.rows[0].badge, 'Significant · higher');
    t.eq(v.rows[0].tone, 'dev');
    t.eq([v.rows[0].t0, v.rows[0].t1], [0.2, 1.4]);
    t.eq(v.rows[1].heading, 'Heavy (C40–C60)');
    t.eq(v.rows[1].badge, 'Not evaluated');
    t.eq(v.rows[1].tone, 'na');
    t.eq(v.rows[2].heading, 'Outside the defined ranges');
    t.eq(v.rows[2].text.startsWith('lower than Std'), true);
    t.eq(v.rows[2].badge, 'Marginal · lower');
    t.eq([v.rows[2].t0, v.rows[2].t1], [4.2, 5.0]);
    t.eq(v.deviating, 2);
    // the whole text is the server's: joining the rows gives it back
    t.eq(v.rows.map(r => '• ' + (r.heading ? r.heading + ': ' : '') + r.text).join('\n'), result.text);

    // no deviation: one plain row, no badge severity
    const none = L.findingsView({ items: [{ kind: 'none', within_ranges: true }],
        text: 'No differences found in the ranges.' });
    t.eq(none.rows, [{ key: '0-none', kind: 'none', heading: null,
        text: 'No differences found in the ranges.',
        badge: 'Within', tone: 'ok', severity: null, direction: null, t0: null, t1: null }]);
    t.eq(none.deviating, 0);
    // mixed direction (v7: the verdict, as the bullet leads with it)
    const mixedRow = L.findingsView({ items: [{ kind: 'range', label: 'Oil', c_start: 20, c_end: 44,
        severity: 'moderate', direction: 'lower', verdict: 'mixed', mixed: true, t0: 4, t1: 6 }],
        text: '• Oil (C20–C44): both higher and lower than S — moderate' }).rows[0];
    t.eq([mixedRow.badge, mixedRow.direction], ['Moderate · mixed', 'mixed']);
    // a v6 answer (no verdict): mixed, else the direction
    t.eq(L.findingsView({ items: [{ kind: 'range', label: 'Oil', c_start: 20, c_end: 44,
        severity: 'moderate', direction: 'lower', mixed: true }],
        text: '• Oil (C20–C44): x' }).rows[0].badge, 'Moderate · mixed');
    // sharp peaks only: the badge says so, as the bullet does
    const peaks = L.findingsView({ items: [{ kind: 'range', label: 'Gas', c_start: 5, c_end: 11,
        severity: 'significant', direction: 'higher', verdict: 'higher', spike_only: true,
        t0: 0.5, t1: 3.5 }],
        text: '• Gas (C5–C11): 1 sharp peak above S — significant' }).rows[0];
    t.eq([peaks.heading, peaks.badge, peaks.direction], ['Gas (C5–C11)', 'Significant · sharp peaks', 'higher']);
    // a label with a colon in it still splits at the right place
    t.eq(L.findingsView({ items: [{ kind: 'range', label: 'A: B', c_start: 1, c_end: 2,
        severity: 'marginal', direction: 'higher' }],
        text: '• A: B (C1–C2): higher than S — marginal' }).rows[0].heading, 'A: B (C1–C2)');
    // lines and items disagree: the lines, as plain rows (never invented text)
    const odd = L.findingsView({ items: [], text: 'line one\n\nline two' });
    t.eq(odd.rows.map(r => r.text), ['line one', 'line two']);
    t.eq(L.findingsView(null).rows, []);

    // ── the Adjust drawer: validation before anything is sent
    const good = { baseline: 0, detail: 0, smoothing: 0.25, thresh_marginal: '100',
        thresh_moderate: '500', thresh_significant: '2000', x_max_min: '7',
        ranges: [{ id: 1, label: ' Gas ', c_start: '5', c_end: '11', color: '#3fb95044' }] };
    const ok = L.validateAdjust(good);
    t.eq(ok.ok, true);
    t.eq(ok.errors, {});
    t.eq(ok.params.thresh_moderate, 500);
    t.eq(ok.params.window, 301);
    t.eq(ok.params.sigma, L.sliderReal('smoothing', 0.25));
    t.eq(ok.ranges, [{ id: 1, label: 'Gas', c_start: 5, c_end: 11, color: '#3fb95044' }]);

    const bad = L.validateAdjust(Object.assign({}, good, {
        thresh_marginal: '-1', thresh_moderate: 'abc', x_max_min: '0',
        ranges: [{ id: 1, label: '', c_start: '12', c_end: '5' },
                 { id: 2, label: 'x'.repeat(41), c_start: '1.5', c_end: '200' }] }));
    t.eq(bad.ok, false);
    t.eq(Object.keys(bad.errors).sort(), ['thresh_marginal', 'thresh_moderate', 'x_max_min']);
    t.eq(bad.rangeErrors[0].label, 'Give the range a name.');
    t.eq(bad.rangeErrors[0].c_end, 'The end must be at or after the start.');
    t.eq(bad.rangeErrors[1].label, 'At most 40 characters.');
    t.eq(bad.rangeErrors[1].c_start, 'A whole carbon number from 1 to 100.');
    t.eq(bad.rangeErrors[1].c_end, 'A whole carbon number from 1 to 100.');
    // thresholds must rise: marginal <= moderate <= significant
    const order = L.validateAdjust(Object.assign({}, good, { thresh_moderate: '50' }));
    t.eq(order.errors.thresh_moderate, 'At least the marginal threshold (100).');
    const order2 = L.validateAdjust(Object.assign({}, good, { thresh_significant: '400' }));
    t.eq(order2.errors.thresh_significant, 'At least the moderate threshold (500).');
    // a label is one line (the server cleans it the same way)
    t.eq(L.validateAdjust(Object.assign({}, good, { ranges: [{ label: 'a\nb', c_start: 1, c_end: 2 }] }))
        .ranges[0].label, 'a b');
    // no ranges is valid ([] = none)
    t.eq(L.validateAdjust(Object.assign({}, good, { ranges: [] })).ok, true);

    // the drawer's draft from params + overlays (and back, unchanged)
    const draft = L.draftFrom(d0, ranges);
    t.eq([draft.baseline, draft.detail, draft.smoothing], [0, 0, 0]);
    t.eq(draft.ranges[0].c_start, '5');
    t.eq(L.validateAdjust(draft).params, d0);

    // a new range gets a label, a span and a colour the payload keeps
    const added = L.newRange([{ id: 1 }, { id: 4 }]);
    t.eq(added.id, 5);
    t.eq(added.label, 'Range 3');
    t.eq(typeof added.color, 'string');

    // the parameter line (Export sheet, report queue rows)
    t.eq(L.paramSummary(d0, [{ label: 'Gas', c_start: 5, c_end: 11 }]),
        'Baseline +0.00 · Detail +0.00 · Smoothing +0.00 · Thresholds 100 / 500 / 2000 · Gas C5–C11 · Up to 7 min');
    t.eq(L.paramSummary(d0, []).includes('No ranges'), true);

    // chart colours from tokens
    t.eq(L.withAlpha('#0f172a', 0.5), 'rgba(15,23,42,0.5)');
    t.eq(L.withAlpha('#abc', 1), 'rgba(170,187,204,1)');
    t.eq(L.withAlpha('rgb(1, 2, 3)', 0.2), 'rgba(1,2,3,0.2)');
    t.eq(L.withAlpha('rgba(1,2,3,0.5)', 0.2), 'rgba(1,2,3,0.1)');
    t.eq(L.withAlpha('nonsense', 0.2), 'rgba(128,128,128,0.2)');

    // the top axis: carbon ticks thinned to fit
    t.eq(L.carbonTicks([1, 2, 3], [6, 7, 8]), { vals: [1, 2, 3], text: ['C6', 'C7', 'C8'] });
    const ticks = L.carbonTicks(Array.from({ length: 30 }, (_, i) => i), Array.from({ length: 30 }, (_, i) => i + 5));
    t.eq(ticks.vals.length <= 15, true);
    t.eq(ticks.text[0], 'C6');   // even carbons when thinned
    t.eq(L.carbonTicks([1, 2], [3]), { vals: [], text: [] });

    // the difference axis: scaled to the data and spikes, not the significant line
    t.eq(L.diffSpan([0, 1, 2, 3], [10, -300, 50, 9999], [], 100, 2.5), 300);
    t.eq(L.diffSpan([0, 1], [10, 20], [{ value: -800 }], 100, null), 800);
    t.eq(L.diffSpan([0, 1], [10, 20], [], 100, null), 100);

    // v6: conclusion presets are inserted into the conclusion text (then edited)
    const gen = 'No significant deviations from Diesel were found.';
    // no caret: appended to the end, one space apart
    t.eq(L.insertPreset(gen, 'Re-run requested.'),
        { text: gen + ' Re-run requested.', caret: (gen + ' Re-run requested.').length });
    // into nothing: just the preset
    t.eq(L.insertPreset('', '  Re-run requested. '), { text: 'Re-run requested.', caret: 17 });
    t.eq(L.insertPreset(null, 'A.'), { text: 'A.', caret: 2 });
    // a text ending in white space (a new line) needs no separator
    t.eq(L.insertPreset('Line one.\n', 'B.'), { text: 'Line one.\nB.', caret: 12 });
    // at the caret, padded with a space on each side that needs one
    t.eq(L.insertPreset('One.Two.', 'Mid.', { start: 4, end: 4 }), { text: 'One. Mid. Two.', caret: 9 });
    t.eq(L.insertPreset('One. Two.', 'Mid.', { start: 5, end: 5 }), { text: 'One. Mid. Two.', caret: 9 });
    t.eq(L.insertPreset('One.', 'Mid.', { start: 0, end: 0 }), { text: 'Mid. One.', caret: 4 });
    // a selection is replaced
    t.eq(L.insertPreset('Keep OLD keep.', 'new', { start: 5, end: 8 }), { text: 'Keep new keep.', caret: 8 });
    // a caret out of range is clamped; an empty preset changes nothing
    t.eq(L.insertPreset('abc', 'X.', { start: 99, end: 99 }), { text: 'abc X.', caret: 6 });
    t.eq(L.insertPreset('abc', '   '), { text: 'abc', caret: 3 });
    // never longer than the conclusion limit (1,500, the server's comments.CONCLUSION_MAX)
    t.eq(L.CONCLUSION_MAX, 1500);
    t.eq(L.insertPreset('x'.repeat(1495), 'Too long.'), null);
    t.eq(L.insertPreset('x'.repeat(1490), 'Fits.').text.length, 1496);
    t.eq(L.insertPreset('x'.repeat(1494), 'Fits.').text.length, 1500);   // exactly at the limit
    t.eq(L.insertPreset('x'.repeat(1497), 'No.'), null);           // 1,501

    // the live counter: "N / 1,500", warning from 90 % (1,350), at the limit
    t.eq(L.conclusionCount(''), { text: '0 / 1,500', near: false, full: false });
    t.eq(L.conclusionCount('x'.repeat(1349)), { text: '1,349 / 1,500', near: false, full: false });
    t.eq(L.conclusionCount('x'.repeat(1350)), { text: '1,350 / 1,500', near: true, full: false });
    t.eq(L.conclusionCount('x'.repeat(1500)), { text: '1,500 / 1,500', near: true, full: true });
    t.eq(L.conclusionCount(null).text, '0 / 1,500');

    // a paste: the result cut to the limit, and whether it was cut
    t.eq(L.pasteInto('abc', 'XY', { start: 1, end: 2 }), { text: 'aXYc', caret: 3, cut: false });
    const big = L.pasteInto('x'.repeat(1490), 'y'.repeat(50), { start: 1490, end: 1490 });
    t.eq([big.text.length, big.caret, big.cut], [1500, 1500, true]);
    t.eq(big.text.endsWith('y'.repeat(10)), true);
    // pasted into the middle: what follows the caret is kept, the paste is cut
    const mid = L.pasteInto('a'.repeat(1000) + 'TAIL', 'p'.repeat(800), { start: 1000, end: 1000 });
    t.eq([mid.text.length, mid.text.endsWith('TAIL'), mid.caret, mid.cut], [1500, true, 1496, true]);
    // replacing a selection frees its room
    t.eq(L.pasteInto('a'.repeat(1500), 'bb', { start: 0, end: 2 }).cut, false);

    // ── v7: a sample's Adjust state, kept in this tab (sessionStorage) ────
    t.eq(L.parseAdjustments('junk'), []);
    t.eq(L.parseAdjustments(null), []);
    const adjRanges = [{ id: 7, label: 'Jet fuel cut', c_start: 12, c_end: 18, color: '#3498db44' },
                       { id: 2, label: 'Heavy', c_start: '60', c_end: '80' }];
    let adj = L.rememberAdjustments([], 40329, { params: { quantile: 0.3, window: 'x', sigma: 20 }, ranges: adjRanges });
    adj = L.rememberAdjustments(adj, 7, { params: {}, ranges: [] });
    const back = L.recallAdjustments(JSON.stringify(adj), '40329');
    t.eq(back.params, { quantile: 0.3, sigma: 20 });
    t.eq(back.ranges.map(r => [r.id, r.label, r.c_start, r.c_end]), [[1, 'Jet fuel cut', 12, 18], [2, 'Heavy', 60, 80]]);
    t.eq(back.ranges[0].color, '#3498db44');
    t.eq(!!back.ranges[1].color, true);
    t.eq(L.recallAdjustments(adj, 7).ranges, []);             // "no ranges" is kept as such
    t.eq(L.recallAdjustments(adj, 8), null);
    t.eq(L.recallAdjustments(L.forgetAdjustments(adj, 40329), 40329), null);
    // the newest wins, at most `cap` samples kept (oldest dropped)
    let keptList = [];
    for (let i = 1; i <= 5; i++) keptList = L.rememberAdjustments(keptList, i, { params: {}, ranges: [] }, 3);
    t.eq(keptList.map(a => a.sample_id), ["3", "4", "5"]);
    keptList = L.rememberAdjustments(keptList, 3, { params: { sigma: 9 }, ranges: [] }, 3);
    t.eq(keptList.map(a => a.sample_id), ["4", "5", "3"]);
    // the summary line names every range as the report's footer does
    t.eq(L.paramSummary(d0, adjRanges).includes('Jet fuel cut C12–C18, Heavy C60–C80'), true);
    // labels are one line as the server keeps them (format characters too)
    t.eq(L.cleanLabel('Jet​fuel  cut﻿'), 'Jet fuel cut');
};
