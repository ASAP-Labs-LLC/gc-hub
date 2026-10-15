/* learn.js (v8.2, v8.3): the Learn page's DOM. learn_logic.js (GCLearn,
   node-tested) has the sketches, the cases, the lesson, their SVG and the
   players' rules.

   * One chart drawing for every sketch: carbon numbers along the top (as
     the hub's charts), time along the bottom, a reference as a grey area,
     lines over it, shaded bands, the difference filled red / blue.
   * Every .lc-fig[data-sketch] draws its fuel: data-sketch (the line),
     data-std (a reference under it), data-bands ("5-11:Lighter,…").
   * Reading a chromatogram (v8.3): a self-paced lesson (GCLearn.lesson), each
     step marking its part of the chart; Back / Next / a dot per step / arrows.
   * What we see most (v8.3): one chart, Diesel #2 as the reference with every
     case over it (GCLearn.patterns / applyPattern); hovering or picking a case
     draws it bold with its band and its difference from #2, and its notes.
   * How to: the lesson first, then the walk-throughs of
     static/learn/tutorials.json (tools/learn_capture.py), checked with
     GCLearn.checkManifest; Watch opens the player, which shows the dark
     screenshots in the dark theme (and switches with it).
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
    function svg(tag, attrs, text) {
        const e = document.createElementNS(NS, tag);
        for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, String(v));
        if (text !== undefined) e.textContent = String(text);
        return e;
    }
    function theme() {
        return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    }

    // ── one chart ───────────────────────────────────────────────────────────
    /** spec: {ref, lines: [{trace, cls}], bands: [{c, label, cls}], diff: {a, b},
        focus: 'axis' | 'ticks' | null, big}. */
    function chart(fig, spec) {
        const W = spec.big ? 640 : 320;
        const H = spec.big ? 250 : 136;
        const box = { x: 8, y: 26, w: W - 16, h: H - 50 };
        const range = G.RANGE;
        const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'lc-svg' + (spec.big ? ' lc-svg-big' : ''),
                               'aria-hidden': 'true', focusable: 'false' });
        for (const b of spec.bands || []) {
            const r = G.bandRect(b.c, box, range);
            if (!r) continue;
            s.append(svg('rect', { x: r.x, y: r.y, width: r.w, height: r.h, class: 'lc-band ' + (b.cls || '') }));
            if (b.label) {
                s.append(svg('text', { x: r.x + r.w / 2, y: box.y + 12, class: 'lc-band-label ' + (b.cls ? b.cls + '-label' : ''),
                                       'text-anchor': 'middle' }, b.label));
            }
        }
        if (spec.ref) s.append(svg('path', { d: G.areaD(spec.ref, box, range), class: 'lc-std' }));
        if (spec.diff) {
            const d = G.diffD(spec.diff.a, spec.diff.b, box, range);
            s.append(svg('path', { d: d.above, class: 'lc-above' }), svg('path', { d: d.below, class: 'lc-below' }));
        }
        for (const ln of spec.lines || []) s.append(svg('path', { d: G.pathD(ln.trace, box, range), class: 'lc-line ' + (ln.cls || '') }));
        // carbon numbers on top, time along the bottom (as the hub's charts)
        const ticksCls = spec.focus === 'ticks' ? ' lc-focus' : '';
        for (const k of G.carbonTicks(range, box, spec.big ? 5 : 10)) {
            s.append(svg('line', { x1: k.x, x2: k.x, y1: box.y - 3, y2: box.y, class: 'lc-axis' + ticksCls }));
            s.append(svg('text', { x: k.x, y: box.y - 7, class: 'lc-tick' + ticksCls, 'text-anchor': 'middle' }, k.label));
        }
        const axisCls = spec.focus === 'axis' ? ' lc-focus' : '';
        const floor = box.y + box.h;
        s.append(svg('line', { x1: box.x, x2: box.x + box.w, y1: floor, y2: floor, class: 'lc-axis' + axisCls }));
        s.append(svg('text', { x: box.x + box.w, y: H - 6, class: 'lc-tick' + axisCls, 'text-anchor': 'end' }, 'time →'));
        fig.replaceChildren(s);
    }
    function bandsOf(spec) {
        return String(spec || '').split(',').map(x => x.trim()).filter(Boolean).map((x) => {
            const m = /^(\d+)-(\d+)(?::(.+))?$/.exec(x);
            return m ? { c: [Number(m[1]), Number(m[2])], label: m[3] || '' } : null;
        }).filter(Boolean);
    }
    function drawSketch(fig) {
        const line = G.sketch(fig.dataset.sketch);
        if (!line) return;
        chart(fig, { ref: fig.dataset.std ? G.sketch(fig.dataset.std) : null, lines: [{ trace: line }],
                     bands: bandsOf(fig.dataset.bands) });
    }
    /** A case applied to the Diesel #2 sketch, as a trace. */
    function caseTrace(id) {
        const ref = G.sketch('diesel2');
        return { x: ref.x, y: G.applyPattern(id, { c: ref.x, y: ref.y }) };
    }
    /** Traces on one scale: the tallest at the top (a case can rise above #2). */
    function fit(traces) {
        const top = traces.reduce((m, t) => t.y.reduce((a, v) => Math.max(a, v), m), 0) || 1;
        return traces.map(t => ({ x: t.x, y: t.y.map(v => v / top) }));
    }

    // ── Reading a chromatogram: the lesson ──────────────────────────────────
    const LS = { steps: null, s: null };
    function drawLesson() {
        const st = LS.steps[LS.s.i];
        const ref = G.sketch('diesel2');
        const spec = { big: true, focus: st.focus === 'solvent' ? null : st.focus, bands: [] };
        if (st.focus === 'solvent') spec.bands.push({ c: [4, 5.2], label: 'Solvent', cls: 'lc-band-on' });
        if (st.band) spec.bands.push({ c: st.band, label: st.label, cls: 'lc-band-on' });
        if (st.sample) {
            const [scaledRef, sample] = fit([ref, caseTrace(st.sample)]);
            spec.ref = scaledRef;
            spec.lines = [{ trace: sample }];
            if (st.diff) spec.diff = { a: scaledRef, b: sample };
        } else {
            spec.lines = [{ trace: G.sketch('withSolvent') }];
        }
        chart($('lesson-chart'), spec);
        $('lesson-count').textContent = `Step ${LS.s.i + 1} of ${LS.s.n}`;
        $('lesson-title').textContent = st.title;
        $('lesson-text').textContent = st.text;
        $('lesson-prev').disabled = LS.s.i === 0;
        $('lesson-next').textContent = LS.s.i >= LS.s.n - 1 ? 'Start over' : 'Next';
        [...$('lesson-dots').children].forEach((d, i) => d.setAttribute('aria-current', i === LS.s.i ? 'step' : 'false'));
    }
    function lessonAct(action) {
        if (action === 'next' && LS.s.i >= LS.s.n - 1) action = { go: 0 };
        LS.s = G.step(LS.s, action);
        drawLesson();
    }
    function initLesson() {
        LS.steps = G.lesson();
        LS.s = G.player(LS.steps.length, false);
        $('lesson-dots').replaceChildren(...LS.steps.map((st, i) => {
            const d = el('button', 'tut-dot');
            d.type = 'button';
            d.setAttribute('aria-label', `Step ${i + 1}: ${st.title}`);
            d.addEventListener('click', () => lessonAct({ go: i }));
            return d;
        }));
        $('lesson-prev').addEventListener('click', () => lessonAct('prev'));
        $('lesson-next').addEventListener('click', () => lessonAct('next'));
        $('lesson').addEventListener('keydown', (ev) => {
            if (ev.key === 'ArrowRight') { ev.preventDefault(); lessonAct('next'); }
            if (ev.key === 'ArrowLeft') { ev.preventDefault(); lessonAct('prev'); }
        });
        drawLesson();
    }

    // ── What we see most: one chart, the cases over Diesel #2 ───────────────
    const CS = { list: null, picked: null, shown: null };
    function drawCases() {
        const id = CS.shown || CS.picked;
        const p = CS.list.find(q => q.id === id);
        const all = fit([G.sketch('diesel2')].concat(CS.list.map(q => caseTrace(q.id))));
        const ref = all[0];
        const traces = CS.list.map((q, k) => ({ id: q.id, trace: all[k + 1] }));
        const on = traces.find(t => t.id === id);
        chart($('cases-chart'), {
            big: true, ref,
            bands: p && p.band ? [{ c: p.band, label: p.label, cls: 'lc-band-on' }] : [],
            diff: on ? { a: ref, b: on.trace } : null,
            lines: traces.filter(t => t.id !== id).map(t => ({ trace: t.trace, cls: 'lc-case' }))
                .concat(on ? [{ trace: on.trace, cls: 'lc-case-on' }] : []),
        });
        [...$('case-list').children].forEach(b => b.setAttribute('aria-pressed', b.dataset.case === CS.picked ? 'true' : 'false'));
        const det = $('case-detail');
        if (!p) { det.replaceChildren(); return; }
        const dl = el('dl');
        for (const [k, v] of [['On the GC', p.see], ['Why it matters', p.why], ['Check', p.check], ['How often', p.often]]) {
            if (v) dl.append(el('dt', null, k), el('dd', null, v));
        }
        det.replaceChildren(el('h3', null, p.name), dl);
    }
    function initCases() {
        CS.list = G.patterns();
        CS.picked = CS.list[0].id;
        $('case-list').replaceChildren(...CS.list.map((p) => {
            const b = el('button', 'case-btn', p.name);
            b.type = 'button';
            b.dataset.case = p.id;
            b.setAttribute('data-testid', 'learn-case');
            b.addEventListener('click', () => { CS.picked = p.id; CS.shown = null; drawCases(); });
            b.addEventListener('mouseenter', () => { CS.shown = p.id; drawCases(); });
            b.addEventListener('mouseleave', () => { CS.shown = null; drawCases(); });
            b.addEventListener('focus', () => { CS.picked = p.id; CS.shown = null; drawCases(); });
            return b;
        }));
        drawCases();
    }

    // ── How to: the lesson and the walk-throughs ────────────────────────────
    const P = { tut: null, s: null, timer: null, manifest: null };
    function stopTimer() { clearTimeout(P.timer); P.timer = null; }
    function render() {
        const t = P.tut;
        const s = P.s;
        const st = t.steps[s.i];
        const shot = G.stepImage(st, theme());
        $('tut-title').textContent = t.title;
        $('tut-count').textContent = `Step ${s.i + 1} of ${s.n}`;
        const img = $('tut-img');
        img.src = BASE + shot.image;
        img.alt = st.caption;
        $('tut-caption').textContent = st.caption;
        const box = $('tut-box');
        if (Array.isArray(shot.box)) {
            box.style.left = (shot.box[0] * 100) + '%';
            box.style.top = (shot.box[1] * 100) + '%';
            box.style.width = (shot.box[2] * 100) + '%';
            box.style.height = (shot.box[3] * 100) + '%';
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
    function lessonCard() {
        const card = el('article', 'tut-card');
        card.setAttribute('data-testid', 'learn-tutorial');
        const fig = el('div', 'lc-fig tut-thumb-fig');
        chart(fig, { ref: G.sketch('diesel2'), lines: [{ trace: caseTrace('gas') }], bands: [{ c: [5, 11], cls: 'lc-band-on' }] });
        const btn = el('button', 'btn btn-sm btn-primary', 'Start');
        btn.type = 'button';
        btn.setAttribute('data-testid', 'learn-watch-lesson');
        btn.setAttribute('aria-label', 'Start the lesson: Read a chromatogram');
        btn.addEventListener('click', () => {
            $('read').scrollIntoView({ behavior: 'smooth', block: 'start' });
            lessonAct({ go: 0 });
            $('lesson-next').focus({ preventScroll: true });
        });
        const foot = el('div', 'tut-foot');
        foot.append(el('span', 'note', `${LS.steps.length} steps`), btn);
        card.append(fig, el('h3', null, 'Read a chromatogram'),
                    el('p', null, 'A short lesson: what each part of the chart tells you.'), foot);
        return card;
    }
    function cards() {
        const m = P.manifest;
        const grid = $('learn-tutorials');
        grid.replaceChildren(lessonCard(), ...(m ? m.tutorials : []).map((t) => {
            const card = el('article', 'tut-card');
            card.setAttribute('data-testid', 'learn-tutorial');
            const thumb = el('img', 'tut-thumb');
            thumb.src = BASE + G.stepImage(t.steps[0], theme()).image;
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
        try {
            const r = await fetch(BASE + 'tutorials.json', { headers: { Accept: 'application/json' } });
            const res = await window.GCSession.readJson(r);
            const problems = r.ok ? G.checkManifest(res.body) : ['HTTP ' + r.status];
            if (problems.length) throw new Error(problems[0]);
            P.manifest = res.body;
            cards();
        } catch (e) {
            cards();
            $('learn-tutorials').append(el('p', 'note', 'The walk-throughs could not be loaded (' + e.message + ').'));
        }
    }
    // the theme changed: the walk-throughs' screenshots follow it
    function onTheme() {
        if (P.manifest) cards();
        if (P.tut) render();
    }

    document.addEventListener('DOMContentLoaded', () => {
        if (!$('learn-tutorials') || !G) return;
        document.querySelectorAll('.lc-fig[data-sketch]').forEach(drawSketch);
        initLesson();
        initCases();
        $('tut-prev').addEventListener('click', () => act('prev'));
        $('tut-next').addEventListener('click', () => act('next'));
        $('tut-play').addEventListener('click', () => act(P.s && P.s.playing ? 'pause' : 'play'));
        $('tut-close').addEventListener('click', close);
        $('tut-player').addEventListener('close', () => { stopTimer(); P.tut = null; });
        $('tut-player').addEventListener('keydown', (ev) => {
            if (ev.key === 'ArrowRight') { ev.preventDefault(); act('next'); }
            if (ev.key === 'ArrowLeft') { ev.preventDefault(); act('prev'); }
        });
        document.addEventListener('gc:theme', onTheme);
        if (typeof MutationObserver !== 'undefined') {
            new MutationObserver(onTheme).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        }
        loadTutorials();
    });
})();
