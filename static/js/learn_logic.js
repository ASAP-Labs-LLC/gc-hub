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
        heavyEnd: () => mix([[1, base('diesel2')],
                             [0.32, scaled(AXIS.map(x => gauss(x, 29, 5.5) + 0.3 * pickets(x, 22, 40, c => gauss(c, 29, 5.5))))]]),
    };
    const cache = {};
    /** {x: carbons C4..C44, y: 0..1} for a fuel or situation, else null. */
    function sketch(name) {
        if (!cache[name]) {
            let y = null;
            if (SHAPES[name]) y = base(name);
            else if (MIXES[name]) y = scaled(MIXES[name]());
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

    // ── the tutorial player ─────────────────────────────────────────────────
    function player(n) { return { i: 0, n, playing: n > 0 }; }
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
                else if (st.box !== null && st.box !== undefined) {
                    const b = st.box;
                    const ok = Array.isArray(b) && b.length === 4 && b.every(v => Number.isFinite(v) && v >= 0)
                        && b[0] + b[2] <= 1 + 1e-9 && b[1] + b[3] <= 1 + 1e-9;
                    if (!ok) out.push(at + ': box');
                }
            });
        });
        return out;
    }

    const api = { sketch, pathD, areaD, carbonTicks, bandRect, player, step, stepDelay, checkManifest,
                  RANGE: [X0, X1] };
    root.GCLearn = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
