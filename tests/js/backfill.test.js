// v4.0.1: the Backfill section's pure logic (static/js/backfill_logic.js):
// selecting many rows (select all, shift-click range, drag), pruning after a
// release or a live update, chunking releases, and why each row is backfill.
const B = require('../../static/js/backfill_logic.js');

module.exports = (t) => {
    const order = [9, 8, 7, 6, 5, 4];               // newest injection first, as listed
    const selectable = new Set([9, 8, 7, 5, 4]);    // 6 is not final (or already released)
    const ids = (set) => Array.from(set).sort((a, b) => a - b);

    // ── a range between two rows, in list order, either direction
    t.eq(B.rangeIds(order, 8, 5), [8, 7, 6, 5]);
    t.eq(B.rangeIds(order, 5, 8), [8, 7, 6, 5]);
    t.eq(B.rangeIds(order, 7, 7), [7]);
    t.eq(B.rangeIds(order, 99, 7), [7]);             // the anchor left the list: just this row
    t.eq(B.rangeIds(order, 7, 99), []);

    // ── setRange: the range takes one state; rows that can't be selected are skipped
    t.eq(ids(B.setRange(new Set(), order, 8, 5, true, selectable)), [5, 7, 8]);
    t.eq(ids(B.setRange(new Set([9, 8, 7, 5]), order, 7, 5, false, selectable)), [8, 9]);
    const base = new Set([4]);
    t.eq(ids(B.setRange(base, order, 9, 7, true, selectable)), [4, 7, 8, 9]);
    t.eq(ids(base), [4]);                           // never mutates its input

    // ── drag: always applied to the selection as it was when the drag began,
    //    so dragging back over rows gives them their old state again
    const start = new Set([7]);                     // 7 was selected, 9 and 8 were not
    const down = B.setRange(start, order, 9, 5, true, selectable);
    t.eq(ids(down), [5, 7, 8, 9]);
    const back = B.setRange(start, order, 9, 8, true, selectable);
    t.eq(ids(back), [7, 8, 9]);                     // 5 is back to unselected
    // the first row's new state wins: starting on a selected row deselects
    t.eq(ids(B.setRange(new Set([9, 8, 7]), order, 8, 9, false, selectable)), [7]);

    // ── the header checkbox: none / some (indeterminate) / all of the rows shown
    t.eq(B.headerState(new Set(), selectable), 'none');
    t.eq(B.headerState(new Set([9]), selectable), 'some');
    t.eq(B.headerState(new Set([9, 8, 7, 5, 4]), selectable), 'all');
    t.eq(B.headerState(new Set([1234]), selectable), 'none');   // not shown: doesn't count
    t.eq(B.headerState(new Set(), new Set()), 'none');
    t.eq(ids(B.toggleAll(new Set([9]), selectable)), [4, 5, 7, 8, 9]);
    t.eq(ids(B.toggleAll(new Set([9, 8, 7, 5, 4]), selectable)), []);

    // ── pruning: rows that disappear (released, changed, filtered out) drop out
    t.eq(ids(B.prune(new Set([9, 8, 6, 1234]), selectable)), [8, 9]);
    t.eq(ids(B.prune(new Set([9]), new Set())), []);

    // ── releases go at most 500 ids per call
    t.eq(B.RELEASE_MAX, 500);
    const many = Array.from({ length: 1201 }, (_, i) => i + 1);
    const parts = B.chunk(many, B.RELEASE_MAX);
    t.eq(parts.map(p => p.length), [500, 500, 201]);
    t.eq(parts[2][200], 1201);
    t.eq(B.chunk([], 500), []);

    // ── the count and the action
    t.eq(B.barText(0), { count: 'None selected', action: 'Release' });
    t.eq(B.barText(1), { count: '1 selected', action: 'Release 1' });
    t.eq(B.barText(1200), { count: '1,200 selected', action: 'Release 1,200' });
    t.eq(B.progressText(500, 1200), 'Releasing 500 of 1,200…');
    t.eq(B.progressText(0, 30), 'Releasing 0 of 30…');

    // ── why each row is backfill: the same comparison as store.is_backfill
    //    (injection_dt < live_since, the GC's clock; none set: everything is)
    const gc2 = { name: 'GC-2', live_since: '2026-09-30 14:30:00', live_since_set_at: '2026-09-30T19:31:00.000000+00:00' };
    t.eq(B.whyText({ injection_dt: '2026-09-30 14:02:00', received_at: '2026-09-30T19:02:30+00:00' }, gc2),
         'injected 14:02 · before GC-2 went live (Sep 30 14:30)');
    t.eq(B.whyText({ injection_dt: '2026-09-28 09:15:00', received_at: '2026-09-28T14:20:00+00:00' }, gc2),
         'injected Sep 28 09:15 · before GC-2 went live (Sep 30 14:30)');
    t.eq(B.whyText({ injection_dt: '2025-12-31 23:59:00' }, gc2),
         'injected Dec 31, 2025 23:59 · before GC-2 went live (Sep 30, 2026 14:30)');
    // not before live_since: live_since was set after the run arrived
    t.eq(B.whyText({ injection_dt: '2026-09-30 15:00:00', received_at: '2026-09-30T19:05:00.123456+00:00' }, gc2),
         'injected 15:00 · arrived before GC-2 went live');
    // no record of when live_since was set: the same (it was set later)
    t.eq(B.whyText({ injection_dt: '2026-09-30 15:00:00', received_at: '2026-09-30T19:05:00+00:00' },
                   Object.assign({}, gc2, { live_since_set_at: null })),
         'injected 15:00 · arrived before GC-2 went live');
    // arrived after live_since was set and not before it: loaded as backfill (a folder load)
    t.eq(B.whyText({ injection_dt: '2026-09-30 15:00:00', received_at: '2026-09-30T20:00:00+00:00' }, gc2),
         'injected 15:00 · loaded as backfill');
    // exactly at live_since is not before it (strictly less than)
    t.eq(B.whyText({ injection_dt: '2026-09-30 14:30:00', received_at: '2026-09-30T19:00:00+00:00' }, gc2),
         'injected 14:30 · arrived before GC-2 went live');
    // seconds and fractions count, as in Python's comparison
    t.eq(B.whyText({ injection_dt: '2026-09-30 14:29:59.5' }, gc2),
         'injected 14:29 · before GC-2 went live (Sep 30 14:30)');
    // no live_since at all
    t.eq(B.whyText({ injection_dt: '2026-09-28 09:15:00' }, { name: 'GC-2', live_since: null }),
         'injected Sep 28 09:15 · arrived before GC-2 had a live-since time');
    t.eq(B.whyText({ injection_dt: '2026-09-28T09:15:00' }, { name: 'GC-2', live_since: '' }),
         'injected Sep 28 09:15 · arrived before GC-2 had a live-since time');
    // a T separator or a bare minute in live_since compares the same way
    t.eq(B.whyText({ injection_dt: '2026-09-30 14:02:00' }, { name: 'GC-2', live_since: '2026-09-30T14:30' }),
         'injected 14:02 · before GC-2 went live (Sep 30 14:30)');
    // an unreadable injection time: just the reason
    t.eq(B.whyText({ injection_dt: 'garbage' }, { name: 'GC-2', live_since: null }),
         'arrived before GC-2 had a live-since time');
};
