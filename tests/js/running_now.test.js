// static/js/running_now.js (v4.0 lane E, after the review): the "running now"
// indicator, the compact GC strip and the processing-paused banner, from
// /api/live's tasks, agents and hub. Pure helpers only; the DOM wiring is
// covered by the Selenium smoke test.
const R = require('../../static/js/running_now.js');

const T = (over) => Object.assign({ id: 'import-history:3', kind: 'import-history',
    title: 'Importing GC-2 history', instrument: 'gc2', state: 'running',
    progress: { done: 3000, total: 12000, text: 'Classifying samples' }, by: 'Ryan C',
    started_at: '2026-09-29T14:02:00+00:00', ended_at: null, open_url: '/admin/hub#import-history',
    download_url: null, mine: true, outcome: null }, over || {});
const ENDED = (over) => T(Object.assign({ state: 'done', ended_at: '2026-09-29T14:05:00+00:00',
    progress: { done: 1, total: 1, text: '1 of 1 re-processed' },
    title: 'Re-process · 1 sample', outcome: 'Re-processed 1 sample' }, over || {}));

module.exports = (t) => {
    t.eq(R.fmtNum(3000), '3,000');
    t.eq(R.fmtNum(1234567), '1,234,567');
    t.eq(R.fmtNum(12), '12');

    // ── a running task: its title, then its progress in words ──
    t.eq(R.progressText(T()), 'Classifying samples · 3,000 of 12,000');
    t.eq(R.progressText(T({ progress: { done: 2, total: 5, text: null } })), '2 of 5');
    t.eq(R.progressText(T({ progress: { done: null, total: null, text: 'Scanning the folder' } })),
         'Scanning the folder');
    t.eq(R.progressText(T({ progress: null })), 'Starting…');

    // ── an ended task: ONE outcome line (the hub's, with counts), never the
    //    title plus the last phase ("Re-process · 1 sample … Finished · 1 of 1") ──
    t.eq(R.headline(T()), 'Importing GC-2 history');
    t.eq(R.headline(ENDED()), 'Re-processed 1 sample');
    t.eq(R.detailLine(ENDED()), null);
    t.eq(R.detailLine(T()), 'Classifying samples · 3,000 of 12,000');
    // an older hub without `outcome`: title + state, still one line
    t.eq(R.headline(ENDED({ outcome: undefined, title: 'Diagnostics bundle', state: 'failed' })),
         'Diagnostics bundle failed');
    t.eq(R.headline(ENDED({ outcome: undefined, title: 'Report ZIP', state: 'done' })),
         'Report ZIP finished');

    // who and when: started while running, finished once ended (local time)
    const hm = (iso) => { const d = new Date(iso); return String(d.getHours()).padStart(2, '0') + ':' +
                                                        String(d.getMinutes()).padStart(2, '0'); };
    t.eq(R.metaLine(T()), 'Ryan C · started ' + hm('2026-09-29T14:02:00+00:00'));
    t.eq(R.metaLine(ENDED()), 'Ryan C · finished ' + hm('2026-09-29T14:05:00+00:00'));
    t.eq(R.metaLine(ENDED({ state: 'failed', by: null })), 'ended ' + hm('2026-09-29T14:05:00+00:00'));

    // ── the popover's title ──
    t.eq(R.popoverTitle([T(), ENDED()]), 'Running now');
    t.eq(R.popoverTitle([ENDED()]), 'Recent work');

    // ── the collapsed indicator: most urgent first ──
    t.eq(R.summary([]), null);
    // the phase names its count, so a count restarting in the next phase reads right (lane E2 review)
    t.eq(R.summary([T({ progress: { done: 3000, total: 12000, text: null } })]).text,
        'Importing GC-2 history · 3,000/12,000');
    t.eq(R.summary([T({ progress: { done: 1500, total: 5000, text: 'Reading CDFs' } })]).text,
        'Importing GC-2 history · Reading CDFs 1,500/5,000');
    t.eq(R.summary([T()]), { text: 'Importing GC-2 history · Classifying samples 3,000/12,000', glyph: 'spinner',
                             state: 'running', count: 1 });
    t.eq(R.summary([T({ progress: { done: null, total: null, text: 'Scanning the folder' } })]).text,
         'Importing GC-2 history · Scanning the folder');
    t.eq(R.summary([T({ progress: null })]).text, 'Importing GC-2 history');
    t.eq(R.summary([T(), T({ id: 'reprocess:1' }), ENDED({ id: 'x' })]),
         { text: '2 running', glyph: 'spinner', state: 'running', count: 3 });
    t.eq(R.summary([ENDED({ outcome: 'GC-1 dry run finished · 40 classified' })]),
         { text: 'GC-1 dry run finished · 40 classified', glyph: 'dot', state: 'done', count: 1 });
    t.eq(R.summary([ENDED({ state: 'failed', outcome: 'Diagnostics bundle failed' })]).glyph, 'triangle');
    t.eq(R.summary([ENDED({ state: 'stopped' })]).glyph, 'ring');
    t.eq(R.summary([ENDED({ state: 'interrupted' })]).glyph, 'ring');

    // ── dismissing: per browser, keyed by the hub's boot id and the task id ──
    const tasks = [T(), ENDED({ id: 'reprocess:1' }), ENDED({ id: 'zip:2', state: 'failed' })];
    const dismissed = new Set(['b00t:reprocess:1', 'other:zip:2', 'b00t:import-history:3']);
    t.eq(R.visibleTasks(tasks, dismissed, 'b00t').map(x => x.id), ['import-history:3', 'zip:2']);
    t.eq(R.dismissKey('b00t', T()), 'b00t:import-history:3');
    const mem = { v: {}, getItem(k) { return this.v[k] === undefined ? null : this.v[k]; },
                  setItem(k, v) { this.v[k] = String(v); } };
    t.eq([...R.loadDismissed(mem)], []);
    R.saveDismissed(new Set(['a:1', 'b:2']), mem);
    t.eq([...R.loadDismissed(mem)].sort(), ['a:1', 'b:2']);
    mem.v[R.DISMISS_KEY] = '{not json';
    t.eq([...R.loadDismissed(mem)], []);
    const broken = { getItem() { throw new Error('no'); }, setItem() { throw new Error('no'); } };
    t.eq([...R.loadDismissed(broken)], []);
    R.saveDismissed(new Set(['x']), broken);                       // no throw
    const many = new Set(Array.from({ length: 500 }, (_, i) => 'b:' + i));
    R.saveDismissed(many, mem);
    t.eq(R.loadDismissed(mem).size, R.DISMISS_MAX);

    // ── one GC: glyph + text, never colour alone; the hub decides live, and the
    //    age keeps ticking from when the answer was read (review blocker) ──
    const NOW = 1000000;
    const A = (over) => Object.assign({ instrument_id: 'gc1', name: 'GC-1', enabled: true,
        last_seen: '2026-09-29T12:00:00+00:00', version: 'v1.2.0', host: 'GC1-PC', status: 'idle',
        live: true, last_seen_age_s: 12, read_at: NOW }, over || {});
    t.eq(R.gcChip(A(), NOW), { id: 'gc1', name: 'GC-1', glyph: '●', text: 'Live', cls: 'live',
                               href: '/instruments/gc1',
                               title: 'GC-1 · GC1-PC · agent v1.2.0 · checked in 12 s ago' });
    t.eq(R.gcChip(A({ live: false, last_seen_age_s: 95 }), NOW).text, 'Not seen for 1 min');
    // …and 14 min later, with no new answer, it says so (it used to freeze)
    t.eq(R.gcChip(A({ live: false, last_seen_age_s: 95 }), NOW + 14 * 60000).text,
         'Not seen for 15 min');
    t.eq(R.gcChip(A({ live: false, last_seen_age_s: 7200 }), NOW).text, 'Not seen for 2 h');
    t.eq(R.gcChip(A({ live: false, last_seen_age_s: 200000 }), NOW).text, 'Not seen for 2 d');
    t.eq(R.gcChip(A({ live: false, last_seen_age_s: 400 }), NOW).cls, 'quiet');
    t.eq(R.gcChip(A({ live: false, last_seen_age_s: 400 }), NOW).glyph, '○');
    // live at the last answer, but 2 minutes without one: no longer live
    t.eq(R.gcChip(A(), NOW + 120000).text, 'Not seen for 2 min');
    const never = R.gcChip(A({ instrument_id: 'gc 2', name: null, live: false, last_seen: null,
                               last_seen_age_s: null, host: null, version: null }), NOW);
    t.eq([never.name, never.text, never.cls, never.href, never.title],
         ['gc 2', 'Never checked in', 'never', '/instruments/gc%202',
          'gc 2 · the agent has never checked in']);
    t.eq(R.gcChip(A({ enabled: false }), NOW).text, 'Disabled');
    t.eq(R.gcChip(A({ enabled: false }), NOW).glyph, '–');
    t.eq(R.gcChip(A({ live: undefined }), NOW).text, 'Not seen for 12 s');   // older hub: never guess
    t.eq(R.gcChip(A(), NOW, '/instruments?instrument={id}').href, '/instruments?instrument=gc1');

    // ── the strip is one compact chip: "2/2 GCs live" (the toolbar must fit) ──
    const two = [A(), A({ instrument_id: 'gc2', name: 'GC-2', live: false, last_seen_age_s: 400 }),
                 A({ instrument_id: 'gc3', name: 'GC-3', enabled: false })];
    t.eq(R.gcStrip(two, NOW), { text: '1 of 2 GCs live', glyph: '○', cls: 'quiet',
                                title: 'GC-1: Live · GC-2: Not seen for 6 min · GC-3: Disabled' });
    t.eq(R.gcStrip([A(), A({ instrument_id: 'gc2', name: 'GC-2' })], NOW).text, '2 of 2 GCs live');
    t.eq(R.gcStrip([A(), A({ instrument_id: 'gc2', name: 'GC-2' })], NOW).glyph, '●');
    t.eq(R.gcStrip([A()], NOW).text, '1 of 1 GC live');
    t.eq(R.gcStrip([A({ live: false, last_seen: null, last_seen_age_s: null })], NOW).cls, 'never');
    t.eq(R.gcStrip([], NOW), null);
    t.eq(R.gcSummary(two, NOW), '1 of 2 GCs live');
    t.eq(R.gcSummary([A()], NOW), '1 of 1 GC live');
    t.eq(R.gcSummary([], NOW), '');

    // ── the processing-paused banner ──
    t.eq(R.pausedBanner(null), null);
    t.eq(R.pausedBanner({ processing_paused: false }), null);
    const since = new Date(2026, 8, 29, 14, 2).toISOString();
    t.eq(R.pausedBanner({ processing_paused: true, paused_by: 'Ryan C', paused_since: since,
                          queue: { waiting: 14, running: 0 } }),
         'Processing is paused (Ryan C, 14:02). New runs are received and wait: 14 waiting. ' +
         'Resume it from the hub tray on the server.');
    t.eq(R.pausedBanner({ processing_paused: true, queue: { waiting: 1 } }),
         'Processing is paused. New runs are received and wait: 1 waiting. ' +
         'Resume it from the hub tray on the server.');
    t.eq(R.pausedBanner({ processing_paused: true, queue: { waiting: 0 } }),
         'Processing is paused. New runs are received and wait. ' +
         'Resume it from the hub tray on the server.');
};
