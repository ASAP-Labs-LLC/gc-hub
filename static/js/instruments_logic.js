// The Instruments page's pure logic (2A2): no DOM, node-tested
// (tests/js/instruments.test.js). Window globals under InstrumentsLogic,
// module.exports for node.
(function (root) {
    'use strict';

    const SKEW_WARN_SECONDS = 120;
    const STALE_MINUTES = 15;          // spec, Notifications: an agent not seen for 15 minutes

    function formatSkew(seconds) {
        if (seconds === null || seconds === undefined || !isFinite(seconds)) return 'unknown';
        const abs = Math.abs(Math.round(seconds));
        if (abs < 5) return 'in step';
        const side = seconds > 0 ? 'ahead' : 'behind';
        if (abs < 60) return abs + ' s ' + side;
        const m = Math.floor(abs / 60);
        const s = abs % 60;
        return m + ' min' + (s ? ' ' + s + ' s' : '') + ' ' + side;
    }

    function skewWarning(seconds, threshold) {
        const limit = threshold === undefined ? SKEW_WARN_SECONDS : threshold;
        if (seconds === null || seconds === undefined || !isFinite(seconds)) return null;
        if (Math.abs(seconds) <= limit) return null;
        return "The GC PC's clock is " + formatSkew(seconds) + " of the hub's clock.";
    }

    // agent: a row of GET /api/agents. level: never | stale | paused | error | ok.
    function agentHealth(agent, nowMs) {
        if (!agent || !agent.last_seen) return { level: 'never', text: 'Agent has never reported' };
        const seen = Date.parse(agent.last_seen);
        if (isNaN(seen)) return { level: 'never', text: 'Agent has never reported' };
        const ageMin = (nowMs - seen) / 60000;
        if (ageMin > STALE_MINUTES) {
            return { level: 'stale', text: 'Not seen for ' + Math.round(ageMin) + ' min' };
        }
        if (agent.state === 'paused') return { level: 'paused', text: 'Paused' };
        if (agent.last_error) return { level: 'error', text: 'Reporting an error' };
        return { level: 'ok', text: 'Connected' };
    }

    const NUMBER_RE = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

    // inputs: {cut: text}. Mirrors corrections.validate_values: every cut, a
    // finite number, |v| <= maxAbs. {values|null, errors: [text]}.
    function parseCorrections(inputs, cuts, maxAbs) {
        const values = {};
        const errors = [];
        for (const cut of cuts) {
            const raw = String((inputs || {})[cut] === undefined ? '' : inputs[cut]).trim();
            if (raw === '') { errors.push(cut + ': enter a value (0 for no correction).'); continue; }
            if (!NUMBER_RE.test(raw)) { errors.push(cut + ': "' + raw + '" is not a number.'); continue; }
            const v = Number(raw);
            if (!isFinite(v)) { errors.push(cut + ': "' + raw + '" is not a number.'); continue; }
            if (Math.abs(v) > maxAbs) { errors.push(cut + ': ' + v + ' °C is beyond ±' + maxAbs + ' °C.'); continue; }
            values[cut] = v;
        }
        return { values: errors.length ? null : values, errors };
    }

    const LOCAL_RE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(:\d{2})?$/;

    // From <input type="datetime-local"> (naive local) to the store's form.
    function liveSinceValue(text) {
        const raw = String(text || '').trim();
        if (!raw) return { value: '', error: null };
        const m = LOCAL_RE.exec(raw);
        if (!m) return { value: null, error: 'Enter the GC\'s local date and time, e.g. 2026-10-01 08:00 (no time zone).' };
        return { value: m[1] + ' ' + m[2] + (m[3] || ':00'), error: null };
    }

    function liveSinceInput(stored) {
        if (!stored) return '';
        return String(stored).replace(' ', 'T');
    }

    // POST /api/admin/instruments/<id>/installer answer -> what the page does.
    function installerOutcome(status, body) {
        if (status === 200) return { kind: 'download', message: '' };
        const b = body || {};
        if (status === 409 && b.needs_confirm) {
            return { kind: 'confirm', message: (b.error || 'This instrument already has an agent token.') +
                (b.hub_url ? ' The new installer will point at ' + b.hub_url + '.' : '') +
                ' Download a new installer and revoke the old token?' };
        }
        if (status === 409 && b.needs_hub_url) {
            return { kind: 'needs_hub_url', message: b.error || 'Set the hub URL first.' };
        }
        return { kind: 'error', message: b.error || ('HTTP ' + status) };
    }

    // GET /api/instruments/<id>/methods "seen" -> table rows with the action offered.
    function methodRows(seen) {
        const rows = (seen || []).map(r => ({
            name: r.method_name,
            label: r.method_name ? r.method_name : '(no method name)',
            count: r.count || 0,
            first_seen: r.first_seen || null,
            last_seen: r.last_seen || null,
            mapped_to: r.mapped_to || null,
            action: !r.method_name ? 'review' : (r.mapped_to ? 'unmap' : 'map'),
        }));
        rows.sort((a, b) => {
            if (!a.name !== !b.name) return a.name ? -1 : 1;
            return a.label < b.label ? -1 : a.label > b.label ? 1 : 0;
        });
        return rows;
    }

    // D12: the sample's instrument's standards first, then the others (cross).
    function orderStandards(list, sampleInstrument) {
        const own = [];
        const other = [];
        for (const s of list || []) {
            if (s.instrument_id && s.instrument_id === sampleInstrument) own.push(Object.assign({}, s, { cross: false }));
            else other.push(Object.assign({}, s, { cross: true }));
        }
        return own.concat(other);
    }

    function releaseSummary(results) {
        const list = results || [];
        if (!list.length) return 'Nothing released.';
        const ok = list.filter(r => r.ok).length;
        const bad = list.filter(r => !r.ok);
        let text = ok + ' released';
        if (bad.length) {
            text += ', ' + bad.length + ' refused (' + bad.map(r => r.sample_id + ': ' + r.error).join('; ') + ')';
        }
        return text;
    }

    function calibrationBadge(status) {
        if (!status) return { text: 'Calibration unknown', level: 'bad' };
        if (status.usable) return { text: 'Calibration usable (' + (status.assigned || 0) + ' anchors)', level: 'ok' };
        return { text: status.problem || 'Calibration not usable', level: 'bad' };
    }

    const api = {
        SKEW_WARN_SECONDS, formatSkew, skewWarning, agentHealth, parseCorrections,
        liveSinceValue, liveSinceInput, installerOutcome, methodRows, orderStandards,
        releaseSummary, calibrationBadge,
    };
    root.InstrumentsLogic = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
