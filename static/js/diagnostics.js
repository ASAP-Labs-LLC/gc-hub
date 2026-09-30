/* Hub admin "Download diagnostics" (/admin/hub, the Diagnostics panel).
   The pure helpers are module.exports for the Node tests; in the browser the
   panel is wired up below. Sizes come from POST /api/admin/diagnostics/estimate
   and the build from POST .../bundle (JSON with the password), which answers
   202 {job} at once (v3.0.1: a build takes minutes, and Cloudflare ends a
   request after 100 s); the page polls POST .../status until the job is done,
   then navigates to its one-time download URL, so a large zip streams to disk
   instead of into memory. DOM text is textContent only. */
(function () {
    'use strict';

    const WARN_BYTES = 500 * 1024 * 1024;

    function formatBytes(n) {
        if (typeof n !== 'number' || !isFinite(n) || n < 0) return '?';
        if (n < 1024) return `${n} B`;
        const units = ['KB', 'MB', 'GB', 'TB'];
        let v = n / 1024;
        let i = 0;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return `${v.toFixed(1)} ${units[i]}`;
    }

    // [{key, checked}] -> {key: bool}
    function chosenOptions(boxes) {
        const out = {};
        for (const b of boxes) out[b.key] = !!b.checked;
        return out;
    }

    function estimateTotal(rows, chosen) {
        let total = 0;
        for (const r of rows) if (chosen[r.key] && typeof r.bytes === 'number') total += r.bytes;
        return total;
    }

    // A warning for the chosen set, or null: always when all raw CDFs are on
    // (their size grows forever), and whenever the total is large.
    function sizeWarning(rows, chosen) {
        const all = rows.find((r) => r.key === 'all_cdfs');
        const total = estimateTotal(rows, chosen);
        if (chosen.all_cdfs) {
            return `All raw CDFs adds about ${formatBytes(all ? all.bytes : null)}: the build ` +
                'and the download can take a long time. Leave it off unless Claude asked for it.';
        }
        if (total > WARN_BYTES) {
            return `This bundle is about ${formatBytes(total)} before compression; ` +
                'untick what you do not need.';
        }
        return null;
    }

    // The file name from Content-Disposition, without any path.
    function filenameFromDisposition(header) {
        const m = /filename="?([^";]+)"?/i.exec(header || '');
        const name = m ? m[1].split(/[\\/]/).pop() : '';
        return name && name !== '.' && name !== '..' ? name : 'gc-diagnostics.zip';
    }

    // Only the hub's own one-time download path is followed.
    function isDownloadUrl(u) {
        return typeof u === 'string' &&
            /^\/api\/admin\/diagnostics\/download\/[A-Za-z0-9_-]+$/.test(u);
    }

    const BUILDING_NOTE = '(this page keeps working; large bundles take a while)';

    // What the panel shows for a polled build job, and whether it is over.
    function buildView(job) {
        if (job.state === 'running') {
            const phase = job.progress && job.progress.phase;
            return { done: false, cls: '', result: null,
                     message: phase ? `Building the bundle: ${phase}… ${BUILDING_NOTE}`
                                    : `Building the bundle… ${BUILDING_NOTE}` };
        }
        if (job.state === 'done' && job.result && job.result.summary) {
            return { done: true, message: null, cls: 'ok', result: job.result.summary };
        }
        return { done: true, cls: 'err', result: null,
                 message: `The diagnostics bundle failed: ${job.error || job.state}` };
    }

    function downloadStartedText(j) {
        return `Download started: ${j.name} (${formatBytes(j.size)}, ${j.files} files` +
            (j.skipped ? `; ${j.skipped} left out, see summary.txt` : '') + ')';
    }

    const pure = { formatBytes, chosenOptions, estimateTotal, sizeWarning,
                   filenameFromDisposition, isDownloadUrl, buildView, downloadStartedText };
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = pure;
        return;
    }
    window.Diagnostics = pure;

    // ── the browser panel ─────────────────────────────────────────────────
    const $ = (id) => document.getElementById(id);
    let estimateRows = [];

    function boxes() {
        return Array.from(document.querySelectorAll('#diag-options input[type=checkbox]'))
            .map((b) => ({ key: b.dataset.key, checked: b.checked }));
    }

    function say(text, cls) {
        const m = $('diag-msg');
        m.textContent = text || '';
        m.className = cls || '';
    }

    function refreshTotals() {
        const chosen = chosenOptions(boxes());
        $('diag-total').textContent = estimateRows.length
            ? `Estimated size before compression: ${formatBytes(estimateTotal(estimateRows, chosen))}`
            : '';
        const warn = sizeWarning(estimateRows, chosen);
        $('diag-warning').textContent = (chosen.all_cdfs && !estimateRows.length)
            ? 'All raw CDFs can be very large: press Check sizes first.' : (warn || '');
    }

    async function checkSizes() {
        say('Checking sizes…');
        try {
            const r = await fetch('/api/admin/diagnostics/estimate', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store',
                body: JSON.stringify({ password: $('pw').value }) });
            const j = (await window.GCSession.readJson(r)).body || {};
            if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
            estimateRows = j.options || [];
            for (const row of estimateRows) {
                const td = document.querySelector(`#diag-options tr[data-key="${row.key}"] .diag-size`);
                if (td) {
                    td.textContent = `${formatBytes(row.bytes)} (${row.files} file${row.files === 1 ? '' : 's'})` +
                        (row.note ? ` — ${row.note}` : '');
                }
            }
            say('');
        } catch (e) { say(e.message, 'err'); }
        refreshTotals();
    }

    async function post(path, body) {
        const r = await fetch(path, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store',
            body: JSON.stringify(Object.assign({ password: $('pw').value }, body || {})),
        });
        const j = (await window.GCSession.readJson(r)).body || {};
        if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
        return j;
    }

    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    // Start the build, then poll its job until it is over; the result, or throws.
    async function build() {
        const started = await post('/api/admin/diagnostics/bundle',
                                   { options: chosenOptions(boxes()) });
        let job = started.job;
        for (;;) {
            const v = buildView(job);
            if (v.done) {
                if (!v.result) throw new Error(v.message);
                return v.result;
            }
            say(v.message);
            await sleep(1500);
            const s = await post('/api/admin/diagnostics/status');
            if (!s.job || s.job.id !== started.job.id) {
                throw new Error('The build was replaced by another one; press Download again');
            }
            job = s.job;
        }
    }

    async function download() {
        const btn = $('btn-diag-download');
        btn.disabled = true;
        say(buildView({ state: 'running' }).message);
        try {
            const j = await build();
            if (!isDownloadUrl(j.download)) throw new Error('Unexpected answer from the hub');
            // A navigation, not fetch + blob: the browser streams the zip to disk.
            const a = document.createElement('a');
            a.href = j.download;
            a.download = j.name || 'gc-diagnostics.zip';
            document.body.appendChild(a);
            a.click();
            a.remove();
            say(downloadStartedText(j), 'ok');
        } catch (e) {
            say(e.message, 'err');
        } finally {
            btn.disabled = false;
        }
    }

    function init() {
        if (!$('diag-panel')) return;
        $('btn-diag-estimate').addEventListener('click', checkSizes);
        $('btn-diag-download').addEventListener('click', download);
        document.querySelectorAll('#diag-options input[type=checkbox]')
            .forEach((b) => b.addEventListener('change', refreshTotals));
        refreshTotals();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
