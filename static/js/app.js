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

const state = {
    settings: {},
    files: [],
    selectedFile: null,
    selectedUids: new Set(),   // multi-selection (shift / ctrl-cmd)
    selectionAnchor: null,     // last plainly-clicked uid (range anchor)
    traces: [],             // [{path, name, visible, color, x, y}]
    dcTraces: [],           // [{path, name, visible, color, percent, temperature}]
    tableData: { columns: [], rows: [] },
    calibration: { peak_times: [], carbon_numbers: [], boiling_points: [] },
    comparisonStandards: [],
    analysisResult: null,
    analysisQueue: [],      // [{lab_id, sample_name, pdf_path, added_at}]
    advancedViewsVisible: false,
    correctedD86: false,
    scanning: false,
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

function escapeHtml(str) {
    const el = document.createElement('span');
    el.textContent = str;
    return el.innerHTML;
}

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

async function api(method, url, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    if (!resp.ok) {
        let errMsg = `API error ${resp.status}`;
        try { const j = await resp.json(); errMsg = j.error || j.message || errMsg; } catch { /* ignore */ }
        throw new Error(errMsg);
    }
    const ct = resp.headers.get('content-type') || '';
    if (ct.includes('application/json')) return resp.json();
    return resp;
}

async function apiGet(url) { return api('GET', url); }
async function apiPost(url, body) { return api('POST', url, body); }
async function apiDelete(url) { return api('DELETE', url); }

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
        // Sync range overlays from settings
        if (s.analysis_range_overlays) {
            try {
                const saved = typeof s.analysis_range_overlays === 'string'
                    ? JSON.parse(s.analysis_range_overlays) : s.analysis_range_overlays;
                if (Array.isArray(saved) && saved.length > 0) {
                    state.rangeOverlays = saved.map((r, i) => ({
                        id: i + 1, label: r.label, c_start: r.c_start, c_end: r.c_end,
                        color: r.color || '#3fb95044'
                    }));
                    state.nextRangeId = state.rangeOverlays.length + 1;
                }
            } catch (_) { /* fall through to legacy keys */ }
        } else {
            // Legacy: only Gas/Oil by name
            if (s.analysis_gas_c_start && s.analysis_gas_c_end) {
                const gas = state.rangeOverlays.find(r => r.label === 'Gas');
                if (gas) { gas.c_start = parseInt(s.analysis_gas_c_start); gas.c_end = parseInt(s.analysis_gas_c_end); }
            }
            if (s.analysis_oil_c_start && s.analysis_oil_c_end) {
                const oil = state.rangeOverlays.find(r => r.label === 'Oil');
                if (oil) { oil.c_start = parseInt(s.analysis_oil_c_start); oil.c_end = parseInt(s.analysis_oil_c_end); }
            }
        }
        // Update UI inputs
        populateAnalysisParamInputs();
    } catch (e) {
        console.error('Failed to load settings:', e);
        showNotification('Failed to load settings: ' + e.message, 'error');
    }
}

async function loadFiles() {
    try {
        state.files = await apiGet('/api/files');
        console.log('[GC Viewer] Loaded', state.files.length, 'files');
        renderAllFileLists();
        // If the cache returned empty but we expect files, retry after a short delay
        // (the background cache builder may not have finished yet)
        if (state.files.length === 0) {
            setTimeout(async () => {
                try {
                    const retry = await apiGet('/api/files');
                    if (retry.length > 0) {
                        state.files = retry;
                        renderAllFileLists();
                        showNotification(`Loaded ${retry.length} samples`, 'success');
                    }
                } catch (_) { /* silent retry */ }
            }, 5000);
        }
    } catch (e) {
        console.error('Failed to load files:', e);
        showNotification('Failed to load file list: ' + e.message, 'error');
    }
}

async function loadCalibration() {
    try {
        state.calibration = await apiGet('/api/calibration');
    } catch (e) {
        console.error('Failed to load calibration:', e);
    }
}

async function loadTableData() {
    try {
        state.tableData = await apiGet('/api/table');
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
    // Trigger a background file cache rebuild on the server
    try { apiPost('/api/files/refresh'); } catch (_) { /* fire-and-forget */ }
    // Short delay to let the rebuild start, then fetch
    await new Promise(r => setTimeout(r, 500));
    await Promise.all([
        loadFiles(),
        loadTableData(),
        loadCalibration(),
        loadComparisonStandards(),
    ]);
    showNotification('Data refreshed', 'success');
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
        d86[cut] = round2(a0 + a1 * tPrev + a2 * tCurr + a3 * tNext);
    }
    // 40% and 60% have no D86 equation
    d86["40%"] = null;
    d86["60%"] = null;
    return d86;
}

/* ===================================================================
   7. FILE LIST RENDERING
   =================================================================== */

// Global filter: show only early-signal-flagged samples
let earlySignalFilterActive = false;

function renderFileList(containerId, files, mode) {
    const container = document.getElementById(containerId);
    if (!container) return;

    // Universal search — read from the shared search input
    const searchEl = document.getElementById('universal-search');
    const filter = searchEl ? searchEl.value.toLowerCase() : '';

    let filtered = files.filter(f =>
        f.name.toLowerCase().includes(filter) ||
        (f.display_name || '').toLowerCase().includes(filter));

    // Apply early-signal filter if active
    if (earlySignalFilterActive) {
        filtered = filtered.filter(f => f.early_signal);
    }

    container.innerHTML = '';
    for (const file of filtered) {
        const item = document.createElement('li');
        item.dataset.path = file.path;
        item.dataset.name = file.name;
        // uid distinguishes re-runs that share a name (and possibly a path),
        // so each injection selects independently.
        item.dataset.uid = file.uid || file.path;

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
        const fileUid = file.uid || file.path;
        const selUid = state.selectedFile && (state.selectedFile.uid || state.selectedFile.path);
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

        container.appendChild(item);
    }
}

function renderAllFileLists() {
    renderFileList('dash-file-list', state.files, 'dashboard');
    renderFileList('chrom-file-list', state.files, 'chrom');
    renderFileList('dcurve-file-list', state.files, 'dc');
    renderFileList('analysis-sample-list', state.files, 'analysis');
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
    const uid = file.uid || file.path;
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
            // Batch-aware: act on the whole multi-selection, else the clicked one.
            // Prefer exact CDF paths so daily-QC samples that share a Lab ID
            // reprocess the run the user actually selected, not the newest one.
            const files = selectionFilesOr(state.files, state.selectedUids, file);
            const paths = [];
            const samples = [];
            for (const f of files) {
                if (f.path && f.path !== f.name) paths.push(f.path);
                else {
                    const sid = (f.name || '').replace(/\.CDF$/i, '').trim();
                    if (sid) samples.push(sid);
                }
            }
            if (!paths.length && !samples.length) {
                showNotification('No sample ID available for this file', 'error');
                return;
            }
            try {
                const result = await apiPost('/api/reprocess', { paths, samples });
                _showReprocessToast(files.length, result.pending || 0);
                _pollReprocessStatus();
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
                await apiPost('/api/comparison-standard', { source_path: file.path, name });
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
            // Export sends the selection down the SAME tunnel as reprocess
            // ({paths, samples}); the backend computes + appends a CSV row each.
            const files = selectionFilesOr(state.files, state.selectedUids, file);
            const paths = [];
            const samples = [];
            for (const f of files) {
                if (f.path && f.path !== f.name) paths.push(f.path);
                else {
                    const sid = (f.name || '').replace(/\.CDF$/i, '').trim();
                    if (sid) samples.push(sid);
                }
            }
            if (!paths.length && !samples.length) {
                showNotification('No sample ID available for this file', 'error');
                return;
            }
            try {
                const result = await apiPost('/api/export-lims', { paths, samples });
                showNotification(`Exporting ${result.count} sample(s) to LIMS…`, 'info');
                _pollReprocessStatus();
            } catch (err) {
                showNotification('Export to LIMS failed: ' + err.message, 'error');
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

function removeContextMenu() {
    const menu = document.getElementById('context-menu');
    if (menu) menu.classList.remove('open');
}

/* ===================================================================
   9. DASHBOARD
   =================================================================== */

async function loadDashboardData(file) {
    const chromDiv = document.getElementById('dash-chrom-plot');
    const dcDiv = document.getElementById('dash-distill-plot');

    // Show loading spinners on all 4 dashboard quadrants
    document.querySelectorAll('#dash-grid .panel').forEach(p => _setLoading(p, true));

    try {
        // Fetch both in parallel, but keep them independent: a failure in the
        // distillation curve must NOT blank the chromatogram (and vice versa).
        const [traceRes, dcRes] = await Promise.allSettled([
            apiGet(`/api/trace?path=${encodeURIComponent(file.path)}`),
            apiGet(`/api/distillation-curve?path=${encodeURIComponent(file.path)}`),
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
            name: traceData.name || file.name,
            line: { color: '#58a6ff', width: 1.5 },
        }];
        // Add calibration overlays
        const calShapes = [];
        const calAnnotations = [];
        if (state.calibration.peak_times && state.calibration.peak_times.length > 0) {
            for (let i = 0; i < state.calibration.peak_times.length; i++) {
                const rt = state.calibration.peak_times[i];
                const cn = state.calibration.carbon_numbers ? state.calibration.carbon_numbers[i] : (i + 5);
                calShapes.push({
                    type: 'line', x0: rt, x1: rt, y0: 0, y1: 1, yref: 'paper',
                    line: { color: '#d29922', width: 1, dash: 'dot' },
                });
                calAnnotations.push({
                    x: rt, y: 1, yref: 'paper', text: `C${cn}`,
                    showarrow: false, font: { color: '#d29922', size: 9 }, yanchor: 'bottom',
                });
            }
        }
        Plotly.react(chromDiv, chromTraces, basePlotlyLayout({
            title: { text: file.name, font: { size: 14 } },
            xaxis: { title: 'Time (min)', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
            yaxis: { title: 'Intensity', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
            shapes: calShapes,
            annotations: calAnnotations,
        }), PLOTLY_CONFIG);
        }   // end chromatogram block

        // -- Distillation Curve plot (independent of the chromatogram) --
        if (dcRes.status === 'rejected') {
            console.error('Distillation curve load error:', dcRes.reason);
            showNotification('Distillation curve failed: ' + (dcRes.reason?.message || dcRes.reason), 'error');
        } else {
        const dcData = dcRes.value;
        // Use authoritative D2887/D86 from CSV (computed with proper blank
        // subtraction and corrections) when available.  Fall back to
        // client-side computation only if the CSV lookup misses.
        let d2887, d86;
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
            const d86Map = {"D86 IBP":"IBP","D86 T5":"5%","D86 T10":"10%","D86 T20":"20%","D86 T30":"30%","D86 T40":"40%","D86 T50":"50%","D86 T60":"60%","D86 T70":"70%","D86 T80":"80%","D86 T90":"90%","D86 T95":"95%","D86 FBP":"FBP"};
            // Toggle picks between pre-calculated corrected/uncorrected sets from backend
            const d86Source = state.correctedD86 ? csvD86 : (dcData.d86_uncorrected || {});
            d86 = {};
            for (const [csvKey, label] of Object.entries(d86Map)) {
                d86[label] = d86Source[csvKey] != null ? round2(d86Source[csvKey]) : null;
            }
            if (d86["40%"] === undefined) d86["40%"] = null;
            if (d86["60%"] === undefined) d86["60%"] = null;
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
            title: { text: 'Distillation Curve', font: { size: 14 } },
            xaxis: { title: 'Recovery (%)', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
            yaxis: { title: 'Temperature (\u00B0C)', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
        }), PLOTLY_CONFIG);

        // -- Populate D2887 and D86 tables --
        populateDashboardTables(d2887, d86, dcDiv);
        }   // end distillation curve block

    } catch (e) {
        console.error('Dashboard load error:', e);
        showNotification('Failed to load dashboard data: ' + e.message, 'error');
    } finally {
        document.querySelectorAll('#dash-grid .panel').forEach(p => _setLoading(p, false));
    }
}

function populateDashboardTables(d2887, d86, dcDiv) {
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
            // 40% and 60% have no D86 equation
            const tempStr = (label === '40%' || label === '60%') ? '\u2014' : (temp != null ? temp.toFixed(2) : '\u2014');
            tr.innerHTML = `<td>${escapeHtml(label)}</td><td>${tempStr}</td>`;
            tr.style.cursor = 'pointer';
            if (temp != null && label !== '40%' && label !== '60%') {
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
    if (state.traces.find(t => t.path === file.path)) {
        showNotification('Trace already on chart', 'info');
        return;
    }
    try {
        const data = await apiGet(`/api/trace?path=${encodeURIComponent(file.path)}`);
        const color = seriesColor(state.traces.length);
        state.traces.push({
            path: file.path,
            name: data.name || file.name,
            visible: true,
            color,
            x: data.x,
            y: data.y,
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

    // Calibration overlays
    const calShapes = [];
    const calAnnotations = [];
    if (state.calibration.peak_times) {
        for (let i = 0; i < state.calibration.peak_times.length; i++) {
            const rt = state.calibration.peak_times[i];
            const cn = state.calibration.carbon_numbers ? state.calibration.carbon_numbers[i] : (i + 5);
            calShapes.push({
                type: 'line', x0: rt, x1: rt, y0: 0, y1: 1, yref: 'paper',
                line: { color: '#d29922', width: 1, dash: 'dot' },
            });
            calAnnotations.push({
                x: rt, y: 1, yref: 'paper', text: `C${cn}`,
                showarrow: false, font: { color: '#d29922', size: 9 }, yanchor: 'bottom',
            });
        }
    }

    Plotly.react(div, traces, basePlotlyLayout({
        title: { text: 'Chromatogram Overlay', font: { size: 14 } },
        xaxis: { title: 'Time (min)', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
        yaxis: { title: 'Intensity', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
        shapes: calShapes,
        annotations: calAnnotations,
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

    // Update header sort indicators on the existing table
    const tableEl = document.getElementById('distill-table');
    if (!tableEl) return;
    const thead = tableEl.querySelector('thead');
    if (thead) {
        thead.querySelectorAll('th').forEach((th, ci) => {
            // Update sort arrow
            const col = th.dataset.col || columns[ci] || '';
            const arrow = tableSortCol === ci ? (tableSortAsc ? ' \u25B2' : ' \u25BC') : '';
            th.textContent = col + arrow;
        });
    }

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

    for (const row of displayRows) {
        const tr = document.createElement('tr');
        row.forEach((cell, ci) => {
            const td = document.createElement('td');
            const colName = columns[ci] || '';
            // Apply column group class
            if (COL_GROUP_META.includes(colName)) {
                td.className = 'col-meta';
            } else if (colName.startsWith('D86') || colName.includes('D86')) {
                td.className = 'col-d86';
            } else if (colName.startsWith('2887') || colName.includes('2887')) {
                td.className = 'col-d2887';
            }
            td.textContent = cell != null ? String(cell) : '';
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    }

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
    if (state.dcTraces.find(t => t.path === file.path)) {
        showNotification('Curve already on chart', 'info');
        return;
    }
    try {
        const data = await apiGet(`/api/distillation-curve?path=${encodeURIComponent(file.path)}`);
        const color = seriesColor(state.dcTraces.length);
        state.dcTraces.push({
            path: file.path,
            name: file.name,
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
        const res = await apiPost('/api/best-fit', { path: file.path });
        // Ignore stale responses after the user clicked another sample
        if (!state.selectedSample || state.selectedSample.path !== file.path) return;
        state.bestFit = res;
        renderBestFitPanel(res);
        if (!state.standardPinned && res.best_standard) {
            const std = state.comparisonStandards.find(s => s.name === res.best_standard);
            if (std && (!state.selectedStandard || state.selectedStandard.name !== std.name)) {
                state.selectedStandard = std;
                renderAnalysisStandards();
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
                await apiPost('/api/comparison-standard/rename', { old_name: std.name, new_name: newName });
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
                await apiDelete(`/api/comparison-standard/${encodeURIComponent(std.name)}`);
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
 * Add shaded rectangles + labels for each range overlay to the given shapes/annotations arrays.
 * Uses calibration peak_times/carbon_numbers to map C-number → retention time via interpolation.
 */
function addRangeShapes(shapes, annotations) {
    const peakTimes = state.calibration.peak_times;
    const carbonNums = state.calibration.carbon_numbers;
    if (!peakTimes || peakTimes.length === 0) return;

    const cn = carbonNums || peakTimes.map((_, i) => i + 5);
    const n = Math.min(peakTimes.length, cn.length);

    for (const range of state.rangeOverlays) {
        // Interpolate carbon numbers to retention times: linterp(xs, ys, target)
        const t0 = linterp(cn.slice(0, n), peakTimes.slice(0, n), range.c_start);
        const t1 = linterp(cn.slice(0, n), peakTimes.slice(0, n), range.c_end);
        if (t0 == null || t1 == null) continue;

        // Parse color (stored as #RRGGBBAA)
        const hex = (range.color || '#3fb95044').replace('#', '');
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
                text: range.label,
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
        sample_path: state.selectedSample.path,
        standard_name: state.selectedStandard.name,
        ...state.analysisParams,
        ranges: state.rangeOverlays.map(r => ({
            label: r.label, c_start: r.c_start, c_end: r.c_end
        })),
    };

    try {
        const result = await apiPost('/api/analysis', body);
        state.analysisResult = result;
        renderAnalysisResults(result);
    } catch (e) {
        showNotification('Analysis failed: ' + e.message, 'error');
    } finally {
        _setAnalysisLoading(false);
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
}

function renderAnalysisResults(result) {
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

        // Calibration vertical lines
        const shapes = [];
        const annotations = [];
        if (state.calibration.peak_times) {
            for (let i = 0; i < state.calibration.peak_times.length; i++) {
                const rt = state.calibration.peak_times[i];
                const cn = state.calibration.carbon_numbers ? state.calibration.carbon_numbers[i] : (i + 5);
                shapes.push({
                    type: 'line', x0: rt, x1: rt, y0: 0, y1: 1, yref: 'paper',
                    line: { color: '#d29922', width: 0.5, dash: 'dot' },
                });
                annotations.push({
                    x: rt, y: 1, yref: 'paper', text: `C${cn}`,
                    showarrow: false, font: { color: '#d29922', size: 8 }, yanchor: 'bottom',
                });
            }
        }

        // Dynamic range overlay shading
        addRangeShapes(shapes, annotations);

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

        // Dynamic range overlays on diff plot too
        const diffShapes = [];
        const diffAnnotations = [];
        addRangeShapes(diffShapes, diffAnnotations);

        Plotly.react(diffDiv, diffTraces, basePlotlyLayout({
            title: { text: 'Difference Plot', font: { size: 14 } },
            xaxis: {
                title: 'Retention Time (min)',
                gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590',
                range: result.diff.x_range || undefined,
            },
            yaxis: { title: 'Difference', gridcolor: '#21262d', zerolinecolor: '#30363d', color: '#7d8590' },
            shapes: diffShapes,
        }), PLOTLY_CONFIG);
    }

    // Deviation report — format like the desktop version
    // Store raw bullets separately so the export modal doesn't include the header
    state._lastReportBullets = result.report || 'No deviations detected.';

    const reportDiv = document.getElementById('analysis-report-text');
    if (reportDiv) {
        const p = state.analysisParams;
        const sampleName = state.selectedSample ? state.selectedSample.name : '?';
        const stdName = state.selectedStandard ? state.selectedStandard.name : '?';
        const rangesStr = state.rangeOverlays.map(r => `${r.label}(C${r.c_start}\u2013C${r.c_end})`).join('  ');
        const header = [
            `Sample:   ${sampleName}`,
            `Standard: ${stdName}`,
            `Params:   baseline=${realToSlider('baseline', p.quantile).toFixed(2)}  detail=${realToSlider('detail', p.window).toFixed(2)}  smoothing=${realToSlider('smoothing', p.sigma).toFixed(2)}`,
            `Thresholds: marginal\u2265${p.thresh_marginal}  moderate\u2265${p.thresh_moderate}  significant\u2265${p.thresh_significant}`,
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
   =================================================================== */

let annotationMode = false;
const annotationData = [];  // [{t_start, t_end, c_range, comment}]

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
    if (!trendDiv) return;

    trendDiv.on('plotly_selected', (eventData) => {
        if (!annotationMode || !eventData || !eventData.range) return;

        const t_start = eventData.range.x[0];
        const t_end = eventData.range.x[1];
        if (Math.abs(t_end - t_start) < 0.01) return; // too small

        // Compute carbon range
        const cn = state.calibration.carbon_numbers || [];
        const pt = state.calibration.peak_times || [];
        const n = Math.min(cn.length, pt.length);
        let c_range = '';
        if (n > 0) {
            const c_s = linterp(pt.slice(0, n), cn.slice(0, n), t_start);
            const c_e = linterp(pt.slice(0, n), cn.slice(0, n), t_end);
            if (!isNaN(c_s) && !isNaN(c_e)) {
                c_range = `C${Math.round(c_s)}-C${Math.round(c_e)}`;
            }
        }

        const regionDesc = c_range
            ? `${c_range}  (${t_start.toFixed(2)}\u2013${t_end.toFixed(2)} min)`
            : `(${t_start.toFixed(2)}\u2013${t_end.toFixed(2)} min)`;

        // Open annotation modal instead of prompt()
        _openAnnotationModal(regionDesc, t_start, t_end, c_range, trendDiv);

        // Disable annotation mode after one annotation (no endless loop)
        annotationMode = false;
        const btn = document.getElementById('btn-annotation-toggle');
        if (btn) btn.classList.remove('active');
        Plotly.relayout(trendDiv, { dragmode: 'zoom' });
        trendDiv.style.cursor = '';
    });
}

function _openAnnotationModal(regionDesc, t_start, t_end, c_range, trendDiv) {
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

    function _save() {
        const comment = (inputEl ? inputEl.value : '').trim();
        // Store annotation
        annotationData.push({ t_start, t_end, c_range, comment });
        updateAnnotationCount();
        redrawAnnotations();

        // Append bullet to deviation report (not conclusion)
        const reportEl = document.getElementById('analysis-report-text');
        if (reportEl) {
            const prefix = c_range
                ? `\u2022 ${c_range} (${t_start.toFixed(2)}\u2013${t_end.toFixed(2)} min):`
                : `\u2022 (${t_start.toFixed(2)}\u2013${t_end.toFixed(2)} min):`;
            const bullet = comment ? `${prefix} ${comment}` : prefix;
            reportEl.textContent = reportEl.textContent.trimEnd() + '\n' + bullet;
            // Also update the stored bullets so they flow to exports
            state._lastReportBullets = (state._lastReportBullets || '').trimEnd() + '\n' + bullet;
        }

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

function redrawAnnotations() {
    const trendDiv = document.getElementById('analysis-trend-plot');
    if (!trendDiv || !trendDiv.layout) return;

    // Get existing shapes/annotations and filter out old annotation shapes
    const existingShapes = (trendDiv.layout.shapes || []).filter(s => !s._annotation);
    const existingAnnotations = (trendDiv.layout.annotations || []).filter(a => !a._annotation);

    // Add annotation shapes
    for (const ann of annotationData) {
        existingShapes.push({
            _annotation: true,
            type: 'rect',
            x0: ann.t_start, x1: ann.t_end,
            y0: 0, y1: 1, yref: 'paper',
            fillcolor: 'rgba(88, 166, 255, 0.15)',
            line: { width: 1, color: 'rgba(88, 166, 255, 0.5)', dash: 'dash' },
            layer: 'above',
        });
        if (ann.comment) {
            existingAnnotations.push({
                _annotation: true,
                x: (ann.t_start + ann.t_end) / 2,
                y: 0.95, yref: 'paper',
                text: ann.comment.length > 30 ? ann.comment.slice(0, 30) + '...' : ann.comment,
                showarrow: false,
                font: { color: '#58a6ff', size: 9 },
                bgcolor: 'rgba(13,17,23,0.8)',
                borderpad: 2,
            });
        }
    }

    Plotly.relayout(trendDiv, {
        shapes: existingShapes,
        annotations: existingAnnotations,
    });
}

function updateAnnotationCount() {
    const el = document.getElementById('annotation-count');
    if (el) el.textContent = annotationData.length > 0 ? `${annotationData.length} annotation(s)` : '';
}

function clearAllAnnotations() {
    annotationData.length = 0;
    updateAnnotationCount();

    // Force-remove all annotation shapes/labels from the plot
    const trendDiv = document.getElementById('analysis-trend-plot');
    if (trendDiv && trendDiv.layout) {
        // Keep only non-annotation shapes (use fillcolor as marker since
        // Plotly may strip custom _annotation property)
        const cleanShapes = (trendDiv.layout.shapes || []).filter(s =>
            !s._annotation && s.fillcolor !== 'rgba(88, 166, 255, 0.15)'
        );
        const cleanAnnotations = (trendDiv.layout.annotations || []).filter(a =>
            !a._annotation && a.bgcolor !== 'rgba(13,17,23,0.8)'
        );
        Plotly.relayout(trendDiv, {
            shapes: cleanShapes,
            annotations: cleanAnnotations,
        });
    }

    // Strip annotation bullets (lines starting with •) from deviation report
    const reportEl = document.getElementById('analysis-report-text');
    if (reportEl) {
        const lines = reportEl.textContent.split('\n');
        reportEl.textContent = lines.filter(ln => !ln.startsWith('\u2022') || !ln.includes(' min')).join('\n').trimEnd();
    }
    // Also clean stored bullets
    if (state._lastReportBullets) {
        const lines = state._lastReportBullets.split('\n');
        state._lastReportBullets = lines.filter(ln => !ln.startsWith('\u2022') || !ln.includes(' min')).join('\n').trimEnd();
    }
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
        item.sample_path = state.selectedSample?.path || item.sample_path;
        item.standard_name = state.selectedStandard?.name || item.standard_name;
        item.bullets = state._lastReportBullets || item.bullets;
        item.annotations = annotationData.map(a => ({...a}));  // save current annotations
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

    // Lab ID = everything before the first underscore (matches desktop app logic)
    // e.g. "32815_04202026.CDF" → "32815"
    if (labIdInput) {
        const raw = state.selectedSample ? state.selectedSample.name.replace(/\.CDF$/i, '') : '';
        labIdInput.value = raw.includes('_') ? raw.split('_')[0] : raw;
    }
    if (docNameInput) docNameInput.value = 'GC Analysis';

    // Pull ONLY the deviation bullets (not the header block) from the stored result
    const conclusionEl = document.getElementById('analysis-conclusion');
    if (bulletsInput) bulletsInput.value = state._lastReportBullets || '';
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

    // Grab the edited bullets and conclusion from the modal
    const bullets = (document.getElementById('export-bullets')?.value || '').trim();
    const conclusion = (document.getElementById('export-conclusion')?.value || '').trim();

    state.analysisQueue.push({
        lab_id: labId,
        sample_name: sampleName,
        sample_path: state.selectedSample?.path || '',
        standard_name: state.selectedStandard?.name || '',
        bullets,
        conclusion,
        overlay_standards: overlayStds,
        ranges: rangesForPayload(state.rangeOverlays),  // capture regions at queue time
        annotations: annotationData.map(a => ({...a})),  // deep copy
        pdf_path: '',
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
            _stageAnalysisSample(item.sample_path);
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
    if (item.sample_path) {
        const file = state.files.find(f => f.path === item.sample_path);
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

    // Restore saved annotations into the global array
    annotationData.length = 0;
    if (item.annotations && item.annotations.length > 0) {
        for (const ann of item.annotations) {
            annotationData.push({...ann});
        }
    }
    updateAnnotationCount();

    // Re-run the analysis so graphs + report load
    if (state.selectedSample && state.selectedStandard) {
        readAnalysisParams();
        runAnalysis().then(() => {
            // After analysis completes, restore the saved conclusion
            // (runAnalysis overwrites it with the auto-generated one)
            const conclusionEl = document.getElementById('analysis-conclusion');
            if (conclusionEl && item.conclusion) conclusionEl.value = item.conclusion;

            // Redraw restored annotations on the trend plot
            if (annotationData.length > 0) {
                redrawAnnotations();
            }
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
function _stageAnalysisSample(samplePath) {
    const list = document.getElementById('analysis-sample-list');
    if (!list) return;
    list.querySelectorAll('li.selected').forEach(el => el.classList.remove('selected'));
    list.querySelectorAll('li.staged').forEach(el => el.classList.remove('staged'));
    if (samplePath) {
        const item = list.querySelector(`li[data-path="${CSS.escape(samplePath)}"]`);
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
                buildReportItemPayload(item, state.rangeOverlays));
            await downloadBlob(resp, `${item.lab_id}_analysis.pdf`);
            showNotification('Download complete', 'success');
        } else {
            // Multiple files — download as ZIP via browser
            const items = state.analysisQueue.map(item =>
                buildReportItemPayload(item, state.rangeOverlays));
            const resp = await api('POST', '/api/export-analysis-reports-zip', { items });
            await downloadBlob(resp, 'analysis_reports.zip');
            showNotification(`Downloaded ${count} reports as ZIP`, 'success');
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

        const resp = await apiPost('/api/qbench-upload', {
            queue: state.analysisQueue,
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
    };
    for (const [elId, [key, dflt]] of Object.entries(bfMap)) {
        const el = document.getElementById(elId);
        if (el) el.value = state.settings[key] || dflt;
    }

    // Populate comparison standards list in settings
    renderSettingsStandards();

    openModal(modal);
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
                await apiDelete('/api/comparison-standard/' + encodeURIComponent(std.name));
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

async function browseAndAddStandard() {
    try {
        const result = await apiPost('/api/browse', {
            type: 'file', title: 'Select CDF file for comparison standard',
        });
        if (!result.path) return;
        const defaultName = result.path.split(/[/\\]/).pop().replace(/\.cdf$/i, '');
        const name = prompt('Name for this comparison standard:', defaultName);
        if (!name) return;
        await apiPost('/api/comparison-standard', { source_path: result.path, name });
        await loadComparisonStandards();
        renderSettingsStandards();
        showNotification(`Added standard: ${name}`, 'success');
    } catch (e) {
        showNotification('Failed: ' + e.message, 'error');
    }
}

async function browseForPath(inputId, type) {
    const inputEl = document.getElementById(inputId);
    if (!inputEl) return;
    const current = inputEl.value || '';
    try {
        const result = await apiPost('/api/browse', {
            type: type || 'dir',
            title: 'Select ' + (type === 'file' ? 'File' : 'Folder'),
            initial: current,
        });
        if (result.path) {
            inputEl.value = result.path;
        }
    } catch (e) {
        showNotification('Browse failed: ' + e.message, 'error');
    }
}

async function saveSettings() {
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

    const newSettings = { ...state.settings };
    for (const [elId, key] of Object.entries(fieldsMap)) {
        const el = document.getElementById(elId);
        if (el) newSettings[key] = el.value;
    }

    // Flag rules (replaces the legacy early-signal fields)
    const flagRules = readFlagRulesFromDOM();
    if (flagRules !== null) newSettings.sample_flag_rules = JSON.stringify(flagRules);

    // Best-fit settings
    const bfEnabled = document.getElementById('set-bestfit-enabled');
    if (bfEnabled) newSettings.bestfit_enabled = bfEnabled.checked ? 'true' : 'false';
    const bfSaveMap = {
        'set-bestfit-threshold': 'bestfit_threshold',
        'set-bestfit-shift': 'bestfit_shift_tolerance_min',
        'set-bestfit-minfrac': 'bestfit_mix_min_frac',
    };
    for (const [elId, key] of Object.entries(bfSaveMap)) {
        const el = document.getElementById(elId);
        if (el && el.value !== '') newSettings[key] = el.value;
    }

    // Also persist current analysis params to settings
    const p = state.analysisParams;
    newSettings.analysis_quantile = String(p.quantile);
    newSettings.analysis_window = String(p.window);
    newSettings.analysis_sigma = String(p.sigma);
    newSettings.analysis_thresh_marginal = String(p.thresh_marginal);
    newSettings.analysis_thresh_moderate = String(p.thresh_moderate);
    newSettings.analysis_thresh_significant = String(p.thresh_significant);
    newSettings.analysis_x_max_min = String(p.x_max_min);
    // Save range overlays
    const gas = state.rangeOverlays.find(r => r.label === 'Gas');
    const oil = state.rangeOverlays.find(r => r.label === 'Oil');
    if (gas) { newSettings.analysis_gas_c_start = String(gas.c_start); newSettings.analysis_gas_c_end = String(gas.c_end); }
    if (oil) { newSettings.analysis_oil_c_start = String(oil.c_start); newSettings.analysis_oil_c_end = String(oil.c_end); }

    try {
        await apiPost('/api/settings', newSettings);
        state.settings = newSettings;
        closeAllModals();
        showNotification('Settings saved — refreshing data...', 'success');
        await refreshAll();
        showNotification('Data refreshed with new settings', 'success');
    } catch (e) {
        showNotification('Failed to save settings: ' + e.message, 'error');
    }
}

/* ===================================================================
   17. SCAN & LOG MODAL
   =================================================================== */

let scanSSE = null;

function openLogModal(title) {
    const modal = document.getElementById('modal-log');
    if (!modal) return;
    const titleEl = modal.querySelector('.modal-header h2');
    if (titleEl) titleEl.textContent = title || 'Log';
    const logBody = document.getElementById('log-output');
    if (logBody) logBody.textContent = '';
    // Reset status
    const statusEl = document.getElementById('log-status');
    if (statusEl) statusEl.textContent = 'Waiting...';
    openModal(modal);
}

function appendLog(msg) {
    const logBody = document.getElementById('log-output');
    if (!logBody) return;
    logBody.textContent += msg + '\n';
    logBody.scrollTop = logBody.scrollHeight;
}

function updateScanProgress(data) {
    // Update log modal status
    const statusEl = document.getElementById('log-status');
    // Update persistent status bar
    const barInfo = document.getElementById('status-scan-info');
    const barWrap = document.getElementById('status-scan-bar-wrap');
    const barFill = document.getElementById('status-scan-bar-fill');
    const barCounts = document.getElementById('status-scan-counts');

    if (data.type === 'total') {
        const msg = data.new > 0
            ? `Found ${data.total} CDF files (${data.already} done, ${data.new} new)`
            : `All ${data.total} files already processed`;
        if (statusEl) statusEl.innerHTML = `<b>${msg}</b>`;
        if (barInfo) barInfo.innerHTML = data.new > 0
            ? `<span style="color:#58a6ff;">Scanning:</span> ${data.new} new files`
            : `<span style="color:#3fb950;">Up to date</span>`;
        if (barWrap) barWrap.style.display = data.new > 0 ? '' : 'none';
        if (barFill) barFill.style.width = '0%';
        if (barCounts) barCounts.textContent = '';
    } else if (data.type === 'progress') {
        const pct = Math.round((data.current / data.total) * 100);
        const batchInfo = data.total_batches > 1 ? `Batch ${data.batch}/${data.total_batches}` : '';
        // Log modal
        if (statusEl) statusEl.innerHTML = `
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <span style="min-width:60px; font-weight:600;">${pct}%</span>
                <div style="flex:1; min-width:100px; height:8px; background:#21262d; border-radius:4px; overflow:hidden;">
                    <div style="width:${pct}%; height:100%; background:linear-gradient(90deg,#1f6feb,#58a6ff); border-radius:4px; transition:width .3s;"></div>
                </div>
                <span style="font-size:10px; color:#7d8590;">${batchInfo}</span>
            </div>
            <div style="font-size:10px; color:#7d8590; margin-top:2px;">
                ${data.processed} processed · ${data.skipped} existing · ${data.errors} errors
            </div>
        `;
        // Status bar
        if (barInfo) barInfo.innerHTML = `<span style="color:#58a6ff;">Processing:</span> ${pct}% ${batchInfo}`;
        if (barWrap) barWrap.style.display = '';
        if (barFill) barFill.style.width = pct + '%';
        if (barCounts) barCounts.textContent = `${data.processed} new · ${data.skipped} existing · ${data.errors} err`;
    } else if (data.type === 'done') {
        if (statusEl) statusEl.innerHTML = `<span style="color:#3fb950; font-weight:600;">Complete</span> — ${data.processed} processed, ${data.skipped} skipped, ${data.errors} errors`;
        if (barInfo) barInfo.innerHTML = `<span style="color:#3fb950;">Scan complete</span> — ${data.processed} processed`;
        if (barWrap) barWrap.style.display = 'none';
        if (barCounts) barCounts.textContent = '';
        state.scanning = false;
        updateScanButtons();
        refreshAll();
    } else if (data.type === 'stopped') {
        if (statusEl) statusEl.innerHTML = `<span style="color:#d29922; font-weight:600;">Stopped</span> — ${data.processed} processed, ${data.skipped} skipped`;
        if (barInfo) barInfo.innerHTML = `<span style="color:#d29922;">Scan stopped</span>`;
        if (barWrap) barWrap.style.display = 'none';
        if (barCounts) barCounts.textContent = '';
        state.scanning = false;
        updateScanButtons();
        refreshAll();
    } else if (data.type === 'error') {
        if (statusEl) statusEl.innerHTML = `<span style="color:#f85149;">Error:</span> ${escapeHtml(data.message || '')}`;
        if (barInfo) barInfo.innerHTML = `<span style="color:#f85149;">Scan error</span>`;
        if (barWrap) barWrap.style.display = 'none';
        state.scanning = false;
        updateScanButtons();
    }
}

function connectSSE(url) {
    disconnectSSE();
    scanSSE = new EventSource(url);
    scanSSE.onmessage = (e) => {
        const raw = e.data;
        // Try parsing as JSON (structured progress), fallback to plain text log
        try {
            const data = JSON.parse(raw);
            if (data.type) {
                updateScanProgress(data);
                return;
            }
        } catch (_) { /* not JSON, treat as log text */ }
        appendLog(raw);
    };
    scanSSE.onerror = () => {
        disconnectSSE();
        if (state.scanning) {
            state.scanning = false;
            updateScanButtons();
            refreshAll();
        }
    };
}

function disconnectSSE() {
    if (scanSSE) {
        scanSSE.close();
        scanSSE = null;
    }
}

async function startScan(silent) {
    if (state.scanning) return;
    state.scanning = true;
    updateScanButtons();
    try {
        await apiPost('/api/scan');
        if (!silent) openLogModal('Scan & Parse');
        connectSSE('/api/scan/stream');
        // Also start polling for status updates (more reliable than SSE alone)
        startScanStatusPolling();
    } catch (e) {
        state.scanning = false;
        updateScanButtons();
        if (!silent) showNotification('Failed to start scan: ' + e.message, 'error');
    }
}

async function stopScan() {
    try {
        await apiPost('/api/stop-scan');
        appendLog('--- Stop requested ---');
        // Close SSE + polling immediately so they don't hold connections
        disconnectSSE();
        stopScanStatusPolling();
        state.scanning = false;
        updateScanButtons();
        const barInfo = document.getElementById('status-scan-info');
        if (barInfo) barInfo.innerHTML = '<span style="color:#d29922;">Stopped</span>';
        showNotification('Scan stopped', 'info');
        // Refresh file list so processed samples appear
        await refreshAll();
    } catch (e) {
        showNotification('Failed to stop scan: ' + e.message, 'error');
    }
}

function updateScanButtons() {
    const scanBtn = document.getElementById('btn-scan');
    const stopBtn = document.getElementById('btn-stop');
    if (scanBtn) scanBtn.disabled = state.scanning;
    if (stopBtn) stopBtn.disabled = !state.scanning;
}

// Poll /api/scan/status every 2s for progress (works even if SSE drops)
let _scanPollTimer = null;
function startScanStatusPolling() {
    stopScanStatusPolling();
    _scanPollTimer = setInterval(async () => {
        try {
            const st = await apiGet('/api/scan/status');
            if (st.phase === 'processing' || st.phase === 'scanning') {
                state.scanning = true;
                updateScanButtons();
                // Update the log status bar
                if (st.phase === 'processing' && st.total > 0) {
                    const pct = Math.round(((st.already + st.processed + st.errors) / st.total) * 100);
                    updateScanProgress({
                        type: 'progress',
                        current: st.already + st.processed + st.errors,
                        total: st.total,
                        batch: st.current_batch,
                        total_batches: st.total_batches,
                        processed: st.processed,
                        skipped: st.already,
                        errors: st.errors,
                        file: st.current_file || '',
                        status: 'ok',
                    });
                }
            } else if (st.phase === 'done' || st.phase === 'idle' || st.phase === 'stopped') {
                if (state.scanning) {
                    state.scanning = false;
                    updateScanButtons();
                    refreshAll();
                }
                if (st.phase !== 'idle') {
                    stopScanStatusPolling();
                }
            }
        } catch (_) { /* ignore polling errors */ }
    }, 2000);
}

function stopScanStatusPolling() {
    if (_scanPollTimer) {
        clearInterval(_scanPollTimer);
        _scanPollTimer = null;
    }
}

/* ===================================================================
   18. REPROCESS MODAL
   =================================================================== */

// Holds the latest preview so Confirm reprocesses exactly what was shown.
let _reprocessPreview = { matched: [], missing: [] };

function openReprocessModal() {
    const modal = document.getElementById('modal-reprocess');
    if (!modal) return;
    const input = document.getElementById('reprocess-ids');
    if (input) input.value = '';
    _reprocessPreview = { matched: [], missing: [] };
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
        _reprocessPreview = { matched: [], missing: [] };
        renderReprocessPreview();
        return;
    }
    try {
        const result = await apiPost('/api/reprocess/preview', { query });
        if (result && result.error) {
            _reprocessPreview = { matched: [], missing: [] };
            renderReprocessPreview(result.error);
            return;
        }
        _reprocessPreview = {
            matched: result.matched || [],
            missing: result.missing || [],
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
    const { matched, missing } = _reprocessPreview;
    if (!matched.length) {
        showNotification('No matching samples to re-process', 'info');
        return;
    }
    closeAllModals();

    try {
        const result = await apiPost('/api/reprocess', { samples: matched, missing });
        const pending = result.pending || 0;

        // Show a persistent toast in top-right (not a full modal)
        _showReprocessToast(matched.length, pending);

        // Poll for completion via SSE
        _pollReprocessStatus();

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

async function loadNotifications() {
    try {
        const notes = await apiGet('/api/notifications');
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

/** Settings: re-derive injection times from CDFs and reorder the library. */
async function reindexLibraryTimes() {
    try {
        await apiPost('/api/library/reindex-times');
        showNotification('Reordering library in the background — check notifications', 'info');
        // Pick up the completion notification shortly after.
        setTimeout(loadNotifications, 3000);
    } catch (e) {
        showNotification('Reorder failed to start: ' + e.message, 'error');
    }
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

/** Poll reprocess status and update the persistent toast. */
function _pollReprocessStatus() {
    const _timer = setInterval(async () => {
        const toast = document.getElementById('reprocess-toast');
        if (!toast) { clearInterval(_timer); return; }

        try {
            const st = await apiGet('/api/reprocess/status');
            const titleEl = toast.querySelector('#reprocess-toast-title');
            const barEl = toast.querySelector('#reprocess-toast-bar');
            const detailEl = toast.querySelector('#reprocess-toast-detail');
            const closeBtn = toast.querySelector('#reprocess-toast-close');

            if (st.phase === 'processing' || st.phase === 'scanning') {
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

                clearInterval(_timer);
                refreshAll();

                // Auto-dismiss after 8 seconds
                setTimeout(() => { if (toast.parentNode) toast.remove(); }, 8000);
            }
        } catch (_) { /* ignore */ }
    }, 1500);
}

/* ===================================================================
   19. REBUILD DATABASE
   =================================================================== */

async function rebuildDatabase() {
    if (!confirm('This will clear the distillation database and rebuild from scratch. A backup of the CSV will be created. Continue?')) {
        return;
    }

    try {
        await apiPost('/api/rebuild-db');
        openLogModal('Rebuild Database');
        connectSSE('/api/scan/stream');
        showNotification('Database rebuild started', 'info');
    } catch (e) {
        showNotification('Rebuild failed: ' + e.message, 'error');
    }
}

/* ===================================================================
   19b. SERVER RESTART
   =================================================================== */

async function restartServer() {
    if (!confirm('Restart the server? The page will reload automatically once the server is back up.')) {
        return;
    }
    try {
        await apiPost('/api/restart');
        showNotification('Server is restarting...', 'info');
        // Poll until the server comes back, then reload the page
        _waitForServerAndReload();
    } catch (e) {
        showNotification('Restart failed: ' + e.message, 'error');
    }
}

function _waitForServerAndReload() {
    let attempts = 0;
    const maxAttempts = 60; // give up after ~60 seconds
    const interval = setInterval(async () => {
        attempts++;
        if (attempts > maxAttempts) {
            clearInterval(interval);
            showNotification('Server did not come back — try refreshing manually', 'error');
            return;
        }
        try {
            const resp = await fetch('/api/server-status', { method: 'GET' });
            if (resp.ok) {
                clearInterval(interval);
                // Small extra delay so the server finishes initialising
                setTimeout(() => location.reload(), 1500);
            }
        } catch (_) {
            // Server still down — keep polling
        }
    }, 1000);
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
        const resp = await api('POST', '/api/export-pdf', { path: state.selectedFile.path });
        await downloadBlob(resp, 'export.pdf');
        showNotification('PDF exported', 'success');
    } catch (e) {
        showNotification('Export failed: ' + e.message, 'error');
    }
}

async function exportComparison() {
    // Gather all selected/loaded chromatogram paths
    const paths = [];
    if (state.selectedFile) paths.push(state.selectedFile.path);
    // Also include any traces on the chromatogram overlay
    for (const tr of state.traces) {
        if (!paths.includes(tr.path)) paths.push(tr.path);
    }
    if (paths.length === 0) {
        showNotification('Select a sample first (Dashboard or Chromatograms tab)', 'info');
        return;
    }
    try {
        const result = await apiPost('/api/export-comparison', { sample_paths: paths });
        if (result && result.files && result.files.length > 0) {
            showNotification(`Comparison exported: ${result.files.length} file(s)`, 'success');
            // Open the export folder
            try { await apiGet(`/api/open-folder?path=${encodeURIComponent(state.settings.export_folder || '')}`); } catch (_) { /* ignore */ }
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
    const bullets = document.getElementById('analysis-report-text')?.textContent || '';
    const rawName = state.selectedSample.name.replace(/\.CDF$/i, '');
    const labId = rawName.includes('_') ? rawName.split('_')[0] : rawName;

    // Gather overlay standards
    const overlayStds = state.comparisonStandards.map(s => s.path);

    try {
        const resp = await api('POST', '/api/export-analysis-report', {
            sample_path: state.selectedSample.path,
            standard_name: state.selectedStandard.name,
            conclusion,
            bullets,
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

async function openProcessedFolder() {
    try {
        const dir = state.settings.processed_cdf_dir || '';
        await apiGet(`/api/open-folder?path=${encodeURIComponent(dir)}`);
    } catch (e) {
        showNotification('Failed to open folder: ' + e.message, 'error');
    }
}

async function openWatchFolder() {
    try {
        const dir = state.settings.watch_dir || '';
        await apiGet(`/api/open-folder?path=${encodeURIComponent(dir)}`);
    } catch (e) {
        showNotification('Failed to open folder: ' + e.message, 'error');
    }
}

async function openExportFolder() {
    try {
        const dir = state.settings.export_folder || '';
        await apiGet(`/api/open-folder?path=${encodeURIComponent(dir)}`);
    } catch (e) {
        showNotification('Failed to open folder: ' + e.message, 'error');
    }
}

/* ===================================================================
   22. HELP MODAL
   =================================================================== */

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
        'btn-open-processed': openProcessedFolder,
        'btn-open-watch': openWatchFolder,
        'btn-refresh': () => refreshAll(),
        'btn-scan': startScan,
        'btn-stop': stopScan,
        'btn-log-stop': stopScan,
        'btn-reprocess': openReprocessModal,
        'btn-rebuild-db': rebuildDatabase,
        'btn-help': openHelpModal,
        'btn-restart-server': restartServer,
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
        // Reprocess modal
        'btn-reprocess-run': submitReprocess,
        // Notification tray
        'btn-notif-tray': () => toggleNotifPanel(),
        'btn-notif-dismiss-all': dismissAllNotifications,
        // Settings: library reorder
        'btn-reindex-times': reindexLibraryTimes,
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
        universalSearch.addEventListener('input', debounce(() => renderAllFileLists(), 150));
    }

    // Reprocess query — live preview of matching samples
    const reprocInput = document.getElementById('reprocess-ids');
    if (reprocInput) {
        reprocInput.addEventListener('input', debounce(() => previewReprocess(), 250));
    }

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

    // Browse buttons in settings modal
    document.querySelectorAll('.browse-btn[data-browse]').forEach(btn => {
        btn.addEventListener('click', () => {
            const inputId = btn.dataset.browse;
            const type = btn.dataset.type || 'dir';
            browseForPath(inputId, type);
        });
    });

    // "Add" button in settings comparison standards
    const addStdBtn = document.getElementById('btn-settings-add-standard');
    if (addStdBtn) {
        addStdBtn.addEventListener('click', () => {
            // Just open a native browse to pick a CDF file, then save as standard
            browseAndAddStandard();
        });
    }

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
        // Ctrl+R — Refresh
        if ((e.ctrlKey || e.metaKey) && e.key === 'r') {
            e.preventDefault();
            refreshAll();
        }
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
    console.log('[GC Viewer] Charts initialized');

    // Set up event listeners
    setupEventListeners();

    // Populate analysis parameter inputs with defaults
    populateAnalysisParamInputs();

    // Show the getting-started overlay
    updateAnalysisOverlay();

    // Hide advanced tabs initially
    updateAdvancedViewsVisibility();

    // Update scan button states
    updateScanButtons();

    // Render empty queue
    renderAnalysisQueue();

    // Load all data in parallel
    try {
        await Promise.all([
            loadSettings(),
            loadFiles(),
            loadCalibration(),
            loadComparisonStandards(),
            loadTableData(),
        ]);
    } catch (e) {
        console.error('Initialization error:', e);
        showNotification('Some data failed to load on startup', 'error');
    }

    showNotification('GC Viewer ready', 'success');

    // Load persistent system notifications and poll for new ones.
    loadNotifications();
    setInterval(loadNotifications, 30000);

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

    // Auto-scan on startup (silent — no modal, just background processing)
    setTimeout(() => startScan(true), 2000);
});
