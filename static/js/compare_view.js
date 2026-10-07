/* v5.0.0 lane C: the Compare view (window.GCCompare).

   GCCompare.mount(el, {sample, standards, settings, onUrlChange})
       -> {unmount(), setStandard(name), standard(), params(), queueItem(),
           addToQueue(), openExport()}

   - sample:    a /api/files row ({sample_id, lab_id, name, best_fit, ...});
   - standards: /api/comparison-standards ([{name, path}]);
   - settings:  /api/settings (the saved analysis defaults, bestfit_enabled);
   - onUrlChange({standard}) is called when the standard changes here (the
     default resolving, or a pick), so the page can keep ?standard= current.

   It owns the standard picker (the best fit by default; a manual pick is
   remembered per sample in localStorage), the Trend and Difference graphs
   (Plotly, a monochrome template read from the tokens, range bands,
   drag-zoom, double-click to reset, fullscreen, Annotate), Findings (the
   /api/analysis items as rows, the sentence always the server's), the
   Conclusion (read-only until Edit; v6: conclusion presets are inserted into
   it, and the sample's earlier notes, comments from before v6, are listed
   under it; there is no separate Comments section) and the Adjust
   drawer (it dispatches `gc:adjust` {open} on document; the page hides its
   list while it is open). Every change recomputes on the hub: there is no
   Run button, and the numbers are never computed here.

   Also: GCCompare.addToQueue({sample, standards, settings}) and
   GCCompare.openExportSheet({...}) for the page header's actions (they use
   the mounted view's parameters for that sample, else the saved defaults),
   and GCCompare.defaultStandard(sample, standards) for bulk actions.

   Needs: session.js, shell.js (GCShell), compare_logic.js, report_payload.js,
   ladder.js is not needed; comments.js (marked regions and earlier notes)
   and Plotly are used when present.
   Text only (textContent); Plotly strings are escaped. */
(function (root) {
    'use strict';

    const L = root.GCCompareLogic;
    const PICKS_KEY = 'gc.compare.picks';
    const mounted = new Set();

    // ── small helpers ───────────────────────────────────────────────────────
    function h(tag, props, ...children) {
        const el = document.createElement(tag);
        for (const [k, v] of Object.entries(props || {})) {
            if (v === null || v === undefined || v === false) continue;
            if (k === 'className') el.className = v;
            else if (k === 'text') el.textContent = String(v);
            else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
            else if (k === 'value') el.value = v;
            else el.setAttribute(k, v === true ? '' : String(v));
        }
        for (const c of children.flat()) {
            if (c === null || c === undefined || c === false) continue;
            el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
        }
        return el;
    }
    /** A 16px stroke icon (no text node, so a button's textContent is its label). */
    function svgIcon(name) {
        const NS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(NS, 'svg');
        svg.setAttribute('viewBox', '0 0 16 16');
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('class', 'ico-svg');
        const path = document.createElementNS(NS, 'path');
        path.setAttribute('d', name === 'check' ? 'M3.5 8.5l3 3 6-7' : 'M8 3.5v9M3.5 8h9');
        svg.appendChild(path);
        return svg;
    }
    function safe(s) {           // Plotly renders pseudo-HTML in labels and names
        return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
    function toast(msg, kind) { if (root.GCShell && root.GCShell.toast) root.GCShell.toast(msg, kind); }
    function labelOf(sample) {
        return sample ? String(sample.lab_id || sample.name || ('sample ' + sample.sample_id)) : '';
    }
    function loadPicks() {
        try { return L.parsePicks(root.localStorage.getItem(PICKS_KEY)); } catch (_e) { return []; }
    }
    function savePick(sampleId, name) {
        try { root.localStorage.setItem(PICKS_KEY, JSON.stringify(L.rememberPick(loadPicks(), sampleId, name))); }
        catch (_e) { /* private mode: not remembered */ }
    }
    async function postJson(url, body) {
        const r = await fetch(url, { method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify(body || {}) });
        const res = await root.GCSession.readJson(r);
        return { ok: r.ok, status: r.status, body: res.body || {} };
    }
    function bestFitEnabled(settings) {
        return String((settings && settings.bestfit_enabled) || 'true').toLowerCase() === 'true';
    }
    function tokens() {
        const cs = getComputedStyle(document.documentElement);
        const v = (n, d) => (cs.getPropertyValue(n) || '').trim() || d;
        return {
            ink: v('--chart-ink', '#0f172a'), ref: v('--chart-ref', '#a8b0bc'),
            grid: v('--chart-grid', '#eef0f3'), axis: v('--chart-axis', '#64748b'),
            band: v('--chart-band', 'rgba(15,23,42,0.045)'), tick: v('--chart-tick', '#cbd2da'),
            above: v('--dev-above', '#dc2626'), aboveFill: v('--dev-above-fill', '#fecaca'),
            below: v('--dev-below', '#2563eb'), belowFill: v('--dev-below-fill', '#bfdbfe'),
            card: v('--bg-card', '#ffffff'), text: v('--text', '#0f172a'),
            bandText: v('--text-muted-sunken', '#5b6678'),     // AA over a band (test_ui_contrast)
            font: v('--font', 'system-ui, sans-serif'),
        };
    }
    const PLOT_CONFIG = { displayModeBar: false, responsive: true, displaylogo: false,
                          doubleClick: 'reset', scrollZoom: false };

    // ── one mounted view ────────────────────────────────────────────────────
    function mount(el, opts) {
        if (!el) throw new Error('GCCompare.mount: no element');
        const o = opts || {};
        const v = {
            el, sample: o.sample || {}, standards: (o.standards || []).slice(),
            settings: o.settings || {}, onUrlChange: typeof o.onUrlChange === 'function' ? o.onUrlChange : null,
            standard: null, source: null,
            params: null, overlays: null, draft: null,
            result: null, bestFit: null, seq: 0, timer: null, alive: true,
            edited: {},              // standard -> the operator's conclusion
            editing: false, annotating: false, drawerOpen: false,
            cleanups: [],
        };
        v.params = L.defaultParams(v.settings);
        v.overlays = (root.overlaysFromSettings ? root.overlaysFromSettings(v.settings) : []);
        v.draft = L.draftFrom(v.params, v.overlays);
        const sid = v.sample.sample_id;

        build(v);
        mounted.add(v);

        // the standard: explicit (lane S calls setStandard) > remembered > best fit
        const remembered = L.recallPick(loadPicks(), sid);
        const first = L.pickStandard({ remembered, sampleBestFit: v.sample.best_fit, standards: v.standards });
        if (!first) {
            renderEmpty(v, 'No comparison standards yet. An admin adds one from a sample (Settings, Comparison standards).');
        } else if (first.source === 'remembered' || !bestFitEnabled(v.settings)) {
            choose(v, first.name, first.source, true);
        } else {
            // wait (briefly) for the best fit, so the first analysis is the right one
            v.standard = null;
            setStatus(v, 'Finding the best-fitting standard…');
            const fallback = setTimeout(() => { if (!v.standard && v.alive) choose(v, first.name, first.source, true); }, 4000);
            fetchBestFit(v).then((best) => {
                clearTimeout(fallback);
                if (!v.alive) return;
                const pick = L.pickStandard({ explicit: v.source === 'explicit' ? v.standard : null,
                    remembered, best, sampleBestFit: v.sample.best_fit, standards: v.standards });
                if (v.source === 'explicit' || v.source === 'pick') return;     // chosen meanwhile
                if (pick && pick.name !== v.standard) choose(v, pick.name, pick.source, true);
            });
        }
        if (first && first.source === 'remembered' && bestFitEnabled(v.settings)) fetchBestFit(v);

        const handle = {
            unmount: () => unmount(v),
            setStandard: (name) => {
                if (!v.standards.some(s => s.name === name)) return false;
                if (name === v.standard) { v.source = 'explicit'; return true; }
                choose(v, name, 'explicit', false);
                return true;
            },
            standard: () => v.standard,
            params: () => Object.assign({}, v.params),
            queueItem: () => queueItem(v),
            addToQueue: () => addViewToQueue(v),
            openExport: () => openExport(v),
        };
        v.handle = handle;
        return handle;
    }

    function unmount(v) {
        if (!v.alive) return;
        v.alive = false;
        clearTimeout(v.timer);
        if (v.drawerOpen) setDrawer(v, false);
        for (const fn of v.cleanups) { try { fn(); } catch (_e) { /* already gone */ } }
        if (root.Plotly) {
            for (const d of [v.dom.trend, v.dom.diff]) { try { root.Plotly.purge(d); } catch (_e) { /* */ } }
        }
        if (v.dom.drawer && v.dom.drawer.parentNode) v.dom.drawer.remove();
        v.el.textContent = '';
        mounted.delete(v);
    }

    async function fetchBestFit(v) {
        if (!bestFitEnabled(v.settings)) return null;
        try {
            const r = await postJson('/api/best-fit', { sample_id: v.sample.sample_id });
            if (!r.ok || !v.alive) return null;
            v.bestFit = r.body;
            renderBestFit(v);
            return r.body.best_standard || null;
        } catch (_e) { return null; }
    }

    function choose(v, name, source, notify) {
        const changed = name !== v.standard;
        v.standard = name;
        v.source = source;
        if (source === 'pick') savePick(v.sample.sample_id, name);
        if (v.dom.picker.value !== name) v.dom.picker.value = name;
        v.dom.legendStd.textContent = name;
        renderBestFit(v);
        if (changed && notify && v.onUrlChange) {
            try { v.onUrlChange({ standard: name, source }); } catch (e) { console.error(e); }
        }
        if (changed) {
            v.editing = false;
            renderConclusion(v);
            analyse(v, 0);
        }
    }

    // ── the analysis (the hub computes; we draw) ────────────────────────────
    function analyse(v, delay) {
        clearTimeout(v.timer);
        if (!v.standard) return;
        v.timer = setTimeout(() => run(v), delay == null ? 450 : delay);
    }

    async function run(v) {
        if (!v.alive || !v.standard) return;
        const seq = ++v.seq;
        const body = L.analysisBody(v.sample.sample_id, v.standard, v.params, v.overlays);
        v.el.classList.add('is-loading');
        setStatus(v, 'Updating…');
        let r;
        try { r = await postJson('/api/analysis', body); }
        catch (e) { r = { ok: false, status: 0, body: { error: e.message } }; }
        if (!v.alive || seq !== v.seq) return;             // a newer request won
        v.el.classList.remove('is-loading');
        if (!r.ok) {
            setStatus(v, 'Could not compare: ' + (r.body.error || ('HTTP ' + r.status)), true);
            return;
        }
        v.result = r.body;
        setStatus(v, '');
        draw(v);
        renderFindings(v);
        renderConclusion(v);
    }

    function setStatus(v, text, isError) {
        const s = v.dom.status;
        s.textContent = text || '';
        s.hidden = !text;
        s.classList.toggle('err', !!isError);
        s.setAttribute('role', isError ? 'alert' : 'status');
    }

    // ── DOM ─────────────────────────────────────────────────────────────────
    function iconBtn(label, cls, testid, onclick) {
        return h('button', { type: 'button', className: 'icon-btn cmp-icon ' + cls, 'aria-label': label,
                             title: label, 'data-testid': testid, onclick },
            h('span', { className: 'ico ' + cls + '-ico', 'aria-hidden': 'true' }));
    }

    function build(v) {
        const d = {};
        v.dom = d;
        const lab = labelOf(v.sample);

        d.picker = h('select', { id: 'cmp-standard-' + v.sample.sample_id, 'data-testid': 'compare-standard',
                                 onchange: () => choose(v, d.picker.value, 'pick', true) },
            v.standards.map(s => h('option', { value: s.name, text: s.name })));
        d.bestfit = h('span', { className: 'caption cmp-bestfit', 'data-testid': 'compare-bestfit' });
        d.legendStd = h('span', { text: '' });
        d.adjustBtn = h('button', { type: 'button', className: 'btn btn-sm', 'data-testid': 'compare-adjust-toggle',
                                    'aria-expanded': 'false', onclick: () => setDrawer(v, !v.drawerOpen) },
            h('span', { className: 'ico cmp-sliders-ico', 'aria-hidden': 'true' }), 'Adjust');

        const toolbar = h('div', { className: 'cmp-toolbar' },
            h('label', { className: 'cmp-picker' }, h('span', { text: 'Compared with' }), d.picker),
            d.bestfit,
            h('span', { className: 'spacer' }),
            h('span', { className: 'cmp-legend', 'aria-hidden': 'true' },
                h('i', { className: 'sw sw-sample' }), h('span', { text: lab }),
                h('i', { className: 'sw sw-std' }), d.legendStd),
            d.adjustBtn);

        // Trend
        d.trend = h('div', { className: 'cmp-plot cmp-plot-trend', 'data-testid': 'compare-trend',
                             role: 'img', 'aria-label': 'Trend: the sample over the standard' });
        d.annotBtn = h('button', { type: 'button', className: 'btn btn-sm btn-ghost', 'aria-pressed': 'false',
                                   'data-testid': 'compare-annotate', onclick: () => setAnnotate(v, !v.annotating) },
            h('span', { className: 'ico cmp-pen-ico', 'aria-hidden': 'true' }), 'Annotate');
        d.annotMenuBtn = h('button', { type: 'button', className: 'icon-btn cmp-icon', 'aria-haspopup': 'menu',
                                       'aria-expanded': 'false', 'aria-label': 'More annotation actions',
                                       'data-testid': 'compare-annotate-menu',
                                       onclick: () => toggleAnnotMenu(v) },
            h('span', { className: 'ico cmp-caret-ico', 'aria-hidden': 'true' }));
        d.annotCount = h('span', { id: 'annotation-count', className: 'visually-hidden' });
        d.clearAnnot = h('button', { type: 'button', role: 'menuitem', className: 'menu-item',
                                     'data-testid': 'compare-clear-annotations', onclick: () => clearAnnotations(v) },
            'Clear annotations…');
        d.regionItems = h('div', { className: 'cmp-region-items', 'data-testid': 'compare-regions' });
        d.annotMenu = h('div', { className: 'menu cmp-menu', role: 'menu', hidden: true },
            d.regionItems, d.clearAnnot);
        d.annotHint = h('p', { className: 'caption cmp-annot-hint', hidden: true,
                               text: 'Drag across the trend to mark a region. Esc cancels.' });
        d.popRegion = h('p', { className: 'cmp-pop-region' });
        d.popInput = h('input', { type: 'text', maxlength: '500', 'aria-label': 'Label for this region',
                                  placeholder: 'What is here? (optional)', 'data-testid': 'compare-annotation-text' });
        d.pop = h('form', { className: 'cmp-pop', hidden: true, 'data-testid': 'compare-annotation-pop' },
            d.popRegion, d.popInput,
            h('div', { className: 'row' },
                h('span', { className: 'spacer' }),
                h('button', { type: 'button', className: 'btn btn-sm btn-ghost', text: 'Cancel',
                              onclick: () => closePop(v) }),
                h('button', { type: 'submit', className: 'btn btn-sm btn-primary', text: 'Save',
                              'data-testid': 'compare-annotation-save' })));
        d.pop.addEventListener('submit', (e) => { e.preventDefault(); savePop(v); });
        d.status = h('p', { className: 'cmp-status caption', hidden: true, role: 'status' });

        d.trendCard = h('section', { className: 'cmp-chart', 'aria-labelledby': 'cmp-trend-h' },
            h('div', { className: 'cmp-chart-head' },
                h('h3', { id: 'cmp-trend-h', text: 'Trend' }),
                h('span', { className: 'spacer' }),
                h('div', { className: 'cmp-annot' }, d.annotBtn, d.annotMenuBtn, d.annotMenu, d.annotCount),
                iconBtn('Show the trend full screen', 'cmp-full', 'compare-fullscreen-trend',
                        () => fullscreen(v, d.trendCard))),
            d.annotHint, d.status, d.trend, d.pop);

        d.diff = h('div', { className: 'cmp-plot cmp-plot-diff', 'data-testid': 'compare-diff',
                            role: 'img', 'aria-label': 'Difference: sample minus standard' });
        d.diffCard = h('section', { className: 'cmp-chart', 'aria-labelledby': 'cmp-diff-h' },
            h('div', { className: 'cmp-chart-head' },
                h('h3', { id: 'cmp-diff-h', text: 'Difference' }),
                h('span', { className: 'spacer' }),
                h('span', { className: 'cmp-key', 'aria-hidden': 'true' },
                    h('i', { className: 'sw sw-above' }), 'Sample higher',
                    h('i', { className: 'sw sw-below' }), 'Sample lower',
                    h('i', { className: 'sw sw-spike' }), 'Counted spikes'),
                iconBtn('Show the difference full screen', 'cmp-full', 'compare-fullscreen-diff',
                        () => fullscreen(v, d.diffCard))),
            d.diff);

        // Findings, Conclusion (with its presets and the earlier notes)
        d.findCount = h('span', { className: 'caption' });
        d.findings = h('ul', { className: 'cmp-findings', 'data-testid': 'compare-findings-list' });
        const findSec = h('section', { className: 'cmp-sec', 'data-testid': 'compare-findings',
                                       'aria-labelledby': 'cmp-find-h' },
            h('div', { className: 'cmp-sec-head' }, h('h3', { id: 'cmp-find-h', text: 'Findings' }), d.findCount),
            d.findings);

        d.conclText = h('p', { className: 'cmp-concl-text', 'data-testid': 'compare-conclusion-text' });
        d.conclNote = h('p', { className: 'caption cmp-concl-note', hidden: true });
        d.conclEdit = h('button', { type: 'button', className: 'btn btn-sm btn-ghost', text: 'Edit',
                                    'data-testid': 'compare-conclusion-edit', onclick: () => editConclusion(v) });
        d.conclUse = h('button', { type: 'button', className: 'btn btn-sm btn-ghost', hidden: true,
                                   text: 'Use the generated text', onclick: () => {
                                       delete v.edited[v.standard]; renderConclusion(v); } });
        d.conclArea = h('textarea', { rows: '5', maxlength: String(L.CONCLUSION_MAX), 'aria-label': 'Conclusion',
                                      'aria-describedby': 'cmp-concl-count',
                                      'data-testid': 'compare-conclusion-input' });
        // the limit (v6): a live counter, typing stops at maxlength, a paste is cut with a notice
        d.conclCount = h('span', { className: 'caption cmp-concl-count', id: 'cmp-concl-count',
                                   'aria-live': 'polite', 'data-testid': 'compare-conclusion-count' });
        d.conclCut = h('p', { className: 'caption cmp-concl-cut', role: 'status', hidden: true,
                              'data-testid': 'compare-conclusion-cut' });
        d.conclArea.addEventListener('input', () => { d.conclCut.hidden = true; syncCount(v); });
        d.conclArea.addEventListener('paste', (e) => pasteConclusion(v, e));
        d.conclForm = h('div', { className: 'cmp-concl-form', hidden: true },
            d.conclArea, d.conclCut,
            h('div', { className: 'row' },
                d.conclCount,
                h('span', { className: 'spacer' }),
                h('button', { type: 'button', className: 'btn btn-sm btn-ghost', text: 'Cancel',
                              onclick: () => { v.editing = false; renderConclusion(v); } }),
                h('button', { type: 'button', className: 'btn btn-sm btn-primary', text: 'Save',
                              'data-testid': 'compare-conclusion-save', onclick: () => saveConclusion(v) })));
        // conclusion presets (v6.0.1): a tray of chips under the editor, never a
        // menu over the text it edits. The header's "Add preset" opens the editor
        // on the tray; a chip adds its text at the cursor (Undo takes it back), and
        // a preset already in the conclusion shows a check.
        d.presetBtn = h('button', { type: 'button', className: 'btn btn-sm', hidden: true,
                                    'data-testid': 'compare-preset-menu', onclick: () => openPresets(v) },
            svgIcon('plus'), 'Add preset');
        d.presetFilter = h('input', { type: 'search', className: 'cmp-presets-filter', hidden: true,
                                      placeholder: 'Filter presets', 'aria-label': 'Filter presets',
                                      oninput: () => renderPresetChips(v) });
        d.presetStatus = h('span', { className: 'caption cmp-presets-status', role: 'status', 'aria-live': 'polite' });
        d.presetList = h('div', { className: 'cmp-presets-list' });
        d.presetEmpty = h('p', { className: 'caption cmp-presets-empty', hidden: true });
        d.presetTray = h('div', { className: 'cmp-presets', role: 'group', hidden: true,
                                  'aria-labelledby': 'cmp-presets-h', 'data-testid': 'compare-presets' },
            h('div', { className: 'cmp-presets-head' },
                h('span', { className: 'cmp-presets-title', id: 'cmp-presets-h', text: 'Presets' }),
                d.presetStatus, h('span', { className: 'spacer' }), d.presetFilter),
            d.presetList, d.presetEmpty);
        d.conclForm.insertBefore(d.presetTray, d.conclForm.lastChild);
        // the cursor is kept across a click on a chip (the click blurs the editor)
        const keepSel = () => { v.presetSel = { start: d.conclArea.selectionStart, end: d.conclArea.selectionEnd }; };
        for (const ev of ['keyup', 'mouseup', 'select', 'blur']) d.conclArea.addEventListener(ev, keepSel);
        d.conclArea.addEventListener('input', () => { keepSel(); renderPresetChips(v); });
        // the sample's earlier notes (comments from before v6): read-only
        d.notesList = h('ul', { className: 'cmp-notes-list', 'data-testid': 'compare-notes-list' });
        d.notes = h('div', { className: 'cmp-notes', hidden: true, 'data-testid': 'compare-notes' },
            h('h4', { className: 'cmp-notes-h', text: 'Notes' }),
            h('p', { className: 'caption cmp-notes-intro',
                     text: 'Earlier comments on this sample. ' +
                           'They print under the conclusion on the report.' }),
            d.notesList);
        const conclSec = h('section', { className: 'cmp-sec', 'data-testid': 'compare-conclusion',
                                        'aria-labelledby': 'cmp-concl-h' },
            h('div', { className: 'cmp-sec-head' }, h('h3', { id: 'cmp-concl-h', text: 'Conclusion' }),
                h('span', { className: 'spacer' }), d.conclUse,
                d.presetBtn, d.conclEdit),
            d.conclText, d.conclNote, d.conclForm, d.notes);

        const main = h('div', { className: 'cmp-main' }, d.trendCard, d.diffCard);
        const side = h('div', { className: 'cmp-side' }, findSec, conclSec);
        const wrap = h('div', { className: 'cmp', 'data-testid': 'compare', 'data-sample': String(v.sample.sample_id) },
            toolbar, h('div', { className: 'cmp-body' }, main, side));

        d.drawer = buildDrawer(v);
        v.el.textContent = '';
        v.el.appendChild(wrap);
        document.body.appendChild(d.drawer);
        renderConclusion(v);
        renderFindings(v);

        // the sample's comments: marked regions (drawn) and earlier notes (listed)
        if (root.Comments && typeof root.Comments.init === 'function') {
            root.Comments.init({ onChange: () => {
                if (!v.alive) return;
                drawAnnotations(v); syncAnnotCount(v); renderNotes(v);
            } });
            root.Comments.setSample(v.sample.sample_id);
        }
        loadPresets(v);

        // re-theme: GCTheme's event, and the attribute itself when it is absent
        const onTheme = () => { if (v.alive && v.result) draw(v); };
        document.addEventListener('gc:theme', onTheme);
        v.cleanups.push(() => document.removeEventListener('gc:theme', onTheme));
        if (!root.GCTheme && typeof MutationObserver !== 'undefined') {
            const mo = new MutationObserver(onTheme);
            mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
            v.cleanups.push(() => mo.disconnect());
        }
        if (typeof ResizeObserver !== 'undefined' && root.Plotly) {
            const ro = new ResizeObserver(() => {
                for (const p of [d.trend, d.diff]) {
                    if (p._fullLayout) { try { root.Plotly.Plots.resize(p); } catch (_e) { /* */ } }
                }
            });
            ro.observe(d.trend);
            ro.observe(d.diff);
            v.cleanups.push(() => ro.disconnect());
        }
        const onKey = (e) => {
            if (e.key !== 'Escape') return;
            if (!d.pop.hidden) { closePop(v); return; }
            if (v.annotating) { setAnnotate(v, false); return; }
            if (!d.annotMenu.hidden) { toggleAnnotMenu(v, false); return; }
            if (v.drawerOpen && !document.querySelector('dialog[open]')) setDrawer(v, false);
        };
        document.addEventListener('keydown', onKey);
        v.cleanups.push(() => document.removeEventListener('keydown', onKey));
        const onDocClick = (e) => {
            if (!d.annotMenu.hidden && !d.annotMenu.contains(e.target) && !d.annotMenuBtn.contains(e.target)) {
                toggleAnnotMenu(v, false);
            }
        };
        document.addEventListener('click', onDocClick, true);
        v.cleanups.push(() => document.removeEventListener('click', onDocClick, true));
        const onFs = () => {
            for (const p of [d.trend, d.diff]) {
                if (root.Plotly && p._fullLayout) { try { root.Plotly.Plots.resize(p); } catch (_e) { /* */ } }
            }
        };
        document.addEventListener('fullscreenchange', onFs);
        v.cleanups.push(() => document.removeEventListener('fullscreenchange', onFs));
    }

    function renderEmpty(v, text) {
        setStatus(v, text, false);
        v.dom.picker.disabled = true;
    }

    function renderBestFit(v) {
        const b = v.bestFit;
        const el = v.dom.bestfit;
        if (!b || !b.label) { el.textContent = ''; return; }
        const score = Number.isFinite(Number(b.score)) ? ` (${Number(b.score).toFixed(2)})` : '';
        el.textContent = `Best fit is ${b.label}${score}`;
    }

    // ── charts ──────────────────────────────────────────────────────────────
    function baseLayout(c, extra) {
        const axis = {
            gridcolor: c.grid, linecolor: c.tick, tickcolor: c.tick, zeroline: false,
            tickfont: { color: c.axis, size: 11 }, showline: true, ticks: 'outside', ticklen: 3,
        };
        return Object.assign({
            paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
            font: { family: c.font, color: c.axis, size: 11 },
            margin: { l: 52, r: 12, t: 24, b: 34 }, showlegend: false, dragmode: 'zoom',
            hovermode: 'x', hoverlabel: { bgcolor: c.card, bordercolor: c.tick, font: { color: c.text, family: c.font } },
            xaxis: Object.assign({}, axis, { title: { text: 'Retention time (min)', font: { size: 10, color: c.axis }, standoff: 4 } }),
            yaxis: Object.assign({}, axis, { tickformat: '~s' }),
        }, extra || {});
    }

    function bandShapes(c, windows, withLabels) {
        const shapes = [];
        const labels = [];
        for (const w of windows || []) {
            if (!w.evaluable) continue;
            shapes.push({ type: 'rect', x0: w.t0, x1: w.t1, y0: 0, y1: 1, yref: 'paper',
                          fillcolor: c.band, line: { width: 0 }, layer: 'below' });
            if (withLabels) {
                labels.push({ x: w.t0, xanchor: 'left', y: 1, yref: 'paper', yanchor: 'top', xshift: 4, yshift: -2,
                              text: safe(`${w.label} · C${w.c_start}–C${w.c_end}`), showarrow: false,
                              font: { size: 10, color: c.bandText } });
            }
        }
        return { shapes, labels };
    }

    function annotationOverlay(v, c) {
        if (!root.Comments || !root.Comments.annotationOverlay) return { shapes: [], labels: [] };
        const ov = root.Comments.annotationOverlay(root.Comments.current());
        return {
            shapes: ov.shapes.map(s => Object.assign({}, s, { fillcolor: L.withAlpha(c.ink, 0.06),
                line: { width: 1, color: c.ink, dash: 'dot' }, layer: 'above' })),
            labels: ov.labels.map(a => Object.assign({}, a, { y: 0.02, yanchor: 'bottom',
                font: { size: 10, color: c.text }, bgcolor: c.card, bordercolor: c.tick, borderwidth: 1 })),
        };
    }

    function draw(v) {
        const P = root.Plotly;
        const r = v.result;
        if (!r) return;
        if (!P) { setStatus(v, 'The graphs could not load (Plotly is missing). The findings are below.', true); return; }
        const c = tokens();
        const lab = labelOf(v.sample);
        const std = r.standard_name || v.standard;
        const xr = (r.trend && r.trend.x_range) || undefined;

        // Trend: the standard a light area behind, the sample a 1.5px ink line on top
        const trendTraces = [];
        if (r.trend) {
            trendTraces.push({ x: r.trend.standard_x, y: r.trend.standard_y, type: 'scatter', mode: 'lines',
                name: safe(std), fill: 'tozeroy', fillcolor: L.withAlpha(c.ref, 0.35),
                line: { color: c.ref, width: 1 }, hovertemplate: '%{y:.0f}<extra>' + safe(std) + '</extra>' });
            trendTraces.push({ x: r.trend.sample_x, y: r.trend.sample_y, type: 'scatter', mode: 'lines',
                name: safe(lab), line: { color: c.ink, width: 1.5 },
                hovertemplate: '%{x:.2f} min · %{y:.0f}<extra>' + safe(lab) + '</extra>' });
        }
        const ticks = L.carbonTicks(r.cal_times, r.cal_carbons);
        const bands = bandShapes(c, r.windows, true);
        const notes = annotationOverlay(v, c);
        const trendLayout = baseLayout(c, {
            shapes: bands.shapes.concat(notes.shapes),
            annotations: bands.labels.concat(notes.labels),
            dragmode: v.annotating ? 'select' : 'zoom', selectdirection: 'h',
        });
        trendLayout.xaxis.range = xr;
        if (ticks.vals.length) {
            trendTraces.push({ x: [ticks.vals[0]], y: [null], xaxis: 'x2', type: 'scatter', mode: 'lines',
                               hoverinfo: 'skip', showlegend: false });
            trendLayout.xaxis2 = { overlaying: 'x', side: 'top', matches: 'x', tickvals: ticks.vals,
                ticktext: ticks.text, tickfont: { size: 10, color: c.axis }, ticks: 'outside', ticklen: 3,
                tickcolor: c.tick, showgrid: false, showline: false, zeroline: false };
            trendLayout.margin = Object.assign({}, trendLayout.margin, { t: 26 });
        }

        // Difference: above / below fills, the ink line, counted spikes, thresholds
        const diffTraces = [];
        const p = r.params_used || v.params;
        const spikes = r.spikes || [];
        if (r.diff && r.diff.x) {
            const x = r.diff.x;
            const y = r.diff.y;
            diffTraces.push({ x, y: y.map(q => (q > 0 ? q : 0)), type: 'scatter', mode: 'lines', fill: 'tozeroy',
                fillcolor: c.aboveFill, line: { width: 0, color: c.above }, hoverinfo: 'skip', name: 'higher' });
            diffTraces.push({ x, y: y.map(q => (q < 0 ? q : 0)), type: 'scatter', mode: 'lines', fill: 'tozeroy',
                fillcolor: c.belowFill, line: { width: 0, color: c.below }, hoverinfo: 'skip', name: 'lower' });
            diffTraces.push({ x, y, type: 'scatter', mode: 'lines', line: { color: c.ink, width: 1.25 },
                name: 'difference', hovertemplate: '%{x:.2f} min · %{y:+.0f}<extra></extra>' });
        }
        if (spikes.length) {
            diffTraces.push({ x: spikes.map(s => s.t), y: spikes.map(s => s.value), type: 'scatter', mode: 'markers',
                name: 'spikes', marker: { symbol: spikes.map(s => (s.sign > 0 ? 'triangle-up' : 'triangle-down')),
                size: 8, color: c.ink }, hovertemplate: 'Spike %{x:.2f} min: %{y:+.0f}<extra></extra>' });
        }
        const dBands = bandShapes(c, r.windows, false);
        const shapes = dBands.shapes.slice();
        const ann = [];
        const levels = [[p.thresh_marginal, 'marginal', 'dot'], [p.thresh_moderate, 'moderate', 'dash'],
                        [p.thresh_significant, 'significant', 'dash']];
        for (const [level, name, dash] of levels) {
            if (!Number.isFinite(Number(level))) continue;
            for (const sg of [1, -1]) {
                shapes.push({ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: sg * level, y1: sg * level,
                              line: { color: c.axis, width: 1, dash }, layer: 'below', opacity: 0.55 });
            }
            ann.push({ xref: 'paper', x: 1, xanchor: 'right', y: level, yanchor: 'bottom', showarrow: false,
                       text: `${name} ±${level}`, font: { size: 9, color: c.axis } });
        }
        const span = L.diffSpan(r.diff ? r.diff.x : [], r.diff ? r.diff.y : [], spikes, p.thresh_marginal,
                                (r.diff && r.diff.x_range) ? r.diff.x_range[1] : null);
        const diffLayout = baseLayout(c, { shapes, annotations: ann, margin: { l: 52, r: 12, t: 8, b: 34 } });
        diffLayout.xaxis.range = (r.diff && r.diff.x_range) || xr;
        diffLayout.yaxis = Object.assign(diffLayout.yaxis, { range: [-span * 1.15, span * 1.15], zeroline: true,
                                                              zerolinecolor: c.tick, tickformat: '+~s' });

        P.react(v.dom.trend, trendTraces, trendLayout, PLOT_CONFIG);
        P.react(v.dom.diff, diffTraces, diffLayout, PLOT_CONFIG);
        wire(v);
        // keep the operator's zoom across a recompute (applied after the draw,
        // so a double-click still resets to the whole run)
        if (v.zoom) applyRange(v, v.zoom);
    }

    function applyRange(v, range) {
        v.syncing = true;
        Promise.all([v.dom.trend, v.dom.diff].map(gd => root.Plotly.relayout(gd, { 'xaxis.range': range.slice() })))
            .catch(() => {}).then(() => { v.syncing = false; });
    }

    function drawAnnotations(v) {
        if (v.result && root.Plotly) draw(v);
    }

    function wire(v) {
        const d = v.dom;
        for (const [gd, other] of [[d.trend, d.diff], [d.diff, d.trend]]) {
            if (typeof gd.on !== 'function') continue;
            if (gd.removeAllListeners) {
                gd.removeAllListeners('plotly_relayout');
                gd.removeAllListeners('plotly_selected');
            }
            gd.on('plotly_relayout', (ev) => {
                if (v.syncing || !ev) return;
                let range = null;
                if (ev['xaxis.range[0]'] !== undefined) range = [ev['xaxis.range[0]'], ev['xaxis.range[1]']];
                else if (Array.isArray(ev['xaxis.range'])) range = ev['xaxis.range'].slice();
                const reset = ev['xaxis.autorange'] === true;
                if (!range && !reset) return;
                const init = v.result && v.result.trend && v.result.trend.x_range;
                const whole = range && init && Math.abs(range[0] - init[0]) < 1e-9 && Math.abs(range[1] - init[1]) < 1e-9;
                v.zoom = (reset || whole) ? null : range;
                const target = reset ? (v.result && v.result.trend && v.result.trend.x_range) : range;
                v.syncing = true;
                Promise.resolve(root.Plotly.relayout(other, { 'xaxis.range': target ? target.slice() : undefined,
                    'xaxis.autorange': !target }))
                    .catch(() => {}).then(() => { v.syncing = false; });
            });
        }
        if (typeof d.trend.on === 'function') {
            d.trend.on('plotly_selected', (ev) => {
                if (!v.annotating || !ev || !ev.range || !ev.range.x) return;
                const t0 = Math.min(ev.range.x[0], ev.range.x[1]);
                const t1 = Math.max(ev.range.x[0], ev.range.x[1]);
                if (t1 - t0 < 0.01) return;
                openPop(v, t0, t1);
            });
        }
    }

    function zoomTo(v, t0, t1) {
        if (!root.Plotly || !v.result) return;
        const pad = Math.max(0.05, (t1 - t0) * 0.15);
        const range = [Math.max(0, t0 - pad), t1 + pad];
        v.zoom = range;
        applyRange(v, range);
    }

    function fullscreen(v, card) {
        if (document.fullscreenElement === card) { document.exitFullscreen(); return; }
        if (card.requestFullscreen) card.requestFullscreen().catch(() => toast('Full screen is not available here.', 'err'));
    }

    // ── Annotate ────────────────────────────────────────────────────────────
    function setAnnotate(v, on) {
        v.annotating = !!on;
        const d = v.dom;
        d.annotBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
        d.annotBtn.classList.toggle('is-on', !!on);
        d.annotHint.hidden = !on;
        d.trend.classList.toggle('annotating', !!on);
        if (root.Plotly && d.trend._fullLayout) {
            root.Plotly.relayout(d.trend, { dragmode: on ? 'select' : 'zoom', selectdirection: 'h' });
        }
    }

    function openPop(v, t0, t1) {
        const d = v.dom;
        const r = v.result || {};
        const span = root.carbonSpanText ? root.carbonSpanText(t0, t1, r.cal_times, r.cal_carbons) : '';
        v.popRange = [t0, t1];
        v.popSample = v.sample.sample_id;
        d.popRegion.textContent = `Region ${span ? span + ', ' : ''}${t0.toFixed(2)}–${t1.toFixed(2)} min`;
        d.popInput.value = '';
        d.pop.hidden = false;
        setAnnotate(v, false);
        setTimeout(() => d.popInput.focus(), 0);
    }

    function closePop(v) {
        v.dom.pop.hidden = true;
        v.popRange = null;
        if (root.Plotly && v.dom.trend._fullLayout) {
            try {
                root.Plotly.restyle(v.dom.trend, { selectedpoints: [null] });
                root.Plotly.relayout(v.dom.trend, { selections: [] });
            } catch (_e) { /* */ }
        }
    }

    async function savePop(v) {
        if (!v.popRange || !root.Comments) return;
        const [t0, t1] = v.popRange;
        const text = v.dom.popInput.value.trim();
        await root.Comments.setSample(v.popSample);
        const saved = await root.Comments.add({ text, t0, t1 }, v.popSample);
        if (saved) closePop(v);
    }

    function syncAnnotCount(v) {
        const C = root.Comments;
        const regions = C && C.annotationComments ? C.annotationComments(C.current()) : [];
        const n = regions.length;
        const d = v.dom;
        d.clearAnnot.textContent = n === 1 ? 'Clear 1 marked region…'
            : n ? `Clear all ${n} marked regions…` : 'No marked regions to clear';
        d.clearAnnot.disabled = !n;
        d.regionItems.textContent = '';
        if (!n) return;
        d.regionItems.appendChild(h('div', { className: 'menu-head', text: 'Marked regions' }));
        for (const c of regions) {
            const label = C.regionLabel(c);
            d.regionItems.appendChild(h('button', {
                type: 'button', role: 'menuitem', className: 'menu-item cmp-region-item',
                'aria-label': 'Remove the marked region ' + label, title: 'Remove this marked region',
                'data-testid': 'compare-region-remove',
                onclick: () => { toggleAnnotMenu(v, false); removeRegion(v, c); } },
                h('span', { className: 'cmp-region-text', text: label }),
                h('span', { className: 'cmp-region-x', 'aria-hidden': 'true', text: '×' })));
        }
    }

    function removeRegion(v, c) {
        const C = root.Comments;
        if (!C || c.sample_id !== v.sample.sample_id) return;
        C.remove(c, C.removeRegionConfirmText(c));
    }

    function toggleAnnotMenu(v, force) {
        const d = v.dom;
        const open = force === undefined ? d.annotMenu.hidden : !!force;
        syncAnnotCount(v);
        d.annotMenu.hidden = !open;
        d.annotMenuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    function clearAnnotations(v) {
        toggleAnnotMenu(v, false);
        if (!root.Comments) return;
        const sid = v.sample.sample_id;
        root.Comments.setSample(sid).then(() => root.Comments.clearAnnotations(sid, labelOf(v.sample)));
    }

    // ── Findings and Conclusion ─────────────────────────────────────────────
    function renderFindings(v) {
        const d = v.dom;
        d.findings.textContent = '';
        if (!v.result) {
            d.findCount.textContent = '';
            d.findings.appendChild(h('li', { className: 'cmp-find muted', text: v.standard ? 'Comparing…' : '' }));
            return;
        }
        const view = L.findingsView(v.result);
        d.findCount.textContent = view.deviating
            ? `${view.deviating} deviating` : 'Nothing above the marginal threshold';
        for (const row of view.rows) {
            const li = h('li', { className: 'cmp-find tone-' + row.tone, 'data-testid': 'compare-finding',
                                 'data-kind': row.kind, 'data-severity': row.severity || '' },
                h('div', { className: 'cmp-find-top' },
                    row.badge ? h('span', { className: 'cmp-sev sev-' + (row.severity || row.tone), text: row.badge }) : null,
                    row.heading ? h('b', { className: 'cmp-find-head', text: row.heading }) : null,
                    h('span', { className: 'spacer' }),
                    (row.t0 !== null && row.t1 !== null)
                        ? h('button', { type: 'button', className: 'btn btn-ghost btn-sm cmp-find-go', text: 'Show',
                                        'aria-label': 'Show ' + (row.heading || 'this') + ' on the graphs',
                                        onclick: () => zoomTo(v, row.t0, row.t1) })
                        : null),
                h('p', { className: 'cmp-find-text', text: row.text }));
            d.findings.appendChild(li);
        }
    }

    function generated(v) { return (v.result && v.result.conclusion) || ''; }
    function conclusionText(v) {
        const e = v.edited[v.standard];
        return typeof e === 'string' ? e : generated(v);
    }

    function renderConclusion(v) {
        const d = v.dom;
        const edited = typeof v.edited[v.standard] === 'string';
        d.conclText.textContent = conclusionText(v) || (v.result ? '' : 'The conclusion appears with the findings.');
        d.conclText.classList.toggle('muted', !v.result && !edited);
        d.conclText.hidden = v.editing;
        d.conclForm.hidden = !v.editing;
        d.conclEdit.hidden = v.editing;
        d.conclEdit.disabled = !v.result && !edited;
        d.conclUse.hidden = v.editing || !edited;
        d.conclNote.hidden = v.editing || !edited;
        d.conclNote.textContent = edited ? 'Edited. Reports and the queue use your text.' : '';
        const hasPresets = !!(v.presets && v.presets.length);
        d.presetBtn.hidden = v.editing || !hasPresets;
        d.presetBtn.disabled = !v.result && !edited;
        d.presetTray.hidden = !(v.editing && hasPresets);
        if (!v.editing) setPresetStatus(v, null);
    }

    function editConclusion(v) {
        v.editing = true;
        const full = conclusionText(v);
        v.dom.conclArea.value = full.slice(0, L.CONCLUSION_MAX);
        v.dom.conclCut.hidden = full.length <= L.CONCLUSION_MAX;
        v.dom.conclCut.textContent = v.dom.conclCut.hidden ? ''
            : `The conclusion was cut to the ${L.CONCLUSION_MAX.toLocaleString('en-US')}-character limit.`;
        syncCount(v);
        v.presetSel = null;
        renderConclusion(v);
        renderPresetChips(v);
        v.dom.conclArea.focus();
    }

    function saveConclusion(v) {
        const text = v.dom.conclArea.value.trim();
        if (!text || text === generated(v).trim()) delete v.edited[v.standard];
        else v.edited[v.standard] = text;
        v.editing = false;
        renderConclusion(v);
    }

    // ── conclusion presets and earlier notes (v6) ──────────────────────────
    async function loadPresets(v) {
        let presets = [];
        try {
            const r = await fetch('/api/conclusion-presets', { headers: { Accept: 'application/json' } });
            const res = await root.GCSession.readJson(r);
            if (!r.ok) throw new Error((res.body && res.body.error) || ('HTTP ' + r.status));
            presets = (res.body && res.body.presets) || [];
        } catch (e) {
            if (v.alive) toast('Could not load the conclusion presets: ' + e.message, 'err');
        }
        if (!v.alive) return;
        v.presets = presets;
        const d = v.dom;
        d.presetFilter.hidden = presets.length <= 6;
        renderPresetChips(v);
        renderConclusion(v);
    }

    /** The chips, filtered, each marked when its text is already in the
        conclusion (a click then shows it in the editor) or would pass the
        limit (a click says so). The text of a chip is the preset's, exactly. */
    function renderPresetChips(v) {
        const d = v.dom;
        const q = d.presetFilter.value.trim().toLowerCase();
        const text = d.conclArea.value;
        const norm = (s) => String(s).replace(/\s+/g, ' ').trim().toLowerCase();
        const have = norm(text);
        d.presetList.textContent = '';
        let shown = 0;
        for (const p of v.presets || []) {
            if (q && !p.text.toLowerCase().includes(q)) continue;
            shown++;
            const inIt = have.includes(norm(p.text));
            const tooLong = !inIt && !L.insertPreset(text, p.text, v.presetSel || null);
            const chip = h('button', {
                type: 'button', className: 'cmp-chip' + (inIt ? ' is-in' : '') + (tooLong ? ' is-full' : ''),
                'data-testid': 'compare-preset', title: inIt ? 'In the conclusion: click to show it'
                    : tooLong ? 'Too long to fit the 1,500-character limit' : p.text,
                'aria-disabled': tooLong ? 'true' : null,
                onmousedown: (e) => e.preventDefault(),     // keep the editor's cursor
                onclick: () => (inIt ? showInConclusion(v, p.text) : insertIntoConclusion(v, p.text)) },
                svgIcon(inIt ? 'check' : 'plus'), h('span', { className: 'cmp-chip-text', text: p.text }));
            d.presetList.appendChild(chip);
        }
        d.presetEmpty.hidden = shown > 0 || !(v.presets && v.presets.length);
        d.presetEmpty.textContent = q ? `No preset matches “${d.presetFilter.value.trim()}”.` : '';
    }

    function openPresets(v) {
        const d = v.dom;
        if (!v.editing) editConclusion(v);
        if (!v.editing) return;
        d.conclForm.scrollIntoView({ block: 'nearest' });   // the tray and Save in view
        const first = d.presetFilter.hidden ? d.presetList.querySelector('button') : d.presetFilter;
        if (first) first.focus({ preventScroll: true });
    }

    /** Select a preset's text where it already is in the conclusion. */
    function showInConclusion(v, text) {
        const d = v.dom;
        const i = d.conclArea.value.toLowerCase().indexOf(String(text).trim().toLowerCase());
        d.conclArea.focus();
        if (i >= 0) { try { d.conclArea.setSelectionRange(i, i + String(text).trim().length); } catch (_e) { /* */ } }
    }

    function setPresetStatus(v, info) {
        const d = v.dom;
        d.presetStatus.textContent = '';
        v.presetUndo = info;
        if (!info) return;
        const short = info.text.length > 48 ? info.text.slice(0, 47) + '…' : info.text;
        d.presetStatus.append(`Added “${short}”`,
            h('button', { type: 'button', className: 'cmp-presets-undo', text: 'Undo',
                          'data-testid': 'compare-preset-undo', onmousedown: (e) => e.preventDefault(),
                          onclick: () => undoPreset(v) }));
    }

    function undoPreset(v) {
        const d = v.dom;
        const u = v.presetUndo;
        if (!u || d.conclArea.value !== u.after) { setPresetStatus(v, null); return; }
        d.conclArea.value = u.before;
        syncCount(v);
        d.conclArea.focus();
        try { d.conclArea.setSelectionRange(u.sel.start, u.sel.end); } catch (_e) { /* */ }
        v.presetSel = u.sel;
        setPresetStatus(v, null);
        renderPresetChips(v);
    }

    /** Put `text` into the conclusion: at the cursor while editing, else the
        editor opens on the conclusion with `text` appended. The analyst
        then edits and saves (Save keeps it per standard, as any edit). */
    function insertIntoConclusion(v, text) {
        const d = v.dom;
        if (!v.result && typeof v.edited[v.standard] !== 'string') {
            toast('The conclusion appears with the findings; insert it then.', 'err');
            return false;
        }
        let sel = null;
        if (v.editing) {
            sel = document.activeElement === d.conclArea
                ? { start: d.conclArea.selectionStart, end: d.conclArea.selectionEnd }
                : (v.presetSel || null);
        } else editConclusion(v);
        const out = L.insertPreset(d.conclArea.value, text, sel);
        if (!out) {
            const max = L.CONCLUSION_MAX.toLocaleString('en-US');
            toast(`Not inserted: the conclusion would be longer than ${max} characters. ` +
                  'Shorten it first.', 'err');
            d.conclArea.focus();
            return false;
        }
        const before = d.conclArea.value;
        d.conclArea.value = out.text;
        d.conclCut.hidden = true;
        syncCount(v);
        d.conclArea.focus();
        try { d.conclArea.setSelectionRange(out.caret, out.caret); } catch (_e) { /* */ }
        v.presetSel = { start: out.caret, end: out.caret };
        setPresetStatus(v, { text: String(text).trim(), before, after: out.text,
                             sel: sel || { start: before.length, end: before.length } });
        renderPresetChips(v);
        return true;
    }

    function syncCount(v) {
        const d = v.dom;
        const c = L.conclusionCount(d.conclArea.value);
        d.conclCount.textContent = c.text;
        d.conclCount.classList.toggle('is-near', c.near);
        d.conclCount.classList.toggle('is-full', c.full);
    }

    /** A paste that would pass the limit is cut to fit, with a one-line notice
        (the browser's maxlength would cut it silently). */
    function pasteConclusion(v, e) {
        const d = v.dom;
        const data = e.clipboardData && e.clipboardData.getData('text');
        if (data == null) return;
        const out = L.pasteInto(d.conclArea.value, data,
                                { start: d.conclArea.selectionStart, end: d.conclArea.selectionEnd });
        if (!out.cut) return;
        e.preventDefault();
        d.conclArea.value = out.text;
        try { d.conclArea.setSelectionRange(out.caret, out.caret); } catch (_e) { /* */ }
        d.conclCut.textContent = `The pasted text was cut to fit the ${L.CONCLUSION_MAX.toLocaleString('en-US')}-character limit.`;
        d.conclCut.hidden = false;
        syncCount(v);
        renderPresetChips(v);
    }

    function renderNotes(v) {
        const d = v.dom;
        const C = root.Comments;
        const notes = C && C.noteComments ? C.noteComments(C.current()) : [];
        d.notesList.textContent = '';
        d.notes.hidden = !notes.length;
        for (const c of notes) {
            d.notesList.appendChild(h('li', { className: 'cmp-note', 'data-testid': 'compare-note' },
                h('p', { className: 'cmp-note-text', text: c.text }),
                h('div', { className: 'cmp-note-foot' },
                    h('span', { className: 'caption', text: C.commentMeta(c) }),
                    h('span', { className: 'spacer' }),
                    h('button', { type: 'button', className: 'btn btn-sm btn-ghost', text: 'Add to conclusion',
                                  'data-testid': 'compare-note-insert',
                                  onclick: () => insertIntoConclusion(v, c.text) }),
                    h('button', { type: 'button', className: 'btn btn-sm btn-ghost', text: 'Remove',
                                  'data-testid': 'compare-note-remove',
                                  onclick: () => C.remove(c, C.removeNoteConfirmText(c)) }))));
        }
    }

    // ── the Adjust drawer ───────────────────────────────────────────────────
    function buildDrawer(v) {
        const d = v.dom;
        d.sliders = {};
        d.fieldErr = {};
        const sliderRow = (name, label) => {
            const out = h('output', { className: 'cmp-slider-val' });
            const input = h('input', { type: 'range', min: '-1', max: '1', step: '0.05', 'aria-label': label,
                                       'data-testid': 'adjust-' + name,
                                       oninput: () => { v.draft[name] = Number(input.value);
                                                        out.textContent = L.sliderLabel(input.value); changed(v); } });
            d.sliders[name] = { input, out };
            return h('label', { className: 'cmp-slider' }, h('span', { text: label }), input, out);
        };
        const numRow = (key, label, testid) => {
            const input = h('input', { type: 'text', inputmode: 'decimal', 'data-testid': testid,
                                       'aria-describedby': 'err-' + key + '-' + v.sample.sample_id,
                                       oninput: () => { v.draft[key] = input.value; changed(v); } });
            const err = h('span', { className: 'cmp-err', id: 'err-' + key + '-' + v.sample.sample_id, hidden: true });
            d.sliders[key] = { input };
            d.fieldErr[key] = err;
            return h('div', { className: 'cmp-num' }, h('label', { text: label, for: null }, input), err);
        };
        d.rangeList = h('div', { className: 'cmp-ranges', 'data-testid': 'adjust-ranges' });
        d.saveDefault = h('button', { type: 'button', className: 'btn btn-sm', 'data-testid': 'adjust-save-default',
                                      text: 'Save as default', onclick: () => saveDefault(v) });
        d.drawerMsg = h('p', { className: 'caption', role: 'status' });
        const drawer = h('aside', { className: 'cmp-drawer', 'data-testid': 'compare-drawer', hidden: true,
                                    'aria-labelledby': 'cmp-adj-h-' + v.sample.sample_id },
            h('div', { className: 'cmp-drawer-head' },
                h('h2', { id: 'cmp-adj-h-' + v.sample.sample_id, text: 'Adjust' }),
                h('span', { className: 'spacer' }),
                h('button', { type: 'button', className: 'icon-btn', 'aria-label': 'Close Adjust',
                              'data-testid': 'adjust-close', onclick: () => setDrawer(v, false) },
                    h('span', { className: 'ico rq-x', 'aria-hidden': 'true' }))),
            h('div', { className: 'cmp-drawer-body' },
                h('p', { className: 'caption', text: 'Changes redraw the graphs and findings straight away. They apply to this sample only until you save them as the default.' }),
                h('h3', { text: 'Trend line' }),
                sliderRow('baseline', 'Baseline'), sliderRow('detail', 'Detail'), sliderRow('smoothing', 'Smoothing'),
                h('h3', {}, 'Deviation thresholds', h('span', { className: 'caption', text: 'intensity' })),
                numRow('thresh_marginal', 'Marginal', 'adjust-marginal'),
                numRow('thresh_moderate', 'Moderate', 'adjust-moderate'),
                numRow('thresh_significant', 'Significant', 'adjust-significant'),
                h('h3', {}, 'Ranges', h('span', { className: 'caption', text: 'shaded on the graphs' })),
                d.rangeList,
                h('button', { type: 'button', className: 'btn btn-ghost btn-sm', 'data-testid': 'adjust-add-range',
                              onclick: () => { v.draft.ranges.push(L.newRange(v.draft.ranges)); renderRanges(v); changed(v); } },
                    h('span', { className: 'ico ico-plus', 'aria-hidden': 'true' }), 'Add range'),
                h('h3', { text: 'Graph' }),
                numRow('x_max_min', 'Show up to (min)', 'adjust-xmax'),
                d.drawerMsg),
            h('div', { className: 'cmp-drawer-foot' },
                h('button', { type: 'button', className: 'btn btn-ghost btn-sm', text: 'Reset',
                              'data-testid': 'adjust-reset', onclick: () => resetDraft(v) }),
                h('span', { className: 'spacer' }), d.saveDefault));
        return drawer;
    }

    function fillDrawer(v) {
        const d = v.dom;
        for (const name of L.SLIDERS) {
            d.sliders[name].input.value = String(v.draft[name]);
            d.sliders[name].out.textContent = L.sliderLabel(v.draft[name]);
        }
        for (const key of ['thresh_marginal', 'thresh_moderate', 'thresh_significant', 'x_max_min']) {
            d.sliders[key].input.value = v.draft[key];
        }
        renderRanges(v);
        showErrors(v, L.validateAdjust(v.draft));
    }

    function renderRanges(v) {
        const d = v.dom;
        d.rangeList.textContent = '';
        v.draft.ranges.forEach((r, i) => {
            const set = (key) => (e) => { r[key] = e.target.value; changed(v); };
            const errs = h('p', { className: 'cmp-err', hidden: true });
            const card = h('div', { className: 'cmp-range', 'data-testid': 'adjust-range' },
                h('input', { type: 'text', className: 'cmp-range-label', value: r.label, maxlength: '60',
                             'aria-label': `Range ${i + 1} name`, oninput: set('label') }),
                h('span', { className: 'caption', text: 'C' }),
                h('input', { type: 'text', inputmode: 'numeric', className: 'cmp-range-c', value: r.c_start,
                             'aria-label': `Range ${i + 1} first carbon`, oninput: set('c_start') }),
                h('span', { className: 'caption', text: '–' }),
                h('input', { type: 'text', inputmode: 'numeric', className: 'cmp-range-c', value: r.c_end,
                             'aria-label': `Range ${i + 1} last carbon`, oninput: set('c_end') }),
                h('button', { type: 'button', className: 'icon-btn', 'aria-label': `Remove range ${r.label || i + 1}`,
                              'data-testid': 'adjust-remove-range',
                              onclick: () => { v.draft.ranges.splice(i, 1); renderRanges(v); changed(v); } },
                    h('span', { className: 'ico rq-x', 'aria-hidden': 'true' })),
                errs);
            d.rangeList.appendChild(card);
        });
        if (!v.draft.ranges.length) {
            d.rangeList.appendChild(h('p', { className: 'caption', text: 'No ranges: the findings cover the whole run.' }));
        }
    }

    function showErrors(v, res) {
        const d = v.dom;
        for (const [key, err] of Object.entries(d.fieldErr)) {
            const msg = res.errors[key];
            err.textContent = msg || '';
            err.hidden = !msg;
            d.sliders[key].input.setAttribute('aria-invalid', msg ? 'true' : 'false');
        }
        const cards = d.rangeList.querySelectorAll('.cmp-range');
        cards.forEach((card, i) => {
            const e = res.rangeErrors[i] || {};
            const msgs = [e.label, e.c_start && ('First carbon: ' + e.c_start), e.c_end && ('Last carbon: ' + e.c_end)]
                .filter(Boolean);
            const p = card.querySelector('.cmp-err');
            p.textContent = msgs.join(' ');
            p.hidden = !msgs.length;
            const inputs = card.querySelectorAll('input');
            inputs[0].setAttribute('aria-invalid', e.label ? 'true' : 'false');
            inputs[1].setAttribute('aria-invalid', e.c_start ? 'true' : 'false');
            inputs[2].setAttribute('aria-invalid', e.c_end ? 'true' : 'false');
        });
        d.saveDefault.disabled = !res.ok;
    }

    function changed(v) {
        const res = L.validateAdjust(v.draft);
        showErrors(v, res);
        if (!res.ok) { v.dom.drawerMsg.textContent = 'Fix the marked fields to update the graphs.'; return; }
        v.dom.drawerMsg.textContent = '';
        if (!v.params || v.params.x_max_min !== res.params.x_max_min) v.zoom = null;
        v.params = res.params;
        v.overlays = res.ranges;
        analyse(v);
    }

    function resetDraft(v) {
        v.draft = L.draftFrom(L.defaultParams(v.settings),
            root.overlaysFromSettings ? root.overlaysFromSettings(v.settings) : []);
        fillDrawer(v);
        changed(v);
    }

    async function saveDefault(v) {
        const res = L.validateAdjust(v.draft);
        if (!res.ok || !root.GCShell || !root.GCShell.adminPost) return;
        const body = { params: res.params,
                       range_overlays: res.ranges.map(r => ({ label: r.label, c_start: r.c_start,
                                                              c_end: r.c_end, color: r.color })) };
        const out = await root.GCShell.adminPost('/api/save-analysis-defaults', body,
            { reason: 'Saving these as the defaults for every sample needs the admin password. It is kept in this tab for 15 minutes.' });
        if (!out) return;                                   // cancelled
        if (out.status >= 200 && out.status < 300) {
            // the saved defaults are now these (Reset returns to them)
            const s = Object.assign({}, v.settings);
            const keys = { quantile: 'analysis_quantile', window: 'analysis_window', sigma: 'analysis_sigma',
                thresh_marginal: 'analysis_thresh_marginal', thresh_moderate: 'analysis_thresh_moderate',
                thresh_significant: 'analysis_thresh_significant', x_max_min: 'analysis_x_max_min' };
            for (const [k, sk] of Object.entries(keys)) s[sk] = String(res.params[k]);
            s.analysis_range_overlays = JSON.stringify(body.range_overlays);
            v.settings = s;
            v.dom.drawerMsg.textContent = 'Saved as the default for every sample.';
            toast('Saved as the default for every sample');
        }
    }

    function setDrawer(v, open) {
        const d = v.dom;
        v.drawerOpen = !!open;
        if (open) { v.draft = L.draftFrom(v.params, v.overlays); fillDrawer(v); }
        d.drawer.hidden = !open;
        d.adjustBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
        d.adjustBtn.classList.toggle('is-on', !!open);
        document.documentElement.classList.toggle('cmp-adjust-open', !!open);
        document.dispatchEvent(new CustomEvent('gc:adjust', { detail: { open: !!open } }));
        if (open) {
            const first = d.drawer.querySelector('input');
            if (first) setTimeout(() => first.focus(), 0);
        } else if (v.alive) {
            d.adjustBtn.focus();
        }
    }

    // ── report queue and the Export report sheet ────────────────────────────
    function queueItem(v) {
        const e = v.edited[v.standard];
        return {
            sample_id: v.sample.sample_id, lab_id: v.sample.lab_id || v.sample.name || '',
            sample_name: 'GC Analysis', standard_name: v.standard || '',
            conclusion: typeof e === 'string' ? e : '',
            params: Object.assign({}, v.params), ranges: (v.overlays || []).slice(), overlay_standards: [],
        };
    }

    function addViewToQueue(v) {
        if (!v.standard) { toast('Pick a standard first.', 'err'); return null; }
        if (!root.GCReportQueue) { toast('The report queue is not available on this page.', 'err'); return null; }
        return root.GCReportQueue.add(queueItem(v));
    }

    function viewFor(sample) {
        const sid = sample && sample.sample_id;
        for (const v of mounted) if (v.alive && v.sample.sample_id === sid) return v;
        return null;
    }

    /** The standard a sample's report uses without a mounted view (bulk
        actions): its remembered pick, else the list's best-fit label when it
        names a standard, else the first standard. */
    function defaultStandard(sample, standards) {
        const pick = L.pickStandard({ remembered: L.recallPick(loadPicks(), sample && sample.sample_id),
            sampleBestFit: sample && sample.best_fit, standards: standards || [] });
        return pick ? pick.name : null;
    }

    function detachedItem(o) {
        const sample = o.sample || {};
        const std = defaultStandard(sample, o.standards);
        return {
            sample_id: sample.sample_id, lab_id: sample.lab_id || sample.name || '', sample_name: 'GC Analysis',
            standard_name: std || '', conclusion: '', params: L.defaultParams(o.settings),
            ranges: root.overlaysFromSettings ? root.overlaysFromSettings(o.settings || {}) : undefined,
            overlay_standards: [],
        };
    }

    function addToQueue(o) {
        const v = viewFor(o && o.sample);
        if (v) return addViewToQueue(v);
        const item = detachedItem(o || {});
        if (!item.standard_name) { toast('No comparison standard to compare with.', 'err'); return null; }
        return root.GCReportQueue ? root.GCReportQueue.add(item) : null;
    }

    let exportDlg = null;
    const ex = {};
    function buildExport() {
        if (exportDlg) return exportDlg;
        ex.title = h('h2', { id: 'cmp-export-h', text: 'Export report' });
        ex.sub = h('p', { className: 'caption' });
        ex.std = h('select', { 'data-testid': 'export-standard' });
        ex.doc = h('input', { type: 'text', value: 'GC Analysis Report', maxlength: '120', 'data-testid': 'export-title' });
        ex.concl = h('textarea', { rows: '4', maxlength: String(L.CONCLUSION_MAX), 'data-testid': 'export-conclusion',
                                   placeholder: 'Leave empty to use the generated conclusion.' });
        ex.others = h('div', { className: 'cmp-export-others' });
        ex.params = h('p', { className: 'caption cmp-export-params', 'data-testid': 'export-params' });
        ex.msg = h('p', { className: 'caption', role: 'status' });
        ex.dl = h('button', { type: 'submit', className: 'btn btn-primary', text: 'Download PDF', 'data-testid': 'export-download' });
        ex.queue = h('button', { type: 'button', className: 'btn', text: 'Add to queue', 'data-testid': 'export-queue' });
        const form = h('form', { method: 'dialog', className: 'form' },
            ex.title, ex.sub,
            h('label', { className: 'field' }, h('span', { text: 'Compared with' }), ex.std),
            h('label', { className: 'field' }, h('span', { text: 'Report title' }), ex.doc),
            h('label', { className: 'field' }, h('span', { text: 'Conclusion' }), ex.concl),
            h('details', { className: 'more' }, h('summary', { text: 'Also draw other standards on the report' }), ex.others),
            ex.params, ex.msg,
            h('div', { className: 'actions' },
                h('button', { type: 'button', className: 'btn btn-ghost', text: 'Cancel', onclick: () => exportDlg.close() }),
                ex.queue, ex.dl));
        exportDlg = h('dialog', { className: 'sheet cmp-export', 'data-testid': 'export-sheet', 'aria-labelledby': 'cmp-export-h' }, form);
        form.addEventListener('submit', (e) => { e.preventDefault(); exportDownload(); });
        ex.queue.addEventListener('click', () => {
            const item = exportItem();
            if (!item) return;
            if (root.GCReportQueue && root.GCReportQueue.add(item)) exportDlg.close();
        });
        document.body.appendChild(exportDlg);
        return exportDlg;
    }

    function openExportFor(ctx) {
        buildExport();
        ex.ctx = ctx;
        ex.sub.textContent = `Sample ${labelOf(ctx.sample)}. One PDF, built by the hub with these settings.`;
        ex.std.textContent = '';
        for (const s of ctx.standards) ex.std.appendChild(h('option', { value: s.name, text: s.name }));
        ex.std.value = ctx.standard || '';
        ex.generated = ctx.generated || '';
        ex.concl.value = ctx.conclusion || ctx.generated || '';
        ex.std.onchange = () => {
            // another standard: the generated text was for the first one
            if (ex.concl.value.trim() === ex.generated.trim()) ex.concl.value = '';
            ex.generated = '';
        };
        ex.others.textContent = '';
        for (const s of ctx.standards) {
            ex.others.appendChild(h('label', { className: 'check' },
                h('input', { type: 'checkbox', 'data-path': s.path || '' }), h('span', { text: s.name })));
        }
        ex.params.textContent = 'Built with: ' + L.paramSummary(ctx.params, ctx.ranges) +
            (ctx.view ? '. Change these in Adjust.' : ' (the saved defaults).');
        ex.msg.textContent = '';
        ex.dl.disabled = !ctx.standards.length;
        if (typeof exportDlg.showModal === 'function') exportDlg.showModal(); else exportDlg.setAttribute('open', '');
        return exportDlg;
    }

    function exportItem() {
        const c = ex.ctx;
        if (!c) return null;
        const std = ex.std.value;
        if (!std) { ex.msg.textContent = 'Pick a standard.'; return null; }
        let concl = ex.concl.value.trim();
        if (ex.generated && concl === ex.generated.trim()) concl = '';
        return {
            sample_id: c.sample.sample_id, lab_id: c.sample.lab_id || c.sample.name || '',
            sample_name: ex.doc.value.trim() || 'GC Analysis Report', standard_name: std, conclusion: concl,
            params: Object.assign({}, c.params), ranges: (c.ranges || []).slice(),
            overlay_standards: Array.from(ex.others.querySelectorAll('input:checked'))
                .map(i => i.getAttribute('data-path')).filter(Boolean),
        };
    }

    async function exportDownload() {
        const item = exportItem();
        if (!item) return;
        const body = root.buildReportItemPayload(item, undefined, undefined);
        ex.dl.disabled = true;
        ex.msg.textContent = 'Building the PDF…';
        try {
            const r = await fetch('/api/export-analysis-report', { method: 'POST',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            if (!r.ok) throw new Error(((await root.GCSession.readJson(r)).body || {}).error || ('HTTP ' + r.status));
            const cd = r.headers.get('content-disposition') || '';
            const m = cd.match(/filename="?([^";\n]+)"?/);
            const name = m ? m[1] : `${item.lab_id || item.sample_id}_analysis_report.pdf`;
            const blob = await r.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = name;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(a.href), 1000);
            toast(`${name} downloaded`);
            exportDlg.close();
        } catch (e) {
            ex.msg.textContent = 'Not exported: ' + e.message;
        } finally {
            ex.dl.disabled = false;
        }
    }

    function openExport(v) {
        return openExportFor({ view: true, sample: v.sample, standards: v.standards, standard: v.standard,
            params: v.params, ranges: v.overlays, generated: generated(v),
            conclusion: typeof v.edited[v.standard] === 'string' ? v.edited[v.standard] : '' });
    }

    function openExportSheet(o) {
        const v = viewFor(o && o.sample);
        if (v) return openExport(v);
        const opts = o || {};
        const item = detachedItem(opts);
        return openExportFor({ view: false, sample: opts.sample || {}, standards: opts.standards || [],
            standard: item.standard_name, params: item.params, ranges: item.ranges || [],
            generated: '', conclusion: '' });
    }

    root.GCCompare = { mount, addToQueue, openExportSheet, defaultStandard };
})(typeof window !== 'undefined' ? window : globalThis);
