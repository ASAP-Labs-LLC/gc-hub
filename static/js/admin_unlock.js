/* admin_unlock.js: unlock admin once (v4.0 lane E; spec "Global status, a
   way home, and Hub admin": "Unlock once with the admin password: a 15-minute
   closure").

   The password is kept only in a closure (never a global, never storage) for
   TTL_MS from when it was entered, then forgotten; using it does not extend
   it. A page forgets it at once when the hub refuses it (HTTP 403), or on
   Lock. window.GCAdminUnlock:
     createUnlock({now?})    a closure: get() -> password | null, set(pw),
                             forget(), isUnlocked(), remainingMs(),
                             onChange(fn(unlocked)) -> off
     page                    this page's closure
     ask(what) -> Promise    the password: the page's, or asked for in a masked
                             dialog ("Admin password to <what>"); null when
                             cancelled. Replaces window.prompt, which shows
                             the password in plain text.
     refused(error)          forget the password when a call answered 403.
   Pure parts are node-tested (tests/js/admin_unlock.test.js). */
(function (root) {
    'use strict';

    const TTL_MS = 15 * 60 * 1000;

    function createUnlock(opts) {
        const now = (opts && opts.now) || (() => Date.now());
        let secret = null;
        let until = 0;
        let subs = [];

        function notify(v) {
            for (const fn of subs.slice()) { try { fn(v); } catch (_) { /* its problem */ } }
        }

        function expired() {
            if (secret !== null && now() >= until) { secret = null; until = 0; notify(false); }
        }

        const api = {
            get() { expired(); return secret; },
            set(pw) {
                if (typeof pw !== 'string' || pw === '') return;
                secret = pw;
                until = now() + TTL_MS;
                notify(true);
            },
            forget() {
                const was = secret !== null;
                secret = null;
                until = 0;
                if (was) notify(false);
            },
            isUnlocked() { expired(); return secret !== null; },
            remainingMs() { expired(); return secret === null ? 0 : Math.max(0, until - now()); },
            onChange(fn) {
                subs.push(fn);
                return () => { subs = subs.filter(x => x !== fn); };
            },
            toJSON() { return { unlocked: api.isUnlocked() }; },
            toString() { return '[admin unlock]'; },
        };
        return api;
    }

    /** "15 min", "1 min", "under a minute". */
    function remainingText(ms) {
        if (ms < 60 * 1000) return 'under a minute';
        return Math.ceil(ms / 60000) + ' min';
    }

    /** v4.0 lane E2: the shell's admin gate (GCShell.adminPost, the unlock
        chip) as a view of an unlock closure, so a page has ONE unlock: the
        shell dialog, the Hub admin bar and Calibration's Save share it. */
    function gateFor(unlock) {
        return {
            get: () => unlock.get(),
            set: (pw) => unlock.set(String(pw)),
            clear: () => unlock.forget(),
            remainingMs: () => unlock.remainingMs(),
            onChange: (fn) => unlock.onChange(fn),
            toJSON: () => ({}),
        };
    }

    const pure = { TTL_MS, createUnlock, remainingText, gateFor };
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = pure;
        return;
    }

    // ── the page ──────────────────────────────────────────────────────────
    const page = createUnlock();
    let pending = null;              // typed in the dialog, not yet accepted by the hub

    function el(tag, text, css) {
        const e = document.createElement(tag);
        if (text) e.textContent = text;
        if (css) e.style.cssText = css;
        return e;
    }

    /** The masked password dialog; resolves the password or null. On a
        shell page it is the shell's own dialog (#admin-dialog, the tokens). */
    function dialog(what) {
        const shell = root.GCShell;
        if (shell && typeof shell.askPassword === 'function' && document.getElementById('admin-dialog')) {
            return shell.askPassword((what ? 'To ' + what + ', enter the admin password. ' : '') +
                'It stays unlocked in this tab for 15 minutes, never saved.').then((pw) => {
                // not kept yet: only once the hub accepts it (accepted())
                if (pw) pending = pw;
                return pw || null;
            });
        }
        return new Promise((resolve) => {
            const overlay = el('div', null, 'position:fixed;inset:0;z-index:20000;display:flex;' +
                'align-items:center;justify-content:center;background:rgba(13,17,23,.75)');
            overlay.setAttribute('data-testid', 'admin-unlock-dialog');
            const box = el('form', null, 'background:#161b22;border:1px solid #30363d;border-radius:10px;' +
                'padding:18px;width:min(380px,92vw);color:#e6edf3;font:14px/1.5 -apple-system,' +
                'BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;box-shadow:0 16px 64px #000a');
            box.setAttribute('role', 'dialog');
            box.setAttribute('aria-modal', 'true');
            const title = el('div', 'Admin password' + (what ? ' to ' + what : ''),
                'font-weight:600;margin-bottom:10px');
            title.id = 'admin-unlock-title';
            box.setAttribute('aria-labelledby', title.id);
            const input = el('input', null, 'width:100%;box-sizing:border-box;padding:7px 9px;' +
                'background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;font:inherit');
            input.type = 'password';
            input.id = 'admin-unlock-password';
            input.autocomplete = 'current-password';
            const note = el('div', 'Stays unlocked in this tab for 15 minutes.',
                'color:#7d8590;font-size:12px;margin:8px 0 12px');
            const row = el('div', null, 'display:flex;gap:8px;justify-content:flex-end');
            const cancel = el('button', 'Cancel', 'background:#21262d;color:#c9d1d9;border:1px solid #30363d;' +
                'border-radius:6px;padding:6px 14px;cursor:pointer;font:inherit');
            cancel.type = 'button';
            const ok = el('button', 'Unlock', 'background:#1f6feb;color:#fff;border:1px solid #1f6feb;' +
                'border-radius:6px;padding:6px 14px;cursor:pointer;font:inherit');
            ok.type = 'submit';
            ok.id = 'admin-unlock-submit';
            row.append(cancel, ok);
            box.append(title, input, note, row);
            overlay.appendChild(box);
            const done = (value) => {
                overlay.remove();
                document.removeEventListener('keydown', onKey, true);
                resolve(value);
            };
            const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); done(null); } };
            document.addEventListener('keydown', onKey, true);
            cancel.addEventListener('click', () => done(null));
            box.addEventListener('submit', (e) => {
                e.preventDefault();
                const pw = input.value;
                input.value = '';
                if (!pw) { done(null); return; }
                // not kept yet: only once the hub accepts it (accepted(); v4.0 lane E review)
                pending = pw;
                done(pw);
            });
            document.body.appendChild(overlay);
            input.focus();
        });
    }

    function ask(what) {
        const pw = page.get();
        return pw ? Promise.resolve(pw) : dialog(what);
    }

    /** Forget the password when the hub refused it (an Error or response with status 403). */
    function refused(err) {
        if (err && (err.status === 403)) page.forget();
    }

    /** A call carrying ``pw`` succeeded: if it is the one just typed in the
        dialog, keep it for 15 minutes (never any other password, e.g. a
        QBench login sent the same way). */
    function accepted(pw) {
        if (pending !== null && pw === pending) page.set(pw);
        pending = null;
    }

    root.GCAdminUnlock = Object.assign({}, pure, { page, ask, refused, accepted });
})(typeof window !== 'undefined' ? window : globalThis);
