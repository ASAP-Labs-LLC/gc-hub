/* learn.js (v8.2): the Learn page's DOM. learn_logic.js (GCLearn,
   node-tested) has the sketches, their SVG and the player's rules.

   * Every .lc-fig draws its sketch: data-sketch (the line), data-std (a
     standard under it, the grey area), data-bands ("5-11:Lighter,…"),
     data-ticks (carbon labels every N carbons).
   * The walk-throughs: static/learn/tutorials.json (tools/learn_capture.py),
     checked with GCLearn.checkManifest, one card each; Watch opens the
     player: the step's screenshot, its highlight, its caption; it plays by
     itself (pausable), Back / Next, a dot per step, Escape closes.
   Every string is set with textContent; answers go through GCSession.readJson. */
(function () {
    'use strict';
    const G = window.GCLearn;
    const $ = (id) => document.getElementById(id);
    const NS = 'http://www.w3.org/2000/svg';
    const BASE = '/static/learn/';

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = String(text);
        return e;
    }
    function svg(tag, attrs) {
        const e = document.createElementNS(NS, tag);
        for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, String(v));
        return e;
    }

    // ── sketches ────────────────────────────────────────────────────────────
    function bands(spec) {
        return String(spec || '').split(',').map(s => s.trim()).filter(Boolean).map((s) => {
            const m = /^(\d+)-(\d+)(?::(.+))?$/.exec(s);
            return m ? { c: [Number(m[1]), Number(m[2])], label: m[3] || '' } : null;
        }).filter(Boolean);
    }
    function drawSketch(fig) {
        const line = G.sketch(fig.dataset.sketch);
        if (!line) return;
        const std = fig.dataset.std ? G.sketch(fig.dataset.std) : null;
        const ticks = Number(fig.dataset.ticks) || 10;
        const W = 320;
        const H = 128;
        const box = { x: 6, y: 18, w: W - 12, h: H - 36 };
        const range = G.RANGE;
        const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'lc-svg', 'aria-hidden': 'true', focusable: 'false' });
        for (const b of bands(fig.dataset.bands)) {
            const r = G.bandRect(b.c, box, range);
            if (!r) continue;
            s.append(svg('rect', { x: r.x, y: r.y, width: r.w, height: r.h, class: 'lc-band' }));
            if (b.label) {
                const t = svg('text', { x: r.x + r.w / 2, y: box.y - 6, class: 'lc-band-label', 'text-anchor': 'middle' });
                t.textContent = b.label;
                s.append(t);
            }
        }
        s.append(svg('line', { x1: box.x, x2: box.x + box.w, y1: box.y + box.h, y2: box.y + box.h, class: 'lc-axis' }));
        if (std) s.append(svg('path', { d: G.areaD(std, box, range), class: 'lc-std' }));
        s.append(svg('path', { d: G.pathD(line, box, range), class: 'lc-line' }));
        for (const k of G.carbonTicks(range, box, ticks)) {
            s.append(svg('line', { x1: k.x, x2: k.x, y1: box.y + box.h, y2: box.y + box.h + 3, class: 'lc-axis' }));
            const t = svg('text', { x: k.x, y: H - 4, class: 'lc-tick', 'text-anchor': 'middle' });
            t.textContent = k.label;
            s.append(t);
        }
        fig.replaceChildren(s);
    }

    // ── the walk-throughs ───────────────────────────────────────────────────
    const P = { tut: null, s: null, timer: null };
    function stopTimer() { clearTimeout(P.timer); P.timer = null; }
    function render() {
        const t = P.tut;
        const s = P.s;
        const st = t.steps[s.i];
        $('tut-title').textContent = t.title;
        $('tut-count').textContent = `Step ${s.i + 1} of ${s.n}`;
        const img = $('tut-img');
        img.src = BASE + st.image;
        img.alt = st.caption;
        $('tut-caption').textContent = st.caption;
        const box = $('tut-box');
        if (Array.isArray(st.box)) {
            box.style.left = (st.box[0] * 100) + '%';
            box.style.top = (st.box[1] * 100) + '%';
            box.style.width = (st.box[2] * 100) + '%';
            box.style.height = (st.box[3] * 100) + '%';
            box.hidden = false;
        } else {
            box.hidden = true;
        }
        $('tut-prev').disabled = s.i === 0;
        $('tut-next').disabled = s.i >= s.n - 1;
        $('tut-play').textContent = s.playing ? 'Pause' : (s.i >= s.n - 1 ? 'Watch again' : 'Play');
        [...$('tut-dots').children].forEach((d, i) => {
            d.setAttribute('aria-current', i === s.i ? 'step' : 'false');
        });
        stopTimer();
        if (s.playing) P.timer = setTimeout(() => act('tick'), G.stepDelay(st.caption));
    }
    function act(action) {
        if (!P.tut) return;
        P.s = G.step(P.s, action);
        render();
    }
    function open(t) {
        P.tut = t;
        P.s = G.player(t.steps.length);
        $('tut-dots').replaceChildren(...t.steps.map((st, i) => {
            const d = el('button', 'tut-dot');
            d.type = 'button';
            d.setAttribute('aria-label', `Step ${i + 1}`);
            d.addEventListener('click', () => act({ go: i }));
            return d;
        }));
        const dlg = $('tut-player');
        render();
        if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
        $('tut-play').focus();
    }
    function close() {
        stopTimer();
        P.tut = null;
        const dlg = $('tut-player');
        if (dlg.open) dlg.close();
    }
    function cards(manifest) {
        const grid = $('learn-tutorials');
        grid.replaceChildren(...manifest.tutorials.map((t) => {
            const card = el('article', 'tut-card');
            card.setAttribute('data-testid', 'learn-tutorial');
            const thumb = el('img', 'tut-thumb');
            thumb.src = BASE + t.steps[0].image;
            thumb.alt = '';
            thumb.loading = 'lazy';
            const btn = el('button', 'btn btn-sm btn-primary', 'Watch');
            btn.type = 'button';
            btn.setAttribute('data-testid', 'learn-watch-' + t.id);
            btn.setAttribute('aria-label', 'Watch: ' + t.title);
            btn.addEventListener('click', () => open(t));
            const foot = el('div', 'tut-foot');
            foot.append(el('span', 'note', `${t.steps.length} steps`), btn);
            card.append(thumb, el('h3', null, t.title), el('p', null, t.summary || ''), foot);
            return card;
        }));
    }
    async function loadTutorials() {
        const note = $('learn-tut-note');
        try {
            const r = await fetch(BASE + 'tutorials.json', { headers: { Accept: 'application/json' } });
            const res = await window.GCSession.readJson(r);
            const problems = r.ok ? G.checkManifest(res.body) : ['HTTP ' + r.status];
            if (problems.length) throw new Error(problems[0]);
            cards(res.body);
        } catch (e) {
            if (note) note.textContent = 'The walk-throughs could not be loaded (' + e.message + ').';
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        if (!$('learn-tutorials') || !G) return;
        document.querySelectorAll('.lc-fig[data-sketch]').forEach(drawSketch);
        $('tut-prev').addEventListener('click', () => act('prev'));
        $('tut-next').addEventListener('click', () => act('next'));
        $('tut-play').addEventListener('click', () => act(P.s && P.s.playing ? 'pause' : 'play'));
        $('tut-close').addEventListener('click', close);
        $('tut-player').addEventListener('close', () => { stopTimer(); P.tut = null; });
        $('tut-player').addEventListener('keydown', (ev) => {
            if (ev.key === 'ArrowRight') { ev.preventDefault(); act('next'); }
            if (ev.key === 'ArrowLeft') { ev.preventDefault(); act('prev'); }
        });
        loadTutorials();
    });
})();
