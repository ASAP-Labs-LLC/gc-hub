// The Backfill section's pure logic (v4.0.1): selecting many rows at once
// (select all, shift-click ranges, drag), pruning the selection when rows go
// away, chunking releases to the route's limit, and the muted line that says
// why each row is backfill. No DOM; node-tested (tests/js/backfill.test.js).
// Window global GCBackfill, module.exports for node.
(function (root) {
    'use strict';

    // POST /api/admin/instruments/<id>/backfill/release takes at most this many
    // ids per call (instrument_admin.RELEASE_MAX).
    const RELEASE_MAX = 500;
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    // The ids from a to b inclusive, in list order. An anchor no longer in the
    // list gives just the target.
    function rangeIds(order, a, b) {
        const j = order.indexOf(b);
        if (j === -1) return [];
        const i = order.indexOf(a);
        if (i === -1) return [b];
        return order.slice(Math.min(i, j), Math.max(i, j) + 1);
    }

    // `base` with every selectable row from `from` to `to` set to `state`.
    // A drag always starts again from the selection it began with, so
    // dragging back over rows gives them their old state. Never mutates base.
    function setRange(base, order, from, to, state, selectable) {
        const out = new Set(base);
        for (const id of rangeIds(order, from, to)) {
            if (!selectable.has(id)) continue;
            if (state) out.add(id); else out.delete(id);
        }
        return out;
    }

    // The header checkbox: 'none' | 'some' (indeterminate) | 'all' of the
    // selectable rows shown.
    function headerState(selected, selectable) {
        let n = 0;
        for (const id of selectable) if (selected.has(id)) n++;
        if (!n) return 'none';
        return n === selectable.size ? 'all' : 'some';
    }

    function toggleAll(selected, selectable) {
        return headerState(selected, selectable) === 'all' ? new Set() : new Set(selectable);
    }

    // Keep only rows still shown and selectable (released, changed or
    // filtered-out rows drop out).
    function prune(selected, selectable) {
        return new Set(Array.from(selected).filter(id => selectable.has(id)));
    }

    function chunk(list, size) {
        const out = [];
        for (let i = 0; i < list.length; i += size) out.push(list.slice(i, i + size));
        return out;
    }

    const num = (n) => Number(n).toLocaleString('en-US');

    function barText(n) {
        return n ? { count: num(n) + ' selected', action: 'Release ' + num(n) }
                 : { count: 'None selected', action: 'Release' };
    }

    function progressText(done, total) {
        return 'Releasing ' + num(done) + ' of ' + num(total) + '…';
    }

    // ── why a row is backfill ───────────────────────────────────────────────
    // GC-clock times are naive local 'YYYY-MM-DD HH:MM[:SS[.ffffff]]' (or a T
    // separator), compared as store.local_dt does: normalised, then in order.
    const DT_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?)?$/;
    function parts(value) {
        const m = DT_RE.exec(String(value === null || value === undefined ? '' : value).trim());
        if (!m) return null;
        return { y: +m[1], mo: +m[2], d: +m[3], hh: m[4] || '00', mm: m[5] || '00',
                 key: m[1] + '-' + m[2] + '-' + m[3] + ' ' + (m[4] || '00') + ':' + (m[5] || '00') + ':' +
                      (m[6] || '00') + (m[7] || '') };
    }
    // "14:02" on the reference's day, "Sep 28 09:15" in its year, else with the year
    function short(p, ref) {
        const time = p.hh + ':' + p.mm;
        if (ref && ref.y === p.y && ref.mo === p.mo && ref.d === p.d) return time;
        const day = MONTHS[p.mo - 1] + ' ' + p.d;
        return (ref && ref.y !== p.y ? day + ', ' + p.y : day) + ' ' + time;
    }
    function isBefore(a, b) {      // store.is_backfill's injection_dt < live_since
        return a.key < b.key;
    }

    // sample: a /backfill row (injection_dt, received_at); info: the
    // instrument's name, live_since and live_since_set_at (the hub's UTC time
    // it was last set). Backfill is decided when a run arrives, so a run not
    // injected before live_since arrived before it was set, or was loaded as
    // backfill (a folder load or the history import) after that.
    function whyText(sample, info) {
        const name = (info && info.name) || 'the GC';
        const inj = parts(sample && sample.injection_dt);
        const live = info && info.live_since ? parts(info.live_since) : null;
        let reason;
        if (!live) reason = 'arrived before ' + name + ' had a live-since time';
        else if (inj && isBefore(inj, live)) reason = 'before ' + name + ' went live (' + short(live, { y: inj.y }) + ')';
        else {
            const got = Date.parse(sample && sample.received_at);
            const set = Date.parse(info.live_since_set_at);
            reason = isFinite(got) && isFinite(set) && got >= set ? 'loaded as backfill'
                : 'arrived before ' + name + ' went live';
        }
        return inj ? 'injected ' + short(inj, live || { y: inj.y }) + ' · ' + reason : reason;
    }

    const api = { RELEASE_MAX, rangeIds, setRange, headerState, toggleAll, prune, chunk, barText,
                  progressText, whyText };
    root.GCBackfill = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
