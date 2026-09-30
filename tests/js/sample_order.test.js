// static/js/sample_order.js (v4.0 lane E): the sample list's order and day
// grouping. Newest run (injection time, newest first, under day headings) or
// Lab ID (natural numeric); runs with no readable injection time go last.
const O = require('../../static/js/sample_order.js');

const S = (id, name, dt, source) => ({ sample_id: id, lab_id: name, name, display_name: name,
                                       injection_dt: dt, injection_dt_source: source || 'cdf' });

module.exports = (t) => {
    // ── natural compare: numbers as numbers, text case-insensitively ──
    const sorted = ['40318-RERUN-2', '40318', '9', '40319', '40318-rerun-10', 'AB12', 'ab2',
                    '40318-RERUN-3', 'blank', '0040317'].sort(O.naturalCompare);
    t.eq(sorted, ['9', '0040317', '40318', '40318-RERUN-2', '40318-RERUN-3', '40318-rerun-10',
                  '40319', 'ab2', 'AB12', 'blank']);
    t.eq(O.naturalCompare('40318', '40318'), 0);
    t.eq(O.naturalCompare('', '1') < 0, true);
    t.eq(O.naturalCompare(null, '1') < 0, true);

    // ── the injection time ──
    t.eq(O.parseInjection('2026-09-29 14:02:11'), { day: '2026-09-29', hm: '14:02', key: '2026-09-29 14:02:11' });
    t.eq(O.parseInjection('2026-09-29T14:02:11.5'), { day: '2026-09-29', hm: '14:02', key: '2026-09-29 14:02:11.5' });
    t.eq(O.parseInjection('garbage'), null);
    t.eq(O.parseInjection(null), null);
    t.eq(O.hasInjectionTime(S(1, 'a', '2026-09-29 14:02:11')), true);
    t.eq(O.hasInjectionTime(S(1, 'a', '2026-09-29 14:02:11', 'mtime')), false);   // the file's time
    t.eq(O.hasInjectionTime(S(1, 'a', 'bad')), false);

    t.eq(O.rowTime(S(1, 'a', '2026-09-29 14:02:11')),
         { text: '2026-09-29 14:02', title: 'Injected 2026-09-29 14:02:11' });
    t.eq(O.rowTime(S(1, 'a', '2026-09-29 14:02:11', 'mtime')),
         { text: 'file 2026-09-29 14:02',
           title: 'No injection time in the CDF; this is the file’s time, 2026-09-29 14:02:11' });
    t.eq(O.rowTime(S(1, 'a', null)), { text: '', title: 'No injection time' });

    // ── day labels, on the browser's calendar ──
    const now = new Date(2026, 8, 29, 9, 30);        // Tue 29 Sep 2026, local
    t.eq(O.dayLabel('2026-09-29', now), 'Today');
    t.eq(O.dayLabel('2026-09-28', now), 'Yesterday');
    t.eq(O.dayLabel('2026-09-24', now), 'Thu 24 Sep');
    t.eq(O.dayLabel('2025-12-31', now), 'Wed 31 Dec 2025');
    t.eq(O.dayLabel('2026-09-30', now), 'Wed 30 Sep');   // a GC clock ahead: just the date

    // ── Newest run: injection time, newest first; no injection time last ──
    const files = [
        S(1, '40304', '2026-09-28 16:45:10'),
        S(2, '40318', '2026-09-29 08:00:00'),
        S(3, '9', '2026-09-24 10:00:00'),
        S(4, 'X-NO-TIME', '2026-09-29 12:00:00', 'mtime'),
        S(5, '40318-RERUN-2', '2026-09-29 09:15:00'),
        S(6, '40304', '2026-09-28 14:23:00'),
    ];
    t.eq(O.sortSamples(files, 'newest').map(f => f.sample_id), [5, 2, 1, 6, 3, 4]);
    t.eq(files.map(f => f.sample_id), [1, 2, 3, 4, 5, 6]);            // not modified
    const groups = O.groupSamples(files, 'newest', now);
    t.eq(groups.map(g => [g.label, g.items.map(f => f.sample_id)]), [
        ['Today', [5, 2]], ['Yesterday', [1, 6]], ['Thu 24 Sep', [3]],
        ['No injection time', [4]]]);
    t.eq(groups.map(g => g.key), ['2026-09-29', '2026-09-28', '2026-09-24', 'none']);

    // ── Lab ID: natural numeric, runs of one ID oldest first; one flat group ──
    t.eq(O.sortSamples(files, 'lab').map(f => f.sample_id), [3, 6, 1, 2, 5, 4]);
    const flat = O.groupSamples(files, 'lab', now);
    t.eq(flat.length, 1);
    t.eq(flat[0].label, null);
    t.eq(flat[0].items.map(f => f.sample_id), [3, 6, 1, 2, 5, 4]);

    // an unknown mode is Newest run; empty lists group to nothing
    t.eq(O.sortSamples(files, 'bogus').map(f => f.sample_id), [5, 2, 1, 6, 3, 4]);
    t.eq(O.groupSamples([], 'newest', now), []);

    // ── the choice is remembered per browser; storage may throw ──
    const mem = { v: {}, getItem(k) { return this.v[k] === undefined ? null : this.v[k]; },
                  setItem(k, v) { this.v[k] = String(v); } };
    t.eq(O.loadSortMode(mem), 'newest');
    O.saveSortMode('lab', mem);
    t.eq(mem.v[O.SORT_KEY], 'lab');
    t.eq(O.loadSortMode(mem), 'lab');
    mem.v[O.SORT_KEY] = 'weird';
    t.eq(O.loadSortMode(mem), 'newest');
    const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
    t.eq(O.loadSortMode(broken), 'newest');
    O.saveSortMode('lab', broken);                                 // no throw
    t.eq(O.loadSortMode(null), 'newest');
};
