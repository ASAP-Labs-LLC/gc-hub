/* login.js: the sign-in page (templates/login.html). Card first (a keyboard
   wedge types the code and presses Enter), then LabLink username/password,
   then (LAN only) the admin-password break-glass. `next` is re-validated here
   (GCSession.safeNext) before the page goes there. */
(function () {
    'use strict';
    const $ = (id) => document.getElementById(id);
    const panel = $('login');
    const next = window.GCSession.safeNext(panel.getAttribute('data-next') || '/');
    const msg = $('msg');
    let busy = false;

    function say(text, cls) { msg.textContent = text || ''; msg.className = cls || ''; }

    function setBusy(on) {
        busy = on;
        document.querySelectorAll('button').forEach((b) => { b.disabled = on; });
    }

    async function signIn(path, body) {
        if (busy) return;
        setBusy(true);
        say('Signing in…');
        try {
            const r = await fetch(path, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(Object.assign({ next }, body)),
            });
            const j = (await window.GCSession.readJson(r)).body || {};
            if (r.ok && !j.error) {
                say('Signed in as ' + (j.name || '') + '.', 'ok');
                window.location.assign(window.GCSession.safeNext(j.next || next));
                return;
            }
            say(j.error || ('HTTP ' + r.status), 'err');
            if (j.labcore_unavailable && $('admin-details')) $('admin-details').open = true;
        } catch (e) {
            say('The hub could not be reached: ' + e, 'err');
        } finally {
            setBusy(false);
        }
    }

    $('card-form').addEventListener('submit', (ev) => {
        ev.preventDefault();
        const input = $('card');
        const code = input.value.trim();
        input.value = '';                    // never leave a credential in the field
        if (!code) return;                   // a stray Enter is not an attempt
        signIn('/api/login/card', { code });
    });

    $('password-form').addEventListener('submit', (ev) => {
        ev.preventDefault();
        const pw = $('password');
        const body = { username: $('username').value.trim(), password: pw.value };
        pw.value = '';
        signIn('/api/login', body);
    });

    const adminForm = $('admin-form');
    if (adminForm) {
        adminForm.addEventListener('submit', (ev) => {
            ev.preventDefault();
            const pw = $('admin-password');
            const body = { password: pw.value };
            pw.value = '';
            signIn('/api/login/admin', body);
        });
    }

    $('card').focus();
})();
