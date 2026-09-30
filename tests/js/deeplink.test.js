// deeplink.js: sendable sample links (v3.1). The URL → what to open, the tab
// ids, the copied link (always the hub URL, never location.origin), the
// "Other runs" line, and the standard picked by ?standard=.
const D = require('../../static/js/deeplink.js');

module.exports = (t) => {
    // ── parseLocation ────────────────────────────────────────────────────
    t.eq(D.parseLocation('/lab/40329', ''), { kind: 'lab', labId: '40329' });
    t.eq(D.parseLocation('/lab/40318-RERUN-2', ''), { kind: 'lab', labId: '40318-RERUN-2' });
    t.eq(D.parseLocation('/lab/40318-rerun-2/', ''), { kind: 'lab', labId: '40318-rerun-2' });
    // the browser keeps the path encoded: decoded exactly once
    t.eq(D.parseLocation('/lab/40318%2DRERUN%2D2', ''), { kind: 'lab', labId: '40318-RERUN-2' });
    t.eq(D.parseLocation('/lab/A%2541', ''), { kind: 'lab', labId: 'A%41' });
    t.eq(D.parseLocation('/lab/40%20329', ''), { kind: 'lab', labId: '40 329' });
    t.eq(D.parseLocation('/lab/bad%E0%A4', ''), { kind: 'lab', labId: 'bad%E0%A4' });   // malformed: as is
    t.eq(D.parseLocation('/lab/', ''), null);
    t.eq(D.parseLocation('/lab/a/b', ''), null);

    t.eq(D.parseLocation('/samples/12', ''), { kind: 'sample', sampleId: 12, tab: 'dashboard', standard: null });
    t.eq(D.parseLocation('/samples/12/', ''), { kind: 'sample', sampleId: 12, tab: 'dashboard', standard: null });
    t.eq(D.parseLocation('/samples/12/compare', ''),
         { kind: 'sample', sampleId: 12, tab: 'analysis', standard: null });
    t.eq(D.parseLocation('/samples/12/compare', '?standard=Diesel%20B'),
         { kind: 'sample', sampleId: 12, tab: 'analysis', standard: 'Diesel B' });
    t.eq(D.parseLocation('/samples/12/compare', '?standard=Jet+A'),
         { kind: 'sample', sampleId: 12, tab: 'analysis', standard: 'Jet A' });
    t.eq(D.parseLocation('/samples/12/compare', '?standard='),
         { kind: 'sample', sampleId: 12, tab: 'analysis', standard: null });
    t.eq(D.parseLocation('/samples/12/data', ''), { kind: 'sample', sampleId: 12, tab: 'data', standard: null });
    t.eq(D.parseLocation('/samples/12/other', ''), null);
    t.eq(D.parseLocation('/samples/x', ''), null);
    t.eq(D.parseLocation('/samples/0', ''), null);

    // v5.0.0: the classic page's own links, under /classic
    t.eq(D.parseLocation('/classic/lab/40329', ''), { kind: 'lab', labId: '40329' });
    t.eq(D.parseLocation('/classic/samples/12/compare', '?standard=D'),
         { kind: 'sample', sampleId: 12, tab: 'analysis', standard: 'D' });
    t.eq(D.parseLocation('/classic', '?q=40329'), { kind: 'search', q: '40329' });
    t.eq(D.parseLocation('/classic/', ''), null);
    t.eq(D.parseLocation('/classicx/samples/12', ''), null);
    t.eq(D.parseLocation('/', '?q=40329'), { kind: 'search', q: '40329' });
    t.eq(D.parseLocation('/', '?q=%3Cb%3E'), { kind: 'search', q: '<b>' });
    t.eq(D.parseLocation('/', ''), null);
    t.eq(D.parseLocation('/', '?q=%20'), null);
    t.eq(D.parseLocation('/instruments', ''), null);
    t.eq(D.parseLocation('', ''), null);
    t.eq(D.parseLocation(undefined, undefined), null);

    // ── tabs ─────────────────────────────────────────────────────────────
    t.eq(D.tabId('dashboard'), 'tab-dashboard');
    t.eq(D.tabId('analysis'), 'tab-analysis');
    t.eq(D.tabId('data'), 'tab-distilldata');
    t.eq(D.tabId('nope'), 'tab-dashboard');
    t.eq(D.isAdvancedTab('data'), true);
    t.eq(D.isAdvancedTab('analysis'), false);

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

    // the one row a link needs when the loaded list leaves it out: its lab
    // ID on its instrument on its day (a handful of rows, never crowded out)
    t.eq(D.exactFileUrl({ lab_id: '40304', instrument: 'gc2', injection_datetime: '2026-09-25 14:23:00' }),
         '/api/files?limit=50&q=40304&instrument=gc2&date_from=2026-09-25&date_to=2026-09-25');
    t.eq(D.exactFileUrl({ lab_id: 'A&B', instrument: 'gc 1', injection_datetime: '2026-09-25T01:02:03' }),
         '/api/files?limit=50&q=A%26B&instrument=gc%201&date_from=2026-09-25&date_to=2026-09-25');
    t.eq(D.exactFileUrl({ lab_id: '1' }), '/api/files?limit=50&q=1');
    t.eq(D.exactFileUrl(null), null);

    t.eq(D.labApiUrl('40318-RERUN-2'), '/api/lab/40318-RERUN-2');
    t.eq(D.labApiUrl('A%41'), '/api/lab/A%2541');
    t.eq(D.labApiUrl('a b'), '/api/lab/a%20b');

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
    t.eq(D.otherRunsTitle('40329'), 'Other runs of 40329:');

    // ── ?standard= ───────────────────────────────────────────────────────
    const stds = [{ name: 'Diesel' }, { name: 'diesel B' }, { name: 'Jet A' }];
    t.eq(D.findStandard(stds, 'Diesel'), { name: 'Diesel' });
    t.eq(D.findStandard(stds, 'DIESEL B'), { name: 'diesel B' });
    t.eq(D.findStandard(stds, 'jet a'), { name: 'Jet A' });
    t.eq(D.findStandard(stds, 'Gasoline'), null);
    t.eq(D.findStandard([], 'x'), null);
    t.eq(D.findStandard(null, 'x'), null);

    // ── the sample the list shows ────────────────────────────────────────
    const files = [{ sample_id: 3, instrument: 'gc1' }, { sample_id: 9, instrument: 'gc2' }];
    t.eq(D.findFile(files, 9), files[1]);
    t.eq(D.findFile(files, '3'), files[0]);
    t.eq(D.findFile(files, 5), null);
    t.eq(D.findFile(null, 5), null);

    // ── wiring (source checks: the browser hook is not loaded under node) ──
    const src = require('fs').readFileSync(require('path').join(__dirname, '../../static/js/deeplink.js'), 'utf8');
    // every answer is parsed with GCSession.readJson, never resp.json()
    t.eq(/\.json\(\)/.test(src), false);
    t.eq((src.match(/GCSession\.readJson\(/g) || []).length >= 2, true);
    // the start-up warm-up of /api/session is the page's own fetch: a
    // background GET (not activity); the lookups a link asks for are not
    t.eq(/_loadSession\(true\)/.test(src), true);
    t.eq(/GCLive\.bgFetch\('\/api\/session'/.test(src), true);
};
