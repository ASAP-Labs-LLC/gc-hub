// Carbon labels drawn from a sample's own ladder (its revision's anchors, as
// served with its trace or analysis), never gc1's /api/calibration and never
// a fabricated (i + 5) carbon.
const { carbonMarkers, carbonAt, carbonSpanText, overlayLadder } = require('../../static/js/ladder.js');

module.exports = (t) => {
    // markers: one dotted line + one "Cn" label per ladder point
    const m = carbonMarkers([1.0, 2.0, 3.5], [10, 12, 16]);
    t.eq(m.shapes.map(s => [s.x0, s.x1, s.type, s.yref]), [[1, 1, 'line', 'paper'], [2, 2, 'line', 'paper'], [3.5, 3.5, 'line', 'paper']]);
    t.eq(m.annotations.map(a => [a.x, a.text]), [[1, 'C10'], [2, 'C12'], [3.5, 'C16']]);

    // no ladder, or no carbons: nothing is drawn (no invented carbon numbers)
    t.eq(carbonMarkers([], []), { shapes: [], annotations: [] });
    t.eq(carbonMarkers(null, undefined), { shapes: [], annotations: [] });
    t.eq(carbonMarkers([1.0, 2.0], []), { shapes: [], annotations: [] });
    // mismatched lengths: only the pairs
    t.eq(carbonMarkers([1.0, 2.0, 3.0], [10, 12]).annotations.map(a => a.text), ['C10', 'C12']);

    // carbonAt: linear inside, extrapolated from the end pairs (the server's rule)
    const T = [1.0, 2.0, 4.0], C = [10, 12, 16];
    t.eq(carbonAt(1.5, T, C), 11);
    t.eq(carbonAt(3.0, T, C), 14);
    t.eq(carbonAt(0.5, T, C), 9);        // below: slope of the first pair
    t.eq(carbonAt(5.0, T, C), 18);       // above: slope of the last pair
    t.eq(Number.isNaN(carbonAt(1.0, [1.0], [10])), true);
    t.eq(Number.isNaN(carbonAt(1.0, [], [])), true);

    // span text, as the server's default annotation label writes it
    t.eq(carbonSpanText(1.5, 3.0, T, C), 'C11–C14');
    t.eq(carbonSpanText(1.0, 1.1, T, C), 'C10');
    t.eq(carbonSpanText(1.0, 2.0, [], []), '');

    // chromatogram overlay: the first visible trace's ladder; `differs` when
    // another visible trace has another ladder (e.g. gc1 next to gc2)
    const gc1 = { visible: true, name: 'A', cal_times: [1, 2], cal_carbons: [10, 12] };
    const gc1b = { visible: true, name: 'B', cal_times: [1, 2], cal_carbons: [10, 12] };
    const gc2 = { visible: true, name: 'C', cal_times: [1.5, 2.5], cal_carbons: [8, 20] };
    t.eq(overlayLadder([gc1, gc1b]), { times: [1, 2], carbons: [10, 12], owner: 'A', differs: false });
    t.eq(overlayLadder([gc2, gc1]), { times: [1.5, 2.5], carbons: [8, 20], owner: 'C', differs: true });
    t.eq(overlayLadder([{ ...gc2, visible: false }, gc1]).owner, 'A');
    t.eq(overlayLadder([]), { times: [], carbons: [], owner: null, differs: false });
};
