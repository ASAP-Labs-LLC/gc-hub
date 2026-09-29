// session.js: the sign-in plumbing every page loads first (spec D3/D6 rev 2):
// the `next` rule (mirrors web_auth.safe_next), the login URL, the central
// 401 handling that wraps window.fetch, and the SSE check.
const S = require('../../static/js/session.js');

// A synchronous thenable, so the wrapper can be tested without a runtime.
function sync(value) { return { then(cb) { return sync(cb(value)); } }; }
function resp(status, loginHeader) {
    return { status, ok: status >= 200 && status < 300,
             headers: { get: (k) => (loginHeader && k === 'X-GC-Login-Required' ? '1' : null) },
             json: () => ({ name: 'Ryan C', method: 'password' }) };
}

module.exports = (t) => {
    // next: a relative path, never //host, /\host, /api/ or /login, no controls
    t.eq(S.safeNext('/instruments?x=1'), '/instruments?x=1');
    t.eq(S.safeNext('/'), '/');
    t.eq(S.safeNext('//evil.example/'), '/');
    t.eq(S.safeNext('/\\evil.example'), '/');
    t.eq(S.safeNext('https://evil.example/'), '/');
    t.eq(S.safeNext('/api/files'), '/');
    t.eq(S.safeNext('/API/files'), '/');
    t.eq(S.safeNext('/login?next=/x'), '/');
    t.eq(S.safeNext('/a\nb'), '/');
    t.eq(S.safeNext('/a\\b'), '/');
    t.eq(S.safeNext(''), '/');
    t.eq(S.safeNext(null), '/');
    t.eq(S.safeNext('/' + 'x'.repeat(3000)), '/');

    t.eq(S.loginUrl({ pathname: '/calibration', search: '?instrument=gc2' }),
        '/login?next=%2Fcalibration%3Finstrument%3Dgc2');
    t.eq(S.loginUrl({ pathname: '/api/x', search: '' }), '/login?next=%2F');

    // only the gate's 401 (with its header) sends the page to /login
    t.eq(S.isLoginRequired(resp(401, true)), true);
    t.eq(S.isLoginRequired(resp(401, false)), false);   // a wrong password, say
    t.eq(S.isLoginRequired(resp(403, true)), false);
    t.eq(S.isLoginRequired(resp(200, false)), false);
    t.eq(S.isLoginRequired(null), false);

    let redirects = 0;
    const calls = [];
    const fake = (status, hdr) => function (url, opts) { calls.push([url, opts]); return sync(resp(status, hdr)); };
    const wrapped = S.wrapFetch(fake(401, true), () => { redirects++; });
    t.eq(wrapped.__gcWrapped, true);
    let got = null;
    wrapped('/api/files', { method: 'GET' }).then((r) => { got = r; });
    t.eq(redirects, 1);
    t.eq(got.status, 401);                 // the caller still gets the response
    t.eq(calls[0], ['/api/files', { method: 'GET' }]);

    const ok = S.wrapFetch(fake(200, false), () => { redirects++; });
    ok('/api/files');
    const wrong = S.wrapFetch(fake(401, false), () => { redirects++; });
    wrong('/api/login/admin');
    t.eq(redirects, 1);

    // the SSE stream can't see a 401: onerror asks /api/session
    let n = 0;
    S.checkSession(fake(401, true), () => { n++; });
    t.eq(n, 1);
    let who = null;
    S.checkSession(fake(200, false), () => { n++; }).then((s) => { who = s; });
    t.eq(n, 1);
    t.eq(who, { name: 'Ryan C', method: 'password' });
};
