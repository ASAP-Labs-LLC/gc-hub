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

    // ── v8.3: the cases as what they do to a reference curve (Learn's chart,
    // Compare's Patterns): on a carbon axis, any reference
    const ids = G.patterns().map(p => p.id);
    t.eq(ids, ['gas', 'light', 'highflash', 'mix', 'heavy', 'bio', 'water']);
    for (const p of G.patterns()) {
        t.eq([typeof p.name, typeof p.see, typeof p.why, typeof p.check].every(x => x === 'string'), true);
        t.eq(p.name.length <= 40 && p.see.length <= 220 && p.why.length <= 220 && p.check.length <= 120, true);
        t.eq(p.band === null ? p.id === 'water'                         // water: the whole curve
             : p.band[0] < p.band[1] && p.band[0] >= 4 && p.band[1] <= 44, true);
        t.eq('apply' in p, false);                                     // data only
    }
    const ref = { c: G.sketch('diesel2').x, y: G.sketch('diesel2').y.map(v => v * 5000) };   // any scale
    const sum = (y, lo, hi) => y.reduce((a, v, i) => a + (ref.c[i] >= lo && ref.c[i] <= hi ? v : 0), 0);
    const by = {};
    for (const id of ids) {
        const y = G.applyPattern(id, ref);
        t.eq(y.length === ref.y.length && y.every(v => Number.isFinite(v) && v >= 0), true);
        by[id] = y;
    }
    t.eq(sum(by.gas, 5, 11) > 1.5 * sum(ref.y, 5, 11), true);           // gasoline: more light end
    t.eq(sum(by.light, 8, 13) > 1.2 * sum(ref.y, 8, 13), true);         // #1/jet: a lighter shoulder
    t.eq(sum(by.highflash, 5, 9) < 0.3 * sum(ref.y, 5, 9), true);       // light ends gone
    t.eq(Math.abs(sum(by.highflash, 16, 30) - sum(ref.y, 16, 30)) < 1e-6 * sum(ref.y, 16, 30) + 1e-9, true);
    t.eq(Math.max(...by.mix.filter((_v, i) => ref.c[i] >= 15 && ref.c[i] <= 18))
         > Math.max(...ref.y.filter((_v, i) => ref.c[i] >= 15 && ref.c[i] <= 18)), true);      // renewable band
    t.eq(sum(by.heavy, 22, 44) > 1.5 * sum(ref.y, 22, 44), true);       // heavy tail
    t.eq(Math.max(...by.bio.filter((_v, i) => ref.c[i] >= 19 && ref.c[i] <= 21))
         > 1.3 * Math.max(...ref.y.filter((_v, i) => ref.c[i] >= 19 && ref.c[i] <= 21)), true); // FAME spikes
    t.eq(Math.max(...by.water) < 0.12 * Math.max(...ref.y), true);    // water: a flat curve
    t.eq(G.applyPattern('nope', ref), null);
    t.eq(G.applyPattern('gas', null), null);

    // time <-> carbon through a run's calibration ladder (Compare's x axis)
    const calT = [1.0, 2.0, 4.0], calC = [6, 10, 20];
    t.eq(G.carbonsAt([1.0, 1.5, 3.0, 4.0], calT, calC), [6, 8, 15, 20]);
    t.eq(G.carbonsAt([0.5, 5.0], calT, calC), [4, 25]);                 // the end slopes beyond the ladder
    t.eq(G.carbonsAt([1, 2], [1], [6]), null);                          // no ladder, no carbons
    t.eq(G.timeAt(15, calT, calC), 3);
    t.eq(G.timeAt(4, calT, calC), 0.5);

    // the difference between two curves, filled: red where b is above a, blue below
    const dbox = { x: 0, y: 0, w: 40, h: 10 };
    const two = G.diffD({ x: [4, 24, 44], y: [0.5, 0.5, 0.5] }, { x: [4, 24, 44], y: [0.5, 1, 0] }, dbox, [4, 44]);
    t.eq(two.above.startsWith('M') && two.above.endsWith('Z') && two.below.endsWith('Z'), true);
    t.eq(two.above.includes('20.0 0.0'), true);                          // the sample's peak, above
    t.eq(two.below.includes('40.0 10.0'), true);                         // the sample's dip, below

    // the lesson: steps that name their part of the chart
    const lesson = G.lesson();
    t.eq(lesson.length >= 8 && lesson.length <= 12, true);
    lesson.forEach((st, i) => {
        t.eq(typeof st.title === 'string' && st.title.length <= 48 && typeof st.text === 'string'
             && st.text.length >= 40 && st.text.length <= 280, true);
        if (st.band) t.eq(st.band[0] >= 4 && st.band[1] <= 44 && st.band[0] < st.band[1], true);
        if (st.sample) t.eq(ids.includes(st.sample), true);
        t.eq(['axis', 'ticks', 'solvent', null].includes(st.focus || null), true);
    });
    t.eq(lesson.some(st => st.diff) && lesson.some(st => st.focus === 'solvent'), true);
    t.eq(G.sketch('withSolvent').y.every(v => v <= 1), true);
    // the lesson is self-paced: it starts paused
    t.eq(G.player(lesson.length, false), { i: 0, n: lesson.length, playing: false });

    // the manifest's dark set (v8.3)
    const dark = JSON.parse(JSON.stringify(good));
    dark.tutorials[0].steps[0].image_dark = 'shots/dark/search-1.webp';
    dark.tutorials[0].steps[0].box_dark = [0.1, 0.1, 0.2, 0.05];
    t.eq(G.checkManifest(dark), []);
    dark.tutorials[0].steps[0].image_dark = 'shots/search-1.webp';         // not in shots/dark/
    dark.tutorials[0].steps[0].box_dark = [0.9, 0.9, 0.2, 0.2];
    t.eq(G.checkManifest(dark).length, 2);
    t.eq(G.stepImage(good.tutorials[0].steps[0], 'dark'), { image: 'shots/search-1.webp', box: [0.1, 0.1, 0.2, 0.05] });
    t.eq(G.stepImage({ image: 'shots/a.webp', box: null, image_dark: 'shots/dark/a.webp', box_dark: [0, 0, 0.5, 0.5] }, 'dark'),
         { image: 'shots/dark/a.webp', box: [0, 0, 0.5, 0.5] });
};
