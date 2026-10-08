/* v5.0.0 lane C: the Compare view's pure logic, shared by the browser
   (window.GCCompareLogic) and the Node tests (module.exports). No DOM, no
   fetch. compare_view.js draws; this decides.

   - the trend sliders (-1..+1 <-> the server's quantile/window/sigma), the
     same mapping as the classic Analysis tab;
   - the saved defaults from /api/settings and the /api/analysis body;
   - ?standard= in the address bar, and which standard a sample opens with;
   - the findings view model: one row per /api/analysis item, the sentence
     always the server's own line (never recomposed here);
   - the Adjust drawer's validation (nothing invalid is ever sent). */
(function (root) {
    'use strict';

    const payload = (typeof module !== 'undefined' && module.exports)
        ? require('./report_payload.js') : root;

    // ── trend sliders ───────────────────────────────────────────────────────
    const TREND_SLIDER_MAP = {
        baseline:  { min: 0.05, def: 0.20, max: 0.50, real: 'quantile' },
        detail:    { min: 51,   def: 301,  max: 2001, real: 'window', invert: true },
        smoothing: { min: 1.0,  def: 34.0, max: 200.0, real: 'sigma' },
    };
    const SLIDERS = ['baseline', 'detail', 'smoothing'];

    function sliderToReal(name, norm) {
        const m = TREND_SLIDER_MAP[name];
        const n = m.invert ? -norm : norm;
        if (n <= 0) return m.def + n * (m.def - m.min);
        return m.def + n * (m.max - m.def);
    }

    function realToSlider(name, real) {
        const m = TREND_SLIDER_MAP[name];
        let r;
        if (real <= m.def) r = (m.def === m.min) ? 0 : (real - m.def) / (m.def - m.min);
        else r = (m.max === m.def) ? 0 : (real - m.def) / (m.max - m.def);
        r = m.invert ? -r : r;
        return Math.max(-1, Math.min(1, r)) + 0;        // + 0: never -0
    }

    /** The value the server gets for a slider position: the window odd, the
        others to 2 places (as the classic tab sends them). */
    function sliderReal(name, norm) {
        const real = sliderToReal(name, Number(norm) || 0);
        if (name === 'detail') {
            let w = Math.round(real);
            if (w % 2 === 0) w += 1;
            return w;
        }
        return parseFloat(real.toFixed(2));
    }

    function sliderLabel(norm) {
        const n = Number(norm) || 0;
        return (n >= 0 ? '+' : '') + n.toFixed(2);
    }

    // ── parameters ──────────────────────────────────────────────────────────
    const PARAM_DEFAULTS = { quantile: 0.20, window: 301, sigma: 34.0, thresh_marginal: 100,
        thresh_moderate: 500, thresh_significant: 2000, x_max_min: 7.0 };
    const SETTING_KEYS = { quantile: 'analysis_quantile', window: 'analysis_window',
        sigma: 'analysis_sigma', thresh_marginal: 'analysis_thresh_marginal',
        thresh_moderate: 'analysis_thresh_moderate', thresh_significant: 'analysis_thresh_significant',
        x_max_min: 'analysis_x_max_min' };

    /** The saved defaults (/api/settings), as the classic loadSettings reads them. */
    function defaultParams(settings) {
        const s = settings || {};
        const out = {};
        for (const key of Object.keys(PARAM_DEFAULTS)) {
            const raw = s[SETTING_KEYS[key]];
            const v = key === 'window' ? parseInt(raw, 10) : parseFloat(raw);
            out[key] = (raw !== undefined && raw !== null && raw !== '' && Number.isFinite(v))
                ? v : PARAM_DEFAULTS[key];
        }
        return out;
    }

    function analysisBody(sampleId, standardName, params, overlays) {
        return Object.assign({ sample_id: sampleId, standard_name: standardName },
            payload.captureReportParams(params),
            { ranges: payload.rangesForPayload(overlays || []) });
    }

    // ── the address bar ─────────────────────────────────────────────────────
    function standardFromSearch(search) {
        const q = String(search || '').replace(/^\?/, '');
        for (const part of q.split('&')) {
            const i = part.indexOf('=');
            if (i < 0) continue;
            if (part.slice(0, i) !== 'standard') continue;
            let v;
            try { v = decodeURIComponent(part.slice(i + 1).replace(/\+/g, ' ')); } catch (_e) { v = ''; }
            return v || null;
        }
        return null;
    }

    function comparePath(sampleId, standard) {
        const base = `/samples/${encodeURIComponent(String(sampleId))}/compare`;
        return standard ? `${base}?standard=${encodeURIComponent(standard)}` : base;
    }

    // ── the standard a sample opens with ────────────────────────────────────
    /** explicit (the URL) > remembered manual pick > best fit (/api/best-fit's
        best_standard, else the list row's best-fit label when it names one
        standard) > the first standard. Names that aren't standards are skipped. */
    function pickStandard(opts) {
        const o = opts || {};
        const names = (o.standards || []).map(s => (s && typeof s === 'object') ? s.name : s);
        if (!names.length) return null;
        const has = n => typeof n === 'string' && names.includes(n);
        if (has(o.explicit)) return { name: o.explicit, source: 'explicit' };
        if (has(o.remembered)) return { name: o.remembered, source: 'remembered' };
        if (has(o.best)) return { name: o.best, source: 'best' };
        if (has(o.sampleBestFit)) return { name: o.sampleBestFit, source: 'best' };
        return { name: names[0], source: 'first' };
    }

    const PICKS_CAP = 300;
    function parsePicks(raw) {
        let v;
        try { v = typeof raw === 'string' ? JSON.parse(raw) : raw; } catch (_e) { return []; }
        if (!Array.isArray(v)) return [];
        return v.filter(p => Array.isArray(p) && p.length === 2
            && typeof p[0] === 'string' && typeof p[1] === 'string');
    }
    function rememberPick(list, sampleId, name, cap) {
        const key = String(sampleId);
        const out = parsePicks(list).filter(p => p[0] !== key);
        out.push([key, String(name)]);
        const max = cap || PICKS_CAP;
        return out.length > max ? out.slice(out.length - max) : out;
    }
    function recallPick(list, sampleId) {
        const key = String(sampleId);
        const hit = parsePicks(list).find(p => p[0] === key);
        return hit ? hit[1] : null;
    }

    // ── v7: a sample's Adjust state, kept for this tab ──────────────────────
    // The parameters and ranges on screen for a sample, so they still make
    // its report after the view goes (the Samples page unmounts Compare on
    // Overview/Data and on another sample) and when it is opened again.
    // [{sample_id: '<id>', params, ranges}], newest last, at most ADJUST_CAP.
    const ADJUST_CAP = 100;
    function parseAdjustments(raw) {
        let v;
        try { v = typeof raw === 'string' ? JSON.parse(raw) : raw; } catch (_e) { return []; }
        if (!Array.isArray(v)) return [];
        return v.filter(a => a && typeof a === 'object' && typeof a.sample_id === 'string'
            && Array.isArray(a.ranges));
    }
    function rememberAdjustments(list, sampleId, state, cap) {
        const key = String(sampleId);
        const out = parseAdjustments(list).filter(a => a.sample_id !== key);
        const s = state || {};
        out.push({ sample_id: key, params: payload.captureReportParams(s.params || {}),
                   ranges: payload.rangesForPayload(Array.isArray(s.ranges) ? s.ranges : []) });
        const max = cap || ADJUST_CAP;
        return out.length > max ? out.slice(out.length - max) : out;
    }
    /** -> {params, ranges (with UI ids and colours)} or null. */
    function recallAdjustments(list, sampleId) {
        const key = String(sampleId);
        const hit = parseAdjustments(list).find(a => a.sample_id === key);
        if (!hit) return null;
        return {
            params: payload.captureReportParams(hit.params || {}),
            ranges: payload.rangesForPayload(hit.ranges).map((r, i) => ({
                id: i + 1, label: r.label, c_start: r.c_start, c_end: r.c_end,
                color: r.color || RANGE_PALETTE[i % RANGE_PALETTE.length] })),
        };
    }
    function forgetAdjustments(list, sampleId) {
        const key = String(sampleId);
        return parseAdjustments(list).filter(a => a.sample_id !== key);
    }

    // ── findings ────────────────────────────────────────────────────────────
    function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''; }

    /** Split the server's line into the range name and its sentence: the
        line is "• <label> (C..–C..[, evaluated …]): <sentence>" or
        "• <label>: <sentence>"; anything else is kept whole. */
    function splitLine(line, label) {
        const bare = line.replace(/^•\s?/, '');
        if (typeof label === 'string' && label && bare.startsWith(label)) {
            const rest = bare.slice(label.length);
            if (rest.startsWith(' (')) {
                const end = rest.indexOf('): ');
                if (end >= 0) return { heading: label + rest.slice(0, end + 1), text: rest.slice(end + 3) };
            }
            if (rest.startsWith(': ')) return { heading: label, text: rest.slice(2) };
        }
        return { heading: null, text: bare };
    }

    function badgeOf(it) {
        switch (it.kind) {
            case 'none': return { badge: 'Within', tone: 'ok' };
            case 'not-evaluated': return { badge: 'Not evaluated', tone: 'na' };
            case 'no-calibration':
                if (!it.deviates) return { badge: 'No calibration', tone: 'na' };
                break;
            default: break;
        }
        if (!it.severity) return { badge: 'Within', tone: 'ok' };
        const dir = it.mixed ? 'mixed' : (it.direction || '');
        return { badge: cap(it.severity) + (dir ? ' · ' + dir : ''), tone: 'dev' };
    }

    /** /api/analysis → {rows, deviating}. The rows' text is the server's
        `text`, line for line (render_bullets writes one line per item); when
        the two disagree, the lines alone, as plain rows. */
    function findingsView(result) {
        const r = result || {};
        const items = Array.isArray(r.items) ? r.items : [];
        const lines = String(r.text || '').split('\n');
        if (!items.length || lines.length !== items.length) {
            const rows = lines.map(l => l.trim()).filter(Boolean).map((l, i) => ({
                key: i + '-text', kind: 'text', heading: null, text: l.replace(/^•\s?/, ''),
                badge: null, tone: 'na', severity: null, direction: null, t0: null, t1: null,
            }));
            return { rows, deviating: 0 };
        }
        let deviating = 0;
        const rows = items.map((it, i) => {
            const { heading, text } = it.kind === 'none'
                ? { heading: null, text: lines[i] } : splitLine(lines[i], it.label);
            const b = badgeOf(it);
            if (b.tone === 'dev') deviating++;
            let t0 = (typeof it.t0 === 'number') ? it.t0 : null;
            let t1 = (typeof it.t1 === 'number') ? it.t1 : null;
            if (t0 === null && Array.isArray(it.spans) && it.spans.length) {
                t0 = Number(it.spans[0][0]);
                t1 = Number(it.spans[it.spans.length - 1][1]);
            }
            return { key: i + '-' + it.kind, kind: it.kind, heading, text, badge: b.badge,
                     tone: b.tone, severity: it.severity || null,
                     direction: it.mixed ? 'mixed' : (it.direction || null), t0, t1 };
        });
        return { rows, deviating };
    }

    // ── the Adjust drawer ───────────────────────────────────────────────────
    const LABEL_MAX = 40;
    const RANGE_PALETTE = ['#3fb95044', '#d2992244', '#3498db44', '#8e44ad44', '#e67e2244', '#27ae6044'];

    function cleanLabel(s) {
        // one line, as analysis_core.clean_range_label keeps it
        // (Cc, Cf, Zl, Zp → a space: control and format characters, zero-width
        // spaces and joiners, bidi marks and the BOM included)
        return String(s == null ? '' : s).replace(
            /[\u0000-\u001f\u007f-\u009f\u00ad\u0600-\u0605\u061c\u06dd\u070f\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb]/g, ' ')
            .split(/\s+/).filter(Boolean).join(' ');
    }

    /** The drawer's editable state from parameters and range overlays. */
    function draftFrom(params, overlays) {
        const p = Object.assign({}, PARAM_DEFAULTS, params || {});
        return {
            baseline: realToSlider('baseline', p.quantile),
            detail: realToSlider('detail', p.window),
            smoothing: realToSlider('smoothing', p.sigma),
            thresh_marginal: String(p.thresh_marginal),
            thresh_moderate: String(p.thresh_moderate),
            thresh_significant: String(p.thresh_significant),
            x_max_min: String(p.x_max_min),
            ranges: (overlays || []).map((r, i) => ({
                id: r.id != null ? r.id : i + 1, label: String(r.label == null ? '' : r.label),
                c_start: String(r.c_start), c_end: String(r.c_end), color: r.color || '',
            })),
        };
    }

    function num(v) {
        if (v === null || v === undefined || String(v).trim() === '') return NaN;
        return Number(v);
    }
    function isCarbon(v) {
        const n = num(v);
        return Number.isInteger(n) && n >= 1 && n <= 100;
    }

    /** -> {ok, errors: {field: message}, rangeErrors: [{label?, c_start?, c_end?}],
        params, ranges}. Messages are short sentences shown by the field. */
    function validateAdjust(draft) {
        const d = draft || {};
        const errors = {};
        const params = {
            quantile: sliderReal('baseline', d.baseline),
            window: sliderReal('detail', d.detail),
            sigma: sliderReal('smoothing', d.smoothing),
        };
        for (const key of ['thresh_marginal', 'thresh_moderate', 'thresh_significant']) {
            const v = num(d[key]);
            if (!Number.isFinite(v) || v < 0) errors[key] = 'A number, 0 or more.';
            else params[key] = v;
        }
        if (!errors.thresh_marginal && !errors.thresh_moderate
            && params.thresh_moderate < params.thresh_marginal) {
            errors.thresh_moderate = `At least the marginal threshold (${params.thresh_marginal}).`;
        }
        if (!errors.thresh_moderate && !errors.thresh_significant
            && params.thresh_significant < params.thresh_moderate) {
            errors.thresh_significant = `At least the moderate threshold (${params.thresh_moderate}).`;
        }
        const x = num(d.x_max_min);
        if (!Number.isFinite(x) || x <= 0 || x > 1000) errors.x_max_min = 'Minutes, more than 0.';
        else params.x_max_min = x;

        const ranges = [];
        const rangeErrors = [];
        let rangesOk = true;
        (d.ranges || []).forEach((r, i) => {
            const e = {};
            const label = cleanLabel(r.label);
            if (!label) e.label = 'Give the range a name.';
            else if (label.length > LABEL_MAX) e.label = `At most ${LABEL_MAX} characters.`;
            if (!isCarbon(r.c_start)) e.c_start = 'A whole carbon number from 1 to 100.';
            if (!isCarbon(r.c_end)) e.c_end = 'A whole carbon number from 1 to 100.';
            if (!e.c_start && !e.c_end && num(r.c_end) < num(r.c_start)) {
                e.c_end = 'The end must be at or after the start.';
            }
            rangeErrors.push(e);
            if (Object.keys(e).length) { rangesOk = false; return; }
            ranges.push({ id: r.id != null ? r.id : i + 1, label, c_start: num(r.c_start),
                          c_end: num(r.c_end), color: r.color || RANGE_PALETTE[i % RANGE_PALETTE.length] });
        });
        const ok = !Object.keys(errors).length && rangesOk;
        return { ok, errors, rangeErrors, params, ranges };
    }

    function newRange(existing) {
        const list = existing || [];
        const id = list.reduce((m, r) => Math.max(m, Number(r.id) || 0), 0) + 1;
        return { id, label: 'Range ' + (list.length + 1), c_start: '5', c_end: '15',
                 color: RANGE_PALETTE[list.length % RANGE_PALETTE.length] };
    }

    /** One line naming the parameters a report is built with. */
    function paramSummary(params, ranges) {
        const p = Object.assign({}, PARAM_DEFAULTS, params || {});
        const parts = [
            'Baseline ' + sliderLabel(realToSlider('baseline', p.quantile)),
            'Detail ' + sliderLabel(realToSlider('detail', p.window)),
            'Smoothing ' + sliderLabel(realToSlider('smoothing', p.sigma)),
            `Thresholds ${p.thresh_marginal} / ${p.thresh_moderate} / ${p.thresh_significant}`,
        ];
        // the ranges as the report's footer names them (every one, in order)
        const rs = payload.rangesText((ranges || []).map(r => Object.assign({}, r, { label: cleanLabel(r.label) })));
        parts.push(rs || 'No ranges');
        parts.push(`Up to ${p.x_max_min} min`);
        return parts.join(' · ');
    }

    // ── charts ──────────────────────────────────────────────────────────────
    /** A CSS colour (#rgb, #rrggbb, rgb(), rgba()) with an alpha, for Plotly. */
    function withAlpha(color, alpha) {
        const c = String(color || '').trim();
        let rgb = null;
        let a = 1;
        let m = c.match(/^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i);
        if (m) {
            let h = m[1];
            if (h.length === 3) h = h.split('').map(x => x + x).join('');
            rgb = [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16));
            if (h.length === 8) a = parseInt(h.slice(6, 8), 16) / 255;
        } else if ((m = c.match(/^rgba?\(([^)]+)\)$/i))) {
            const parts = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
            if (parts.length >= 3 && parts.slice(0, 3).every(Number.isFinite)) {
                rgb = parts.slice(0, 3).map(Math.round);
                if (parts.length > 3 && Number.isFinite(parts[3])) a = parts[3];
            }
        }
        if (!rgb) rgb = [128, 128, 128];
        const out = Math.round(a * alpha * 1000) / 1000;
        return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${out})`;
    }

    /** The carbon ticks on the trend's top axis (the sample's own ladder),
        thinned to even carbons when there are more than 14. */
    function carbonTicks(times, carbons) {
        const t = Array.isArray(times) ? times : [];
        const c = Array.isArray(carbons) ? carbons : [];
        if (t.length !== c.length || !t.length) return { vals: [], text: [] };
        let pairs = t.map((x, i) => [Number(x), Number(c[i])])
            .filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]));
        if (pairs.length > 14) pairs = pairs.filter(p => p[1] % 2 === 0);
        if (pairs.length > 15) pairs = pairs.filter(p => p[1] % 4 === 0);
        return { vals: pairs.map(p => p[0]), text: pairs.map(p => 'C' + p[1]) };
    }

    /** The difference axis half-span: the data (up to x-max) and the counted
        spikes, at least the marginal threshold; never the significant line. */
    function diffSpan(xs, ys, spikes, marginal, xMax) {
        let span = Number(marginal) || 1;
        (ys || []).forEach((v, i) => {
            if (xMax === null || xMax === undefined || xs[i] <= xMax) {
                if (Number.isFinite(v)) span = Math.max(span, Math.abs(v));
            }
        });
        (spikes || []).forEach(s => { if (Number.isFinite(s.value)) span = Math.max(span, Math.abs(s.value)); });
        return span;
    }


    /** v6: the longest conclusion (characters): the Conclusion box's
        maxlength; mirrors the server's comments.CONCLUSION_MAX, which refuses
        a longer one with a 400 (tests/test_comments_api.py pins the two). */
    const CONCLUSION_MAX = 1500;
    const CONCLUSION_NEAR = 0.9;          // the counter warns from 90 %

    function fmtCount(n) { return Number(n).toLocaleString('en-US'); }

    /** The live counter under the Conclusion editor: "N / 1,500", `near`
        from 90 % of the limit (warning colour), `full` at the limit. */
    function conclusionCount(text) {
        const n = String(text == null ? '' : text).length;
        return { text: `${fmtCount(n)} / ${fmtCount(CONCLUSION_MAX)}`,
                 near: n >= Math.ceil(CONCLUSION_MAX * CONCLUSION_NEAR), full: n >= CONCLUSION_MAX };
    }

    /** A paste into the conclusion over `sel` ({start, end}): the pasted
        text cut so the result fits CONCLUSION_MAX (what is around the caret
        is kept). Returns {text, caret, cut}. */
    function pasteInto(text, pasted, sel) {
        const base = String(text == null ? '' : text);
        const add = String(pasted == null ? '' : pasted);
        const clamp = (n) => Math.max(0, Math.min(base.length, Number.isFinite(n) ? n : base.length));
        const start = clamp(sel && sel.start);
        const end = Math.max(start, clamp(sel && Number.isFinite(sel.end) ? sel.end : start));
        const before = base.slice(0, start);
        const after = base.slice(end);
        const room = Math.max(0, CONCLUSION_MAX - before.length - after.length);
        let piece = add.slice(0, room);
        if (piece.length < add.length && /[\uD800-\uDBFF]$/.test(piece)) piece = piece.slice(0, -1);
        return { text: before + piece + after, caret: before.length + piece.length,
                 cut: piece.length < add.length };
    }

    /** Insert a conclusion preset into the conclusion text: at the caret or
        over the selection (`sel` = {start, end}), else appended to the end;
        padded with one space on a side that touches text. Returns {text,
        caret} (the caret just after the preset), or null when the result
        would be longer than CONCLUSION_MAX. An empty preset changes nothing. */
    function insertPreset(text, preset, sel) {
        const base = String(text == null ? '' : text);
        const piece = String(preset == null ? '' : preset).trim();
        const clamp = (n) => Math.max(0, Math.min(base.length, Number.isFinite(n) ? n : base.length));
        let start = base.length;
        let end = base.length;
        if (sel && Number.isFinite(sel.start)) {
            start = clamp(sel.start);
            end = Math.max(start, clamp(Number.isFinite(sel.end) ? sel.end : sel.start));
        }
        if (!piece) return { text: base, caret: end };
        const before = base.slice(0, start);
        const after = base.slice(end);
        const pre = (!before || /\s$/.test(before)) ? '' : ' ';
        const post = (!after || /^\s/.test(after)) ? '' : ' ';
        const out = before + pre + piece + post + after;
        if (out.length > CONCLUSION_MAX) return null;
        return { text: out, caret: (before + pre + piece).length };
    }

    const api = {
        TREND_SLIDER_MAP, SLIDERS, PARAM_DEFAULTS, LABEL_MAX,
        sliderToReal, realToSlider, sliderReal, sliderLabel,
        defaultParams, analysisBody, standardFromSearch, comparePath,
        pickStandard, parsePicks, rememberPick, recallPick,
        ADJUST_CAP, parseAdjustments, rememberAdjustments, recallAdjustments, forgetAdjustments,
        findingsView, draftFrom, validateAdjust, newRange, paramSummary,
        withAlpha, carbonTicks, diffSpan, cleanLabel,
        CONCLUSION_MAX, insertPreset, conclusionCount, pasteInto,
    };
    root.GCCompareLogic = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
