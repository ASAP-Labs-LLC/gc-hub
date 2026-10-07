// The v4.0 pages' pure logic (Instruments, one instrument, the setup guide,
// the shell): no DOM, node-tested (tests/js/ui_logic.test.js). Window global
// GCUi, module.exports for node. Everything a page shows from here is set
// with textContent by the caller.
(function (root) {
    'use strict';

    const LIVE_SECONDS = 90;           // live.LIVE_SECONDS: the hub's one rule (v4.0 lane E)
    const AGENT_ERROR_STATES = ['hub-unreachable', 'auth-error', 'config-error'];

    function parseMs(iso) {
        if (!iso || typeof iso !== 'string') return null;
        const ms = Date.parse(iso);
        return isNaN(ms) ? null : ms;
    }

    function relTime(iso, nowMs) {
        const at = parseMs(iso);
        if (at === null) return null;
        const s = Math.round((nowMs - at) / 1000);
        if (s < 5) return 'just now';
        if (s < 60) return s + ' s ago';
        const m = Math.floor(s / 60);
        if (m < 60) return m + ' min ago';
        const h = Math.floor(m / 60);
        if (h < 48) return h + ' h ago';
        return Math.floor(h / 24) + ' d ago';
    }

    const pad = (n) => String(n).padStart(2, '0');
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    // "12:31" today, "Sep 29 12:31" otherwise (this browser's local time).
    function clockTime(iso, nowMs) {
        const at = parseMs(iso);
        if (at === null) return '';
        const d = new Date(at);
        const n = new Date(nowMs === undefined ? Date.now() : nowMs);
        const hm = pad(d.getHours()) + ':' + pad(d.getMinutes());
        if (d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate()) return hm;
        return MONTHS[d.getMonth()] + ' ' + d.getDate() + ' ' + hm;
    }

    // web_auth.actor() is '<name> (<address>)': the name, or the address alone.
    function actorName(by) {
        if (!by || typeof by !== 'string') return null;
        const m = /^(.*\S)\s+\([^()]*\)$/.exec(by.trim());
        return m ? m[1] : by.trim();
    }

    function initials(name) {
        const words = String(name || '').trim().split(/\s+/).filter(Boolean);
        if (!words.length) return '?';
        return words.slice(0, 2).map(w => w[0].toUpperCase()).join('');
    }

    // Seconds since the agent checked in: the hub's age (`last_seen_age_s`)
    // plus the time since it was read (`read_at`, stamped by live.js); null
    // when the hub gave none.
    function agentAge(agent, nowMs) {
        const age = agent && agent.last_seen_age_s;
        if (typeof age !== 'number' || !isFinite(age)) return null;
        const at = agent.read_at;
        const extra = typeof at === 'number' && isFinite(at) ? Math.max(0, (nowMs - at) / 1000) : 0;
        return Math.round(age + extra);
    }

    // Agent status with its glyph (never by colour alone): Live / Last seen N
    // min ago / Never checked in; Paused and Reporting an error when live.
    // Live is the hub's rule (v4.0 lane E): its `live` flag, and still within
    // 90 s by its age; the browser's clock is never compared with last_seen.
    function agentStatus(agent, nowMs) {
        const at = parseMs(agent && agent.last_seen);
        if (at === null) return { glyph: 'never', label: 'Never checked in', since: null };
        const age = agentAge(agent, nowMs);
        const ago = age === null ? relTime(agent.last_seen, nowMs)
            : relTime(new Date(nowMs - age * 1000).toISOString(), nowMs);
        const since = 'checked in ' + ago;
        const live = agent.live === true && (age === null || age <= LIVE_SECONDS);
        if (!live) return { glyph: 'held', label: 'Last seen ' + ago, since };
        // `status` is GCLive's (the agent's own state), `state` is /api/agents'
        const state = agent.status || agent.state;
        if (state === 'paused') return { glyph: 'held', label: 'Paused', since };
        if (AGENT_ERROR_STATES.includes(state) || agent.last_error) {
            return { glyph: 'error', label: 'Reporting an error', since };
        }
        return { glyph: 'final', label: 'Live', since };
    }

    const SAMPLE_STATUS = {
        final: { glyph: 'final', text: 'Final' },
        received: { glyph: 'working', text: 'Processing' },
        awaiting_calibration: { glyph: 'held', text: 'Waiting for calibration' },
        pending_corrections: { glyph: 'held', text: 'Waiting for correction factors' },
        other_method: { glyph: 'held', text: 'Another method' },
        review_method: { glyph: 'held', text: 'Method to review' },
        raw_only: { glyph: 'held', text: 'Raw only' },
        error: { glyph: 'error', text: 'Error' },
    };
    function sampleStatus(status) {
        return Object.assign({}, SAMPLE_STATUS[status] || { glyph: 'held', text: String(status || 'Unknown') });
    }

    // One wording on every page: "5 of 8 done · Next: Wait for the agent to check in".
    function setupLabel(summary) {
        if (!summary) return '';
        if (summary.ready) return 'Ready';
        return summary.done + ' of ' + summary.total + ' done' +
            (summary.next_title ? ' · Next: ' + summary.next_title : '');
    }

    const STEP_BADGE = { done: 'Done', current: 'Now', waiting: 'Waiting', blocked: 'Blocked' };
    function stepBadge(status) { return STEP_BADGE[status] || ''; }

    // The sidebar's "Setup guide" item names the GC ("GC-2 · 5/8"): every
    // enabled, unfinished instrument, the one being viewed (`preferId`) first,
    // then the furthest along (then by id); null when every instrument is ready.
    function setupNav(instruments, preferId) {
        const open = (instruments || []).filter(i => i && i.enabled && i.setup && !i.setup.ready && i.setup.step)
            .sort((a, b) => ((b.id === preferId) - (a.id === preferId)) ||
                ((b.setup.done || 0) - (a.setup.done || 0)) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
        if (!open.length) return null;
        const one = (i) => (i.name || i.id) + ' · ' + (i.setup.done || 0) + '/' + i.setup.total;
        const sm = open[0].setup;
        return { instrument_id: open[0].id, step: sm.step, text: open.map(one).join(', '),
                 title: 'Setup: ' + open.map(i => (i.name || i.id) + ' · ' + setupLabel(i.setup)).join('; ') };
    }

    // ── the Activity feed ──────────────────────────────────────────────────
    const ACTIVITY_KINDS = ['created', 'installer', 'token_revoked', 'calibration_cdf', 'calibration_saved',
        'corrections_saved', 'method_mapped', 'export_path', 'export_adopted', 'live_since',
        'sample_received', 'report', 'export_written', 'agent_seen'];

    const ICONS = {
        created: 'plus', installer: 'download', token_revoked: 'key', calibration_cdf: 'target',
        calibration_saved: 'target', corrections_saved: 'thermo', method_mapped: 'flask',
        export_path: 'file', export_adopted: 'file', live_since: 'play', sample_received: 'inbox',
        report: 'doc', export_written: 'check', agent_seen: 'signal',
    };
    function activityIcon(kind) { return ICONS[kind] || 'dot'; }

    // [{text, strong?}] segments; the page sets each with textContent.
    function activityText(e) {
        const d = e.detail || {};
        const inst = { text: e.instrument_name || e.instrument_id || '?', strong: true };
        const who = { text: actorName(e.by) || 'The hub' };
        const T = (text) => ({ text });
        const S = (text) => ({ text: String(text), strong: true });
        switch (e.kind) {
            case 'created': return [who, T(' added '), inst];
            case 'installer': return [who, T(' downloaded the '), inst, T(' installer')];
            case 'token_revoked': return [who, T(' revoked the '), inst, T(' agent key')];
            case 'calibration_cdf': return [who, T(' chose the '), inst, T(' calibration run' + (d.file ? ' · ' + d.file : ''))];
            case 'calibration_saved': return [who, T(' saved the '), inst, T(' calibration' + (d.assigned !== undefined ? ' · ' + d.assigned + ' peaks' : ''))];
            case 'corrections_saved': return [who, T(' saved '), inst, T(' correction factors' + (d.changed !== undefined ? ' (' + d.changed + ' changed)' : ''))];
            case 'method_mapped':
                return d.hub_method
                    ? [who, T(' mapped '), S(d.method || '?'), T(' on '), inst, T(' to ' + d.hub_method)]
                    : [who, T(' unmapped '), S(d.method || '?'), T(' on '), inst];
            case 'export_path': return [who, T(' set the '), inst, T(' results file')];
            case 'export_adopted': return [who, T(' adopted the '), inst, T(' results file')];
            case 'live_since':
                return d.live_since
                    ? [who, T(' set '), inst, T(' live from ' + String(d.live_since).slice(0, 16))]
                    : [who, T(' took '), inst, T(' off live')];
            case 'sample_received': return [inst, T(' sent '), S(e.lab_id || '?')];
            case 'report':
                return d.report_kind === 'qbench'
                    ? [who, T(' uploaded the '), S(e.lab_id || '?'), T(' report to QBench')]
                    : [who, T(' downloaded the '), S(e.lab_id || '?'), T(' report')];
            case 'export_written': return [S(e.lab_id || '?'), T(' · Written to results CSV ('), inst, T(')')];
            case 'agent_seen': return [inst, T(' agent checked in')];
            case 'purge': return [who, T(' purged '), S(d.samples !== undefined ? d.samples : '?'),
                T(' samples of '), inst, T(d.scope === 'backfill' ? ' (imported history)' : '')];
            default: return [inst, T(' · ' + String(e.kind || 'activity'))];
        }
    }

    // Merge a fresh answer into what the page shows: keys not shown before
    // (or shown with another time, like an agent's check-in) are "added" and
    // go to the top; newest first; at most `max`.
    function mergeActivity(existing, incoming, max) {
        const byKey = new Map((existing || []).map(e => [e.key, e]));
        const added = [];
        for (const e of incoming || []) {
            const old = byKey.get(e.key);
            if (!old || old.at !== e.at) added.push(e.key);
            byKey.set(e.key, e);
        }
        const list = Array.from(byKey.values()).sort((a, b) => (parseMs(b.at) || 0) - (parseMs(a.at) || 0));
        return { list: list.slice(0, max || 50), added };
    }

    // ── the admin password, kept in a closure for `ttlMs` (never storage) ──
    function makeAdminGate(opts) {
        const ttl = (opts && opts.ttlMs) || 15 * 60 * 1000;
        const now = (opts && opts.now) || (() => Date.now());
        let secret = null;
        let until = 0;
        return {
            get() {
                if (secret !== null && now() > until) { secret = null; until = 0; }
                return secret;
            },
            set(pw) { secret = String(pw); until = now() + ttl; },
            clear() { secret = null; until = 0; },
            remainingMs() { return secret === null ? 0 : Math.max(0, until - now()); },
            toJSON() { return {}; },
        };
    }

    // ── recents "on this computer" (localStorage, via the shell) ───────────
    const SAFE_HREF = /^\/(?![\/\\])[^\s\\]*$/;
    function recentClean(list) {
        return (Array.isArray(list) ? list : []).filter(r => r && typeof r === 'object' &&
            typeof r.href === 'string' && SAFE_HREF.test(r.href) && typeof r.label === 'string');
    }
    function recentAdd(list, item, max, atMs) {
        const rest = recentClean(list).filter(r => r.href !== item.href);
        return [{ href: item.href, label: String(item.label || item.href).slice(0, 60), at: atMs }]
            .concat(rest).slice(0, max || 6);
    }

    // v5.0: System (follow the OS's prefers-color-scheme, live), Light or
    // Dark; anyone who hasn't chosen (or junk in storage) gets System.
    const THEME_CHOICES = ['system', 'light', 'dark'];
    function themeChoice(stored) {
        return THEME_CHOICES.includes(stored) ? stored : 'system';
    }
    function resolveTheme(pref, prefersDark) {
        const choice = themeChoice(pref);
        if (choice === 'system') return prefersDark ? 'dark' : 'light';
        return choice;
    }

    // ── the 5 s fallback poll when live.js is absent ───────────────────────
    function diffSummaries(prevMap, list) {
        const map = {};
        const changed = [];
        for (const i of list || []) {
            const copy = Object.assign({}, i);
            delete copy.agent;             // heartbeats arrive as agent updates
            const sig = JSON.stringify(copy);
            map[i.id] = sig;
            if (!prevMap || prevMap[i.id] !== sig) changed.push(i.id);
        }
        return { map, changed };
    }

    function agentsFromStatus(rows) {
        return (Array.isArray(rows) ? rows : []).map(a => ({
            instrument_id: a.instrument_id, last_seen: a.last_seen || null, version: a.version || null,
            host: a.host || null, status: a.state || null,
        }));
    }

    // ── "Go live now": the hub's clock, never the browser's ────────────────
    const LOCAL_TS = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$/;
    function parseLocal(text) {
        const m = LOCAL_TS.exec(String(text || ''));
        return m ? Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : null;
    }
    function formatLocal(ms) {
        const d = new Date(ms);
        return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + ' ' +
            pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds());
    }
    // hubLocalNow: the hub's local time when the page loaded (loadedAtMs, the
    // browser's clock then); skewSeconds: the GC PC's clock minus the hub's, when
    // an agent has reported it. live_since is the GC's clock, so that is used
    // when known. -> {value, basis: gc|hub, text} or null.
    function goLiveTime(hubLocalNow, loadedAtMs, nowMs, skewSeconds) {
        const base = parseLocal(hubLocalNow);
        if (base === null) return null;
        const hubNow = base + Math.max(0, nowMs - loadedAtMs);
        const known = typeof skewSeconds === 'number' && isFinite(skewSeconds);
        const value = formatLocal(hubNow + (known ? skewSeconds * 1000 : 0));
        return {
            value, basis: known ? 'gc' : 'hub',
            text: known ? value.slice(0, 16) + " by the GC's clock"
                : value.slice(0, 16) + " by the hub's clock (the GC's clock is unknown until its agent checks in)",
        };
    }

    function liveSinceState(liveSince, hubLocalNow) {
        if (!liveSince) return { live: false, text: 'Not live' };
        const at = String(liveSince).slice(0, 16);
        if (hubLocalNow && String(liveSince) > String(hubLocalNow)) return { live: false, text: 'Waiting until ' + at };
        return { live: true, text: 'Live since ' + at };
    }

    // Ids the hub's page addresses use: such an instrument has no page of its
    // own (v6.0.0: the classic Instruments page that could open it is gone).
    const RESERVED_IDS = ['activity', 'classic', 'new', 'setup'];
    const UNREACHABLE_IDS = ['activity', 'classic'];
    function instrumentHref(id) {
        if (UNREACHABLE_IDS.includes(id)) return '/instruments';
        return '/instruments/' + encodeURIComponent(id);
    }

    function activitySample(e) {
        if (!e || e.sample_id === null || e.sample_id === undefined) return null;
        return { href: '/samples/' + encodeURIComponent(e.sample_id),
                 injected: e.injection_dt ? 'injected ' + String(e.injection_dt).slice(0, 16) : null };
    }

    function lemTitle(answer, uid) {
        if (!uid) return null;
        const list = answer && Array.isArray(answer.machines) ? answer.machines : [];
        const m = list.find(x => x && x.uid === uid);
        return m && typeof m.title === 'string' && m.title ? m.title : uid;
    }

    const api = {
        LIVE_SECONDS, ACTIVITY_KINDS, relTime, clockTime, actorName, initials, agentStatus, agentAge,
        sampleStatus,
        setupLabel, stepBadge, setupNav, activityText, activityIcon, mergeActivity, makeAdminGate,
        recentAdd, recentClean, resolveTheme, themeChoice, THEME_CHOICES, diffSummaries, agentsFromStatus, lemTitle,
        goLiveTime, liveSinceState, RESERVED_IDS, instrumentHref, activitySample,
    };
    root.GCUi = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
