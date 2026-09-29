/* Analysis-tab comments (phase 4): preset chips, free text, the sample's
   comment list, and the annotation spans drawn on the trend plot.

   Pure helpers (commentingAs ... clearConfirmText) are shared with the Node
   tests (module.exports); the rest runs in the browser as window.Comments.
   Every string from the server is rendered with textContent; Plotly label
   text goes through plotlySafe (Plotly treats < > as markup). The author is
   the signed-in account (GET /api/session via GCSession.whoami, shown as
   "Commenting as <name>"); the server derives the initials from it and
   ignores any the page might send. */
(function (root) {
    'use strict';

    const ANNOT_FILL = 'rgba(88, 166, 255, 0.15)';      // the shape filter keys on these
    const ANNOT_LABEL_BG = 'rgba(13,17,23,0.8)';
    const LABEL_MAX = 30;

    /** The line above the comment box: who comments are saved as. */
    function commentingAs(name) {
        const n = String(name == null ? '' : name).trim();
        return n ? `Commenting as ${n}` : '';
    }

    function plotlySafe(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function annotationComments(list) {
        return (list || []).filter(c => c && c.source === 'annotation'
            && c.t0 != null && c.t1 != null);
    }

    /** Plotly shapes + labels for the annotation comments. */
    function annotationOverlay(list) {
        const shapes = [];
        const labels = [];
        for (const c of annotationComments(list)) {
            shapes.push({
                type: 'rect', x0: c.t0, x1: c.t1, y0: 0, y1: 1, yref: 'paper',
                fillcolor: ANNOT_FILL,
                line: { width: 1, color: 'rgba(88, 166, 255, 0.5)', dash: 'dash' },
                layer: 'above',
            });
            const text = String(c.text || '');
            labels.push({
                x: (c.t0 + c.t1) / 2, y: 0.95, yref: 'paper',
                text: plotlySafe(text.length > LABEL_MAX ? text.slice(0, LABEL_MAX) + '...' : text),
                showarrow: false,
                font: { color: '#58a6ff', size: 9 },
                bgcolor: ANNOT_LABEL_BG,
                borderpad: 2,
            });
        }
        return { shapes, labels };
    }

    function isAnnotationShape(s) { return !!s && (s._annotation || s.fillcolor === ANNOT_FILL); }
    function isAnnotationLabel(a) { return !!a && (a._annotation || a.bgcolor === ANNOT_LABEL_BG); }

    function _when(iso) {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return String(iso || '');
        const p = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
            `${p(d.getHours())}:${p(d.getMinutes())}`;
    }

    /** "RB, 2026-09-29 14:05" (annotations: "1.20–1.50 min; RB, ..."). */
    function commentMeta(c) {
        const who = `${c.name || c.initials || '?'}, ${_when(c.created_at)}`;
        if (c.source === 'annotation' && c.t0 != null && c.t1 != null) {
            return `${Number(c.t0).toFixed(2)}–${Number(c.t1).toFixed(2)} min; ${who}`;
        }
        return who;
    }

    function clearConfirmText(n, label) {
        const where = label ? `on sample ${label}` : 'on this sample';
        return `Delete the ${n} annotation comment${n === 1 ? '' : 's'} ${where}? ` +
            'They stay in the record as deleted and leave the report.';
    }

    const pure = {
        ANNOT_FILL, ANNOT_LABEL_BG, commentingAs, plotlySafe,
        annotationComments, annotationOverlay, isAnnotationShape, isAnnotationLabel,
        commentMeta, clearConfirmText,
    };
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = pure;
    }
    if (typeof document === 'undefined') return;

    // ── browser part ──────────────────────────────────────────────────────

    const $ = id => document.getElementById(id);
    let sampleId = null;          // the sample asked for
    let loadedId = null;          // the sample ``comments`` belong to (null while loading)
    let comments = [];
    let inflight = null;          // the pending load of ``sampleId``
    let loadSeq = 0;
    let onChange = null;          // app.js: redraw the annotation shapes

    function notify(msg, kind) {
        if (typeof root.showNotification === 'function') root.showNotification(msg, kind);
    }

    /** Show "Commenting as <name>" (the signed-in account). */
    function showAuthor() {
        const box = $('comment-author');
        if (!box || !root.GCSession || !root.GCSession.whoami) return Promise.resolve();
        return root.GCSession.whoami().then((me) => {
            box.textContent = commentingAs(me && me.name);
        }).catch(() => {});
    }

    async function request(method, url, body) {
        const opts = { method, headers: {} };
        if (body !== undefined) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        const r = await fetch(url, opts);
        let j = {};
        try { j = await r.json(); } catch (_) { j = {}; }
        if (!r.ok) throw new Error((j && j.error) || `HTTP ${r.status}`);
        return j;
    }

    function el(tag, text, cls) {
        const e = document.createElement(tag);
        if (text != null) e.textContent = String(text);
        if (cls) e.className = cls;
        return e;
    }

    function render() {
        const list = $('comment-list');
        if (list) {
            list.textContent = '';
            if (sampleId == null) {
                list.appendChild(el('li', 'Select a sample to see its comments.', 'muted'));
            } else if (!comments.length) {
                list.appendChild(el('li', 'No comments yet.', 'muted'));
            }
            for (const c of comments) {
                const li = el('li', null, 'comment-item');
                li.dataset.id = c.id;
                li.appendChild(el('span', c.text, 'comment-text'));
                li.appendChild(el('span', commentMeta(c), 'comment-meta'));
                const del = el('button', '×', 'comment-delete');
                del.title = 'Delete this comment';
                del.addEventListener('click', () => remove(c));
                li.appendChild(del);
                list.appendChild(li);
            }
        }
        const n = annotationComments(comments).length;
        const count = $('annotation-count');
        if (count) count.textContent = n ? `${n} annotation${n === 1 ? '' : 's'}` : '';
        if (onChange) {
            try { onChange(current()); } catch (e) { console.error(e); }
        }
    }

    /** The comments of the sample asked for; [] while they are loading (never
        another sample's). */
    function current() {
        return (loadedId !== null && loadedId === sampleId) ? comments.slice() : [];
    }

    /** Load the comments of ``id`` (null clears). Resolves when drawn. */
    function load(id) {
        const target = id == null ? null : id;
        const seq = ++loadSeq;
        const changed = target !== sampleId;
        sampleId = target;
        if (changed) {             // never show the previous sample's list or spans
            comments = [];
            loadedId = null;
            render();
        }
        if (target == null) {
            loadedId = null;
            inflight = null;
            render();
            return Promise.resolve([]);
        }
        const p = (async () => {
            let list = [];
            try {
                const j = await request('GET', `/api/samples/${encodeURIComponent(target)}/comments`);
                list = (j.comments || []).filter(c => c.sample_id === target);
            } catch (e) {
                if (seq === loadSeq) notify('Could not load comments: ' + e.message, 'error');
            }
            if (seq !== loadSeq) return current();         // a newer load won
            comments = list;
            loadedId = target;
            inflight = null;
            render();
            return current();
        })();
        inflight = p;
        return p;
    }

    /** Called on every sample change: reload only when the sample differs. A
        call for the sample being loaded waits for that load. */
    function setSample(id) {
        const next = id == null ? null : id;
        if (next === sampleId) {
            if (inflight) return inflight;
            if (loadedId === next) return Promise.resolve(current());
        }
        return load(next);
    }

    /** POST one comment ({text} | {preset_id} | {text, t0, t1}) to ``forSample``
        (default: the current sample). Refused (null) when the current sample is
        not ``forSample``. Returns the comment, or null. */
    async function add(fields, forSample) {
        const target = forSample === undefined ? sampleId : forSample;
        if (target == null) { notify('Select a sample first', 'info'); return null; }
        if (target !== sampleId) {
            notify('The selected sample changed; comment not added', 'error');
            return null;
        }
        try {
            const j = await request('POST', `/api/samples/${encodeURIComponent(target)}/comments`,
                Object.assign({}, fields));
            if (target === sampleId) await load(target);
            return j.comment;
        } catch (e) {
            notify('Comment not added: ' + e.message, 'error');
            return null;
        }
    }

    async function _delete(c) {
        await request('POST', `/api/samples/${encodeURIComponent(c.sample_id)}` +
            `/comments/${encodeURIComponent(c.id)}/delete`, {});
    }

    async function remove(c) {
        if (!root.confirm(`Delete this comment?\n\n${c.text}`)) return;
        try {
            await _delete(c);
        } catch (e) {
            notify('Comment not deleted: ' + e.message, 'error');
        }
        await load(sampleId);
    }

    /** Clear Annotations: soft-delete the annotation comments of ``forSample``,
        which must be the loaded current sample (``label`` names it in the
        confirmation). */
    async function clearAnnotations(forSample, label) {
        const target = forSample === undefined ? sampleId : forSample;
        if (target == null || target !== sampleId || loadedId !== target) {
            notify('The selected sample changed; nothing was deleted', 'error');
            return 0;
        }
        const targets = annotationComments(comments).filter(c => c.sample_id === target);
        if (!targets.length) { notify('No annotations on this sample', 'info'); return 0; }
        if (!root.confirm(clearConfirmText(targets.length, label))) return 0;
        let done = 0;
        for (const c of targets) {
            try { await _delete(c); done++; } catch (e) {
                notify('Annotation not deleted: ' + e.message, 'error');
            }
        }
        await load(sampleId);
        return done;
    }

    async function loadPresets() {
        const box = $('comment-presets');
        if (!box) return;
        let presets = [];
        try { presets = (await request('GET', '/api/comment-presets')).presets || []; } catch (e) {
            notify('Could not load comment presets: ' + e.message, 'error');
        }
        box.textContent = '';
        for (const p of presets) {
            const b = el('button', p.text, 'comment-chip');
            b.type = 'button';
            b.title = 'Add this comment';
            b.addEventListener('click', () => add({ preset_id: p.id }));
            box.appendChild(b);
        }
    }

    async function addFreeText() {
        const input = $('comment-free-text');
        const text = input ? input.value.trim() : '';
        if (!text) return;
        const c = await add({ text });
        if (c && input) input.value = '';
    }

    function init(opts) {
        onChange = (opts && opts.onChange) || null;
        showAuthor();
        const btn = $('btn-comment-add');
        if (btn) btn.addEventListener('click', addFreeText);
        const input = $('comment-free-text');
        if (input) {
            input.addEventListener('keydown', e => {
                if (e.key === 'Enter') { e.preventDefault(); addFreeText(); }
            });
        }
        render();
        return loadPresets();
    }

    root.Comments = Object.assign({}, pure, {
        init, load, setSample, add, remove, clearAnnotations, loadPresets,
        current,
        sampleId: () => sampleId,
    });
})(typeof window !== 'undefined' ? window : globalThis);
