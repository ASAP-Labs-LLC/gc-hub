/* Hub admin "Download diagnostics" (/admin/hub, the Diagnostics panel).
   The pure helpers are module.exports for the Node tests; in the browser the
   panel is wired up below. Sizes come from POST /api/admin/diagnostics/estimate
   and the build from POST .../bundle (JSON with the password); the bundle
   answers a one-time download URL the browser then navigates to, so a large
   zip streams to disk instead of into memory. DOM text is textContent only. */
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

    const pure = { formatBytes, chosenOptions, estimateTotal, sizeWarning,
                   filenameFromDisposition, isDownloadUrl };
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

    async function download() {
        const btn = $('btn-diag-download');
        btn.disabled = true;
        say('Building the bundle… (this page keeps working; large bundles take a while)');
        try {
            const r = await fetch('/api/admin/diagnostics/bundle', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store',
                body: JSON.stringify({ password: $('pw').value, options: chosenOptions(boxes()) }),
            });
            const j = (await window.GCSession.readJson(r)).body || {};
            if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
            if (!isDownloadUrl(j.download)) throw new Error('Unexpected answer from the hub');
            // A navigation, not fetch + blob: the browser streams the zip to disk.
            const a = document.createElement('a');
            a.href = j.download;
            a.download = j.name || 'gc-diagnostics.zip';
            document.body.appendChild(a);
            a.click();
            a.remove();
            say(`Download started: ${j.name} (${formatBytes(j.size)}, ${j.files} files` +
                (j.skipped ? `; ${j.skipped} left out, see summary.txt` : '') + ')', 'ok');
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
