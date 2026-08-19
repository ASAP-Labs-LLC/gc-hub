/* Pure, DOM-free report-payload helpers — shared by the browser (window
   globals) and Node tests (module.exports). No document/fetch references. */
(function (root) {
    /** Region overlays → the payload shape the backend expects.
        Strips UI-only fields (id) and coerces DOM string numbers. */
    function rangesForPayload(rangeOverlays) {
        if (!Array.isArray(rangeOverlays)) return [];
        return rangeOverlays.map(r => ({
            label: String(r.label != null ? r.label : 'Range'),
            c_start: parseInt(r.c_start, 10),
            c_end: parseInt(r.c_end, 10),
            color: r.color || '',
        }));
    }

    /** Queue item → /api/export-analysis-report(-s-zip) payload.
        Prefers the ranges captured when the item was queued; falls back to
        *fallbackOverlays* (current UI state) for items queued before ranges
        were captured; omits the key entirely when neither exists so the
        backend uses its saved defaults. */
    function buildReportItemPayload(item, fallbackOverlays) {
        const payload = {
            sample_path: item.sample_path,
            standard_name: item.standard_name,
            conclusion: item.conclusion || '',
            bullets: item.bullets || '',
            doc_name: item.sample_name || 'GC Analysis',
            lab_id: item.lab_id,
            overlay_standards: item.overlay_standards || [],
        };
        const ranges = (item.ranges && item.ranges.length)
            ? rangesForPayload(item.ranges)
            : rangesForPayload(fallbackOverlays);
        if (ranges.length) payload.ranges = ranges;
        return payload;
    }

    root.rangesForPayload = rangesForPayload;
    root.buildReportItemPayload = buildReportItemPayload;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { rangesForPayload, buildReportItemPayload };
    }
})(typeof window !== 'undefined' ? window : globalThis);
