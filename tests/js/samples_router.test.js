// samples_router.js (v5.0.0 lane S): the address bar is the link. The URL ↔
// the Samples page's view: which sample, which view (Overview · Compare ·
// Data), ?standard= on Compare, and the list's filters, search and sort.
const R = require('../../static/js/samples_router.js');

const EMPTY = { instrument: [], status: [], q: '', sort: 'newest', notsent: false };

module.exports = (t) => {
    // ── parse ────────────────────────────────────────────────────────────
    t.eq(R.parse('/', ''), { sampleId: null, view: 'overview', standard: null, filters: EMPTY, legacy: false });
    t.eq(R.parse('/samples', ''), { sampleId: null, view: 'overview', standard: null, filters: EMPTY, legacy: false });
    t.eq(R.parse('/samples/', '').sampleId, null);
    t.eq(R.parse('/samples/12', ''), { sampleId: 12, view: 'overview', standard: null, filters: EMPTY, legacy: false });
    t.eq(R.parse('/samples/12/', '').sampleId, 12);
    t.eq(R.parse('/samples/12/compare', '?standard=Diesel%20B'),
         { sampleId: 12, view: 'compare', standard: 'Diesel B', filters: EMPTY, legacy: false });
    t.eq(R.parse('/samples/12/compare', '?standard=Jet+A').standard, 'Jet A');
    t.eq(R.parse('/samples/12/compare', '?standard=').standard, null);
    // ?standard= belongs to Compare only
    t.eq(R.parse('/samples/12/data', '?standard=X'),
         { sampleId: 12, view: 'data', standard: null, filters: EMPTY, legacy: false });
    t.eq(R.parse('/samples/0', '').sampleId, null);
    t.eq(R.parse('/samples/x', '').sampleId, null);
    t.eq(R.parse('/samples/12/other', '').view, 'overview');
    t.eq(R.parse('/samples/99999999999999999999', '').sampleId, null);   // beyond a safe integer

    // filters: comma lists, known statuses only, trimmed search, sort, notsent
    t.eq(R.parse('/samples', '?instrument=gc2,gc1&status=held,error,bogus&q=%20403%20&sort=lab&notsent=1').filters,
         { instrument: ['gc1', 'gc2'], status: ['held', 'error'], q: '403', sort: 'lab', notsent: true });
    t.eq(R.parse('/', '?status=final&status=held').filters.status, ['final', 'held']);
    t.eq(R.parse('/', '?sort=weird&notsent=0').filters, EMPTY);
    t.eq(R.parse('/samples/5', '?instrument=gc1&q=40%26').filters,
         { instrument: ['gc1'], status: [], q: '40&', sort: 'newest', notsent: false });
    // the old links: /?sample=<id> and the classic tab names
    t.eq(R.parse('/', '?sample=7'), { sampleId: 7, view: 'overview', standard: null, filters: EMPTY, legacy: true });
    t.eq(R.parse('/', '?sample=7&tab=analysis&standard=D').view, 'compare');
    t.eq(R.parse('/', '?sample=7&tab=analysis&standard=D').standard, 'D');
    t.eq(R.parse('/', '?sample=7&tab=data').view, 'data');
    t.eq(R.parse('/', '?sample=nope').sampleId, null);
    t.eq(R.parse(undefined, undefined).sampleId, null);

    // ── build ────────────────────────────────────────────────────────────
    t.eq(R.build({ sampleId: null, view: 'overview', filters: EMPTY }), '/samples');
    t.eq(R.build({ sampleId: 12, view: 'overview', filters: EMPTY }), '/samples/12');
    t.eq(R.build({ sampleId: 12, view: 'data', filters: EMPTY }), '/samples/12/data');
    t.eq(R.build({ sampleId: 12, view: 'compare', standard: 'Diesel B', filters: EMPTY }),
         '/samples/12/compare?standard=Diesel%20B');
    t.eq(R.build({ sampleId: 12, view: 'compare', standard: null, filters: EMPTY }), '/samples/12/compare');
    t.eq(R.build({ sampleId: 12, view: 'data', standard: 'X', filters: EMPTY }), '/samples/12/data');
    t.eq(R.build({ sampleId: 12, view: 'overview',
                   filters: { instrument: ['gc2', 'gc1'], status: ['error', 'held'], q: ' 40& ', sort: 'lab', notsent: true } }),
         '/samples/12?instrument=gc1%2Cgc2&status=held%2Cerror&q=40%26&sort=lab&notsent=1');
    t.eq(R.build({ sampleId: 3, view: 'compare', standard: 'S',
                   filters: { instrument: ['gc1'], status: [], q: '', sort: 'newest', notsent: false } }),
         '/samples/3/compare?standard=S&instrument=gc1');
    t.eq(R.build({}), '/samples');

    // round trip
    for (const url of ['/samples', '/samples/4', '/samples/4/data?instrument=gc1',
                       '/samples/4/compare?standard=Jet%20A&status=held%2Cerror&q=403&sort=lab&notsent=1']) {
        const [p, q] = url.split('?');
        t.eq(R.build(R.parse(p, q ? '?' + q : '')), url);
    }

    // ── what changed between two views (push, replace or nothing) ────────
    const a = R.parse('/samples/4', '');
    t.eq(R.sameUrl(a, R.parse('/samples/4', '')), true);
    t.eq(R.sameUrl(a, R.parse('/samples/5', '')), false);

    // ── the server's status list for the chips ──────────────────────────
    t.eq(R.statusList(['held']), ['awaiting_calibration', 'pending_corrections', 'other_method', 'review_method', 'raw_only']);
    t.eq(R.statusList(['error', 'processing']), ['error', 'received']);
    t.eq(R.statusList([]), []);
    t.eq(R.statusGroup('awaiting_calibration'), 'held');
    t.eq(R.statusGroup('received'), 'processing');
    t.eq(R.statusGroup('final'), 'final');
    t.eq(R.statusGroup('error'), 'error');
    t.eq(R.statusGroup('nonsense'), 'held');

    // /api/files and /api/files/ids take the same query
    t.eq(R.filesQuery({ instrument: ['gc1'], status: ['held'], q: '40 3', sort: 'lab', notsent: true }),
         'instrument=gc1&status=awaiting_calibration%2Cpending_corrections%2Cother_method%2Creview_method%2Craw_only&q=40%203&notsent=1');
    t.eq(R.filesQuery(EMPTY), '');
    t.eq(R.filtersActive(EMPTY), false);
    t.eq(R.filtersActive(Object.assign({}, EMPTY, { sort: 'lab' })), false);   // order is not a filter
    t.eq(R.filtersActive(Object.assign({}, EMPTY, { q: 'x' })), true);
};
