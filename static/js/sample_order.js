/* sample_order.js: the sample list's order and day grouping (v4.0 lane E;
   spec "Global status, a way home, and Hub admin", Sample list order).

   Pure and DOM-free: window.SampleOrder in the browser, module.exports for
   the node tests (tests/js/sample_order.test.js).

   Two orders, chosen with the list's sort switch and remembered per browser
   (localStorage, every access in try/catch):
     newest  injection time, newest first, under day headings (Today,
             Yesterday, "Thu 24 Sep"); runs with no readable injection time
             (the CDF had none, so the hub used the file's time) go last under
             "No injection time".
     lab     lab ID in natural numeric order (40318 < 40318-RERUN-2 <
             40318-RERUN-10 < 40319); the runs of one lab ID oldest first; no
             headings.
   An injection time is the store's naive local "YYYY-MM-DD HH:MM:SS[.ffffff]",
   compared as text; day labels use the browser's calendar. */
(function (root) {
    'use strict';

    const SORT_KEY = 'gc-sample-sort';
    const MODES = ['newest', 'lab'];
    const TOKEN_RE = /(\d+)|(\D+)/g;
    const INJ_RE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(:\d{2}(?:\.\d+)?)?$/;
    const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct',
                    'Nov', 'Dec'];

    function _tokens(s) {
        return String(s == null ? '' : s).match(TOKEN_RE) || [];
    }

    /** Natural order: digit runs compare as numbers (leading zeros ignored,
        then the shorter run first), other runs case-insensitively; numbers
        before text; a prefix before what extends it. Ties: plain text order. */
    function naturalCompare(a, b) {
        const ta = _tokens(a);
        const tb = _tokens(b);
        const n = Math.min(ta.length, tb.length);
        for (let i = 0; i < n; i++) {
            const x = ta[i];
            const y = tb[i];
            const dx = /^\d/.test(x);
            const dy = /^\d/.test(y);
            if (dx && dy) {
                const sx = x.replace(/^0+(?=\d)/, '');
                const sy = y.replace(/^0+(?=\d)/, '');
                if (sx.length !== sy.length) return sx.length - sy.length;
                if (sx !== sy) return sx < sy ? -1 : 1;
                if (x.length !== y.length) return x.length - y.length;
            } else if (dx !== dy) {
                return dx ? -1 : 1;
            } else {
                const lx = x.toLowerCase();
                const ly = y.toLowerCase();
                if (lx !== ly) return lx < ly ? -1 : 1;
            }
        }
        if (ta.length !== tb.length) return ta.length - tb.length;
        const ra = String(a == null ? '' : a);
        const rb = String(b == null ? '' : b);
        return ra === rb ? 0 : (ra < rb ? -1 : 1);
    }

    /** {day: 'YYYY-MM-DD', hm: 'HH:MM', key} of a stored injection time, or null. */
    function parseInjection(value) {
        if (typeof value !== 'string') return null;
        const m = INJ_RE.exec(value.trim());
        if (!m) return null;
        return { day: m[1], hm: m[2] + ':' + m[3], key: m[1] + ' ' + m[2] + ':' + m[3] + (m[4] || '') };
    }

    /** True when the run's injection time came from the CDF (not the file's time). */
    function hasInjectionTime(file) {
        return !!file && file.injection_dt_source !== 'mtime' && parseInjection(file.injection_dt) !== null;
    }

    /** The row's date and time: {text, title}. */
    function rowTime(file) {
        const p = parseInjection(file && file.injection_dt);
        if (!p) return { text: '', title: 'No injection time' };
        const full = String(file.injection_dt).replace('T', ' ');
        if (!hasInjectionTime(file)) {
            return { text: 'file ' + p.day + ' ' + p.hm,
                     title: 'No injection time in the CDF; this is the file’s time, ' + full };
        }
        return { text: p.day + ' ' + p.hm, title: 'Injected ' + full };
    }

    function _dayKey(d) {
        const pad = (n) => String(n).padStart(2, '0');
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    }

    /** "Today", "Yesterday", "Thu 24 Sep", or "Wed 31 Dec 2025" in another year. */
    function dayLabel(day, now) {
        const today = now instanceof Date ? now : new Date();
        if (day === _dayKey(today)) return 'Today';
        const y = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
        if (day === _dayKey(y)) return 'Yesterday';
        const [yy, mm, dd] = day.split('-').map(Number);
        const d = new Date(yy, mm - 1, dd);
        const base = DAYS[d.getDay()] + ' ' + dd + ' ' + MONTHS[mm - 1];
        return yy === today.getFullYear() ? base : base + ' ' + yy;
    }

    function _labId(f) {
        return f.lab_id != null ? f.lab_id : (f.name != null ? f.name : '');
    }

    function _injKey(f) {
        const p = parseInjection(f.injection_dt);
        return p ? p.key : '';
    }

    function _desc(a, b) {
        return a === b ? 0 : (a < b ? 1 : -1);
    }

    /** A sorted copy of *files* ('newest' or 'lab'; anything else is 'newest'). */
    function sortSamples(files, mode) {
        const list = (files || []).slice();
        if (mode === 'lab') {
            return list.sort((a, b) => naturalCompare(_labId(a), _labId(b))
                || -_desc(_injKey(a), _injKey(b)) || (a.sample_id - b.sample_id));
        }
        return list.sort((a, b) => {
            const ha = hasInjectionTime(a);
            const hb = hasInjectionTime(b);
            if (ha !== hb) return ha ? -1 : 1;
            return _desc(_injKey(a), _injKey(b)) || (b.sample_id - a.sample_id);
        });
    }

    /** [{key, label, items}]: day groups (then 'none') for 'newest'; one
        group without a label for 'lab'. */
    function groupSamples(files, mode, now) {
        const sorted = sortSamples(files, mode);
        if (!sorted.length) return [];
        if (mode === 'lab') return [{ key: 'all', label: null, items: sorted }];
        const groups = [];
        let current = null;
        for (const f of sorted) {
            const key = hasInjectionTime(f) ? parseInjection(f.injection_dt).day : 'none';
            if (!current || current.key !== key) {
                current = { key, label: key === 'none' ? 'No injection time' : dayLabel(key, now),
                            items: [] };
                groups.push(current);
            }
            current.items.push(f);
        }
        return groups;
    }

    function _storage(s) {
        if (s !== undefined) return s;
        try { return root.localStorage || null; } catch (_) { return null; }
    }

    /** The remembered order ('newest' when none, unreadable or unknown). */
    function loadSortMode(storage) {
        try {
            const st = _storage(storage);
            const v = st ? st.getItem(SORT_KEY) : null;
            return MODES.includes(v) ? v : 'newest';
        } catch (_) {
            return 'newest';
        }
    }

    function saveSortMode(mode, storage) {
        try {
            const st = _storage(storage);
            if (st && MODES.includes(mode)) st.setItem(SORT_KEY, mode);
        } catch (_) { /* private window, blocked storage: not remembered */ }
    }

    const api = { SORT_KEY, MODES, naturalCompare, parseInjection, hasInjectionTime, rowTime,
                  dayLabel, sortSamples, groupSamples, loadSortMode, saveSortMode };
    root.SampleOrder = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
