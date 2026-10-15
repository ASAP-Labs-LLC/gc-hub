// v8.2: the Learn page's pure part (static/js/learn_logic.js): the fuel
// sketches, their SVG, the carbon bands, and the tutorial player.
const G = require('../../static/js/learn_logic.js');

module.exports = (t) => {
    const near = (a, b, tol) => Math.abs(a - b) <= (tol || 1e-9);

    // ── sketches: deterministic shapes on a carbon axis, peak scaled to 1
    for (const name of ['diesel2', 'diesel1', 'renewable', 'biodiesel', 'b20', 'gasoline', 'jet',
                        'gasInDiesel', 'mix12', 'heavyEnd']) {
        const s = G.sketch(name);
        t.eq([s.x.length === s.y.length, s.x.length > 200, s.x[0], s.x[s.x.length - 1]], [true, true, 4, 44]);
        t.eq(near(Math.max(...s.y), 1), true);
        t.eq(s.y.every(v => Number.isFinite(v) && v >= 0), true);
        t.eq(G.sketch(name), s);                                   // the same every time
    }
    t.eq(G.sketch('nonsense'), null);
    // where each fuel sits (its weighted middle, in carbons)
    const mid = (n) => { const s = G.sketch(n); let a = 0, w = 0;
        s.x.forEach((c, i) => { a += c * s.y[i]; w += s.y[i]; }); return a / w; };
    t.eq(mid('gasoline') < mid('diesel1') && mid('diesel1') < mid('diesel2') && mid('diesel2') < mid('heavyEnd'), true);
    t.eq(mid('renewable') > 15 && mid('renewable') < 18.5, true);
    t.eq(mid('biodiesel') > 18 && mid('biodiesel') < 21.5, true);            // the Bio C19–C21 band
    // Diesel #2's standard: 10/50/90% of its area near C10.6 / C15.4 / C21.0 (the lab's)
    const cum = (n) => { const s = G.sketch(n); const tot = s.y.reduce((a, b) => a + b, 0); let run = 0;
        return (f) => { for (let i = 0; i < s.x.length; i++) { run += s.y[i]; if (run >= f * tot) { const r = s.x[i]; run = 0; return r; } } }; };
    const d2 = cum('diesel2');
    t.eq([near(d2(0.1), 10.6, 1.0), near(d2(0.5), 15.4, 1.0), near(d2(0.9), 21.0, 1.0)], [true, true, true]);
    // gasoline in diesel: more than diesel alone below C11, the same above C14
    const gd = G.sketch('gasInDiesel'), d = G.sketch('diesel2');
    const area = (s, lo, hi) => s.x.reduce((a, c, i) => a + (c >= lo && c <= hi ? s.y[i] : 0), 0);
    t.eq(area(gd, 4, 11) / area(gd, 4, 44) > 2 * area(d, 4, 11) / area(d, 4, 44), true);

    // ── SVG: a line path and a filled area in a box, y up
    const box = { x: 10, y: 5, w: 200, h: 100 };
    const line = G.pathD({ x: [4, 24, 44], y: [0, 1, 0.5] }, box, [4, 44]);
    t.eq(line, 'M10.0 105.0L110.0 5.0L210.0 55.0');
    t.eq(G.areaD({ x: [4, 44], y: [1, 1] }, box, [4, 44]), 'M10.0 105.0L10.0 5.0L210.0 5.0L210.0 105.0Z');
    t.eq(G.carbonTicks([4, 44], box, 10).map(k => [k.label, k.x]),
         [['C10', 40], ['C20', 90], ['C30', 140], ['C40', 190]]);
    t.eq(G.bandRect([19, 21], box, [4, 44]), { x: 85, y: 5, w: 10, h: 100 });
    t.eq(G.bandRect([50, 60], box, [4, 44]), null);                 // off the chart

    // ── the tutorial player: steps, play/pause, autoplay stops at the end
    let s = G.player(4);
    t.eq(s, { i: 0, n: 4, playing: true });
    s = G.step(s, 'next'); t.eq([s.i, s.playing], [1, true]);
    s = G.step(s, 'pause'); t.eq(s.playing, false);
    s = G.step(s, 'tick'); t.eq(s.i, 1);                          // paused: no step
    s = G.step(s, 'play'); s = G.step(s, 'tick'); t.eq(s.i, 2);
    s = G.step(s, 'tick'); t.eq([s.i, s.playing], [3, true]);
    s = G.step(s, 'tick'); t.eq([s.i, s.playing], [3, false]);    // the last step: it stops there
    s = G.step(s, 'next'); t.eq(s.i, 3);
    s = G.step(s, 'play'); t.eq([s.i, s.playing], [0, true]);     // play at the end starts over
    s = G.step(s, 'prev'); t.eq(s.i, 0);
    s = G.step(s, { go: 2 }); t.eq([s.i, s.playing], [2, false]); // a dot: there, paused
    s = G.step(s, { go: 9 }); t.eq(s.i, 3);
    t.eq(G.player(0), { i: 0, n: 0, playing: false });
    // how long a step shows: longer captions stay longer, within bounds
    t.eq(G.stepDelay('Short.') >= 2500 && G.stepDelay('x'.repeat(400)) <= 7000, true);
    t.eq(G.stepDelay('A longer caption that says a bit more.') > G.stepDelay('Short.'), true);

    // ── the manifest the page trusts
    const good = { version: 1, size: [1280, 800], tutorials: [{ id: 'search', title: 'Find a sample',
        summary: 'Filter the list.', steps: [{ image: 'shots/search-1.webp', caption: 'Type a lab ID.', box: [0.1, 0.1, 0.2, 0.05] },
                                              { image: 'shots/search-2.webp', caption: 'Open a run.', box: null }] }] };
    t.eq(G.checkManifest(good), []);
    t.eq(G.checkManifest(null).length > 0, true);
    const bad = JSON.parse(JSON.stringify(good));
    bad.tutorials[0].steps[0].box = [0.9, 0.1, 0.2, 0.05];
    bad.tutorials[0].steps[1].image = '../../etc/passwd';
    t.eq(G.checkManifest(bad).length, 2);
};
