// deeplink.js: sendable sample links (v3.1). The copied link (always the hub
// URL, never location.origin) and the "Other runs" line. v6.0.0: the URL
// parsing and the classic page's hook went with the classic page.
const D = require('../../static/js/deeplink.js');

module.exports = (t) => {
    // ── the copied link ──────────────────────────────────────────────────
    t.eq(D.sampleLink('https://gc.asaplabs.net', 12), 'https://gc.asaplabs.net/samples/12');
    t.eq(D.sampleLink('https://gc.asaplabs.net/', 12), 'https://gc.asaplabs.net/samples/12');
    t.eq(D.sampleLink('https://gc.example.org//', '7'), 'https://gc.example.org/samples/7');
    // no hub URL known yet: the default, never the page's own (LAN) origin
    t.eq(D.sampleLink('', 12), 'https://gc.asaplabs.net/samples/12');
    t.eq(D.sampleLink(null, 12), 'https://gc.asaplabs.net/samples/12');
    t.eq(D.DEFAULT_HUB_URL, 'https://gc.asaplabs.net');
    // opened over the LAN, the session still says gc.asaplabs.net
    // GET /api/session's link_url (the server already swaps a LAN-only hub
    // URL for the public one); hub_url and the page origin are never used
    t.eq(D.linkFromSession({ name: 'x', link_url: 'https://gc.asaplabs.net' }, 40,
                           { origin: 'http://asapsv1:5560' }),
         'https://gc.asaplabs.net/samples/40');
    t.eq(D.linkFromSession({ link_url: 'https://gc.example.org/' }, 40), 'https://gc.example.org/samples/40');
    t.eq(D.linkFromSession({ hub_url: 'http://asapsv1:5560' }, 40, { origin: 'http://asapsv1:5560' }),
         'https://gc.asaplabs.net/samples/40');
    t.eq(D.linkFromSession(null, 40, { origin: 'http://192.168.1.20:5560' }),
         'https://gc.asaplabs.net/samples/40');

    // ── other runs ───────────────────────────────────────────────────────
    const resolved = {
        lab_id: '40329', sample_id: 9,
        runs: [
            { sample_id: 9, instrument: 'gc2', instrument_name: 'GC-2', injection_dt: '2026-09-28 15:30:00', status: 'final' },
            { sample_id: 4, instrument: 'gc1', instrument_name: '', injection_dt: '2026-09-27 09:05:00', status: 'final' },
            { sample_id: 11, instrument: 'gc2', instrument_name: 'GC-2', injection_dt: '2026-09-29 08:00:00', status: 'error' },
        ],
    };
    t.eq(D.runLabel(resolved.runs[0]), 'GC-2 · Sep 28 15:30');
    t.eq(D.runLabel(resolved.runs[1]), 'gc1 · Sep 27 09:05');
    t.eq(D.runLabel({ instrument: 'gc1', injection_dt: 'garbage' }), 'gc1 · garbage');
    t.eq(D.otherRuns(resolved), [
        { sample_id: 4, href: '/samples/4', label: 'gc1 · Sep 27 09:05', status: 'final' },
        { sample_id: 11, href: '/samples/11', label: 'GC-2 · Sep 29 08:00', status: 'error' },
    ]);
    t.eq(D.otherRuns({ sample_id: 9, runs: [resolved.runs[0]] }), []);
    t.eq(D.otherRuns(null), []);

    // ── v6.0.0: only the pure helpers are left (the classic page's hook is gone)
    t.eq(Object.keys(D).sort(), ['DEFAULT_HUB_URL', 'linkFromSession', 'otherRuns', 'runLabel', 'sampleLink']);
};
