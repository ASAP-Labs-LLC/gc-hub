/* running_now.js: what is happening right now, on the classic main page
   (v4.0 lane E; spec "Global status, a way home, and Hub admin").

   From GCLive's /api/live answers (tasks, agents, hub) it keeps three things
   current, without a reload:
     #running-now          the "running now" indicator at bottom-left (it
                           replaced "Ready"): one line, most urgent first;
                           click for the list of tasks with Open / Download /
                           Dismiss. Finished and failed tasks stay for 30
                           minutes (the server drops them), or until dismissed
                           in this browser (localStorage, keyed
                           "<boot id>:<task id>": task ids restart with the hub).
     #gc-strip             one chip per GC: glyph + text ("GC-1 ● Live"),
                           never colour alone; `live` is the server's (last
                           check-in at most 90 s ago on the hub's clock).
     #paused-banner        one line while processing is paused.
   The pure helpers are module.exports for tests/js/running_now.test.js; the
   DOM part mounts itself when the page has those elements and GCLive. Every
   string is set with textContent. The shell's sidebar footer (lane E2) can use
   summary(), gcChip() and gcSummary() as they are. */
(function (root) {
    'use strict';

    const DISMISS_KEY = 'gc-running-dismissed';
    const DISMISS_MAX = 200;
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

    /** An ended task's outcome in words. */
    function outcomeText(task) {
        const p = (task && task.progress) || {};
        if (task.state === 'done') return 'Finished' + (p.text ? ' · ' + p.text : '');
        if (task.state === 'stopped') {
            const done = _num(p.done);
            const total = _num(p.total);
            if (done === null) return 'Stopped';
            return 'Stopped after ' + fmtNum(done) + (total !== null ? ' of ' + fmtNum(total) : '');
        }
        if (task.state === 'interrupted') return 'Interrupted: it stopped without finishing';
        return task.open_url ? 'Failed · see its page for why' : 'Failed';
    }

    function taskLine(task) {
        return task.state === 'running' ? progressText(task) : outcomeText(task);
    }

    /** The collapsed indicator ({text, glyph, state, count}), or null when
        there is nothing to show. */
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
        const t = list[0];                     // the server sends the most recent first
        return { text: t.title + ' ' + (VERBS[t.state] || 'ended') + ' · View',
                 glyph: GLYPHS[t.state] || 'ring', state: t.state, count: list.length };
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

    /** One GC's chip: {id, name, glyph, text, cls, href, title}. ``href``
        follows ``pattern`` ("{id}" is replaced; default the Instruments page
        with that GC selected). */
    function gcChip(agent, pattern) {
        const id = String(agent.instrument_id);
        const name = agent.name || id;
        const href = (pattern || '/instruments?instrument={id}').replace('{id}', encodeURIComponent(id));
        const age = _num(agent.last_seen_age_s);
        const bits = [name];
        if (agent.host) bits.push(agent.host);
        if (agent.version) bits.push('agent ' + agent.version);
        let glyph = '○';
        let text;
        let cls;
        if (agent.enabled === false) {
            glyph = '–'; text = 'Disabled'; cls = 'off';
        } else if (agent.live === true) {
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

    /** "2 of 2 GCs connected" (enabled GCs only); '' without any. */
    function gcSummary(agents) {
        const on = (agents || []).filter(a => a.enabled !== false);
        if (!on.length) return '';
        const live = on.filter(a => a.live === true).length;
        return live + ' of ' + on.length + (on.length === 1 ? ' GC' : ' GCs') + ' connected';
    }

    function _hm(iso) {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return null;
        return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    }

    /** "Ryan C · 14:02" (who started it, local time). */
    function whenText(iso, by) {
        const hm = iso ? _hm(iso) : null;
        return [by, hm].filter(Boolean).join(' · ');
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

    const pure = { DISMISS_KEY, DISMISS_MAX, fmtNum, progressText, outcomeText, taskLine, summary,
                   dismissKey, visibleTasks, loadDismissed, saveDismissed, gcChip, gcSummary,
                   whenText, pausedBanner };
    root.GCRunningNow = pure;
    if (typeof module !== 'undefined' && module.exports) module.exports = pure;
    if (typeof document === 'undefined') return;

    // ── the page ──────────────────────────────────────────────────────────
    const $ = (id) => document.getElementById(id);
    let last = { tasks: [], agents: [], hub: null, boot: null };
    let dismissed = loadDismissed();
    let open = false;

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
        if (!s) { setOpen(false); return; }
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
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (open) renderPopover();
    }

    function action(label, cls, attrs) {
        const a = el(attrs.href ? 'a' : 'button', 'rn-action ' + cls, label);
        for (const [k, v] of Object.entries(attrs)) {
            if (k === 'onclick') a.addEventListener('click', v);
            else a.setAttribute(k, v);
        }
        return a;
    }

    function renderPopover() {
        const pop = $('running-now-popover');
        if (!pop) return;
        const list = el('ul', 'rn-list');
        for (const t of shown()) {
            const li = el('li', 'rn-task rn-' + t.state);
            li.dataset.taskId = t.id;
            const head = el('div', 'rn-task-title');
            head.append(glyph(GLYPHS[t.state] || 'ring'), el('span', null, t.title));
            li.append(head, el('div', 'rn-task-meta', whenText(t.started_at, t.by)),
                      el('div', 'rn-task-line', taskLine(t)));
            const acts = el('div', 'rn-actions');
            if (t.open_url) acts.appendChild(action('Open', 'rn-open', { href: t.open_url }));
            if (t.download_url) {
                acts.appendChild(action('Download', 'rn-download', { href: t.download_url, download: '' }));
            }
            if (t.state !== 'running') {
                acts.appendChild(action('Dismiss', 'rn-dismiss', { type: 'button', onclick: () => {
                    dismissed.add(dismissKey(last.boot, t));
                    saveDismissed(dismissed);
                    renderIndicator();
                } }));
            }
            if (acts.childNodes.length) li.appendChild(acts);
            list.appendChild(li);
        }
        pop.replaceChildren(el('div', 'rn-pop-title', 'Running now'), list);
    }

    function setOpen(v) {
        open = !!v;
        const pop = $('running-now-popover');
        const btn = $('running-now');
        if (pop) pop.hidden = !open;
        if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (open) renderPopover();
    }

    function renderStrip() {
        const strip = $('gc-strip');
        if (!strip) return;
        const chips = (last.agents || []).map(a => {
            const c = gcChip(a);
            const link = el('a', 'gc-chip gc-chip-' + c.cls);
            link.href = c.href;
            link.title = c.title;
            link.dataset.instrument = c.id;
            link.append(el('span', 'gc-chip-name', c.name), glyph0(c.glyph),
                        el('span', 'gc-chip-text', c.text));
            return link;
        });
        strip.replaceChildren(...chips);
        strip.hidden = chips.length === 0;
    }

    function glyph0(ch) {
        const g = el('span', 'gc-chip-glyph', ch);
        g.setAttribute('aria-hidden', 'true');
        return g;
    }

    function renderBanner() {
        const b = $('paused-banner');
        if (!b) return;
        const text = pausedBanner(last.hub);
        b.hidden = !text;
        b.textContent = text || '';
    }

    function onUpdate(u) {
        last = { tasks: u.tasks || [], agents: u.agents || [], hub: u.hub || null,
                 boot: u.boot || last.boot };
        renderIndicator();
        renderStrip();
        renderBanner();
    }

    function mount() {
        if (!root.GCLive || !($('running-now') || $('gc-strip') || $('paused-banner'))) return;
        const btn = $('running-now');
        if (btn) {
            btn.addEventListener('click', (e) => { e.stopPropagation(); setOpen(!open); });
            document.addEventListener('click', (e) => {
                const pop = $('running-now-popover');
                if (open && pop && !pop.contains(e.target)) setOpen(false);
            });
            document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && open) setOpen(false); });
        }
        root.GCLive.subscribe(onUpdate);
        setInterval(() => { if (open) renderPopover(); }, 15000);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
    else mount();
})(typeof window !== 'undefined' ? window : globalThis);
