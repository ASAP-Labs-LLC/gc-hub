/* Sendable sample links (v4.0): the link to copy for a sample and the
   "Other runs" of a lab ID. Pure helpers (window.DeepLink + module.exports,
   node tested in tests/js/deeplink.test.js), used by the Samples page
   (samples_page.js: Copy link, the Overview's other runs).

   A copied link is <link_url>/samples/<id>, link_url from GET /api/session
   (the configured hub, or https://gc.asaplabs.net when that is LAN-only),
   never location.origin: a link copied over the LAN must still open from
   anywhere. v6.0.0: the classic page and its browser hook are gone; the URL
   itself is read by samples_router.js. */
(function (root) {
    const DEFAULT_HUB_URL = 'https://gc.asaplabs.net';
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    /** The link to copy for a sample: always the hub's address. */
    function sampleLink(hubUrl, sampleId) {
        const base = String(hubUrl || '').replace(/\/+$/, '') || DEFAULT_HUB_URL;
        return `${base}/samples/${sampleId}`;
    }

    /** The link from GET /api/session's ``link_url`` (the hub URL, or the
        public default when the hub URL is LAN-only). ``loc`` is deliberately
        ignored: the page's own origin (a LAN address) is never used. */
    function linkFromSession(session, sampleId, loc) {
        return sampleLink(session && session.link_url, sampleId);
    }

    /** "GC-2 · Sep 28 15:30" for a run. */
    function runLabel(run) {
        const inst = (run && (run.instrument_name || run.instrument)) || '?';
        const dt = String((run && run.injection_dt) || '');
        const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(dt);
        const when = m ? `${MONTHS[Number(m[2]) - 1] || m[2]} ${Number(m[3])} ${m[4]}:${m[5]}` : dt;
        return `${inst} · ${when}`;
    }

    /** The runs of a resolved lab ID other than the one opened. */
    function otherRuns(resolved) {
        if (!resolved || !Array.isArray(resolved.runs)) return [];
        return resolved.runs.filter(r => r.sample_id !== resolved.sample_id).map(r => ({
            sample_id: r.sample_id, href: `/samples/${r.sample_id}`, label: runLabel(r),
            status: r.status,
        }));
    }

    const api = {
        DEFAULT_HUB_URL, sampleLink, linkFromSession, runLabel, otherRuns,
    };

    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    root.DeepLink = api;
})(typeof window !== 'undefined' ? window : globalThis);
