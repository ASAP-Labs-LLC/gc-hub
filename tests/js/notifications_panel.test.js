// notifications_panel.js (v5.0 lane R): the shell's bell opens a panel that
// lists the hub's notifications (newest first), each with its level as a
// glyph + word (never colour alone), when, and a "Go to" link to the fix;
// dismiss one, or all; it follows GCLive.
const N = require('../../static/js/notifications_panel.js');

module.exports = (t) => {
    t.eq(N.level('error'), { glyph: 'error', word: 'Error' });
    t.eq(N.level('warning'), { glyph: 'held', word: 'Warning' });
    t.eq(N.level('success'), { glyph: 'final', word: 'Done' });
    t.eq(N.level('info'), { glyph: 'never', word: 'Note' });
    t.eq(N.level('whatever'), { glyph: 'never', word: 'Note' });

    // v5.1.0: a Lab ID LEM will misread goes to its sample, whatever the
    // (CDF-controlled) name says; the id is read from the start only.
    t.eq(N.link("Sample 412 on GC-1: Lab ID '40304, rerun' contains a comma; LEM will read this row's values one column off. Rename the sample in QBench/LEM by hand."),
         { href: '/samples/412', text: 'Open sample' });
    t.eq(N.link("Sample 7 on GC-2: Lab ID 'purge (Sample 9 on x), export' contains 2 commas; LEM will read this row's values two columns off. Rename the sample in QBench/LEM by hand."),
         { href: '/samples/7', text: 'Open sample' });
    t.eq(N.link("Lab ID 'Sample 5 on x: y' LEM will"), null);
    t.eq(N.link('Purge of GC-1 finished after a restart'), { href: '/admin/hub#purge-panel', text: 'Open Purge' });
    t.eq(N.link('History import interrupted by a restart'), { href: '/admin/hub#import-history', text: 'Open Import history' });
    t.eq(N.link('Results file for gc1 refused: ledger-mismatch'), { href: '/admin/hub#exports', text: 'Open Results files' });
    t.eq(N.link('GC-2 agent has not checked in for 10 minutes'), { href: '/instruments', text: 'Open Instruments' });
    t.eq(N.link('3 samples held: awaiting calibration'), { href: '/results?status=held', text: 'Show held runs' });
    t.eq(N.link('Processing paused by Ryan C'), { href: '/admin/hub#status', text: 'Open Hub status' });
    // hub_control.PAUSED_NOTICE mentions the results CSV too: it is about the pause
    t.eq(N.link('Processing is paused by Ryan C (since 09:12). Samples are still received and queued, but nothing is processed, exported to the results CSV or backed up until processing is resumed (hub tray: Resume processing).').href, '/admin/hub#status');
    t.eq(N.link('Processing resumed: queued samples are being processed again.').href, '/admin/hub#status');
    t.eq(N.link('Nightly backup failed: disk full'), { href: '/admin/hub#diagnostics', text: 'Open Diagnostics' });
    t.eq(N.link('Something else'), null);
    t.eq(N.link(''), null);

    const now = Date.parse('2026-09-30T15:00:00');
    t.eq(N.when('2026-09-30T14:59:30', now), '30 s ago');
    t.eq(N.when('2026-09-30T14:10:00', now), '50 min ago');
    t.eq(N.when('2026-09-30T09:12:00', now), '09:12');
    t.eq(N.when('2026-09-28T09:12:00', now), 'Sep 28 09:12');
    t.eq(N.when(null, now), '');

    const list = [
        { id: 'a', ts: '2026-09-30T10:00:00', level: 'info', message: 'A' },
        { id: 'b', ts: '2026-09-30T12:00:00', level: 'error', message: 'B' },
        { id: 'c', level: 'warning', message: 'C' },
    ];
    t.eq(N.sorted(list).map((n) => n.id), ['b', 'a', 'c']);
    t.eq(N.sorted(null), []);
    t.eq(N.title(0), 'Notifications');
    t.eq(N.title(3), 'Notifications · 3');
    t.eq(N.badge(0), '');
    t.eq(N.badge(7), '7');
    t.eq(N.badge(150), '99+');
    t.eq(N.bellLabel(2), 'Notifications: 2');
    t.eq(N.bellLabel(0), 'Notifications');
    // a live update means a reload when the count moved or a notification event came
    t.eq(N.needsReload({ notifications_unread: 2 }, 2), false);
    t.eq(N.needsReload({ notifications_unread: 3 }, 2), true);
    t.eq(N.needsReload({ kinds: ['notification'], notifications_unread: 2 }, 2), true);
    t.eq(N.needsReload({ reset: true }, 2), true);
    t.eq(N.needsReload({}, 2), false);
    t.eq(N.without(list, 'a').map((n) => n.id), ['b', 'c']);
};
