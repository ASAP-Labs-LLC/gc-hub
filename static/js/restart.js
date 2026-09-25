/* Pure, DOM-free helpers for the Restart button — shared by the browser
   (window globals) and Node tests (module.exports). The decision object is
   POST /api/restart's answer: {mode: "switch"|"restart", tag, pid}. */
(function (root) {
    function _switching(decision) {
        return !!(decision && decision.mode === 'switch' && decision.tag);
    }

    /** Button label: names the release a restart would install. */
    function restartLabel(decision) {
        return _switching(decision) ? `Restart & install ${decision.tag}` : 'Restart';
    }

    function restartConfirmText(decision) {
        if (_switching(decision)) {
            return `Restart and install ${decision.tag}? Everyone using the app is ` +
                'disconnected briefly; this page reloads once the new version is up.';
        }
        return 'Restart the server? The page will reload automatically once the server is back up.';
    }

    /** True once /healthz answers from a different process than ``oldPid``.
        The old process keeps answering while it waits to be stopped, so
        "answers at all" is not enough. */
    function serverReplaced(oldPid, body) {
        if (!body || body.status !== 'ok') return false;
        return oldPid == null || body.pid !== oldPid;
    }

    root.restartLabel = restartLabel;
    root.restartConfirmText = restartConfirmText;
    root.serverReplaced = serverReplaced;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { restartLabel, restartConfirmText, serverReplaced };
    }
})(typeof window !== 'undefined' ? window : globalThis);
