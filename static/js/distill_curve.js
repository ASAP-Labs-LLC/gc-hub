/* distill_curve.js (v7): the distillation curve on the Samples page's
   Overview — temperature against % recovered, D86 and D2887, a dot at each
   percent point.

   window.GCDistillCurve.mount(host, {legend, onActive}) ->
     {render(rows, {corrected}), message(text), setActive(sel, source),
      clear(source), active(), redraw()}
   rows are the Results card's own rows (SamplesLogic.resultRows), so every
   dot is the number in its Results cell; sel = {series: 'd86'|'d2887',
   label: 'IBP'|'5%'…'FBP'}; onActive(sel|null, source) lets the page mark the
   matching Results cell.

   Pointing at the plot (or tapping it, or dragging across it) picks the
   nearest point (SamplesLogic.curveHit) and shows its temperature in a
   callout by the dot; the dots are one tab stop (a roving tabindex: the
   arrow keys walk them, Home/End, Escape hides the callout), each with an
   accessible name "50%: 285.1 °C, D86 uncorrected". Plain SVG built with
   createElementNS and textContent (no WebGL, no Plotly): colours come from
   the CSS tokens through classes (samples.css), so a theme change needs no
   new colours; the page still redraws on gc:theme and on every resize. */
(function () {
    'use strict';

    const L = window.SamplesLogic;
    const NS = 'http://www.w3.org/2000/svg';
    const DASH = { d86: '', d2887: '6 4' };

    function svgEl(tag, attrs, parent) {
        const el = document.createElementNS(NS, tag);
        for (const k of Object.keys(attrs || {})) {
            if (attrs[k] !== null && attrs[k] !== undefined) el.setAttribute(k, String(attrs[k]));
        }
        if (parent) parent.appendChild(el);
        return el;
    }
    function htmlEl(tag, cls, text) {
        const el = document.createElement(tag);
        if (cls) el.className = cls;
        if (text !== undefined) el.textContent = text;
        return el;
    }
    function r1(v) { return Math.round(v * 10) / 10; }

    /** A short line in the series' style (the legend and the callout). */
    function swatch(id) {
        const s = svgEl('svg', { viewBox: '0 0 28 10', class: 'cv-swatch', 'aria-hidden': 'true', focusable: 'false' });
        svgEl('line', { x1: 1, x2: 27, y1: 5, y2: 5, class: 'cv-line cv-' + id, 'stroke-dasharray': DASH[id] || null }, s);
        svgEl('circle', { cx: 14, cy: 5, r: 3, class: 'cv-dot cv-dot-' + id }, s);
        return s;
    }

    function mount(host, opts) {
        const o = opts || {};
        const S = { rows: null, corrected: false, series: [], scale: null, active: null, source: null,
                    hovering: false, dots: new Map(), anchor: null, msg: null, pending: false };
        host.replaceChildren();
        const wrap = htmlEl('div', 'curve-wrap');
        wrap.setAttribute('data-testid', 'curve-plot');
        const callout = htmlEl('div', 'curve-callout');
        callout.setAttribute('data-testid', 'curve-callout');
        callout.setAttribute('aria-hidden', 'true');
        callout.hidden = true;
        const note = htmlEl('p', 'curve-msg');
        note.setAttribute('data-testid', 'curve-message');
        note.hidden = true;
        host.append(wrap, note);

        const key = (sel) => sel.series + '|' + sel.label;

        function message(text) {
            S.rows = null;
            S.series = [];
            S.msg = text;
            S.sig = null;
            S.svg = null;
            S.dots = new Map();
            wrap.replaceChildren();
            wrap.hidden = true;
            note.textContent = text;
            note.hidden = false;
            if (o.legend) o.legend.replaceChildren();
            setActive(null, null);
        }

        function render(rows, ropts) {
            S.rows = rows || [];
            S.corrected = !!(ropts && ropts.corrected);
            S.series = L.curveSeries(S.rows, S.corrected);
            S.msg = null;
            if (!S.series.some(s => s.points.length)) {
                message('No temperatures are stored for this result.');
                return;
            }
            note.hidden = true;
            wrap.hidden = false;
            renderLegend();
            draw(false);
        }

        function renderLegend() {
            if (!o.legend) return;
            o.legend.replaceChildren(...S.series.filter(s => s.points.length).map((s) => {
                const li = htmlEl('li', 'cv-key');
                li.setAttribute('data-series', s.id);
                li.append(swatch(s.id), htmlEl('span', '', s.name));
                return li;
            }));
        }

        /** Build the plot for the wrap's size. Skipped when the size and the
            numbers are what is drawn already (a resize that changed nothing),
            unless forced (a theme change). */
        function draw(force) {
            if (!S.rows || S.msg) return;
            const width = wrap.clientWidth;
            const height = wrap.clientHeight;
            if (!width || !height) { S.pending = true; return; }
            S.pending = false;
            const sig = width + 'x' + height + '|' + JSON.stringify(S.series);
            if (force !== true && sig === S.sig && S.svg && S.svg.isConnected) return;
            S.sig = sig;
            const hadFocus = wrap.contains(document.activeElement);
            const narrow = width < 360;
            const margin = { l: 46, r: narrow ? 14 : 56, t: 24, b: 46 };
            const sc = L.curveScale(S.series, width, height, margin);
            S.scale = sc;
            const p = sc.plot;
            const names = S.series.filter(s => s.points.length).map(s => s.name).join(' and ');
            const svg = svgEl('svg', {
                class: 'curve-svg', width, height, viewBox: '0 0 ' + width + ' ' + height, role: 'group', focusable: 'false',
                'aria-label': 'Distillation curve: temperature in °C against percent recovered, ' + names +
                    '. Use the arrow keys to move between points.',
            });
            // grid and the y axis (°C)
            const grid = svgEl('g', { class: 'cv-grid', 'aria-hidden': 'true' }, svg);
            sc.yTicks.forEach((t, i) => {
                const y = r1(sc.y(t));
                svgEl('line', { x1: p.left, x2: p.right, y1: y, y2: y, class: 'cv-gridline' }, grid);
                const tx = svgEl('text', { x: p.left - 8, y, class: 'cv-tick', 'text-anchor': 'end', 'dominant-baseline': 'middle' }, grid);
                tx.textContent = L.fmt(t, Number.isInteger(t) ? 0 : 1);
                if (i === sc.yTicks.length - 1) {          // the unit, once, beside the top label
                    const u = svgEl('text', { x: p.left - 4, y: p.top - 13, class: 'cv-tick cv-unit', 'text-anchor': 'end' }, grid);
                    u.textContent = '°C';
                }
            });
            // the x axis: a mark at every point, the labels thinned to fit
            const ax = svgEl('g', { class: 'cv-axis', 'aria-hidden': 'true' }, svg);
            svgEl('line', { x1: p.left, x2: p.right, y1: p.bottom, y2: p.bottom, class: 'cv-baseline' }, ax);
            for (const m of sc.xTicks.marks) {
                const x = r1(sc.x(m));
                svgEl('line', { x1: x, x2: x, y1: p.bottom, y2: p.bottom + 4, class: 'cv-mark' }, ax);
            }
            for (const lab of sc.xTicks.labels) {
                const tx = svgEl('text', { x: r1(sc.x(lab.pct)), y: p.bottom + 17, class: 'cv-tick', 'text-anchor': 'middle' }, ax);
                tx.textContent = lab.text;
            }
            const title = svgEl('text', { x: r1((p.left + p.right) / 2), y: p.bottom + 37, class: 'cv-title', 'text-anchor': 'middle' }, ax);
            title.textContent = '% recovered';
            // the guides to both axes, shown with the callout
            const guides = svgEl('g', { class: 'cv-guides', 'aria-hidden': 'true' }, svg);
            S.gv = svgEl('line', { class: 'cv-guide' }, guides);
            S.gh = svgEl('line', { class: 'cv-guide' }, guides);
            // the lines: D2887 under D86
            const order = S.series.slice().reverse();
            for (const s of order) {
                if (!s.points.length) continue;
                const d = s.points.map((pt, i) => (i ? 'L' : 'M') + r1(sc.x(pt.pct)) + ' ' + r1(sc.y(pt.t))).join(' ');
                svgEl('path', { d, class: 'cv-line cv-' + s.id, 'stroke-dasharray': DASH[s.id] || null, 'aria-hidden': 'true' }, svg);
            }
            // direct labels at the FBP end, nudged apart (not on a narrow plot: the legend names them)
            if (!narrow) {
                const ends = S.series.filter(s => s.points.length).map(s => {
                    const last = s.points[s.points.length - 1];
                    return { s, x: sc.x(last.pct), y: sc.y(last.t) };
                }).sort((a, b) => a.y - b.y);
                for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
                for (const e of ends) {
                    const tx = svgEl('text', { x: r1(e.x + 9), y: r1(e.y), class: 'cv-end', 'dominant-baseline': 'middle', 'aria-hidden': 'true' }, svg);
                    tx.textContent = e.s.short;
                }
            }
            // the dots: focusable, one tab stop between them
            S.dots = new Map();
            const dots = svgEl('g', { class: 'cv-dots' }, svg);
            for (const s of order) {
                for (const pt of s.points) {
                    const sel = { series: s.id, label: pt.label };
                    const c = svgEl('circle', {
                        cx: r1(sc.x(pt.pct)), cy: r1(sc.y(pt.t)), r: 4, class: 'cv-dot cv-dot-' + s.id, tabindex: -1,
                        role: 'img', 'aria-label': L.curvePointLabel(s, pt), 'data-series': s.id, 'data-label': pt.label,
                        'data-testid': 'curve-dot',
                    }, dots);
                    c.addEventListener('focus', () => setActive(sel, 'focus'));
                    S.dots.set(key(sel), c);
                }
            }
            // the pointer's layer: the whole plot (and a little round it) picks the nearest point
            const pad = 12;
            const hit = svgEl('rect', { x: p.left - pad, y: p.top - pad, width: p.right - p.left + 2 * pad,
                                        height: p.bottom - p.top + 2 * pad, class: 'cv-hit', 'data-testid': 'curve-hit' }, svg);
            hit.addEventListener('pointermove', onPointer);
            hit.addEventListener('pointerdown', (ev) => {
                ev.preventDefault();                      // a press shows a point; it never focuses the plot itself
                onPointer(ev);
            });
            hit.addEventListener('pointerleave', (ev) => {
                if (ev.pointerType === 'touch') return;           // a tap keeps its point
                S.hovering = false;
                if (S.source === 'pointer') setActive(null, null);
            });
            svg.addEventListener('keydown', onKey);
            svg.addEventListener('focusout', (ev) => {
                if (S.svg !== svg || S.redrawing) return;        // this plot is being replaced: its point stays
                if (!svg.contains(ev.relatedTarget) && S.source === 'focus') setActive(null, null);
            });
            S.redrawing = true;
            S.svg = svg;
            wrap.replaceChildren(svg, callout);
            if (S.active && !L.curveFind(S.series, S.active)) S.active = null;
            S.anchor = null;
            paint();
            if (hadFocus) {
                const el = S.dots.get(key(S.active || L.curveStep(S.series, null, 'Home')));
                if (el) el.focus({ preventScroll: true });
            }
            S.redrawing = false;
        }

        function onPointer(ev) {
            if (!S.scale || !S.svg) return;
            const box = S.svg.getBoundingClientRect();
            const sel = L.curveHit(S.series, S.scale, ev.clientX - box.left, ev.clientY - box.top);
            if (!sel) return;
            if (ev.type === 'pointermove' && ev.pointerType === 'touch' && !S.active) return;
            S.hovering = ev.pointerType !== 'touch';
            setActive(sel, ev.pointerType === 'touch' ? 'tap' : 'pointer');
        }

        function onKey(ev) {
            if (ev.key === 'Escape') {
                if (S.active) { ev.stopPropagation(); setActive(null, null); }
                return;
            }
            if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(ev.key)) return;
            ev.preventDefault();
            ev.stopPropagation();                         // the page's ↑/↓ step through the samples list: not from here
            const cur = S.active || (document.activeElement && document.activeElement.dataset
                ? { series: document.activeElement.dataset.series, label: document.activeElement.dataset.label } : null);
            const next = L.curveStep(S.series, cur, ev.key);
            const el = next && S.dots.get(key(next));
            if (!el) return;
            if (document.activeElement === el) setActive(next, 'focus');
            else el.focus({ preventScroll: true });
        }

        /** Mark the active dot (and its partner at the same %), draw the
            guides, place the callout; tell the page. */
        function setActive(sel, source) {
            const found = sel ? L.curveFind(S.series, sel) : null;
            const before = S.active ? key(S.active) : null;
            S.active = found ? { series: found.series.id, label: found.point.label } : null;
            S.source = found ? source : null;
            paint();
            const now = S.active ? key(S.active) : null;
            if (o.onActive && (before !== now || source === 'focus')) o.onActive(S.active, S.source);
        }

        function paint() {
            // the roving tab stop: the active dot, else the first D86 point
            const anchorSel = S.active || L.curveStep(S.series, null, 'Home');
            const anchorKey = anchorSel ? key(anchorSel) : null;
            for (const [k, el] of S.dots) {
                el.setAttribute('tabindex', k === anchorKey ? '0' : '-1');
                const on = !!S.active && k === key(S.active);
                const peer = !!S.active && !on && el.getAttribute('data-label') === S.active.label;
                el.classList.toggle('is-active', on);
                el.classList.toggle('is-peer', peer);
                el.setAttribute('r', on ? '6' : peer ? '5' : '4');
            }
            if (!S.svg) return;
            S.svg.classList.toggle('has-active', !!S.active);
            const found = S.active ? L.curveFind(S.series, S.active) : null;
            if (!found || !S.scale) {
                callout.hidden = true;
                if (S.gv) { S.gv.setAttribute('visibility', 'hidden'); S.gh.setAttribute('visibility', 'hidden'); }
                return;
            }
            const sc = S.scale;
            const px = r1(sc.x(found.point.pct));
            const py = r1(sc.y(found.point.t));
            S.gv.setAttribute('x1', px); S.gv.setAttribute('x2', px);
            S.gv.setAttribute('y1', py); S.gv.setAttribute('y2', sc.plot.bottom);
            S.gh.setAttribute('x1', sc.plot.left); S.gh.setAttribute('x2', px);
            S.gh.setAttribute('y1', py); S.gh.setAttribute('y2', py);
            S.gv.setAttribute('visibility', 'visible');
            S.gh.setAttribute('visibility', 'visible');
            fillCallout(found.series, found.point);
            callout.hidden = false;
            const place = L.calloutPlace(px, py, callout.offsetWidth, callout.offsetHeight, wrap.clientWidth, wrap.clientHeight, 12);
            callout.style.left = place.left + 'px';
            callout.style.top = place.top + 'px';
            callout.setAttribute('data-side', place.side);
        }

        function fillCallout(series, pt) {
            const at = htmlEl('div', 'cc-at', pt.label === 'IBP' || pt.label === 'FBP' ? pt.label : pt.label + ' recovered');
            const temp = htmlEl('div', 'cc-temp', L.tempText(pt.t));
            temp.setAttribute('data-testid', 'curve-callout-temp');
            const what = htmlEl('div', 'cc-what');
            what.append(swatch(series.id), htmlEl('span', '', series.name));
            const parts = [at, temp, what];
            if (pt.note) parts.push(htmlEl('div', 'cc-note', pt.note));
            callout.replaceChildren(...parts);
            callout.setAttribute('data-series', series.id);
            callout.setAttribute('data-label', pt.label);
        }

        function clear(source) {
            if (!source || S.source === source) setActive(null, null);
        }

        if (window.ResizeObserver) {
            let frame = 0;
            new ResizeObserver(() => {
                cancelAnimationFrame(frame);
                frame = requestAnimationFrame(() => draw(false));
            }).observe(wrap);
        }
        // a tap anywhere else puts a tapped point away
        document.addEventListener('pointerdown', (ev) => {
            if (S.source === 'tap' && !wrap.contains(ev.target) && !(o.keepFor && o.keepFor(ev.target))) setActive(null, null);
        });

        return {
            render, message, setActive, clear, redraw: () => draw(true),
            active: () => (S.active ? Object.assign({}, S.active) : null),
            pending: () => S.pending,
        };
    }

    window.GCDistillCurve = { mount, swatch };
})();
