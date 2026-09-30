/* Sendable sample links (v4.0): what a link URL opens on the classic page,
   and "Copy link". v5.0.0: the classic page is at /classic, so its links are
   /classic/lab/<id> and /classic/samples/<id>[/compare|/data] (the paths
   below, after /classic); the plain paths open the new Samples page. Pure helpers first (window globals + module.exports, node
   tested in tests/js/deeplink.test.js), then a thin browser hook:

     /lab/<lab_id>                         the lab ID's newest run (GET /api/lab/<id>),
                                           with its other runs listed on the Dashboard
     /samples/<id>                         that run, Dashboard
     /samples/<id>/compare[?standard=<n>]  that run, Analysis (the standard picked)
     /samples/<id>/data                    that run, Distillation Data
     /?q=<text>                            the sample search (the not-found page's link)

   app.js calls DeepLink.start() once, after the sample list first loads, and
   DeepLink.wireContextItem(file) from the sample context menu. A copied link is
   <link_url>/samples/<id>, link_url from GET /api/session (the configured hub,
   or https://gc.asaplabs.net when that is LAN-only), never location.origin: a link copied
   over the LAN must still open from anywhere. The DOM is built with
   textContent only (lab IDs and instrument names are untrusted). */
(function (root) {
    const DEFAULT_HUB_URL = 'https://gc.asaplabs.net';
    const TABS = { dashboard: 'tab-dashboard', analysis: 'tab-analysis', data: 'tab-distilldata' };
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    function _decodeOnce(s) {
        try { return decodeURIComponent(s); } catch (_) { return s; }
    }

    function _query(search, key) {
        const qs = String(search || '').replace(/^\?/, '');
        for (const part of qs.split('&')) {
            const eq = part.indexOf('=');
            const k = eq < 0 ? part : part.slice(0, eq);
            if (_decodeOnce(k.replace(/\+/g, ' ')) !== key) continue;
            const v = eq < 0 ? '' : _decodeOnce(part.slice(eq + 1).replace(/\+/g, ' '));
            return v.trim() === '' ? null : v;
        }
        return null;
    }

    /** What the URL asks to open, or null: {kind: 'lab', labId} |
        {kind: 'sample', sampleId, tab, standard} | {kind: 'search', q}.
        ``pathname`` is as the browser keeps it (encoded): decoded once. */
    function parseLocation(pathname, search) {
        // v5.0.0: the classic page lives at /classic, its links under /classic/…
        const path = String(pathname || '').replace(/\/+$/, '').replace(/^\/classic(?=\/|$)/, '');
        let m = /^\/lab\/([^/]+)$/.exec(path);
        if (m) return { kind: 'lab', labId: _decodeOnce(m[1]) };
        m = /^\/samples\/([1-9][0-9]*)(?:\/(compare|data))?$/.exec(path);
        if (m) {
            const tab = m[2] === 'compare' ? 'analysis' : (m[2] === 'data' ? 'data' : 'dashboard');
            return { kind: 'sample', sampleId: Number(m[1]), tab,
                     standard: tab === 'analysis' ? _query(search, 'standard') : null };
        }
        if (path === '' && pathname) {          // / or /classic
            const q = _query(search, 'q');
            if (q !== null) return { kind: 'search', q };
        }
        return null;
    }

    function tabId(tab) { return TABS[tab] || TABS.dashboard; }
    function isAdvancedTab(tab) { return tab === 'data'; }

    /** The link to copy for a sample: always the hub's address. */
    function sampleLink(hubUrl, sampleId) {
        const base = String(hubUrl || '').replace(/\/+$/, '') || DEFAULT_HUB_URL;
        return `${base}/samples/${sampleId}`;
    }

    /** The link from GET /api/session's ``link_url`` (the hub URL, or the
        public default when the hub URL is LAN-only). ``loc`` is deliberately
        ignored: the page's own origin (a LAN address) is never used. */
    function linkFromSession(session, sampleId, loc) {
        return sampleLink(session && session.link_url, sampleId);
    }

    /** /api/files for the one row a link needs when the loaded list leaves it
        out: its lab ID, on its instrument, on its day (never crowded out). */
    function exactFileUrl(meta) {
        if (!meta || meta.lab_id == null) return null;
        const parts = ['limit=50', 'q=' + encodeURIComponent(meta.lab_id)];
        if (meta.instrument) parts.push('instrument=' + encodeURIComponent(meta.instrument));
        const day = /^(\d{4}-\d{2}-\d{2})/.exec(String(meta.injection_datetime || ''));
        if (day) parts.push(`date_from=${day[1]}`, `date_to=${day[1]}`);
        return '/api/files?' + parts.join('&');
    }

    function labApiUrl(labId) { return '/api/lab/' + encodeURIComponent(labId); }

    /** "GC-2 · Sep 28 15:30" for a run. */
    function runLabel(run) {
        const inst = (run && (run.instrument_name || run.instrument)) || '?';
        const dt = String((run && run.injection_dt) || '');
        const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(dt);
        const when = m ? `${MONTHS[Number(m[2]) - 1] || m[2]} ${Number(m[3])} ${m[4]}:${m[5]}` : dt;
        return `${inst} · ${when}`;
    }

    /** The runs of a resolved lab ID other than the one opened. */
    function otherRuns(resolved) {
        if (!resolved || !Array.isArray(resolved.runs)) return [];
        return resolved.runs.filter(r => r.sample_id !== resolved.sample_id).map(r => ({
            sample_id: r.sample_id, href: `/samples/${r.sample_id}`, label: runLabel(r),
            status: r.status,
        }));
    }

    function otherRunsTitle(labId) { return `Other runs of ${labId}:`; }

    /** ?standard=: exact name, else case-insensitive, else null. */
    function findStandard(standards, name) {
        const list = Array.isArray(standards) ? standards : [];
        const want = String(name || '');
        return list.find(s => s && s.name === want)
            || list.find(s => s && String(s.name).toLowerCase() === want.toLowerCase())
            || null;
    }

    function findFile(files, sampleId) {
        const id = Number(sampleId);
        return (Array.isArray(files) ? files : []).find(f => f && Number(f.sample_id) === id) || null;
    }

    const pure = {
        DEFAULT_HUB_URL, parseLocation, tabId, isAdvancedTab, sampleLink, linkFromSession,
        exactFileUrl, labApiUrl, runLabel, otherRuns, otherRunsTitle, findStandard, findFile,
    };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = pure;
        return;
    }

    /* ── the browser hook (uses app.js's globals: state, switchTab, …) ── */

    let _session = null;

    /** GET /api/session once. ``background``: the start-up warm-up, which the
        page makes on its own (GCLive.bgFetch: not activity); a Copy link
        click that finds nothing cached asks with a plain fetch. */
    async function _loadSession(background) {
        if (!_session) {
            try {
                const opts = { cache: 'no-store', headers: { Accept: 'application/json' } };
                const r = (background && root.GCLive)
                    ? await GCLive.bgFetch('/api/session', opts)
                    : await fetch('/api/session', opts);
                const res = await GCSession.readJson(r);
                if (r.ok && res.body && !res.body.error) _session = res.body;
            } catch (_) { /* linkFromSession falls back to the default */ }
        }
        return _session;
    }

    function _notify(msg, type) {
        if (typeof showNotification === 'function') showNotification(msg, type);
    }

    async function _copyText(text) {
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
                return true;
            }
        } catch (_) { /* fall back */ }
        // Plain http over the LAN has no async clipboard.
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        let ok = false;
        try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
        ta.remove();
        return ok;
    }

    async function copyLink(file) {
        if (!file || file.sample_id == null) {
            _notify('Select a sample first', 'error');
            return null;
        }
        const url = linkFromSession(await _loadSession(), file.sample_id);
        api.lastCopied = url;
        if (await _copyText(url)) _notify('Link copied', 'success');
        else window.prompt('Copy this link:', url);
        return url;
    }

    /** The sample context menu's "Copy link" (called by showContextMenu). */
    function wireContextItem(file) {
        const item = document.getElementById('ctx-copy-link');
        if (!item) return;
        const fresh = item.cloneNode(true);
        item.replaceWith(fresh);
        fresh.style.display = '';
        fresh.addEventListener('click', () => {
            if (typeof removeContextMenu === 'function') removeContextMenu();
            copyLink(file);
        });
    }

    function _renderOtherRuns(resolved) {
        const old = document.getElementById('deeplink-runs');
        if (old) old.remove();
        const runs = otherRuns(resolved);
        const grid = document.getElementById('dash-grid');
        if (!runs.length || !grid) return;
        const box = document.createElement('div');
        box.id = 'deeplink-runs';
        box.dataset.testid = 'other-runs';
        box.style.cssText = 'font-size:12px;color:#9da7b3;padding:4px 8px;';
        const title = document.createElement('span');
        title.textContent = otherRunsTitle(resolved.lab_id) + ' ';
        box.appendChild(title);
        runs.forEach((r, i) => {
            if (i) box.appendChild(document.createTextNode(', '));
            const a = document.createElement('a');
            a.href = r.href;
            a.textContent = r.label + (r.status && r.status !== 'final' ? ` (${r.status})` : '');
            a.style.color = '#58a6ff';
            box.appendChild(a);
        });
        grid.parentElement.insertBefore(box, grid);
    }

    async function _json(url) {
        // the lookups the opened link asks for (not background work)
        const r = await fetch(url, { cache: 'no-store', headers: { Accept: 'application/json' } });
        const res = await GCSession.readJson(r);
        return { ok: r.ok, body: res.body || {} };
    }

    /** Make ``file`` the one selected sample, as a plain click would. */
    function _select(file) {
        const uid = typeof sampleUid === 'function' ? sampleUid(file) : String(file.sample_id);
        state.selectedUids = new Set([uid]);
        state.selectionAnchor = uid;
        state.selectedFile = file;
        state.selectedSample = file;
        const label = document.getElementById('analysis-sample-label');
        if (label) label.textContent = file.name;
    }

    /** The sample's row. When the list's instrument filter leaves it out, the
        list is reloaded for every instrument (this view only; the remembered
        filter is not changed), so "All" really shows all. When the loaded page
        leaves it out, the list shows a search for its lab ID, with the row
        itself fetched exactly if even that search is crowded. */
    async function _fileFor(sampleId) {
        const file = findFile(state.files, sampleId);
        if (file && (!state.listInstrument || file.instrument === state.listInstrument)) return file;
        const meta = await _json(`/api/samples/${sampleId}/metadata`);
        if (!meta.ok) return null;
        if (state.listInstrument) {
            state.listInstrument = null;
            const sel = document.getElementById('instrument-filter');
            if (sel) sel.value = '';
            state.searchResult = null;
            await loadFiles();
            const reloaded = findFile(state.files, sampleId);
            if (reloaded) return reloaded;
        }
        const labId = meta.body.lab_id;
        const limit = typeof FILES_PAGE_LIMIT !== 'undefined' ? FILES_PAGE_LIMIT : 5000;
        const res = await _json(filesUrl({ q: labId }, limit));
        let samples = res.ok ? (res.body.samples || []) : [];
        let found = findFile(samples, sampleId);
        if (!found) {
            const one = await _json(exactFileUrl(meta.body));
            found = one.ok ? findFile(one.body.samples, sampleId) : null;
            if (!found) return null;
            samples = [found].concat(samples);
        }
        const search = document.getElementById('universal-search');
        if (search) search.value = labId;
        state.searchResult = { q: labId, samples,
                               total: Math.max((res.ok && res.body.total) || 0, samples.length) };
        return found;
    }

    /** Distillation Data: mark the sample's row and scroll to it. */
    function _showDataRow(sampleId) {
        state.linkedTableSampleId = sampleId;
        if (typeof renderDistillTable === 'function') renderDistillTable();
        const tr = document.querySelector('#distill-table-body tr.linked-row');
        if (tr && tr.scrollIntoView) tr.scrollIntoView({ block: 'center' });
        else _notify(`Sample #${sampleId} has no row in Distillation Data (no result yet)`, 'info');
    }

    async function _open(target) {
        if (target.kind === 'search') {
            const search = document.getElementById('universal-search');
            if (search) search.value = target.q;
            if (typeof onSearchInput === 'function') onSearchInput();
            return;
        }
        let sampleId = target.sampleId;
        let resolved = null;
        if (target.kind === 'lab') {
            const res = await _json(labApiUrl(target.labId));
            if (!res.ok) {
                _notify(`No GC result for lab ID ${target.labId} yet`, 'error');
                return;
            }
            resolved = res.body;
            sampleId = resolved.sample_id;
        }
        const file = await _fileFor(sampleId);
        if (!file) {
            _notify(`Sample #${sampleId} could not be found`, 'error');
            return;
        }
        _select(file);
        if (resolved) _renderOtherRuns(resolved);
        const tab = target.tab || 'dashboard';
        if (tab === 'analysis') {
            const std = target.standard ? findStandard(state.comparisonStandards, target.standard) : null;
            if (target.standard && !std) _notify(`No comparison standard named ${target.standard}`, 'error');
            if (std) {
                state.selectedStandard = std;
                state.standardPinned = true;
                if (typeof renderComparisonStandards === 'function') renderComparisonStandards();
            } else {
                state.standardPinned = false;
                if (typeof autoSelectBestFitStandard === 'function') autoSelectBestFitStandard(file);
            }
        }
        if (isAdvancedTab(tab) && !state.advancedViewsVisible && typeof toggleAdvancedViews === 'function') {
            toggleAdvancedViews();
        }
        const idx = Array.from(document.querySelectorAll('.tab-btn'))
            .findIndex(b => b.dataset.tab === tabId(tab));
        switchTab(idx < 0 ? 0 : idx);     // re-renders the lists and loads the tab
        if (tab === 'data') {
            _showDataRow(file.sample_id);
            return;
        }
        const row = document.querySelector('.tab-pane.active li.selected');
        if (row && row.scrollIntoView) row.scrollIntoView({ block: 'center' });
    }

    /** Called once by app.js after the sample list first loads. */
    async function start() {
        const btn = document.getElementById('btn-copy-link');
        if (btn) btn.addEventListener('click', () => copyLink(state.selectedFile));
        _loadSession(true);                         // warm: copying stays in the click
        api.started = true;
        const target = parseLocation(location.pathname, location.search);
        if (!target) return;
        try {
            await _open(target);
        } catch (e) {
            console.error('[deeplink]', e);
            _notify('Could not open the linked sample: ' + e.message, 'error');
        }
        api.applied = target;
    }

    const api = Object.assign({}, pure, { start, copyLink, wireContextItem,
                                          lastCopied: null, started: false, applied: null });
    root.DeepLink = api;
})(typeof window !== 'undefined' ? window : globalThis);
