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

    function oneLine(s) {
        return String(s == null ? '' : s).split(/\s+/).filter(Boolean).join(' ');
    }

    /** The ranges a report prints, in the report footer's words and order:
        "Gas C5–C11, Jet C12–C18" (carbons low to high, as the hub swaps
        them). '' for an explicit [] (no ranges); null when nothing was
        captured (the hub then uses its saved defaults when it builds). */
    function rangesText(ranges) {
        if (!Array.isArray(ranges)) return null;
        return rangesForPayload(ranges).map((r) => {
            const lo = Math.min(r.c_start, r.c_end);
            const hi = Math.max(r.c_start, r.c_end);
            return `${oneLine(r.label) || 'Range'} C${lo}–C${hi}`;
        }).join(', ');
    }

    function rangesKey(ranges) {
        if (!Array.isArray(ranges)) return null;
        return JSON.stringify(rangesForPayload(ranges).map(r => [oneLine(r.label), r.c_start, r.c_end]));
    }

    /** Would *current* (the open Compare view's queue item) build another
        report than the queued *item*? The standard, the parameters, the
        ranges (label and carbons, in order; UI ids and colours don't count)
        and a conclusion edited on screen. A conclusion typed only in the
        export sheet is the item's own and never counts. */
    function reportDiffers(item, current) {
        if (!item || !current) return false;
        if (String(item.standard_name || '') !== String(current.standard_name || '')) return true;
        const a = captureReportParams(item.params);
        const b = captureReportParams(current.params);
        if (REPORT_PARAM_KEYS.some(k => a[k] !== b[k])) return true;
        if (rangesKey(item.ranges) !== rangesKey(current.ranges)) return true;
        const edited = String(current.conclusion || '').trim();
        return !!edited && edited !== String(item.conclusion || '').trim();
    }

    /** The queued *item* brought up to the open view (*current*): its
        standard, parameters and ranges, and its conclusion when edited on
        screen (else the item's own, unless the standard changed: that text
        was written for the other one). The title, the other standards drawn
        and the sample are the item's. */
    function refreshItem(item, current) {
        const edited = String(current.conclusion || '').trim();
        const same = String(item.standard_name || '') === String(current.standard_name || '');
        return Object.assign({}, item, {
            standard_name: current.standard_name,
            params: captureReportParams(current.params),
            ranges: rangesForPayload(Array.isArray(current.ranges) ? current.ranges : []),
            conclusion: edited || (same ? String(item.conclusion || '') : ''),
        });
    }

    /** The Analysis tab's range overlays from the settings, in the server's
        order (analysis_core.resolve_report_ranges): a saved list is final,
        even an empty one ("[]" = no ranges); nothing saved or unreadable →
        the legacy Gas/Oil keys. Each gets a UI id. */
    function overlaysFromSettings(settings) {
        const s = settings || {};
        let saved = s.analysis_range_overlays;
        if (typeof saved === 'string' && saved.trim() !== '') {
            try { saved = JSON.parse(saved); } catch (_) { saved = null; }
        }
        if (Array.isArray(saved)) {
            return saved.map((r, i) => ({
                id: i + 1, label: r.label, c_start: r.c_start, c_end: r.c_end,
                color: r.color || '#3fb95044',
            }));
        }
        const num = (v, d) => { const n = parseInt(v, 10); return Number.isFinite(n) ? n : d; };
        return [
            { id: 1, label: 'Gas', c_start: num(s.analysis_gas_c_start, 5),
              c_end: num(s.analysis_gas_c_end, 11), color: '#3fb95044' },
            { id: 2, label: 'Oil', c_start: num(s.analysis_oil_c_start, 20),
              c_end: num(s.analysis_oil_c_end, 44), color: '#d2992244' },
        ];
    }

    // The report ZIP is a background job (v3.0.1: building N PDFs in the
    // request outlasted Cloudflare's 100 s). Only the hub's own one-time
    // download path is followed.
    function isZipDownloadUrl(u) {
        return typeof u === 'string' &&
            /^\/api\/export-analysis-reports-zip\/[A-Za-z0-9_-]+\/download$/.test(u);
    }

    // What the page shows for a polled ZIP job, and the link to fetch once done.
    function zipJobView(job) {
        if (job.state === 'running') {
            return { done: false, cls: 'info', download: null,
                     message: `Generating reports: ${job.done || 0} of ${job.total}…` };
        }
        if (job.state === 'done') {
            const r = job.result || {};
            const skipped = r.skipped ? ` (${r.skipped} skipped: unknown sample, no CDF or ` +
                'standard not found)' : '';
            return { done: true, cls: 'success',
                     download: isZipDownloadUrl(job.download) ? job.download : null,
                     message: `Downloaded ${r.written} reports as ZIP${skipped}` };
        }
        return { done: true, cls: 'error', download: null,
                 message: `Download failed: ${job.error || job.state}` };
    }

    root.overlaysFromSettings = overlaysFromSettings;
    root.rangesForPayload = rangesForPayload;
    root.captureReportParams = captureReportParams;
    root.buildReportItemPayload = buildReportItemPayload;
    root.zipJobView = zipJobView;
    root.isZipDownloadUrl = isZipDownloadUrl;
    root.rangesText = rangesText;
    root.reportDiffers = reportDiffers;
    root.refreshItem = refreshItem;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { rangesForPayload, captureReportParams, buildReportItemPayload,
            overlaysFromSettings, zipJobView, isZipDownloadUrl, rangesText, reportDiffers, refreshItem };
    }
})(typeof window !== 'undefined' ? window : globalThis);
