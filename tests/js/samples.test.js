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
};
