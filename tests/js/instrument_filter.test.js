// v2.0.0 RC fixes: the main page names each sample's instrument (two
// instruments can hold the same lab ID), filters by instrument (remembered
// per browser), the re-process modal needs an instrument, and a sample's
// review_note is visible.
const s = require('../../static/js/samples.js');

module.exports = (t) => {
    const names = { gc1: 'GC-1', gc2: 'GC-2 (FID)' };
    const a = { sample_id: 1, lab_id: '40304', name: '40304', display_name: '40304', instrument: 'gc1' };
    const b = { sample_id: 2, lab_id: '40304', name: '40304', display_name: '40304', instrument: 'gc2' };

    // ── the instrument badge: the instrument's name, its id in the tooltip
    t.eq(s.instrumentName('gc2', names), 'GC-2 (FID)');
    t.eq(s.instrumentName('gc9', names), 'gc9');            // unknown id: the id itself
    t.eq(s.instrumentName('gc1', null), 'gc1');
    const badge = s.instrumentBadge(b, names);
    t.eq(badge.text, 'GC-2 (FID)');
    t.eq(badge.cls, 'instrument-badge');
    t.eq(badge.title.includes('gc2'), true);
    t.eq(s.instrumentBadge({ sample_id: 3 }, names), null);  // no instrument: no badge

    // ── trace / curve labels: the same lab ID on two instruments stays distinct
    t.eq(s.traceLabel(a, names), '40304 · GC-1');
    t.eq(s.traceLabel(b, names), '40304 · GC-2 (FID)');
    t.eq(s.traceLabel({ name: 'X', display_name: 'X (2)', instrument: 'gc1' }, names), 'X (2) · GC-1');
    t.eq(s.traceLabel({ name: 'X' }, names), 'X');

    // ── the filter select: All, then each instrument by name
    t.eq(s.instrumentFilterOptions([{ id: 'gc1', name: 'GC-1' }, { id: 'gc2', name: '' }]),
        [{ value: '', text: 'All instruments' }, { value: 'gc1', text: 'GC-1' },
         { value: 'gc2', text: 'gc2' }]);
    t.eq(s.instrumentFilterOptions(null), [{ value: '', text: 'All instruments' }]);

    // ── the remembered choice is kept only while that instrument exists
    t.eq(s.restoreInstrumentFilter('gc2', ['gc1', 'gc2']), 'gc2');
    t.eq(s.restoreInstrumentFilter('gc7', ['gc1', 'gc2']), null);
    t.eq(s.restoreInstrumentFilter('', ['gc1']), null);
    t.eq(s.restoreInstrumentFilter(null, ['gc1']), null);
    t.eq(s.restoreInstrumentFilter('gc2', []), 'gc2');       // list not loaded yet: keep it

    // ── client-side filter of the loaded page
    t.eq(s.filterByInstrument([a, b], 'gc2'), [b]);
    t.eq(s.filterByInstrument([a, b], null), [a, b]);
    t.eq(s.filterByInstrument([a, b], ''), [a, b]);

    // the server search carries the instrument filter
    t.eq(s.filesUrl({ q: '40304', instrument: 'gc2' }, 5000),
        '/api/files?limit=5000&q=40304&instrument=gc2');

    // ── re-process modal: the instrument a typed lab-ID list resolves against
    t.eq(s.reprocessDefaultInstrument(['gc1', 'gc2'], 'gc2'), 'gc2');   // the list's filter
    t.eq(s.reprocessDefaultInstrument(['gc1', 'gc2'], null), '');       // must choose
    t.eq(s.reprocessDefaultInstrument(['gc1'], null), 'gc1');           // only one
    t.eq(s.reprocessDefaultInstrument(['gc1', 'gc2'], 'gc9'), '');      // stale filter
    t.eq(s.reprocessDefaultInstrument([], null), '');
    t.eq(typeof s.reprocessInstrumentError(''), 'string');
    t.eq(s.reprocessInstrumentError(null).length > 0, true);
    t.eq(s.reprocessInstrumentError('gc1'), null);

    // ── review_note: a final sample gets a "Review" badge carrying the note
    const rv = s.statusBadge({ status: 'final', review_note: 'blank arrived late' });
    t.eq(rv.text, 'Review');
    t.eq(rv.cls, 'status-badge status-review');
    t.eq(rv.title.includes('blank arrived late'), true);
    // ...a held sample keeps its hold badge, the note added to the tooltip
    const held = s.statusBadge({ status: 'error', error: 'boom', review_note: 'check me' });
    t.eq(held.text, 'Error');
    t.eq(held.title.includes('boom') && held.title.includes('check me'), true);
    // ...an unreleased backfill keeps "Backfill", with the note in the tooltip
    const bf = s.statusBadge({ status: 'final', backfill: 1, released: false, review_note: 'n1' });
    t.eq(bf.text, 'Backfill');
    t.eq(bf.title.includes('n1'), true);
    // no note: unchanged
    t.eq(s.statusBadge({ status: 'final' }), null);
    t.eq(s.reviewNoteTitle({ review_note: 'x' }).includes('x'), true);
    t.eq(s.reviewNoteTitle({ review_note: null }), null);
    t.eq(s.reviewNoteTitle({ review_note: '  ' }), null);
};
