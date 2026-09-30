// /instruments (v3.1): the instrument cards, "Add a GC" and the live Activity
// feed. Reads GET /api/instruments (through the shell, one request),
// /api/lem/machines and /api/instruments/activity; live updates through
// GCLiveAdapter (lane A's GCLive, or a 5 s poll). Agent status and "checked
// in N s ago" tick every second on the client. Text only (M5).
(function () {
    'use strict';
    const U = window.GCUi;
    const S = window.GCShell;
    const h = S.h;
    const $ = (id) => document.getElementById(id);
    const enc = encodeURIComponent;

    let instruments = [];
    const agents = {};                  // instrument_id -> {last_seen, version, host, state, last_error}
    let lem = null;
    let feed = [];

    // ── cards ───────────────────────────────────────────────────────────────
    function card(inst) {
        const a = agents[inst.id] || inst.agent || null;
        const st = U.agentStatus(a, Date.now());
        const pill = h('span', { className: 'pill ' + (st.glyph === 'never' ? '' : st.glyph), 'data-role': 'agent-pill' },
            S.glyph(st.glyph), h('span', { text: st.label }));
        const lemText = U.lemTitle(lem, inst.lem_machine_uid);
        const agentLine = !a || !a.last_seen
            ? h('div', { text: inst.has_token ? 'Agent installed; waiting for its first check-in' : 'Agent not installed yet' })
            : h('div', {}, h('span', { text: 'Agent · ' }), h('b', { 'data-role': 'agent-since', text: st.since }),
                a.version ? h('span', { text: ' · v' + a.version }) : null);
        const setup = inst.setup;
        const pct = setup ? Math.round(100 * setup.done / setup.total) : 0;
        const stat = (n, label, testid) => h('div', { className: 'stat', 'data-testid': testid }, h('b', { text: String(n || 0) }), h('span', { text: label }));
        return h('article', { className: 'card inst-card' + (inst.enabled ? '' : ' disabled'), 'data-testid': 'instrument-card',
                              'data-instrument': inst.id },
            h('div', { className: 'head' },
                h('h2', {}, h('a', { href: U.instrumentHref(inst.id), text: inst.name })), pill),
            h('div', { className: 'meta' },
                h('div', {}, h('span', { text: 'LEM machine · ' }), h('b', { text: lemText || 'not chosen' })),
                agentLine,
                inst.enabled ? null : h('div', { text: 'Disabled: its agent is refused' })),
            h('div', { className: 'stats' },
                stat(inst.today, 'samples today', 'stat-today'), stat(inst.held, 'held', 'stat-held'),
                stat(inst.export_pending, 'waiting to export', 'stat-export')),
            h('div', { className: 'setup-row' },
                h('span', { text: 'Setup' }),
                h('b', { 'data-testid': 'setup-label', text: U.setupLabel(setup) || '—' })),
            h('div', { className: 'progress', role: 'progressbar', 'aria-label': 'Setup progress',
                       'aria-valuemin': 0, 'aria-valuemax': 8, 'aria-valuenow': setup ? setup.done : 0 },
                h('span', { style: 'width:' + pct + '%' })));
    }

    function renderCards() {
        const grid = $('inst-grid');
        const add = $('add-card');
        grid.replaceChildren(...instruments.map(card), add);
        grid.setAttribute('aria-busy', 'false');
    }

    // tick: agent status text and the "checked in … ago" line
    function tick() {
        const now = Date.now();
        document.querySelectorAll('[data-testid="instrument-card"]').forEach(el => {
            const inst = instruments.find(i => i.id === el.dataset.instrument);
            if (!inst) return;
            const a = agents[inst.id] || inst.agent;
            const st = U.agentStatus(a, now);
            const pill = el.querySelector('[data-role="agent-pill"]');
            if (pill) {
                pill.className = 'pill ' + (st.glyph === 'never' ? '' : st.glyph);
                pill.firstChild.className = 'glyph ' + st.glyph;
                pill.lastChild.textContent = st.label;
            }
            const since = el.querySelector('[data-role="agent-since"]');
            if (since && st.since) since.textContent = st.since;
        });
        document.querySelectorAll('#feed [data-at]').forEach(el => {
            el.textContent = U.relTime(el.dataset.at, now) || '';
        });
    }

    // ── the Activity feed ───────────────────────────────────────────────────
    function feedItem(e, isNew) {
        // the sample's lab ID links to its page (/samples/<id>), with its injection time
        const sample = U.activitySample(e);
        const txt = h('span', { className: 'txt' },
            ...U.activityText(e).map(s => {
                if (s.strong && sample && s.text === e.lab_id) {
                    return h('a', { className: 'link', href: sample.href, 'data-testid': 'activity-sample' }, h('b', { text: s.text }));
                }
                return s.strong ? h('b', { text: s.text }) : document.createTextNode(s.text);
            }),
            sample && sample.injected ? h('span', { className: 'caption', text: ' · ' + sample.injected }) : null);
        const at = h('time', { className: 'at', datetime: e.at || '', 'data-at': e.at || '',
                               title: e.at ? new Date(e.at).toLocaleString() : '',
                               text: U.relTime(e.at, Date.now()) || '' });
        return h('li', { className: isNew ? 'new' : null, 'data-key': e.key, 'data-kind': e.kind },
            h('span', { className: 'ic', 'aria-hidden': 'true' }, S.icon(U.activityIcon(e.kind))), txt, at);
    }

    async function loadFeed(first) {
        const r = await S.getJSON('/api/instruments/activity?limit=30', !first);
        if (r.status !== 200 || !r.body) return;
        const merged = U.mergeActivity(feed, r.body.entries || [], 30);
        const fresh = first ? new Set() : new Set(merged.added);
        feed = merged.list;
        $('feed').replaceChildren(...feed.map(e => feedItem(e, fresh.has(e.key))));
        $('feed-empty').hidden = feed.length > 0;
    }

    // ── live ────────────────────────────────────────────────────────────────
    let feedTimer = null;
    function refreshFeedSoon() {
        clearTimeout(feedTimer);
        feedTimer = setTimeout(() => loadFeed(false), 300);
    }
    function onLive(update) {
        let seen = false;
        for (const a of update.agents || []) {
            const old = agents[a.instrument_id] || (instruments.find(i => i.id === a.instrument_id) || {}).agent || {};
            if (a.last_seen && a.last_seen !== old.last_seen) seen = true;
            agents[a.instrument_id] = Object.assign({}, old, a, { state: a.status !== undefined ? a.status : old.state });
        }
        if (update.reset || (update.samples && update.samples.length) || (update.instruments && update.instruments.length) || seen) {
            refreshFeedSoon();
        }
        tick();
    }

    S.onInstruments((body) => {
        instruments = body.instruments || [];
        // v4.0 lane E: the hub's age was read now (read_at), so it ticks from here
        const readAt = Date.now();
        for (const i of instruments) {
            if (i.agent) agents[i.id] = Object.assign({}, agents[i.id] || {}, i.agent, { read_at: readAt });
        }
        renderCards();
    });
    S.getJSON('/api/lem/machines').then(r => {
        if (r.status === 200) { lem = r.body; if (instruments.length) renderCards(); }
    }).catch(() => null);
    loadFeed(true);
    if (window.GCLiveAdapter) window.GCLiveAdapter.subscribe(onLive);
    setInterval(tick, 1000);
})();
