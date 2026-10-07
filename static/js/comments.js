/* A sample's stored comments (phase 4; v6 wording): its marked regions
   (annotation comments: t0/t1 spans drawn on the trend plot) and its earlier
   notes (free or preset comments added before v6, which print under the
   report's conclusion). Since v6 nothing here adds a note: the analyst's
   words go in the Conclusion, and conclusion presets (GET
   /api/conclusion-presets, fetchPresets) are inserted into it.

   Pure helpers (commentingAs ... regionLabel) are shared with the Node
   tests (module.exports); the rest runs in the browser as window.Comments.
   Every string from the server is rendered with textContent; Plotly label
   text goes through plotlySafe (Plotly treats < > as markup). The author is
   the signed-in account (GET /api/session via GCSession.whoami); the server
   derives the initials from it and ignores any the page might send. */
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

    /** The earlier notes: every comment that is not a marked region. */
    function noteComments(list) {
        return (list || []).filter(c => c && !(c.source === 'annotation'
            && c.t0 != null && c.t1 != null));
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
        return `Remove the ${n} marked region${n === 1 ? '' : 's'} ${where}? ` +
            'They stay in the record as deleted and leave the report.';
    }

    function _span(c) { return `${Number(c.t0).toFixed(2)}–${Number(c.t1).toFixed(2)} min`; }

    function removeNoteConfirmText(c) {
        return `Remove this note?\n\n${(c && c.text) || ''}\n\n` +
            'It stays in the record as deleted and no longer prints on reports.';
    }

    function removeRegionConfirmText(c) {
        return `Remove the marked region "${(c && c.text) || ''}" (${_span(c || {})})?`;
    }

    /** One marked region as the Annotate menu lists it. */
    function regionLabel(c) {
        const text = String((c && c.text) || '');
        return `${text.length > LABEL_MAX ? text.slice(0, LABEL_MAX) + '…' : text} · ${_span(c || {})}`;
    }

    const pure = {
        ANNOT_FILL, ANNOT_LABEL_BG, commentingAs, plotlySafe,
        annotationComments, annotationOverlay, isAnnotationShape, isAnnotationLabel,
        commentMeta, clearConfirmText, noteComments, removeNoteConfirmText,
        removeRegionConfirmText, regionLabel,
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
    let onChange = null;          // redraw the marked regions (and list the notes)
    let onPreset = null;          // the classic page: put a preset into its conclusion

    function notify(msg, kind) {
        if (typeof root.showNotification === 'function') root.showNotification(msg, kind);
        else if (root.GCShell && root.GCShell.toast) root.GCShell.toast(msg, kind === 'error' ? 'err' : kind);
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
        const j = (await root.GCSession.readJson(r)).body || {};
        if (!r.ok || j.error) throw new Error((j && j.error) || `HTTP ${r.status}`);
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
                list.appendChild(el('li', 'Select a sample to see its notes and marked regions.', 'muted'));
            } else if (!comments.length) {
                list.appendChild(el('li', 'No notes or marked regions.', 'muted'));
            }
            for (const c of comments) {
                const li = el('li', null, 'comment-item');
                li.dataset.id = c.id;
                li.appendChild(el('span', c.text, 'comment-text'));
                li.appendChild(el('span', commentMeta(c), 'comment-meta'));
                const del = el('button', '×', 'comment-delete');
                const region = annotationComments([c]).length > 0;
                del.title = region ? 'Remove this marked region' : 'Remove this note';
                del.addEventListener('click', () => remove(c, region
                    ? removeRegionConfirmText(c) : removeNoteConfirmText(c)));
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

    /** Soft-delete one note or marked region after `confirmText` (default:
        the note wording) is confirmed. */
    async function remove(c, confirmText) {
        const ask = confirmText || (annotationComments([c]).length
            ? removeRegionConfirmText(c) : removeNoteConfirmText(c));
        if (!root.confirm(ask)) return;
        try {
            await _delete(c);
        } catch (e) {
            notify('Not removed: ' + e.message, 'error');
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
        if (!targets.length) { notify('No marked regions on this sample', 'info'); return 0; }
        if (!root.confirm(clearConfirmText(targets.length, label))) return 0;
        let done = 0;
        for (const c of targets) {
            try { await _delete(c); done++; } catch (e) {
                notify('Marked region not removed: ' + e.message, 'error');
            }
        }
        await load(sampleId);
        return done;
    }

    /** The active conclusion presets ([{id, text, sort}], in order). */
    async function fetchPresets() {
        return (await request('GET', '/api/conclusion-presets')).presets || [];
    }

    /** The classic page's preset chips: each puts its text into the
        conclusion (init's onPreset). No chips without onPreset. */
    async function loadPresets() {
        const box = $('comment-presets');
        if (!box || !onPreset) return;
        let presets = [];
        try { presets = await fetchPresets(); } catch (e) {
            notify('Could not load the conclusion presets: ' + e.message, 'error');
        }
        box.textContent = '';
        for (const p of presets) {
            const b = el('button', p.text, 'comment-chip');
            b.type = 'button';
            b.title = 'Insert into the conclusion';
            b.addEventListener('click', () => onPreset(p.text));
            box.appendChild(b);
        }
    }

    function init(opts) {
        onChange = (opts && opts.onChange) || null;
        onPreset = (opts && opts.onPreset) || null;
        showAuthor();
        render();
        return loadPresets();
    }

    root.Comments = Object.assign({}, pure, {
        init, load, setSample, add, remove, clearAnnotations, loadPresets, fetchPresets,
        current,
        sampleId: () => sampleId,
    });
})(typeof window !== 'undefined' ? window : globalThis);
