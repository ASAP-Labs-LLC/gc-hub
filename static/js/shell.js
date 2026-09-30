// The v3.1 shell (templates/_shell.html, _layout.html): theme, the sidebar
// rail, the user menu, Recent, the bell, the Live line, the "Setup guide ·
// Step N" nav item, the admin-password prompt (a closure, 15 minutes, never
// storage) and small DOM helpers the pages share (GCShell). Loaded in <head>
// right after session.js so the theme is set before the page paints. Every
// server or browser-stored string is set with textContent.
(function () {
    'use strict';
    const U = window.GCUi;
    const THEME_KEY = 'gc.theme';
    const SIDEBAR_KEY = 'gc.sidebar';
    const RECENT_KEY = 'gc.recent';

    function load(key) {
        try { return window.localStorage.getItem(key); } catch (_e) { return null; }
    }
    function save(key, value) {
        try {
            if (value === null) window.localStorage.removeItem(key);
            else window.localStorage.setItem(key, value);
        } catch (_e) { /* private mode, blocked storage: the default applies */ }
    }

    // ── theme, before first paint ───────────────────────────────────────────
    const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    function themePref() {
        const v = load(THEME_KEY);
        return v === 'dark' || v === 'system' || v === 'light' ? v : 'light';
    }
    function applyTheme() {
        document.documentElement.setAttribute('data-theme', U.resolveTheme(themePref(), !!(media && media.matches)));
    }
    applyTheme();
    if (media && media.addEventListener) media.addEventListener('change', applyTheme);
    const side = load(SIDEBAR_KEY);
    if (side === 'rail' || side === 'full') document.documentElement.setAttribute('data-sidebar', side);

    // ── DOM helpers (text only) ─────────────────────────────────────────────
    function h(tag, props, ...children) {
        const el = document.createElement(tag);
        for (const [k, v] of Object.entries(props || {})) {
            if (v === null || v === undefined || v === false) continue;
            if (k === 'className') el.className = v;
            else if (k === 'text') el.textContent = String(v);
            else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
            else if (k === 'value') el.value = v;
            else if (k === 'checked') el.checked = !!v;
            else el.setAttribute(k, v === true ? '' : String(v));
        }
        for (const c of children.flat()) {
            if (c === null || c === undefined || c === false) continue;
            el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
        }
        return el;
    }
    const $ = (id) => document.getElementById(id);
    function icon(name) { return h('span', { className: 'ico ico-' + name, 'aria-hidden': 'true' }); }
    function glyph(kind, label) {
        return h('span', { className: 'glyph ' + kind, role: label ? 'img' : null, 'aria-label': label || null,
                           'aria-hidden': label ? null : 'true' });
    }

    let toastTimer = null;
    function toast(message, kind) {
        const t = $('toast');
        if (!t) return;
        t.textContent = message || '';
        t.className = 'toast' + (message ? ' show' : '') + (kind === 'err' ? ' err' : '');
        clearTimeout(toastTimer);
        if (message) toastTimer = setTimeout(() => { t.className = 'toast'; }, kind === 'err' ? 9000 : 5000);
    }

    // `background`: this GET comes from a live update or a timer, not a click,
    // so it carries X-GC-Background: 1 (never activity, never keeps the session
    // alive): through GCLive.bgFetch when live.js provides it.
    async function getJSON(path, background) {
        const headers = { Accept: 'application/json' };
        let r;
        if (background && window.GCLive && typeof window.GCLive.bgFetch === 'function') {
            r = await window.GCLive.bgFetch(path, { headers, cache: 'no-store' });
        } else {
            if (background) headers['X-GC-Background'] = '1';
            r = await fetch(path, { headers, cache: 'no-store' });
        }
        // GCSession.readJson: a web page where data was expected becomes a sentence
        return window.GCSession.readJson(r);
    }

    // ── the admin password: in this closure only, for 15 minutes ────────────
    // v4.0 lane E2: ONE unlock per page. With admin_unlock.js loaded (the
    // layout loads it first) the gate is a view of GCAdminUnlock.page, so the
    // shell's dialog, Hub admin's Unlock and Calibration's Save share it.
    const AU = window.GCAdminUnlock;
    const gate = AU && AU.gateFor ? AU.gateFor(AU.page) : U.makeAdminGate({ ttlMs: 15 * 60 * 1000 });
    let chipTimer = null;
    function syncUnlockChip() {
        const chip = $('unlock-chip');
        if (!chip) return;
        const left = gate.remainingMs();
        chip.hidden = left <= 0;
        $('unlock-text').textContent = 'Admin unlocked · ' + Math.max(1, Math.ceil(left / 60000)) + ' min';
        if (left <= 0 && chipTimer) { clearInterval(chipTimer); chipTimer = null; }
        if (left > 0 && !chipTimer) chipTimer = setInterval(syncUnlockChip, 15000);
    }
    if (gate.onChange) gate.onChange(() => syncUnlockChip());

    function askPassword(reason, error) {
        const dlg = $('admin-dialog');
        return new Promise((resolve) => {
            const input = $('admin-password');
            const form = $('admin-form');
            $('admin-reason').textContent = reason ||
                'This change needs the admin password. It is kept in this tab for 15 minutes, never saved.';
            const err = $('admin-error');
            err.textContent = error || '';
            err.hidden = !error;
            input.value = '';
            let done = false;
            function finish(value) {
                if (done) return;
                done = true;
                form.removeEventListener('submit', onSubmit);
                $('admin-cancel').removeEventListener('click', onCancel);
                dlg.removeEventListener('cancel', onCancel);
                if (dlg.open) dlg.close();
                input.value = '';
                resolve(value);
            }
            function onSubmit(ev) { ev.preventDefault(); finish(input.value || null); }
            function onCancel(ev) { if (ev) ev.preventDefault(); finish(null); }
            form.addEventListener('submit', onSubmit);
            $('admin-cancel').addEventListener('click', onCancel);
            dlg.addEventListener('cancel', onCancel);
            if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
            input.focus();
        });
    }

    // POST an admin route with the password added. Asks for it when this tab
    // has none (or it expired), and again once after "Incorrect password".
    // -> {status, body} (or the raw Response with opts.raw), or null if cancelled.
    async function adminPost(path, payload, opts) {
        let pw = gate.get();
        let error = null;
        for (let attempt = 0; attempt < 3; attempt++) {
            const fresh = pw === null;
            if (fresh) {
                pw = await askPassword(opts && opts.reason, error);
                if (pw === null) return null;
            }
            const r = await fetch(path, {
                method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify(Object.assign({}, payload || {}, { password: pw })),
            });
            if (r.status === 403) {
                const body = (await window.GCSession.readJson(r)).body;
                const msg = (body && body.error) || 'The admin password was refused.';
                gate.clear();
                syncUnlockChip();
                pw = null;
                if (/incorrect password/i.test(msg)) { error = 'That password was not accepted. Try again.'; continue; }
                toast(msg, 'err');
                return { status: 403, body };
            }
            if (fresh) {
                gate.set(pw);
                if (AU && AU.accepted) AU.accepted(pw);
                syncUnlockChip();
            }
            if (opts && opts.raw) return r;
            const body = (await window.GCSession.readJson(r)).body;
            if (r.status >= 400 && !(opts && opts.quiet)) toast((body && body.error) || ('HTTP ' + r.status), 'err');
            return { status: r.status, body };
        }
        toast('The admin password was not accepted.', 'err');
        return null;
    }

    // ── instruments (for the Setup guide nav item; pages reuse the answer) ──
    const instrumentListeners = [];
    let lastInstruments = null;
    async function loadInstruments(background) {
        const r = await getJSON('/api/instruments', background);
        if (r.status !== 200 || !r.body) return null;
        lastInstruments = r.body;
        syncSetupNav(r.body.instruments || []);
        for (const fn of instrumentListeners.slice()) {
            try { fn(r.body); } catch (e) { console.error(e); }
        }
        return r.body;
    }
    function onInstruments(fn) {
        instrumentListeners.push(fn);
        if (lastInstruments) fn(lastInstruments);
    }
    function syncSetupNav(list) {
        const item = $('nav-setup');
        if (!item) return;
        const nav = U.setupNav(list);
        const here = document.body.dataset.nav === 'setup';
        item.hidden = !nav && !here;
        $('nav-setup-step').textContent = nav ? nav.text : '';
        if (nav && !here) item.setAttribute('href', '/setup?instrument=' + encodeURIComponent(nav.instrument_id));
    }

    // ── Recent, on this computer ────────────────────────────────────────────
    function recents() {
        try { return U.recentClean(JSON.parse(load(RECENT_KEY) || '[]')); } catch (_e) { return []; }
    }
    function addRecent(item) {
        save(RECENT_KEY, JSON.stringify(U.recentAdd(recents(), item, 6, Date.now())));
        renderRecent();
    }
    function renderRecent() {
        const ul = $('recent-list');
        if (!ul) return;
        const list = recents();
        ul.replaceChildren(...list.map(r => h('li', {}, h('a', { href: r.href },
            h('span', { text: r.label }), h('span', { className: 'when', text: U.clockTime(new Date(r.at || 0).toISOString()) })))));
        $('recent-empty').hidden = list.length > 0;
    }

    // ── bell ────────────────────────────────────────────────────────────────
    let notes = [];
    async function loadNotes(background) {
        const r = await getJSON('/api/notifications', !!background);
        if (r.status !== 200 || !Array.isArray(r.body)) return;
        notes = r.body;
        renderNotes(notes.length);
    }
    function renderNotes(count) {
        const c = $('bell-count');
        c.hidden = !count;
        c.textContent = count > 99 ? '99+' : String(count || '');
        $('bell').setAttribute('aria-label', count ? 'Notifications: ' + count : 'Notifications');
        const ul = $('bell-list');
        ul.replaceChildren(...notes.map(n => h('li', {},
            glyph(n.level === 'error' ? 'error' : n.level === 'warning' ? 'held' : 'never'),
            h('span', { className: 'msg', text: n.message || '' }),
            h('button', { type: 'button', className: 'btn btn-ghost btn-sm', text: 'Dismiss',
                          onclick: () => dismiss(n.id) }))));
        $('bell-empty').hidden = notes.length > 0;
        $('bell-clear').hidden = !notes.length;
    }
    async function dismiss(id) {
        await fetch('/api/notifications/' + encodeURIComponent(id) + '/dismiss', { method: 'POST' });
        loadNotes();
    }

    // ── menus ───────────────────────────────────────────────────────────────
    function toggle(panel, button, open) {
        const show = open === undefined ? panel.hidden : open;
        panel.hidden = !show;
        button.setAttribute('aria-expanded', show ? 'true' : 'false');
    }
    function syncThemeSwitch() {
        const pref = themePref();
        document.querySelectorAll('[data-theme-choice]').forEach(b =>
            b.setAttribute('aria-checked', b.dataset.themeChoice === pref ? 'true' : 'false'));
    }

    // ── live ────────────────────────────────────────────────────────────────
    let lastUpdateAt = null;
    function renderLive() {
        const status = window.GCLiveAdapter ? window.GCLiveAdapter.status() : null;
        const dot = $('live-dot');
        const text = $('live-text');
        if (!dot) return;
        if (status && status.connected) {
            dot.dataset.state = 'live';
            const ago = U.relTime(status.last_ok_at ? new Date(status.last_ok_at).toISOString() : null, Date.now());
            text.textContent = 'Live · updated ' + (ago || 'just now');
        } else if (status && status.error) {
            dot.dataset.state = 'error';
            text.textContent = 'Not connected · retrying';
        } else {
            dot.dataset.state = 'connecting';
            text.textContent = 'Connecting…';
        }
    }

    function onLive(update) {
        lastUpdateAt = Date.now();
        if (typeof update.notifications_unread === 'number' && update.notifications_unread !== notes.length) loadNotes(true);
        const hub = update.hub || null;
        const upd = $('menu-update');
        if (upd && hub && hub.staged_update) {
            upd.hidden = false;
            upd.textContent = 'Update ready: restart to install ' + String(hub.staged_update.version || hub.staged_update);
        }
        if (update.reset || (update.instruments && update.instruments.length)) loadInstruments(true);
        renderLive();
    }

    // ── wiring ──────────────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => {
        if (!$('sidebar')) return;
        const name = $('user-name').textContent.trim();
        $('user-initials').textContent = U.initials(name);

        $('sb-toggle').addEventListener('click', () => {
            const railNow = getComputedStyle($('sidebar')).width.replace('px', '') < 120;
            const next = railNow ? 'full' : 'rail';
            document.documentElement.setAttribute('data-sidebar', next);
            save(SIDEBAR_KEY, next);
        });

        const chip = $('user-chip');
        const menu = $('user-menu');
        const bell = $('bell');
        const panel = $('bell-panel');
        chip.addEventListener('click', (ev) => { ev.stopPropagation(); toggle(panel, bell, false); toggle(menu, chip); syncThemeSwitch(); });
        bell.addEventListener('click', (ev) => { ev.stopPropagation(); toggle(menu, chip, false); toggle(panel, bell); });
        document.addEventListener('click', (ev) => {
            if (!menu.hidden && !menu.contains(ev.target)) toggle(menu, chip, false);
            if (!panel.hidden && !panel.contains(ev.target)) toggle(panel, bell, false);
        });
        document.addEventListener('keydown', (ev) => {
            if (ev.key === 'Escape') { toggle(menu, chip, false); toggle(panel, bell, false); }
        });
        document.querySelectorAll('[data-theme-choice]').forEach(b => b.addEventListener('click', (ev) => {
            ev.stopPropagation();
            save(THEME_KEY, b.dataset.themeChoice);
            applyTheme();
            syncThemeSwitch();
        }));
        $('menu-signout').addEventListener('click', () => {
            if (window.GCSession && window.GCSession.signOut) window.GCSession.signOut($('menu-signout'));
        });
        $('bell-clear').addEventListener('click', async () => {
            await fetch('/api/notifications/dismiss-all', { method: 'POST' });
            loadNotes();
        });
        if ($('unlock-lock')) {
            $('unlock-lock').addEventListener('click', () => { gate.clear(); syncUnlockChip(); toast('Admin locked.'); });
        }

        renderRecent();
        loadNotes();
        loadInstruments(false);
        if (window.GCLiveAdapter) window.GCLiveAdapter.subscribe(onLive);
        setInterval(renderLive, 1000);
        renderLive();
    });

    window.GCShell = {
        h, icon, glyph, toast, getJSON, adminPost, askPassword, loadInstruments, onInstruments,
        addRecent, applyTheme, lastUpdateAt: () => lastUpdateAt,
    };
})();
