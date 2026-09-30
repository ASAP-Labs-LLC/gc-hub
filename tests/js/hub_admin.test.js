// hub_admin.js: the history dry run is an admin job (v3.0.1): the page polls
// POST /api/admin/jobs/status and shows progress while it runs, then the same
// summary the synchronous route used to answer.
const H = require('../../static/js/hub_admin.js');

module.exports = (t) => {
    // progress text, per phase of jobs.import_history
    t.eq(H.progressText({ state: 'running', progress: {} }), 'Starting…');
    t.eq(H.progressText({ state: 'running', progress: { phase: 'scan' } }),
        'Scanning the folder…');
    t.eq(H.progressText({ state: 'running', progress: { phase: 'match', done: 500, total: 12000 } }),
        'Reading CDFs and matching them to the CSV: 500 of 12000');
    t.eq(H.progressText({ state: 'running', progress: { phase: 'check', done: 3, total: 9 } }),
        'Checking CDFs: 3 of 9');
    t.eq(H.progressText({ state: 'running', progress: { phase: 'import', done: 1, total: 2 } }),
        'Classifying samples: 1 of 2');
    t.eq(H.progressText({ state: 'running', progress: { phase: 'odd', done: 1, total: 2 } }),
        'odd: 1 of 2');

    // the dry run's view of a job
    const summary = { dry_run: true, counts: { new: 3 } };
    t.eq(H.isDryRun({ kind: 'import-history-dry-run' }), true);
    t.eq(H.isDryRun({ kind: 'import-history' }), false);
    t.eq(H.isDryRun(null), false);

    let v = H.dryRunView({ kind: 'import-history-dry-run', state: 'running',
                           progress: { phase: 'import', done: 1, total: 2 }, result: null });
    t.eq(v, { done: false, message: 'Dry run running (nothing is written): Classifying samples: 1 of 2',
              cls: '', summary: null });

    v = H.dryRunView({ kind: 'import-history-dry-run', state: 'done', result: { summary } });
    t.eq(v, { done: true, message: 'Dry run done (nothing written)', cls: 'ok', summary });

    // an older hub answered job.summary without result: still shown
    v = H.dryRunView({ kind: 'import-history-dry-run', state: 'done', summary });
    t.eq(v.summary, summary);

    v = H.dryRunView({ kind: 'import-history-dry-run', state: 'stopped',
                       result: { summary: Object.assign({ stopped: 'x' }, summary) } });
    t.eq(v.done, true);
    t.eq(v.cls, 'warn');
    t.eq(v.message, 'Dry run stopped (nothing written); the summary so far is below');
    t.eq(v.summary.stopped, 'x');

    v = H.dryRunView({ kind: 'import-history-dry-run', state: 'failed', error: 'OSError: gone',
                       result: null });
    t.eq(v, { done: true, message: 'Dry run failed: OSError: gone', cls: 'err', summary: null });
};
