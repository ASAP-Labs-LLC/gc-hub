/* Pure, DOM-free helper for Settings > QBench API — shared by the browser
   (window globals) and Node tests (module.exports). The status object is
   GET /api/qbench-api-credentials: {configured, client_id_hint, source,
   store_path}. It never carries the secret. */
(function (root) {
    function qbApiStatusText(status) {
        if (!status || !status.configured) return 'Not configured';
        const base = `Configured (${status.client_id_hint || ''})`;
        if (status.source === 'environment') {
            return `${base} from environment variables, which override anything saved here`;
        }
        return base;
    }

    root.qbApiStatusText = qbApiStatusText;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { qbApiStatusText };
    }
})(typeof window !== 'undefined' ? window : globalThis);
