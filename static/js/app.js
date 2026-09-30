/**
 * app.js -- GC Viewer & Distillation Parser (Web Edition)
 * ========================================================
 * Single-page application driving all interactivity.
 * Communicates with a Flask backend via REST API calls.
 *
 * Requirements: Plotly.js loaded globally before this script.
 */

/* ===================================================================
   0. CONSTANTS & CONVERSION TABLES
   =================================================================== */

const PERCENT_LEVELS = [0.5, 5.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 95.0, 99.5];
const D86_LABELS = ["IBP", "5%", "10%", "20%", "30%", "40%", "50%", "60%", "70%", "80%", "90%", "95%", "FBP"];

const CONVERSION_COEFF = {
    "IBP": [25.351, 0.32216, 0.71187, -0.04221],
    "5%":  [18.822, 0.06602, 0.15803, 0.77898],
    "10%": [15.173, 0.20149, 0.30606, 0.48227],
    "20%": [13.141, 0.22677, 0.29042, 0.46023],
    "30%": [5.7766, 0.37218, 0.30313, 0.31118],
    "50%": [6.3753, 0.07763, 0.68984, 0.18302],
    "70%": [-2.8437, 0.16366, 0.42102, 0.38252],
    "80%": [-0.21536, 0.25614, 0.40925, 0.27995],
    "90%": [0.09966, 0.24335, 0.32051, 0.37357],
    "95%": [0.89880, -0.09790, 1.03816, -0.00894],
    "FBP": [19.444, -0.38161, 1.08571, 0.17729],
};

const CONVERSION_REL = {
    "IBP": ["IBP", "5%", "10%"],
    "5%":  ["IBP", "5%", "10%"],
    "10%": ["5%", "10%", "20%"],
    "20%": ["10%", "20%", "30%"],
    "30%": ["20%", "30%", "50%"],
    "50%": ["30%", "50%", "70%"],
    "70%": ["50%", "70%", "80%"],
    "80%": ["70%", "80%", "90%"],
    "90%": ["80%", "90%", "95%"],
    "95%": ["90%", "95%", "FBP"],
    "FBP": ["90%", "95%", "FBP"],
};

/* ===================================================================
   1. APPLICATION STATE
   =================================================================== */

// How many samples the lists load (newest first); search filters within them.
const FILES_PAGE_LIMIT = 5000;

const state = {
    settings: {},
    files: [],              // /api/files samples: {sample_id, uid, lab_id, name, status, ...}
    filesTotal: 0,
    instruments: [],        // instrument ids (/api/files, then /api/instruments)
    instrumentNames: {},    // {id: name} from /api/instruments, for badges and labels
    searchResult: null,     // {q, samples, total}: a server search beyond the loaded page
    listInstrument: null,   // the toolbar's instrument select (null = all); sent with the
                            // page load and every server search; remembered per browser
    listStatus: null,       // status filter sent with a server search (no UI control yet)
    selectedFile: null,
    selectedUids: new Set(),   // multi-selection (shift / ctrl-cmd)
    selectionAnchor: null,     // last plainly-clicked uid (range anchor)
    traces: [],             // [{sample_id, name, visible, color, x, y}]
    dcTraces: [],           // [{sample_id, name, visible, color, percent, temperature}]
    tableData: { columns: [], rows: [] },
    comparisonStandards: [],
    analysisResult: null,
    analysisQueue: [],      // [{sample_id, lab_id, sample_name, standard_name, added_at, ...}]
    advancedViewsVisible: false,
    correctedD86: false,
    analysisParams: {
        quantile: 0.20, window: 301, sigma: 34.0,
        thresh_marginal: 100, thresh_moderate: 500, thresh_significant: 2000,
        x_max_min: 7.0
    },
    rangeOverlays: [
        { id: 1, label: 'Gas', c_start: 5, c_end: 11, color: '#3fb95044' },
        { id: 2, label: 'Oil', c_start: 20, c_end: 44, color: '#d2992244' },
    ],
    nextRangeId: 3,
    selectedSample: null,
    selectedStandard: null,
};

/* ===================================================================
   2. PLOTLY THEME
   =================================================================== */

function basePlotlyLayout(extra) {
    return Object.assign({
        paper_bgcolor: '#161b22',
        plot_bgcolor: '#0d1117',
        font: { color: '#e6edf3', family: 'Segoe UI' },
        xaxis: { gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
        yaxis: { gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
        margin: { l: 60, r: 20, t: 40, b: 40 },
        showlegend: true,
        legend: { font: { color: '#7d8590' }, bgcolor: 'rgba(0,0,0,0)' },
    }, extra || {});
}

const PLOTLY_CONFIG = { responsive: true, displayModeBar: true, displaylogo: false };

/* ===================================================================
   3. UTILITY FUNCTIONS
   =================================================================== */

function debounce(fn, ms) {
    let timer = null;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), ms);
    };
}

function formatDateTime(iso) {
    if (!iso) return '';
    try {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleString('en-US', {
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
        });
    } catch {
        return iso;
    }
}

// escapeHtml comes from samples.js (escapes quotes too, for attribute contexts).

function seriesColor(index) {
    const hue = (index * 137.508) % 360;
    return `hsl(${hue}, 70%, 60%)`;
}

/** Show a toast notification. type: 'success' | 'error' | 'info' */
function showNotification(msg, type) {
    type = type || 'info';
    const container = document.getElementById('notification-container') || _createNotifContainer();
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = msg;
    container.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add('toast-visible'));
    setTimeout(() => {
        toast.classList.remove('toast-visible');
        toast.addEventListener('transitionend', () => toast.remove());
        // Fallback removal if transitionend never fires
        setTimeout(() => { if (toast.parentNode) toast.remove(); }, 500);
    }, 4000);
}

function _createNotifContainer() {
    const c = document.createElement('div');
    c.id = 'notification-container';
    c.style.cssText = 'position:fixed;top:16px;right:16px;z-index:10000;display:flex;flex-direction:column;gap:8px;pointer-events:none;';
    document.body.appendChild(c);
    return c;
}

/** Linear interpolation: given sorted arrays xs, ys and a target x, return y. */
function linterp(xs, ys, target) {
    if (xs.length === 0) return NaN;
    if (target <= xs[0]) return ys[0];
    if (target >= xs[xs.length - 1]) return ys[ys.length - 1];
    for (let i = 0; i < xs.length - 1; i++) {
        if (target >= xs[i] && target <= xs[i + 1]) {
            const t = (target - xs[i]) / (xs[i + 1] - xs[i]);
            return ys[i] + t * (ys[i + 1] - ys[i]);
        }
    }
    return ys[ys.length - 1];
}

function round2(v) {
    return Math.round(v * 100) / 100;
}

/* ===================================================================
   3b. TREND-LINE SLIDER MAPPING  (-1..+1  <->  real values)
   =================================================================== */

// Each slider maps -1 → min, 0 → default, +1 → max using piecewise linear
const TREND_SLIDER_MAP = {
    baseline:  { min: 0.05, def: 0.20, max: 0.50,  real: 'quantile' },
    detail:    { min: 51,   def: 301,  max: 2001,   real: 'window', invert: true },
    smoothing: { min: 1.0,  def: 34.0, max: 200.0,  real: 'sigma'  },
};

/** Convert normalized slider value (-1..+1) to the real parameter value. */
function sliderToReal(name, norm) {
    const m = TREND_SLIDER_MAP[name];
    const n = m.invert ? -norm : norm;                       // flip direction
    if (n <= 0) return m.def + n * (m.def - m.min);          // lerp min..def
    return m.def + n * (m.max - m.def);                      // lerp def..max
}

/** Convert a real parameter value back to the normalized -1..+1 slider value. */
function realToSlider(name, real) {
    const m = TREND_SLIDER_MAP[name];
    let result;
    if (real <= m.def) {
        result = (m.def === m.min) ? 0 : (real - m.def) / (m.def - m.min);
    } else {
        result = (m.max === m.def) ? 0 : (real - m.def) / (m.max - m.def);
    }
    return m.invert ? -result : result;
}

/** Sync a single trend slider → hidden real input + display label. */
function syncTrendSlider(name) {
    const slider = document.getElementById('param-' + name);
    if (!slider) return;
    const norm = parseFloat(slider.value);
    const real = sliderToReal(name, norm);
    const m = TREND_SLIDER_MAP[name];
    // Update hidden real input (window must be odd)
    let finalReal = real;
    if (name === 'detail') { finalReal = Math.round(real); if (finalReal % 2 === 0) finalReal += 1; }
    else { finalReal = parseFloat(real.toFixed(2)); }
    const hidden = document.getElementById('param-' + m.real);
    if (hidden) hidden.value = finalReal;
    // Update display value
    const valEl = document.getElementById('param-' + name + '-val');
    if (valEl) valEl.textContent = norm >= 0 ? '+' + norm.toFixed(2) : norm.toFixed(2);
    // Sync into state
    state.analysisParams[m.real] = finalReal;
}

/** Set all trend sliders from the current state (real values). */
function populateTrendSliders() {
    for (const [name, m] of Object.entries(TREND_SLIDER_MAP)) {
        const slider = document.getElementById('param-' + name);
        if (!slider) continue;
        const norm = realToSlider(name, state.analysisParams[m.real]);
        slider.value = norm.toFixed(2);
        const valEl = document.getElementById('param-' + name + '-val');
        if (valEl) valEl.textContent = norm >= 0 ? '+' + norm.toFixed(2) : norm.toFixed(2);
    }
}

/** Tooltip for info buttons. */
function initParamTooltips() {
    let tip = null;
    document.querySelectorAll('.param-info-btn[data-tooltip]').forEach(btn => {
        btn.addEventListener('mouseenter', (e) => {
            if (tip) tip.remove();
            tip = document.createElement('div');
            tip.className = 'param-tooltip';
            tip.textContent = btn.dataset.tooltip;
            document.body.appendChild(tip);
            const r = btn.getBoundingClientRect();
            tip.style.left = Math.min(r.left, window.innerWidth - 260) + 'px';
            tip.style.top = (r.bottom + 6) + 'px';
        });
        btn.addEventListener('mouseleave', () => {
            if (tip) { tip.remove(); tip = null; }
        });
    });
}

/* ===================================================================
   4. API HELPERS
   =================================================================== */

async function api(method, url, body, extra) {
    // extra: optional fetch options merged in (e.g. { signal } for a timeout).
    const opts = { ...(extra || {}), method, headers: {} };
    if (body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    if (!resp.ok) {
        const j = (await GCSession.readJson(resp)).body || {};
        throw new Error(j.error || j.message || `API error ${resp.status}`);
    }
    const ct = resp.headers.get('content-type') || '';
    if (!ct.includes('application/json')) return resp;
    const j = (await GCSession.readJson(resp)).body;
    // a body that claims to be JSON but doesn't parse comes back as readJson's {error}
    if (j && typeof j.error === 'string' && j.error.startsWith('The hub answered HTTP ')) {
        throw new Error(j.error);
    }
    return j;
}

async function apiGet(url) { return api('GET', url); }

/** POST that may answer with ``{refused: [{sample_id, error}]}`` (409): the
    thrown Error then names the refused samples. */
async function apiPostRefusable(url, what, body) {
    const resp = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const j = (await GCSession.readJson(resp)).body || {};
    if (!resp.ok) {
        throw new Error((j.refused && j.refused.length)
            ? refusalSummary(what, j.refused, state.files)
            : (j.error || `API error ${resp.status}`));
    }
    return j;
}
async function apiPost(url, body) { return api('POST', url, body); }
async function apiDelete(url) { return api('DELETE', url); }

/** Ask for the admin password for one action (null if cancelled). */
function adminPassword(what) {
    const pw = window.prompt(`Admin password to ${what}:`);
    return pw ? pw : null;
}

/** Download a blob response (for PDF/file exports). */
async function downloadBlob(resp, fallbackName) {
    const blob = await resp.blob();
    const cd = resp.headers.get('content-disposition') || '';
    let filename = fallbackName || 'download';
    const match = cd.match(/filename="?([^";\n]+)"?/);
    if (match) filename = match[1];
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
}

/* ===================================================================
   5. DATA LOADING
   =================================================================== */

async function loadSettings() {
    try {
        state.settings = await apiGet('/api/settings');
        // Sync analysis params from settings
        const s = state.settings;
        const p = state.analysisParams;
        if (s.analysis_quantile) p.quantile = parseFloat(s.analysis_quantile);
        if (s.analysis_window) p.window = parseInt(s.analysis_window);
        if (s.analysis_sigma) p.sigma = parseFloat(s.analysis_sigma);
        if (s.analysis_thresh_marginal) p.thresh_marginal = parseFloat(s.analysis_thresh_marginal);
        if (s.analysis_thresh_moderate) p.thresh_moderate = parseFloat(s.analysis_thresh_moderate);
        if (s.analysis_thresh_significant) p.thresh_significant = parseFloat(s.analysis_thresh_significant);
        if (s.analysis_x_max_min) p.x_max_min = parseFloat(s.analysis_x_max_min);
        // Range overlays from settings, as the server resolves them
        // ("[]" saved = no ranges; nothing saved = the legacy Gas/Oil keys)
        state.rangeOverlays = overlaysFromSettings(s);
        state.nextRangeId = state.rangeOverlays.length + 1;
        // Update UI inputs
        populateAnalysisParamInputs();
    } catch (e) {
        console.error('Failed to load settings:', e);
        showNotification('Failed to load settings: ' + e.message, 'error');
    }
}

/** Load the sample list. Every change to state.files goes through one
    queue (_listQueue), so a slow answer can't overwrite a newer one.
    `bg`: the page asked on its own (a live reset), not the user. */
function loadFiles(opts) {
    return _listQueue(() => _loadFilesNow(opts));
}

async function _loadFilesNow(opts) {
    const bg = !!(opts && opts.bg);
    try {
        // The sample list is a store query (newest first); the lists filter
        // the loaded page client-side as before.
        const url = filesUrl({ instrument: state.listInstrument }, FILES_PAGE_LIMIT);
        const res = await (bg ? apiGetBg(url) : apiGet(url));
        state.files = res.samples || [];
        state.filesTotal = res.total || state.files.length;
        if (res.instruments && res.instruments.length) state.instruments = res.instruments;
        console.log('[GC Viewer] Loaded', state.files.length, 'of', state.filesTotal, 'samples');
        _renderListsKeepingScroll();
        // Flags/best-fit still being computed in the background: refetch once.
        if (res.cache_pending > 0 && !state._filesRefetchPending) {
            state._filesRefetchPending = true;
            setTimeout(() => {
                state._filesRefetchPending = false;
                loadFiles({ bg: true, quiet: true });
            }, 5000);
        }
    } catch (e) {
        console.error('Failed to load files:', e);
        if (!(opts && opts.quiet)) showNotification('Failed to load sample list: ' + e.message, 'error');
    }
}

// The toolbar's instrument filter, remembered per browser. localStorage can
// throw (private windows, blocked storage): the filter then just isn't kept.
const INSTRUMENT_FILTER_KEY = 'gc-hub.listInstrument';

function readSavedInstrumentFilter() {
    try { return window.localStorage.getItem(INSTRUMENT_FILTER_KEY); } catch (_) { return null; }
}

function saveInstrumentFilter(value) {
    try {
        if (value) window.localStorage.setItem(INSTRUMENT_FILTER_KEY, value);
        else window.localStorage.removeItem(INSTRUMENT_FILTER_KEY);
    } catch (_) { /* not remembered */ }
}

/** Instrument names for the badges, and the filter select's options. */
async function loadInstruments() {
    let list = [];
    try {
        const res = await apiGet('/api/instruments');
        list = (res.instruments || []).map(i => ({ id: i.id, name: i.name }));
    } catch (e) {
        console.error('Failed to load instruments:', e);
        list = (state.instruments || []).map(id => ({ id, name: id }));
    }
    if (list.length) state.instruments = list.map(i => i.id);
    state.instrumentNames = Object.fromEntries(list.map(i => [i.id, i.name || i.id]));
    const restored = restoreInstrumentFilter(state.listInstrument, state.instruments);
    if (restored !== state.listInstrument) {
        state.listInstrument = restored;
        saveInstrumentFilter(restored);
        state.searchResult = null;
        await loadFiles();
    }
    renderInstrumentFilter(list);
    renderAllFileLists();
}

function renderInstrumentFilter(list) {
    const sel = document.getElementById('instrument-filter');
    if (!sel) return;
    sel.innerHTML = '';
    for (const opt of instrumentFilterOptions(list)) {
        const o = document.createElement('option');
        o.value = opt.value;
        o.textContent = opt.text;
        sel.appendChild(o);
    }
    sel.value = state.listInstrument || '';
}

async function onInstrumentFilterChange() {
    const sel = document.getElementById('instrument-filter');
    state.listInstrument = (sel && sel.value) || null;
    saveInstrumentFilter(state.listInstrument);
    state.searchResult = null;
    await loadFiles();
    const q = (document.getElementById('universal-search')?.value || '').trim();
    if (needsServerSearch(q, state.filesTotal, state.files.length)) _serverSearch(q);
}

// The Distillation Data tab's table (tab 2). A live change while another
// tab is showing only marks it stale; switching to it reloads it.
const DISTILL_DATA_TAB = 2;
let _tableStale = false;

async function loadTableData(opts) {
    try {
        _tableStale = false;
        state.tableData = await ((opts && opts.bg) ? apiGetBg('/api/table') : apiGet('/api/table'));
        renderDistillTable();
    } catch (e) {
        console.error('Failed to load table data:', e);
        showNotification('Failed to load distillation table: ' + e.message, 'error');
    }
}

async function loadComparisonStandards() {
    try {
        state.comparisonStandards = await apiGet('/api/comparison-standards');
        renderComparisonStandards();
    } catch (e) {
        console.error('Failed to load comparison standards:', e);
    }
}

async function refreshAll() {
    // The sample list is a live store query: just fetch everything again
    // (after a settings change; everyday changes arrive through GCLive).
    await Promise.all([
        loadFiles(),
        loadTableData(),
        loadComparisonStandards(),
    ]);
}

/* ===================================================================
   5b. LIVE UPDATES (static/js/live.js; v3.1)
   New, changed and finalised samples are fetched by id and merged into
   the list in place; the notification badge follows the live count; a
   reset (the hub restarted, or this tab fell too far behind) reloads.
   =================================================================== */

const _LIVE_LISTS = ['dash-file-list', 'chrom-file-list', 'dcurve-file-list', 'analysis-sample-list'];
const LIVE_IDS_PER_FETCH = 1000;
let _liveFirstReset = true;
let _liveUnread = null;
let _listChain = Promise.resolve();

/** Run list work one at a time, in order (loads and live merges). */
function _listQueue(fn) {
    const next = _listChain.then(fn, fn);
    _listChain = next.catch(() => {});
    return next;
}

/** GET for the page's own follow-ups (never activity; GCLive.bgFetch). */
async function apiGetBg(url) {
    const resp = (typeof GCLive !== 'undefined')
        ? await GCLive.bgFetch(url, { headers: { Accept: 'application/json' } })
        : await fetch(url, { headers: { 'X-GC-Background': '1' } });
    const j = (await GCSession.readJson(resp)).body;
    if (!resp.ok) throw new Error((j && (j.error || j.message)) || `API error ${resp.status}`);
    // as api(): a web page instead of data comes back as readJson's {error}
    if (j && typeof j.error === 'string' && j.error.startsWith('The hub answered HTTP ')) throw new Error(j.error);
    return j;
}

/** Re-render the lists without losing each one's scroll position. */
function _renderListsKeepingScroll() {
    const tops = _LIVE_LISTS.map(id => {
        const el = document.getElementById(id);
        return el ? el.scrollTop : 0;
    });
    renderAllFileLists();
    _LIVE_LISTS.forEach((id, i) => {
        const el = document.getElementById(id);
        if (el) el.scrollTop = tops[i];
    });
}

/** Fetch the changed samples (with the list's filter) and merge them in:
    queued behind any load or earlier merge, so answers land in order. */
function applyLiveSamples(ids) {
    return _listQueue(() => _applyLiveSamplesNow(ids));
}

async function _applyLiveSamplesNow(ids) {
    let rows = [];
    for (let i = 0; i < ids.length; i += LIVE_IDS_PER_FETCH) {
        const chunk = ids.slice(i, i + LIVE_IDS_PER_FETCH);
        const res = await apiGetBg(filesUrl({ ids: chunk, instrument: state.listInstrument },
                                            FILES_PAGE_LIMIT));
        rows = rows.concat(res.samples || []);
    }
    const pageFull = state.filesTotal > state.files.length;
    const m = mergeChangedRows(state.files, ids, rows, { pageFull });
    state.files = m.files;
    if (!pageFull) {
        state.filesTotal = state.files.length + m.skipped;
    } else if (m.added || m.removed || m.skipped) {
        // only part of the list is loaded: ask the server for the count
        try {
            const res = await apiGetBg(filesUrl({ instrument: state.listInstrument }, 1));
            state.filesTotal = Math.max(res.total || 0, state.files.length);
        } catch (_) {
            state.filesTotal = Math.max(state.files.length,
                                        state.filesTotal + m.added + m.skipped - m.removed);
        }
    }
    // the selected sample: keep the object current (its badges, status)
    if (state.selectedFile) {
        const sel = rows.find(r => sampleUid(r) === sampleUid(state.selectedFile));
        if (sel) state.selectedFile = sel;
    }
    // a server search on screen is re-asked (it has its own query)
    const q = (document.getElementById('universal-search')?.value || '').trim();
    const searching = state.searchResult && state.searchResult.q === q && q;
    if (searching) _serverSearch(q, { bg: true });
    // only the changed rows' items are redrawn, unless rows came or went
    if (searching || m.orderChanged || !patchFileRows(m.replaced)) _renderListsKeepingScroll();
    else _updateSearchCount();
    if (rows.some(r => r.status === 'final')) _tableChanged();
}

/** Final results changed: reload the table if it is on screen, else later. */
function _tableChanged() {
    if (currentTab === DISTILL_DATA_TAB) _reloadTableSoon();
    else _tableStale = true;
}

const _reloadTableSoon = debounce(() => loadTableData({ bg: true }), 2000);

async function onLiveUpdate(u) {
    try {
        if (u.reset) {
            // The first reset is the start: the page's own load covers it.
            if (!_liveFirstReset) {
                await loadFiles({ bg: true });
                _tableChanged();
            }
            _liveFirstReset = false;
        } else if (u.samples && u.samples.length) {
            await applyLiveSamples(u.samples);
        }
    } catch (e) {
        console.error('Live update failed:', e);
    }
    if (u.notifications_unread !== _liveUnread || (u.kinds || []).includes('notification')) {
        _liveUnread = u.notifications_unread;
        loadNotifications({ bg: true });
    }
    if (u.version_changed) showVersionBanner(u.version);
}

/** The hub was updated while this page was open: offer a reload (never
    forced: the operator may be in the middle of something). */
function showVersionBanner(version) {
    let bar = document.getElementById('version-banner');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'version-banner';
        bar.setAttribute('role', 'status');
        bar.setAttribute('data-testid', 'version-banner');
        const text = document.createElement('span');
        text.id = 'version-banner-text';
        const btn = document.createElement('button');
        btn.className = 'btn btn-secondary';
        btn.textContent = 'Reload';
        btn.addEventListener('click', () => window.location.reload());
        const close = document.createElement('button');
        close.className = 'version-banner-close';
        close.title = 'Dismiss';
        close.textContent = '\u00d7';
        close.addEventListener('click', () => bar.remove());
        bar.append(text, btn, close);
        document.body.appendChild(bar);
    }
    document.getElementById('version-banner-text').textContent =
        `Updated to ${version} \u2014 reload to use it`;
}

function renderLiveIndicator() {
    const el = document.getElementById('live-indicator');
    if (!el || typeof GCLive === 'undefined') return;
    const st = GCLive.status();
    el.textContent = GCLive.statusText(st, Date.now());
    el.classList.toggle('live-offline', !st.connected);
    el.title = st.error ? `Live updates: ${st.error}` : 'Live updates from the hub';
}

/** Start live updates (before the first data load, so nothing is missed). */
function startLive() {
    if (typeof GCLive === 'undefined') {           // live.js missing: the old poll
        setInterval(loadNotifications, 30000);
        return Promise.resolve();
    }
    GCLive.subscribe(onLiveUpdate);
    renderLiveIndicator();
    setInterval(renderLiveIndicator, 1000);
    return GCLive.start();                          // resolves after the first answer
}

/* ===================================================================
   6. D2887 / D86 COMPUTATION (client-side, matching desktop exactly)
   =================================================================== */

/**
 * Given a distillation curve (percent[], temperature[]),
 * interpolate D2887 values at PERCENT_LEVELS.
 * Returns an object keyed by D86_LABELS: {IBP: temp, "5%": temp, ...}
 */
function computeD2887(percent, temperature) {
    const result = {};
    for (let i = 0; i < PERCENT_LEVELS.length; i++) {
        const pct = PERCENT_LEVELS[i];
        const label = D86_LABELS[i];
        result[label] = round2(linterp(percent, temperature, pct));
    }
    return result;
}

/**
 * Convert D2887 dict to D86 using ASTM D86 App. X4 coefficients.
 * 40% and 60% have no equation and will be null.
 */
function convertToD86(d2887) {
    const d86 = {};
    for (const [cut, coeffs] of Object.entries(CONVERSION_COEFF)) {
        const [a0, a1, a2, a3] = coeffs;
        const rels = CONVERSION_REL[cut];
        if (!rels) continue;
        const [pPrev, pCurr, pNext] = rels;
        const tPrev = d2887[pPrev];
        const tCurr = d2887[pCurr];
        const tNext = d2887[pNext];
        if (tPrev == null || tCurr == null || tNext == null) continue;
        // rounded like distill._round2 (Python's round), so the numbers match the hub's
        d86[cut] = DistillView.pyRound2(a0 + a1 * tPrev + a2 * tCurr + a3 * tNext);
    }
    // 40% and 60% have no D86 equation: the midpoints of 30/50 and 50/70, as
    // distill.x4_midpoints does
    d86["40%"] = null;
    d86["60%"] = null;
    return DistillView.x4Midpoints(d86);
}

/* ===================================================================
   7. FILE LIST RENDERING
   =================================================================== */

// Global filter: show only early-signal-flagged samples
let earlySignalFilterActive = false;

/** The lists' client-side filters (search box, instrument, Flagged). */
function _listShows(file) {
    // Universal search — read from the shared search input
    const searchEl = document.getElementById('universal-search');
    const filter = searchEl ? searchEl.value.toLowerCase() : '';
    // The instrument filter (the server already applied it; this also covers
    // a list fetched before the filter changed).
    if (!filterByInstrument([file], state.listInstrument).length) return false;
    if (!((file.name || '').toLowerCase().includes(filter) ||
          (file.display_name || '').toLowerCase().includes(filter))) return false;
    // Apply early-signal filter if active
    return !earlySignalFilterActive || !!file.early_signal;
}

const _LIST_MODES = { 'dash-file-list': 'dashboard', 'chrom-file-list': 'chrom',
                      'dcurve-file-list': 'dc', 'analysis-sample-list': 'analysis' };

function renderFileList(containerId, files, mode) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const filtered = (files || []).filter(_listShows);
    container.innerHTML = '';
    for (const file of filtered) container.appendChild(_fileItem(file, mode));
}

/** v3.1 live updates: replace just these rows' <li> in every list. False
    when that isn't enough (a row must appear where it wasn't): the caller
    then re-renders the lists. */
function patchFileRows(rows) {
    for (const [containerId, mode] of Object.entries(_LIST_MODES)) {
        const container = document.getElementById(containerId);
        if (!container) continue;
        for (const file of rows) {
            const old = container.querySelector(`li[data-uid="${CSS.escape(sampleUid(file))}"]`);
            const shows = _listShows(file);
            if (old && shows) old.replaceWith(_fileItem(file, mode));
            else if (old) old.remove();
            else if (shows) return false;
        }
    }
    return true;
}

/** One sample's list item (click, double-click and context menu wired). */
function _fileItem(file, mode) {
    {
        const item = document.createElement('li');
        item.dataset.sampleId = file.sample_id;
        item.dataset.name = file.name;
        // uid (= the sample id) distinguishes re-runs that share a name, so
        // each injection selects independently.
        item.dataset.uid = sampleUid(file);

        // One colored tag per matched flag rule (early_signal kept as the
        // legacy any-flag bool for styling)
        const fileFlags = file.flags || (file.early_signal
            ? [{ name: 'Early High-Signal', color: '#e67e22' }] : []);
        if (fileFlags.length) {
            item.classList.add('early-signal-flagged');
            for (const flag of fileFlags) {
                const tag = document.createElement('span');
                tag.className = 'early-signal-tag';
                tag.title = flag.name;
                if (flag.color) tag.style.background = flag.color;
                item.appendChild(tag);
            }
        }

        const nameSpan = document.createElement('span');
        nameSpan.className = 'file-item-name';
        nameSpan.textContent = file.display_name || file.name;
        item.appendChild(nameSpan);

        // The sample's instrument: the same lab ID can come from two GCs
        const ib = instrumentBadge(file, state.instrumentNames);
        if (ib) {
            const span = document.createElement('span');
            span.className = ib.cls;
            span.textContent = ib.text;
            span.title = ib.title;
            item.appendChild(span);
        }

        // Injection time corrected from v1's misparsed stamp
        const tcTitle = timeCorrectedTitle(file);
        if (tcTitle) {
            const tc = document.createElement('span');
            tc.className = 'time-corrected-mark';
            tc.textContent = '⏱';
            tc.title = tcTitle;
            item.appendChild(tc);
        }

        // Not-final samples: a status badge whose tooltip is the hold reason
        const badge = statusBadge(file);
        if (badge) {
            const sb = document.createElement('span');
            sb.className = badge.cls;
            sb.textContent = badge.text;
            sb.title = badge.title;
            item.appendChild(sb);
        }

        // Fuel-type best-fit badge
        if (file.best_fit && file.best_fit.label) {
            const bf = document.createElement('span');
            bf.className = 'bestfit-badge';
            bf.textContent = file.best_fit.label;
            bf.title = `Best fit: ${file.best_fit.label} (score ${Number(file.best_fit.score).toFixed(3)})`;
            item.appendChild(bf);
        }

        // Highlight if currently selected (universal selection). Compare by
        // uid so one re-run highlights without lighting up its siblings.
        const fileUid = sampleUid(file);
        const selUid = state.selectedFile && sampleUid(state.selectedFile);
        if (selUid && selUid === fileUid) {
            item.classList.add('selected');
        }
        // Multi-selection highlight (shift / ctrl-cmd), distinct from the single
        // active selection above.
        if (state.selectedUids && state.selectedUids.has(fileUid)) {
            item.classList.add('selected-multi');
        }

        // Click handler (ev passed through for shift/ctrl-cmd multi-select)
        item.addEventListener('click', (ev) => onFileClick(file, mode, item, ev));

        // Double-click handlers
        if (mode === 'chrom') {
            item.addEventListener('dblclick', () => addChromatogramTrace(file));
        } else if (mode === 'dc') {
            item.addEventListener('dblclick', () => addDCTrace(file));
        } else if (mode === 'analysis') {
            item.addEventListener('dblclick', () => onFileDblClick(file, mode, item));
        }

        // Right-click context menu for setting as comparison standard
        item.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            showContextMenu(e, file);
        });

        return item;
    }
}

function renderAllFileLists() {
    // A server search result replaces the loaded page while its query is in
    // the search box (the page holds only the newest FILES_PAGE_LIMIT).
    const q = (document.getElementById('universal-search')?.value || '').trim();
    const sr = state.searchResult;
    const useSearch = sr && sr.q === q && q !== '';
    const files = useSearch ? sr.samples : state.files;
    renderFileList('dash-file-list', files, 'dashboard');
    renderFileList('chrom-file-list', files, 'chrom');
    renderFileList('dcurve-file-list', files, 'dc');
    renderFileList('analysis-sample-list', files, 'analysis');
    _updateSearchCount();
}

function _updateSearchCount() {
    const q = (document.getElementById('universal-search')?.value || '').trim();
    const sr = state.searchResult;
    const useSearch = sr && sr.q === q && q !== '';
    const countEl = document.getElementById('search-count');
    if (countEl) {
        countEl.textContent = useSearch ? countLabel(sr.samples.length, sr.total)
                                        : countLabel(state.files.length, state.filesTotal);
    }
}

/** Search box changed: filter the loaded page at once and, when the server
    holds more samples than were loaded, ask it (debounced) and show that. */
let _searchSeq = 0;
const _serverSearch = debounce(async (q, opts) => {
    const seq = ++_searchSeq;
    try {
        const url = filesUrl({ q, instrument: state.listInstrument, status: state.listStatus },
                             FILES_PAGE_LIMIT);
        const res = await ((opts && opts.bg) ? apiGetBg(url) : apiGet(url));
        if (seq !== _searchSeq) return;                 // a newer search is under way
        state.searchResult = { q, samples: res.samples || [], total: res.total || 0 };
        renderAllFileLists();
    } catch (e) {
        console.error('Search failed:', e);
    }
}, 300);

function onSearchInput() {
    const q = (document.getElementById('universal-search')?.value || '').trim();
    if (state.searchResult && state.searchResult.q !== q) state.searchResult = null;
    renderAllFileLists();
    if (needsServerSearch(q, state.filesTotal, state.files.length)) _serverSearch(q);
}

function toggleEarlySignalFilter() {
    earlySignalFilterActive = !earlySignalFilterActive;
    const btn = document.getElementById('btn-early-signal-filter');
    if (btn) btn.classList.toggle('active', earlySignalFilterActive);
    renderAllFileLists();
}

/** The uids of the list that was clicked, in current visual order. */
function orderedUidsForContainer(containerEl) {
    return Array.from(containerEl.querySelectorAll('li[data-uid]'))
        .map(li => li.dataset.uid);
}

function onFileClick(file, mode, itemEl, ev) {
    const uid = sampleUid(file);
    const container = itemEl.parentElement;

    // Ctrl/Cmd-click — toggle one item in the multi-selection; no load action.
    if (ev && (ev.metaKey || ev.ctrlKey)) {
        ev.preventDefault();
        if (state.selectedUids.has(uid)) state.selectedUids.delete(uid);
        else state.selectedUids.add(uid);
        state.selectionAnchor = uid;
        renderAllFileLists();
        return;
    }

    // Shift-click — contiguous range from the anchor; no load action.
    if (ev && ev.shiftKey && state.selectionAnchor) {
        ev.preventDefault();
        const ordered = orderedUidsForContainer(container);
        const range = computeRangeSelection(ordered, state.selectionAnchor, uid);
        state.selectedUids = new Set(range);
        renderAllFileLists();
        return;
    }

    // Plain click — single active selection (existing behaviour).
    state.selectedUids = new Set([uid]);
    state.selectionAnchor = uid;
    state.selectedFile = file;
    state.selectedSample = file;

    // Update analysis sample label
    const label = document.getElementById('analysis-sample-label');
    if (label) label.textContent = file.name;

    // Re-render all file lists to sync selection highlighting
    renderAllFileLists();

    // Mode-specific actions
    if (mode === 'dashboard') {
        loadDashboardData(file);
    } else if (mode === 'analysis') {
        _clearQueueSelection();
        state.standardPinned = false;  // new sample → allow best-fit auto-select
        autoSelectBestFitStandard(file);
        updateAnalysisOverlay();
        maybeAutoRunAnalysis();
    }
    // For chrom/dc modes, single-click just selects (no load action)
}

function onFileDblClick(file, mode, itemEl) {
    if (mode === 'analysis') {
        // Double click = confirmed — run analysis
        state.selectedFile = file;
        state.selectedSample = file;
        const label = document.getElementById('analysis-sample-label');
        if (label) label.textContent = file.name;
        renderAllFileLists();
        _clearQueueSelection();
        state.standardPinned = false;
        autoSelectBestFitStandard(file);
        updateAnalysisOverlay();
        maybeAutoRunAnalysis();
    }
}

function _clearQueueSelection() {
    const qList = document.getElementById('analysis-queue-list');
    if (qList) qList.querySelectorAll('.queue-item.active').forEach(el => el.classList.remove('active'));
    // Exit editing mode
    state._editingQueueIdx = null;
    _updateQueueButton(false);
}

/* ===================================================================
   8. CONTEXT MENU (right-click → Set as comparison standard)
   =================================================================== */

function showContextMenu(e, file) {
    removeContextMenu();
    // Use the static #context-menu element from HTML
    const menu = document.getElementById('context-menu');
    if (!menu) return;
    menu.style.left = e.pageX + 'px';
    menu.style.top = e.pageY + 'px';
    menu.classList.add('open');

    // Wire up the "Set as comparison standard" item
    const setStdItem = document.getElementById('ctx-set-standard');
    const reprocItem = document.getElementById('ctx-reprocess');
    const renameItem = document.getElementById('ctx-rename-standard');
    const removeItem = document.getElementById('ctx-remove-standard');

    // Hide rename/remove (those are for standards list)
    if (renameItem) renameItem.style.display = 'none';
    if (removeItem) removeItem.style.display = 'none';

    // Wire up the "Reprocess sample" item
    if (reprocItem) {
        reprocItem.style.display = '';
        const newReproc = reprocItem.cloneNode(true);
        reprocItem.replaceWith(newReproc);
        newReproc.id = 'ctx-reprocess';
        const reCount = (state.selectedUids && state.selectedUids.size > 1)
            ? state.selectedUids.size : 0;
        newReproc.textContent = reCount > 1 ? `Reprocess ${reCount} selected`
                                            : 'Reprocess sample';
        newReproc.addEventListener('click', async () => {
            removeContextMenu();
            // Batch-aware: act on the whole multi-selection, else the clicked
            // one. Samples are addressed by id, so re-runs that share a Lab ID
            // reprocess exactly the injection the user selected.
            const files = selectionFilesOr(state.files, state.selectedUids, file);
            const sample_ids = sampleIdsOf(files);
            if (!sample_ids.length) {
                showNotification('No sample selected', 'error');
                return;
            }
            try {
                const result = await apiPost('/api/reprocess', { sample_ids });
                if (result.refused && result.refused.length) {
                    showNotification(refusalSummary('Not reprocessed', result.refused, state.files), 'error');
                }
                _showReprocessToast(result.count, 0);
                _pollReprocessStatus(result.sample_ids || []);
            } catch (err) {
                showNotification('Reprocess failed: ' + err.message, 'error');
            }
        });
    }
    if (setStdItem) {
        setStdItem.style.display = '';
        const newSetStd = setStdItem.cloneNode(true);
        setStdItem.replaceWith(newSetStd);
        newSetStd.id = 'ctx-set-standard';
        newSetStd.addEventListener('click', async () => {
            removeContextMenu();
            const name = prompt('Enter a name for this comparison standard:', file.name.replace(/\.CDF$/i, ''));
            if (!name) return;
            try {
                const password = adminPassword('save a comparison standard');
                if (!password) return;
                await apiPost('/api/comparison-standard', { sample_id: file.sample_id, name, password });
                showNotification(`Saved comparison standard: ${name}`, 'success');
                await loadComparisonStandards();
            } catch (err) {
                showNotification('Failed to save standard: ' + err.message, 'error');
            }
        });
    }

    // Wire up "Export to LIMS" — re-send the selected sample(s)' results through
    // the existing CSV upsert tunnel. Batch-aware via the multi-selection.
    const limsItem = document.getElementById('ctx-export-lims');
    if (limsItem) {
        limsItem.style.display = '';
        const newLims = limsItem.cloneNode(true);
        limsItem.replaceWith(newLims);
        newLims.id = 'ctx-export-lims';
        const limsCount = (state.selectedUids && state.selectedUids.size > 1)
            ? state.selectedUids.size : 0;
        newLims.textContent = limsCount > 1 ? `Export ${limsCount} to LIMS`
                                            : 'Export to LIMS';
        newLims.addEventListener('click', async () => {
            removeContextMenu();
            // Export re-sends each sample's current result (a new revision
            // and export row; nothing is recomputed). The server refuses any
            // sample that isn't final, or is unreleased backfill.
            const files = selectionFilesOr(state.files, state.selectedUids, file);
            const sample_ids = sampleIdsOf(files);
            if (!sample_ids.length) {
                showNotification('No sample selected', 'error');
                return;
            }
            try {
                const resp = await fetch('/api/export-lims', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ sample_ids }),
                });
                const result = (await GCSession.readJson(resp)).body || {};
                if (!resp.ok && !(result.refused && result.refused.length)) {
                    throw new Error(result.error || `API error ${resp.status}`);
                }
                const exported = (result.exported || []).length;
                if (exported) showNotification(`Exported ${exported} sample(s) to LIMS`, 'success');
                if (result.refused && result.refused.length) {
                    showNotification(refusalSummary('Not exported', result.refused, state.files), 'error');
                }
            } catch (err) {
                showNotification('Export to LIMS failed: ' + err.message, 'error');
            }
        });
    }

    // "Copy link" (static/js/deeplink.js): <hub_url>/samples/<id>
    if (typeof DeepLink !== 'undefined') DeepLink.wireContextItem(file);

    // Remove on click elsewhere
    const handler = (ev) => {
        if (!menu.contains(ev.target)) {
            removeContextMenu();
            document.removeEventListener('click', handler, true);
        }
    };
    setTimeout(() => document.addEventListener('click', handler, true), 0);
}

function removeContextMenu() {
    const menu = document.getElementById('context-menu');
    if (menu) menu.classList.remove('open');
}

/* ===================================================================
   9. DASHBOARD
   =================================================================== */

/** The dashboard's selected-sample line: its instrument and any review note
    (a late blank, an unverifiable import match). textContent only. */
function renderDashSampleMeta(file) {
    const el = document.getElementById('dash-sample-meta');
    if (!el) return;
    el.textContent = '';
    if (!file) return;
    const ib = instrumentBadge(file, state.instrumentNames);
    if (ib) {
        const span = document.createElement('span');
        span.className = ib.cls;
        span.textContent = ib.text;
        span.title = ib.title;
        el.appendChild(span);
    }
    const note = reviewNoteTitle(file);
    if (note) {
        const span = document.createElement('span');
        span.className = 'review-note';
        span.textContent = note;
        span.title = note;
        el.appendChild(span);
    }
}

async function loadDashboardData(file) {
    const chromDiv = document.getElementById('dash-chrom-plot');
    const dcDiv = document.getElementById('dash-distill-plot');
    renderDashSampleMeta(file);

    // Show loading spinners on all 4 dashboard quadrants
    document.querySelectorAll('#dash-grid .panel').forEach(p => _setLoading(p, true));

    try {
        // Fetch both in parallel, but keep them independent: a failure in the
        // distillation curve must NOT blank the chromatogram (and vice versa).
        const [traceRes, dcRes] = await Promise.allSettled([
            apiGet(`/api/samples/${file.sample_id}/trace`),
            // A sample with no revision (held) has no curve: don't ask.
            curveFetchable(file)
                ? apiGet(`/api/samples/${file.sample_id}/distillation-curve`)
                : Promise.reject(new Error('no result yet')),
        ]);

        // -- Chromatogram plot (renders even if the distillation curve failed) --
        if (traceRes.status === 'rejected') {
            console.error('Chromatogram load error:', traceRes.reason);
            showNotification('Failed to load chromatogram: ' + (traceRes.reason?.message || traceRes.reason), 'error');
        } else {
        const traceData = traceRes.value;
        const chromTraces = [{
            x: traceData.x,
            y: traceData.y,
            type: 'scatter',
            mode: 'lines',
            name: traceLabel(file, state.instrumentNames),
            line: { color: '#58a6ff', width: 1.5 },
        }];
        // Carbon markers: the sample revision's own ladder (served with its
        // trace), so a gc2 sample is labelled with gc2's calibration
        const markers = carbonMarkers(traceData.cal_times, traceData.cal_carbons);
        Plotly.react(chromDiv, chromTraces, basePlotlyLayout({
            title: { text: traceLabel(file, state.instrumentNames), font: { size: 14 } },
            xaxis: { title: 'Time (min)', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
            yaxis: { title: 'Intensity', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
            shapes: markers.shapes,
            annotations: markers.annotations,
        }), PLOTLY_CONFIG);
        }   // end chromatogram block

        // -- Distillation Curve plot (independent of the chromatogram) --
        const holdBadge = statusBadge(file);
        if (dcRes.status === 'rejected' && !curveFetchable(file)) {
            // Not processed (held): no result to show — say why instead.
            Plotly.purge(dcDiv);
            populateDashboardTables({}, {}, dcDiv);
            if (holdBadge) showNotification(`${file.name}: ${holdBadge.text} — ${holdBadge.title}`, 'info');
        } else if (dcRes.status === 'rejected') {
            console.error('Distillation curve load error:', dcRes.reason);
            showNotification('Distillation curve failed: ' + (dcRes.reason?.message || dcRes.reason), 'error');
        } else {
        const dcData = dcRes.value;
        // The numbers are the sample's stored revision (computed with its
        // recorded blank, calibration and corrections), never recomputed
        // here. Client-side computation is only a fallback for a revision
        // that holds none.
        let d2887, d86, d86Notes = null;
        const csvD2887 = dcData.d2887 || {};
        const csvD86   = dcData.d86   || {};
        const hasCSV = Object.keys(csvD2887).length > 0;

        if (hasCSV) {
            // Map CSV column names ("2887 IBP") → D86_LABELS keys ("IBP")
            d2887 = {};
            const d2887Map = {"2887 IBP":"IBP","2887 T5":"5%","2887 T10":"10%","2887 T20":"20%","2887 T30":"30%","2887 T40":"40%","2887 T50":"50%","2887 T60":"60%","2887 T70":"70%","2887 T80":"80%","2887 T90":"90%","2887 T95":"95%","2887 FBP":"FBP"};
            for (const [csvKey, label] of Object.entries(d2887Map)) {
                d2887[label] = csvD2887[csvKey] != null ? round2(csvD2887[csvKey]) : null;
            }
            // "Corrected D86" on: the stored (corrected) cells; off: the stored
            // uncorrected conversion (or, for a revision that has none, the X4
            // conversion of its stored D2887). 40%/60% whenever they exist.
            const view = DistillView.dashboardD86(
                { d86: csvD86, d86_uncorrected: dcData.d86_uncorrected, d2887: csvD2887 },
                state.correctedD86, convertToD86);
            d86 = view.values;
            d86Notes = view.notes;       // tooltips: why a cell is empty; 40%/60% midpoints
        } else {
            // Fallback: compute client-side (no blank, no EQM corrections)
            d2887 = computeD2887(dcData.percent, dcData.temperature);
            d86 = convertToD86(d2887);
        }

        // D2887 scatter points at recovery percentages
        const scatterX = [];
        const scatterY = [];
        for (let i = 0; i < PERCENT_LEVELS.length; i++) {
            const v = d2887[D86_LABELS[i]];
            if (v != null) {
                scatterX.push(PERCENT_LEVELS[i]);
                scatterY.push(v);
            }
        }

        const dcTraces = [
            {
                x: dcData.percent, y: dcData.temperature,
                type: 'scatter', mode: 'lines',
                name: 'Distillation Curve',
                line: { color: '#3fb950', width: 2 },
            },
            {
                x: scatterX, y: scatterY,
                type: 'scatter', mode: 'markers',
                name: 'D2887 Recovery Points',
                marker: { color: '#3fb950', size: 8, line: { color: '#e6edf3', width: 1.5 } },
            },
        ];
        Plotly.react(dcDiv, dcTraces, basePlotlyLayout({
            title: { text: 'Distillation Curve — ' + traceLabel(file, state.instrumentNames), font: { size: 14 } },
            xaxis: { title: 'Recovery (%)', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
            yaxis: { title: 'Temperature (\u00B0C)', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
        }), PLOTLY_CONFIG);

        // -- Populate D2887 and D86 tables --
        populateDashboardTables(d2887, d86, dcDiv, d86Notes);
        }   // end distillation curve block

    } catch (e) {
        console.error('Dashboard load error:', e);
        showNotification('Failed to load dashboard data: ' + e.message, 'error');
    } finally {
        document.querySelectorAll('#dash-grid .panel').forEach(p => _setLoading(p, false));
    }
}

function populateDashboardTables(d2887, d86, dcDiv, d86Notes) {
    // D2887 Table — target the <tbody> inside the table
    const d2887Table = document.getElementById('dash-d2887-table');
    const d2887Body = d2887Table ? (d2887Table.querySelector('tbody') || d2887Table) : null;
    if (d2887Body) {
        d2887Body.innerHTML = '';
        for (let i = 0; i < D86_LABELS.length; i++) {
            const label = D86_LABELS[i];
            const temp = d2887[label];
            const tr = document.createElement('tr');
            tr.innerHTML = `<td>${escapeHtml(label)}</td><td>${temp != null ? temp.toFixed(2) : '\u2014'}</td>`;
            tr.style.cursor = 'pointer';
            tr.addEventListener('click', () => highlightDCPoint(dcDiv, PERCENT_LEVELS[i], temp, '#bc8cff'));
            d2887Body.appendChild(tr);
        }
    }

    // D86 Table — target the <tbody> inside the table
    const d86Table = document.getElementById('dash-d86-table');
    const d86Body = d86Table ? (d86Table.querySelector('tbody') || d86Table) : null;
    if (d86Body) {
        d86Body.innerHTML = '';
        for (let i = 0; i < D86_LABELS.length; i++) {
            const label = D86_LABELS[i];
            const temp = d86[label];
            const tr = document.createElement('tr');
            // 40% and 60% (no X4 equation) show the stored value when there is one
            tr.innerHTML = `<td>${escapeHtml(label)}</td><td>${temp != null ? temp.toFixed(2) : '\u2014'}</td>`;
            const note = (d86Notes && d86Notes[label])
                || (temp == null ? DistillView.missingNote(label) : null);
            if (note) tr.title = note;
            if (temp != null) {
                tr.style.cursor = 'pointer';
                tr.addEventListener('click', () => highlightDCPoint(dcDiv, PERCENT_LEVELS[i], temp, '#d29922'));
            }
            d86Body.appendChild(tr);
        }
    }
}

/**
 * Highlight a point on the distillation curve chart.
 * Adds (or updates) a special highlight trace.
 */
function highlightDCPoint(dcDiv, pct, temp, ringColor) {
    if (!dcDiv || temp == null) return;
    const highlightTrace = {
        x: [pct], y: [temp],
        type: 'scatter', mode: 'markers',
        name: 'Highlight',
        marker: {
            color: '#ffffff', size: 14,
            line: { color: ringColor, width: 3 },
        },
        showlegend: false,
        hoverinfo: 'text',
        text: [`${pct}% : ${temp.toFixed(2)} \u00B0C`],
    };

    // Check if a highlight trace already exists (last trace named 'Highlight')
    const data = dcDiv.data || [];
    const highlightIdx = data.findIndex(t => t.name === 'Highlight');
    if (highlightIdx >= 0) {
        Plotly.deleteTraces(dcDiv, highlightIdx);
    }
    Plotly.addTraces(dcDiv, highlightTrace);
}

/* ===================================================================
   10. CHROMATOGRAM TAB
   =================================================================== */

async function addChromatogramTrace(file) {
    // Check if already added
    if (state.traces.find(t => t.sample_id === file.sample_id)) {
        showNotification('Trace already on chart', 'info');
        return;
    }
    try {
        const data = await apiGet(`/api/samples/${file.sample_id}/trace`);
        const color = seriesColor(state.traces.length);
        state.traces.push({
            sample_id: file.sample_id,
            name: traceLabel(file, state.instrumentNames),
            visible: true,
            color,
            x: data.x,
            y: data.y,
            cal_times: data.cal_times || [],       // this sample's own ladder
            cal_carbons: data.cal_carbons || [],
        });
        renderChromatogramChart();
        renderTraceList();
    } catch (e) {
        showNotification('Failed to load trace: ' + e.message, 'error');
    }
}

function renderChromatogramChart() {
    const div = document.getElementById('chrom-plot');
    if (!div) return;

    const traces = state.traces.filter(t => t.visible).map(t => ({
        x: t.x, y: t.y, type: 'scatter', mode: 'lines',
        name: t.name, line: { color: t.color, width: 1.5 },
    }));

    // Carbon markers: the first visible trace's own ladder; when traces from
    // another calibration (e.g. gc1 next to gc2) are shown, the title says
    // whose markers these are
    const lad = overlayLadder(state.traces);
    const markers = carbonMarkers(lad.times, lad.carbons);
    const title = lad.differs
        ? `Chromatogram Overlay (carbon markers: ${lad.owner})`
        : 'Chromatogram Overlay';

    Plotly.react(div, traces, basePlotlyLayout({
        title: { text: title, font: { size: 14 } },
        xaxis: { title: 'Time (min)', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
        yaxis: { title: 'Intensity', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
        shapes: markers.shapes,
        annotations: markers.annotations,
    }), PLOTLY_CONFIG);
}

function renderTraceList() {
    const container = document.getElementById('chrom-trace-bar');
    if (!container) return;
    container.innerHTML = '';

    state.traces.forEach((trace, idx) => {
        const row = document.createElement('div');
        row.className = 'trace-list-item';

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = trace.visible;
        checkbox.addEventListener('change', () => {
            state.traces[idx].visible = checkbox.checked;
            renderChromatogramChart();
        });

        const swatch = document.createElement('span');
        swatch.className = 'color-swatch';
        swatch.style.backgroundColor = trace.color;

        const label = document.createElement('span');
        label.className = 'trace-label';
        label.textContent = trace.name;

        const removeBtn = document.createElement('button');
        removeBtn.className = 'btn-icon btn-remove-trace';
        removeBtn.textContent = '\u00D7';
        removeBtn.title = 'Remove trace';
        removeBtn.addEventListener('click', () => {
            state.traces.splice(idx, 1);
            renderChromatogramChart();
            renderTraceList();
        });

        row.appendChild(checkbox);
        row.appendChild(swatch);
        row.appendChild(label);
        row.appendChild(removeBtn);
        container.appendChild(row);
    });
}

function clearChromatogramTraces() {
    state.traces = [];
    renderChromatogramChart();
    renderTraceList();
}

/* ===================================================================
   11. DISTILLATION DATA TAB (Full CSV Table)
   =================================================================== */

// Column color groups
const COL_GROUP_META = ['Lab ID', 'InjectionDateTime'];
const COL_COLOR_META = '#58a6ff';
const COL_COLOR_D86 = '#d29922';
const COL_COLOR_D2887 = '#bc8cff';

let tableSortCol = -1;
let tableSortAsc = true;
let tableSearchFilter = '';

function columnColor(colName) {
    if (!colName) return COL_COLOR_META;
    colName = String(colName);
    if (COL_GROUP_META.includes(colName)) return COL_COLOR_META;
    if (colName.startsWith('D86') || colName.includes('D86')) return COL_COLOR_D86;
    if (colName.startsWith('2887') || colName.includes('2887')) return COL_COLOR_D2887;
    return COL_COLOR_META;
}

function renderDistillTable() {
    const wrapEl = document.getElementById('distill-table-wrap');
    if (!wrapEl) return;

    const { columns, rows } = state.tableData;
    if (!columns || columns.length === 0) {
        const body = wrapEl.querySelector('.panel-body') || wrapEl;
        body.innerHTML = '<p style="color:#7d8590;padding:1em;">No distillation data available.</p>';
        return;
    }

    // Filter rows
    let filteredRows = rows;
    if (tableSearchFilter) {
        const q = tableSearchFilter.toLowerCase();
        filteredRows = rows.filter(row =>
            row.some(cell => String(cell).toLowerCase().includes(q))
        );
    }

    // Sort
    let sortedRows = [...filteredRows];
    if (tableSortCol >= 0 && tableSortCol < columns.length) {
        sortedRows.sort((a, b) => {
            let va = a[tableSortCol], vb = b[tableSortCol];
            const na = parseFloat(va), nb = parseFloat(vb);
            if (!isNaN(na) && !isNaN(nb)) {
                return tableSortAsc ? na - nb : nb - na;
            }
            va = String(va || ''); vb = String(vb || '');
            return tableSortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
        });
    }

    // The header is built from the same `columns` as the rows (CSV order), so
    // every header sits over its own value; the sorted column gets an arrow.
    const tableEl = document.getElementById('distill-table');
    if (!tableEl) return;
    let thead = tableEl.querySelector('thead');
    if (!thead) { thead = document.createElement('thead'); tableEl.prepend(thead); }
    const headRow = document.createElement('tr');
    DistillView.tableHeader(columns).forEach((cell, ci) => {
        const th = document.createElement('th');
        if (cell.cls) th.className = cell.cls;
        th.dataset.col = cell.col;
        th.textContent = DistillView.headerText(cell, ci, tableSortCol, tableSortAsc);
        headRow.appendChild(th);
    });
    thead.replaceChildren(headRow);

    // Build tbody rows
    const tbody = document.getElementById('distill-table-body') || tableEl.querySelector('tbody');
    if (!tbody) return;
    tbody.innerHTML = '';

    // When the correction toggle is off, substitute uncorrected D86 values
    // computed from D2887 (which is never corrected) into each row.
    const displayRows = (!state.correctedD86) ? sortedRows.map(row => {
        const d2887ColMap = {
            "IBP": columns.indexOf("2887 IBP"), "5%":  columns.indexOf("2887 T5"),
            "10%": columns.indexOf("2887 T10"),  "20%": columns.indexOf("2887 T20"),
            "30%": columns.indexOf("2887 T30"),  "50%": columns.indexOf("2887 T50"),
            "70%": columns.indexOf("2887 T70"),  "80%": columns.indexOf("2887 T80"),
            "90%": columns.indexOf("2887 T90"),  "95%": columns.indexOf("2887 T95"),
            "FBP": columns.indexOf("2887 FBP"),
        };
        const d86ColMap = {
            "IBP": columns.indexOf("D86 IBP"),  "5%":  columns.indexOf("D86 T5"),
            "10%": columns.indexOf("D86 T10"),   "20%": columns.indexOf("D86 T20"),
            "30%": columns.indexOf("D86 T30"),   "50%": columns.indexOf("D86 T50"),
            "70%": columns.indexOf("D86 T70"),   "80%": columns.indexOf("D86 T80"),
            "90%": columns.indexOf("D86 T90"),   "95%": columns.indexOf("D86 T95"),
            "FBP": columns.indexOf("D86 FBP"),
        };
        const d2887 = {};
        for (const [label, ci] of Object.entries(d2887ColMap)) {
            if (ci >= 0) { const v = parseFloat(row[ci]); d2887[label] = isNaN(v) ? null : v; }
        }
        const uncorrected = convertToD86(d2887);
        const newRow = [...row];
        for (const [label, ci] of Object.entries(d86ColMap)) {
            if (ci >= 0 && uncorrected[label] != null) newRow[ci] = uncorrected[label];
        }
        return newRow;
    }) : sortedRows;

    if (displayRows.length === 0) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td colspan="${columns.length}" style="color:#7d8590; text-align:center; padding:12px;">No matching rows</td>`;
        tbody.appendChild(tr);
        return;
    }

    // Each row's sample id (sample_ids runs parallel to rows): a /data link
    // marks its sample's row (static/js/deeplink.js).
    const idOfRow = new Map(rows.map((r, i) => [r, (state.tableData.sample_ids || [])[i]]));
    displayRows.forEach((row, ri) => {
        const tr = document.createElement('tr');
        const sid = idOfRow.get(sortedRows[ri]);
        if (sid != null) tr.dataset.sampleId = sid;
        if (sid != null && sid === state.linkedTableSampleId) tr.classList.add('linked-row');
        row.forEach((cell, ci) => {
            const td = document.createElement('td');
            const cls = DistillView.columnClass(columns[ci]);   // the same group as its header
            if (cls) td.className = cls;
            td.textContent = cell != null ? String(cell) : '';
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });

    // Attach sort handlers on header
    if (thead) {
        thead.querySelectorAll('th').forEach((th, ci) => {
            th.style.cursor = 'pointer';
            // Replace handler to avoid stacking
            th.onclick = () => {
                if (tableSortCol === ci) {
                    tableSortAsc = !tableSortAsc;
                } else {
                    tableSortCol = ci;
                    tableSortAsc = true;
                }
                renderDistillTable();
            };
        });
    }
}

/* ===================================================================
   12. DISTILLATION CURVE TAB
   =================================================================== */

async function addDCTrace(file) {
    if (state.dcTraces.find(t => t.sample_id === file.sample_id)) {
        showNotification('Curve already on chart', 'info');
        return;
    }
    if (!curveFetchable(file)) {
        const b = statusBadge(file);
        showNotification(`${file.name} has no result yet` + (b ? ` (${b.text}: ${b.title})` : ''), 'info');
        return;
    }
    try {
        const data = await apiGet(`/api/samples/${file.sample_id}/distillation-curve`);
        const color = seriesColor(state.dcTraces.length);
        state.dcTraces.push({
            sample_id: file.sample_id,
            name: traceLabel(file, state.instrumentNames),
            visible: true,
            color,
            percent: data.percent,
            temperature: data.temperature,
        });
        renderDCChart();
        renderDCTraceList();
    } catch (e) {
        showNotification('Failed to load distillation curve: ' + e.message, 'error');
    }
}

function renderDCChart() {
    const div = document.getElementById('dcurve-plot');
    if (!div) return;

    const traces = state.dcTraces.filter(t => t.visible).map(t => ({
        x: t.percent, y: t.temperature, type: 'scatter', mode: 'lines',
        name: t.name, line: { color: t.color, width: 2 },
    }));

    Plotly.react(div, traces, basePlotlyLayout({
        title: { text: 'Distillation Curve Overlay', font: { size: 14 } },
        xaxis: { title: 'Recovery (%)', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
        yaxis: { title: 'Temperature (\u00B0C)', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
    }), PLOTLY_CONFIG);
}

function renderDCTraceList() {
    const container = document.getElementById('dcurve-trace-bar');
    if (!container) return;
    container.innerHTML = '';

    state.dcTraces.forEach((trace, idx) => {
        const row = document.createElement('div');
        row.className = 'trace-list-item';

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = trace.visible;
        checkbox.addEventListener('change', () => {
            state.dcTraces[idx].visible = checkbox.checked;
            renderDCChart();
        });

        const swatch = document.createElement('span');
        swatch.className = 'color-swatch';
        swatch.style.backgroundColor = trace.color;

        const label = document.createElement('span');
        label.className = 'trace-label';
        label.textContent = trace.name;

        const removeBtn = document.createElement('button');
        removeBtn.className = 'btn-icon btn-remove-trace';
        removeBtn.textContent = '\u00D7';
        removeBtn.title = 'Remove curve';
        removeBtn.addEventListener('click', () => {
            state.dcTraces.splice(idx, 1);
            renderDCChart();
            renderDCTraceList();
        });

        row.appendChild(checkbox);
        row.appendChild(swatch);
        row.appendChild(label);
        row.appendChild(removeBtn);
        container.appendChild(row);
    });
}

function clearDCTraces() {
    state.dcTraces = [];
    renderDCChart();
    renderDCTraceList();
}

/* ===================================================================
   13. ANALYSIS TAB
   =================================================================== */

function renderComparisonStandards() {
    const container = document.getElementById('analysis-standards-list');
    if (!container) return;
    container.innerHTML = '';

    if (state.comparisonStandards.length === 0) {
        container.innerHTML = '<li style="color:#7d8590; padding:6px 8px; font-size:11px;">No standards — right-click a sample to add one</li>';
        return;
    }

    for (const std of state.comparisonStandards) {
        const item = document.createElement('li');
        if (state.selectedStandard && state.selectedStandard.name === std.name) {
            item.classList.add('selected');
        }
        item.textContent = std.name;

        item.addEventListener('click', () => {
            // Show loading state
            container.querySelectorAll('li.selected').forEach(el => el.classList.remove('selected'));
            item.classList.add('selected');
            item.style.opacity = '0.6';
            item.textContent = std.name + ' (loading...)';

            state.selectedStandard = std;
            state.standardPinned = true;  // manual choice — stop auto-select
            updateAnalysisOverlay();
            maybeAutoRunAnalysis();

            // Restore after a tick (analysis will re-render)
            setTimeout(() => {
                item.textContent = std.name;
                item.style.opacity = '';
            }, 500);
        });

        // Right-click to delete
        item.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            showStandardContextMenu(e, std);
        });

        container.appendChild(item);
    }
}

/* ── Fuel-type best fit (Analysis tab) ─────────────────────────────── */

/** Fetch the best-fit classification for *file*; show the ranking panel and
    auto-select the winning standard unless the operator picked one manually
    (state.standardPinned). */
async function autoSelectBestFitStandard(file) {
    const panel = document.getElementById('analysis-bestfit-panel');
    if ((state.settings.bestfit_enabled || 'true').toLowerCase() !== 'true') {
        if (panel) panel.innerHTML = '';
        return;
    }
    if (panel) panel.innerHTML = '<span style="color:#7d8590;">Best fit: computing…</span>';
    try {
        const res = await apiPost('/api/best-fit', { sample_id: file.sample_id });
        // Ignore stale responses after the user clicked another sample
        if (!state.selectedSample || state.selectedSample.sample_id !== file.sample_id) return;
        state.bestFit = res;
        renderBestFitPanel(res);
        if (!state.standardPinned && res.best_standard) {
            const std = state.comparisonStandards.find(s => s.name === res.best_standard);
            if (std && (!state.selectedStandard || state.selectedStandard.name !== std.name)) {
                state.selectedStandard = std;
                renderComparisonStandards();   // show it selected, as a manual pick does
                updateAnalysisOverlay();
                maybeAutoRunAnalysis();
            }
        }
    } catch (e) {
        if (panel) panel.innerHTML = '';
    }
}

function renderBestFitPanel(res) {
    const panel = document.getElementById('analysis-bestfit-panel');
    if (!panel) return;
    if (!res || !res.ranking || res.ranking.length === 0) {
        panel.innerHTML = '';
        return;
    }
    const ranking = res.ranking
        .map(r => `${escapeHtml(r.name)}: ${Number(r.score).toFixed(3)}`)
        .join('&nbsp;&nbsp;·&nbsp;&nbsp;');
    panel.innerHTML =
        `<div style="color:#3fb950; font-weight:600;">Best fit: ${escapeHtml(res.label || '—')}` +
        ` <span style="color:#7d8590; font-weight:400;">(score ${Number(res.score).toFixed(3)})</span></div>` +
        `<div style="color:#7d8590; margin-top:1px;">${ranking}</div>`;
}

function showStandardContextMenu(e, std) {
    removeContextMenu();
    const menu = document.getElementById('context-menu');
    if (!menu) return;
    menu.style.left = e.pageX + 'px';
    menu.style.top = e.pageY + 'px';
    menu.classList.add('open');

    // Hide "Set as standard" and "Reprocess" (not relevant for existing standards)
    const setStdItem = document.getElementById('ctx-set-standard');
    if (setStdItem) setStdItem.style.display = 'none';
    const reprocItem = document.getElementById('ctx-reprocess');
    if (reprocItem) reprocItem.style.display = 'none';
    const limsItem = document.getElementById('ctx-export-lims');
    if (limsItem) limsItem.style.display = 'none';
    const copyItem = document.getElementById('ctx-copy-link');
    if (copyItem) copyItem.style.display = 'none';

    // Show rename + remove options
    const renameItem = document.getElementById('ctx-rename-standard');
    const removeItem = document.getElementById('ctx-remove-standard');

    if (renameItem) {
        renameItem.style.display = '';
        const newRenameItem = renameItem.cloneNode(true);
        renameItem.replaceWith(newRenameItem);
        newRenameItem.id = 'ctx-rename-standard';
        newRenameItem.addEventListener('click', async () => {
            removeContextMenu();
            const newName = prompt(`Rename "${std.name}" to:`, std.name);
            if (!newName || newName === std.name) return;
            try {
                const password = adminPassword('rename a comparison standard');
                if (!password) return;
                await apiPost('/api/comparison-standard/rename', { old_name: std.name, new_name: newName, password });
                showNotification(`Renamed to: ${newName}`, 'success');
                if (state.selectedStandard && state.selectedStandard.name === std.name) {
                    state.selectedStandard.name = newName;
                }
                await loadComparisonStandards();
            } catch (err) {
                showNotification('Rename failed: ' + err.message, 'error');
            }
        });
    }

    if (removeItem) {
        removeItem.style.display = '';
        const newRemoveItem = removeItem.cloneNode(true);
        removeItem.replaceWith(newRemoveItem);
        newRemoveItem.id = 'ctx-remove-standard';
        newRemoveItem.addEventListener('click', async () => {
            removeContextMenu();
            if (!confirm(`Delete comparison standard "${std.name}"?`)) return;
            try {
                const password = adminPassword('delete a comparison standard');
                if (!password) return;
                await api('DELETE', `/api/comparison-standard/${encodeURIComponent(std.name)}`, { password });
                showNotification(`Deleted standard: ${std.name}`, 'success');
                if (state.selectedStandard && state.selectedStandard.name === std.name) {
                    state.selectedStandard = null;
                }
                await loadComparisonStandards();
            } catch (err) {
                showNotification('Failed to delete standard: ' + err.message, 'error');
            }
        });
    }

    // Remove on click elsewhere
    const handler = (ev) => {
        if (!menu.contains(ev.target)) {
            removeContextMenu();
            document.removeEventListener('click', handler, true);
        }
    };
    setTimeout(() => document.addEventListener('click', handler, true), 0);
}

function readAnalysisParams() {
    const p = state.analysisParams;
    // Sync trend sliders → real values in state
    syncTrendSlider('baseline');
    syncTrendSlider('detail');
    syncTrendSlider('smoothing');
    // Read remaining numeric inputs
    const fields = {
        'param-thresh-marginal': 'thresh_marginal',
        'param-thresh-moderate': 'thresh_moderate',
        'param-thresh-significant': 'thresh_significant',
        'param-x-max': 'x_max_min',
    };
    for (const [elId, key] of Object.entries(fields)) {
        const el = document.getElementById(elId);
        if (el) {
            const val = parseFloat(el.value);
            if (!isNaN(val)) p[key] = val;
        }
    }
    // Read range overlay values from their cards
    readRangeOverlaysFromDOM();
}

function populateAnalysisParamInputs() {
    const p = state.analysisParams;
    // Populate trend sliders from real values
    populateTrendSliders();
    // Populate remaining numeric inputs
    const fields = {
        'param-thresh-marginal': 'thresh_marginal',
        'param-thresh-moderate': 'thresh_moderate',
        'param-thresh-significant': 'thresh_significant',
        'param-x-max': 'x_max_min',
    };
    for (const [elId, key] of Object.entries(fields)) {
        const el = document.getElementById(elId);
        if (el) el.value = p[key];
    }
    renderRangeOverlays();
}

/* --- Dynamic range overlay management --- */

function renderRangeOverlays() {
    const container = document.getElementById('range-overlays-list');
    if (!container) return;
    container.innerHTML = '';
    for (const range of state.rangeOverlays) {
        const card = document.createElement('div');
        card.className = 'range-card';
        card.dataset.rangeId = range.id;
        card.innerHTML = `
            <div class="range-header">
                <input type="color" class="range-color" value="${(range.color || '#3fb95044').slice(0, 7)}" title="Range color">
                <input type="text" class="range-label-input" value="${escapeHtml(range.label)}" placeholder="Label" title="Range label">
                <button class="range-delete" title="Remove this range">&times;</button>
            </div>
            <div class="range-fields">
                <label>C</label>
                <input type="number" class="range-c-start" value="${range.c_start}" min="1" max="100" step="1">
                <label>&ndash;</label>
                <input type="number" class="range-c-end" value="${range.c_end}" min="1" max="100" step="1">
            </div>
        `;
        // Delete button
        card.querySelector('.range-delete').addEventListener('click', () => {
            state.rangeOverlays = state.rangeOverlays.filter(r => r.id !== range.id);
            renderRangeOverlays();
            debouncedAnalysis();
        });
        // Change handlers for label, color, c_start, c_end
        card.querySelector('.range-label-input').addEventListener('input', () => { readRangeOverlaysFromDOM(); debouncedAnalysis(); });
        card.querySelector('.range-color').addEventListener('input', () => { readRangeOverlaysFromDOM(); debouncedAnalysis(); });
        card.querySelector('.range-c-start').addEventListener('input', () => { readRangeOverlaysFromDOM(); debouncedAnalysis(); });
        card.querySelector('.range-c-end').addEventListener('input', () => { readRangeOverlaysFromDOM(); debouncedAnalysis(); });
        container.appendChild(card);
    }
}

function readRangeOverlaysFromDOM() {
    const container = document.getElementById('range-overlays-list');
    if (!container) return;
    const cards = container.querySelectorAll('.range-card');
    cards.forEach(card => {
        const id = parseInt(card.dataset.rangeId);
        const range = state.rangeOverlays.find(r => r.id === id);
        if (!range) return;
        const labelEl = card.querySelector('.range-label-input');
        const colorEl = card.querySelector('.range-color');
        const startEl = card.querySelector('.range-c-start');
        const endEl = card.querySelector('.range-c-end');
        if (labelEl) range.label = labelEl.value;
        if (colorEl) range.color = colorEl.value + '44'; // add alpha
        if (startEl) range.c_start = parseInt(startEl.value) || range.c_start;
        if (endEl) range.c_end = parseInt(endEl.value) || range.c_end;
    });
}

function addRangeOverlay() {
    const id = state.nextRangeId++;
    // Pick a distinct hue
    const hues = [120, 40, 200, 320, 60, 280, 0, 160];
    const hue = hues[(state.rangeOverlays.length) % hues.length];
    const hex = hslToHex(hue, 60, 50);
    state.rangeOverlays.push({ id, label: 'Range ' + id, c_start: 5, c_end: 15, color: hex + '44' });
    renderRangeOverlays();
    debouncedAnalysis();
}

function hslToHex(h, s, l) {
    s /= 100; l /= 100;
    const a = s * Math.min(l, 1 - l);
    const f = n => { const k = (n + h / 30) % 12; return l - a * Math.max(Math.min(k - 3, 9 - k, 1), -1); };
    const toHex = x => Math.round(x * 255).toString(16).padStart(2, '0');
    return '#' + toHex(f(0)) + toHex(f(8)) + toHex(f(4));
}

async function saveAnalysisDefaults() {
    const pw = prompt('Enter admin password to save current settings as defaults:');
    if (pw === null) return;  // cancelled
    readAnalysisParams();
    const body = {
        password: pw,
        params: { ...state.analysisParams },
        range_overlays: state.rangeOverlays.map(r => ({
            label: r.label, c_start: r.c_start, c_end: r.c_end, color: r.color
        })),
    };
    try {
        await apiPost('/api/save-analysis-defaults', body);
        showNotification('Defaults saved — will persist across restarts', 'success');
    } catch (e) {
        showNotification(e.message || 'Failed to save defaults', 'error');
    }
}

const debouncedAnalysis = debounce(() => {
    readAnalysisParams();
    runAnalysis();
}, 600);

/**
 * Add shaded rectangles + labels for each range window to the given
 * shapes/annotations arrays. The windows are /api/analysis's: the server's
 * one carbon→time conversion on the sample revision's ladder, clipped to the
 * displayed axis — the very windows the bullets were evaluated on. Windows
 * that could not be evaluated (width 0) draw nothing.
 */
function addRangeShapes(shapes, annotations, windows) {
    for (const w of (windows || [])) {
        if (!w.evaluable) continue;
        const t0 = w.t0, t1 = w.t1;
        const overlay = state.rangeOverlays[w.index] || {};   // fallback colour only

        // Parse color (stored as #RRGGBBAA)
        const hex = (w.color || overlay.color || '#3fb95044').replace('#', '');
        const r = parseInt(hex.slice(0, 2), 16);
        const g = parseInt(hex.slice(2, 4), 16);
        const b = parseInt(hex.slice(4, 6), 16);
        const a = hex.length >= 8 ? parseInt(hex.slice(6, 8), 16) / 255 : 0.15;

        shapes.push({
            type: 'rect', x0: t0, x1: t1,
            y0: 0, y1: 1, yref: 'paper',
            fillcolor: `rgba(${r},${g},${b},${a * 0.5})`,
            line: { width: 1, color: `rgba(${r},${g},${b},${a})`, dash: 'dot' },
            layer: 'below',
        });

        // Label at top of the range
        if (annotations) {
            annotations.push({
                x: (t0 + t1) / 2, y: 1.02, yref: 'paper',
                text: escapeHtml(w.label),   // Plotly renders pseudo-HTML
                showarrow: false,
                font: { color: `rgb(${r},${g},${b})`, size: 9 },
                yanchor: 'bottom',
            });
        }
    }
}

function maybeAutoRunAnalysis() {
    // Auto-run if both sample and standard are selected
    if (state.selectedSample && state.selectedStandard) {
        debouncedAnalysis();
    }
}

/** Show/hide a loading overlay on an element. */
function _setLoading(el, loading) {
    if (!el) return;
    // Remove any existing overlay
    const existing = el.querySelector('.loading-spinner-overlay');
    if (existing) existing.remove();
    if (!loading) return;
    const overlay = document.createElement('div');
    overlay.className = 'loading-spinner-overlay';
    overlay.innerHTML = `
        <div style="display:flex;flex-direction:column;align-items:center;gap:8px;">
            <div class="spinner"></div>
            <span style="font-size:11px;color:#7d8590;">Loading...</span>
        </div>`;
    el.style.position = 'relative';
    overlay.style.cssText = 'position:absolute;inset:0;z-index:20;display:flex;align-items:center;justify-content:center;background:rgba(13,17,23,0.75);border-radius:6px;';
    el.appendChild(overlay);
}

function _setAnalysisLoading(on) {
    const ids = ['analysis-trend-plot', 'analysis-diff-plot', 'analysis-report-text', 'analysis-conclusion'];
    for (const id of ids) {
        const el = document.getElementById(id);
        if (el) _setLoading(el.closest('.panel') || el.parentElement || el, on);
    }
    const statusEl = document.getElementById('analysis-status');
    if (statusEl) statusEl.textContent = on ? 'Running analysis...' : 'Ready';
}

let _analysisSeq = 0;

async function runAnalysis() {
    if (!state.selectedSample) {
        showNotification('Select a sample first', 'info');
        return;
    }
    if (!state.selectedStandard) {
        showNotification('Select a comparison standard first', 'info');
        return;
    }

    readAnalysisParams();
    _setAnalysisLoading(true);

    const body = {
        sample_id: state.selectedSample.sample_id,
        standard_name: state.selectedStandard.name,
        ...state.analysisParams,
        ranges: rangesForPayload(state.rangeOverlays),   // [] = no ranges
    };

    // Only the latest request renders: a slow earlier analysis (another
    // sample or other parameters) must not draw over a newer one.
    const seq = ++_analysisSeq;
    try {
        const result = await apiPost('/api/analysis', body);
        if (seq !== _analysisSeq) return;
        state.analysisResult = result;
        state._renderedAnalysisSampleId = body.sample_id;
        renderAnalysisResults(result);
    } catch (e) {
        if (seq === _analysisSeq) showNotification('Analysis failed: ' + e.message, 'error');
    } finally {
        if (seq === _analysisSeq) _setAnalysisLoading(false);
    }
}

/** Update the getting-started overlay: show checkmarks and hide when both selected. */
function updateAnalysisOverlay() {
    const overlay = document.getElementById('analysis-overlay');
    if (!overlay) return;
    const chkSample = document.getElementById('chk-sample');
    const chkStandard = document.getElementById('chk-standard');
    const hasSample = !!state.selectedSample;
    const hasStandard = !!state.selectedStandard;
    if (chkSample) chkSample.innerHTML = hasSample ? '&#x2713;' : '&#x200B;';
    if (chkStandard) chkStandard.innerHTML = hasStandard ? '&#x2713;' : '&#x200B;';
    overlay.style.display = (hasSample && hasStandard) ? 'none' : 'flex';
    if (typeof Comments !== 'undefined') {
        Comments.setSample(hasSample ? state.selectedSample.sample_id : null);
    }
}

function renderAnalysisResults(result) {
    // The calibration lines and range boxes are the server's: the sample
    // revision's ladder (cal_times/cal_carbons) and the report's windows,
    // never gc1's /api/calibration.
    const calTimes = result.cal_times || [];
    const calCarbons = result.cal_carbons || [];

    // Trend plot
    const trendDiv = document.getElementById('analysis-trend-plot');
    if (trendDiv && result.trend) {
        const trendTraces = [];

        // Sample trend
        if (result.trend.sample_x && result.trend.sample_y) {
            trendTraces.push({
                x: result.trend.sample_x, y: result.trend.sample_y,
                type: 'scatter', mode: 'lines',
                name: 'Sample', line: { color: '#f85149', width: 2 },
            });
        }

        // Standard trend
        if (result.trend.standard_x && result.trend.standard_y) {
            trendTraces.push({
                x: result.trend.standard_x, y: result.trend.standard_y,
                type: 'scatter', mode: 'lines',
                name: 'Standard', line: { color: '#7d8590', width: 2 },
            });
        }

        // Calibration vertical lines (the revision's ladder)
        const shapes = [];
        const annotations = [];
        for (let i = 0; i < calTimes.length; i++) {
            const rt = calTimes[i];
            shapes.push({
                type: 'line', x0: rt, x1: rt, y0: 0, y1: 1, yref: 'paper',
                line: { color: '#d29922', width: 0.5, dash: 'dot' },
            });
            annotations.push({
                x: rt, y: 1, yref: 'paper', text: `C${calCarbons[i]}`,
                showarrow: false, font: { color: '#d29922', size: 8 }, yanchor: 'bottom',
            });
        }

        // Range boxes from the report's windows
        addRangeShapes(shapes, annotations, result.windows);

        Plotly.react(trendDiv, trendTraces, basePlotlyLayout({
            title: { text: 'Trend Comparison', font: { size: 14 } },
            xaxis: {
                title: 'Retention Time (min)',
                gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590',
                range: result.trend.x_range || undefined,
            },
            yaxis: { title: 'Intensity', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
            shapes, annotations,
        }), PLOTLY_CONFIG);
    }

    // Difference plot
    const diffDiv = document.getElementById('analysis-diff-plot');
    if (diffDiv && result.diff) {
        const diffTraces = [];
        const diffX = result.diff.x;
        const diffY = result.diff.y;
        const p = result.params_used || state.analysisParams;
        const spikes = result.spikes || [];

        if (diffX && diffY) {
            // Positive fill (red above zero)
            const posY = diffY.map(v => v > 0 ? v : 0);
            diffTraces.push({
                x: diffX, y: posY, type: 'scatter', mode: 'lines',
                name: 'Above baseline', fill: 'tozeroy',
                fillcolor: 'rgba(248, 81, 73, 0.3)',
                line: { color: 'rgba(248, 81, 73, 0.6)', width: 1 },
            });

            // Negative fill (blue below zero)
            const negY = diffY.map(v => v < 0 ? v : 0);
            diffTraces.push({
                x: diffX, y: negY, type: 'scatter', mode: 'lines',
                name: 'Below baseline', fill: 'tozeroy',
                fillcolor: 'rgba(56, 139, 253, 0.3)',
                line: { color: 'rgba(56, 139, 253, 0.6)', width: 1 },
            });

            // Full difference line
            diffTraces.push({
                x: diffX, y: diffY, type: 'scatter', mode: 'lines',
                name: 'Difference', line: { color: '#e6edf3', width: 1.5 },
                showlegend: true,
            });
        }

        // The spikes the report counted (raw difference, so every "sharp
        // spike" in a bullet can be found on the graph)
        if (spikes.length) {
            diffTraces.push({
                x: spikes.map(s => s.t), y: spikes.map(s => s.value),
                type: 'scatter', mode: 'markers', name: 'Counted spikes',
                marker: {
                    symbol: spikes.map(s => s.sign > 0 ? 'triangle-up' : 'triangle-down'),
                    size: 10, color: '#e3b341', line: { color: '#0d1117', width: 1 },
                },
                hovertemplate: 'Spike %{x:.2f} min: %{y:.0f}<extra></extra>',
            });
        }

        // Range boxes + ±threshold lines (dotted)
        const diffShapes = [];
        const diffAnnotations = [];
        addRangeShapes(diffShapes, diffAnnotations, result.windows);
        const levels = [
            [p.thresh_marginal, 'marginal', '#7d8590'],
            [p.thresh_moderate, 'moderate', '#d29922'],
            [p.thresh_significant, 'significant', '#f85149'],
        ];
        for (const [level, name, color] of levels) {
            for (const sgn of [1, -1]) {
                diffShapes.push({
                    type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y',
                    y0: sgn * level, y1: sgn * level,
                    line: { color, width: 1, dash: 'dot' },
                });
            }
            diffAnnotations.push({
                xref: 'paper', x: 1, xanchor: 'right', yref: 'y', y: level, yanchor: 'bottom',
                text: `${name} ±${level}`, showarrow: false, font: { color, size: 9 },
            });
        }
        // Keep the data readable: scale to the data and the spikes, not to
        // the significant line (lines beyond the data are simply off-scale).
        let span = Number(p.thresh_marginal) || 1;
        const xMax = (result.diff.x_range || [])[1];
        (diffY || []).forEach((v, i) => {
            if (xMax == null || diffX[i] <= xMax) span = Math.max(span, Math.abs(v));
        });
        spikes.forEach(s => { span = Math.max(span, Math.abs(s.value)); });

        Plotly.react(diffDiv, diffTraces, basePlotlyLayout({
            title: { text: 'Difference Plot', font: { size: 14 } },
            xaxis: {
                title: 'Retention Time (min)',
                gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590',
                range: result.diff.x_range || undefined,
            },
            yaxis: { title: 'Difference', gridcolor: '#21262d', zerolinecolor: '#30363d',
                     color: '#7d8590', range: [-span * 1.15, span * 1.15] },
            shapes: diffShapes,
            annotations: diffAnnotations,
        }), PLOTLY_CONFIG);
    }

    // Deviation report: the server's bullets, read-only (a <pre>). Operators
    // change the parameters or ranges, or add a comment, to change them.
    state._lastReportBullets = result.text || '';

    const reportDiv = document.getElementById('analysis-report-text');
    if (reportDiv) {
        const p = result.params_used || state.analysisParams;
        const sampleName = state.selectedSample ? state.selectedSample.name : '?';
        const stdName = result.standard_name || (state.selectedStandard ? state.selectedStandard.name : '?');
        const rangesStr = (result.ranges || []).map(r => `${r.label}(C${r.c_start}–C${r.c_end})`).join('  ');
        const header = [
            `Sample:   ${sampleName}`,
            `Standard: ${stdName}`,
            `Params:   baseline=${realToSlider('baseline', p.quantile).toFixed(2)}  detail=${realToSlider('detail', p.window).toFixed(2)}  smoothing=${realToSlider('smoothing', p.sigma).toFixed(2)}`,
            `Thresholds: marginal≥${p.thresh_marginal}  moderate≥${p.thresh_moderate}  significant≥${p.thresh_significant}`,
            `Ranges:   ${rangesStr || '(none)'}`,
            '',
        ].join('\n');
        reportDiv.textContent = header + state._lastReportBullets;
    }

    // Conclusion (editable)
    const conclusionEl = document.getElementById('analysis-conclusion');
    if (conclusionEl && result.conclusion != null) {
        conclusionEl.value = result.conclusion;
    }

    // Re-draw any persistent annotations on the trend plot
    // (Plotly.react destroys old shapes, and also kills event listeners)
    setTimeout(() => {
        redrawAnnotations();
        setupAnnotationHandler();
        // Restore annotation mode if active
        if (annotationMode) {
            const trendDiv = document.getElementById('analysis-trend-plot');
            if (trendDiv) {
                Plotly.relayout(trendDiv, { dragmode: 'select' });
                trendDiv.style.cursor = 'crosshair';
            }
        }
    }, 100);
}

/* ===================================================================
   13b. ANNOTATION TOOL
   Annotations are sample comments (phase 4, static/js/comments.js):
   saving the modal POSTs one comment {text, t0, t1}; the shapes on the
   trend plot are drawn from the sample's comments (GET), on every sample
   change and after each analysis. Nothing is written into the bullets.
   =================================================================== */

let annotationMode = false;

function toggleAnnotationMode() {
    annotationMode = !annotationMode;
    const btn = document.getElementById('btn-annotation-toggle');
    if (btn) {
        btn.classList.toggle('active', annotationMode);
        btn.title = annotationMode ? 'Click & drag on trend plot to annotate a region' : 'Enable annotation mode';
    }

    const trendDiv = document.getElementById('analysis-trend-plot');
    if (!trendDiv) return;

    if (annotationMode) {
        // Enable Plotly box-select mode for drag annotation
        Plotly.relayout(trendDiv, { dragmode: 'select' });
        trendDiv.style.cursor = 'crosshair';
    } else {
        Plotly.relayout(trendDiv, { dragmode: 'zoom' });
        trendDiv.style.cursor = '';
    }
}

function setupAnnotationHandler() {
    const trendDiv = document.getElementById('analysis-trend-plot');
    if (!trendDiv || typeof trendDiv.on !== 'function') return;

    trendDiv.on('plotly_selected', (eventData) => {
        if (!annotationMode || !eventData || !eventData.range) return;

        const t_start = Math.min(eventData.range.x[0], eventData.range.x[1]);
        const t_end = Math.max(eventData.range.x[0], eventData.range.x[1]);
        if (Math.abs(t_end - t_start) < 0.01) return; // too small

        // Carbon range, for the modal's description only (the saved default
        // label comes from the server): the rendered analysis's ladder, i.e.
        // the sample revision's own anchors, with the server's extrapolation
        const res = state.analysisResult || {};
        const c_range = carbonSpanText(t_start, t_end, res.cal_times, res.cal_carbons);

        const regionDesc = c_range
            ? `${c_range}  (${t_start.toFixed(2)}–${t_end.toFixed(2)} min)`
            : `(${t_start.toFixed(2)}–${t_end.toFixed(2)} min)`;

        // The sample whose analysis the trend plot shows (else the selection)
        const sid = state._renderedAnalysisSampleId != null ? state._renderedAnalysisSampleId
            : (state.selectedSample ? state.selectedSample.sample_id : null);
        _openAnnotationModal(regionDesc, t_start, t_end, trendDiv, sid);

        // Disable annotation mode after one annotation (no endless loop)
        annotationMode = false;
        const btn = document.getElementById('btn-annotation-toggle');
        if (btn) btn.classList.remove('active');
        Plotly.relayout(trendDiv, { dragmode: 'zoom' });
        trendDiv.style.cursor = '';
    });
}

function _openAnnotationModal(regionDesc, t_start, t_end, trendDiv, sampleId) {
    const modal = document.getElementById('modal-annotation');
    if (!modal) return;

    const descEl = document.getElementById('annotation-region-desc');
    const inputEl = document.getElementById('annotation-comment-input');
    if (descEl) descEl.textContent = `Region: ${regionDesc}`;
    if (inputEl) inputEl.value = '';

    openModal(modal);
    if (inputEl) setTimeout(() => inputEl.focus(), 100);

    // Wire up save/cancel (replace handlers to avoid stacking)
    const saveBtn = document.getElementById('btn-annotation-save');
    const cancelBtn = document.getElementById('btn-annotation-cancel');
    let busy = false;

    async function _save() {
        if (busy) return;                        // one POST per save
        const current = state.selectedSample ? state.selectedSample.sample_id : null;
        if (sampleId == null || current !== sampleId) {
            // Drawn on one sample, another is selected now: never save it there
            showNotification('The selected sample changed since this region was drawn. ' +
                'Select that sample again to save it, or Cancel.', 'error');
            return;                              // the modal stays open
        }
        busy = true;
        const text = (inputEl ? inputEl.value : '').trim();
        await Comments.setSample(sampleId);
        const saved = await Comments.add({ text, t0: t_start, t1: t_end }, sampleId);
        if (!saved) { busy = false; return; }    // refused: the modal stays open
        closeModal(modal);
        Plotly.restyle(trendDiv, { selectedpoints: [null] });
        _cleanup();
    }

    function _cancel() {
        closeModal(modal);
        Plotly.restyle(trendDiv, { selectedpoints: [null] });
        _cleanup();
    }

    function _cleanup() {
        if (saveBtn) saveBtn.replaceWith(saveBtn.cloneNode(true));
        if (cancelBtn) cancelBtn.replaceWith(cancelBtn.cloneNode(true));
        if (inputEl) inputEl.onkeydown = null;
    }

    if (saveBtn) saveBtn.addEventListener('click', _save);
    if (cancelBtn) cancelBtn.addEventListener('click', _cancel);

    // Enter key saves
    if (inputEl) {
        inputEl.onkeydown = (e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _save(); }
        };
    }
}

/** Draw the selected sample's annotation comments on the trend plot. When
    the selected sample changed, its comments are fetched first (the
    redraw then comes from Comments' onChange). */
function redrawAnnotations() {
    const sid = state.selectedSample ? state.selectedSample.sample_id : null;
    if (typeof Comments === 'undefined') return;
    if (sid !== Comments.sampleId()) { Comments.setSample(sid); return; }
    _drawAnnotationShapes(Comments.current());
}

function _drawAnnotationShapes(list) {
    const trendDiv = document.getElementById('analysis-trend-plot');
    if (!trendDiv || !trendDiv.layout) return;
    // Old annotation shapes are found by their fill/label colours (Plotly may
    // strip custom properties such as _annotation)
    const shapes = (trendDiv.layout.shapes || []).filter(s => !Comments.isAnnotationShape(s));
    const annotations = (trendDiv.layout.annotations || []).filter(a => !Comments.isAnnotationLabel(a));
    const overlay = Comments.annotationOverlay(list);   // label text escaped for Plotly
    Plotly.relayout(trendDiv, {
        shapes: shapes.concat(overlay.shapes),
        annotations: annotations.concat(overlay.labels),
    });
}

/** Clear Annotations: soft-delete the sample's annotation comments, after a
    confirmation naming the count (Comments.clearAnnotations). */
function clearAllAnnotations() {
    if (typeof Comments === 'undefined') return;
    const sid = state.selectedSample ? state.selectedSample.sample_id : null;
    if (sid == null) { showNotification('Select a sample first', 'info'); return; }
    const label = state.selectedSample.lab_id || state.selectedSample.name || String(sid);
    Comments.setSample(sid).then(() => Comments.clearAnnotations(sid, label));
}

/* ===================================================================
   13c. ANALYSIS QUEUE
   =================================================================== */

function addToAnalysisQueue() {
    if (!state.selectedSample) {
        showNotification('No sample selected for queue', 'info');
        return;
    }

    // If we're editing an existing queue item, update it in place
    if (state._editingQueueIdx != null && state._editingQueueIdx >= 0 &&
        state._editingQueueIdx < state.analysisQueue.length) {
        const idx = state._editingQueueIdx;
        const item = state.analysisQueue[idx];
        // Update with current analysis state
        if (state.selectedSample) {
            item.sample_id = state.selectedSample.sample_id;
            item.lab_id = state.selectedSample.lab_id || item.lab_id;
        }
        item.standard_name = state.selectedStandard?.name || item.standard_name;
        // Bullets are computed by the server from these at export time.
        item.params = captureReportParams(state.analysisParams);
        item.ranges = rangesForPayload(state.rangeOverlays);
        delete item.bullets;
        const conclusionEl = document.getElementById('analysis-conclusion');
        if (conclusionEl) item.conclusion = conclusionEl.value.trim();

        // Exit editing mode
        state._editingQueueIdx = null;
        _updateQueueButton(false);
        renderAnalysisQueue();
        showNotification(`Updated queue item: ${item.lab_id}`, 'success');
        return;
    }

    // Otherwise open the "Add to Queue" modal for a new item
    openAnalysisExportModal();
}

function openAnalysisExportModal() {
    const modal = document.getElementById('modal-analysis-export');
    if (!modal) return;

    // Pre-fill with sample info
    const labIdInput = document.getElementById('export-lab-id');
    const docNameInput = document.getElementById('export-doc-name');
    const bulletsInput = document.getElementById('export-bullets');
    const conclusionInput = document.getElementById('export-conclusion');

    // The Lab ID is the sample's, from the store: the server uploads to the
    // QBench sample of that Lab ID, so it isn't editable here.
    if (labIdInput) {
        labIdInput.value = state.selectedSample ? (state.selectedSample.lab_id || state.selectedSample.name) : '';
        labIdInput.readOnly = true;
    }
    if (docNameInput) docNameInput.value = 'GC Analysis';

    // A read-only preview of the server's bullets (the export recomputes
    // them from the parameters and ranges captured with the queue item).
    const conclusionEl = document.getElementById('analysis-conclusion');
    if (bulletsInput) {
        bulletsInput.value = (state.analysisResult && state.analysisResult.text) || '';
        bulletsInput.readOnly = true;
    }
    if (conclusionInput) conclusionInput.value = conclusionEl ? conclusionEl.value.trim() : '';

    // Populate overlay standards checklist
    const checklistDiv = document.getElementById('export-standards-checklist');
    if (checklistDiv) {
        checklistDiv.innerHTML = '';
        if (state.comparisonStandards.length === 0) {
            checklistDiv.innerHTML = '<span style="font-size:11px; color:#7d8590;">No standards available</span>';
        } else {
            for (const std of state.comparisonStandards) {
                const label = document.createElement('label');
                label.style.cssText = 'display:flex; align-items:center; gap:6px; padding:3px 4px; font-size:11px; color:#c9d1d9; cursor:pointer;';
                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.checked = false;  // unchecked by default
                cb.dataset.stdPath = std.path;
                cb.dataset.stdName = std.name;
                const span = document.createElement('span');
                span.textContent = std.name;
                label.appendChild(cb);
                label.appendChild(span);
                checklistDiv.appendChild(label);
            }
        }
    }

    openModal(modal);
}

function confirmAddToQueue() {
    const labId = (document.getElementById('export-lab-id')?.value || '').trim();
    const sampleName = (document.getElementById('export-doc-name')?.value || '').trim();
    if (!labId) {
        showNotification('Lab ID is required', 'error');
        return;
    }

    // Collect selected overlay standards from the checklist
    const overlayStds = [];
    const checklistDiv = document.getElementById('export-standards-checklist');
    if (checklistDiv) {
        checklistDiv.querySelectorAll('input[type="checkbox"]:checked').forEach(cb => {
            if (cb.dataset.stdPath) overlayStds.push(cb.dataset.stdPath);
        });
    }

    // The conclusion stays editable; bullets are never sent (the server
    // computes them from the captured params and ranges).
    const conclusion = (document.getElementById('export-conclusion')?.value || '').trim();

    state.analysisQueue.push({
        lab_id: labId,
        sample_name: sampleName,
        sample_id: state.selectedSample ? state.selectedSample.sample_id : null,
        standard_name: state.selectedStandard?.name || '',
        conclusion,
        params: captureReportParams(state.analysisParams),  // capture parameters at queue time
        overlay_standards: overlayStds,
        ranges: rangesForPayload(state.rangeOverlays),  // capture regions at queue time
        added_at: new Date().toISOString(),
    });

    renderAnalysisQueue();
    closeAllModals();
    showNotification(`Added "${labId}" to export queue`, 'success');
}

function renderAnalysisQueue() {
    const container = document.getElementById('analysis-queue-list');
    if (!container) return;
    container.innerHTML = '';

    if (state.analysisQueue.length === 0) {
        container.innerHTML = '<p class="muted">Queue is empty</p>';
        return;
    }

    state.analysisQueue.forEach((item, idx) => {
        const row = document.createElement('div');
        row.className = 'queue-item';
        row.style.cursor = 'pointer';
        row.innerHTML = `
            <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(item.lab_id)}</span>
            <span style="font-size:10px; color:#7d8590; flex-shrink:0;">${escapeHtml(item.sample_name || '')}</span>
            <button class="qi-remove" title="Remove from queue">&times;</button>
        `;

        // Click row = restore this item's analysis context
        row.addEventListener('click', (e) => {
            if (e.target.classList.contains('qi-remove')) return;
            _restoreQueueItem(item, idx);
            // Highlight this queue item as active (blue)
            container.querySelectorAll('.queue-item.active').forEach(el => el.classList.remove('active'));
            row.classList.add('active');
            // Grey-out sample in the sample list (staged, not confirmed)
            _stageAnalysisSample(item.sample_id);
        });

        row.querySelector('.qi-remove').addEventListener('click', (e) => {
            e.stopPropagation();
            state.analysisQueue.splice(idx, 1);
            renderAnalysisQueue();
        });
        container.appendChild(row);
    });
}

/** Restore analysis context from a queued item so user can edit details. */
function _restoreQueueItem(item, idx) {
    // Set the selected sample/standard from the queue item
    if (item.sample_id != null) {
        const file = state.files.find(f => f.sample_id === item.sample_id);
        if (file) state.selectedSample = file;
    }
    if (item.standard_name) {
        const std = state.comparisonStandards.find(s => s.name === item.standard_name);
        if (std) state.selectedStandard = std;
    }
    // Update labels
    const label = document.getElementById('analysis-sample-label');
    if (label) label.textContent = item.lab_id || '';

    // Re-render standard selection highlight
    renderComparisonStandards();

    // Store which queue index is being edited so we can update it
    state._editingQueueIdx = idx;

    // Change "Send to Queue" button to "Apply Changes"
    _updateQueueButton(true);

    // Annotations are the sample's comments (not part of the queue item)
    redrawAnnotations();

    // Re-run the analysis so graphs + report load
    if (state.selectedSample && state.selectedStandard) {
        readAnalysisParams();
        runAnalysis().then(() => {
            // After analysis completes, restore the saved conclusion
            // (runAnalysis overwrites it with the auto-generated one)
            const conclusionEl = document.getElementById('analysis-conclusion');
            if (conclusionEl && item.conclusion) conclusionEl.value = item.conclusion;
        });
    }

    showNotification(`Editing queue item: ${item.lab_id}`, 'info');
}

/** Toggle the "Send to Queue" button between add and edit modes. */
function _updateQueueButton(editing) {
    const btn = document.getElementById('btn-send-to-queue');
    if (!btn) return;
    if (editing) {
        btn.textContent = 'Apply Changes';
        btn.className = 'btn btn-success';
        btn.style.width = '100%';
    } else {
        btn.textContent = 'Send to Queue';
        btn.className = 'btn btn-danger';
        btn.style.width = '100%';
    }
}

/** Grey-highlight the sample in the analysis sample list (staged state). */
function _stageAnalysisSample(sampleId) {
    const list = document.getElementById('analysis-sample-list');
    if (!list) return;
    list.querySelectorAll('li.selected').forEach(el => el.classList.remove('selected'));
    list.querySelectorAll('li.staged').forEach(el => el.classList.remove('staged'));
    if (sampleId != null) {
        const item = list.querySelector(`li[data-sample-id="${CSS.escape(String(sampleId))}"]`);
        if (item) item.classList.add('staged');
    }
}

async function exportQueueToQBench() {
    if (state.analysisQueue.length === 0 && !_uploadActive) {
        showNotification('Queue is empty', 'info');
        return;
    }

    const modal = document.getElementById('modal-qbench');
    if (!modal) return;

    // If an upload is already active, restore the modal to its current state
    if (_uploadActive) {
        _rebuildUploadCardsFromState();
        const statusDiv = document.getElementById('qbench-status');
        if (statusDiv && state.analysisQueue.length > 0) {
            statusDiv.innerHTML = `<span style="color:#58a6ff;">Upload in progress. ${state.analysisQueue.length} new item(s) ready to add.</span>`;
        }
        openModal(modal);
        return;
    }

    // Fresh open — build cards from the analysis queue
    _buildUploadCards();

    // Auto-fill credentials from the shared server file
    try {
        const creds = await apiGet('/api/qbench-credentials');
        const uEl = document.getElementById('qb-username');
        if (uEl && creds.username && !uEl.value) uEl.value = creds.username;
    } catch (_) { /* ignore */ }

    // Reset status
    const statusDiv = document.getElementById('qbench-status');
    if (statusDiv) statusDiv.textContent = `${state.analysisQueue.length} item(s) ready`;
    const startBtn = document.getElementById('btn-qbench-start');
    const stopBtn = document.getElementById('btn-qbench-stop');
    if (startBtn) startBtn.disabled = false;
    if (stopBtn) stopBtn.disabled = true;

    openModal(modal);
}

/* ===================================================================
   13d. EXPORT TO PC
   =================================================================== */

/** Poll a report ZIP job until it is over; its zipJobView. Progress goes on
    the button (and a notification every 20 PDFs), so a long build shows. */
async function waitForReportZip(job, btn) {
    let lastNote = 0;
    for (;;) {
        const v = zipJobView(job);
        if (v.done) return v;
        if (btn) btn.textContent = `Generating ${job.done || 0}/${job.total}...`;
        if ((job.done || 0) - lastNote >= 20) {
            lastNote = job.done;
            showNotification(v.message, 'info');
        }
        await new Promise(resolve => setTimeout(resolve, 1500));
        job = (await apiGet(`/api/export-analysis-reports-zip/${encodeURIComponent(job.id)}`)).job;
    }
}

async function exportToPC() {
    if (state.analysisQueue.length === 0) {
        showNotification('Queue is empty — add samples first', 'info');
        return;
    }

    const btn = document.getElementById('btn-export-pc');
    const origText = btn ? btn.textContent : '';
    if (btn) { btn.textContent = 'Generating...'; btn.disabled = true; }

    const count = state.analysisQueue.length;
    showNotification(`Generating ${count} report(s) — please wait...`, 'info');

    try {
        if (count === 1) {
            // Single file — download PDF directly via browser
            const item = state.analysisQueue[0];
            const resp = await api('POST', '/api/export-analysis-report',
                buildReportItemPayload(item, state.rangeOverlays, state.analysisParams));
            await downloadBlob(resp, `${item.lab_id}_analysis.pdf`);
            showNotification('Download complete', 'success');
        } else {
            // Multiple files — a ZIP built in the background (v3.0.1): poll
            // the job, then let the browser fetch the one-time link.
            const items = state.analysisQueue.map(item =>
                buildReportItemPayload(item, state.rangeOverlays, state.analysisParams));
            const started = await api('POST', '/api/export-analysis-reports-zip', { items });
            const v = await waitForReportZip(started.job, btn);
            if (!v.download) throw new Error(v.message.replace(/^Download failed: /, ''));
            const a = document.createElement('a');
            a.href = v.download;
            a.download = 'analysis_reports.zip';
            document.body.appendChild(a);
            a.click();
            a.remove();
            showNotification(v.message, 'success');
        }
    } catch (e) {
        console.error('Export failed:', e);
        showNotification('Download failed: ' + e.message, 'error');
    } finally {
        if (btn) { btn.textContent = origText; btn.disabled = false; }
    }
}

/* ===================================================================
   13e. QBENCH UPLOAD
   =================================================================== */

// ── Persistent upload state — survives modal close/reopen ────────────
let _uploadActive = false;
let _uploadItemStates = [];   // [{idx, lab_id, status, step, steps, msg}, ...]
let uploadSSE = null;

async function startQBenchUpload() {
    if (state.analysisQueue.length === 0 && !_uploadActive) {
        showNotification('Queue is empty — add samples first', 'info');
        return;
    }

    const username = (document.getElementById('qb-username')?.value || '').trim();
    const password = (document.getElementById('qb-password')?.value || '').trim();
    const apiUrl = (document.getElementById('qb-api-url')?.value || '').trim();

    const startBtn = document.getElementById('btn-qbench-start');
    const stopBtn = document.getElementById('btn-qbench-stop');

    try {
        if (startBtn) startBtn.disabled = true;
        if (stopBtn) stopBtn.disabled = false;

        // The server refuses the whole queue (409, nothing queued) if any
        // sample isn't final or is unreleased backfill.
        const resp = await apiPostRefusable('/api/qbench-upload', 'Not uploaded', {
            // the same payload as the PC export: captured params and ranges, no bullets
            queue: state.analysisQueue.map(item =>
                buildReportItemPayload(item, state.rangeOverlays, state.analysisParams)),
            username,
            password,
            api_url: apiUrl,
        });

        if (resp.status === 'appended') {
            // Items were appended to an existing upload
            const baseIdx = resp.base_idx;
            state.analysisQueue.forEach((item, i) => {
                const idx = baseIdx + i;
                _uploadItemStates[idx] = {
                    idx, lab_id: item.lab_id, status: 'waiting',
                    step: 0, steps: 1, msg: 'Waiting',
                };
            });
            _rebuildUploadCardsFromState();
            showNotification(`Added ${resp.count} item(s) to running upload`, 'success');
        } else {
            // Fresh upload started
            _uploadActive = true;
            _uploadItemStates = state.analysisQueue.map((item, idx) => ({
                idx, lab_id: item.lab_id, status: 'waiting',
                step: 0, steps: 1, msg: 'Waiting',
            }));
            _rebuildUploadCardsFromState();
            connectUploadSSE();
        }
    } catch (e) {
        showNotification('QBench upload failed: ' + e.message, 'error');
        if (startBtn) startBtn.disabled = false;
    }
}

/** Build upload cards from the persistent _uploadItemStates array. */
function _buildUploadCards() {
    _uploadItemStates = state.analysisQueue.map((item, idx) => ({
        idx, lab_id: item.lab_id, status: 'waiting',
        step: 0, steps: 1, msg: 'Waiting',
    }));
    _renderUploadCards();
}

/** Rebuild the modal DOM from _uploadItemStates (used when modal is reopened). */
function _rebuildUploadCardsFromState() {
    _renderUploadCards();
    // Update button states
    const startBtn = document.getElementById('btn-qbench-start');
    const stopBtn = document.getElementById('btn-qbench-stop');
    if (_uploadActive) {
        if (startBtn) { startBtn.disabled = false; startBtn.textContent = 'Add More'; }
        if (stopBtn) stopBtn.disabled = false;
    } else {
        if (startBtn) { startBtn.disabled = false; startBtn.textContent = 'Start Upload'; }
        if (stopBtn) stopBtn.disabled = true;
    }
    // Refresh overall bar
    const overallFill = document.getElementById('qbench-overall-fill');
    const overallBar = document.getElementById('qbench-overall-bar');
    if (overallBar) overallBar.style.display = '';
    if (overallFill && _uploadItemStates.length > 0) {
        const done = _uploadItemStates.filter(s =>
            s.status === 'ok' || s.status === 'failed' || s.status === 'error' || s.status === 'skipped'
        ).length;
        overallFill.style.width = Math.round((done / _uploadItemStates.length) * 100) + '%';
    }
}

function _renderUploadCards() {
    const queueDiv = document.getElementById('qbench-queue');
    if (!queueDiv) return;
    queueDiv.innerHTML = '';
    _uploadItemStates.forEach((st) => {
        const card = document.createElement('div');
        card.id = `qb-card-${st.idx}`;
        card.style.cssText = 'background:#0d1117; border:1px solid #21262d; border-radius:6px; padding:8px 10px; display:flex; flex-direction:column; gap:4px;';
        const isDone = st.status === 'ok' || st.status === 'failed' || st.status === 'error' || st.status === 'skipped';
        card.innerHTML = `
            <div style="display:flex; align-items:center; justify-content:space-between; gap:6px;">
                <span style="font-size:12px; font-weight:600; color:#e6edf3; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(st.lab_id)}</span>
                <span class="qb-card-badge" style="font-size:10px; padding:1px 8px; border-radius:10px; background:#21262d; color:#7d8590;">Waiting</span>
                ${isDone ? '' : `<button class="qb-card-skip" data-idx="${st.idx}" title="Skip this sample" style="background:none; border:1px solid #da3633; color:#da3633; border-radius:4px; font-size:10px; padding:1px 6px; cursor:pointer; flex-shrink:0;">✕</button>`}
            </div>
            <div style="height:4px; background:#21262d; border-radius:2px; overflow:hidden;">
                <div class="qb-card-bar" style="width:0%; height:100%; background:#58a6ff; border-radius:2px; transition:width .3s;"></div>
            </div>
            <div class="qb-card-msg" style="font-size:10px; color:#7d8590; min-height:14px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"></div>
        `;
        queueDiv.appendChild(card);
        // Apply current state to the card
        _applyCardState(st.idx, st);
    });
    // Attach skip button handlers
    queueDiv.querySelectorAll('.qb-card-skip').forEach(btn => {
        btn.addEventListener('click', () => _skipUploadItem(parseInt(btn.dataset.idx)));
    });
    // Reset overall bar
    const overallBar = document.getElementById('qbench-overall-bar');
    const overallFill = document.getElementById('qbench-overall-fill');
    if (overallBar) overallBar.style.display = '';
    if (overallFill) overallFill.style.width = '0%';
}

function _applyCardState(idx, data) {
    const card = document.getElementById(`qb-card-${idx}`);
    if (!card) return;
    const badge = card.querySelector('.qb-card-badge');
    const bar = card.querySelector('.qb-card-bar');
    const msg = card.querySelector('.qb-card-msg');
    const skipBtn = card.querySelector('.qb-card-skip');

    if (msg && data.msg) msg.textContent = data.msg;

    const pct = data.steps > 0 ? Math.round((data.step / data.steps) * 100) : 0;
    if (bar) bar.style.width = pct + '%';

    if (badge) {
        const s = data.status;
        if (s === 'ok') {
            badge.textContent = 'OK'; badge.style.background = '#238636'; badge.style.color = '#fff';
            if (bar) { bar.style.width = '100%'; bar.style.background = '#3fb950'; }
        } else if (s === 'skipped') {
            badge.textContent = 'SKIPPED'; badge.style.background = '#484f58'; badge.style.color = '#8b949e';
            if (bar) { bar.style.width = '100%'; bar.style.background = '#484f58'; }
        } else if (s === 'login_failed') {
            badge.textContent = 'LOGIN FAILED'; badge.style.background = '#d29922'; badge.style.color = '#000';
            if (bar) { bar.style.background = '#d29922'; }
        } else if (s === 'failed' || s === 'error') {
            badge.textContent = 'FAILED'; badge.style.background = '#da3633'; badge.style.color = '#fff';
            if (bar) { bar.style.background = '#f85149'; }
        } else if (s === 'generating' || s === 'uploading' || s === 'report_ok') {
            badge.textContent = s === 'generating' ? 'Generating' : s === 'report_ok' ? 'Report OK' : 'Uploading';
            badge.style.background = '#1f6feb'; badge.style.color = '#fff';
            if (bar) bar.style.background = '#58a6ff';
        }
    }
    // Hide skip button for completed/failed/skipped items
    const isDone = data.status === 'ok' || data.status === 'failed' || data.status === 'error' || data.status === 'skipped';
    if (skipBtn && isDone) skipBtn.style.display = 'none';
}

function _updateUploadCard(idx, data) {
    // Store state persistently
    if (idx < _uploadItemStates.length) {
        _uploadItemStates[idx] = { ...data, idx };
    }
    _applyCardState(idx, data);
}

async function _skipUploadItem(idx) {
    try {
        await apiPost('/api/qbench-skip-item', { idx });
    } catch (e) {
        showNotification('Skip failed: ' + e.message, 'error');
    }
}

async function stopQBenchUpload() {
    try {
        await apiPost('/api/qbench-cancel');
        showNotification('Upload cancel requested', 'info');
        _uploadActive = false;
        const startBtn = document.getElementById('btn-qbench-start');
        const stopBtn = document.getElementById('btn-qbench-stop');
        if (startBtn) { startBtn.disabled = false; startBtn.textContent = 'Start Upload'; }
        if (stopBtn) stopBtn.disabled = true;
    } catch (e) {
        showNotification('Cancel failed: ' + e.message, 'error');
    }
}

/** Show / update / hide the persistent upload pill in the toolbar. */
function _updateUploadIndicator(mode, text, pct) {
    const el = document.getElementById('upload-indicator');
    const txt = document.getElementById('upload-indicator-text');
    const fill = document.getElementById('upload-indicator-fill');
    if (!el) return;

    if (mode === 'hide') {
        el.style.display = 'none';
        return;
    }
    el.style.display = 'flex';
    if (txt) txt.textContent = text || 'Uploading…';

    if (fill) {
        if (mode === 'done') {
            fill.style.width = '100%';
            fill.style.background = 'linear-gradient(90deg,#238636,#3fb950)';
        } else if (mode === 'error') {
            fill.style.width = '100%';
            fill.style.background = '#f85149';
        } else if (mode === 'partial') {
            fill.style.width = '100%';
            fill.style.background = '#d29922';
        } else {
            fill.style.width = (pct || 0) + '%';
            fill.style.background = 'linear-gradient(90deg,#1f6feb,#58a6ff)';
        }
    }
}

function connectUploadSSE() {
    if (uploadSSE) { uploadSSE.close(); uploadSSE = null; }
    uploadSSE = new EventSource('/api/qbench-upload/stream');

    // Show the toolbar indicator immediately
    _updateUploadIndicator('progress', 'Uploading…', 0);

    uploadSSE.onmessage = (e) => {
        let data;
        try { data = JSON.parse(e.data); } catch (_) { return; }

        const statusDiv = document.getElementById('qbench-status');
        const overallFill = document.getElementById('qbench-overall-fill');

        if (data.t === 'item') {
            _updateUploadCard(data.idx, data);
            if (statusDiv) statusDiv.textContent = `[${data.idx + 1}/${data.total}] ${data.lab_id}: ${data.msg || data.status}`;
            const done = _uploadItemStates.filter(s =>
                s.status === 'ok' || s.status === 'failed' || s.status === 'error' || s.status === 'skipped'
            ).length;
            const pct = data.total > 0 ? Math.round((done / data.total) * 100) : 0;
            if (overallFill && data.total > 0) overallFill.style.width = pct + '%';
            _updateUploadIndicator('progress', `Uploading ${data.idx + 1}/${data.total}: ${data.lab_id}`, pct);

        } else if (data.t === 'items_added') {
            // New items were appended by another "Start Upload" call
            for (const it of data.items) {
                if (it.idx >= _uploadItemStates.length) {
                    _uploadItemStates.push({
                        idx: it.idx, lab_id: it.lab_id, status: 'waiting',
                        step: 0, steps: 1, msg: 'Waiting',
                    });
                }
            }
            _rebuildUploadCardsFromState();

        } else if (data.t === 'overall') {
            if (statusDiv) {
                const color = data.status === 'done' ? '#3fb950'
                    : data.status === 'allfailed' ? '#f85149'
                    : data.status === 'partial' ? '#d29922'
                    : data.status === 'credentials_needed' ? '#d29922'
                    : data.status === 'precheck_failed' ? '#f85149'
                    : '#7d8590';
                statusDiv.innerHTML = `<span style="color:${color}; font-weight:600;">${escapeHtml(data.msg)}</span>`;
            }

            // Soft precheck failed — show notification globally (even if popup closed)
            if (data.status === 'precheck_failed') {
                showNotification('QBench upload error: ' + data.msg, 'error');
                _updateUploadIndicator('error', 'Upload error — check details');
                return;
            }
            if (data.status === 'credentials_needed') {
                _showCredentialPrompt();
                _updateUploadIndicator('error', 'Login failed — enter credentials');
                return;
            }
            if (data.status === 'credentials_updated') {
                _hideCredentialPrompt();
                _updateUploadIndicator('progress', 'Retrying with new credentials…', 0);
                return;
            }
            if (data.status === 'done' || data.status === 'allfailed' || data.status === 'partial' || data.status === 'cancelled') {
                _uploadActive = false;
                if (overallFill) overallFill.style.width = '100%';
                if (data.status !== 'done') {
                    if (overallFill) overallFill.style.background = data.status === 'allfailed' ? '#f85149' : '#d29922';
                }
                const startBtn = document.getElementById('btn-qbench-start');
                const stopBtn = document.getElementById('btn-qbench-stop');
                if (startBtn) { startBtn.disabled = false; startBtn.textContent = 'Start Upload'; }
                if (stopBtn) stopBtn.disabled = true;
                if (uploadSSE) { uploadSSE.close(); uploadSSE = null; }

                const imode = data.status === 'done' ? 'done'
                    : data.status === 'allfailed' ? 'error' : 'partial';
                const summary = data.status === 'done'
                    ? `All ${data.total} uploaded`
                    : data.status === 'allfailed'
                    ? `Upload failed (${data.total})`
                    : `${data.ok} OK, ${data.fail} failed`;
                _updateUploadIndicator(imode, summary);
                setTimeout(() => _updateUploadIndicator('hide'), 8000);

                // Show notification when done (visible even if modal is closed)
                if (data.status === 'done') {
                    showNotification(summary, 'success');
                } else {
                    showNotification('QBench: ' + summary, data.status === 'allfailed' ? 'error' : 'info');
                }
            }
        }
    };
    uploadSSE.onerror = () => {
        if (uploadSSE) { uploadSSE.close(); uploadSSE = null; }
        // An EventSource can't see a 401: ask /api/session, which sends the
        // page to /login when the session has ended (session.js).
        if (window.GCSession && window.GCSession.check) window.GCSession.check();
        const startBtn = document.getElementById('btn-qbench-start');
        if (startBtn) { startBtn.disabled = false; startBtn.textContent = 'Start Upload'; }
        _updateUploadIndicator('error', 'Upload connection lost');
        setTimeout(() => _updateUploadIndicator('hide'), 6000);
    };
}

/** Show credential re-prompt — opens the QBench modal, expands credentials, shows submit button. */
function _showCredentialPrompt() {
    const modal = document.getElementById('modal-qbench');
    if (modal) openModal(modal);

    const details = modal?.querySelector('details');
    if (details) details.open = true;

    let banner = document.getElementById('qb-reauth-banner');
    if (!banner) {
        const credsSection = modal?.querySelector('details > div');
        if (credsSection) {
            banner = document.createElement('div');
            banner.id = 'qb-reauth-banner';
            banner.style.cssText = 'background:#d29922; color:#000; padding:8px 12px; border-radius:6px; margin-bottom:8px; font-size:12px; font-weight:600; display:flex; flex-direction:column; gap:6px;';
            banner.innerHTML = `
                <span>Login failed — please re-enter your QBench credentials and click Submit.</span>
                <button id="btn-qb-submit-creds" class="btn btn-success" style="font-size:11px; padding:4px 12px; align-self:flex-start;">Submit Credentials</button>
            `;
            credsSection.prepend(banner);
            document.getElementById('btn-qb-submit-creds')?.addEventListener('click', submitQBenchCredentials);
        }
    }
    banner?.style.setProperty('display', 'flex');

    const pwField = document.getElementById('qb-password');
    if (pwField) { pwField.value = ''; pwField.focus(); }

    showNotification('QBench login failed — please re-enter your credentials in the upload dialog', 'error');
    try { window.focus(); } catch (_) {}
}

function _hideCredentialPrompt() {
    const banner = document.getElementById('qb-reauth-banner');
    if (banner) banner.style.display = 'none';
}

async function submitQBenchCredentials() {
    const username = (document.getElementById('qb-username')?.value || '').trim();
    const password = (document.getElementById('qb-password')?.value || '').trim();
    if (!username || !password) {
        showNotification('Please enter both username and password', 'error');
        return;
    }
    try {
        await apiPost('/api/qbench-update-credentials', { username, password });
        _hideCredentialPrompt();
        showNotification('Credentials submitted — retrying upload...', 'success');
    } catch (e) {
        showNotification('Failed to submit credentials: ' + e.message, 'error');
    }
}

function clearAnalysisQueue() {
    state.analysisQueue = [];
    renderAnalysisQueue();
    showNotification('Queue cleared', 'info');
}

/* ===================================================================
   14. TAB MANAGEMENT
   =================================================================== */

let currentTab = 0;

function switchTab(tabIndex) {
    currentTab = tabIndex;

    // Update tab buttons
    document.querySelectorAll('.tab-btn').forEach((th, i) => {
        th.classList.toggle('active', i === tabIndex);
    });

    // Update tab panes — CSS handles display via .active class
    document.querySelectorAll('.tab-pane').forEach((tp, i) => {
        tp.classList.toggle('active', i === tabIndex);
    });

    // Re-render file lists to sync selection highlighting in newly visible tab
    renderAllFileLists();
    // live changes arrived while the table was hidden
    if (tabIndex === DISTILL_DATA_TAB && _tableStale) loadTableData({ bg: true });

    // Tab-specific actions when switching with a selected sample
    if (state.selectedFile) {
        if (tabIndex === 0) {
            // Dashboard — load dashboard data for the selected sample
            loadDashboardData(state.selectedFile);
        } else if (tabIndex === 4) {
            // Analysis tab — sync analysis sample label and trigger analysis
            const label = document.getElementById('analysis-sample-label');
            if (label) label.textContent = state.selectedFile.name;
            state.selectedSample = state.selectedFile;
            updateAnalysisOverlay();
            maybeAutoRunAnalysis();
        }
        // Tabs 1-3 (chrom, distill data, dc): selection is shown but no heavy work triggered
    }

    // Resize any Plotly charts in the newly active tab
    requestAnimationFrame(() => {
        const activePane = document.querySelectorAll('.tab-pane')[tabIndex];
        if (activePane) {
            activePane.querySelectorAll('[id$="-plot"]').forEach(div => {
                if (div.data) Plotly.Plots.resize(div);
            });
        }
    });
}

function updateAdvancedViewsVisibility() {
    // Inline CSS: .tab-btn.advanced-tab { display: none; }
    // .tab-btn.advanced-tab.visible { display: inline-block; }
    document.querySelectorAll('.tab-btn.advanced-tab').forEach(el => {
        el.classList.toggle('visible', state.advancedViewsVisible);
    });
    // If current tab is hidden, switch to dashboard
    if (!state.advancedViewsVisible && currentTab >= 1 && currentTab <= 3) {
        switchTab(0);
    }
}

function toggleAdvancedViews() {
    state.advancedViewsVisible = !state.advancedViewsVisible;
    updateAdvancedViewsVisibility();
    const btn = document.getElementById('btn-advanced-views');
    if (btn) {
        btn.classList.toggle('active', state.advancedViewsVisible);
        btn.title = state.advancedViewsVisible ? 'Hide Advanced Views' : 'Show Advanced Views';
    }
}

/* ===================================================================
   15. MODALS
   =================================================================== */

function openModal(modalEl) {
    if (!modalEl) return;
    // modalEl is a .modal-overlay wrapper — inline CSS uses .open to show
    modalEl.classList.add('open');
}

function closeModal(modalEl) {
    if (!modalEl) return;
    // If we received the inner .modal div, find the overlay parent
    if (!modalEl.classList.contains('modal-overlay')) {
        modalEl = modalEl.closest('.modal-overlay') || modalEl;
    }
    if (modalEl) modalEl.classList.remove('open');
}

function closeAllModals() {
    document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
}

function setupModalCloseHandlers() {
    // Close on X button (.modal-close)
    document.querySelectorAll('.modal-close').forEach(btn => {
        btn.addEventListener('click', () => {
            const overlay = btn.closest('.modal-overlay');
            if (overlay) closeModal(overlay);
        });
    });

    // Close on any [data-dismiss="modal"] button
    document.querySelectorAll('[data-dismiss="modal"]').forEach(btn => {
        btn.addEventListener('click', () => {
            const overlay = btn.closest('.modal-overlay');
            if (overlay) closeModal(overlay);
        });
    });

    // Close on overlay background click
    document.querySelectorAll('.modal-overlay').forEach(overlay => {
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) closeModal(overlay);
        });
    });

    // Close on Escape key
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeAllModals();
    });
}

/* ===================================================================
   16. SETTINGS MODAL
   =================================================================== */

function openSettingsModal() {
    const modal = document.getElementById('modal-settings');
    if (!modal) return;

    // "Restart & install vX.Y.Z" when the updater has a newer release staged
    refreshRestartLabel();

    // Populate fields
    const fieldsMap = {
        'set-watch-dir': 'watch_dir',
        'set-processed-dir': 'processed_cdf_dir',
        'set-distill-output': 'distill_output',
        'set-calibration-cdf': 'calibration_cdf',
        'set-export-folder': 'export_folder',
        'set-comparison-dir': 'comparison_defaults_dir',
        'set-correction-factors': 'correction_factors_json',
        'set-series-colors': 'series_colors',
        'set-report-logo': 'analysis_report_logo',
    };
    for (const [elId, key] of Object.entries(fieldsMap)) {
        const el = document.getElementById(elId);
        if (el) el.value = state.settings[key] || '';
    }

    // Populate the flag-rules editor (migrates legacy early-signal settings)
    renderFlagRulesEditor(effectiveFlagRules(state.settings));

    // Populate best-fit fields
    const bfEnabled = document.getElementById('set-bestfit-enabled');
    if (bfEnabled) bfEnabled.checked = (state.settings.bestfit_enabled || 'true').toLowerCase() === 'true';
    const bfMap = {
        'set-bestfit-threshold': ['bestfit_threshold', '0.93'],
        'set-bestfit-shift': ['bestfit_shift_tolerance_min', '0.05'],
        'set-bestfit-minfrac': ['bestfit_mix_min_frac', '0.10'],
        // deviation bullets (phase 3)
        'set-analysis-min-width': ['analysis_min_width_min', '0.05'],
        'set-analysis-merge-gap': ['analysis_merge_gap_min', '0.10'],
        'set-analysis-spike-width': ['analysis_spike_min_width_min', '0.02'],
        'set-analysis-spike-report': ['analysis_spike_report_threshold', ''],
        'set-analysis-spike-fwhm': ['analysis_spike_max_fwhm_min', '0.20'],
        'set-analysis-spike-dominance': ['analysis_spike_min_dominance', '0.6'],
    };
    for (const [elId, [key, dflt]] of Object.entries(bfMap)) {
        const el = document.getElementById(elId);
        if (el) el.value = state.settings[key] || dflt;
    }

    // Populate comparison standards list in settings
    renderSettingsStandards();

    // QBench API credentials: status only; the secret is never sent back.
    _clearQbApiSecrets();
    const qbMsg = document.getElementById('qb-api-message');
    if (qbMsg) qbMsg.textContent = '';
    refreshQbApiStatus();

    openModal(modal);
}

/* ── QBench API credentials (Settings modal) ───────────────────────── */

function _renderQbApiStatus(status) {
    const el = document.getElementById('qb-api-status');
    if (el) el.textContent = qbApiStatusText(status);
    const pathEl = document.getElementById('qb-api-store-path');
    if (pathEl) pathEl.textContent = (status && status.store_path) || '';
}

async function refreshQbApiStatus() {
    try {
        _renderQbApiStatus(await apiGet('/api/qbench-api-credentials'));
    } catch (e) {
        const el = document.getElementById('qb-api-status');
        if (el) el.textContent = `Unknown (${e.message})`;
    }
}

function _clearQbApiSecrets() {
    for (const id of ['qb-api-client-secret', 'qb-api-admin']) {
        const el = document.getElementById(id);
        if (el) el.value = '';
    }
}

async function saveQbApiCredentials() {
    const idEl = document.getElementById('qb-api-client-id');
    const secretEl = document.getElementById('qb-api-client-secret');
    const adminEl = document.getElementById('qb-api-admin');
    const msg = document.getElementById('qb-api-message');
    const btn = document.getElementById('btn-qb-api-save');
    const body = {
        client_id: idEl ? idEl.value.trim() : '',
        client_secret: secretEl ? secretEl.value : '',
        password: adminEl ? adminEl.value : '',
    };
    // Clear the secret and admin password now, whatever the outcome.
    _clearQbApiSecrets();
    if (msg) { msg.style.color = '#7d8590'; msg.textContent = 'Checking with QBench\u2026'; }
    if (btn) btn.disabled = true;
    // The server gives QBench ~15 s at most; don't leave the button disabled
    // forever if the request itself stalls.
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 20000);
    try {
        const status = await api('POST', '/api/qbench-api-credentials', body, { signal: ctl.signal });
        _renderQbApiStatus(status);
        if (idEl) idEl.value = '';
        if (msg) {
            msg.style.color = '#3fb950';
            msg.textContent = status.source === 'environment'
                ? 'Saved, but environment variables on the server override it.'
                : 'QBench accepted the credentials. Saved.';
        }
    } catch (e) {
        const text = e.name === 'AbortError'
            ? 'No answer from the server in 20 s. Reopen Settings to check the status before trying again.'
            : e.message;
        if (msg) { msg.style.color = '#f85149'; msg.textContent = text; }
    } finally {
        clearTimeout(timer);
        if (btn) btn.disabled = false;
    }
}

/* ── Flag-rules editor (Settings modal) ────────────────────────────── */

function renderFlagRulesEditor(rules) {
    const list = document.getElementById('flag-rules-list');
    if (!list) return;
    list.innerHTML = '';
    rules.forEach((rule, i) => {
        const row = document.createElement('div');
        row.className = 'flag-rule-row';
        row.style.cssText = 'display:flex; align-items:center; gap:6px; margin-bottom:5px; flex-wrap:wrap;';
        row.innerHTML = `
            <input type="checkbox" class="fr-enabled" title="Enabled" style="width:auto;" ${rule.enabled ? 'checked' : ''}>
            <input type="text" class="fr-name" title="Rule name" value="${escapeHtml(rule.name)}" style="width:130px; font-size:11px;">
            <select class="fr-condition" title="Condition" style="width:78px; font-size:11px;">
                <option value="above" ${rule.condition === 'above' ? 'selected' : ''}>Above</option>
                <option value="below" ${rule.condition === 'below' ? 'selected' : ''}>Below</option>
            </select>
            <input type="number" class="fr-threshold" title="Intensity threshold" value="${rule.threshold}" step="100" style="width:80px; font-size:11px;">
            <span style="font-size:11px; color:#7d8590;">from</span>
            <input type="number" class="fr-t-start" title="Window start (min)" value="${rule.t_start}" step="0.1" style="width:58px; font-size:11px;">
            <span style="font-size:11px; color:#7d8590;">to</span>
            <input type="number" class="fr-t-end" title="Window end (min)" value="${rule.t_end}" step="0.1" style="width:58px; font-size:11px;">
            <span style="font-size:11px; color:#7d8590;">min</span>
            <input type="color" class="fr-color" title="Tag color" value="${rule.color || '#e67e22'}" style="width:28px; height:22px; padding:0;">
            <button class="btn btn-secondary fr-remove" title="Remove rule" style="font-size:11px; padding:1px 7px;">✕</button>`;
        row.querySelector('.fr-remove').addEventListener('click', () => row.remove());
        list.appendChild(row);
    });
}

/** Read the editor rows back into a rule list; invalid rows are dropped.
    Returns null when the editor isn't in the DOM (modal markup missing). */
function readFlagRulesFromDOM() {
    const list = document.getElementById('flag-rules-list');
    if (!list) return null;
    const rules = [];
    list.querySelectorAll('.flag-rule-row').forEach(row => {
        const rule = cleanFlagRule({
            name: row.querySelector('.fr-name')?.value,
            condition: row.querySelector('.fr-condition')?.value,
            threshold: row.querySelector('.fr-threshold')?.value,
            t_start: row.querySelector('.fr-t-start')?.value,
            t_end: row.querySelector('.fr-t-end')?.value,
            color: row.querySelector('.fr-color')?.value,
            enabled: row.querySelector('.fr-enabled')?.checked,
        });
        if (rule) rules.push(rule);
    });
    return rules;
}

function addFlagRuleRow() {
    const list = document.getElementById('flag-rules-list');
    if (!list) return;
    const existing = readFlagRulesFromDOM() || [];
    existing.push(newFlagRule(list.querySelectorAll('.flag-rule-row').length));
    renderFlagRulesEditor(existing);
}

function renderSettingsStandards() {
    const list = document.getElementById('settings-standards-list');
    if (!list) return;
    list.innerHTML = '';
    if (state.comparisonStandards.length === 0) {
        list.innerHTML = '<li style="color:#7d8590; padding:6px 8px; font-size:11px;">No comparison standards</li>';
        return;
    }
    for (const std of state.comparisonStandards) {
        const li = document.createElement('li');
        li.style.cssText = 'display:flex; align-items:center; justify-content:space-between; padding:3px 8px;';
        li.innerHTML = `
            <span style="font-size:11px; color:#c9d1d9;">${escapeHtml(std.name)}</span>
            <button class="range-delete" title="Remove" data-std="${escapeHtml(std.name)}">&times;</button>
        `;
        li.querySelector('.range-delete').addEventListener('click', async () => {
            if (!confirm(`Remove standard "${std.name}"?`)) return;
            try {
                const password = adminPassword('delete a comparison standard');
                if (!password) return;
                await api('DELETE', '/api/comparison-standard/' + encodeURIComponent(std.name), { password });
                await loadComparisonStandards();
                renderSettingsStandards();
                showNotification(`Removed: ${std.name}`, 'success');
            } catch (err) {
                showNotification('Failed: ' + err.message, 'error');
            }
        });
        list.appendChild(li);
    }
}

async function saveSettings() {
    // v2 (T5 review C1): only operator keys save freely; best-fit needs the
    // admin password; paths, calibration and corrections are read-only here.
    // The Analysis tab's defaults are saved by "Set as Default" (admin).
    const body = {};
    const colors = document.getElementById('set-series-colors');
    if (colors) body.series_colors = colors.value;

    // Flag rules (replaces the legacy early-signal fields)
    const flagRules = readFlagRulesFromDOM();
    if (flagRules !== null) body.sample_flag_rules = JSON.stringify(flagRules);

    // Best-fit settings: an export column, so admin only
    const bf = {};
    const bfEnabled = document.getElementById('set-bestfit-enabled');
    if (bfEnabled) bf.bestfit_enabled = bfEnabled.checked ? 'true' : 'false';
    const bfSaveMap = {
        'set-bestfit-threshold': 'bestfit_threshold',
        'set-bestfit-shift': 'bestfit_shift_tolerance_min',
        'set-bestfit-minfrac': 'bestfit_mix_min_frac',
        'set-analysis-min-width': 'analysis_min_width_min',
        'set-analysis-merge-gap': 'analysis_merge_gap_min',
        'set-analysis-spike-width': 'analysis_spike_min_width_min',
        'set-analysis-spike-fwhm': 'analysis_spike_max_fwhm_min',
        'set-analysis-spike-dominance': 'analysis_spike_min_dominance',
    };
    for (const [elId, key] of Object.entries(bfSaveMap)) {
        const el = document.getElementById(elId);
        if (el && el.value !== '') bf[key] = el.value;
    }
    // empty is meaningful here (= the moderate threshold)
    const spikeReport = document.getElementById('set-analysis-spike-report');
    if (spikeReport) bf.analysis_spike_report_threshold = spikeReport.value.trim();
    const bfChanged = Object.entries(bf).some(
        ([k, v]) => String(v) !== String(state.settings[k] == null ? '' : state.settings[k]));
    if (bfChanged) {
        const password = adminPassword('change the best-fit / deviation-bullet settings');
        if (!password) { showNotification('Best-fit / deviation-bullet settings not saved (no admin password)', 'info'); }
        else { Object.assign(body, bf); body.password = password; }
    }

    try {
        const saved = await apiPost('/api/settings', body);
        state.settings = saved;
        closeAllModals();
        showNotification('Settings saved — refreshing data...', 'success');
        await refreshAll();
        showNotification('Data refreshed with new settings', 'success');
    } catch (e) {
        showNotification('Failed to save settings: ' + e.message, 'error');
    }
}

/* ===================================================================
   18. REPROCESS MODAL
   =================================================================== */

// Holds the latest preview so Confirm reprocesses exactly what was shown
// (sample_ids: the latest injection of each matched Lab ID).
let _reprocessPreview = { matched: [], missing: [], sample_ids: [] };

/** The instrument a typed Lab-ID selection applies to: the modal's picker
    (required: two instruments can hold the same lab ID). */
function reprocessInstrument() {
    const sel = document.getElementById('reprocess-instrument');
    return (sel && sel.value) || '';
}

function renderReprocessInstrumentPicker() {
    const sel = document.getElementById('reprocess-instrument');
    if (!sel) return;
    sel.innerHTML = '';
    const ids = state.instruments || [];
    const choose = document.createElement('option');
    choose.value = '';
    choose.textContent = 'Choose…';
    sel.appendChild(choose);
    for (const id of ids) {
        const o = document.createElement('option');
        o.value = id;
        o.textContent = instrumentName(id, state.instrumentNames);
        sel.appendChild(o);
    }
    sel.value = reprocessDefaultInstrument(ids, state.listInstrument);
}

function openReprocessModal() {
    const modal = document.getElementById('modal-reprocess');
    if (!modal) return;
    const input = document.getElementById('reprocess-ids');
    if (input) input.value = '';
    renderReprocessInstrumentPicker();
    _reprocessPreview = { matched: [], missing: [], sample_ids: [] };
    renderReprocessPreview();
    openModal(modal);
}

/** Ask the backend to expand the query (IDs, lists, ranges) against the
 *  library, then render which samples match and which IDs are missing. */
async function previewReprocess() {
    const input = document.getElementById('reprocess-ids');
    if (!input) return;
    const query = input.value.trim();
    if (!query) {
        _reprocessPreview = { matched: [], missing: [], sample_ids: [] };
        renderReprocessPreview();
        return;
    }
    const instrument = reprocessInstrument();
    const instErr = reprocessInstrumentError(instrument);
    if (instErr) {
        _reprocessPreview = { matched: [], missing: [], sample_ids: [] };
        renderReprocessPreview(instErr);
        return;
    }
    try {
        const result = await apiPost('/api/reprocess/preview', { query, instrument });
        if (result && result.error) {
            _reprocessPreview = { matched: [], missing: [], sample_ids: [] };
            renderReprocessPreview(result.error);
            return;
        }
        _reprocessPreview = {
            matched: result.matched || [],
            missing: result.missing || [],
            sample_ids: result.sample_ids || [],
        };
        renderReprocessPreview();
    } catch (e) {
        renderReprocessPreview('Preview failed: ' + e.message);
    }
}

function renderReprocessPreview(errorMsg) {
    const matchedWrap = document.getElementById('reprocess-matched-wrap');
    const missingWrap = document.getElementById('reprocess-missing-wrap');
    const emptyEl = document.getElementById('reprocess-preview-empty');
    const runBtn = document.getElementById('btn-reprocess-run');
    if (!matchedWrap || !missingWrap || !emptyEl) return;

    const { matched, missing } = _reprocessPreview;

    // Matched list
    if (matched.length) {
        document.getElementById('reprocess-matched-count').textContent = matched.length;
        const ul = document.getElementById('reprocess-matched-list');
        ul.innerHTML = '';
        for (const name of matched) {
            const li = document.createElement('li');
            li.textContent = name;
            ul.appendChild(li);
        }
        matchedWrap.style.display = '';
    } else {
        matchedWrap.style.display = 'none';
    }

    // Missing list
    if (missing.length) {
        document.getElementById('reprocess-missing-count').textContent = missing.length;
        document.getElementById('reprocess-missing-list').textContent = missing.join(', ');
        missingWrap.style.display = '';
    } else {
        missingWrap.style.display = 'none';
    }

    // Empty / error state
    if (errorMsg) {
        emptyEl.textContent = errorMsg;
        emptyEl.style.display = '';
    } else if (!matched.length && !missing.length) {
        emptyEl.textContent = 'No matching samples.';
        emptyEl.style.display = 'none';
    } else if (!matched.length) {
        emptyEl.textContent = 'No matching samples.';
        emptyEl.style.display = '';
    } else {
        emptyEl.style.display = 'none';
    }

    if (runBtn) {
        runBtn.disabled = matched.length === 0;
        runBtn.textContent = matched.length
            ? `Re-process ${matched.length} sample(s)`
            : 'Re-process';
    }
}

async function submitReprocess() {
    const { matched, missing, sample_ids } = _reprocessPreview;
    if (!sample_ids.length) {
        showNotification('No matching samples to re-process', 'info');
        return;
    }
    closeAllModals();

    try {
        const result = await apiPost('/api/reprocess', { sample_ids, missing });
        if (result.refused && result.refused.length) {
            showNotification(refusalSummary('Not reprocessed', result.refused, state.files), 'error');
        }

        // Show a persistent toast in top-right (not a full modal)
        _showReprocessToast(result.count || matched.length, 0);

        // Poll the queued samples until their jobs are done
        _pollReprocessStatus(result.sample_ids || []);

        // Missing IDs were logged server-side — refresh the tray.
        if (missing.length) loadNotifications();
    } catch (e) {
        showNotification('Reprocess failed: ' + e.message, 'error');
    }
}

/* ===================================================================
   18b. NOTIFICATION TRAY (persistent system messages)
   =================================================================== */

function toggleNotifPanel(forceOpen) {
    const panel = document.getElementById('notif-panel');
    if (!panel) return;
    const show = forceOpen !== undefined ? forceOpen : panel.style.display === 'none';
    panel.style.display = show ? 'block' : 'none';
    if (show) loadNotifications();
}

async function loadNotifications(opts) {
    try {
        const bg = !!(opts && opts.bg);
        const notes = await (bg ? apiGetBg('/api/notifications') : apiGet('/api/notifications'));
        renderNotifications(Array.isArray(notes) ? notes : []);
    } catch (_) { /* tray is best-effort */ }
}

function renderNotifications(notes) {
    const badge = document.getElementById('notif-badge');
    if (badge) {
        if (notes.length) {
            badge.textContent = notes.length > 99 ? '99+' : notes.length;
            badge.style.display = '';
        } else {
            badge.style.display = 'none';
        }
    }

    const list = document.getElementById('notif-list');
    if (!list) return;
    list.innerHTML = '';
    if (!notes.length) {
        const li = document.createElement('li');
        li.style.cssText = 'padding:14px 12px; color:#7d8590; font-size:12px; text-align:center;';
        li.textContent = 'No notifications';
        list.appendChild(li);
        return;
    }

    const levelColor = { info: '#58a6ff', success: '#3fb950', warning: '#d29922', error: '#f85149' };
    for (const n of notes) {
        const li = document.createElement('li');
        li.style.cssText = 'display:flex; gap:8px; padding:8px 12px; border-bottom:1px solid #21262d; align-items:flex-start;';
        li.style.borderLeft = `3px solid ${levelColor[n.level] || '#58a6ff'}`;

        const body = document.createElement('div');
        body.style.cssText = 'flex:1; min-width:0;';
        const msg = document.createElement('div');
        msg.style.cssText = 'font-size:12px; color:#e6edf3; word-break:break-word;';
        msg.textContent = n.message;
        const ts = document.createElement('div');
        ts.style.cssText = 'font-size:10px; color:#7d8590; margin-top:2px;';
        ts.textContent = (n.ts || '').replace('T', ' ');
        body.appendChild(msg);
        body.appendChild(ts);

        const x = document.createElement('button');
        x.className = 'modal-close';
        x.style.cssText = 'font-size:16px; line-height:1; flex-shrink:0; background:none; border:none; color:#7d8590; cursor:pointer;';
        x.title = 'Dismiss';
        x.innerHTML = '&times;';
        x.addEventListener('click', (e) => { e.stopPropagation(); dismissNotification(n.id); });

        li.appendChild(body);
        li.appendChild(x);
        list.appendChild(li);
    }
}

async function dismissNotification(id) {
    try {
        await apiPost(`/api/notifications/${id}/dismiss`);
        loadNotifications();
    } catch (_) { /* ignore */ }
}

async function dismissAllNotifications() {
    try {
        await apiPost('/api/notifications/dismiss-all');
        loadNotifications();
    } catch (_) { /* ignore */ }
}

/** Show a persistent, dismissable toast for reprocess progress. */
function _showReprocessToast(count, pending) {
    // Remove any existing reprocess toast
    const existing = document.getElementById('reprocess-toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.id = 'reprocess-toast';
    toast.style.cssText = `
        position:fixed; top:16px; right:16px; z-index:10001;
        background:#161b22; border:1px solid #30363d; border-left:3px solid #58a6ff;
        border-radius:6px; padding:10px 16px; min-width:260px; max-width:380px;
        box-shadow:0 4px 16px rgba(0,0,0,0.4); font-size:12px; color:#e6edf3;
        display:flex; flex-direction:column; gap:6px;
    `;
    const statusText = pending > 0
        ? `Reprocessing: waiting for ${pending} task(s) ahead...`
        : `Reprocessing ${count} sample(s)...`;
    toast.innerHTML = `
        <div style="display:flex; align-items:center; justify-content:space-between;">
            <span id="reprocess-toast-title" style="font-weight:600;">${statusText}</span>
            <button id="reprocess-toast-close" style="display:none; background:none; border:none; color:#7d8590; font-size:16px; cursor:pointer; padding:0 4px;">&times;</button>
        </div>
        <div style="height:4px; background:#21262d; border-radius:2px; overflow:hidden;">
            <div id="reprocess-toast-bar" style="width:0%; height:100%; background:#58a6ff; border-radius:2px; transition:width .5s;"></div>
        </div>
        <div id="reprocess-toast-detail" style="font-size:10px; color:#7d8590;"></div>
    `;
    document.body.appendChild(toast);

    // Close button handler
    toast.querySelector('#reprocess-toast-close').addEventListener('click', () => toast.remove());
}

/** Follow the reprocess of *sampleIds* in the persistent toast: its status
    is asked once now and again whenever GCLive reports one of the samples
    changed (the Worker publishes each one it processes), not on a timer. */
function _pollReprocessStatus(sampleIds) {
    const ids = (sampleIds || []).join(',');
    const wanted = new Set((sampleIds || []).map(Number));
    let unsubscribe = () => {};
    let _timer = null;
    let busy = false, again = false, stopped = false;
    const stop = () => { stopped = true; unsubscribe(); if (_timer) clearInterval(_timer); };
    const check = async () => {
        if (stopped) return;
        if (busy) { again = true; return; }
        busy = true;
        try { await checkOnce(); } finally {
            busy = false;
            if (again) { again = false; check(); }
        }
    };
    if (typeof GCLive !== 'undefined') {
        unsubscribe = GCLive.subscribe(u => {
            if (u.reset || (u.samples || []).some(id => wanted.has(Number(id)))) check();
        });
    } else {
        _timer = setInterval(check, 1500);         // live.js missing: the old poll
    }
    check();

    async function checkOnce() {
        const toast = document.getElementById('reprocess-toast');
        if (!toast) { stop(); return; }

        try {
            const st = await apiGetBg(`/api/reprocess/status?sample_ids=${encodeURIComponent(ids)}`);
            const titleEl = toast.querySelector('#reprocess-toast-title');
            const barEl = toast.querySelector('#reprocess-toast-bar');
            const detailEl = toast.querySelector('#reprocess-toast-detail');
            const closeBtn = toast.querySelector('#reprocess-toast-close');

            if (st.phase === 'processing') {
                if (titleEl) titleEl.textContent = 'Reprocessing...';
                if (st.total > 0 && barEl) {
                    const pct = Math.round(((st.processed + st.errors) / st.total) * 100);
                    barEl.style.width = pct + '%';
                }
                if (detailEl) detailEl.textContent = `${st.processed || 0} done, ${st.errors || 0} errors`;
            } else if (st.phase === 'done' || st.phase === 'idle'
                       || st.phase === 'stopped' || st.phase === 'error') {
                // Complete — show result and make dismissable
                const accent = st.phase === 'error' ? '#f85149'
                             : st.phase === 'stopped' ? '#d29922' : '#3fb950';
                const label = st.phase === 'error' ? 'Reprocess failed'
                            : st.phase === 'stopped' ? 'Reprocess stopped' : 'Reprocess complete';
                if (titleEl) {
                    titleEl.textContent = label;
                    titleEl.style.color = accent;
                }
                if (barEl) {
                    barEl.style.width = '100%';
                    barEl.style.background = accent;
                }
                if (detailEl) detailEl.textContent = `${st.processed || 0} processed, ${st.errors || 0} errors`;
                if (closeBtn) closeBtn.style.display = '';
                toast.style.borderLeftColor = accent;

                stop();
                // the rows themselves arrived through GCLive; the table follows
                _tableChanged();

                // Auto-dismiss after 8 seconds
                setTimeout(() => { if (toast.parentNode) toast.remove(); }, 8000);
            }
        } catch (_) { /* ignore */ }
    }
}

/* ===================================================================
   19b. SERVER RESTART
   =================================================================== */

// What a restart would do right now ({mode, tag, pid} from a dry run), so
// both Restart buttons can say "Restart & install vX.Y.Z" when the updater
// has a newer release staged.
let _restartDecision = null;

async function refreshRestartLabel() {
    try {
        _restartDecision = await apiPost('/api/restart', { dry_run: true });
    } catch (_) {
        _restartDecision = null;
    }
    const label = restartLabel(_restartDecision);
    const btn = document.getElementById('btn-restart');
    if (btn) btn.textContent = label;
    const tb = document.getElementById('btn-restart-server');
    if (tb) tb.textContent = label === 'Restart' ? 'Restart Server' : label;
}

const RESTART_NOTICE_KEY = 'gc-restart-notice';

function _setRestartButtonsBusy(busy) {
    for (const id of ['btn-restart', 'btn-restart-server']) {
        const b = document.getElementById(id);
        if (!b) continue;
        b.disabled = busy;
        if (busy) b.textContent = 'Restarting…';
    }
    if (!busy) refreshRestartLabel();
}

async function _fetchHealthz(timeoutMs) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
        const resp = await fetch('/healthz', { method: 'GET', cache: 'no-store', signal: ctl.signal });
        if (!resp.ok) return null;
        const j = (await GCSession.readJson(resp)).body;
        return (j && j.status) ? j : null;     // a page instead of /healthz's JSON: not up
    } finally {
        clearTimeout(timer);
    }
}

async function restartServer() {
    await refreshRestartLabel();
    if (!confirm(restartConfirmText(_restartDecision))) {
        return;
    }
    let oldVersion = null;
    try {
        const h = await _fetchHealthz(3000);
        oldVersion = h ? h.version : null;
    } catch (_) { /* not needed to restart */ }
    try {
        const res = await apiPost('/api/restart', {});
        const installing = res && res.mode === 'switch' && res.tag;
        showNotification(installing
            ? `Restarting and installing ${res.tag}… this page will reconnect`
            : 'Restarting… this page will reconnect', 'info');
        _setRestartButtonsBusy(true);
        _waitForServerAndReload(res ? res.pid : null, oldVersion, installing ? res.tag : null);
    } catch (e) {
        showNotification('Restart failed: ' + e.message, 'error');
    }
}

// Poll /healthz every 2 s (each poll abandoned after 3 s) and reload once a
// *different* process answers: while a switch is pending the old process
// keeps serving until the updater stops it, and the updater then
// health-checks the new release before starting it — allow a few minutes.
function _waitForServerAndReload(oldPid, oldVersion, expectedTag) {
    let attempts = 0;
    let busy = false;
    const maxAttempts = 150; // ~5 minutes at 2 s
    const interval = setInterval(async () => {
        if (busy) return;
        attempts++;
        if (attempts > maxAttempts) {
            clearInterval(interval);
            _setRestartButtonsBusy(false);
            showNotification('Server did not come back — try refreshing manually', 'error');
            return;
        }
        busy = true;
        try {
            const body = await _fetchHealthz(3000);
            if (serverReplaced(oldPid, body, oldVersion)) {
                clearInterval(interval);
                const notice = switchOutcomeNotice(expectedTag, body.version);
                if (notice) {
                    try { sessionStorage.setItem(RESTART_NOTICE_KEY, notice); } catch (_) { /* shown now instead */ }
                    showNotification(notice, 'warning');
                }
                // Small extra delay so the server finishes initialising
                setTimeout(() => location.reload(), 1500);
            }
        } catch (_) {
            // Server still down (or poll timed out) — keep polling
        } finally {
            busy = false;
        }
    }, 2000);
}

// A notice carried across the post-restart reload (see above).
function showPendingRestartNotice() {
    let notice = null;
    try {
        notice = sessionStorage.getItem(RESTART_NOTICE_KEY);
        sessionStorage.removeItem(RESTART_NOTICE_KEY);
    } catch (_) { return; }
    if (notice) showNotification(notice, 'warning');
}

/* ===================================================================
   20. EXPORT ACTIONS
   =================================================================== */

async function exportPDF() {
    if (!state.selectedFile) {
        showNotification('Select a file first', 'info');
        return;
    }
    try {
        const resp = await api('POST', '/api/export-pdf', { sample_id: state.selectedFile.sample_id });
        await downloadBlob(resp, 'export.pdf');
        showNotification('PDF exported', 'success');
    } catch (e) {
        showNotification('Export failed: ' + e.message, 'error');
    }
}

async function exportComparison() {
    // Gather the selected sample and every chromatogram on the overlay
    const ids = [];
    if (state.selectedFile) ids.push(state.selectedFile.sample_id);
    for (const tr of state.traces) {
        if (!ids.includes(tr.sample_id)) ids.push(tr.sample_id);
    }
    if (ids.length === 0) {
        showNotification('Select a sample first (Dashboard or Chromatograms tab)', 'info');
        return;
    }
    try {
        const result = await apiPost('/api/export-comparison', { sample_ids: ids });
        if (result && result.files && result.files.length > 0) {
            showNotification(`Comparison exported: ${result.files.length} file(s)`, 'success');
        } else {
            showNotification('No files were generated', 'info');
        }
    } catch (e) {
        showNotification('Comparison export failed: ' + e.message, 'error');
    }
}

async function exportAnalysisReport() {
    if (!state.analysisResult) {
        showNotification('Run an analysis first', 'info');
        return;
    }
    if (!state.selectedSample || !state.selectedStandard) {
        showNotification('Select both a sample and standard first', 'info');
        return;
    }

    const conclusion = document.getElementById('analysis-conclusion')?.value || '';
    const labId = state.selectedSample.lab_id || state.selectedSample.name;

    // Gather overlay standards
    const overlayStds = state.comparisonStandards.map(s => s.path);

    try {
        const resp = await api('POST', '/api/export-analysis-report', {
            sample_id: state.selectedSample.sample_id,
            standard_name: state.selectedStandard.name,
            conclusion,
            doc_name: 'GC Analysis Report',
            lab_id: labId,
            overlay_standards: overlayStds,
            ranges: rangesForPayload(state.rangeOverlays),
            ...state.analysisParams,
        });
        await downloadBlob(resp, `${labId}_analysis_report.pdf`);
        showNotification('Analysis report exported', 'success');
    } catch (e) {
        showNotification('Analysis export failed: ' + e.message, 'error');
    }
}

/* ===================================================================
   21. FOLDER OPEN
   =================================================================== */

/* ===================================================================
   22. HELP MODAL
   =================================================================== */

function openFromQuery() {
    const params = new URLSearchParams(window.location.search);
    const what = params.get('open');
    if (!what) return;
    params.delete('open');
    const rest = params.toString();
    window.history.replaceState(null, '', window.location.pathname + (rest ? '?' + rest : '') + window.location.hash);
    if (what === 'settings') openSettingsModal();
    else if (what === 'help') openHelpModal();
}

function openHelpModal() {
    const modal = document.getElementById('modal-help');
    if (modal) openModal(modal);
}

/* ===================================================================
   23. CORRECTED D86 TOGGLE
   =================================================================== */

function toggleCorrectedD86() {
    const chk = document.getElementById('chk-corrected-d86');
    state.correctedD86 = chk ? chk.checked : !state.correctedD86;
    // Re-render dashboard if a file is selected
    if (state.selectedFile) {
        loadDashboardData(state.selectedFile);
    }
    // Reload distillation table (it already returns corrected values from the backend)
    loadTableData();
}

/* ===================================================================
   24. CHART INITIALIZATION (empty charts on load)
   =================================================================== */

function initCharts() {
    const emptyLayout = basePlotlyLayout();

    const chartIds = [
        'dash-chrom-plot', 'dash-distill-plot',
        'chrom-plot', 'dcurve-plot',
        'analysis-trend-plot', 'analysis-diff-plot',
    ];

    for (const id of chartIds) {
        const el = document.getElementById(id);
        if (el) {
            Plotly.newPlot(el, [], emptyLayout, PLOTLY_CONFIG);
        }
    }
}

/* ===================================================================
   25. EVENT LISTENER SETUP
   =================================================================== */

function setupEventListeners() {
    // Tab buttons
    document.querySelectorAll('.tab-btn').forEach((th, i) => {
        th.addEventListener('click', () => switchTab(i));
    });

    // Toolbar buttons
    const btnMap = {
        'btn-settings': openSettingsModal,
        'btn-export': exportPDF,
        'btn-comparison-export': exportComparison,
        'btn-reprocess': openReprocessModal,
        'btn-help': openHelpModal,
        'btn-restart-server': restartServer,
        'btn-restart': restartServer,
        'btn-advanced-views': toggleAdvancedViews,
        'upload-indicator': () => {
            const m = document.getElementById('modal-qbench');
            if (m) m.classList.add('open');
        },
        'chk-corrected-d86': toggleCorrectedD86,
        'btn-early-signal-filter': toggleEarlySignalFilter,
        'btn-add-flag-rule': addFlagRuleRow,
        // Chromatogram tab
        'btn-chrom-clear': clearChromatogramTraces,
        // DC tab
        'btn-dcurve-clear': clearDCTraces,
        // Analysis tab
        'btn-add-range': addRangeOverlay,
        'btn-annotation-toggle': toggleAnnotationMode,
        'btn-clear-annotations': clearAllAnnotations,
        'btn-run-analysis': runAnalysis,
        'btn-set-defaults': saveAnalysisDefaults,
        'btn-export-analysis': exportAnalysisReport,
        'btn-send-to-queue': addToAnalysisQueue,
        'btn-export-pc': exportToPC,
        'btn-export-qbench': exportQueueToQBench,
        // Settings modal
        'btn-settings-save': saveSettings,
        'btn-qb-api-save': saveQbApiCredentials,
        // Reprocess modal
        'btn-reprocess-run': submitReprocess,
        // Notification tray
        'btn-notif-tray': () => toggleNotifPanel(),
        'btn-notif-dismiss-all': dismissAllNotifications,
        // Settings: library reorder
        // Analysis export modal (Export PDF button inside the modal)
        'btn-export-pdf': confirmAddToQueue,
        // QBench modal
        'btn-qbench-start': startQBenchUpload,
        'btn-qbench-stop': stopQBenchUpload,
        // Queue management
        'btn-queue-clear': clearAnalysisQueue,
    };

    for (const [id, handler] of Object.entries(btnMap)) {
        const el = document.getElementById(id);
        if (!el) continue;
        // Use 'change' event for checkbox inputs
        if (el.tagName === 'INPUT' && el.type === 'checkbox') {
            el.addEventListener('change', handler);
        } else {
            el.addEventListener('click', handler);
        }
    }

    // Universal search input — syncs across all file lists
    const universalSearch = document.getElementById('universal-search');
    if (universalSearch) {
        universalSearch.addEventListener('input', debounce(onSearchInput, 150));
    }

    // Reprocess query — live preview of matching samples
    const reprocInput = document.getElementById('reprocess-ids');
    if (reprocInput) {
        reprocInput.addEventListener('input', debounce(() => previewReprocess(), 250));
    }
    const reprocInst = document.getElementById('reprocess-instrument');
    if (reprocInst) reprocInst.addEventListener('change', () => previewReprocess());

    // Instrument filter (remembered per browser)
    const instFilter = document.getElementById('instrument-filter');
    if (instFilter) instFilter.addEventListener('change', () => onInstrumentFilterChange());

    // Close the notification panel when clicking outside it
    document.addEventListener('click', (e) => {
        const wrap = document.getElementById('notif-tray-wrap');
        const panel = document.getElementById('notif-panel');
        if (wrap && panel && panel.style.display !== 'none' && !wrap.contains(e.target)) {
            panel.style.display = 'none';
        }
    });

    // Distillation table search (separate, not part of sample selection)
    const distillSearch = document.getElementById('distill-search');
    if (distillSearch) {
        distillSearch.addEventListener('input', debounce((e) => {
            tableSearchFilter = e.target.value;
            renderDistillTable();
        }, 150));
    }

    // Analysis parameter inputs (debounced re-analysis)
    const paramIds = [
        'param-baseline', 'param-detail', 'param-smoothing',
        'param-thresh-marginal', 'param-thresh-moderate', 'param-thresh-significant',
        'param-x-max',
    ];
    for (const id of paramIds) {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener('input', debouncedAnalysis);
            el.addEventListener('change', debouncedAnalysis);
        }
    }

    // Initialize tooltips for info buttons
    initParamTooltips();

    // (v2: no server-side Browse; paths are set on the server. Standards are
    // added from a sample's context menu, "Set as comparison standard".)

    // Modal close handlers
    setupModalCloseHandlers();

    // Window resize: resize all visible Plotly charts
    window.addEventListener('resize', debounce(() => {
        document.querySelectorAll('[id$="-plot"]').forEach(div => {
            if (div.data && div.offsetParent !== null) {
                Plotly.Plots.resize(div);
            }
        });
    }, 200));

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        // Ctrl+S — Settings
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            e.preventDefault();
            openSettingsModal();
        }
    });
}

/* ===================================================================
   26. INITIALIZATION
   =================================================================== */

document.addEventListener('DOMContentLoaded', async () => {
    console.log('[GC Viewer] DOMContentLoaded fired');

    // Initialize empty charts first (Plotly elements need to exist)
    initCharts();
    setupAnnotationHandler();
    // Comments section (static/js/comments.js); its changes redraw the
    // annotation spans on the trend plot
    if (typeof Comments !== 'undefined') {
        Comments.init({ onChange: list => _drawAnnotationShapes(list) });
    }
    console.log('[GC Viewer] Charts initialized');

    // Set up event listeners
    setupEventListeners();
    showPendingRestartNotice();

    // Populate analysis parameter inputs with defaults
    populateAnalysisParamInputs();

    // Show the getting-started overlay
    updateAnalysisOverlay();

    // Hide advanced tabs initially
    updateAdvancedViewsVisibility();

    // Render empty queue
    renderAnalysisQueue();

    // The instrument filter this browser used last (checked against the
    // instruments once they are loaded).
    state.listInstrument = restoreInstrumentFilter(readSavedInstrumentFilter(), []);

    // Live updates first: take the cursor, then load, so nothing that
    // changes during the load is missed (the first answer is a reset, which
    // the load below covers).
    await startLive();

    // Load all data in parallel
    try {
        await Promise.all([
            loadSettings(),
            loadFiles(),
            loadComparisonStandards(),
            loadTableData(),
        ]);
        // Names for the instrument badges and the filter (after the list, so
        // a remembered instrument that no longer exists is dropped cleanly).
        await loadInstruments();
    } catch (e) {
        console.error('Initialization error:', e);
        showNotification('Some data failed to load on startup', 'error');
    }

    // Sendable links (/lab/<id>, /samples/<id>[/compare|/data]): select the
    // linked sample and tab once the list is loaded (static/js/deeplink.js).
    if (typeof DeepLink !== 'undefined') DeepLink.start();

    showNotification('GC Viewer ready', 'success');

    // v3.1: the new pages' user menu links here with ?open=settings|help
    // (after the settings have loaded, so the Settings form is filled).
    openFromQuery();

    // Load persistent system notifications (GCLive reports new ones).
    loadNotifications();

    // Reconnect to any active upload (e.g. page was refreshed during upload)
    try {
        const us = await apiGet('/api/qbench-upload-status');
        if (us.active && us.items && us.items.length > 0) {
            _uploadActive = true;
            _uploadItemStates = us.items;
            connectUploadSSE();
            _updateUploadIndicator('progress', 'Upload in progress…', 0);
        }
    } catch (_) { /* ignore */ }
});
