/* learn_logic.js (v8.2): the Learn page's pure part (window.GCLearn), shared
   with the Node tests: the fuel sketches, their SVG, and the tutorial player.

   * Sketches: what each fuel and each common situation looks like on a
     D2887 chromatogram, drawn on a carbon-number axis (C4–C44), the peak
     scaled to 1. They are sketches, not runs: shapes placed from the lab's
     own numbers (the Diesel #2 standard's 10/50/90% at C10.6 / C15.4 /
     C21.0, renewable's narrow C15–C18 band, FAME in the Bio C19–C21 band).
   * SVG: a line, a filled area, carbon ticks and a range band in a box.
   * The tutorial player: which step shows, playing or paused; autoplay
     stops on the last step; play there starts over.
   * checkManifest: the tutorials.json the page shows (tools/learn_capture.py). */
(function (root) {
    'use strict';

    const X0 = 4;
    const X1 = 44;
    const STEP = 0.125;                    // carbons per point: integers fall on the grid
    const AXIS = Array.from({ length: Math.round((X1 - X0) / STEP) + 1 }, (_v, i) => X0 + i * STEP);

    function gauss(x, mu, sig) { return Math.exp(-0.5 * ((x - mu) / sig) ** 2); }
    /** A hump skewed to the heavy side: its own spread on each side. */
    function hump(x, mu, left, right) { return gauss(x, mu, x < mu ? left : right); }
    /** The n-alkane "picket fence": a thin peak at each carbon, as tall as env(c). */
    function pickets(x, lo, hi, env, width) {
        let y = 0;
        for (let c = lo; c <= hi; c++) y += env(c) * gauss(x, c, width || 0.07);
        return y;
    }
    /** A fixed pseudo-random sequence (the gasoline sketch is the same every time). */
    function seq(seed, n) {
        const out = [];
        let s = seed;
        for (let i = 0; i < n; i++) { s = (s * 1103515245 + 12345) % 2147483648; out.push(s / 2147483648); }
        return out;
    }
    function scaled(y) {
        const m = Math.max(...y);
        return y.map(v => (m > 0 ? v / m : 0));
    }
    function mix(parts) {                    // [[weight, ys]], each ys peak-scaled first
        return AXIS.map((_x, i) => parts.reduce((a, [w, ys]) => a + w * ys[i], 0));
    }

    const SHAPES = {
        diesel2: x => hump(x, 15.4, 3.75, 4.4) + 0.6 * pickets(x, 8, 26, c => hump(c, 15.4, 3.75, 4.4)),
        diesel1: x => hump(x, 11.8, 1.7, 2.1) + 0.6 * pickets(x, 7, 18, c => hump(c, 11.8, 1.7, 2.1)),
        jet: x => hump(x, 11.4, 1.6, 2.0) + 0.5 * pickets(x, 7, 17, c => hump(c, 11.4, 1.6, 2.0)),
        renewable: x => 0.35 * gauss(x, 16.6, 1.3)
            + pickets(x, 14, 19, c => ({ 14: 0.12, 15: 0.75, 16: 1.0, 17: 0.95, 18: 0.8, 19: 0.1 })[c], 0.09),
        biodiesel: x => 0.45 * gauss(x, 18.7, 0.12) + gauss(x, 20.3, 0.12) + 0.25 * gauss(x, 20.55, 0.1),
        oil: x => gauss(x, 29, 5.5) + 0.3 * pickets(x, 22, 40, c => gauss(c, 29, 5.5)),
        gasoline: (() => {
            const r = seq(7, 60);
            const peaks = r.slice(0, 30).map((v, k) => [4.3 + v * 7.5, 0.25 + 0.75 * r[30 + k]]);
            return x => 0.3 * gauss(x, 7, 1.6) + peaks.reduce((a, [c, hgt]) =>
                a + hgt * Math.exp(-(c - 4.3) / 6) * gauss(x, c, 0.09), 0);
        })(),
    };
    function base(name) { return scaled(AXIS.map(SHAPES[name])); }
    const MIXES = {
        b20: () => mix([[0.8, base('diesel2')], [0.55, base('biodiesel')]]),
        gasInDiesel: () => mix([[1, base('diesel2')], [0.9, base('gasoline')]]),
        mix12: () => mix([[0.55, base('diesel2')], [0.6, base('diesel1')]]),
        heavyEnd: () => mix([[1, base('diesel2')], [0.32, base('oil')]]),
    };
    const cache = {};
    /** {x: carbons C4..C44, y: 0..1} for a fuel or situation, else null.
        'withSolvent' (v8.3, the lesson): Diesel #2 with the solvent's spike
        at the start, cut off at the top as the hub's charts cut it. */
    function sketch(name) {
        if (!cache[name]) {
            let y = null;
            if (SHAPES[name]) y = base(name);
            else if (MIXES[name]) y = scaled(MIXES[name]());
            else if (name === 'withSolvent') {
                y = base('diesel2').map((v, i) => Math.min(1, v + 2.5 * gauss(AXIS[i], 4.45, 0.07)));
            }
            if (!y) return null;
            cache[name] = { x: AXIS.slice(), y };
        }
        return { x: cache[name].x.slice(), y: cache[name].y.slice() };
    }

    // ── SVG in a box {x, y, w, h}; y up, 0..1 ──────────────────────────────
    function sx(c, box, range) { return box.x + (c - range[0]) / (range[1] - range[0]) * box.w; }
    function sy(v, box) { return box.y + box.h * (1 - Math.min(1, Math.max(0, v))); }
    function f(n) { return n.toFixed(1); }
    function pathD(trace, box, range) {
        return trace.x.map((c, i) => (i ? 'L' : 'M') + f(sx(c, box, range)) + ' ' + f(sy(trace.y[i], box))).join('');
    }
    function areaD(trace, box, range) {
        const n = trace.x.length;
        const floor = f(box.y + box.h);
        return 'M' + f(sx(trace.x[0], box, range)) + ' ' + floor
            + trace.x.map((c, i) => 'L' + f(sx(c, box, range)) + ' ' + f(sy(trace.y[i], box))).join('')
            + 'L' + f(sx(trace.x[n - 1], box, range)) + ' ' + floor + 'Z';
    }
    /** Carbon labels every *every* carbons inside the range: [{label, x}]. */
    function carbonTicks(range, box, every) {
        const out = [];
        for (let c = Math.ceil(range[0] / every) * every; c <= range[1]; c += every) {
            out.push({ label: 'C' + c, x: sx(c, box, range) });
        }
        return out;
    }
    /** A range band [c0, c1] as a rect in the box (clipped to it), or null. */
    function bandRect(band, box, range) {
        const lo = Math.max(band[0], range[0]);
        const hi = Math.min(band[1], range[1]);
        if (hi <= lo) return null;
        const x = sx(lo, box, range);
        return { x, y: box.y, w: sx(hi, box, range) - x, h: box.h };
    }

    /** v8.3: the area between curve *a* and curve *b* (same x), split:
        {above: where b is over a (red), below: where b is under a (blue)}. */
    function diffD(a, b, box, range) {
        function between(upper, lower) {
            const fwd = a.x.map((c, i) => (i ? 'L' : 'M') + f(sx(c, box, range)) + ' ' + f(sy(upper[i], box))).join('');
            const back = a.x.map((c, i) => 'L' + f(sx(c, box, range)) + ' ' + f(sy(lower[i], box))).reverse().join('');
            return fwd + back + 'Z';
        }
        const hi = a.y.map((v, i) => Math.max(v, b.y[i]));
        const lo = a.y.map((v, i) => Math.min(v, b.y[i]));
        return { above: between(hi, a.y), below: between(a.y, lo) };
    }

    // ── v8.3: the cases we see most, as what each does to a reference ──────
    // The same seven cases on the Learn page's chart (the reference: the
    // Diesel #2 sketch) and in Compare's Patterns (the reference: the
    // standard on screen, its times turned into carbons with the run's
    // ladder): each a change to the reference curve at the carbons it shows.
    const NORM = {};
    function shapeAt(name, c) {
        if (!(name in NORM)) NORM[name] = AXIS.reduce((m, x) => Math.max(m, SHAPES[name](x)), 0) || 1;
        return SHAPES[name](c) / NORM[name];
    }
    function ramp(c, lo, hi) {
        const t = Math.min(1, Math.max(0, (c - lo) / (hi - lo)));
        return t * t * (3 - 2 * t);
    }
    const PATTERNS = [
        { id: 'gas', name: 'Gasoline in diesel', band: [5, 11], label: 'Lighter',
          see: 'Extra sharp peaks in Lighter C5–C11, before the hump starts. Red at the far left of the Difference chart; often an early high-signal flag and a best fit of Mix.',
          why: 'A little gasoline drops the flash a lot (one #2 and gasoline mix flashed at 19 °C): a fire risk, and a fail as diesel.',
          check: 'Check the flash point.', often: 'About 1 in 70 samples.',
          apply: (y, c, top) => y + 0.7 * top * shapeAt('gasoline', c) },
        { id: 'light', name: 'Lighter fuel in diesel', band: [8, 13], label: '#1 / jet',
          see: 'The hump spreads to the left, around C9–C12, with no sharp early peaks. Best fit: Mix: Diesel #2 + Diesel #1.',
          why: 'Diesel #1, jet or kerosene in #2. It passes as #1 (38 °C) but fails as #2 (52 °C): low flash without gasoline.',
          check: 'Check the flash point; at 52 °C or lower the COA needs a GC graph.', often: 'About 1 in 90 flash results is 52 °C or lower.',
          apply: (y, c, top) => 0.7 * y + 0.55 * top * shapeAt('diesel1', c) },
        { id: 'highflash', name: 'Light ends missing', band: [5, 11], label: 'Lighter',
          see: 'Less than the standard in Lighter C5–C11: the light ends are gone (evaporated, or a heavier fuel).',
          why: 'Not a safety fail (flash is a minimum), but a flash above 84 °C is flagged and the COA needs a graph that explains it.',
          check: 'Check the flash point and the initial boiling point.', often: 'About 1 in 90 flash results is above 84 °C.',
          apply: (y, c) => y * ramp(c, 9.5, 12.5) },
        { id: 'mix', name: 'Renewable mixed in', band: [15, 18], label: 'R99',
          see: 'A tall, narrow band at C15–C18 standing out of the hump. Best fit "Mix: A + B (80/20)", or just Mix; a range reads both higher and lower.',
          why: 'A fuel mix: its properties fall between the two fuels. Check them against what was declared.',
          check: 'Check flash, T90, cetane and density against the declared product.', often: 'Common: #2 with #1, or with renewable.',
          apply: (y, c, top) => 0.6 * y + 0.65 * top * shapeAt('renewable', c) },
        { id: 'heavy', name: 'Heavy end, oil', band: [22, 44], label: 'Heavier',
          see: 'More than the standard in Heavier C22–C44: a tail on the right of the hump.',
          why: 'It raises T90 and the final boiling point (#2’s T90 limit is 338 °C) and can cost power and fuel economy.',
          check: 'Check the distillation’s T90 and final boiling point.', often: '',
          apply: (y, c, top) => y + 0.35 * top * shapeAt('oil', c) },
        { id: 'bio', name: 'Biodiesel in diesel', band: [19, 21], label: 'Bio',
          see: 'Sharp peaks standing out of the hump in Bio C19–C21.',
          why: 'More than 5% in fuel sold as straight diesel means unintended blending; it also brings cold-flow, water and oxidation risks.',
          check: 'Check the biodiesel content.', often: '',
          apply: (y, c, top) => y + 0.6 * top * shapeAt('biodiesel', c) },
        { id: 'water', name: 'Water: a flat curve', band: null, label: '',
          see: 'A flat, low curve: little fuel signal anywhere, and the distillation results come out wrong.',
          why: 'Water over 200 ppm is a warning, over 500 critical; water and sediment over 0.05% is flagged. Corrosion, bacteria, plugged filters.',
          check: 'A messed-up distillation: check the water content (and water and sediment) first.', often: '',
          apply: y => 0.07 * y },
    ];
    /** The cases, as data: [{id, name, band, label, see, why, check, often}]. */
    function patterns() {
        return PATTERNS.map(({ apply: _a, ...rest }) => Object.assign({}, rest, { band: rest.band ? rest.band.slice() : null }));
    }
    /** The reference {c: carbons, y} as case *id* would show it (same length), or null. */
    function applyPattern(id, ref) {
        const p = PATTERNS.find(q => q.id === id);
        if (!p || !ref || !Array.isArray(ref.c) || !Array.isArray(ref.y)) return null;
        let top = 0;
        for (const v of ref.y) if (Number.isFinite(v) && v > top) top = v;
        return ref.y.map((v, i) => {
            const out = p.apply(Number.isFinite(v) ? v : 0, ref.c[i], top);
            return Number.isFinite(out) && out > 0 ? out : 0;
        });
    }
    /** Carbon numbers at *times* from a run's ladder (its anchors' times and
        carbons): straight lines between anchors, the end slopes beyond; null
        with fewer than two anchors. */
    function carbonsAt(times, calTimes, calCarbons) {
        const pts = (calTimes || []).map((t, i) => [Number(t), Number(calCarbons[i])])
            .filter(([t, c]) => Number.isFinite(t) && Number.isFinite(c)).sort((a, b) => a[0] - b[0]);
        if (pts.length < 2) return null;
        return (times || []).map((t) => {
            let k = 1;
            while (k < pts.length - 1 && t > pts[k][0]) k++;
            const [t0, c0] = pts[k - 1];
            const [t1, c1] = pts[k];
            return t1 === t0 ? c0 : c0 + (t - t0) * (c1 - c0) / (t1 - t0);
        });
    }
    /** The time a carbon number comes off at, from the same ladder (null without one). */
    function timeAt(carbon, calTimes, calCarbons) {
        const flipped = carbonsAt([carbon], calCarbons, calTimes);
        return flipped ? flipped[0] : null;
    }

    // ── v8.3: reading a chromatogram, a short lesson ───────────────────────
    // Each step names its part of the chart: a band (carbons), a focus
    // ('axis', 'ticks', 'solvent'), and from step 6 the standard (#2, grey)
    // and a sample (a case applied to it), with the difference filled.
    const LESSON = [
        { title: 'Time runs left to right', focus: 'axis',
          text: 'The GC boils the sample off its column: the lightest material comes off first, on the left, the heaviest last, on the right. The bottom axis is time in minutes.' },
        { title: 'Carbon numbers are on top', focus: 'ticks',
          text: 'The run’s calibration (its n-alkane ladder) puts carbon numbers along the top: C5, C10, C20… Read where things are in carbons. No calibration, no carbon numbers.' },
        { title: 'Skip the solvent', focus: 'solvent',
          text: 'The tall spike at the very start is the solvent the sample was diluted in, not the fuel. The hub skips the first moments; so can you.' },
        { title: 'Find the hump', band: [9, 25], label: 'Hump',
          text: 'Most of the fuel comes off in one broad hump. Where it sits is the boiling range: Diesel #2 from about C9 to C25, centred near C15. How wide it is, is the cut.' },
        { title: 'Read the picket fence', band: [8, 26], label: 'n-alkanes',
          text: 'The tall, thin peaks are the n-alkanes, one at each carbon. A clear fence is ordinary petroleum fuel; renewable has a few tall ones in a narrow band; biodiesel has none of its own.' },
        { title: 'Check the light end', band: [5, 11], label: 'Lighter', sample: 'gas',
          text: 'Lighter C5–C11, against the standard (grey). More here is a lighter fuel or gasoline: low flash. Less here means the light ends are gone: high flash.' },
        { title: 'Check the Bio band', band: [19, 21], label: 'Bio', sample: 'bio',
          text: 'Bio C19–C21: sharp peaks standing out of the hump are biodiesel (FAME). Over 5% in straight diesel is unintended blending.' },
        { title: 'Check the heavy end', band: [22, 44], label: 'Heavier', sample: 'heavy',
          text: 'Heavier C22–C44: a tail here is a heavier product, such as oil. It raises T90 and the final boiling point.' },
        { title: 'Compare shapes, not heights', sample: 'light',
          text: 'In Compare the sample is the dark line, the standard the grey area. How tall a trace is depends on how much was injected, so compare where it sits and its shape.' },
        { title: 'Read the difference', sample: 'gas', diff: true,
          text: 'Sample minus standard: red where the sample has more, blue where it has less. Marginal from 100, moderate from 500, significant from 2000; sharp peaks are counted on their own.' },
        { title: 'Decide, then confirm', sample: 'gas', band: [5, 11], label: 'Lighter',
          text: 'Match the shape to a case (What we see most here, or Patterns in Compare), write the conclusion, and confirm it with flash, distillation and the other tests. A GC result is screening, not proof.' },
    ];
    function lesson() {
        return LESSON.map(st => Object.assign({ band: null, label: '', focus: null, sample: null, diff: false }, st,
                                              { band: st.band ? st.band.slice() : null }));
    }

    // ── the tutorial player ─────────────────────────────────────────────────
    /** v8.3: *playing* false for a self-paced player (the lesson). */
    function player(n, playing) { return { i: 0, n, playing: n > 0 && playing !== false }; }
    /** 'next' | 'prev' | 'play' | 'pause' | 'tick' (the timer) | {go: i} (a dot). */
    function step(s, action) {
        const last = Math.max(0, s.n - 1);
        if (action && typeof action === 'object' && 'go' in action) {
            return { i: Math.min(last, Math.max(0, Number(action.go) || 0)), n: s.n, playing: false };
        }
        switch (action) {
            case 'next': return { i: Math.min(last, s.i + 1), n: s.n, playing: s.playing };
            case 'prev': return { i: Math.max(0, s.i - 1), n: s.n, playing: s.playing };
            case 'pause': return { i: s.i, n: s.n, playing: false };
            case 'play': return s.n ? { i: s.i >= last ? 0 : s.i, n: s.n, playing: true } : s;
            case 'tick':
                if (!s.playing) return s;
                if (s.i >= last) return { i: s.i, n: s.n, playing: false };
                return { i: s.i + 1, n: s.n, playing: true };
            default: return s;
        }
    }
    /** How long a step shows while playing (ms): time to read its caption. */
    function stepDelay(caption) {
        return Math.min(7000, 2500 + 45 * String(caption || '').length);
    }

    const IMAGE = /^shots\/[a-z0-9-]+\.(webp|png)$/;
    const IMAGE_DARK = /^shots\/dark\/[a-z0-9-]+\.(webp|png)$/;
    function boxOk(b) {
        return b === null || b === undefined || (Array.isArray(b) && b.length === 4
            && b.every(v => Number.isFinite(v) && v >= 0) && b[0] + b[2] <= 1 + 1e-9 && b[1] + b[3] <= 1 + 1e-9);
    }
    /** v8.3: a step's screenshot and box for the theme (the dark set when
        there is one): {image, box}. */
    function stepImage(st, theme) {
        if (theme === 'dark' && st.image_dark) return { image: st.image_dark, box: st.box_dark || null };
        return { image: st.image, box: st.box || null };
    }
    /** The problems with a tutorials manifest ([] when the page can show it). */
    function checkManifest(m) {
        if (!m || typeof m !== 'object' || m.version !== 1 || !Array.isArray(m.tutorials)) {
            return ['not a version 1 tutorials manifest'];
        }
        const out = [];
        m.tutorials.forEach((t, ti) => {
            const where = `tutorial ${ti + 1}`;
            if (!t || typeof t.id !== 'string' || typeof t.title !== 'string' || !Array.isArray(t.steps) || !t.steps.length) {
                out.push(where + ': id, title and steps are needed');
                return;
            }
            t.steps.forEach((st, si) => {
                const at = `${t.id} step ${si + 1}`;
                if (!st || typeof st.image !== 'string' || !IMAGE.test(st.image)) out.push(at + ': image');
                else if (typeof st.caption !== 'string' || !st.caption.trim()) out.push(at + ': caption');
                else if (!boxOk(st.box)) out.push(at + ': box');
                if (st && st.image_dark !== undefined && !(typeof st.image_dark === 'string' && IMAGE_DARK.test(st.image_dark))) {
                    out.push(at + ': image_dark');
                }
                if (st && !boxOk(st.box_dark)) out.push(at + ': box_dark');
            });
        });
        return out;
    }

    const api = { sketch, pathD, areaD, diffD, carbonTicks, bandRect, player, step, stepDelay, checkManifest,
                  stepImage, patterns, applyPattern, carbonsAt, timeAt, lesson,
                  RANGE: [X0, X1] };
    root.GCLearn = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
