/* Pure, DOM-free helpers for the Restart button — shared by the browser
   (window globals) and Node tests (module.exports). The decision object is
   POST /api/restart's answer: {mode: "switch"|"restart", tag, pid, busy, blocked}. */
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

    /** True once /healthz answers from a different process than ``oldPid``
        (or a different version than ``oldVersion``, should the OS reuse the
        pid). The old process keeps answering while it waits to be stopped,
        so "answers at all" is not enough. */
    function serverReplaced(oldPid, body, oldVersion) {
        if (!body || body.status !== 'ok') return false;
        if (oldPid == null) return true;
        if (body.pid !== oldPid) return true;
        return oldVersion != null && body.version != null && body.version !== oldVersion;
    }

    /** After a restart that asked the updater to install ``expectedTag``:
        a notice when the process that came back runs something else. */
    function switchOutcomeNotice(expectedTag, runningVersion) {
        if (!expectedTag || _sameRelease(runningVersion, expectedTag)) return null;
        return `Update was not installed — still on ${String(runningVersion || '').trim()}`;
    }

    // The updater's differs_from: trimmed, case-insensitive equality.
    function _sameRelease(a, b) {
        return String(a || '').trim().toLowerCase() === String(b || '').trim().toLowerCase();
    }

    /** What to ask before a restart while background work runs (the dry
        run's ``busy``/``blocked`` lists), or null when nothing does.
        ``canForce`` is false for work that is never interrupted (a purge). */
    function restartBusyPrompt(decision) {
        const items = (list) => list.map((b) => `- ${b}`).join('\n');
        const blocked = (decision && decision.blocked) || [];
        const busy = (decision && decision.busy) || [];
        if (blocked.length) {
            return { canForce: false, text: `Restart is not possible now:\n\n${items(blocked)}` +
                '\n\nThis cannot be overridden; try again when it has finished.' };
        }
        if (busy.length) {
            return { canForce: true, text: `The hub is busy:\n\n${items(busy)}\n\n` +
                'Restarting now cuts that short. Restart anyway?' };
        }
        return null;
    }

    root.restartLabel = restartLabel;
    root.restartConfirmText = restartConfirmText;
    root.serverReplaced = serverReplaced;
    root.switchOutcomeNotice = switchOutcomeNotice;
    root.restartBusyPrompt = restartBusyPrompt;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { restartLabel, restartConfirmText, serverReplaced, switchOutcomeNotice,
            restartBusyPrompt };
    }
})(typeof window !== 'undefined' ? window : globalThis);
