// hub_admin.js (v4.0 lane E): each job renders in its own card, summaries are
// a short table of counts (never JSON), Stop is enabled only while that card's
// job runs, and a running job is picked up from the task feed on load.
const H = require('../../static/js/hub_admin.js');

module.exports = (t) => {
    // ── which card a job belongs to ──
    t.eq(H.cardFor('load-folder'), 'lf');
    t.eq(H.cardFor('import-history'), 'ih');
    t.eq(H.cardFor('import-history-dry-run'), 'ih');
    t.eq(H.cardFor('purge'), 'purge');
    t.eq(H.cardFor('diagnostics-bundle'), 'diag');
    t.eq(H.cardFor('nonsense'), null);
    t.eq(H.cardFor(undefined), null);

    // ── summaries: non-zero counts, in order, in words; never paths or JSON ──
    const ih = { dry_run: true, processed_dir: 'C:\\secret', seconds: 12.4, run_id: 7,
                 counts: { cdf_files: 1204, cdfs_read: 1201, cdf_errors: 3, csv_rows: 0,
                           imported: 1198, conflicts: 0, cross_instrument: 2 },
                 warnings: ['A different CSV than last time'], examples: { x: [1] } };
    t.eq(H.summaryRows(ih), [['CDF files', '1,204'], ['CDFs read', '1,201'],
                             ['Unreadable CDFs', '3'], ['Imported', '1,198'],
                             ['Cross instrument', '2'], ['Took', '12 s']]);
    t.eq(H.summaryWarnings(ih), ['A different CSV than last time']);
    const lf = { instrument: 'gc1', folder: '/x/y', backfill_forced: false, files: 12, created: 10,
                 duplicate: 2, conflict: 0, rejected: 0, failed: 0, backfill: 0,
                 late_blank_review_notes: 0, sample_ids: [1, 2], rejected_files: [],
                 started_at: '2026-09-29T10:00:00+00:00', seconds: 75 };
    t.eq(H.summaryRows(lf), [['Files', '12'], ['Created', '10'], ['Duplicate', '2'],
                             ['Took', '1 min 15 s']]);
    t.eq(H.summaryRows({ counts: { cdf_files: 0 } }), [['Nothing to report', '0']]);
    t.eq(H.summaryRows(null), []);
    t.eq(H.summaryWarnings(lf), []);
    t.eq(H.summaryWarnings({ warnings: 'not a list' }), []);
    t.eq(H.label('no_injection_time'), 'No injection time');
    t.eq(H.label('csv_rows'), 'CSV rows');

    // ── one line for a job, running or ended ──
    const running = { kind: 'load-folder', state: 'running',
                      progress: { phase: 'submit', done: 3, total: 9 } };
    t.eq(H.jobView(running), { title: 'Folder load', line: 'Submitting CDFs: 3 of 9', cls: '',
                               running: true });
    t.eq(H.jobView({ kind: 'import-history-dry-run', state: 'done' }),
         { title: 'Dry run', line: 'Finished (nothing written)', cls: 'ok', running: false });
    t.eq(H.jobView({ kind: 'import-history', state: 'stopped' }).line,
         'Stopped; running it again resumes');
    t.eq(H.jobView({ kind: 'import-history', state: 'failed', error: 'OSError: gone' }),
         { title: 'History import', line: 'Failed: OSError: gone', cls: 'err', running: false });

    // ── Stop: only while this card's job runs ──
    t.eq(H.stopEnabled('ih', { kind: 'import-history-dry-run', state: 'running' }), true);
    t.eq(H.stopEnabled('ih', { kind: 'import-history', state: 'done' }), false);
    t.eq(H.stopEnabled('lf', { kind: 'import-history', state: 'running' }), false);
    t.eq(H.stopEnabled('lf', null), false);

    // ── picking a running job up from the feed (no password needed) ──
    const feed = [
        { id: 'reprocess:1', kind: 'reprocess', state: 'running' },
        { id: 'import-history-dry-run:4', kind: 'import-history-dry-run', state: 'running',
          progress: { done: null, total: null, text: 'Scanning the folder' }, by: 'Ryan C' },
        { id: 'load-folder:2', kind: 'load-folder', state: 'done', progress: null },
    ];
    t.eq(H.feedTaskFor('ih', feed).id, 'import-history-dry-run:4');
    t.eq(H.feedTaskFor('lf', feed).id, 'load-folder:2');
    t.eq(H.feedTaskFor('diag', feed), null);
    t.eq(H.feedLine(feed[1]), 'Dry run running · Scanning the folder · started by Ryan C');
    t.eq(H.feedLine(feed[2]), 'Folder load finished');
};
