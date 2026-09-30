// Hub admin "Purge instrument data": when Start is allowed, what the preview says.
const P = require('../../static/js/purge.js');

module.exports = (t) => {
    const pv = {
        ok: true, problems: [], instrument: 'gc1', instrument_name: 'GC-1', scope: 'all',
        confirm_text: 'PURGE GC-1', samples: 1234,
        tables: { samples: 1234, sample_results: 1300, export_rows: 900, jobs: 0 },
        files: { move: 1200, bytes: 5 * 1024 * 1024, missing: 2, outside: 0 },
        kept_samples: [{ id: 7, lab_id: 'Blank', reason: 'kept: it is the blank a kept result (sample 9) subtracted' }],
        kept_files: [{ path: 'cdf/gc1/2026/09/CAL_3.CDF', reason: 'the calibration CDF of GC-1' }],
        kept_files_total: 1, appended: 900, warning: '900 of these samples were already appended',
    };

    // Start: exact text, same instrument and scope, something to purge, no problems
    t.eq(P.canStart(pv, 'PURGE GC-1', 'gc1', 'all'), true);
    t.eq(P.canStart(pv, 'PURGE GC-1 ', 'gc1', 'all'), false);
    t.eq(P.canStart(pv, 'purge GC-1', 'gc1', 'all'), false);
    t.eq(P.canStart(pv, 'PURGE GC-1', 'gc2', 'all'), false);
    t.eq(P.canStart(pv, 'PURGE GC-1', 'gc1', 'backfill'), false);
    t.eq(P.canStart(null, 'PURGE GC-1', 'gc1', 'all'), false);
    t.eq(P.canStart(Object.assign({}, pv, { ok: false }), 'PURGE GC-1', 'gc1', 'all'), false);
    t.eq(P.canStart(Object.assign({}, pv, { samples: 0 }), 'PURGE GC-1', 'gc1', 'all'), false);
    t.eq(P.canStart(pv, undefined, 'gc1', 'all'), false);

    const lines = P.previewLines(pv).map((l) => l.text);
    t.eq(lines[0].startsWith('1,234 samples of GC-1 (all)'), true);
    t.eq(lines.some((l) => l.includes('sample_results: 1300') && !l.includes('jobs')), true);
    t.eq(lines.some((l) => l.includes('1,200 CDF files (5.0 MB)') && l.includes('never deleted')
        && l.includes('2 files already missing')), true);
    t.eq(lines.some((l) => l.includes('Kept: sample 7 (Blank)')), true);
    t.eq(lines.some((l) => l.includes('cdf/gc1/2026/09/CAL_3.CDF: the calibration CDF of GC-1')), true);
    t.eq(lines.some((l) => l.includes('already appended')), true);
    t.eq(lines[lines.length - 1], 'Type PURGE GC-1 below to confirm.');

    const none = P.previewLines(Object.assign({}, pv, { samples: 0, scope: 'backfill' }));
    t.eq(none.length, 1);
    t.eq(none[0].text, 'Nothing to purge: GC-1 has no backfill (imported history) samples.');
    const bad = P.previewLines(Object.assign({}, pv, { ok: false, problems: ['unsafe db'] }));
    t.eq(bad[0], { text: 'unsafe db', cls: 'err' });
    t.eq(P.previewLines(null), []);

    t.eq(P.jobLine({ kind: 'load-folder', state: 'running' }), '');
    t.eq(P.jobLine({ kind: 'purge', state: 'running', params: { instrument: 'gc1', scope: 'all' },
        progress: { phase: 'moving files', done: 3, total: 10 } }),
        'Purge gc1 (all): running — moving files 3/10');
    t.eq(P.jobLine({ kind: 'purge', state: 'done', params: { instrument: 'gc1', scope: 'all' },
        summary: { samples: 2, files: { moved: 1 }, purged_folder: 'P', backup: 'B' } }),
        'Purge gc1 (all): done — 2 samples purged, 1 file moved to P; backup B');
    t.eq(P.jobLine({ kind: 'purge', state: 'done', params: { instrument: 'gc1', scope: 'all' },
        summary: { nothing_to_do: true } }), 'Purge gc1 (all): done — nothing to purge');
    t.eq(P.jobLine({ kind: 'purge', state: 'failed', params: { instrument: 'gc1', scope: 'all' },
        error: 'PurgeError: busy' }), 'Purge gc1 (all): failed — PurgeError: busy');

    // The finished state names its warnings and the files that could not be moved.
    const warned = { samples: 3, files: { moved: 2, failed: [{ path: 'cdf/gc1/a.CDF', error: 'denied' }],
        failed_total: 1 }, purged_folder: 'P', backup: 'B', completed_with_warnings: true,
        warnings: ['1 CDF(s) could not be moved'] };
    t.eq(P.jobLine({ kind: 'purge', state: 'done', params: { instrument: 'gc1', scope: 'all' },
        summary: warned }),
        'Purge gc1 (all): completed with warnings — 3 samples purged, 2 files moved to P; backup B');
    t.eq(P.finishedLines(warned), [
        { text: 'Warning: 1 CDF(s) could not be moved', cls: 'warn' },
        { text: 'Not moved: cdf/gc1/a.CDF (denied)', cls: 'warn' }]);
    t.eq(P.finishedLines({ warnings: [], files: { failed: [] } }), []);
    t.eq(P.finishedLines(null), []);

    // The newest journal, shown on page load when no purge job is in memory
    // (e.g. one a restart finished or abandoned).
    t.eq(P.journalLine(null), '');
    t.eq(P.journalLine({ instrument: 'gc1', scope: 'all', state: 'done', recovered: true,
        samples: 5, finished_at: '2026-09-30T01:02:03' }),
        'Last purge: gc1 (all) done, finished after a restart — 5 samples, 2026-09-30T01:02:03');
    t.eq(P.journalLine({ instrument: 'gc1', scope: 'backfill', state: 'abandoned',
        reason: 'power cut', finished_at: 'T' }),
        'Last purge: gc1 (backfill) abandoned — nothing was removed (power cut), T');

    t.eq(P.instrumentOptions({ instruments: [{ id: 'gc1', name: 'GC-1' }, { id: 'gc2', name: 'gc2' },
        { id: 'gc3' }] }), [{ value: 'gc1', text: 'GC-1 (gc1)' }, { value: 'gc2', text: 'gc2' },
        { value: 'gc3', text: 'gc3' }]);
    t.eq(P.instrumentOptions(null), []);

    // Without the admin password the status is reduced (GET /api/purge/status):
    // counts instead of texts and paths, the name without the address.
    const pub = { kind: 'purge', state: 'done', params: { instrument: 'gc1', scope: 'all' },
        by: 'Ryan C', summary: { samples: 4, files: { moved: 3, failed: 1 }, warnings: 2,
            completed_with_warnings: true } };
    t.eq(P.jobLine(pub),
        'Purge gc1 (all): completed with warnings — 4 samples purged, 3 files moved, by Ryan C');
    t.eq(P.finishedLines(pub.summary), [
        { text: '2 warnings and 1 file not moved: enter the admin password and press Show ' +
            'progress to see them', cls: 'warn' }]);
    t.eq(P.finishedLines({ warnings: 0, files: { failed: 0 } }), []);
};
