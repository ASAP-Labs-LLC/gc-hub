// Analysis-tab comments (phase 4): the pure helpers in static/js/comments.js.
const C = require('../../static/js/comments.js');

module.exports = (t) => {
    // the author is the signed-in account (rev 2: no initials box)
    t.eq(C.commentingAs('Ryan C'), 'Commenting as Ryan C');
    t.eq(C.commentingAs('  '), '');
    t.eq(C.commentingAs(null), '');
    t.eq('normInitials' in C || 'validInitials' in C || 'INITIALS_KEY' in C, false);

    // Plotly annotation text is HTML-ish: < > & must be escaped
    t.eq(C.plotlySafe('<img src=x onerror=alert(1)> & <b>'),
        '&lt;img src=x onerror=alert(1)&gt; &amp; &lt;b&gt;');
    t.eq(C.plotlySafe(null), '');

    const list = [
        { id: 1, source: 'free', text: 'Looks fine', initials: 'RB',
          created_at: '2026-09-29T14:05:00.000000+00:00', t0: null, t1: null },
        { id: 2, source: 'annotation', text: '<b>hump</b>', initials: 'JD',
          created_at: '2026-09-29T14:06:00.000000+00:00', t0: 1.2, t1: 1.5 },
        { id: 3, source: 'annotation', text: 'A very long annotation comment that goes on and on',
          initials: 'JD', created_at: '2026-09-29T14:07:00.000000+00:00', t0: 2, t1: 2.5 },
        { id: 4, source: 'preset', text: 'Re-run requested.', initials: 'RB',
          created_at: '2026-09-29T14:08:00.000000+00:00', t0: null, t1: null },
    ];
    t.eq(C.annotationComments(list).map(c => c.id), [2, 3]);

    // shapes/labels only for annotation comments, marked so a redraw can find them
    const o = C.annotationOverlay(list);
    t.eq(o.shapes.length, 2);
    t.eq(o.labels.length, 2);
    t.eq([o.shapes[0].x0, o.shapes[0].x1], [1.2, 1.5]);
    t.eq(o.shapes[0].fillcolor, C.ANNOT_FILL);
    t.eq(o.labels[0].bgcolor, C.ANNOT_LABEL_BG);
    t.eq(o.labels[0].text, '&lt;b&gt;hump&lt;/b&gt;');
    t.eq(o.labels[0].x, (1.2 + 1.5) / 2);
    // long labels are cut before escaping (never inside an entity)
    t.eq(o.labels[1].text, 'A very long annotation comment...');
    t.eq(C.annotationOverlay([]).shapes, []);
    t.eq(C.isAnnotationShape({ fillcolor: C.ANNOT_FILL }), true);
    t.eq(C.isAnnotationShape({ fillcolor: 'rgba(1,2,3,0.5)' }), false);
    t.eq(C.isAnnotationLabel({ bgcolor: C.ANNOT_LABEL_BG }), true);

    // list line: initials and date; annotations add their span
    t.eq(C.commentMeta(list[0]).startsWith('RB, '), true);
    t.eq(C.commentMeta(list[1]).includes('1.20–1.50 min'), true);
    t.eq(C.commentMeta(list[1]).includes('JD'), true);

    // Clear Annotations confirmation names the count
        t.eq(C.clearConfirmText(2, '40304').includes('on sample 40304'), true);

    // the list line prefers the account name, else the initials
    t.eq(C.commentMeta({ source: 'free', name: 'Ryan C', initials: 'RC',
                         created_at: '2026-09-29T14:07:00.000000+00:00' }).startsWith('Ryan C, '), true);

    // v6: free and preset comments are the sample's earlier notes (they print
    // under the conclusion); annotations are its marked regions on the chart
    t.eq(C.noteComments(list).map(c => c.id), [1, 4]);
    t.eq(C.noteComments(null), []);
    t.eq(C.clearConfirmText(2).includes('2 marked regions'), true);
    t.eq(C.clearConfirmText(1).includes('1 marked region '), true);
    t.eq(/comment/i.test(C.clearConfirmText(2, '40304')), false);
    t.eq(C.removeNoteConfirmText({ text: 'Re-run requested.' }),
        'Remove this note?\n\nRe-run requested.\n\nIt stays in the record as deleted and no longer prints on reports.');
    t.eq(C.removeRegionConfirmText({ text: 'Hump', t0: 1.2, t1: 1.5 }),
        'Remove the marked region "Hump" (1.20–1.50 min)?');
    // the Annotate menu's line for one region
    t.eq(C.regionLabel({ text: 'Hump', t0: 1.2, t1: 1.5 }), 'Hump · 1.20–1.50 min');
    t.eq(C.regionLabel({ text: 'x'.repeat(40), t0: 1, t1: 2 }), 'x'.repeat(30) + '… · 1.00–2.00 min');
};
