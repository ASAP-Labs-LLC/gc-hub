/* Pure, DOM-free helpers for carbon labels — shared by the browser (window
   globals) and Node tests (module.exports).

   A sample is labelled with its OWN ladder: the calibration anchors of the
   revision it was computed with, served as cal_times/cal_carbons with its
   trace (/api/samples/<id>/trace) and its analysis (/api/analysis). Never
   gc1's /api/calibration, and never an invented carbon number: no ladder,
   no labels. */
(function (root) {
    const MARKER_COLOR = '#d29922';

    function _pairs(times, carbons) {
        const t = Array.isArray(times) ? times : [];
        const c = Array.isArray(carbons) ? carbons : [];
        const n = Math.min(t.length, c.length);
        const out = [];
        for (let i = 0; i < n; i++) out.push([Number(t[i]), Number(c[i])]);
        return out;
    }

    /** Plotly shapes (dotted verticals) and "Cn" labels for a ladder. */
    function carbonMarkers(times, carbons) {
        const shapes = [];
        const annotations = [];
        for (const [rt, cn] of _pairs(times, carbons)) {
            shapes.push({
                type: 'line', x0: rt, x1: rt, y0: 0, y1: 1, yref: 'paper',
                line: { color: MARKER_COLOR, width: 1, dash: 'dot' },
            });
            annotations.push({
                x: rt, y: 1, yref: 'paper', text: `C${cn}`,
                showarrow: false, font: { color: MARKER_COLOR, size: 9 }, yanchor: 'bottom',
            });
        }
        return { shapes, annotations };
    }

    /** Carbon number at time t: linear inside the ladder, extrapolated from
        the end pairs outside it (the server's rule: comments._carbon_at,
        analysis_core.ladder_time_to_carbon). NaN with fewer than 2 points. */
    function carbonAt(t, times, carbons) {
        const p = _pairs(times, carbons).sort((a, b) => a[0] - b[0]);
        if (p.length < 2) return NaN;
        let i;
        if (t <= p[0][0]) i = 0;
        else if (t >= p[p.length - 1][0]) i = p.length - 2;
        else {
            i = 0;
            while (i < p.length - 2 && !(p[i][0] <= t && t <= p[i + 1][0])) i++;
        }
        const [x0, y0] = p[i];
        const [x1, y1] = p[i + 1];
        if (x1 === x0) return y0;
        return y0 + (t - x0) * (y1 - y0) / (x1 - x0);
    }

    /** "C11–C14" (or "C10" when both ends round alike), as the server's
        default annotation label writes it; '' without a ladder. */
    function carbonSpanText(t0, t1, times, carbons) {
        const a = carbonAt(t0, times, carbons);
        const b = carbonAt(t1, times, carbons);
        if (Number.isNaN(a) || Number.isNaN(b)) return '';
        const c0 = Math.round(a);
        const c1 = Math.round(b);
        return c0 === c1 ? `C${c0}` : `C${c0}–C${c1}`;
    }

    function _sameLadder(a, b) {
        return JSON.stringify(_pairs(a.cal_times, a.cal_carbons))
            === JSON.stringify(_pairs(b.cal_times, b.cal_carbons));
    }

    /** The ladder the chromatogram overlay labels: the first visible trace's.
        ``differs`` is true when another visible trace (e.g. another
        instrument's) has another ladder, so the page can say whose it is. */
    function overlayLadder(traces) {
        const visible = (traces || []).filter(t => t && t.visible);
        if (!visible.length) return { times: [], carbons: [], owner: null, differs: false };
        const first = visible[0];
        return {
            times: first.cal_times || [],
            carbons: first.cal_carbons || [],
            owner: first.name,
            differs: visible.some(t => !_sameLadder(t, first)),
        };
    }

    root.carbonMarkers = carbonMarkers;
    root.carbonAt = carbonAt;
    root.carbonSpanText = carbonSpanText;
    root.overlayLadder = overlayLadder;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { carbonMarkers, carbonAt, carbonSpanText, overlayLadder };
    }
})(typeof window !== 'undefined' ? window : globalThis);
