// Sample-list helpers (phase 2 T4): samples are addressed by sample_id,
// non-final statuses get a badge whose tooltip is the hold reason, and a
// corrected injection time gets a marker.
const { sampleUid, sampleIdsOf, statusBadge, timeCorrectedTitle, refusalSummary } =
    require('../../static/js/samples.js');

module.exports = (t) => {
    // uid is the sample id as a string (re-runs of one lab ID stay distinct)
    t.eq(sampleUid({ sample_id: 12, uid: '12' }), '12');
    t.eq(sampleUid({ sample_id: 7 }), '7');
    t.eq(sampleIdsOf([{ sample_id: 3 }, { sample_id: 5 }, { sample_id: 3 }, {}]), [3, 5]);

    // final (not backfill) → no badge
    t.eq(statusBadge({ status: 'final', backfill: 0 }), null);

    // every non-final status has a badge; the title carries samples.error
    const held = statusBadge({ status: 'awaiting_calibration',
        error: 'No calibration CDF is set for this instrument.' });
    t.eq(held.text, 'Awaiting calibration');
    t.eq(held.cls, 'status-badge status-awaiting_calibration');
    t.eq(held.title, 'No calibration CDF is set for this instrument.');
    for (const st of ['pending_corrections', 'other_method', 'review_method', 'error',
                      'raw_only', 'received']) {
        const b = statusBadge({ status: st });
        t.eq(typeof b.text, 'string');
        t.eq(b.cls, `status-badge status-${st}`);
        t.eq(b.title.length > 0, true);
    }
    t.eq(statusBadge({ status: 'pending_corrections' }).text, 'Pending corrections');
    t.eq(statusBadge({ status: 'error', error: 'boom' }).title, 'boom');
    // other_method names the method it was classified by
    t.eq(statusBadge({ status: 'other_method', method_name: 'D7096.M' }).title.includes('D7096.M'), true);
    // an unknown future status still gets a badge
    t.eq(statusBadge({ status: 'quarantined' }).text, 'quarantined');

    // final backfill: not exported until released
    const bf = statusBadge({ status: 'final', backfill: 1, released: false });
    t.eq(bf.text, 'Backfill');
    t.eq(bf.cls, 'status-badge status-backfill');
    t.eq(statusBadge({ status: 'final', backfill: 1, released: true }), null);

    // time_corrected marker
    t.eq(timeCorrectedTitle({ time_corrected: 0 }), null);
    t.eq(timeCorrectedTitle({ time_corrected: 1 }).includes('corrected'), true);

    // refusals name the samples by their list label
    const files = [{ sample_id: 4, display_name: '40299' }];
    t.eq(refusalSummary('Not exported', [{ sample_id: 4, error: 'backfill' }], files),
        'Not exported: 40299 (backfill)');
    t.eq(refusalSummary('Not exported', [{ sample_id: 9, error: 'x' }, { sample_id: 4, error: 'y' }], files),
        'Not exported: #9 (x); 40299 (y)');

    // ── review fixes ───────────────────────────────────────────────────
    const s = require('../../static/js/samples.js');
    // search beyond the loaded page only when there is more on the server
    t.eq(s.needsServerSearch('4030', 12000, 5000), true);
    t.eq(s.needsServerSearch('  ', 12000, 5000), false);
    t.eq(s.needsServerSearch('4030', 5000, 5000), false);
    // the URL carries the search and the list's filters, encoded
    t.eq(s.filesUrl({ q: 'AB 1&2', instrument: 'gc1', status: ['final', 'error'] }, 5000),
        '/api/files?limit=5000&q=AB%201%262&instrument=gc1&status=final%2Cerror');
    t.eq(s.filesUrl({}, 500), '/api/files?limit=500');
    // "showing N of M"
    t.eq(s.countLabel(5000, 12000), 'showing 5000 of 12000');
    t.eq(s.countLabel(40, 40), '');
    // the curve is fetched only for a sample with a revision
    t.eq(s.curveFetchable({ current_revision: 2 }), true);
    t.eq(s.curveFetchable({ current_revision: null }), false);
    // escapeHtml escapes quotes too (attribute contexts)
    t.eq(s.escapeHtml(`<a href="x" title='y'>&`), '&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;');
    t.eq(s.escapeHtml(null), '');

    // ── v3.1 live updates: fetch only the changed rows, merge them in place ──
    t.eq(s.filesUrl({ ids: [3, 9], instrument: 'gc1' }, 5000),
         '/api/files?limit=5000&instrument=gc1&ids=3%2C9');
    t.eq(s.filesUrl({ ids: [] }, 10), '/api/files?limit=10');

    const row = (id, dt, over) => Object.assign({ sample_id: id, uid: String(id), injection_dt: dt,
                                                  status: 'final' }, over || {});
    const list = [row(5, '2026-09-25 12:00:00'), row(4, '2026-09-25 11:00:00'),
                  row(2, '2026-09-24 10:00:00')];
    // a changed row is replaced in place; a new one lands in injection order
    let m = s.mergeChangedRows(list, [4, 7], [row(4, '2026-09-25 11:00:00', { status: 'error' }),
                                             row(7, '2026-09-25 11:30:00', { status: 'received' })]);
    t.eq(m.files.map(f => f.sample_id), [5, 7, 4, 2]);
    t.eq(m.files[2].status, 'error');
    t.eq(m.added, 1);
    t.eq(m.removed, 0);
    // the input list is not modified
    t.eq(list.map(f => f.sample_id), [5, 4, 2]);
    t.eq(list[1].status, 'final');
    // a changed id the server no longer returns (filtered out, or gone) is removed
    m = s.mergeChangedRows(list, [4], []);
    t.eq(m.files.map(f => f.sample_id), [5, 2]);
    t.eq(m.removed, 1);
    // newest first; ties on the time: the higher id first (as the server orders)
    m = s.mergeChangedRows(list, [6], [row(6, '2026-09-25 12:00:00')]);
    t.eq(m.files.map(f => f.sample_id), [6, 5, 4, 2]);
    m = s.mergeChangedRows(list, [1], [row(1, '2026-09-30 00:00:00')]);
    t.eq(m.files.map(f => f.sample_id), [1, 5, 4, 2]);
    // an older sample than the page holds, when the page is full, is not added
    m = s.mergeChangedRows(list, [1], [row(1, '2020-01-01 00:00:00')], { pageFull: true });
    t.eq(m.files.map(f => f.sample_id), [5, 4, 2]);
    t.eq(m.added, 0);
    t.eq(m.skipped, 1);
    // nothing changed
    m = s.mergeChangedRows(list, [], []);
    t.eq(m.files.map(f => f.sample_id), [5, 4, 2]);
    t.eq(m.orderChanged, false);
    // a replaced row whose injection time changed (a conflict Replace) moves
    m = s.mergeChangedRows(list, [2], [row(2, '2026-09-26 08:00:00')]);
    t.eq(m.files.map(f => f.sample_id), [2, 5, 4]);
    t.eq(m.orderChanged, true);
    t.eq([m.added, m.removed], [0, 0]);
    // a plain in-place update keeps the order and says so
    m = s.mergeChangedRows(list, [4], [row(4, '2026-09-25 11:00:00', { status: 'error' })]);
    t.eq(m.orderChanged, false);
    t.eq(m.replaced.map(f => f.sample_id), [4]);
    // additions and removals change the order
    t.eq(s.mergeChangedRows(list, [4], []).orderChanged, true);
    t.eq(s.mergeChangedRows(list, [7], [row(7, '2026-09-25 11:30:00')]).orderChanged, true);
    // an id repeated in the answer is merged once (no duplicate rows)
    m = s.mergeChangedRows(list, [7], [row(7, '2026-09-25 11:30:00'), row(7, '2026-09-25 11:30:00')]);
    t.eq(m.files.map(f => f.sample_id), [5, 7, 4, 2]);
};
