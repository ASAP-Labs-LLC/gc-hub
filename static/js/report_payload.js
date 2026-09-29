/* Pure, DOM-free report-payload helpers — shared by the browser (window
   globals) and Node tests (module.exports). No document/fetch references.

   Bullets are never sent: the server computes them from the parameters and
   ranges carried here (phase 3). */
(function (root) {
    /** The operator parameters a report is computed with. */
    const REPORT_PARAM_KEYS = ['quantile', 'window', 'sigma', 'thresh_marginal',
        'thresh_moderate', 'thresh_significant', 'x_max_min'];

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

    /** The trend parameters, thresholds and graph x-max from the Analysis
        tab's state, as numbers (captured on a queue item at queue time).
        Non-numeric values are left out (the server then uses its defaults). */
    function captureReportParams(analysisParams) {
        const out = {};
        if (!analysisParams || typeof analysisParams !== 'object') return out;
        for (const key of REPORT_PARAM_KEYS) {
            const v = parseFloat(analysisParams[key]);
            if (Number.isFinite(v)) out[key] = v;
        }
        return out;
    }

    /** Queue item → /api/export-analysis-report(-s-zip) and QBench payload.
        Ranges: the ones captured when the item was queued (an empty list
        means "no ranges" and is sent as `ranges: []`); items queued before
        ranges were captured fall back to *fallbackOverlays* (current UI
        state); with neither, the key is omitted so the server uses its saved
        defaults. Parameters: the item's captured `params`, else
        *fallbackParams* (current state), flattened to top-level keys. */
    function buildReportItemPayload(item, fallbackOverlays, fallbackParams) {
        const payload = {
            sample_id: item.sample_id,
            standard_name: item.standard_name,
            conclusion: item.conclusion || '',
            doc_name: item.sample_name || 'GC Analysis',
            lab_id: item.lab_id,
            overlay_standards: item.overlay_standards || [],
        };
        const ranges = Array.isArray(item.ranges) ? item.ranges : fallbackOverlays;
        if (Array.isArray(ranges)) payload.ranges = rangesForPayload(ranges);
        const params = (item.params && typeof item.params === 'object')
            ? captureReportParams(item.params)
            : captureReportParams(fallbackParams);
        Object.assign(payload, params);
        return payload;
    }

    root.rangesForPayload = rangesForPayload;
    root.captureReportParams = captureReportParams;
    root.buildReportItemPayload = buildReportItemPayload;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { rangesForPayload, captureReportParams, buildReportItemPayload };
    }
})(typeof window !== 'undefined' ? window : globalThis);
