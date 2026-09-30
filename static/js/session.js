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
                if (!r.ok) return null;
                // readJson: a web page here (Cloudflare's, say) is "no session", never
                // "Unexpected token '<'"
                return readJson(r).then(function (res) {
                    const b = res.body;
                    return b && typeof b === 'object' && !Array.isArray(b) && !b.error ? b : null;
                });
            });
    }

    // ── readJson (v4.0.0) ──────────────────────────────────────────────
    // Every fetch that expects data parses it through readJson(resp) →
    // {status, body}. When the answer is not JSON (a hub error page, or a
    // Cloudflare block/challenge/5xx page in front of gc.asaplabs.net) body
    // is {error: "The hub answered HTTP <status> with a web page instead of
    // data (Cloudflare: <title or 'error code: N'>)."} instead of the
    // browser's "Unexpected token '<'". The hub marks its own responses with
    // X-GC-Hub: 1 (api_errors.py); through the tunnel every response also
    // has cf-ray, so only an unmarked HTML page counts as Cloudflare's.
    // An answer under another content type (or none) whose body parses as
    // JSON and is not a web page (does not start with '<') is data too.
    const JSON_CT_RE = /[/+]json\b/i;
    const HTML_RE = /<!doctype|<html/i;
    const CF_CODE_RE = /error code:\s*(\d+)/i;
    const TITLE_RE = /<title[^>]*>([\s\S]*?)<\/title>/i;

    function header(headers, name) {
        if (!headers || typeof headers.get !== 'function') return null;
        const v = headers.get(name);
        return v == null ? null : String(v);
    }

    function decodeEntities(s) {
        return s.replace(/&(amp|lt|gt|quot|#39|#x27|apos);/gi, function (_m, e) {
            return { amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'", '#x27': "'", apos: "'" }[e.toLowerCase()];
        });
    }

    function cloudflareDetail(headers, text) {
        if (header(headers, 'x-gc-hub') === '1') return null;          // the hub's own page
        const code = CF_CODE_RE.exec(text);
        const viaCf = header(headers, 'cf-ray') !== null
            || /cloudflare/i.test(header(headers, 'server') || '');
        if (!code && !(viaCf && HTML_RE.test(text))) return null;
        const title = TITLE_RE.exec(text);
        const t = title ? decodeEntities(title[1]).replace(/\s+/g, ' ').trim().slice(0, 120) : '';
        return t || (code ? 'error code: ' + code[1] : '');
    }

    function looksLikeJson(text) {
        const t = text.trim();
        return t !== '' && t[0] !== '<' && !HTML_RE.test(t);
    }

    function parseBody(status, headers, text) {
        text = typeof text === 'string' ? text : '';
        if (status === 204 && !text) return {};
        if (JSON_CT_RE.test(header(headers, 'content-type') || '')) {
            try { return JSON.parse(text); } catch (_e) { /* a page after all */ }
        } else if (looksLikeJson(text)) {
            try { return JSON.parse(text); } catch (_e) { /* not JSON after all */ }
        }
        const cf = cloudflareDetail(headers, text);
        const extra = cf === null ? '' : (cf ? ' (Cloudflare: ' + cf + ')' : ' (Cloudflare)');
        return { error: 'The hub answered HTTP ' + status + ' with a web page instead of data' +
                        extra + '.' };
    }

    function readJson(resp) {
        return resp.text().then(function (text) {
            return { status: resp.status, body: parseBody(resp.status, resp.headers, text) };
        });
    }

    const api = { safeNext, loginUrl, isLoginRequired, wrapFetch, checkSession, parseBody, readJson };

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
