// v5.0.0 lane C: the report queue's pure part (static/js/report_queue.js):
// the sessionStorage store, the payloads it sends, and the upload wording.
const Q = require('../../static/js/report_queue.js');

function memStorage() {
    const m = new Map();
    return {
        getItem: k => (m.has(k) ? m.get(k) : null),
        setItem: (k, v) => { m.set(k, String(v)); },
        removeItem: k => { m.delete(k); },
        _m: m,
    };
}

module.exports = (t) => {
    const params = { quantile: 0.2, window: 301, sigma: 34, thresh_marginal: 100,
        thresh_moderate: 500, thresh_significant: 2000, x_max_min: 7, junk: 'x' };
    const ranges = [{ id: 3, label: 'Gas', c_start: '5', c_end: 11, color: '#3fb95044' }];
    const raw = { sample_id: 40, lab_id: '40329', standard_name: 'Diesel ULSD Std',
        conclusion: '  Edited.  ', params, ranges, bullets: 'never sent' };

    // an item keeps what a report needs, captured now; never bullets
    const it = Q.normalizeItem(raw, '2026-09-30T12:00:00Z');
    t.eq(it, { id: 's40', sample_id: 40, lab_id: '40329', sample_name: 'GC Analysis',
        standard_name: 'Diesel ULSD Std', conclusion: 'Edited.',
        params: { quantile: 0.2, window: 301, sigma: 34, thresh_marginal: 100,
                  thresh_moderate: 500, thresh_significant: 2000, x_max_min: 7 },
        ranges: [{ label: 'Gas', c_start: 5, c_end: 11, color: '#3fb95044' }],
        overlay_standards: [], added_at: '2026-09-30T12:00:00Z', sent_at: null });
    t.eq(Q.normalizeItem({ sample_id: 'x', standard_name: 'A' }), null);
    t.eq(Q.normalizeItem({ sample_id: 4 }), null);
    t.eq(Q.normalizeItem({ sample_id: '12', standard_name: 'A' }).sample_id, 12);
    // no ranges captured: the key is left out (the server's saved defaults)
    t.eq('ranges' in Q.normalizeItem({ sample_id: 1, standard_name: 'A' }), false);
    t.eq(Q.normalizeItem({ sample_id: 1, standard_name: 'A', ranges: [] }).ranges, []);

    // the payload is report_payload.js's, unchanged: params flattened, no bullets
    const p = Q.payloads([it])[0];
    t.eq(p.sample_id, 40);
    t.eq(p.standard_name, 'Diesel ULSD Std');
    t.eq(p.thresh_marginal, 100);
    t.eq(p.ranges, [{ label: 'Gas', c_start: 5, c_end: 11, color: '#3fb95044' }]);
    t.eq('bullets' in p || 'params' in p, false);
    t.eq(p.doc_name, 'GC Analysis');

    // the store: persisted in sessionStorage, one entry per sample
    const ss = memStorage();
    const s = Q.createStore(ss);
    let seen = 0;
    s.onChange(() => { seen++; });
    t.eq(s.items(), []);
    t.eq(s.add(raw, 'T1').replaced, false);
    t.eq(s.add({ sample_id: 41, lab_id: '40330', standard_name: 'B' }, 'T2').replaced, false);
    const again = s.add(Object.assign({}, raw, { standard_name: 'Other' }), 'T3');
    t.eq(again.replaced, true);
    t.eq(s.items().map(i => [i.id, i.standard_name]), [['s40', 'Other'], ['s41', 'B']]);
    t.eq(seen, 3);
    // a second store on the same storage (the next page in this tab) sees them
    const s2 = Q.createStore(ss);
    t.eq(s2.count(), 2);
    t.eq(JSON.parse(ss.getItem(Q.STORAGE_KEY)).length, 2);
    t.eq(s.add({ sample_id: null }), null);          // refused, nothing changes
    t.eq(s.count(), 2);
    t.eq(s.remove('s40'), true);
    t.eq(s.remove('s40'), false);
    t.eq(s.items().map(i => i.id), ['s41']);
    // sent to QBench: marked, and not sent again
    s.add({ sample_id: 42, lab_id: '40331', standard_name: 'B' });
    s.markSent(['s41'], 'T9');
    t.eq(s.unsent().map(i => i.id), ['s42']);
    t.eq(s.items()[0].sent_at, 'T9');
    // re-adding a sent sample makes it unsent again (a new report)
    s.add({ sample_id: 41, lab_id: '40330', standard_name: 'C' });
    t.eq(s.unsent().map(i => i.id).sort(), ['s41', 's42']);
    t.eq(s.addMany([{ sample_id: 50, standard_name: 'A' }, { sample_id: 'bad' },
                    { sample_id: 51, standard_name: 'A' }]), 2);
    s.clear();
    t.eq(s.count(), 0);
    t.eq(ss.getItem(Q.STORAGE_KEY), '[]');

    // storage that throws or holds garbage: an in-memory queue, never a crash
    const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); },
                     removeItem() {} };
    const s3 = Q.createStore(broken);
    s3.add({ sample_id: 1, standard_name: 'A' });
    t.eq(s3.count(), 1);
    const junk = memStorage();
    junk.setItem(Q.STORAGE_KEY, '{nope');
    t.eq(Q.createStore(junk).items(), []);
    junk.setItem(Q.STORAGE_KEY, JSON.stringify([{ sample_id: 3, standard_name: 'A' }, 7, null]));
    t.eq(Q.createStore(junk).items().map(i => i.id), ['s3']);
    t.eq(Q.createStore(null).count(), 0);

    // wording
    t.eq(Q.buttonText(0), 'Report queue');
    t.eq(Q.buttonText(3), 'Report queue 3');
    t.eq(Q.addedText({ lab_id: '40329', standard_name: 'Diesel ULSD Std' }, false),
        'Added 40329 to the report queue, compared with Diesel ULSD Std');
    t.eq(Q.addedText({ lab_id: '40329', standard_name: 'X' }, true),
        'Updated 40329 in the report queue, compared with X');
    t.eq(Q.addedText({ sample_id: 9, standard_name: 'X' }, false),
        'Added sample 9 to the report queue, compared with X');

    // the upload's per-item and overall lines (the SSE stream's statuses)
    t.eq(Q.statusText('ok'), 'Uploaded');
    t.eq(Q.statusText('skipped'), 'Skipped');
    t.eq(Q.statusText('login_failed'), 'QBench sign-in failed');
    t.eq(Q.statusText('generating'), 'Building the PDF');
    t.eq(Q.statusText('whatever'), 'Waiting');
    t.eq(Q.isFinished('ok') && Q.isFinished('failed') && Q.isFinished('error') && Q.isFinished('skipped'), true);
    t.eq(Q.isFinished('uploading'), false);
    t.eq(Q.progress([{ status: 'ok' }, { status: 'skipped' }, { status: 'uploading' }, { status: 'waiting' }]),
        { done: 2, total: 4, pct: 50 });
    t.eq(Q.progress([]), { done: 0, total: 0, pct: 0 });
    t.eq(Q.overallView({ status: 'done', total: 3, ok: 3, fail: 0 }),
        { text: 'All 3 uploaded to QBench.', tone: 'ok', finished: true });
    t.eq(Q.overallView({ status: 'partial', total: 3, ok: 2, fail: 1 }),
        { text: '2 uploaded, 1 failed.', tone: 'warn', finished: true });
    t.eq(Q.overallView({ status: 'allfailed', total: 2, ok: 0, fail: 2, msg: 'x' }),
        { text: 'The upload failed (2).', tone: 'err', finished: true });
    t.eq(Q.overallView({ status: 'cancelled' }), { text: 'Upload stopped.', tone: 'warn', finished: true });
    t.eq(Q.overallView({ status: 'credentials_needed', msg: 'Login failed' }).finished, false);
    // a failed first item is a warning; the upload carries on
    t.eq(Q.overallView({ status: 'precheck_failed', msg: 'No API key' }),
        { text: 'No API key', tone: 'err', finished: false });
    t.eq(Q.overallView({ status: 'running', msg: 'Signing in' }), { text: 'Signing in', tone: 'info', finished: false });

    // ── v6.0.0: retry without clearing the queue ─────────────────────────
    // a failure is one plain line: the first line, spaces collapsed, capped
    t.eq(Q.failureLine('Chrome could not start: session not created\nStacktrace:\n0x1 0x2'),
        'Chrome could not start: session not created');
    t.eq(Q.failureLine('  \n  QBench API:   connection refused  \n'), 'QBench API: connection refused');
    t.eq(Q.failureLine(''), '');
    t.eq(Q.failureLine(null), '');
    t.eq(Q.failureLine('x'.repeat(400)).length, 240);
    t.eq(Q.failureLine('x'.repeat(400)).endsWith('…'), true);

    // an item that failed keeps the reason, is unsent, and only then has the key
    const fs = Q.createStore(memStorage());
    fs.add({ sample_id: 60, lab_id: '600', standard_name: 'A' });
    fs.add({ sample_id: 61, lab_id: '601', standard_name: 'A' });
    fs.add({ sample_id: 62, lab_id: '602', standard_name: 'A' });
    t.eq('upload_error' in fs.items()[0], false);
    fs.markSent(['s60', 's61', 's62'], 'T1');                 // handed to the hub
    fs.markFailed({ s60: 'QBench has no sample with lab ID 600' }, 'T2');
    const f60 = fs.items()[0];
    t.eq(f60.sent_at, null);
    t.eq(f60.upload_error, { msg: 'QBench has no sample with lab ID 600', at: 'T2' });
    t.eq(fs.unsent().map(i => i.id), ['s60']);
    t.eq(fs.failed().map(i => i.id), ['s60']);
    // the key survives the next page (normalizeItem keeps it)
    t.eq(Q.normalizeItem(f60).upload_error, { msg: 'QBench has no sample with lab ID 600', at: 'T2' });
    t.eq('upload_error' in Q.normalizeItem({ sample_id: 1, standard_name: 'A', upload_error: 'junk' }), false);
    // a retry hands it to the hub again: sent, the error gone
    fs.markSent(['s60'], 'T3');
    t.eq(fs.items()[0].sent_at, 'T3');
    t.eq('upload_error' in fs.items()[0], false);
    t.eq(fs.failed(), []);
    // re-adding a failed sample clears its error (a new report)
    fs.markFailed({ s61: 'x' }, 'T4');
    fs.add({ sample_id: 61, lab_id: '601', standard_name: 'B' });
    t.eq(fs.failed(), []);
    // back to unsent without an error (skipped)
    fs.markUnsent(['s62']);
    t.eq(fs.items()[2].sent_at, null);
    t.eq('upload_error' in fs.items()[2], false);

    // the run's rows → what each queued report's state is now. The newest row
    // for a sample wins (a retry appended to a running upload adds a row).
    const rows = [
        { idx: 0, sample_id: 70, lab_id: '700', status: 'failed', msg: 'Upload returned false' },
        { idx: 1, sample_id: 71, lab_id: '701', status: 'ok', msg: 'Uploaded' },
        { idx: 2, sample_id: 72, lab_id: '702', status: 'error', msg: 'Report failed: Standard not found: X\nTraceback' },
        { idx: 3, sample_id: 73, lab_id: '703', status: 'skipped', msg: 'Skipped by user' },
        { idx: 4, sample_id: 74, lab_id: '704', status: 'uploading', msg: 'Step 3/10' },
        { idx: 5, sample_id: 75, lab_id: '705', status: 'login_failed', msg: 'Login failed: Bad credentials' },
        { idx: 6, sample_id: 70, lab_id: '700', status: 'ok', msg: 'Uploaded' },
        { idx: 7, lab_id: '?', status: 'error', msg: 'No lab_id' },             // no sample: ignored
    ];
    t.eq(Q.reconcile(rows, false), {
        sent: [71, 70],
        failed: [{ sample_id: 72, msg: 'Report failed: Standard not found: X' }],
        unsent: [73],
    });
    // once the run has ended, anything not uploaded is not sent
    t.eq(Q.reconcile(rows, true), {
        sent: [71, 70],
        failed: [{ sample_id: 72, msg: 'Report failed: Standard not found: X' },
                 { sample_id: 74, msg: 'Not uploaded: the upload ended before this report was sent.' },
                 { sample_id: 75, msg: 'Login failed: Bad credentials' }],
        unsent: [73],
    });
    t.eq(Q.reconcile(null, true), { sent: [], failed: [], unsent: [] });
    t.eq(Q.reconcile([{ idx: 0, sample_id: 9, status: 'failed', msg: '' }], true).failed,
        [{ sample_id: 9, msg: 'Failed' }]);

    // applying it touches only the reports this tab handed to the hub
    const as = Q.createStore(memStorage());
    for (const sid of [70, 71, 72, 73, 74, 75]) as.add({ sample_id: sid, lab_id: String(sid), standard_name: 'A' });
    as.markSent(['s70', 's71', 's72', 's73', 's74'], 'T1');   // s75 was re-added after the start: not in flight
    as.applyOutcome(Q.reconcile(rows, true), 'T9');
    const byId = Object.fromEntries(as.items().map(i => [i.id, i]));
    t.eq(byId.s70.sent_at, 'T1');
    t.eq(byId.s71.sent_at, 'T1');
    t.eq(byId.s72.sent_at, null);
    t.eq(byId.s72.upload_error, { msg: 'Report failed: Standard not found: X', at: 'T9' });
    t.eq(byId.s73.sent_at, null);
    t.eq('upload_error' in byId.s73, false);
    t.eq(byId.s74.upload_error.msg, 'Not uploaded: the upload ended before this report was sent.');
    t.eq('upload_error' in byId.s75, false);                // untouched
    t.eq(as.failed().map(i => i.id), ['s72', 's74']);

    // wording: the sheet's retry actions
    t.eq(Q.retryText(1), 'Retry failed (1)');
    t.eq(Q.retryText(3), 'Retry failed (3)');
    // the credentials problem the hub names, offered with the password box
    t.eq(Q.startRefusal({ need_password: true, error: "Can't read the saved QBench sign-in file X: denied" }),
        { needPassword: true, text: "Can't read the saved QBench sign-in file X: denied" });
    t.eq(Q.startRefusal({ error: 'boom' }, 500), { needPassword: false, text: 'Not uploaded: boom' });
    t.eq(Q.startRefusal({ refused: [{ lab_id: '1', error: 'not final' }, { sample_id: 2, error: 'gone' }] }, 409),
        { needPassword: false, text: 'Not uploaded: 1 (not final); 2 (gone)' });
    t.eq(Q.startRefusal({}, 502), { needPassword: false, text: 'Not uploaded: HTTP 502' });

    // v7: the sheet names the ranges each report prints, in order
    t.eq(Q.rangesLine(it.ranges), 'Ranges: Gas C5–C11');
    t.eq(Q.rangesLine([{ label: 'Gas', c_start: 5, c_end: 11 }, { label: 'Heavy tail', c_start: 60, c_end: 80 }]),
        'Ranges: Gas C5–C11, Heavy tail C60–C80');
    t.eq(Q.rangesLine([]), 'Ranges: none');
    t.eq(Q.rangesLine(undefined), 'Ranges: the saved defaults when it is built');
    // an item added with no ranges keeps "none" through storage and the payload
    const st = Q.createStore(memStorage(), 'k');
    st.add(Object.assign({}, raw, { ranges: [] }));
    t.eq(st.items()[0].ranges, []);
    t.eq(Q.payloads(st.items())[0].ranges, []);
};
