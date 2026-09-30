/* samples_router.js (v5.0.0 lane S): the address bar is the link.

   Pure and DOM-free (window.SamplesRouter in the browser, module.exports for
   tests/js/samples_router.test.js). The Samples page keeps the URL current
   with history.pushState; a reload or a pasted link restores the same view:

     /  and  /samples                         the list, nothing open
     /samples/<id>                            that run, Overview
     /samples/<id>/compare[?standard=<name>]  that run, Compare
     /samples/<id>/data                       that run, Data

   plus the list's state in the query, on every one of them:
     instrument=gc1,gc2  status=held,error  q=<text>  sort=lab  notsent=1
   (sort=newest and empty values are left out). The old links keep working:
   /?sample=<id>[&tab=analysis|data][&standard=] (legacy: the page rewrites
   the address to the new form with replaceState). /lab/<lab_id> is resolved
   by the server, which redirects to /samples/<id>. */
(function (root) {
    'use strict';

    const VIEWS = ['overview', 'compare', 'data'];
    const SORTS = ['newest', 'lab'];
    // the status chips and the store statuses each one stands for
    const STATUS_GROUPS = {
        final: ['final'],
        held: ['awaiting_calibration', 'pending_corrections', 'other_method', 'review_method', 'raw_only'],
        error: ['error'],
        processing: ['received'],
    };
    const GROUP_ORDER = ['final', 'held', 'error', 'processing'];

    function _decode(s) {
        try { return decodeURIComponent(String(s).replace(/\+/g, ' ')); } catch (_) { return String(s); }
    }

    /** {key: [values in order]} of a query string (each value decoded once). */
    function _params(search) {
        const out = {};
        const qs = String(search || '').replace(/^\?/, '');
        if (!qs) return out;
        for (const part of qs.split('&')) {
            if (!part) continue;
            const eq = part.indexOf('=');
            const k = _decode(eq < 0 ? part : part.slice(0, eq));
            const v = eq < 0 ? '' : _decode(part.slice(eq + 1));
            (out[k] = out[k] || []).push(v);
        }
        return out;
    }

    function _first(params, key) {
        const v = params[key];
        if (!v || !v.length) return null;
        const s = v[0].trim();
        return s === '' ? null : s;
    }

    function _list(params, key) {
        const out = [];
        for (const v of params[key] || []) {
            for (const item of String(v).split(',')) {
                const s = item.trim();
                if (s && !out.includes(s)) out.push(s);
            }
        }
        return out;
    }

    function _id(text) {
        if (!/^[1-9][0-9]*$/.test(String(text || ''))) return null;
        const n = Number(text);
        return Number.isSafeInteger(n) ? n : null;
    }

    function _filters(params) {
        const status = _list(params, 'status').filter(s => GROUP_ORDER.includes(s));
        const sort = _first(params, 'sort');
        const q = _first(params, 'q');
        return {
            instrument: _list(params, 'instrument').sort(),
            status,
            q: q || '',
            sort: SORTS.includes(sort) ? sort : 'newest',
            notsent: _first(params, 'notsent') === '1',
        };
    }

    /** The view a URL asks for: {sampleId, view, standard, filters, legacy}. */
    function parse(pathname, search) {
        const path = String(pathname || '').replace(/\/+$/, '');
        const params = _params(search);
        const filters = _filters(params);
        let sampleId = null;
        let view = 'overview';
        let legacy = false;
        const m = /^\/samples\/([^/]+)(?:\/([^/]+))?$/.exec(path);
        if (m) {
            sampleId = _id(m[1]);
            if (sampleId !== null && VIEWS.includes(m[2])) view = m[2];
        } else if ((path === '' || path === '/samples') && _first(params, 'sample') !== null) {
            sampleId = _id(_first(params, 'sample'));
            legacy = sampleId !== null;
            const tab = _first(params, 'tab') || _first(params, 'view');
            if (tab === 'analysis' || tab === 'compare') view = 'compare';
            else if (tab === 'data' || tab === 'distilldata') view = 'data';
        }
        const standard = view === 'compare' ? _first(params, 'standard') : null;
        return { sampleId, view, standard, filters, legacy };
    }

    function _enc(v) {
        return encodeURIComponent(v);
    }

    /** The URL (path + query) of a view. */
    function build(state) {
        const s = state || {};
        const f = s.filters || {};
        let path = '/samples';
        if (s.sampleId != null) {
            path += '/' + s.sampleId;
            if (s.view === 'compare' || s.view === 'data') path += '/' + s.view;
        }
        const parts = [];
        if (s.sampleId != null && s.view === 'compare' && s.standard) parts.push('standard=' + _enc(s.standard));
        const inst = (f.instrument || []).slice().sort();
        if (inst.length) parts.push('instrument=' + _enc(inst.join(',')));
        const status = GROUP_ORDER.filter(g => (f.status || []).includes(g));
        if (status.length) parts.push('status=' + _enc(status.join(',')));
        const q = String(f.q || '').trim();
        if (q) parts.push('q=' + _enc(q));
        if (f.sort === 'lab') parts.push('sort=lab');
        if (f.notsent) parts.push('notsent=1');
        return path + (parts.length ? '?' + parts.join('&') : '');
    }

    function sameUrl(a, b) {
        return build(a) === build(b);
    }

    /** The store statuses of the chosen status chips. */
    function statusList(groups) {
        const out = [];
        for (const g of GROUP_ORDER) {
            if ((groups || []).includes(g)) out.push(...STATUS_GROUPS[g]);
        }
        return out;
    }

    /** The chip a store status belongs to (anything unknown counts as held). */
    function statusGroup(status) {
        for (const g of GROUP_ORDER) {
            if (STATUS_GROUPS[g].includes(status)) return g;
        }
        return 'held';
    }

    /** The /api/files (and /api/files/ids) query for the list's filters;
        the order is the page's own (the server's is always newest first). */
    function filesQuery(filters) {
        const f = filters || {};
        const parts = [];
        const inst = (f.instrument || []).slice().sort();
        if (inst.length) parts.push('instrument=' + _enc(inst.join(',')));
        const st = statusList(f.status || []);
        if (st.length) parts.push('status=' + _enc(st.join(',')));
        const q = String(f.q || '').trim();
        if (q) parts.push('q=' + _enc(q));
        if (f.notsent) parts.push('notsent=1');
        return parts.join('&');
    }

    /** True when the list is narrowed (the order alone is not a filter). */
    function filtersActive(filters) {
        return filesQuery(filters) !== '';
    }

    const api = { VIEWS, SORTS, STATUS_GROUPS, GROUP_ORDER, parse, build, sameUrl, statusList,
                  statusGroup, filesQuery, filtersActive };
    root.SamplesRouter = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
