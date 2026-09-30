/* notifications_panel.js (v5.0 lane R): the shell's notifications panel,
   opened from the bell in the sidebar footer (replaces the classic page's
   dropdown). It lists the hub's notifications newest first, each with its
   level as a glyph and a word (never colour alone), when it came, and a
   "Go to" link to where it is fixed; Dismiss one, or Dismiss all. It follows
   GCLive (through GCLiveAdapter): a new notification, or one dismissed in
   another tab, shows without a reload. Every string is set with
   textContent; answers are parsed with GCSession.readJson; the live
   follow-up GET is a background one (never activity).

   Pure helpers are node-tested (tests/js/notifications_panel.test.js). */
(function (root) {
    'use strict';
    const req = (typeof require === 'function') ? require : null;
    const U = (root && root.GCUi) || (req ? req('./ui_logic.js') : null);

    const LEVELS = {
        error: { glyph: 'error', word: 'Error' },
        warning: { glyph: 'held', word: 'Warning' },
        success: { glyph: 'final', word: 'Done' },
    };
    function level(l) { return Object.assign({}, LEVELS[l] || { glyph: 'never', word: 'Note' }); }

    // Where a notification is dealt with, from its words (the hub's messages
    // carry no link of their own). The first match wins.
    const LINKS = [
        [/\bpurg/i, '/admin/hub#purge-panel', 'Open Purge'],
        [/\bimport/i, '/admin/hub#import-history', 'Open Import history'],
        [/results file|\bexport|\.csv\b|ledger/i, '/admin/hub#exports', 'Open Results files'],
        [/\bagent\b|checked in|check in|offline|installer|\bkey\b/i, '/instruments', 'Open Instruments'],
        [/\bheld\b|awaiting calibration|pending corrections|waiting for/i, '/results?status=held', 'Show held runs'],
        [/correction factor|calibration|method|conflict|reserved/i, '/instruments', 'Open Instruments'],
        [/\bpaused\b|\bresumed\b|processing|hub (did not|could not) start/i, '/admin/hub#status', 'Open Hub status'],
        [/backup|diagnostic|disk/i, '/admin/hub#diagnostics', 'Open Diagnostics'],
    ];
    function link(message) {
        const m = String(message || '');
        if (!m) return null;
        for (const [re, href, text] of LINKS) if (re.test(m)) return { href, text };
        return null;
    }

    /** "30 s ago" within the hour, else the clock time ("09:12", "Sep 28 09:12"). */
    function when(ts, nowMs) {
        if (!ts) return '';
        const at = Date.parse(ts);
        if (isNaN(at)) return '';
        return nowMs - at < 3600 * 1000 ? (U.relTime(ts, nowMs) || '') : U.clockTime(ts, nowMs);
    }

    function sorted(list) {
        const t = (n) => { const v = Date.parse(n && n.ts); return isNaN(v) ? -Infinity : v; };
        return (Array.isArray(list) ? list : []).map((n, i) => [n, i])
            .sort((a, b) => (t(b[0]) - t(a[0])) || (a[1] - b[1])).map((x) => x[0]);
    }
    function title(n) { return n ? 'Notifications · ' + n : 'Notifications'; }
    function badge(n) { return !n ? '' : n > 99 ? '99+' : String(n); }
    function bellLabel(n) { return n ? 'Notifications: ' + n : 'Notifications'; }
    function needsReload(update, count) {
        if (!update) return false;
        if (update.reset) return true;
        if (Array.isArray(update.kinds) && update.kinds.includes('notification')) return true;
        return typeof update.notifications_unread === 'number' && update.notifications_unread !== count;
    }
    function without(list, id) { return (list || []).filter((n) => n.id !== id); }

    const pure = { level, link, when, sorted, title, badge, bellLabel, needsReload, without };
    if (typeof module !== 'undefined' && module.exports) { module.exports = pure; return; }
    root.GCNotesLogic = pure;
    if (typeof document === 'undefined') return;

    // ── the panel ─────────────────────────────────────────────────────────
    const $ = (id) => document.getElementById(id);
    let notes = [];
    let loading = null;

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = String(text);
        return e;
    }
    function toast(msg, kind) { if (root.GCShell && root.GCShell.toast) root.GCShell.toast(msg, kind); }

    function render() {
        const list = sorted(notes);
        const count = list.length;
        const c = $('bell-count');
        if (c) { c.hidden = !count; c.textContent = badge(count); }
        const bell = $('bell');
        if (bell) bell.setAttribute('aria-label', bellLabel(count));
        const head = $('bell-title');
        if (head) head.textContent = title(count);
        const ul = $('bell-list');
        if (!ul) return;
        const now = Date.now();
        ul.replaceChildren(...list.map((n) => {
            const lv = level(n.level);
            const li = el('li', 'note note-' + lv.glyph);
            li.dataset.id = n.id;
            li.setAttribute('data-testid', 'note');
            const g = el('span', 'glyph ' + lv.glyph);
            g.setAttribute('aria-hidden', 'true');
            const body = el('div', 'note-body');
            const meta = el('div', 'note-meta');
            meta.append(el('span', 'note-level', lv.word));
            const w = when(n.ts, now);
            if (w) { meta.append(el('span', 'note-sep', '·')); meta.append(el('span', 'note-when', w)); }
            body.append(meta, el('p', 'note-msg', n.message || ''));
            const go = link(n.message);
            if (go) {
                const a = el('a', 'note-go', go.text);
                a.href = go.href;
                body.append(a);
            }
            const x = el('button', 'icon-btn note-dismiss');
            x.type = 'button';
            x.setAttribute('aria-label', 'Dismiss: ' + String(n.message || '').slice(0, 80));
            x.title = 'Dismiss';
            x.append(el('span', 'ico ico-x'));
            x.addEventListener('click', (ev) => { ev.stopPropagation(); dismiss(n.id, li); });
            li.append(g, body, x);
            return li;
        }));
        const empty = $('bell-empty');
        if (empty) empty.hidden = count > 0;
        const clear = $('bell-clear');
        if (clear) clear.hidden = !count;
    }

    async function load(background) {
        if (loading) return loading;
        loading = (async () => {
            try {
                const headers = { Accept: 'application/json' };
                let r;
                if (background && root.GCLive && typeof root.GCLive.bgFetch === 'function') {
                    r = await root.GCLive.bgFetch('/api/notifications', { headers, cache: 'no-store' });
                } else {
                    if (background) headers['X-GC-Background'] = '1';
                    r = await fetch('/api/notifications', { headers, cache: 'no-store' });
                }
                const res = await root.GCSession.readJson(r);
                if (res.status === 200 && Array.isArray(res.body)) { notes = res.body; render(); }
            } catch (_e) { /* the next live update tries again */ }
            finally { loading = null; }
        })();
        return loading;
    }

    async function post(path) {
        const r = await fetch(path, { method: 'POST', headers: { Accept: 'application/json' } });
        return root.GCSession.readJson(r);
    }

    async function dismiss(id, li) {
        if (li) li.classList.add('leaving');
        notes = without(notes, id);
        render();
        const res = await post('/api/notifications/' + encodeURIComponent(id) + '/dismiss').catch(() => null);
        if (!res || res.status >= 400) {
            toast('Could not dismiss it' + (res && res.body && res.body.error ? ': ' + res.body.error : '.'), 'err');
            load(false);
        }
    }

    async function dismissAll() {
        const n = notes.length;
        notes = [];
        render();
        const res = await post('/api/notifications/dismiss-all').catch(() => null);
        if (!res || res.status >= 400) {
            toast('Could not dismiss them' + (res && res.body && res.body.error ? ': ' + res.body.error : '.'), 'err');
            load(false);
            return;
        }
        toast(n === 1 ? 'Dismissed 1 notification.' : 'Dismissed ' + n + ' notifications.');
        const bell = $('bell');
        if (bell) bell.focus();
    }

    document.addEventListener('DOMContentLoaded', () => {
        if (!$('bell-panel')) return;
        $('bell-clear').addEventListener('click', (ev) => { ev.stopPropagation(); dismissAll(); });
        // opening the panel refreshes it (a quiet GET: not activity)
        $('bell').addEventListener('click', () => { if (!$('bell-panel').hidden) load(true); });
        load(false);
        if (root.GCLiveAdapter) {
            root.GCLiveAdapter.subscribe((u) => { if (needsReload(u, notes.length)) load(true); });
        }
        // "12 s ago" keeps counting while the panel is open
        setInterval(() => { if (!$('bell-panel').hidden) render(); }, 15000);
    });

    root.GCNotes = Object.assign({}, pure, { load, dismiss, dismissAll, list: () => notes.slice() });
})(typeof window !== 'undefined' ? window : null);
