// admin_page.js (v4.0 lane E2): the Hub admin page in the shell. One sub-nav of
// sections in the order Ryan uses them, a hash that finds its section (the
// task feed's Open links), which section is in view, and the Server section's
// facts in words (never JSON).
const A = require('../../static/js/admin_page.js');

module.exports = (t) => {
    // ── the section order: running work first, one-time setup last ──
    t.eq(A.SECTIONS.map(s => s.id), ['status', 'import-history', 'load-folder', 'purge-panel',
        'exports', 'diagnostics', 'presets-panel', 'sessions', 'server', 'hub-address']);
    for (const s of A.SECTIONS) t.eq(typeof s.label === 'string' && s.label.length > 0, true);

    // ── a hash finds its section (task Open links, old anchors) ──
    t.eq(A.sectionForHash('#load-folder'), 'load-folder');
    t.eq(A.sectionForHash('#import-history'), 'import-history');
    t.eq(A.sectionForHash('#purge'), 'purge-panel');
    t.eq(A.sectionForHash('#purge-panel'), 'purge-panel');
    t.eq(A.sectionForHash('#diagnostics'), 'diagnostics');
    t.eq(A.sectionForHash('#imports'), 'import-history');
    t.eq(A.sectionForHash('#presets'), 'presets-panel');
    t.eq(A.sectionForHash('#running'), 'status');
    t.eq(A.sectionForHash(''), null);
    t.eq(A.sectionForHash('#nope'), null);
    t.eq(A.sectionForHash(undefined), null);

    // ── which section is in view: the last one whose top passed the line ──
    const tops = [['status', 0], ['import-history', 400], ['load-folder', 900]];
    t.eq(A.activeSection(tops, 72), 'status');
    t.eq(A.activeSection(tops, 450), 'import-history');
    t.eq(A.activeSection(tops, 5000), 'load-folder');
    t.eq(A.activeSection([], 10), null);
    // scrolled to the very bottom: the last section, even if its top never passed
    t.eq(A.activeSection(tops, 100, true), 'load-folder');

    // ── the Server section, in words ──
    const running = { version: 'v3.1.0', state: 'running', processing_paused: false,
        uptime_seconds: 3 * 3600 + 5 * 60, queue: { waiting: 0, running: 1 },
        exporter: { pending_rows: 0, alive: true }, rss_bytes: 212 * 1024 * 1024, cpu_percent: 1.2,
        staged_update: null, updater_paused: false, stale: false };
    const rows = A.serverRows(running);
    t.eq(rows.map(r => r[0]), ['Version', 'Up for', 'Processing', 'Results files', 'Memory']);
    t.eq(rows[0][1], 'v3.1.0');
    t.eq(rows[1][1], '3 h 5 min');
    t.eq(rows[2][1], 'Running · 0 waiting, 1 running');
    t.eq(rows[3][1], 'Up to date');
    t.eq(rows[4][1], '212 MB · CPU 1%');
    for (const [, v] of rows) t.eq(/[{}[\]]/.test(v), false);

    const paused = Object.assign({}, running, { processing_paused: true, processing_paused_by: 'Ryan C',
        staged_update: 'v3.1.1', exporter: { pending_rows: 12, alive: true }, uptime_seconds: 40 });
    const p = A.serverRows(paused);
    t.eq(p[0][1], 'v3.1.0 · v3.1.1 is ready to install');
    t.eq(p[1][1], 'under a minute');
    t.eq(p[2][1], 'Paused by Ryan C · 0 waiting, 1 running');
    t.eq(p[3][1], '12 rows waiting to be written');
    t.eq(A.serverRows(Object.assign({}, running, { exporter: { pending_rows: 1 } }))[3][1],
        '1 row waiting to be written');
    t.eq(A.serverState(paused), 'held');
    t.eq(A.serverState(running), 'final');
    t.eq(A.serverState(null), 'never');
    t.eq(A.serverRows(null), []);

    // ── uptime in human units ──
    t.eq(A.uptimeText(59), 'under a minute');
    t.eq(A.uptimeText(60 * 12), '12 min');
    t.eq(A.uptimeText(86400 * 2 + 3600), '2 d 1 h');
};
