// session.js: the sign-in plumbing every page loads first (spec D3/D6 rev 2):
// the `next` rule (mirrors web_auth.safe_next), the login URL, the central
// 401 handling that wraps window.fetch, and the SSE check.
const S = require('../../static/js/session.js');

// A synchronous thenable, so the wrapper can be tested without a runtime.
// Like a Promise, a thenable returned from a callback is adopted (flattened).
function sync(value) {
    if (value && typeof value.then === 'function') return value;
    return { then(cb) { return sync(cb(value)); } };
}
function resp(status, loginHeader, text, contentType) {
    const body = text === undefined ? '{"name":"Ryan C","method":"password"}' : text;
    const ct = contentType === undefined ? 'application/json' : contentType;
    return { status, ok: status >= 200 && status < 300,
             headers: { get: (k) => (loginHeader && k === 'X-GC-Login-Required' ? '1'
                 : (k.toLowerCase() === 'content-type' ? ct : null)) },
             text: () => sync(body),
             json: () => { throw new Error('checkSession must parse with readJson'); } };
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
    // parsed with readJson: a web page where /api/session was expected is no session
    // (never "Unexpected token '<'")
    const pageFetch = () => sync(resp(200, false, '<!DOCTYPE html><title>Just a moment...</title>',
        'text/html'));
    who = 'unset';
    S.checkSession(pageFetch, () => { n++; }).then((s) => { who = s; });
    t.eq(who, null);
    t.eq(n, 1);

    readJsonTests(t);
};

// readJson (v3.1.0): a web page where data was expected (a hub error page, or
// Cloudflare's block/challenge/5xx page) becomes {error} instead of
// "SyntaxError: Unexpected token '<'".
function readJsonTests(t) {
    const hdrs = (o) => ({ get: (k) => { const v = o[k.toLowerCase()]; return v === undefined ? null : v; } });
    const mk = (status, headers, text) => ({ status, ok: status >= 200 && status < 300,
        headers: hdrs(headers), text: () => sync(text) });
    const read = (r) => { let got = null; S.readJson(r).then((x) => { got = x; }); return got; };
    const JSON_CT = { 'content-type': 'application/json' };
    const page = (status, extra) => 'The hub answered HTTP ' + status + ' with a web page instead of data' +
        (extra ? ' (' + extra + ')' : '') + '.';

    // JSON passes straight through, whatever the status
    t.eq(read(mk(201, JSON_CT, '{"ok":true}')), { status: 201, body: { ok: true } });
    t.eq(read(mk(403, { 'content-type': 'application/json; charset=utf-8' }, '{"error":"Nope"}')),
        { status: 403, body: { error: 'Nope' } });
    t.eq(read(mk(200, { 'content-type': 'application/problem+json' }, '[1,2]')), { status: 200, body: [1, 2] });

    // the hub's own HTML page (werkzeug's error page, a template)
    t.eq(read(mk(500, { 'content-type': 'text/html; charset=utf-8', 'x-gc-hub': '1' },
        '<!doctype html>\n<html lang=en><title>500 Internal Server Error</title>')),
    { status: 500, body: { error: page(500) } });
    // through the tunnel every response has cf-ray: the hub's own page is still the hub's
    t.eq(read(mk(200, { 'content-type': 'text/html', 'cf-ray': '8c1d-DFW', server: 'cloudflare', 'x-gc-hub': '1' },
        '<!DOCTYPE html><html><head><title>Sign in</title>')).body, { error: page(200) });

    // Cloudflare's own pages: block, challenge, 5xx, plain "error code"
    t.eq(read(mk(403, { 'content-type': 'text/html; charset=UTF-8', 'cf-ray': '8c1d-DFW', server: 'cloudflare' },
        '<!DOCTYPE html>\n<html><head><title>Attention Required! | Cloudflare</title></head>')).body,
    { error: page(403, 'Cloudflare: Attention Required! | Cloudflare') });
    t.eq(read(mk(403, { 'content-type': 'text/html', server: 'cloudflare' },
        '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>')).body,
    { error: page(403, 'Cloudflare: Just a moment...') });
    t.eq(read(mk(502, { 'content-type': 'text/html', 'cf-ray': 'x' },
        '<!DOCTYPE html><title>\n  gc.asaplabs.net | 502: Bad gateway\n</title>')).body,
    { error: page(502, 'Cloudflare: gc.asaplabs.net | 502: Bad gateway') });
    t.eq(read(mk(403, { 'content-type': 'text/plain; charset=UTF-8' }, 'error code: 1020')).body,
        { error: page(403, 'Cloudflare: error code: 1020') });
    t.eq(read(mk(530, { 'content-type': 'text/html', 'cf-ray': 'x' },
        '<html><body>error code: 1033</body></html>')).body,
    { error: page(530, 'Cloudflare: error code: 1033') });
    // entities in the title are decoded; a Cloudflare page with neither title nor code
    t.eq(read(mk(403, { 'cf-ray': 'x' }, '<html><title>A &amp; B &#39;x&#39;</title>')).body,
        { error: page(403, "Cloudflare: A & B 'x'") });
    t.eq(read(mk(520, { 'cf-ray': 'x' }, '<html><body>oops</body></html>')).body,
        { error: page(520, 'Cloudflare') });

    // cf headers but neither HTML nor an error code: not called Cloudflare
    t.eq(read(mk(502, { 'cf-ray': 'x', 'content-type': 'text/plain' }, 'Bad Gateway')).body,
        { error: page(502) });
    // a JSON content-type that doesn't parse
    t.eq(read(mk(200, JSON_CT, '<!DOCTYPE html>')).body, { error: page(200) });
    // no headers object at all; an empty 204
    t.eq(read({ status: 500, text: () => sync('<html>') }).body, { error: page(500) });
    t.eq(read(mk(204, {}, '')), { status: 204, body: {} });

    // JSON under another content type (or none) is still data, as long as it
    // parses and is not a web page
    t.eq(read(mk(200, { 'content-type': 'text/plain' }, '{"a":1}')), { status: 200, body: { a: 1 } });
    t.eq(read(mk(200, {}, '{"a":1}')), { status: 200, body: { a: 1 } });
    t.eq(read(mk(400, { 'content-type': 'text/html', 'x-gc-hub': '1' }, ' {"error":"x"}\n')).body,
        { error: 'x' });
    t.eq(read(mk(200, { 'content-type': 'text/plain' }, '[1,2]')).body, [1, 2]);
    t.eq(read(mk(403, { 'content-type': 'text/plain', 'cf-ray': 'x' }, 'error code: 1020')).body,
        { error: page(403, 'Cloudflare: error code: 1020') });
    t.eq(read(mk(200, { 'content-type': 'text/html' }, '<html>{"a":1}</html>')).body, { error: page(200) });
    t.eq(read(mk(500, { 'content-type': 'text/plain' }, '')).body, { error: page(500) });

    // the pure part is exported too
    t.eq(S.parseBody(418, hdrs({}), 'teapot'), { error: page(418) });
}
