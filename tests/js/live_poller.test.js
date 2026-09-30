// static/js/live.js: the poller (GCLive.createPoller), driven with fake timers,
// a fake fetch and a fake page visibility.
const L = require('../../static/js/live.js');

function answer(over) {
    return Object.assign({ cursor: 'b:1', reset: false, samples: [], instruments: [], kinds: [],
        agents: [], notifications_unread: 0, hub: { state: 'running', staged_update: null },
        version: 'v1' }, over || {});
}

/** A fake world: timers you advance, a fetch you answer by hand. */
function world() {
    const w = { now: 0, timers: [], calls: [], pending: [], visible: true, handlers: {},
                inFlight: 0, maxInFlight: 0, fail: false, next: [] };
    w.deps = {
        now: () => w.now,
        setTimeout: (fn, ms) => { const tm = { fn, at: w.now + ms, id: Symbol('t') }; w.timers.push(tm); return tm.id; },
        clearTimeout: (id) => { w.timers = w.timers.filter(tm => tm.id !== id); },
        isVisible: () => w.visible,
        on: (ev, fn) => { (w.handlers[ev] = w.handlers[ev] || []).push(fn); },
        off: (ev, fn) => { w.handlers[ev] = (w.handlers[ev] || []).filter(f => f !== fn); },
        fetch: (url, opts) => {
            w.calls.push({ url, opts });
            w.inFlight++;
            w.maxInFlight = Math.max(w.maxInFlight, w.inFlight);
            return new Promise((resolve, reject) => w.pending.push({ resolve, reject }));
        },
    };
    // answer the oldest open request
    w.reply = async (body) => {
        const p = w.pending.shift();
        w.inFlight--;
        if (body instanceof Error) p.reject(body);
        else p.resolve({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) });
        await flush();
    };
    w.replyStatus = async (status) => {
        const p = w.pending.shift();
        w.inFlight--;
        p.resolve({ ok: false, status, json: async () => ({}), text: async () => '{}' });
        await flush();
    };
    // the next timer's delay from now, and run it
    w.nextDelay = () => {
        const tm = w.timers.slice().sort((a, b) => a.at - b.at)[0];
        return tm ? tm.at - w.now : null;
    };
    w.fire = async () => {
        const tm = w.timers.slice().sort((a, b) => a.at - b.at)[0];
        w.timers = w.timers.filter(x => x !== tm);
        w.now = tm.at;
        tm.fn();
        await flush();
    };
    w.emit = async (ev) => { for (const fn of w.handlers[ev] || []) fn(); await flush(); };
    return w;
}

async function flush() {
    for (let i = 0; i < 10; i++) await new Promise(r => setImmediate(r));
}

module.exports = async (t) => {
    // ── start: one request, resolves after the first answer, idempotent ──
    {
        const w = world();
        const p = L.createPoller(w.deps);
        const got = [];
        p.subscribe(u => got.push(u));
        let started = false;
        const s1 = p.start().then(() => { started = true; });
        const s2 = p.start();
        await flush();
        t.eq(w.calls.length, 1);
        t.eq(w.calls[0].url, '/api/live');
        t.eq(w.calls[0].opts.headers['X-GC-Background'], '1');
        t.eq(started, false);
        await w.reply(answer({ reset: true }));
        await s1; await s2;
        t.eq(started, true);
        t.eq(got.length, 1);
        t.eq(got[0].reset, true);
        t.eq(p.status().connected, true);
        // the next poll carries the cursor
        t.eq(w.nextDelay(), 3000);
        await w.fire();
        t.eq(w.calls[1].url, '/api/live?since=b%3A1');
        p.stop();
    }

    // ── start resolves after a failed first answer too (the page loads anyway) ──
    {
        const w = world();
        const p = L.createPoller(w.deps);
        let started = false;
        p.start().then(() => { started = true; });
        await flush();
        await w.replyStatus(503);
        t.eq(started, true);
        t.eq(p.status().connected, false);
        t.eq(p.status().error, 'HTTP 503');
        p.stop();
    }

    // ── cadence: 3 s visible, 30 s hidden, at once on becoming visible / focus ──
    {
        const w = world();
        const p = L.createPoller(w.deps);
        p.start();
        await flush();
        await w.reply(answer());
        t.eq(w.nextDelay(), 3000);
        await w.fire();
        w.visible = false;
        await w.reply(answer());
        t.eq(w.nextDelay(), 30000);
        // becoming visible polls at once (and reschedules)
        w.visible = true;
        const before = w.calls.length;
        await w.emit('visibilitychange');
        t.eq(w.calls.length, before + 1);
        await w.reply(answer());
        t.eq(w.nextDelay(), 3000);
        // a hidden tab's visibilitychange does not poll
        w.visible = false;
        await w.emit('visibilitychange');
        t.eq(w.calls.length, before + 1);
        // focus polls at once
        w.visible = true;
        await w.emit('focus');
        t.eq(w.calls.length, before + 2);
        p.stop();
    }

    // ── backoff with a cap; recovery resets it ──
    {
        const w = world();
        const p = L.createPoller(w.deps);
        p.start();
        await flush();
        const delays = [];
        for (let i = 0; i < 5; i++) {
            await w.reply(new Error('network down'));
            delays.push(w.nextDelay());
            await w.fire();
        }
        t.eq(delays, [6000, 12000, 12000, 12000, 12000]);
        t.eq(p.status().connected, false);
        t.eq(p.status().error, 'network down');
        await w.reply(answer());
        t.eq(w.nextDelay(), 3000);
        t.eq(p.status().connected, true);
        // hidden: 30 s, backing off to 60 s
        w.visible = false;
        await w.fire();
        await w.reply(new Error('x'));
        t.eq(w.nextDelay(), 60000);
        await w.fire();
        await w.reply(new Error('x'));
        t.eq(w.nextDelay(), 60000);
        p.stop();
    }

    // ── overlapping triggers: never two requests at once, one follow-up ──
    {
        const w = world();
        const p = L.createPoller(w.deps);
        p.start();
        await flush();
        p.pollNow(); p.pollNow();
        await w.emit('focus');
        t.eq(w.calls.length, 1);
        await w.reply(answer());
        t.eq(w.calls.length, 2);               // the queued follow-up, at once
        t.eq(w.nextDelay(), null);             // no timer while it is in flight
        await w.reply(answer());
        t.eq(w.calls.length, 2);
        t.eq(w.maxInFlight, 1);
        t.eq(w.nextDelay(), 3000);
        p.stop();
        // stopped: nothing more
        await w.fire().catch(() => {});
        t.eq(w.calls.length, 2);
    }

    // ── a late subscriber gets its own reset with the snapshot; unsubscribe ──
    {
        const w = world();
        const p = L.createPoller(w.deps);
        const early = [];
        p.subscribe(u => early.push(u));
        p.start();
        await flush();
        await w.reply(answer({ reset: true, agents: [{ instrument_id: 'gc1', last_seen: 'x',
                                                      version: '1', host: 'h', status: 'idle' }],
                               notifications_unread: 4 }));
        const late = [];
        const off = p.subscribe(u => late.push(u));
        await flush();
        t.eq(late.length, 1);
        t.eq(late[0].reset, true);
        t.eq(late[0].agents[0].instrument_id, 'gc1');
        t.eq(late[0].notifications_unread, 4);
        t.eq(early.length, 1);                  // the early one got no second reset
        t.eq(p.agents()[0].instrument_id, 'gc1');
        off();
        await w.fire();
        await w.reply(answer({ cursor: 'b:2', samples: [7] }));
        t.eq(late.length, 1);
        t.eq(early.length, 2);
        t.eq(early[1].samples, [7]);
        // a subscriber that throws doesn't stop the others
        p.subscribe(() => { throw new Error('boom'); });
        const after = [];
        p.subscribe(u => after.push(u));
        await flush();
        await w.fire();
        await w.reply(answer({ cursor: 'b:3', samples: [8] }));
        t.eq(after.some(u => (u.samples || []).includes(8)), true);
        p.stop();
    }

    // ── bgFetch marks a request as background ──
    {
        const seen = [];
        const f = (url, opts) => { seen.push({ url, opts }); return Promise.resolve('ok'); };
        await L.bgFetch('/api/files?ids=1', { headers: { Accept: 'application/json' } }, f);
        t.eq(seen[0].url, '/api/files?ids=1');
        t.eq(seen[0].opts.headers['X-GC-Background'], '1');
        t.eq(seen[0].opts.headers.Accept, 'application/json');
        t.eq(seen[0].opts.credentials, 'same-origin');
        // never on a write
        let threw = false;
        try { await L.bgFetch('/api/x', { method: 'POST' }, f); } catch (_) { threw = true; }
        t.eq(threw, true);
    }
};
