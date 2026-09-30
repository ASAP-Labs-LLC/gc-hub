/* running_now.js: what is happening right now, on the classic main page
   (v4.0 lane E; spec "Global status, a way home, and Hub admin").

   From GCLive's /api/live answers (tasks, agents, hub) it keeps three things
   current, without a reload:
     #running-now          the indicator at bottom-left (it replaced "Ready"):
                           one line, most urgent first; click for the list,
                           titled "Running now" while something runs, else
                           "Recent work". A running task shows its title and
                           progress; an ended one ONE outcome line from the hub
                           ("Re-processed 1 sample", "Dry run finished · 40
                           classified"), who, and when it finished. Open /
                           Download / Dismiss; Dismiss keeps the list open.
                           Ended tasks stay 30 minutes (the hub drops them) or
                           until dismissed in this browser (localStorage, keyed
                           "<boot id>:<task id>": task ids restart with the hub).
     #gc-strip             ONE compact chip, "2/2 GCs live" (glyph + text, never
                           colour alone); click for each GC's status and a link
                           to its page. `live` is the hub's rule (seen within
                           90 s on the hub's clock); the age ticks from when the
                           answer was read (GCLive.agentAge), re-rendered every
                           15 s, so "Not seen for …" never freezes.
     #paused-banner        one line while processing is paused.
   The pure helpers are module.exports for tests/js/running_now.test.js; the
   DOM part mounts itself when the page has those elements and GCLive. Every
   string is set with textContent. The shell's sidebar footer (lane E2) can use
   summary(), headline(), gcStrip() and gcSummary() as they are. */
(function (root) {
    'use strict';

    const LV = (typeof module !== 'undefined' && module.exports && typeof require === 'function')
        ? require('./live.js') : root.GCLive;
    const DISMISS_KEY = 'gc-running-dismissed';
    const DISMISS_MAX = 200;
    const STRIP_TICK_MS = 15000;
    const GLYPHS = { running: 'spinner', done: 'dot', failed: 'triangle', stopped: 'ring',
                     interrupted: 'ring' };
    const VERBS = { done: 'finished', failed: 'failed', stopped: 'stopped',
                    interrupted: 'interrupted' };

    function fmtNum(n) {
        return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    }

    function _num(v) {
        return typeof v === 'number' && isFinite(v) ? v : null;
    }

    /** A running task's progress in words. */
    function progressText(task) {
        const p = (task && task.progress) || {};
        const done = _num(p.done);
        const total = _num(p.total);
        const count = total !== null ? fmtNum(done || 0) + ' of ' + fmtNum(total) : null;
        if (p.text && count) return p.text + ' · ' + count;
        if (p.text) return p.text;
        if (count) return count;
        return 'Starting…';
    }

    /** The task's one headline: its title while running, the hub's outcome
        line once it ended (an older hub without one: title + state). */
    function headline(task) {
        if (task.state === 'running') return task.title;
        if (task.outcome) return task.outcome;
        return task.title + ' ' + (VERBS[task.state] || 'ended');
    }

    /** The second line: progress while running; nothing once ended (the
        outcome already says it). */
    function detailLine(task) {
        return task.state === 'running' ? progressText(task) : null;
    }

    function _hm(iso) {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return null;
        return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    }

    /** "Ryan C · started 14:02" / "Ryan C · finished 14:05" (local time). */
    function metaLine(task) {
        const running = task.state === 'running';
        const at = _hm(running ? task.started_at : task.ended_at);
        const when = at ? (running ? 'started ' : task.state === 'done' ? 'finished ' : 'ended ') + at
            : null;
        return [task.by, when].filter(Boolean).join(' · ');
    }

    function popoverTitle(tasks) {
        return (tasks || []).some(t => t.state === 'running') ? 'Running now' : 'Recent work';
    }

    /** The collapsed indicator ({text, glyph, state, count}), or null. */
    function summary(tasks) {
        const list = tasks || [];
        if (!list.length) return null;
        const running = list.filter(t => t.state === 'running');
        if (running.length > 1) {
            return { text: running.length + ' running', glyph: 'spinner', state: 'running',
                     count: list.length };
        }
        if (running.length === 1) {
            const t = running[0];
            const p = t.progress || {};
            const total = _num(p.total);
            let tail = '';
            if (total !== null) tail = ' · ' + fmtNum(_num(p.done) || 0) + '/' + fmtNum(total);
            else if (p.text) tail = ' · ' + p.text;
            return { text: t.title + tail, glyph: 'spinner', state: 'running', count: list.length };
        }
        const t = list[0];                     // the hub sends the most recent first
        return { text: headline(t), glyph: GLYPHS[t.state] || 'ring', state: t.state,
                 count: list.length };
    }

    function dismissKey(boot, task) {
        return String(boot || '') + ':' + task.id;
    }

    /** The tasks this browser has not dismissed (running ones always show). */
    function visibleTasks(tasks, dismissed, boot) {
        return (tasks || []).filter(t => t.state === 'running' || !dismissed.has(dismissKey(boot, t)));
    }

    function _storage(s) {
        if (s !== undefined) return s;
        try { return root.localStorage || null; } catch (_) { return null; }
    }

    function loadDismissed(storage) {
        try {
            const st = _storage(storage);
            const raw = st ? st.getItem(DISMISS_KEY) : null;
            const list = raw ? JSON.parse(raw) : [];
            return new Set(Array.isArray(list) ? list.filter(x => typeof x === 'string') : []);
        } catch (_) {
            return new Set();
        }
    }

    function saveDismissed(set, storage) {
        try {
            const st = _storage(storage);
            if (st) st.setItem(DISMISS_KEY, JSON.stringify([...set].slice(-DISMISS_MAX)));
        } catch (_) { /* not remembered: private window, blocked storage */ }
    }

    function agoSeconds(s) {
        if (s < 60) return s + ' s';
        if (s < 3600) return Math.floor(s / 60) + ' min';
        if (s < 86400) return Math.floor(s / 3600) + ' h';
        return Math.floor(s / 86400) + ' d';
    }

    /** One GC at ``nowMs``: {id, name, glyph, text, cls, href, title}. Its age
        is the hub's plus the time since the answer was read; ``pattern``'s
        "{id}" is replaced (default the GC's own page). */
    function gcChip(agent, nowMs, pattern) {
        const id = String(agent.instrument_id);
        const name = agent.name || id;
        const href = (pattern || '/instruments/{id}').replace('{id}', encodeURIComponent(id));
        const age = LV.agentAge(agent, nowMs);
        const bits = [name];
        if (agent.host) bits.push(agent.host);
        if (agent.version) bits.push('agent ' + agent.version);
        let glyph = '○';
        let text;
        let cls;
        if (agent.enabled === false) {
            glyph = '–'; text = 'Disabled'; cls = 'off';
        } else if (LV.agentLive(agent, nowMs)) {
            glyph = '●'; text = 'Live'; cls = 'live';
        } else if (age === null) {
            text = 'Never checked in'; cls = 'never';
        } else {
            text = 'Not seen for ' + agoSeconds(age); cls = 'quiet';
        }
        bits.push(age === null ? 'the agent has never checked in'
                               : 'checked in ' + agoSeconds(age) + ' ago');
        return { id, name, glyph, text, cls, href, title: bits.join(' · ') };
    }

    /** The toolbar's one chip: {text: "2/2 GCs live", glyph, cls, title}, or
        null without any GC. Disabled GCs are not counted. */
    function gcStrip(agents, nowMs) {
        const all = agents || [];
        if (!all.length) return null;
        const chips = all.map(a => gcChip(a, nowMs));
        const on = chips.filter(c => c.cls !== 'off');
        const live = on.filter(c => c.cls === 'live').length;
        const allLive = on.length > 0 && live === on.length;
        const noneSeen = on.length > 0 && on.every(c => c.cls === 'never');
        return { text: live + '/' + on.length + (on.length === 1 ? ' GC live' : ' GCs live'),
                 glyph: allLive ? '●' : '○',
                 cls: allLive ? 'live' : (noneSeen ? 'never' : 'quiet'),
                 title: chips.map(c => c.name + ': ' + c.text).join(' · ') };
    }

    /** "2 of 2 GCs connected" (enabled GCs only); '' without any. */
    function gcSummary(agents, nowMs) {
        const on = (agents || []).filter(a => a.enabled !== false);
        if (!on.length) return '';
        const live = on.filter(a => LV.agentLive(a, nowMs)).length;
        return live + ' of ' + on.length + (on.length === 1 ? ' GC' : ' GCs') + ' connected';
    }

    /** The processing-paused banner's line, or null. */
    function pausedBanner(hub) {
        if (!hub || hub.processing_paused !== true) return null;
        const who = [hub.paused_by, hub.paused_since ? _hm(hub.paused_since) : null].filter(Boolean);
        const waiting = hub.queue ? _num(hub.queue.waiting) : null;
        return 'Processing is paused' + (who.length ? ' (' + who.join(', ') + ')' : '') +
            '. New runs are received and wait' + (waiting ? ': ' + fmtNum(waiting) + ' waiting' : '') +
            '. Resume it from the hub tray on the server.';
    }

    const pure = { DISMISS_KEY, DISMISS_MAX, fmtNum, progressText, headline, detailLine, metaLine,
                   popoverTitle, summary, dismissKey, visibleTasks, loadDismissed, saveDismissed,
                   gcChip, gcStrip, gcSummary, pausedBanner };
    root.GCRunningNow = pure;
    if (typeof module !== 'undefined' && module.exports) module.exports = pure;
    if (typeof document === 'undefined') return;

    // ── the page ──────────────────────────────────────────────────────────
    const $ = (id) => document.getElementById(id);
    let last = { tasks: [], hub: null, boot: null };
    let dismissed = loadDismissed();
    let openPop = null;                 // 'tasks' | 'gcs' | null

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = String(text);
        return e;
    }

    function glyph(kind) {
        const g = el('span', 'rn-glyph rn-glyph-' + kind);
        g.setAttribute('aria-hidden', 'true');
        g.textContent = { spinner: '', dot: '●', triangle: '▲', ring: '○' }[kind] || '';
        return g;
    }

    function shown() {
        return visibleTasks(last.tasks, dismissed, last.boot);
    }

    function renderIndicator() {
        const btn = $('running-now');
        if (!btn) return;
        const tasks = shown();
        const s = summary(tasks);
        btn.hidden = !s;
        btn.replaceChildren();
        if (!s) { if (openPop === 'tasks') setOpen(null); return; }
        btn.dataset.state = s.state;
        btn.append(glyph(s.glyph), el('span', 'rn-text', s.text));
        const one = tasks.filter(t => t.state === 'running');
        const p = one.length === 1 ? (one[0].progress || {}) : {};
        if (_num(p.total) && p.total > 0) {
            const bar = el('span', 'rn-bar');
            const fill = el('span', 'rn-bar-fill');
            fill.style.width = Math.min(100, Math.round(100 * (_num(p.done) || 0) / p.total)) + '%';
            bar.appendChild(fill);
            btn.appendChild(bar);
        }
        btn.setAttribute('aria-expanded', openPop === 'tasks' ? 'true' : 'false');
        if (openPop === 'tasks') renderTasksPopover();
    }

    function action(label, cls, attrs) {
        const a = el(attrs.href ? 'a' : 'button', 'rn-action ' + cls, label);
        for (const [k, v] of Object.entries(attrs)) {
            if (k === 'onclick') a.addEventListener('click', v);
            else a.setAttribute(k, v);
        }
        return a;
    }

    function renderTasksPopover() {
        const pop = $('running-now-popover');
        if (!pop) return;
        const tasks = shown();
        const list = el('ul', 'rn-list');
        for (const t of tasks) {
            const li = el('li', 'rn-task rn-' + t.state);
            li.dataset.taskId = t.id;
            const head = el('div', 'rn-task-title');
            head.append(glyph(GLYPHS[t.state] || 'ring'), el('span', null, headline(t)));
            li.appendChild(head);
            const detail = detailLine(t);
            if (detail) li.appendChild(el('div', 'rn-task-line', detail));
            li.appendChild(el('div', 'rn-task-meta', metaLine(t)));
            const acts = el('div', 'rn-actions');
            if (t.open_url) acts.appendChild(action('Open', 'rn-open', { href: t.open_url }));
            if (t.download_url) {
                acts.appendChild(action('Download', 'rn-download', { href: t.download_url, download: '' }));
            }
            if (t.state !== 'running') {
                acts.appendChild(action('Dismiss', 'rn-dismiss', { type: 'button', onclick: () => {
                    dismissed.add(dismissKey(last.boot, t));
                    saveDismissed(dismissed);
                    renderIndicator();         // the list stays open while tasks remain
                } }));
            }
            if (acts.childNodes.length) li.appendChild(acts);
            list.appendChild(li);
        }
        pop.replaceChildren(el('div', 'rn-pop-title', popoverTitle(tasks)), list);
    }

    // ── the GC strip ──
    function agentsNow() {
        return root.GCLive ? root.GCLive.agents() : [];
    }

    function renderStrip() {
        const btn = $('gc-strip');
        if (!btn) return;
        const now = Date.now();
        const s = gcStrip(agentsNow(), now);
        btn.hidden = !s;
        btn.replaceChildren();
        if (!s) { if (openPop === 'gcs') setOpen(null); return; }
        btn.className = 'gc-strip gc-strip-' + s.cls;
        btn.title = s.title;
        const g = el('span', 'gc-chip-glyph', s.glyph);
        g.setAttribute('aria-hidden', 'true');
        btn.append(g, el('span', 'gc-strip-text', s.text));
        btn.setAttribute('aria-expanded', openPop === 'gcs' ? 'true' : 'false');
        if (openPop === 'gcs') renderGcPopover();
    }

    function renderGcPopover() {
        const pop = $('gc-strip-popover');
        if (!pop) return;
        const now = Date.now();
        const list = el('ul', 'rn-list');
        for (const a of agentsNow()) {
            const c = gcChip(a, now);
            const li = el('li', 'gc-row gc-chip-' + c.cls);
            li.dataset.instrument = c.id;
            const link = el('a', 'gc-row-name', c.name);
            link.href = c.href;
            link.title = c.title;
            const g = el('span', 'gc-chip-glyph', c.glyph);
            g.setAttribute('aria-hidden', 'true');
            li.append(g, link, el('span', 'gc-row-text', c.text));
            list.appendChild(li);
        }
        pop.replaceChildren(el('div', 'rn-pop-title', 'GC agents'), list);
    }

    function setOpen(which) {
        openPop = which;
        const tp = $('running-now-popover');
        const gp = $('gc-strip-popover');
        if (tp) tp.hidden = which !== 'tasks';
        if (gp) gp.hidden = which !== 'gcs';
        const rb = $('running-now');
        const gb = $('gc-strip');
        if (rb) rb.setAttribute('aria-expanded', which === 'tasks' ? 'true' : 'false');
        if (gb) gb.setAttribute('aria-expanded', which === 'gcs' ? 'true' : 'false');
        if (which === 'tasks') renderTasksPopover();
        if (which === 'gcs' && gp && gb) {
            // fixed, under its chip: the toolbar scrolls sideways and would clip it
            const r = gb.getBoundingClientRect();
            gp.style.top = Math.round(r.bottom + 6) + 'px';
            gp.style.left = Math.round(Math.max(8, Math.min(r.left, window.innerWidth - 328))) + 'px';
            renderGcPopover();
        }
    }

    // The shell's sidebar footer: "2 of 2 GCs connected" (glyph + text).
    function renderGcSummary() {
        const a = $('gc-summary');
        if (!a) return;
        const agents = agentsNow();
        const text = gcSummary(agents, Date.now());
        a.hidden = !text;
        a.replaceChildren();
        if (!text) return;
        const s = gcStrip(agents, Date.now());
        a.className = 'sb-gcs gc-strip-' + s.cls;
        a.title = s.title;
        const g = el('span', 'gc-chip-glyph', s.glyph);
        g.setAttribute('aria-hidden', 'true');
        a.append(g, el('span', 'sb-label', text));
    }

    function renderBanner() {
        const b = $('paused-banner');
        if (!b) return;
        const text = pausedBanner(last.hub);
        b.hidden = !text;
        b.textContent = text || '';
    }

    function onUpdate(u) {
        last = { tasks: u.tasks || [], hub: u.hub || null, boot: u.boot || last.boot };
        renderIndicator();
        renderStrip();
        renderGcSummary();
        renderBanner();
    }

    function mount() {
        if (!root.GCLive || !($('running-now') || $('gc-strip') || $('paused-banner')
                               || $('gc-summary'))) return;
        const toggle = (which) => (e) => { e.stopPropagation(); setOpen(openPop === which ? null : which); };
        if ($('running-now')) $('running-now').addEventListener('click', toggle('tasks'));
        if ($('gc-strip')) $('gc-strip').addEventListener('click', toggle('gcs'));
        // a click inside a popover (Dismiss re-renders it, so its target is gone
        // by the time the document sees the click) never closes it
        for (const id of ['running-now-popover', 'gc-strip-popover']) {
            const pop = $(id);
            if (pop) pop.addEventListener('click', (e) => e.stopPropagation());
        }
        document.addEventListener('click', () => { if (openPop) setOpen(null); });
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && openPop) setOpen(null); });
        root.GCLive.subscribe(onUpdate);
        // a shell page (live_adapter.js) may not start the poller itself
        if (root.GCLive.start) root.GCLive.start();
        // the ages tick between answers (and an agent's age alone is no update)
        setInterval(() => { renderStrip(); renderGcSummary(); if (openPop === 'tasks') renderTasksPopover(); },
                    STRIP_TICK_MS);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
    else mount();
})(typeof window !== 'undefined' ? window : globalThis);
