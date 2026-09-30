// static/js/running_now.js (v4.0 lane E): the "running now" indicator, the GC
// strip and the processing-paused banner, from /api/live's tasks, agents and
// hub. Pure helpers only; the DOM wiring is covered by the Selenium smoke test.
const R = require('../../static/js/running_now.js');

const T = (over) => Object.assign({ id: 'import-history:3', kind: 'import-history',
    title: 'Importing GC-2 history', instrument: 'gc2', state: 'running',
    progress: { done: 3000, total: 12000, text: 'Classifying samples' }, by: 'Ryan C',
    started_at: '2026-09-29T14:02:00+00:00', ended_at: null, open_url: '/admin/hub#import-history',
    download_url: null, mine: true }, over || {});

module.exports = (t) => {
    t.eq(R.fmtNum(3000), '3,000');
    t.eq(R.fmtNum(1234567), '1,234,567');
    t.eq(R.fmtNum(12), '12');

    // ── one line per task ──
    t.eq(R.progressText(T()), 'Classifying samples · 3,000 of 12,000');
    t.eq(R.progressText(T({ progress: { done: 2, total: 5, text: null } })), '2 of 5');
    t.eq(R.progressText(T({ progress: { done: null, total: null, text: 'Scanning the folder' } })),
         'Scanning the folder');
    t.eq(R.progressText(T({ progress: null })), 'Starting…');

    t.eq(R.outcomeText(T({ state: 'done', progress: { done: 2, total: 2, text: '2 of 2 re-processed' } })),
         'Finished · 2 of 2 re-processed');
    t.eq(R.outcomeText(T({ state: 'done', progress: null })), 'Finished');
    t.eq(R.outcomeText(T({ state: 'failed' })), 'Failed · see its page for why');
    t.eq(R.outcomeText(T({ state: 'failed', open_url: null })), 'Failed');
    t.eq(R.outcomeText(T({ state: 'stopped', progress: { done: 400, total: 900, text: null } })),
         'Stopped after 400 of 900');
    t.eq(R.outcomeText(T({ state: 'stopped', progress: null })), 'Stopped');
    t.eq(R.outcomeText(T({ state: 'interrupted' })),
         'Interrupted: it stopped without finishing');
    t.eq(R.taskLine(T()), R.progressText(T()));
    t.eq(R.taskLine(T({ state: 'failed', open_url: null })), 'Failed');

    // ── the collapsed indicator: most urgent first ──
    t.eq(R.summary([]), null);
    t.eq(R.summary([T()]), { text: 'Importing GC-2 history · 3,000/12,000', glyph: 'spinner',
                             state: 'running', count: 1 });
    t.eq(R.summary([T({ progress: { done: null, total: null, text: 'Scanning the folder' } })]).text,
         'Importing GC-2 history · Scanning the folder');
    t.eq(R.summary([T({ progress: null })]).text, 'Importing GC-2 history');
    t.eq(R.summary([T(), T({ id: 'reprocess:1' }), T({ id: 'x', state: 'done' })]),
         { text: '2 running', glyph: 'spinner', state: 'running', count: 3 });
    t.eq(R.summary([T({ state: 'done', title: 'GC-1 history dry run' })]),
         { text: 'GC-1 history dry run finished · View', glyph: 'dot', state: 'done', count: 1 });
    t.eq(R.summary([T({ state: 'failed', title: 'Diagnostics bundle' })]).glyph, 'triangle');
    t.eq(R.summary([T({ state: 'failed', title: 'Diagnostics bundle' })]).text,
         'Diagnostics bundle failed · View');
    t.eq(R.summary([T({ state: 'stopped' })]).glyph, 'ring');
    t.eq(R.summary([T({ state: 'interrupted' })]).text, 'Importing GC-2 history interrupted · View');

    // ── dismissing: per browser, keyed by the hub's boot id and the task id ──
    const tasks = [T(), T({ id: 'reprocess:1', state: 'done' }), T({ id: 'zip:2', state: 'failed' })];
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

    // ── the GC strip: glyph + text, never colour alone; the server decides live ──
    const A = (over) => Object.assign({ instrument_id: 'gc1', name: 'GC-1', enabled: true,
        last_seen: '2026-09-29T12:00:00+00:00', version: 'v1.2.0', host: 'GC1-PC', status: 'idle',
        live: true, last_seen_age_s: 12 }, over || {});
    t.eq(R.gcChip(A()), { id: 'gc1', name: 'GC-1', glyph: '●', text: 'Live', cls: 'live',
                          href: '/instruments?instrument=gc1',
                          title: 'GC-1 · GC1-PC · agent v1.2.0 · checked in 12 s ago' });
    t.eq(R.gcChip(A({ live: false, last_seen_age_s: 400 })).text, 'Not seen for 6 min');
    t.eq(R.gcChip(A({ live: false, last_seen_age_s: 400 })).glyph, '○');
    t.eq(R.gcChip(A({ live: false, last_seen_age_s: 400 })).cls, 'quiet');
    t.eq(R.gcChip(A({ live: false, last_seen_age_s: 7200 })).text, 'Not seen for 2 h');
    t.eq(R.gcChip(A({ live: false, last_seen_age_s: 200000 })).text, 'Not seen for 2 d');
    const never = R.gcChip(A({ instrument_id: 'gc 2', name: null, live: false, last_seen: null,
                               last_seen_age_s: null, host: null, version: null }));
    t.eq([never.name, never.text, never.cls, never.href, never.title],
         ['gc 2', 'Never checked in', 'never', '/instruments?instrument=gc%202',
          'gc 2 · the agent has never checked in']);
    t.eq(R.gcChip(A({ enabled: false })).text, 'Disabled');
    t.eq(R.gcChip(A({ enabled: false })).glyph, '–');
    // an older hub without `live`: never guess on the client
    t.eq(R.gcChip(A({ live: undefined })).text, 'Not seen for 12 s');
    t.eq(R.gcChip(A(), '/instruments/{id}').href, '/instruments/gc1');
    t.eq(R.gcSummary([A(), A({ instrument_id: 'gc2', live: false }), A({ enabled: false })]),
         '1 of 2 GCs connected');
    t.eq(R.gcSummary([A()]), '1 of 1 GC connected');
    t.eq(R.gcSummary([]), '');

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
    t.eq(R.pausedBanner({ processing_paused: true }),
         'Processing is paused. New runs are received and wait. ' +
         'Resume it from the hub tray on the server.');

    t.eq(R.whenText('2026-09-29T14:02:00+00:00', 'Ryan C').startsWith('Ryan C · '), true);
    t.eq(R.whenText(null, null), '');
};
