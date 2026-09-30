/* admin_restart.js (v5.0 lane R): Restart in Admin · Server. It moved here
   from the classic Settings form. The words come from restart.js
   (restartLabel, restartConfirmText, restartBusyPrompt, serverReplaced;
   node-tested). The confirmation is inline, next to the button (no
   confirm()): what a restart would do right now (POST /api/restart
   {dry_run: true}), the work it would cut short, then Restart now / Restart
   anyway. Afterwards it polls /healthz until another process answers and
   reloads. Text only; answers through GCSession.readJson. */
(function () {
    'use strict';
    const $ = (id) => document.getElementById(id);
    let decision = null;

    function msg(text, kind) {
        const m = $('restart-msg');
        m.textContent = text || '';
        m.className = 'msg' + (kind ? ' ' + kind : '');
    }
    async function post(body) {
        const r = await fetch('/api/restart', { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                                                body: JSON.stringify(body) });
        return window.GCSession.readJson(r);
    }
    async function healthz() {
        const ctl = new AbortController();
        const t = setTimeout(() => ctl.abort(), 3000);
        try {
            const r = await fetch('/healthz', { cache: 'no-store', signal: ctl.signal });
            if (!r.ok) return null;
            const b = (await window.GCSession.readJson(r)).body;
            return b && b.status ? b : null;
        } catch (_e) { return null; } finally { clearTimeout(t); }
    }

    async function ask() {
        msg('Checking…');
        const res = await post({ dry_run: true }).catch(() => null);
        if (!res || res.status !== 200) {
            msg((res && res.body && res.body.error) || 'Could not ask the hub what a restart would do.', 'err');
            return;
        }
        decision = res.body;
        msg('');
        const busy = window.restartBusyPrompt(decision);
        const list = $('restart-busy');
        list.replaceChildren(...(busy ? (decision.blocked && decision.blocked.length ? decision.blocked : decision.busy) : [])
            .map((b) => { const li = document.createElement('li'); li.textContent = b; return li; }));
        const go = $('btn-restart-go');
        if (busy && !busy.canForce) {
            $('restart-text').textContent = 'Restart is not possible now. This cannot be overridden; try again when it has finished:';
            go.hidden = true;
        } else if (busy) {
            $('restart-text').textContent = 'The hub is busy. Restarting now cuts this short:';
            go.hidden = false;
            go.textContent = 'Restart anyway';
        } else {
            $('restart-text').textContent = window.restartConfirmText(decision);
            go.hidden = false;
            go.textContent = window.restartLabel(decision) === 'Restart' ? 'Restart now' : window.restartLabel(decision);
        }
        $('restart-confirm').hidden = false;
        $('btn-restart-cancel').focus();
    }

    async function restart() {
        const busy = window.restartBusyPrompt(decision);
        const before = await healthz();
        $('btn-restart-go').disabled = true;
        const res = await post(busy && busy.canForce ? { force: true } : {}).catch(() => null);
        if (!res || res.status !== 200) {
            $('btn-restart-go').disabled = false;
            msg((res && res.body && res.body.error) || 'The restart was refused.', 'err');
            return;
        }
        $('restart-confirm').hidden = true;
        $('btn-restart').disabled = true;
        const tag = res.body.mode === 'switch' ? res.body.tag : null;
        msg(tag ? 'Restarting and installing ' + tag + '… this page reconnects by itself.' : 'Restarting… this page reconnects by itself.');
        const oldPid = res.body.pid;
        const oldVersion = before ? before.version : null;
        let tries = 0;
        const timer = setInterval(async () => {
            tries++;
            const b = await healthz();
            if (window.serverReplaced(oldPid, b, oldVersion)) {
                clearInterval(timer);
                const notice = window.switchOutcomeNotice(tag, b.version);
                msg(notice || 'Back up. Reloading…', notice ? 'err' : 'ok');
                setTimeout(() => location.reload(), notice ? 4000 : 600);
            } else if (tries > 150) {
                clearInterval(timer);
                $('btn-restart').disabled = false;
                msg('The hub did not come back within 5 minutes. Check the tray on ASAPSV1.', 'err');
            }
        }, 2000);
    }

    document.addEventListener('DOMContentLoaded', () => {
        if (!$('btn-restart')) return;
        $('btn-restart').addEventListener('click', ask);
        $('btn-restart-go').addEventListener('click', restart);
        $('btn-restart-cancel').addEventListener('click', () => { $('restart-confirm').hidden = true; $('btn-restart').focus(); });
    });
})();
