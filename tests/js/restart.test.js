// Restart button: labelling from POST /api/restart {dry_run:true}, and the
// "server has been replaced" check made while polling /healthz.
const { restartLabel, restartConfirmText, serverReplaced } = require('../../static/js/restart.js');

module.exports = (t) => {
    t.eq(restartLabel({ mode: 'switch', tag: 'v1.4.0', pid: 1 }), 'Restart & install v1.4.0');
    t.eq(restartLabel({ mode: 'restart', tag: null, pid: 1 }), 'Restart');
    t.eq(restartLabel(null), 'Restart');
    t.eq(restartLabel({ mode: 'switch', tag: '' }), 'Restart');

    t.eq(restartConfirmText({ mode: 'switch', tag: 'v1.4.0' }).includes('v1.4.0'), true);
    t.eq(restartConfirmText({ mode: 'restart' }).includes('Restart the server'), true);

    // Only a healthy answer from a *different* process counts: the old one
    // keeps answering while it waits for the updater to stop it.
    t.eq(serverReplaced(100, { status: 'ok', pid: 100 }), false);
    t.eq(serverReplaced(100, { status: 'ok', pid: 200 }), true);
    t.eq(serverReplaced(100, null), false);
    t.eq(serverReplaced(100, { status: 'down', pid: 200 }), false);
    // Unknown old pid: any healthy answer after the old one went away.
    t.eq(serverReplaced(null, { status: 'ok', pid: 200 }), true);
};
