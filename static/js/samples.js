/* Pure, DOM-free sample-list helpers (phase 2 T4) — shared by the browser
   (window globals) and Node tests (module.exports). Samples come from
   /api/files and are addressed by sample_id. */
(function (root) {
    const STATUS_TEXT = {
        received: ['Queued', 'Received; waiting to be processed.'],
        awaiting_calibration: ['Awaiting calibration',
            'The instrument has no usable calibration; processed automatically when one is saved.'],
        pending_corrections: ['Pending corrections',
            'Correction factors are unavailable; retried every 5 minutes.'],
        other_method: ['Other method',
            'Not a D2887 run for this instrument: stored, never processed or exported.'],
        review_method: ['Method review',
            'The CDF has no method name; an admin must classify it.'],
        error: ['Error', 'Processing failed; it can be reprocessed.'],
        raw_only: ['Raw only', 'Stored without a result; processed only on request.'],
    };

    /** The list uid of a sample: its id, as a string. */
    function sampleUid(s) {
        return s.uid != null ? String(s.uid) : String(s.sample_id);
    }

    /** The distinct sample ids of *files*, in order. */
    function sampleIdsOf(files) {
        const out = [];
        for (const f of files || []) {
            if (f && f.sample_id != null && !out.includes(f.sample_id)) out.push(f.sample_id);
        }
        return out;
    }

    /** "Review: <note>" for a sample with a ``review_note`` (e.g. a late
        blank, an unverifiable import match), or null. */
    function reviewNoteTitle(s) {
        const note = s && s.review_note != null ? String(s.review_note).trim() : '';
        return note ? `Review: ${note}` : null;
    }

    /** ``{text, cls, title}`` for a sample that isn't plainly final (its hold
        reason, from samples.error, is the title), or null. A final backfill
        sample that isn't released gets a "Backfill" badge (never exported
        until an admin releases it). A ``review_note`` is added to the
        tooltip; on an otherwise plain final sample it is a "Review" badge. */
    function statusBadge(s) {
        const status = s.status || '';
        const review = reviewNoteTitle(s);
        const withReview = (title) => (review ? `${title}\n${review}` : title);
        if (status === 'final') {
            if (s.backfill && !s.released) {
                return { text: 'Backfill', cls: 'status-badge status-backfill',
                         title: withReview('Injected before the instrument went live: not ' +
                                           'exported or uploaded until an admin releases it.') };
            }
            if (review) {
                return { text: 'Review', cls: 'status-badge status-review', title: review };
            }
            return null;
        }
        const [text, why] = STATUS_TEXT[status] || [status, `Status: ${status}`];
        let title = s.error || why;
        if (status === 'other_method' && s.method_name && !s.error) {
            title = `Method ${s.method_name} is not mapped for this instrument: stored, ` +
                    'never processed or exported.';
        }
        return { text, cls: `status-badge status-${status}`, title: withReview(title) };
    }

    // ── instruments (two instruments can hold the same lab ID) ──────────

    /** An instrument's display name from ``names`` ({id: name}), else its id. */
    function instrumentName(id, names) {
        const n = names && id != null ? names[id] : null;
        return n ? String(n) : String(id == null ? '' : id);
    }

    /** ``{text, cls, title}`` for a sample's instrument badge, or null. */
    function instrumentBadge(s, names) {
        if (!s || s.instrument == null || s.instrument === '') return null;
        const name = instrumentName(s.instrument, names);
        return { text: name, cls: 'instrument-badge',
                 title: `Instrument: ${name} (${s.instrument})` };
    }

    /** A trace / curve label: "40304 (2) · GC-2" (the lab ID alone when the
        sample names no instrument). */
    function traceLabel(s, names) {
        const base = (s && (s.display_name || s.name || s.lab_id)) || '';
        return s && s.instrument ? `${base} · ${instrumentName(s.instrument, names)}` : base;
    }

    /** The main page's instrument select: All, then each instrument. */
    function instrumentFilterOptions(instruments) {
        const out = [{ value: '', text: 'All instruments' }];
        for (const i of instruments || []) {
            if (i && i.id) out.push({ value: String(i.id), text: String(i.name || i.id) });
        }
        return out;
    }

    /** The remembered filter, kept only while that instrument exists (an
        empty ``known`` list means "not loaded yet": keep it). */
    function restoreInstrumentFilter(saved, known) {
        if (saved == null || String(saved).trim() === '') return null;
        saved = String(saved);
        if (Array.isArray(known) && known.length && !known.includes(saved)) return null;
        return saved;
    }

    /** The samples of one instrument (all of them when ``inst`` is empty). */
    function filterByInstrument(files, inst) {
        if (!inst) return (files || []).slice();
        return (files || []).filter(f => f && f.instrument === inst);
    }

    /** The instrument the re-process modal starts on: the list's filter, else
        the only instrument, else none (the user must choose). */
    function reprocessDefaultInstrument(known, listInstrument) {
        const ids = Array.isArray(known) ? known : [];
        if (listInstrument && ids.includes(listInstrument)) return listInstrument;
        return ids.length === 1 ? ids[0] : '';
    }

    /** Why a lab-ID list can't be resolved yet, or null. */
    function reprocessInstrumentError(inst) {
        return inst ? null : 'Choose the instrument these lab IDs belong to.';
    }

    /** Tooltip for the time-corrected marker, or null. */
    function timeCorrectedTitle(s) {
        return s.time_corrected
            ? 'Injection time corrected: v1 misread this CDF\'s time stamp.'
            : null;
    }

    /** "Not exported: 40299 (why); #9 (why)" for a route's ``refused`` list. */
    function refusalSummary(what, refused, files) {
        const label = (sid) => {
            const f = (files || []).find(x => x.sample_id === sid);
            return f ? (f.display_name || f.name || f.lab_id) : `#${sid}`;
        };
        return `${what}: ` + (refused || [])
            .map(r => `${label(r.sample_id)} (${r.error})`).join('; ');
    }

    /** The lists hold the newest page only; search the server when there is
        more than that and the search box isn't empty. */
    function needsServerSearch(q, filesTotal, filesLength) {
        return String(q || '').trim() !== '' && Number(filesTotal) > Number(filesLength);
    }

    /** /api/files URL for a search: ``q`` plus the list's instrument/status
        filters (a string or a list). */
    function filesUrl(filters, limit) {
        const parts = [`limit=${encodeURIComponent(limit)}`];
        for (const key of ['q', 'instrument', 'status', 'method']) {
            let v = (filters || {})[key];
            if (Array.isArray(v)) v = v.join(',');
            if (v != null && String(v).trim() !== '') {
                parts.push(`${key}=${encodeURIComponent(String(v).trim())}`);
            }
        }
        const ids = (filters || {}).ids;
        if (Array.isArray(ids) && ids.length) parts.push(`ids=${encodeURIComponent(ids.join(','))}`);
        return '/api/files?' + parts.join('&');
    }

    /** The server's list order: newest injection first, then the higher id. */
    function _newerFirst(a, b) {
        const da = String(a.injection_dt || ''), db = String(b.injection_dt || '');
        if (da !== db) return da < db ? 1 : -1;
        return Number(b.sample_id) - Number(a.sample_id);
    }

    /** v3.1 live updates: merge the rows fetched for *changedIds*
        (/api/files?ids=, with the list's own filters) into *files* without
        reloading it. A returned row replaces its old copy or is inserted in
        the server's order; a changed id that was not returned no longer
        matches (or is gone) and is removed. With `pageFull` (the list holds
        only the newest page) a new row older than the page's last is left
        out (counted in `skipped`). A replaced row whose injection time
        changed is moved to its place. Returns {files, added, removed,
        skipped, replaced: [rows updated in place], orderChanged}; *files*
        is not modified. */
    function mergeChangedRows(files, changedIds, rows, opts) {
        const byId = new Map((rows || []).map(r => [Number(r.sample_id), r]));
        const changed = new Set((changedIds || []).map(Number));
        let removed = 0;
        let moved = false;
        const replaced = [];
        const out = [];
        for (const f of files || []) {
            const id = Number(f.sample_id);
            if (!changed.has(id)) { out.push(f); continue; }
            if (byId.has(id)) {
                const r = byId.get(id);
                if (String(r.injection_dt || '') !== String(f.injection_dt || '')) moved = true;
                out.push(r);
                replaced.push(r);
                byId.delete(id);
            } else removed++;
        }
        const last = out.length ? out[out.length - 1] : null;
        let added = 0, skipped = 0;
        for (const r of byId.values()) {
            if (opts && opts.pageFull && last && _newerFirst(r, last) > 0) { skipped++; continue; }
            out.push(r);
            added++;
        }
        if (added || moved) out.sort(_newerFirst);
        return { files: out, added, removed, skipped, replaced,
                 orderChanged: !!(added || removed || moved) };
    }

    /** "showing N of M" when the list shows fewer than the server holds. */
    function countLabel(shown, total) {
        return Number(total) > Number(shown) ? `showing ${shown} of ${total}` : '';
    }

    /** Only a sample with a revision has a distillation curve. */
    function curveFetchable(s) {
        return !!s && s.current_revision != null;
    }

    const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

    /** Text → HTML, safe in element and attribute contexts. */
    function escapeHtml(str) {
        return String(str == null ? '' : str).replace(/[&<>"']/g, c => HTML_ESCAPES[c]);
    }

    const api = { sampleUid, sampleIdsOf, statusBadge, timeCorrectedTitle, refusalSummary,
                  needsServerSearch, filesUrl, countLabel, curveFetchable, escapeHtml,
                  reviewNoteTitle, instrumentName, instrumentBadge, traceLabel,
                  instrumentFilterOptions, restoreInstrumentFilter, filterByInstrument,
                  reprocessDefaultInstrument, reprocessInstrumentError, mergeChangedRows };
    Object.assign(root, api);
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
})(typeof window !== 'undefined' ? window : globalThis);
