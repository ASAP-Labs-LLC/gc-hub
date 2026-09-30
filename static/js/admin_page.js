/* admin_page.js: the Hub admin page inside the shell (v4.0 lane E2; spec
   "Global status, a way home, and Hub admin").

   The page is one column of anchored sections with a left sub-nav, in the
   order Ryan uses them: running work first, one-time setup (hub address)
   last. This file owns only the frame: which sub-nav item is current as the
   page scrolls, landing on the section a task's Open link names (#purge,
   #load-folder, ...), and the read-only Server section (GET /api/hub/status,
   in words). The sections' behaviour stays in hub_admin.js, purge.js and
   diagnostics.js. Every string is set with textContent. The pure helpers are
   module.exports for tests/js/admin_page.test.js. */
(function (root) {
    'use strict';

    const SECTIONS = [
        { id: 'status', label: 'Running now' },
        { id: 'import-history', label: 'Import history' },
        { id: 'load-folder', label: 'Load folder' },
        { id: 'purge-panel', label: 'Purge' },
        { id: 'exports', label: 'Results files' },
        { id: 'diagnostics', label: 'Diagnostics' },
        { id: 'presets-panel', label: 'Comment presets' },
        { id: 'sessions', label: 'Sessions' },
        { id: 'server', label: 'Server' },
        { id: 'hub-address', label: 'Hub address' },
    ];
    // older or shorter anchors (task Open links, bookmarks) → the section id
    const ALIASES = { purge: 'purge-panel', imports: 'import-history', presets: 'presets-panel',
                      running: 'status', 'results-files': 'exports', 'hub-url': 'hub-address' };

    function sectionForHash(hash) {
        const key = String(hash || '').replace(/^#/, '');
        if (!key) return null;
        if (SECTIONS.some(s => s.id === key)) return key;
        return ALIASES[key] || null;
    }

    /** The section in view: the last whose top is at or above `line`
        ([[id, top], ...] in page order); the last one at the page's bottom. */
    function activeSection(tops, line, atBottom) {
        if (!tops || !tops.length) return null;
        if (atBottom) return tops[tops.length - 1][0];
        let cur = tops[0][0];
        for (const [id, top] of tops) if (top <= line) cur = id;
        return cur;
    }

    function fmt(n) {
        return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    }

    function uptimeText(seconds) {
        const s = Math.max(0, Math.floor(Number(seconds) || 0));
        if (s < 60) return 'under a minute';
        const d = Math.floor(s / 86400);
        const h = Math.floor((s % 86400) / 3600);
        const m = Math.floor((s % 3600) / 60);
        if (d) return d + ' d' + (h ? ' ' + h + ' h' : '');
        if (h) return h + ' h' + (m ? ' ' + m + ' min' : '');
        return m + ' min';
    }

    /** 'final' (running), 'held' (processing paused), 'never' (unknown). */
    function serverState(st) {
        if (!st) return 'never';
        return st.processing_paused ? 'held' : 'final';
    }

    /** The Server section's facts, [[label, text]], in words. */
    function serverRows(st) {
        if (!st) return [];
        const q = st.queue || {};
        const counts = fmt(q.waiting || 0) + ' waiting, ' + fmt(q.running || 0) + ' running';
        const state = st.processing_paused
            ? 'Paused' + (st.processing_paused_by ? ' by ' + st.processing_paused_by : '')
            : (!st.state || st.state === 'running' ? 'Running'
                : String(st.state).charAt(0).toUpperCase() + String(st.state).slice(1));
        const pending = st.exporter ? st.exporter.pending_rows : null;
        const files = typeof pending !== 'number' ? 'Not checked yet'
            : pending === 0 ? 'Up to date'
                : fmt(pending) + (pending === 1 ? ' row' : ' rows') + ' waiting to be written';
        const mb = typeof st.rss_bytes === 'number' ? Math.round(st.rss_bytes / (1024 * 1024)) + ' MB' : null;
        const cpu = typeof st.cpu_percent === 'number' ? 'CPU ' + Math.round(st.cpu_percent) + '%' : null;
        return [
            ['Version', String(st.version || '?') + (st.staged_update ? ' · ' + st.staged_update + ' is ready to install' : '')],
            ['Up for', uptimeText(st.uptime_seconds)],
            ['Processing', state + ' · ' + counts],
            ['Results files', files],
            ['Memory', [mb, cpu].filter(Boolean).join(' · ') || 'Not measured'],
        ];
    }

    const pure = { SECTIONS, sectionForHash, activeSection, serverRows, serverState, uptimeText };
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = pure;
        return;
    }
    root.GCAdminPage = pure;
    if (typeof document === 'undefined') return;

    // ── the page ──────────────────────────────────────────────────────────
    const $ = (id) => document.getElementById(id);

    function navLinks() {
        return Array.from(document.querySelectorAll('.adm-nav a[href^="#"]'));
    }

    function markCurrent(id) {
        for (const a of navLinks()) {
            const on = a.getAttribute('href') === '#' + id;
            a.classList.toggle('active', on);
            if (on) a.setAttribute('aria-current', 'true'); else a.removeAttribute('aria-current');
        }
    }

    function topbarHeight() {
        const tb = document.querySelector('.topbar');
        return tb ? tb.getBoundingClientRect().height : 56;
    }

    let spyQueued = false;
    function spy() {
        spyQueued = false;
        const line = topbarHeight() + 24;
        const tops = SECTIONS.map(s => $(s.id)).filter(Boolean)
            .map(el => [el.id, el.getBoundingClientRect().top]);
        const doc = document.documentElement;
        const atBottom = window.innerHeight + window.scrollY >= doc.scrollHeight - 2;
        const id = activeSection(tops, line, atBottom && window.scrollY > 0);
        if (id) markCurrent(id);
    }
    function queueSpy() {
        if (spyQueued) return;
        spyQueued = true;
        window.requestAnimationFrame(spy);
    }

    function goTo(hash, smooth) {
        const id = sectionForHash(hash);
        const el = id && $(id);
        if (!el) return;
        el.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' });
        markCurrent(id);
    }

    // ── Server: read-only facts (pause, resume and stop are the tray's) ──
    async function loadServer() {
        const box = $('server-facts');
        if (!box) return;
        let st = null;
        try {
            const opts = { cache: 'no-store', headers: { Accept: 'application/json' } };
            const r = (root.GCLive && root.GCLive.bgFetch)
                ? await root.GCLive.bgFetch('/api/hub/status', opts)
                : await fetch('/api/hub/status', Object.assign(opts, {
                    headers: { Accept: 'application/json', 'X-GC-Background': '1' } }));
            const j = await root.GCSession.readJson(r);
            if (j.status === 200) st = j.body;
        } catch (_e) { st = null; }
        box.textContent = '';
        const rows = serverRows(st);
        if (!rows.length) {
            box.appendChild(Object.assign(document.createElement('p'),
                { className: 'caption', textContent: 'Could not read the hub status; it retries in a moment.' }));
            return;
        }
        const state = serverState(st);
        for (const [k, v] of rows) {
            const dt = document.createElement('dt');
            dt.textContent = k;
            const dd = document.createElement('dd');
            if (k === 'Processing') {
                const g = document.createElement('span');
                g.className = 'glyph ' + state;
                g.setAttribute('aria-hidden', 'true');
                dd.appendChild(g);
                dd.appendChild(document.createTextNode(' '));
            }
            dd.appendChild(document.createTextNode(v));
            box.append(dt, dd);
        }
    }

    function init() {
        if (!$('adm-page')) return;
        for (const a of navLinks()) {
            a.addEventListener('click', (ev) => {
                ev.preventDefault();
                const hash = a.getAttribute('href');
                history.replaceState(null, '', hash);
                goTo(hash, true);
            });
        }
        window.addEventListener('scroll', queueSpy, { passive: true });
        window.addEventListener('resize', queueSpy);
        window.addEventListener('hashchange', () => goTo(location.hash, true));
        if (sectionForHash(location.hash)) {
            // after the layout settles (instrument lists, fonts)
            setTimeout(() => goTo(location.hash, false), 60);
        } else {
            markCurrent(SECTIONS[0].id);
        }
        loadServer();
        setInterval(loadServer, 15000);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})(typeof window !== 'undefined' ? window : globalThis);
