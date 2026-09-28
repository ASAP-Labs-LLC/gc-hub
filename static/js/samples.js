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

    /** ``{text, cls, title}`` for a sample that isn't plainly final (its hold
        reason, from samples.error, is the title), or null. A final backfill
        sample that isn't released gets a "Backfill" badge (never exported
        until an admin releases it). */
    function statusBadge(s) {
        const status = s.status || '';
        if (status === 'final') {
            if (s.backfill && !s.released) {
                return { text: 'Backfill', cls: 'status-badge status-backfill',
                         title: 'Injected before the instrument went live: not exported ' +
                                'or uploaded until an admin releases it.' };
            }
            return null;
        }
        const [text, why] = STATUS_TEXT[status] || [status, `Status: ${status}`];
        let title = s.error || why;
        if (status === 'other_method' && s.method_name && !s.error) {
            title = `Method ${s.method_name} is not mapped for this instrument: stored, ` +
                    'never processed or exported.';
        }
        return { text, cls: `status-badge status-${status}`, title };
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
        return '/api/files?' + parts.join('&');
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
                  needsServerSearch, filesUrl, countLabel, curveFetchable, escapeHtml };
    Object.assign(root, api);
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
})(typeof window !== 'undefined' ? window : globalThis);
