/* Hub admin "Purge instrument data" (/admin/hub, v3.1).
   Instrument + scope -> Preview (POST /api/admin/purge/preview) -> type the
   confirmation ("PURGE <instrument name>") -> Start (POST /api/admin/purge/start,
   an admin job) -> progress from POST /api/admin/jobs/status. Every call is JSON
   with the admin password from the page's #pw box. The pure helpers are
   module.exports for the Node tests. Answers are parsed with
   GCSession.readJson. DOM text is textContent only: lab IDs,
   paths and names come from the server. */
(function () {
    'use strict';

    function formatBytes(n) {
        if (typeof n !== 'number' || !isFinite(n) || n < 0) return '?';
        if (n < 1024) return `${n} B`;
        const units = ['KB', 'MB', 'GB', 'TB'];
        let v = n / 1024;
        let i = 0;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return `${v.toFixed(1)} ${units[i]}`;
    }

    function plural(n, one, many) {
        return `${Number(n || 0).toLocaleString('en-US')} ${n === 1 ? one : (many || one + 's')}`;
    }

    // Start is allowed only for the preview shown, of the instrument and scope
    // still chosen, when it found something to purge, and the typed text is
    // exactly the confirmation the server asked for.
    function canStart(preview, typed, instrument, scope) {
        return !!(preview && preview.ok && preview.samples > 0 &&
            preview.instrument === instrument && preview.scope === scope &&
            typeof typed === 'string' && typed === preview.confirm_text);
    }

    // [{text, cls}] describing a preview, in the order the panel shows them.
    function previewLines(pv) {
        if (!pv) return [];
        const out = [];
        for (const p of pv.problems || []) out.push({ text: p, cls: 'err' });
        const scope = pv.scope === 'backfill' ? 'backfill (imported history) samples'
            : 'samples';
        if (!pv.samples) {
            out.push({ text: `Nothing to purge: ${pv.instrument_name} has no ${scope}.`, cls: 'ok' });
            return out;
        }
        out.push({
            text: `${plural(pv.samples, 'sample')} of ${pv.instrument_name} (${pv.scope}) will be ` +
                'removed from the database, with every row that belongs to them:', cls: 'warn',
        });
        const tables = Object.entries(pv.tables || {}).filter(([t, n]) => t !== 'samples' && n)
            .map(([t, n]) => `${t}: ${n}`).join(', ');
        if (tables) out.push({ text: tables, cls: 'muted' });
        const f = pv.files || {};
        out.push({
            text: `${plural(f.move, 'CDF file')} (${formatBytes(f.bytes)}) will be moved to the ` +
                'purged folder, never deleted' +
                (f.missing ? `; ${plural(f.missing, 'file')} already missing` : '') + '.', cls: '',
        });
        for (const k of pv.kept_samples || []) {
            out.push({ text: `Kept: sample ${k.id} (${k.lab_id || '?'}), ${k.reason}`, cls: 'muted' });
        }
        if (pv.kept_files_total) {
            out.push({ text: `${plural(pv.kept_files_total, 'CDF')} stay where they are because ` +
                'something kept still uses them:', cls: 'muted' });
            for (const k of pv.kept_files || []) {
                out.push({ text: `  ${k.path}: ${k.reason}`, cls: 'muted' });
            }
        }
        if (pv.warning) out.push({ text: pv.warning, cls: 'warn' });
        out.push({ text: `Type ${pv.confirm_text} below to confirm.`, cls: '' });
        return out;
    }

    function jobLine(job) {
        if (!job || job.kind !== 'purge') return '';
        const p = job.params || {};
        const prog = job.progress || {};
        const warned = job.state === 'done' && job.summary && job.summary.completed_with_warnings;
        let s = `Purge ${p.instrument || ''} (${p.scope || ''}): ` +
            (warned ? 'completed with warnings' : job.state);
        if (job.state === 'running' && prog.phase) {
            s += ` — ${prog.phase}` + (prog.total ? ` ${prog.done || 0}/${prog.total}` : '');
        }
        const sum = job.summary;
        if (sum && job.state === 'done') {
            s += sum.nothing_to_do ? ' — nothing to purge'
                : ` — ${plural(sum.samples, 'sample')} purged, ` +
                  `${plural((sum.files || {}).moved, 'file')} moved to ${sum.purged_folder}; ` +
                  `backup ${sum.backup}`;
        }
        if (job.error) s += ` — ${job.error}`;
        return s;
    }

    // [{text, cls}]: a finished purge's warnings and the files not moved.
    function finishedLines(summary) {
        if (!summary) return [];
        const out = (summary.warnings || []).map((w) => ({ text: `Warning: ${w}`, cls: 'warn' }));
        for (const f of (summary.files || {}).failed || []) {
            out.push({ text: `Not moved: ${f.path} (${f.error})`, cls: 'warn' });
        }
        return out;
    }

    // The newest purge journal (GET /api/purge/status), for a page loaded
    // after the purge ended (a restart may have finished or abandoned it).
    function journalLine(j) {
        if (!j) return '';
        let s = `Last purge: ${j.instrument} (${j.scope}) ${j.state}`;
        if (j.state === 'abandoned') {
            s += ` — nothing was removed${j.reason ? ` (${j.reason})` : ''}`;
        } else {
            if (j.recovered) s += ', finished after a restart';
            if (j.completed_with_warnings) s += ', with warnings';
            s += ` — ${plural(j.samples, 'sample')}`;
        }
        return s + (j.finished_at ? `, ${j.finished_at}` : '');
    }

    // The instrument picker, from GET /api/instruments (the panel fills its
    // own select: it depends on nothing else on the page).
    function instrumentOptions(body) {
        return ((body && body.instruments) || []).map((i) => ({
            value: String(i.id), text: i.name && i.name !== i.id ? `${i.name} (${i.id})` : String(i.id),
        }));
    }

    const api = { formatBytes, canStart, previewLines, jobLine, finishedLines, journalLine,
        instrumentOptions };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (typeof document === 'undefined') return;

    // ── DOM ──────────────────────────────────────────────────────────────
    const $ = (id) => document.getElementById(id);
    let shown = null;           // the preview on screen
    let timer = null;

    function say(text, cls) {
        const m = $('purge-msg');
        m.textContent = text || '';
        m.className = cls || '';
    }

    async function call(path, body) {
        const r = await fetch(path, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(Object.assign({ password: $('pw').value }, body || {})),
        });
        const j = (await window.GCSession.readJson(r)).body || {};
        if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
        return j;
    }

    function chosen() {
        return { instrument: $('purge-inst').value, scope: $('purge-scope').value };
    }

    function refreshStart() {
        const c = chosen();
        $('btn-purge-start').disabled = !canStart(shown, $('purge-confirm').value,
            c.instrument, c.scope);
    }

    function renderPreview(pv) {
        const box = $('purge-preview');
        box.textContent = '';
        for (const line of previewLines(pv)) {
            const p = document.createElement('p');
            p.textContent = line.text;
            if (line.cls) p.className = line.cls;
            box.appendChild(p);
        }
        $('purge-confirm').placeholder = pv && pv.confirm_text ? pv.confirm_text : '';
        $('purge-confirm').value = '';
        refreshStart();
    }

    async function doPreview() {
        shown = null;
        renderPreview(null);
        say('');
        try {
            const j = await call('/api/admin/purge/preview', chosen());
            shown = j.preview;
            renderPreview(shown);
        } catch (e) { say(e.message, 'err'); }
    }

    function renderJob(job, journal) {
        const box = $('purge-job');
        box.textContent = '';
        const head = document.createElement('p');
        const warned = job && job.summary && job.summary.completed_with_warnings;
        head.textContent = job ? jobLine(job) : journalLine(journal);
        head.className = job && job.state === 'failed' ? 'err'
            : (warned ? 'warn' : (job && job.state === 'done' ? 'ok' : 'muted'));
        box.appendChild(head);
        const summary = job ? (job.state === 'done' ? job.summary : null) : journal;
        for (const line of finishedLines(summary)) {
            const p = document.createElement('p');
            p.textContent = line.text;
            p.className = line.cls;
            box.appendChild(p);
        }
    }

    // GET /api/purge/status needs no password: the page shows a running (or
    // the last) purge as soon as it loads, and follows it until it ends.
    async function poll() {
        clearTimeout(timer);
        try {
            // a background poll: never counted as someone using the hub
            const r = await fetch('/api/purge/status', {
                cache: 'no-store', headers: { 'X-GC-Background': '1' } });
            const j = (await window.GCSession.readJson(r)).body || {};
            if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
            if (j.job || j.journal) renderJob(j.job, j.journal);
            if (j.job && j.job.state === 'running') timer = setTimeout(poll, 1500);
        } catch (e) { say(e.message, 'err'); }
    }

    async function doStart() {
        const c = chosen();
        if (!canStart(shown, $('purge-confirm').value, c.instrument, c.scope)) return;
        const body = Object.assign(c, { confirm_text: $('purge-confirm').value });
        const np = $('purge-new-path').value.trim();
        if (np) body.new_results_path = np;
        $('btn-purge-start').disabled = true;
        try {
            const j = await call('/api/admin/purge/start', body);
            shown = null;
            $('purge-confirm').value = '';
            renderJob(j.job);
            say('Purge started', 'ok');
            poll();
        } catch (e) {
            say(e.message, 'err');
            refreshStart();
        }
    }

    async function loadInstruments() {
        try {
            const r = await fetch('/api/instruments', {
                cache: 'no-store', headers: { 'X-GC-Background': '1' } });
            const j = (await window.GCSession.readJson(r)).body || {};
            if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
            const sel = $('purge-inst');
            const chosen = sel.value;
            sel.textContent = '';
            for (const o of instrumentOptions(j)) {
                const opt = document.createElement('option');
                opt.value = o.value;
                opt.textContent = o.text;
                sel.appendChild(opt);
            }
            if (chosen) sel.value = chosen;
        } catch (e) { say(e.message, 'err'); }
    }

    function init() {
        if (!$('purge-panel')) return;
        loadInstruments();
        $('btn-purge-preview').addEventListener('click', doPreview);
        $('btn-purge-start').addEventListener('click', doStart);
        $('purge-confirm').addEventListener('input', refreshStart);
        $('purge-inst').addEventListener('change', () => { shown = null; renderPreview(null); });
        $('purge-scope').addEventListener('change', () => { shown = null; renderPreview(null); });
        $('btn-purge-status').addEventListener('click', poll);
        refreshStart();
        poll();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
