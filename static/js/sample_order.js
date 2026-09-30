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

    const COLLATOR = (typeof Intl !== 'undefined' && Intl.Collator)
        ? new Intl.Collator(undefined, { sensitivity: 'base' }) : null;

    function _textCompare(x, y) {
        if (COLLATOR) return COLLATOR.compare(x, y);
        const lx = x.toLowerCase();
        const ly = y.toLowerCase();
        return lx === ly ? 0 : (lx < ly ? -1 : 1);
    }

    /** Natural order: digit runs compare as numbers (leading zeros ignored,
        then the shorter run first), other runs by their base letters (case
        and accents aside, localeCompare's sensitivity 'base'); numbers before
        text; a prefix before what extends it; an empty lab ID last. Ties:
        plain text order. */
    function naturalCompare(a, b) {
        const ea = a == null || String(a) === '';
        const eb = b == null || String(b) === '';
        if (ea || eb) return ea === eb ? (a == null) - (b == null) : (ea ? 1 : -1);
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
                const c = _textCompare(x, y);
                if (c !== 0) return c < 0 ? -1 : 1;
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

    /** The row's time: {text, title}. Under a day heading ({underDay}) the
        date is already said, so the row shows the time only and the tooltip
        the full date; otherwise the date and time. */
    function rowTime(file, opts) {
        const p = parseInjection(file && file.injection_dt);
        if (!p) return { text: '', title: 'No injection time' };
        const full = String(file.injection_dt).replace('T', ' ');
        if (!hasInjectionTime(file)) {
            return { text: 'file ' + p.day + ' ' + p.hm,
                     title: 'No injection time in the CDF; this is the file’s time, ' + full };
        }
        return { text: opts && opts.underDay ? p.hm : p.day + ' ' + p.hm, title: 'Injected ' + full };
    }

    function _dayKey(d) {
        const pad = (n) => String(n).padStart(2, '0');
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    }

    /** "Today", "Yesterday", "Thu 24 Sep", or "Wed 31 Dec 2025" in another
        year. ``today`` is the hub's date ("YYYY-MM-DD", /api/live's
        server_today: injection times are the hub's local clock), or a Date
        (the browser's calendar, for an older hub). Date arithmetic in UTC, so
        a DST change never skips a day. */
    function dayLabel(day, today) {
        const t = typeof today === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(today) ? today
            : _dayKey(today instanceof Date ? today : new Date());
        if (day === t) return 'Today';
        const [ty, tm, td] = t.split('-').map(Number);
        const y = new Date(Date.UTC(ty, tm - 1, td - 1));
        if (day === y.toISOString().slice(0, 10)) return 'Yesterday';
        const [yy, mm, dd] = day.split('-').map(Number);
        const d = new Date(Date.UTC(yy, mm - 1, dd));
        const base = DAYS[d.getUTCDay()] + ' ' + dd + ' ' + MONTHS[mm - 1];
        return yy === ty ? base : base + ' ' + yy;
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
