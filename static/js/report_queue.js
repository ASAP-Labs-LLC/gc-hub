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
        // key order: id, sample_id, lab_id, sample_name, standard_name,
        // conclusion, params, ranges, overlay_standards, added_at, sent_at
        const ordered = {};
        for (const k of ['id', 'sample_id', 'lab_id', 'sample_name', 'standard_name', 'conclusion',
                         'params', 'ranges', 'overlay_standards', 'added_at', 'sent_at']) {
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
            const item = normalizeItem(Object.assign({}, raw, { sent_at: null }), now);
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
                const item = normalizeItem(Object.assign({}, raw, { sent_at: null }), now);
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
        function markSent(ids, at) {
            const set = new Set(ids || []);
            const when = at || new Date().toISOString();
            write(read().map(x => (set.has(x.id) ? Object.assign({}, x, { sent_at: when }) : x)));
        }
        return {
            items: read,
            count: () => read().length,
            unsent: () => read().filter(x => !x.sent_at),
            add, addMany, remove, markSent,
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

    const pure = { STORAGE_KEY, UPLOAD_KEY, normalizeItem, payloads, createStore, buttonText,
        addedText, statusText, isFinished, progress, overallView };
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
    const timeouts = { start: 60000 };

    async function postJson(url, body, timeoutMs) {
        const ctl = timeoutMs && typeof AbortController === 'function' ? new AbortController() : null;
        let timer = null;
        const req = fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json',
            Accept: 'application/json' }, body: JSON.stringify(body || {}), signal: ctl ? ctl.signal : undefined });
        const gaveUp = !timeoutMs ? null : new Promise((_, reject) => {
            timer = setTimeout(() => {
                if (ctl) { try { ctl.abort(); } catch (_e) { /* done */ } }
                reject(new Error('no answer from the hub in ' + Math.round(timeoutMs / 1000) + ' s; try again'));
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
        el.signin = h('form', { className: 'rq-signin', 'data-testid': 'rq-signin', hidden: true,
                                onsubmit: (e) => { e.preventDefault(); startUpload(); } },
            h('h3', { text: 'QBench sign-in' }),
            h('p', { className: 'caption', text: 'Leave the password empty to use the saved QBench sign-in.' }),
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
              el.download, el.stop, el.upload));
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
        for (const it of items) {
            const meta = ['Compared with ' + it.standard_name];
            if (it.conclusion) meta.push('edited conclusion');
            const state = it.sent_at ? h('span', { className: 'pill', text: 'Sent to QBench' }) : null;
            el.list.appendChild(h('li', { 'data-testid': 'rq-item', 'data-id': it.id },
                h('div', { className: 'rq-main' },
                    h('b', { text: nameOf(it) }),
                    h('span', { className: 'caption', text: meta.join(' · ') })),
                state,
                h('button', { type: 'button', className: 'btn btn-ghost btn-sm', text: 'Remove',
                              'aria-label': 'Remove ' + nameOf(it), 'data-testid': 'rq-remove',
                              onclick: () => { store.remove(it.id); } })));
        }
        el.empty.hidden = items.length > 0;
        el.download.disabled = !items.length || busy.download;
        el.clear.disabled = !items.length;
        const signing = !el.signin.hidden;
        el.upload.hidden = up.active && !unsent.length;          // Stop and the progress say it
        if (up.active) {
            el.upload.textContent = `Add ${unsent.length} to the upload`;
        } else if (signing) {
            el.upload.textContent = `Start upload (${unsent.length})`;
        } else {
            el.upload.textContent = unsent.length && unsent.length !== items.length
                ? `Upload ${unsent.length} to QBench` : 'Upload to QBench';
        }
        // off while a start is out: a second click would queue the same reports again
        el.upload.disabled = !unsent.length || busy.upload;
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
        for (const s of up.states) {
            if (!s) continue;
            const row = h('li', { 'data-testid': 'rq-up-item', 'data-status': s.status || 'waiting' },
                h('b', { text: s.lab_id || '?' }),
                h('span', { className: 'rq-st', text: statusText(s.status) }),
                h('span', { className: 'caption rq-detail', text: s.msg && s.msg !== statusText(s.status) ? s.msg : '' }));
            if (up.active && !isFinished(s.status)) {
                row.appendChild(h('button', { type: 'button', className: 'btn btn-ghost btn-sm', text: 'Skip',
                    'aria-label': 'Skip ' + (s.lab_id || ''), 'data-testid': 'rq-skip',
                    onclick: () => skip(s.idx) }));
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
            el.signin.hidden = false;
            render();
            try {
                const c = await getJson('/api/qbench-credentials');
                if (c.ok && c.body.username && !el.user.value) el.user.value = c.body.username;
            } catch (_e) { /* the fields stay empty */ }
            (el.user.value ? el.pass : el.user).focus();
            return;
        }
        await startUpload();
    }

    async function startUpload() {
        const items = store.unsent();
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
            res = { ok: false, status: 0, body: { error: e.message } };
        }
        busy.upload = false;
        el.pass.value = '';
        if (!res.ok) {
            const refused = Array.isArray(res.body.refused) ? res.body.refused : [];
            el.msg.textContent = refused.length
                ? 'Not uploaded: ' + refused.map(r => `${r.lab_id || r.sample_id} (${r.error})`).join('; ')
                : 'Not uploaded: ' + (res.body.error || ('HTTP ' + res.status));
            render();
            return;
        }
        store.markSent(items.map(x => x.id));
        if (res.body.status === 'appended') {
            items.forEach((it, i) => {
                const idx = res.body.base_idx + i;
                up.states[idx] = up.states[idx] || { idx, lab_id: it.lab_id, status: 'waiting', msg: 'Waiting' };
            });
            el.msg.textContent = `Added ${res.body.count} to the running upload.`;
        } else {
            up.states = items.map((it, idx) => ({ idx, lab_id: it.lab_id, status: 'waiting', msg: 'Waiting' }));
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

    function connect() {
        if (up.sse) { up.sse.close(); up.sse = null; }
        const es = new EventSource('/api/qbench-upload/stream');
        up.sse = es;
        es.onmessage = (e) => {
            let d;
            try { d = JSON.parse(e.data); } catch (_e) { return; }
            if (d.t === 'item') {
                up.states[d.idx] = { idx: d.idx, lab_id: d.lab_id, status: d.status, msg: d.msg || '',
                                     step: d.step, steps: d.steps };
            } else if (d.t === 'items_added') {
                for (const it of d.items || []) {
                    if (!up.states[it.idx]) up.states[it.idx] = { idx: it.idx, lab_id: it.lab_id, status: 'waiting', msg: 'Waiting' };
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
        up.active = false;
        up.needCreds = false;
        flag(UPLOAD_KEY, null);
        if (up.sse) { up.sse.close(); up.sse = null; }
        toast('QBench: ' + ov.text, ov.tone === 'err' ? 'err' : undefined);
    }

    /** Pick up a running upload (after a page change, or a lost stream). */
    async function checkUpload(background) {
        if (up.sse) return;
        let res;
        try { res = await getJson('/api/qbench-upload-status', background); } catch (_e) { return; }
        if (!res.ok) return;
        const b = res.body;
        if (b.active) {
            up.active = true;
            up.states = Array.isArray(b.items) ? b.items.slice() : up.states;
            flag(UPLOAD_KEY, '1');
            connect();
        } else if (up.active) {
            up.active = false;
            flag(UPLOAD_KEY, null);
        } else {
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
