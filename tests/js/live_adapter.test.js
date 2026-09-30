// static/js/live_adapter.js: the 5 s fallback poll used when live.js (GCLive)
// is absent. It emits updates in GCLive's shape.
global.GCUi = require('../../static/js/ui_logic.js');
const A = require('../../static/js/live_adapter.js');

module.exports = async (t) => {
    t.eq(A.VISIBLE_MS, 5000);
    let answers = {
        '/api/instruments': { instruments: [{ id: 'gc1', counts: { final: 1 }, agent: null }] },
        '/api/agents': { agents: [{ instrument_id: 'gc1', last_seen: null, version: null, host: null, state: null }] },
        '/api/notifications': [{ id: 'n1' }],
    };
    const asked = [];
    const fb = A.createFallback({
        fetchJSON: async (p) => { asked.push(p); if (answers === null) throw new Error('down'); return answers[p]; },
        setTimeout: () => 0, clearTimeout: () => {}, now: () => 42,
    });
    const got = [];
    const unsub = fb.subscribe(u => got.push(u));
    t.eq(fb.status(), { connected: false, last_ok_at: null, error: null });

    await fb._poll();
    t.eq(got.length, 1);
    t.eq(got[0], { reset: true, samples: [], instruments: ['gc1'],
                   agents: [{ instrument_id: 'gc1', last_seen: null, version: null, host: null, status: null }],
                   notifications_unread: 1, hub: null });
    t.eq(fb.status(), { connected: true, last_ok_at: 42, error: null });
    t.eq(asked.slice().sort(), ['/api/agents', '/api/instruments', '/api/notifications']);

    await fb._poll();                                  // nothing changed
    t.eq(got[1].reset, false);
    t.eq(got[1].instruments, []);

    answers['/api/agents'] = { agents: [{ instrument_id: 'gc1', last_seen: 'now', version: '2', host: 'h', state: 'running' }] };
    await fb._poll();
    t.eq(got[2].instruments, []);                      // a heartbeat is an agent update
    t.eq(got[2].agents[0].last_seen, 'now');
    t.eq(fb.agents()[0].status, 'running');

    answers['/api/instruments'] = { instruments: [{ id: 'gc1', counts: { final: 2 }, agent: null }] };
    await fb._poll();
    t.eq(got[3].instruments, ['gc1']);

    const saved = answers;
    answers = null;
    await fb._poll();
    t.eq(got.length, 4);                               // an error emits nothing
    t.eq(fb.status().connected, false);
    t.eq(fb.status().error, 'down');
    t.eq(fb.status().last_ok_at, 42);
    answers = saved;

    unsub();
    await fb._poll();
    t.eq(got.length, 4);
};
