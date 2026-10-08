// samples_logic.js (v7): the distillation curve on the Samples page's
// Overview. Its points are the Results card's rows (so a dot and its cell
// agree, toggle and 40/60 midpoints included), plus the geometry, the
// pointer's hit mapping, the keyboard path and the callout's place.
const L = require('../../static/js/samples_logic.js');

module.exports = async (t) => {
    const curve = {
        d2887: { '2887 IBP': 90.2, '2887 T5': 163.2, '2887 T10': 189.8, '2887 T50': 290.04, '2887 T95': 453.6, '2887 FBP': 518.7 },
        d86: { 'D86 IBP': 160.11, 'D86 T30': 250, 'D86 T40': 260, 'D86 T50': 270, 'D86 T60': 285, 'D86 T70': 300, 'D86 FBP': 440 },
        d86_uncorrected: { 'D86 IBP': 162.6, 'D86 T30': 257, 'D86 T40': 271.1, 'D86 T50': 285.1, 'D86 T60': 300.4, 'D86 T70': 315.8,
                           'D86 FBP': 444.5 },
    };

    // ── the points are the Results card's rows ──────────────────────────
    t.eq([L.curvePct('IBP'), L.curvePct('5%'), L.curvePct('95%'), L.curvePct('FBP'), L.curvePct('x')], [0, 5, 95, 100, null]);
    const rowsOff = L.resultRows(curve, false);
    const off = L.curveSeries(rowsOff, false);
    t.eq(off.map(s => [s.id, s.name]), [['d86', 'D86 uncorrected'], ['d2887', 'D2887']]);
    t.eq(off[0].points.map(p => p.label), ['IBP', '30%', '40%', '50%', '60%', '70%', 'FBP']);   // only points with a value
    t.eq(off[0].points[3], { label: '50%', pct: 50, t: 285.1, note: null });
    // every D86 dot equals its Results cell, whichever way the toggle is
    for (const corrected of [false, true]) {
        const rows = L.resultRows(curve, corrected);
        const s = L.curveSeries(rows, corrected);
        for (const p of s[0].points) t.eq(p.t, rows.find(r => r.label === p.label).d86);
        for (const p of s[1].points) t.eq(p.t, rows.find(r => r.label === p.label).d2887);
    }
    const on = L.curveSeries(L.resultRows(curve, true), true);
    t.eq(on[0].name, 'D86 corrected');
    t.eq(on[0].points.find(p => p.label === '50%').t, 270);
    // the 40/60 midpoint keeps the Results card's note; D2887 points have none
    t.eq(on[0].points.find(p => p.label === '40%').note, 'Midpoint of the uncorrected 30% and 50% values');
    t.eq(off[0].points.find(p => p.label === '60%').note, 'Midpoint of the 50% and 70% values');
    t.eq(off[0].points.find(p => p.label === '50%').note, null);
    t.eq(off[1].points.every(p => p.note === null), true);
    t.eq(off[1].points.find(p => p.label === '50%').t, 290.04);
    // a v1 import's table row (curveFromTable) draws the same way, uncorrected via the X4 conversion
    const conv = (byLabel) => { const o = {}; for (const k of Object.keys(byLabel)) o[k] = byLabel[k] === null ? null : byLabel[k] + 1; return o; };
    const imported = { d2887: { '2887 T30': 200, '2887 T50': 250, '2887 T70': 300 }, d86: { 'D86 T50': 255 }, d86_uncorrected: null, fromTable: true };
    const imp = L.curveSeries(L.resultRows(imported, false, conv), false);
    t.eq(imp[0].points.map(p => [p.label, p.t]), [['30%', 201], ['40%', 226], ['50%', 251], ['60%', 276], ['70%', 301]]);
    t.eq(L.curveSeries(L.resultRows(imported, true, conv), true)[0].points.map(p => [p.label, p.t]), [['50%', 255]]);
    t.eq(L.curveSeries([], false).map(s => s.points.length), [0, 0]);
    t.eq(L.curveSeries(null, true)[0].points, []);

    // ── words ───────────────────────────────────────────────────────────
    t.eq(L.tempText(285.1), '285.1 °C');
    t.eq(L.tempText(285.149), '285.1 °C');
    t.eq(L.tempText(-3.25), '−3.3 °C');
    t.eq(L.tempText(null), '—');
    t.eq(L.curvePointLabel(off[0], off[0].points[3]), '50%: 285.1 °C, D86 uncorrected');
    t.eq(L.curvePointLabel(off[1], off[1].points[0]), 'IBP: 90.2 °C, D2887');

    // ── axes ────────────────────────────────────────────────────────────
    t.eq(L.niceTicks(90.2, 518.7, 5), { lo: 0, hi: 600, step: 100, ticks: [0, 100, 200, 300, 400, 500, 600] });
    t.eq(L.niceTicks(162, 445, 6).ticks, [150, 200, 250, 300, 350, 400, 450]);
    t.eq(L.niceTicks(200, 200, 4).ticks, [195, 197.5, 200, 202.5, 205]);
    t.eq(L.niceTicks(10, 0, 5).lo, 0);                     // reversed bounds are fine
    t.eq(L.niceTicks(NaN, 3, 5).ticks, [0, 1]);
    t.eq(L.curveXTicks(600).labels.map(l => l.text), ['IBP', '10', '20', '30', '40', '50', '60', '70', '80', '90', 'FBP']);
    t.eq(L.curveXTicks(260).labels.map(l => l.text), ['IBP', '20', '40', '60', '80', 'FBP']);
    t.eq(L.curveXTicks(600).marks, [0, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100]);

    const sc = L.curveScale(off, 500, 260);
    t.eq(sc.plot, { left: 44, top: 14, right: 484, bottom: 230 });
    t.eq(sc.x(0), 44);
    t.eq(sc.x(100), 484);
    t.eq(sc.x(50), 264);
    t.eq(sc.yTicks[0] <= 90.2 && sc.yTicks[sc.yTicks.length - 1] >= 518.7, true);
    t.eq(sc.y(sc.yTicks[0]), 230);                         // the lowest tick on the x axis
    t.eq(sc.y(sc.yTicks[sc.yTicks.length - 1]), 14);       // the highest at the top
    // empty series still give a usable frame
    t.eq(L.curveScale(L.curveSeries([], false), 300, 200).plot.right, 284);

    // ── the pointer: nearest % column, then the nearer series ───────────
    const at = (s, label) => { const p = s.points.find(x => x.label === label); return [sc.x(p.pct), sc.y(p.t)]; };
    const [x50, y50] = at(off[0], '50%');
    t.eq(L.curveHit(off, sc, x50, y50), { series: 'd86', label: '50%' });
    t.eq(L.curveHit(off, sc, x50 + 6, y50 + 3), { series: 'd86', label: '50%' });
    const [, y50b] = at(off[1], '50%');
    t.eq(L.curveHit(off, sc, x50, y50b - 3), { series: 'd2887', label: '50%' });  // same column, above both: D2887 is the hotter one
    t.eq(L.curveHit(off, sc, x50, y50 + 2), { series: 'd86', label: '50%' });
    // 5% has only a D2887 value: its column picks D2887 wherever the pointer is in y
    t.eq(L.curveHit(off, sc, sc.x(5), sc.plot.top), { series: 'd2887', label: '5%' });
    // between columns, the nearer one (unevenly spaced: 90, 95, 100)
    t.eq(L.curveHit(off, sc, sc.x(96), y50).label, '95%');
    t.eq(L.curveHit(off, sc, sc.x(99), y50).label, 'FBP');
    // outside the plot: nothing (12px of slack by default)
    t.eq(L.curveHit(off, sc, sc.plot.left - 13, y50), null);
    t.eq(L.curveHit(off, sc, sc.plot.left - 11, y50).label, 'IBP');
    t.eq(L.curveHit(off, sc, x50, sc.plot.bottom + 40), null);
    t.eq(L.curveHit(L.curveSeries([], false), sc, x50, y50), null);

    // ── the keyboard path ───────────────────────────────────────────────
    t.eq(L.curveStep(off, null, 'ArrowRight'), { series: 'd86', label: 'IBP' });       // nothing chosen: the first D86 dot
    const d50 = { series: 'd86', label: '50%' };
    t.eq(L.curveStep(off, d50, 'ArrowRight'), { series: 'd86', label: '60%' });
    t.eq(L.curveStep(off, d50, 'ArrowLeft'), { series: 'd86', label: '40%' });
    t.eq(L.curveStep(off, d50, 'Home'), { series: 'd86', label: 'IBP' });
    t.eq(L.curveStep(off, d50, 'End'), { series: 'd86', label: 'FBP' });
    t.eq(L.curveStep(off, { series: 'd86', label: 'FBP' }, 'ArrowRight'), { series: 'd86', label: 'FBP' });   // stays at the end
    t.eq(L.curveStep(off, d50, 'ArrowDown'), { series: 'd2887', label: '50%' });
    t.eq(L.curveStep(off, d50, 'ArrowUp'), { series: 'd2887', label: '50%' });
    // the other series lacks the %: its nearest point
    t.eq(L.curveStep(off, { series: 'd2887', label: '5%' }, 'ArrowDown'), { series: 'd86', label: 'IBP' });
    t.eq(L.curveStep(off, { series: 'd86', label: '30%' }, 'ArrowDown'), { series: 'd2887', label: '10%' });
    t.eq(L.curveStep(off, d50, 'x'), d50);
    t.eq(L.curveStep(L.curveSeries([], false), d50, 'ArrowRight'), null);
    // only D2887 drawn (a corrected view with no stored D86): starts there, ↑/↓ stay
    const onlyB = [{ id: 'd86', points: [] }, off[1]];
    t.eq(L.curveStep(onlyB, null, 'ArrowRight'), { series: 'd2887', label: 'IBP' });
    t.eq(L.curveStep(onlyB, { series: 'd2887', label: '50%' }, 'ArrowDown'), { series: 'd2887', label: '50%' });
    t.eq(L.curveFind(off, d50).point.t, 285.1);
    t.eq(L.curveFind(off, { series: 'd86', label: '5%' }), null);
    t.eq(L.curveFind(off, null), null);

    // ── the callout: up and left of the dot (the curve only rises, so that
    // is empty), down and right when the left has no room, inside the box ──
    t.eq(L.calloutPlace(200, 150, 120, 60, 500, 280), { left: 70, top: 80, side: 'upper-left' });
    t.eq(L.calloutPlace(200, 40, 120, 60, 500, 280), { left: 70, top: 0, side: 'upper-left' });     // never above the top
    t.eq(L.calloutPlace(100, 150, 120, 60, 500, 280), { left: 110, top: 160, side: 'lower-right' });
    t.eq(L.calloutPlace(100, 260, 120, 60, 500, 280), { left: 110, top: 220, side: 'lower-right' }); // never below the bottom
    t.eq(L.calloutPlace(450, 150, 120, 60, 500, 280).side, 'upper-left');
    t.eq(L.calloutPlace(20, 150, 400, 60, 300, 280).left, 0);   // wider than the box: pinned left
    t.eq(L.calloutPlace(200, 150, 120, 60, 500, 280, 20).left, 60);
};
