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
};
