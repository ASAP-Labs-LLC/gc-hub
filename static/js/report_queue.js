/* v5.0.0 lane C: the report queue (window.GCReportQueue).

   A list of reports to build, kept in this tab's sessionStorage, with one
   sheet to act on it: Download all (one PDF, or a ZIP built by the hub's
   background job and fetched once from its one-time link) and Upload to
   QBench (the hub's existing upload: sign-in, progress over the SSE stream,
   skip, stop, and a new password when QBench refuses the sign-in mid-run).

   A queue item captures what its report is built with when it is added
   (standard, trend parameters, thresholds, x-max, ranges, an edited
   conclusion); the payload is report_payload.js's buildReportItemPayload,
   unchanged, so the hub computes the bullets itself. The top bar's
   "Report queue N" button (templates/_layout.html) opens the sheet.

   The pure part (createStore ... overallView) is shared with the Node tests;
   the rest runs in the browser. Text only (textContent); every answer is
   parsed with GCSession.readJson. */
(function (root) {
    'use strict';

    const payloadLib = (typeof module !== 'undefined' && module.exports)
        ? require('./report_payload.js') : root;

    const STORAGE_KEY = 'gc.reportQueue';
    const UPLOAD_KEY = 'gc.reportQueue.upload';
    const MAX_ITEMS = 500;

    // ── pure: items and the store ───────────────────────────────────────────
    /** One plain line for a failure: the first non-empty line, spaces
        collapsed, at most 240 characters (never a stack trace). */
    const FAILURE_MAX = 240;
    function failureLine(msg) {
        if (msg === null || msg === undefined) return '';
        const first = String(msg).split(/\r?\n/).map(l => l.replace(/\s+/g, ' ').trim()).find(Boolean) || '';
        return first.length > FAILURE_MAX ? first.slice(0, FAILURE_MAX - 1) + '…' : first;
    }

    function toId(v) {
        const n = typeof v === 'string' && v.trim() !== '' ? Number(v) : v;
        return Number.isInteger(n) ? n : null;
    }

    /** A queue item from what the page captured, or null when it can't be a
        report (no sample, no standard). Bullets are never kept. */
    function normalizeItem(raw, now) {
        if (!raw || typeof raw !== 'object') return null;
        const sid = toId(raw.sample_id);
        const std = typeof raw.standard_name === 'string' ? raw.standard_name.trim() : '';
        if (sid === null || !std) return null;
        const item = {
            id: 's' + sid,
            sample_id: sid,
            lab_id: raw.lab_id != null ? String(raw.lab_id) : '',
            sample_name: raw.sample_name ? String(raw.sample_name) : 'GC Analysis',
            standard_name: std,
            conclusion: String(raw.conclusion || '').trim(),
            params: payloadLib.captureReportParams(raw.params || {}),
            overlay_standards: Array.isArray(raw.overlay_standards)
                ? raw.overlay_standards.map(String) : [],
            added_at: raw.added_at || now || new Date().toISOString(),
            sent_at: raw.sent_at || null,
        };
        if (Array.isArray(raw.ranges)) item.ranges = payloadLib.rangesForPayload(raw.ranges);
        // v6.0.0: a report whose upload failed keeps the reason (one line) until
        // it is retried, re-added or removed; only then does the key exist
        const err = raw.upload_error;
        if (!item.sent_at && err && typeof err === 'object' && typeof err.msg === 'string') {
            item.upload_error = { msg: failureLine(err.msg) || 'Failed', at: err.at || null };
        }
        // key order: id, sample_id, lab_id, sample_name, standard_name,
        // conclusion, params, ranges, overlay_standards, added_at, sent_at, upload_error
        const ordered = {};
        for (const k of ['id', 'sample_id', 'lab_id', 'sample_name', 'standard_name', 'conclusion',
                         'params', 'ranges', 'overlay_standards', 'added_at', 'sent_at', 'upload_error']) {
            if (k in item) ordered[k] = item[k];
        }
        return ordered;
    }

    /** The upload/ZIP payloads: report_payload.js's format, unchanged. */
    function payloads(items) {
        return (items || []).map(it => payloadLib.buildReportItemPayload(it, undefined, undefined));
    }

    function createStore(storage, key) {
        const k = key || STORAGE_KEY;
        const listeners = [];
        let mem = null;                       // used when storage refuses
        function read() {
            if (mem) return mem.slice();
            let raw = null;
            try { raw = storage ? storage.getItem(k) : null; } catch (_e) { raw = null; }
            let list;
            try { list = raw ? JSON.parse(raw) : []; } catch (_e) { list = []; }
            if (!Array.isArray(list)) list = [];
            return list.map(x => normalizeItem(x)).filter(Boolean);
        }
        function write(list) {
            const clipped = list.slice(-MAX_ITEMS);
            try {
                if (!storage) throw new Error('no storage');
                storage.setItem(k, JSON.stringify(clipped));
                mem = null;
            } catch (_e) {
                mem = clipped.slice();
            }
            for (const fn of listeners.slice()) {
                try { fn(clipped.slice()); } catch (e) { if (root.console) root.console.error(e); }
            }
        }
        function add(raw, now) {
            const item = normalizeItem(Object.assign({}, raw, { sent_at: null, upload_error: null }), now);
            if (!item) return null;
            const list = read();
            const i = list.findIndex(x => x.id === item.id);
            if (i >= 0) list[i] = item; else list.push(item);
            write(list);
            return { item, replaced: i >= 0 };
        }
        function addMany(raws, now) {
            const list = read();
            let n = 0;
            for (const raw of raws || []) {
                const item = normalizeItem(Object.assign({}, raw, { sent_at: null, upload_error: null }), now);
                if (!item) continue;
                const i = list.findIndex(x => x.id === item.id);
                if (i >= 0) list[i] = item; else list.push(item);
                n++;
            }
            if (n) write(list);
            return n;
        }
        function remove(id) {
            const list = read();
            const next = list.filter(x => x.id !== id);
            if (next.length === list.length) return false;
            write(next);
            return true;
        }
        function without(x, extra) {
            const out = Object.assign({}, x, extra);
            delete out.upload_error;
            return out;
        }
        function markSent(ids, at) {
            const set = new Set(ids || []);
            const when = at || new Date().toISOString();
            write(read().map(x => (set.has(x.id) ? without(x, { sent_at: when }) : x)));
        }
        /** Back to unsent with the reason it failed ({id: reason}): the sheet
            offers Retry, and Upload sends it again. Never marked sent. */
        function markFailed(reasons, at) {
            const map = reasons || {};
            const when = at || new Date().toISOString();
            write(read().map(x => (Object.prototype.hasOwnProperty.call(map, x.id)
                ? Object.assign({}, x, { sent_at: null,
                    upload_error: { msg: failureLine(map[x.id]) || 'Failed', at: when } })
                : x)));
        }
        /** Back to unsent with no error (skipped by the operator). */
        function markUnsent(ids) {
            const set = new Set(ids || []);
            write(read().map(x => (set.has(x.id) ? without(x, { sent_at: null }) : x)));
        }
        /** Apply reconcile()'s answer to the reports this tab handed to the hub
            (sent_at set); a report re-added since then is a new one and kept. */
        function applyOutcome(outcome, at) {
            const o = outcome || {};
            const inFlight = new Set(read().filter(x => x.sent_at).map(x => x.id));
            const key = sid => 's' + sid;
            const failed = {};
            for (const f of o.failed || []) if (inFlight.has(key(f.sample_id))) failed[key(f.sample_id)] = f.msg;
            const back = (o.unsent || []).map(key).filter(id => inFlight.has(id));
            if (!Object.keys(failed).length && !back.length) return 0;
            const when = at || new Date().toISOString();
            const backSet = new Set(back);
            write(read().map(x => {
                if (Object.prototype.hasOwnProperty.call(failed, x.id)) {
                    return Object.assign({}, x, { sent_at: null,
                        upload_error: { msg: failureLine(failed[x.id]) || 'Failed', at: when } });
                }
                return backSet.has(x.id) ? without(x, { sent_at: null }) : x;
            }));
            return Object.keys(failed).length + back.length;
        }
        return {
            items: read,
            count: () => read().length,
            unsent: () => read().filter(x => !x.sent_at),
            failed: () => read().filter(x => !x.sent_at && x.upload_error),
            add, addMany, remove, markSent, markFailed, markUnsent, applyOutcome,
            clear: () => write([]),
            onChange: (fn) => { listeners.push(fn); },
        };
    }

    // ── pure: wording ───────────────────────────────────────────────────────
    function buttonText(n) { return n > 0 ? `Report queue ${n}` : 'Report queue'; }

    function nameOf(item) {
        return item && item.lab_id ? String(item.lab_id) : `sample ${item ? item.sample_id : '?'}`;
    }

    function addedText(item, replaced) {
        return (replaced ? `Updated ${nameOf(item)} in` : `Added ${nameOf(item)} to`) +
            ` the report queue, compared with ${item.standard_name}`;
    }

    const STATUS_TEXT = {
        waiting: 'Waiting', generating: 'Building the PDF', report_ok: 'PDF ready',
        uploading: 'Uploading', ok: 'Uploaded', failed: 'Failed', error: 'Failed',
        skipped: 'Skipped', login_failed: 'QBench sign-in failed',
    };
    function statusText(status) { return STATUS_TEXT[status] || 'Waiting'; }
    function isFinished(status) { return ['ok', 'failed', 'error', 'skipped'].includes(status); }

    function progress(states) {
        const list = states || [];
        const done = list.filter(s => s && isFinished(s.status)).length;
        return { done, total: list.length, pct: list.length ? Math.round(done / list.length * 100) : 0 };
    }

    /** The overall line for an SSE `overall` event. `finished`: the upload
        thread has stopped (done, partial, all failed, cancelled). */
    function overallView(d) {
        const data = d || {};
        switch (data.status) {
            case 'done': return { text: `All ${data.total} uploaded to QBench.`, tone: 'ok', finished: true };
            case 'partial': return { text: `${data.ok} uploaded, ${data.fail} failed.`, tone: 'warn', finished: true };
            case 'allfailed': return { text: `The upload failed (${data.total}).`, tone: 'err', finished: true };
            case 'cancelled': return { text: 'Upload stopped.', tone: 'warn', finished: true };
            case 'credentials_needed':
                return { text: 'QBench refused the sign-in. Enter the password again to carry on.',
                         tone: 'warn', finished: false };
            case 'precheck_failed':
            case 'error':
                return { text: String(data.msg || 'The first report failed.'), tone: 'err', finished: false };
            default: return { text: String(data.msg || 'Uploading…'), tone: 'info', finished: false };
        }
    }

    /** The run's rows → each queued report's state now: ``sent`` (uploaded),
        ``failed`` ([{sample_id, msg}]: offer Retry) and ``unsent`` (skipped).
        The newest row for a sample wins (a retry appended to a running upload
        adds a row). While the run is going only a final answer counts; once it
        has ended (``finished``), anything not uploaded failed. */
    const ENDED_EARLY = 'Not uploaded: the upload ended before this report was sent.';
    /** sample_id → the run's newest row for it. */
    function latestRows(rows) {
        const latest = new Map();
        for (const r of Array.isArray(rows) ? rows : []) {
            if (!r || r.sample_id === null || r.sample_id === undefined) continue;
            const sid = Number(r.sample_id);
            if (!Number.isInteger(sid)) continue;
            const prev = latest.get(sid);
            if (!prev || Number(r.idx) >= Number(prev.idx)) latest.set(sid, r);
        }
        return latest;
    }

    function reconcile(rows, finished) {
        const latest = latestRows(rows);
        const out = { sent: [], failed: [], unsent: [] };
        const ordered = [...latest.entries()].sort((a, b) => Number(a[1].idx) - Number(b[1].idx));
        for (const [sid, r] of ordered) {
            const st = r.status;
            if (st === 'ok') out.sent.push(sid);
            else if (st === 'skipped') out.unsent.push(sid);
            else if (st === 'failed' || st === 'error') {
                out.failed.push({ sample_id: sid, msg: failureLine(r.msg) || statusText(st) });
            } else if (finished) {
                const msg = st === 'login_failed' ? failureLine(r.msg) : '';
                out.failed.push({ sample_id: sid, msg: msg || ENDED_EARLY });
            }
        }
        return out;
    }

    function retryText(n) { return `Retry failed (${n})`; }

    /** What the sheet says when the hub refuses to start an upload; a
        credentials problem (``need_password``) is said exactly as the hub
        names it, and the sheet offers the password box. */
    function startRefusal(body, status) {
        const b = body || {};
        if (b.need_password) return { needPassword: true, text: failureLine(b.error) || 'Enter the QBench password.' };
        const refused = Array.isArray(b.refused) ? b.refused : [];
        const text = refused.length
            ? 'Not uploaded: ' + refused.map(r => `${r.lab_id || r.sample_id} (${r.error})`).join('; ')
            : 'Not uploaded: ' + (failureLine(b.error) || ('HTTP ' + status));
        return { needPassword: false, text };
    }

    const pure = { STORAGE_KEY, UPLOAD_KEY, normalizeItem, payloads, createStore, buttonText,
        addedText, statusText, isFinished, progress, overallView, failureLine, latestRows, reconcile,
        retryText, startRefusal };
    if (typeof module !== 'undefined' && module.exports) module.exports = pure;
    if (typeof document === 'undefined') return;

    // ── browser ─────────────────────────────────────────────────────────────
    let session = null;
    try { session = root.sessionStorage; } catch (_e) { session = null; }
    const store = createStore(session);

    const $ = (id) => document.getElementById(id);
    function h(tag, props, ...children) {
        const el = document.createElement(tag);
        for (const [k, v] of Object.entries(props || {})) {
            if (v === null || v === undefined || v === false) continue;
            if (k === 'className') el.className = v;
            else if (k === 'text') el.textContent = String(v);
            else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
            else el.setAttribute(k, v === true ? '' : String(v));
        }
        for (const c of children.flat()) {
            if (c === null || c === undefined || c === false) continue;
            el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
        }
        return el;
    }
    function toast(msg, kind) {
        if (root.GCShell && root.GCShell.toast) root.GCShell.toast(msg, kind);
    }
    function flag(key, value) {
        try {
            if (!session) return null;
            if (value === undefined) return session.getItem(key);
            if (value === null) session.removeItem(key); else session.setItem(key, value);
        } catch (_e) { /* private mode */ }
        return null;
    }

    // The upload's start only queues the work on the hub (the PDFs are built on its
    // thread), so a minute without an answer means the hub is not answering.
    const timeouts = { start: 60000, watch: 60000, watchEvery: 5000, quietStream: 20000 };

    async function postJson(url, body, timeoutMs) {
        const ctl = timeoutMs && typeof AbortController === 'function' ? new AbortController() : null;
        let timer = null;
        const req = fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json',
            Accept: 'application/json' }, body: JSON.stringify(body || {}), signal: ctl ? ctl.signal : undefined });
        const gaveUp = !timeoutMs ? null : new Promise((_, reject) => {
            timer = setTimeout(() => {
                if (ctl) { try { ctl.abort(); } catch (_e) { /* done */ } }
                const err = new Error('no answer from the hub in ' + Math.round(timeoutMs / 1000) + ' s');
                err.timedOut = true;
                reject(err);
            }, timeoutMs);
        });
        try {
            const r = await (gaveUp ? Promise.race([req, gaveUp]) : req);
            const res = await root.GCSession.readJson(r);
            return { status: r.status, ok: r.ok, body: res.body || {} };
        } finally {
            if (timer) clearTimeout(timer);
        }
    }
    async function getJson(url, background) {
        const opts = { headers: { Accept: 'application/json' }, cache: 'no-store' };
        let r;
        if (background && root.GCLive && typeof root.GCLive.bgFetch === 'function') {
            r = await root.GCLive.bgFetch(url, opts);
        } else {
            if (background) opts.headers['X-GC-Background'] = '1';
            r = await fetch(url, opts);
        }
        const res = await root.GCSession.readJson(r);
        return { status: r.status, ok: r.ok, body: res.body || {} };
    }
    function saveBlob(blob, name) {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    }
    function fileName(resp, fallback) {
        const cd = resp.headers.get('content-disposition') || '';
        const m = cd.match(/filename="?([^";\n]+)"?/);
        return m ? m[1] : fallback;
    }

    // ── the upload's state (survives closing the sheet and, through the
    //    hub's status, a page change in this tab) ────────────────────────────
    const up = {
        active: false,          // the hub's upload thread is running
        states: [],             // [{idx, lab_id, status, msg, step, steps}]
        overall: null,          // the last overallView
        needCreds: false,
        sse: null,
        ids: [],                // queue ids sent, by upload index
    };

    // ── the top-bar button ──────────────────────────────────────────────────
    function syncButton() {
        const btn = $('report-queue-btn');
        if (!btn) return;
        const n = store.count();
        const label = btn.querySelector('.rq-label');
        const count = btn.querySelector('.rq-count');
        if (label) label.textContent = 'Report queue';
        if (count) { count.textContent = n ? String(n) : ''; count.hidden = !n; }
        const prog = btn.querySelector('.rq-progress');
        if (prog) {
            const p = progress(up.states);
            prog.textContent = up.active ? `Uploading ${p.done}/${p.total}` : '';
            prog.hidden = !up.active;
        }
        btn.setAttribute('aria-label', buttonText(n) + (up.active ? ', uploading to QBench' : ''));
        btn.hidden = !n && !up.active;
    }

    // ── the sheet ───────────────────────────────────────────────────────────
    let sheet = null;
    const el = {};

    function build() {
        if (sheet) return sheet;
        el.count = h('span', { className: 'caption', id: 'rq-count-line' });
        el.list = h('ul', { className: 'rq-list', 'data-testid': 'rq-list' });
        el.empty = h('p', { className: 'empty-note', 'data-testid': 'rq-empty',
            text: 'Nothing queued. Add a sample from its Compare view with Add to queue.' });
        el.msg = h('p', { className: 'rq-msg', role: 'status', 'aria-live': 'polite', 'data-testid': 'rq-msg' });

        el.user = h('input', { type: 'text', id: 'rq-qb-user', autocomplete: 'username', spellcheck: 'false' });
        el.pass = h('input', { type: 'password', id: 'rq-qb-pass', autocomplete: 'current-password' });
        // v6.0.0: when the saved sign-in can't be used, this line says why
        // (the hub's own words) and asks for the password instead
        el.signinNote = h('p', { className: 'caption', 'data-testid': 'rq-signin-note',
                                 text: 'Leave the password empty to use the saved QBench sign-in.' });
        el.signin = h('form', { className: 'rq-signin', 'data-testid': 'rq-signin', hidden: true,
                                onsubmit: (e) => { e.preventDefault(); startUpload(); } },
            h('h3', { text: 'QBench sign-in' }),
            el.signinNote,
            h('div', { className: 'rq-fields' },
                h('label', { className: 'field' }, h('span', { text: 'Username' }), el.user),
                h('label', { className: 'field' }, h('span', { text: 'Password' }), el.pass)));
        el.reauthPass = h('input', { type: 'password', id: 'rq-reauth-pass', autocomplete: 'current-password' });
        el.reauth = h('form', { className: 'rq-reauth', role: 'alert', 'data-testid': 'rq-reauth', hidden: true,
                                onsubmit: (e) => { e.preventDefault(); submitCreds(); } },
            h('p', { text: 'QBench refused the sign-in. Enter the password again and the upload carries on.' }),
            h('div', { className: 'rq-fields' },
                h('label', { className: 'field' }, h('span', { text: 'Password' }), el.reauthPass),
                h('button', { type: 'submit', className: 'btn btn-primary btn-sm', 'data-testid': 'rq-reauth-submit',
                              text: 'Sign in again' })));
        el.bar = h('span', { style: 'width:0%' });
        el.progress = h('div', { className: 'rq-progress-block', 'data-testid': 'rq-progress', hidden: true },
            h('div', { className: 'progress', role: 'progressbar', 'aria-label': 'Upload progress' }, el.bar),
            h('p', { className: 'rq-overall', id: 'rq-overall', role: 'status', 'aria-live': 'polite' }),
            el.upList = h('ul', { className: 'rq-uplist', 'data-testid': 'rq-uplist' }));

        el.clear = h('button', { type: 'button', className: 'btn btn-ghost btn-sm', 'data-testid': 'rq-clear',
                                 text: 'Clear', onclick: onClear });
        el.close = h('button', { type: 'button', className: 'btn btn-sm', text: 'Close',
                                 onclick: () => sheet.close() });
        el.download = h('button', { type: 'button', className: 'btn btn-sm', 'data-testid': 'rq-download',
                                    text: 'Download all', onclick: downloadAll });
        el.upload = h('button', { type: 'button', className: 'btn btn-primary btn-sm', 'data-testid': 'rq-upload',
                                  text: 'Upload to QBench', onclick: onUpload });
        el.stop = h('button', { type: 'button', className: 'btn btn-sm btn-danger', 'data-testid': 'rq-stop',
                                text: 'Stop', onclick: stopUpload, hidden: true });
        // v6.0.0: send the failed reports again, the queue kept as it is
        el.retryFailed = h('button', { type: 'button', className: 'btn btn-sm', 'data-testid': 'rq-retry-failed',
                                       hidden: true, onclick: () => retry(store.failed().map(x => x.id)) });

        sheet = h('dialog', { className: 'sheet rq-sheet', id: 'report-queue-sheet',
                              'data-testid': 'report-queue-sheet', 'aria-labelledby': 'rq-title' },
            h('div', { className: 'rq-head' },
                h('h2', { id: 'rq-title', text: 'Report queue' }), el.count,
                h('span', { className: 'spacer' }),
                h('button', { type: 'button', className: 'icon-btn', 'aria-label': 'Close the report queue',
                              onclick: () => sheet.close() }, h('span', { className: 'ico rq-x', 'aria-hidden': 'true' }))),
            h('p', { className: 'rq-intro', text: 'Each report is built with the standard and settings it was added with. The queue is kept in this browser tab.' }),
            el.list, el.empty, el.signin, el.reauth, el.progress, el.msg,
            h('div', { className: 'actions' }, el.clear, h('span', { className: 'spacer' }), el.close,
              el.download, el.stop, el.retryFailed, el.upload));
        document.body.appendChild(sheet);
        sheet.addEventListener('close', () => { el.msg.textContent = ''; });
        return sheet;
    }

    function render() {
        if (!sheet) return;
        const items = store.items();
        const unsent = items.filter(x => !x.sent_at);
        el.count.textContent = items.length ? `${items.length} report${items.length === 1 ? '' : 's'}` : '';
        el.list.textContent = '';
        const rowFor = latestRows(up.states);
        const failedN = items.filter(x => !x.sent_at && x.upload_error).length;
        const retryOff = busy.upload || !!up.watching;
        for (const it of items) {
            const meta = ['Compared with ' + it.standard_name];
            if (it.conclusion) meta.push('edited conclusion');
            const row = rowFor.get(Number(it.sample_id));
            let state = null;
            if (it.upload_error) {
                state = h('span', { className: 'pill error', text: 'Not uploaded' });
            } else if (it.sent_at && up.active && row && row.status !== 'ok') {
                state = h('span', { className: 'pill held', text: statusText(row.status) });
            } else if (it.sent_at) {
                state = h('span', { className: 'pill final', text: 'Sent to QBench' });
            }
            el.list.appendChild(h('li', { 'data-testid': 'rq-item', 'data-id': it.id,
                                          'data-state': it.upload_error ? 'failed' : (it.sent_at ? 'sent' : 'queued') },
                h('div', { className: 'rq-main' },
                    h('b', { text: nameOf(it) }),
                    h('span', { className: 'caption', text: meta.join(' · ') }),
                    it.upload_error ? h('span', { className: 'rq-reason', 'data-testid': 'rq-reason',
                                                  text: it.upload_error.msg }) : null),
                state,
                it.upload_error ? h('button', { type: 'button', className: 'btn btn-sm', text: 'Retry',
                    'aria-label': 'Retry ' + nameOf(it), 'data-testid': 'rq-retry', disabled: retryOff,
                    onclick: () => retry([it.id]) }) : null,
                h('button', { type: 'button', className: 'btn btn-ghost btn-sm', text: 'Remove',
                              'aria-label': 'Remove ' + nameOf(it), 'data-testid': 'rq-remove',
                              onclick: () => { store.remove(it.id); } })));
        }
        el.retryFailed.hidden = !failedN;
        el.retryFailed.textContent = retryText(failedN);
        el.retryFailed.disabled = retryOff;
        el.empty.hidden = items.length > 0;
        el.download.disabled = !items.length || busy.download;
        el.clear.disabled = !items.length;
        const signing = !el.signin.hidden;
        el.upload.hidden = up.active && !unsent.length;          // Stop and the progress say it
        if (up.active) {
            el.upload.textContent = `Add ${unsent.length} to the upload`;
        } else if (signing) {
            el.upload.textContent = `Start upload (${pendingItems().length})`;
        } else {
            el.upload.textContent = unsent.length && unsent.length !== items.length
                ? `Upload ${unsent.length} to QBench` : 'Upload to QBench';
        }
        // off while a start is out: a second click would queue the same reports again
        // and while looking for a start that got no answer: a retry would upload twice
        el.upload.disabled = !unsent.length || busy.upload || !!up.watching;
        if (up.watching) el.upload.textContent = 'Checking…';
        el.stop.hidden = !up.active;
        renderUpload();
    }

    function renderUpload() {
        if (!sheet) return;
        const show = up.active || up.states.length > 0;
        el.progress.hidden = !show;
        el.reauth.hidden = !up.needCreds;
        if (!show) return;
        const p = progress(up.states);
        el.bar.style.width = p.pct + '%';
        const line = el.progress.querySelector('.rq-overall');
        const ov = up.overall;
        line.textContent = ov ? ov.text : `${p.done} of ${p.total} done`;
        line.className = 'rq-overall' + (ov ? ' tone-' + ov.tone : '');
        el.upList.textContent = '';
        const latest = latestRows(up.states);
        const failedIds = new Set(store.failed().map(x => x.id));
        for (const s of up.states) {
            if (!s) continue;
            const detail = failureLine(s.msg);
            const row = h('li', { 'data-testid': 'rq-up-item', 'data-status': s.status || 'waiting' },
                h('b', { text: s.lab_id || '?' }),
                h('span', { className: 'rq-st', text: statusText(s.status) }),
                h('span', { className: 'caption rq-detail', title: detail || null,
                            text: detail && detail !== statusText(s.status) ? detail : '' }));
            const qid = s.sample_id != null ? 's' + s.sample_id : null;
            if (up.active && !isFinished(s.status)) {
                row.appendChild(h('button', { type: 'button', className: 'btn btn-ghost btn-sm', text: 'Skip',
                    'aria-label': 'Skip ' + (s.lab_id || ''), 'data-testid': 'rq-skip',
                    onclick: () => skip(s.idx) }));
            } else if (qid && failedIds.has(qid) && latest.get(Number(s.sample_id)) === s) {
                row.appendChild(h('button', { type: 'button', className: 'btn btn-sm', text: 'Retry',
                    'aria-label': 'Retry ' + (s.lab_id || ''), 'data-testid': 'rq-up-retry',
                    disabled: busy.upload || !!up.watching, onclick: () => retry([qid]) }));
            }
            el.upList.appendChild(row);
        }
    }

    function openSheet() {
        build();
        render();
        if (!sheet.open) {
            if (typeof sheet.showModal === 'function') sheet.showModal(); else sheet.setAttribute('open', '');
        }
        checkUpload(false);
        return sheet;
    }

    function onClear() {
        const n = store.count();
        if (!n) return;
        if (!root.confirm(`Remove all ${n} report${n === 1 ? '' : 's'} from the queue?`)) return;
        store.clear();
        el.msg.textContent = 'The queue is empty.';
    }

    // ── Download all ────────────────────────────────────────────────────────
    const busy = { download: false, upload: false };
    async function downloadAll() {
        const items = store.items();
        if (!items.length || busy.download) return;
        busy.download = true;
        const btn = el.download;
        render();
        try {
            if (items.length === 1) {
                btn.textContent = 'Building the PDF…';
                const r = await fetch('/api/export-analysis-report', { method: 'POST',
                    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payloads(items)[0]) });
                if (!r.ok) throw new Error(((await root.GCSession.readJson(r)).body || {}).error || ('HTTP ' + r.status));
                const name = fileName(r, `${nameOf(items[0])}_analysis_report.pdf`);
                saveBlob(await r.blob(), name);
                el.msg.textContent = `${name} downloaded.`;
                toast(`${name} downloaded`);
            } else {
                btn.textContent = 'Starting…';
                const started = await postJson('/api/export-analysis-reports-zip', { items: payloads(items) });
                if (!started.ok || !started.body.job) throw new Error(started.body.error || ('HTTP ' + started.status));
                let job = started.body.job;
                for (;;) {
                    const v = payloadLib.zipJobView(job);
                    el.msg.textContent = v.message;
                    if (v.done) {
                        if (!v.download) throw new Error(v.message.replace(/^Download failed: /, ''));
                        const a = h('a', { href: v.download, download: 'analysis_reports.zip' });
                        document.body.appendChild(a);
                        a.click();
                        a.remove();
                        toast(v.message);
                        break;
                    }
                    btn.textContent = `Building ${job.done || 0} of ${job.total}…`;
                    await new Promise(res => setTimeout(res, 1500));
                    const polled = await getJson(`/api/export-analysis-reports-zip/${encodeURIComponent(job.id)}`);
                    if (!polled.ok || !polled.body.job) throw new Error(polled.body.error || ('HTTP ' + polled.status));
                    job = polled.body.job;
                }
            }
        } catch (e) {
            el.msg.textContent = 'Download failed: ' + e.message;
            toast('Download failed: ' + e.message, 'err');
        } finally {
            busy.download = false;
            btn.textContent = 'Download all';
            render();
        }
    }

    // ── Upload to QBench ────────────────────────────────────────────────────
    async function onUpload() {
        if (up.active) { await startUpload(); return; }
        if (el.signin.hidden) {
            up.pending = null;                       // Upload sends every unsent report
            await openSignin(null);
            return;
        }
        await startUpload();
    }

    /** Show the sign-in with the hub's saved-sign-in status: when it can't be
        used, *problem* (or the hub's own) says exactly why and the password
        box is where the cursor goes. */
    async function openSignin(problem) {
        el.signin.hidden = false;
        render();
        let why = problem;
        try {
            const c = await getJson('/api/qbench-credentials');
            if (c.ok && c.body.username && !el.user.value) el.user.value = c.body.username;
            if (!why && c.ok && c.body.problem) why = c.body.problem;
        } catch (_e) { /* the fields stay empty */ }
        const line = failureLine(why);
        el.signinNote.textContent = !why ? 'Leave the password empty to use the saved QBench sign-in.'
            : (/Enter the QBench password/.test(line) ? line : line + ' Enter the QBench password to upload.');
        el.signinNote.className = why ? 'rq-reason' : 'caption';
        render();
        (el.user.value || why ? el.pass : el.user).focus();
    }

    /** The reports the sign-in's Start sends: a retry's, else every unsent one. */
    function pendingItems() {
        const unsent = store.unsent();
        if (!Array.isArray(up.pending)) return unsent;
        const ids = new Set(up.pending);
        const picked = unsent.filter(x => ids.has(x.id));
        return picked.length ? picked : unsent;
    }

    /** Retry: send these failed reports again, the queue left as it is (a
        running upload takes them; otherwise a new one starts, with the saved
        or last-used sign-in, and asks for the password only if it must). */
    async function retry(ids) {
        const want = new Set(ids || []);
        const items = store.unsent().filter(x => want.has(x.id));
        if (!items.length || busy.upload || up.watching) return;
        up.pending = items.map(x => x.id);
        await startUpload(items);
    }

    async function startUpload(subset) {
        const items = Array.isArray(subset) ? subset : pendingItems();
        if (!items.length || busy.upload) return;
        busy.upload = true;
        el.upload.disabled = true;
        el.msg.textContent = up.active ? 'Adding to the running upload…' : 'Starting the upload…';
        let res;
        try {
            res = await postJson('/api/qbench-upload', {
                queue: payloads(items),
                username: el.user.value.trim(),
                password: el.pass.value,
            }, timeouts.start);
        } catch (e) {
            if (e && e.timedOut) {
                // Giving up in the browser does not stop the hub: the start may still go
                // through. Never say "Not uploaded" here: a retry would upload twice.
                el.pass.value = '';
                await afterStartTimeout(items);
                busy.upload = false;
                render();
                syncButton();
                return;
            }
            res = { ok: false, status: 0, body: { error: e.message } };
        }
        busy.upload = false;
        el.pass.value = '';
        if (!res.ok) {
            const why = startRefusal(res.body, res.status);
            el.msg.textContent = why.text;
            if (why.needPassword) {
                // the saved sign-in can't be used: say why, ask for the password,
                // and Start sends these same reports
                up.pending = items.map(x => x.id);
                await openSignin(why.text);
            }
            render();
            return;
        }
        store.markSent(items.map(x => x.id));
        up.pending = null;
        const waitingRow = (it, idx) => ({ idx, sample_id: it.sample_id, lab_id: it.lab_id,
                                           status: 'waiting', msg: 'Waiting' });
        if (res.body.status === 'appended') {
            items.forEach((it, i) => {
                const idx = res.body.base_idx + i;
                up.states[idx] = up.states[idx] || waitingRow(it, idx);
            });
            el.msg.textContent = `Added ${res.body.count} to the running upload.`;
        } else {
            up.states = items.map(waitingRow);
            up.overall = null;
            up.active = true;
            flag(UPLOAD_KEY, '1');
            el.signin.hidden = true;
            el.msg.textContent = '';
            connect();
        }
        render();
        syncButton();
    }

    /** The run's rows → the queue: failed reports get their reason (and Retry),
        skipped ones go back to unsent. ``finished``: the run has ended, so
        anything not uploaded failed. */
    function settle(finished) {
        store.applyOutcome(reconcile(up.states, finished));
    }

    /** Queued reports that a running upload holds (by sample_id: re-injections
        share a lab ID, and adopting another run's row would leave this report
        never sent): mark them sent, so the sheet never offers to add them a
        second time. Rows with no sample_id, or that failed or were skipped,
        are never adopted. Returns how many were marked. One rule for the
        sheet's status check and for a start that got no answer. */
    const NOT_SENDING = ['failed', 'error', 'skipped'];
    function adoptRunning(running) {
        const sids = new Set();
        for (const x of Array.isArray(running) ? running : []) {
            if (!x || x.sample_id == null || NOT_SENDING.includes(x.status)) continue;
            sids.add(Number(x.sample_id));
        }
        const hit = store.unsent().filter(it => sids.has(Number(it.sample_id)));
        if (hit.length) store.markSent(hit.map(x => x.id));
        return hit.length;
    }

    /** Attach to a running upload: its rows, the stream, the flag for the next page. */
    function attach(running) {
        up.active = true;
        up.states = Array.isArray(running) ? running.slice() : up.states;
        up.overall = null;
        flag(UPLOAD_KEY, '1');
        el.signin.hidden = true;
        connect();
    }

    /** The upload status, or null when the hub cannot say. */
    async function uploadStatus() {
        try {
            const r = await getJson('/api/qbench-upload-status', true);
            return r.ok ? r.body : null;
        } catch (_e) { return null; }
    }

    /** The start got no answer in time. Giving up in the browser does not stop
        the hub, which may still start it: look for it every few seconds for a
        minute (Upload off meanwhile) and attach when it shows. */
    async function afterStartTimeout(items) {
        const ids = new Set(items.map(x => x.id));
        const waiting = () => store.items().some(it => ids.has(it.id) && !it.sent_at);
        up.watching = true;
        el.msg.textContent = 'No answer from the hub yet. Checking whether the hub started it…';
        render();
        const until = Date.now() + timeouts.watch;
        for (;;) {
            const st = await uploadStatus();
            if (st && st.active && Array.isArray(st.items)) {
                adoptRunning(st.items);
                if (!waiting()) {
                    attach(st.items);
                    el.msg.textContent = 'The upload started (the hub answered slowly).';
                    break;
                }
            }
            if (!waiting()) break;                       // attached meanwhile (the sheet's own check)
            if (Date.now() + timeouts.watchEvery > until) {
                el.msg.textContent = 'No answer from the hub, and it has not started the upload in the last '
                    + Math.round(timeouts.watch / 1000) + ' s. It may still start late: this sheet shows it '
                    + 'if it does. Upload again only if it does not appear.';
                break;
            }
            await new Promise(res => setTimeout(res, timeouts.watchEvery));
        }
        up.watching = false;
    }

    /** While an upload runs, a stream that has said nothing for a while (a
        proxy buffering it, a dropped connection the browser hasn't noticed)
        is checked against the hub's status, so the sheet never waits forever. */
    function watchStream() {
        if (up.watchdog) return;
        up.watchdog = setInterval(async () => {
            if (!up.active) { clearInterval(up.watchdog); up.watchdog = null; return; }
            if (Date.now() - (up.lastMsg || 0) < timeouts.quietStream) return;
            up.lastMsg = Date.now();
            const st = await uploadStatus();
            if (!st || !up.active) return;
            if (Array.isArray(st.items) && st.items.length) up.states = st.items.slice();
            if (!st.active) {
                const ov = st.overall ? overallView(st.overall)
                    : { text: 'The upload has ended.', tone: 'info', finished: true };
                up.overall = ov;
                finish(ov);
            } else {
                settle(false);
            }
            render();
            syncButton();
        }, timeouts.watchEvery);
    }

    function connect() {
        if (up.sse) { up.sse.close(); up.sse = null; }
        const es = new EventSource('/api/qbench-upload/stream');
        up.sse = es;
        up.lastMsg = Date.now();
        watchStream();
        es.onmessage = (e) => {
            up.lastMsg = Date.now();
            let d;
            try { d = JSON.parse(e.data); } catch (_e) { return; }
            if (d.t === 'snapshot') {
                // the stream's opening state: the rows so far and, once the
                // run has ended, how it ended (a late client never waits)
                if (Array.isArray(d.items)) up.states = d.items.slice();
                if (!d.active) {
                    const ov = d.overall ? overallView(d.overall)
                        : { text: 'The upload has ended.', tone: 'info', finished: true };
                    up.overall = ov;
                    finish(ov);
                } else {
                    settle(false);
                }
            } else if (d.t === 'item') {
                const prev = up.states[d.idx] || {};
                up.states[d.idx] = { idx: d.idx, sample_id: d.sample_id != null ? d.sample_id : prev.sample_id,
                                     lab_id: d.lab_id, status: d.status, msg: d.msg || '',
                                     step: d.step, steps: d.steps };
                if (isFinished(d.status)) settle(false);
            } else if (d.t === 'items_added') {
                for (const it of d.items || []) {
                    if (!up.states[it.idx]) {
                        up.states[it.idx] = { idx: it.idx, sample_id: it.sample_id, lab_id: it.lab_id,
                                              status: 'waiting', msg: 'Waiting' };
                    }
                }
            } else if (d.t === 'overall') {
                const ov = overallView(d);
                if (d.status === 'credentials_updated') {
                    up.needCreds = false;
                    up.overall = { text: 'Signed in again. Carrying on…', tone: 'info', finished: false };
                } else if (d.status === 'credentials_needed') {
                    up.needCreds = true;
                    up.overall = ov;
                    if (!sheet || !sheet.open) openSheet();
                    setTimeout(() => el.reauthPass && el.reauthPass.focus(), 50);
                    toast('QBench refused the sign-in. Enter the password in the report queue.', 'err');
                } else {
                    up.overall = ov;
                    if (ov.finished) finish(ov);
                }
            }
            render();
            syncButton();
        };
        es.onerror = () => {
            if (up.sse) { up.sse.close(); up.sse = null; }
            // An EventSource can't see a 401: GCSession.check sends a signed-out
            // page to /login. Otherwise ask the hub where the upload stands.
            if (root.GCSession && root.GCSession.check) root.GCSession.check();
            setTimeout(() => checkUpload(true), 1500);
        };
    }

    function finish(ov) {
        const wasActive = up.active;
        up.active = false;
        up.needCreds = false;
        flag(UPLOAD_KEY, null);
        if (up.sse) { up.sse.close(); up.sse = null; }
        settle(true);
        const failed = store.failed().length;
        if (failed && sheet) {
            // never a dead end: what failed says why on its row, and one click sends them again
            el.msg.textContent = `${failed} report${failed === 1 ? ' was' : 's were'} not uploaded; each row says why. `
                + `${retryText(failed)} sends ${failed === 1 ? 'it' : 'them'} again, the queue kept as it is.`;
        }
        if (wasActive) toast('QBench: ' + ov.text, ov.tone === 'err' ? 'err' : undefined);
    }

    /** Pick up a running upload (after a page change, or a lost stream). */
    async function checkUpload(background) {
        if (up.sse) return;
        let res;
        try { res = await getJson('/api/qbench-upload-status', background); } catch (_e) { return; }
        if (!res.ok) return;
        const b = res.body;
        if (b.active) {
            adoptRunning(b.items);
            up.active = true;
            up.states = Array.isArray(b.items) ? b.items.slice() : up.states;
            flag(UPLOAD_KEY, '1');
            connect();
        } else {
            // ended (perhaps while this page was away, or the stream was lost):
            // show how, and settle the reports this tab handed over
            if (Array.isArray(b.items) && b.items.length) up.states = b.items.slice();
            if (b.overall) up.overall = overallView(b.overall);
            const ov = up.overall || { text: 'The upload has ended.', tone: 'info', finished: true };
            if (up.active || store.items().some(x => x.sent_at)) finish(ov);
            flag(UPLOAD_KEY, null);
        }
        render();
        syncButton();
    }

    async function skip(idx) {
        const r = await postJson('/api/qbench-skip-item', { idx });
        if (!r.ok) toast('Skip failed: ' + (r.body.error || r.status), 'err');
    }

    async function stopUpload() {
        if (!root.confirm('Stop the QBench upload? Reports already uploaded stay in QBench.')) return;
        const r = await postJson('/api/qbench-cancel', {});
        if (!r.ok) { toast('Stop failed: ' + (r.body.error || r.status), 'err'); return; }
        el.msg.textContent = 'Stopping the upload…';
    }

    async function submitCreds() {
        const username = el.user.value.trim();
        const password = el.reauthPass.value;
        if (!username || !password) {
            el.msg.textContent = 'Enter the QBench username (above) and the password.';
            el.signin.hidden = false;
            render();
            return;
        }
        const r = await postJson('/api/qbench-update-credentials', { username, password });
        el.reauthPass.value = '';
        if (!r.ok) { el.msg.textContent = 'Not sent: ' + (r.body.error || r.status); return; }
        up.needCreds = false;
        el.msg.textContent = 'Sent. The upload carries on.';
        render();
    }

    // ── public API ──────────────────────────────────────────────────────────
    function add(item, opts) {
        const res = store.add(item);
        if (!res) {
            toast('Not added: pick a standard first.', 'err');
            return null;
        }
        if (!(opts && opts.quiet)) toast(addedText(res.item, res.replaced));
        return res.item;
    }
    function addMany(items, opts) {
        const n = store.addMany(items);
        if (n && !(opts && opts.quiet)) toast(`Added ${n} report${n === 1 ? '' : 's'} to the report queue`);
        return n;
    }

    store.onChange(() => { render(); syncButton(); });

    function init() {
        const btn = $('report-queue-btn');
        if (btn) btn.addEventListener('click', openSheet);
        syncButton();
        if (flag(UPLOAD_KEY) === '1') checkUpload(true);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();

    root.GCReportQueue = Object.assign({}, pure, {
        add, addMany,
        remove: (id) => store.remove(id),
        clear: () => store.clear(),
        items: () => store.items(),
        count: () => store.count(),
        onChange: (fn) => store.onChange(fn),
        openSheet,
        timeouts,
    });
})(typeof window !== 'undefined' ? window : globalThis);
