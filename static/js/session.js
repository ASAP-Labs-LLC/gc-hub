/* session.js: sign-in plumbing, loaded FIRST on every page (spec D3/D6).

   - Central 401 handling: window.fetch is wrapped once, so every fetch on
     the page (app.js api() and its other calls, comments.js, instruments.js,
     hub_admin.js, the inline scripts of calibration.html and admin_setup.html)
     sends the page to /login?next=<this page> when the session gate answers
     401 with X-GC-Login-Required (a wrong-password 401 has no such header).
   - The SSE stream can't see a status: its onerror calls
     GCSession.check(), which asks GET /api/session.
   - The "Sign out" button next to the version badge: POST /api/logout.
   - GCSession.whoami(): {name, method} of the signed-in person (comments).

   Pure helpers are exported for node (tests/js/session.test.js). The `next`
   rule mirrors web_auth.safe_next on the server. */
(function (root) {
    'use strict';

    const NEXT_RE = /^\/(?![\/\\])[^\\\x00-\x1f]*$/;
    const HEADER = 'X-GC-Login-Required';

    function safeNext(raw) {
        if (typeof raw !== 'string' || raw.length > 2000 || !NEXT_RE.test(raw)) return '/';
        const low = raw.toLowerCase();
        if (low === '/api' || low.startsWith('/api/') || low.startsWith('/login')) return '/';
        return raw;
    }

    function loginUrl(loc) {
        const here = ((loc && loc.pathname) || '/') + ((loc && loc.search) || '');
        return '/login?next=' + encodeURIComponent(safeNext(here));
    }

    function isLoginRequired(resp) {
        return !!(resp && resp.status === 401 && resp.headers && typeof resp.headers.get === 'function'
            && resp.headers.get(HEADER) === '1');
    }

    function wrapFetch(fetchFn, onLoginRequired) {
        const wrapped = function () {
            return fetchFn.apply(this, arguments).then(function (resp) {
                if (isLoginRequired(resp)) onLoginRequired();
                return resp;
            });
        };
        wrapped.__gcWrapped = true;
        return wrapped;
    }

    function checkSession(fetchFn, onLoginRequired) {
        return fetchFn('/api/session', { cache: 'no-store', headers: { Accept: 'application/json' } })
            .then(function (r) {
                if (r.status === 401) { onLoginRequired(); return null; }
                return r.ok ? r.json() : null;
            });
    }

    const api = { safeNext, loginUrl, isLoginRequired, wrapFetch, checkSession };

    if (typeof module !== 'undefined' && module.exports) module.exports = api;

    if (typeof window === 'undefined' || typeof document === 'undefined') return;
    root.GCSession = api;

    let redirecting = false;
    function goLogin() {
        if (redirecting) return;
        redirecting = true;
        window.location.assign(loginUrl(window.location));
    }
    api.goLogin = goLogin;

    const onLoginPage = /^\/login(\/|$)/.test(window.location.pathname);
    if (typeof window.fetch === 'function' && !window.fetch.__gcWrapped && !onLoginPage) {
        window.fetch = wrapFetch(window.fetch.bind(window), goLogin);
    }

    api.check = function () {
        return checkSession(window.fetch, goLogin).catch(function () { return null; });
    };
    let who = null;
    api.whoami = function () {
        if (!who) who = api.check();
        return who;
    };

    function signOut(btn) {
        if (btn) btn.disabled = true;
        window.fetch('/api/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                      body: '{}' })
            .catch(function () { return null; })
            .then(function () { window.location.assign('/login'); });
    }
    api.signOut = signOut;

    document.addEventListener('DOMContentLoaded', function () {
        const btn = document.getElementById('sign-out');
        if (btn) btn.addEventListener('click', function () { signOut(btn); });
    });
})(typeof window !== 'undefined' ? window : globalThis);
