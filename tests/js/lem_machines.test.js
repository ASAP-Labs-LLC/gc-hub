// D10: the LEM machine dropdown's options (static/js/instruments_logic.js).
const L = require('../../static/js/instruments_logic.js');

module.exports = (t) => {
    const live = {
        source: 'live', age_seconds: 3,
        machines: [
            { uid: 'bf8e64b59f12', title: 'Agilent GC 1', status: 'RED', closed: false },
            { uid: '3afa991a66e9', title: 'Agilent GC 2', status: 'UNKNOWN', closed: false },
            { uid: 'old1', title: 'Old GC', status: 'CLOSED', closed: true },
            { uid: 'notitle', title: '', status: '', closed: false },
        ],
    };
    const texts = (r) => r.options.map(o => o.text);

    // ── the list: none, one entry per machine, closed marked, Other…
    let r = L.lemMachineOptions(live, '');
    t.eq(texts(r), ['— none —', 'Agilent GC 1 (bf8e64b59f12)', 'Agilent GC 2 (3afa991a66e9)',
        'Old GC (old1) (closed)', 'notitle', 'Other…']);
    t.eq(r.options.map(o => o.value), ['', 'bf8e64b59f12', '3afa991a66e9', 'old1', 'notitle', '']);
    t.eq(r.options.map(o => !!o.other), [false, false, false, false, false, true]);
    t.eq(r.selected, 0);
    t.eq(r.note, null);
    t.eq(L.lemMachineOptions(live, null).selected, 0);
    t.eq(L.lemMachineOptions(live, undefined).selected, 0);

    // ── the saved uid is preselected
    t.eq(L.lemMachineOptions(live, '3afa991a66e9').selected, 2);
    t.eq(L.lemMachineOptions(live, 'old1').selected, 3);

    // ── a saved uid LEM doesn't list is kept, as "Unknown machine (uid)"
    r = L.lemMachineOptions(live, 'M-3');
    t.eq(texts(r)[1], 'Unknown machine (M-3)');
    t.eq(r.options[1].value, 'M-3');
    t.eq(r.selected, 1);
    t.eq(r.options.length, 7);
    t.eq(r.note, null);

    // ── LEM unavailable (or the request failed): the saved value plus Other…
    const down = { source: 'unavailable', machines: [], age_seconds: null, error: 'LEM could not be reached.' };
    const NOTE = "LEM can't be reached; showing the saved value.";
    for (const answer of [down, null, undefined, { machines: 'x' }, 'garbage']) {
        r = L.lemMachineOptions(answer, 'bf8e64b59f12');
        t.eq(texts(r), ['— none —', 'bf8e64b59f12', 'Other…']);
        t.eq(r.options[1].value, 'bf8e64b59f12');
        t.eq(r.selected, 1);
        t.eq(r.note, NOTE);
    }
    r = L.lemMachineOptions(down, '');
    t.eq(texts(r), ['— none —', 'Other…']);
    t.eq(r.selected, 0);
    t.eq(r.note, NOTE);

    // ── a stale list (LEM failed just now) is still offered, with a note
    r = L.lemMachineOptions(Object.assign({}, live, { source: 'cached', age_seconds: 300, error: 'x' }), 'bf8e64b59f12');
    t.eq(r.selected, 1);
    t.eq(r.note, "LEM can't be reached; showing its list from 5 min ago.");
    t.eq(L.lemMachineOptions(Object.assign({}, live, { source: 'cached', age_seconds: 20 }), '').note,
        "LEM can't be reached; showing its list from 1 min ago.");

    // ── untrusted entries: only string uids, text never carries markup meaning
    r = L.lemMachineOptions({ source: 'live', machines: [null, { uid: 5, title: 'x' }, { title: 'no uid' },
        { uid: '<b>x</b>', title: '<img src=x>' }] }, '');
    t.eq(texts(r), ['— none —', '<img src=x> (<b>x</b>)', 'Other…']);

    // ── the value saved: the chosen uid, or the typed text for Other…
    t.eq(L.lemPickerValue({ value: 'bf8e64b59f12' }, 'ignored'), 'bf8e64b59f12');
    t.eq(L.lemPickerValue({ value: '' }, 'ignored'), '');
    t.eq(L.lemPickerValue({ value: '', other: true }, '  typed-uid '), 'typed-uid');
    t.eq(L.lemPickerValue({ value: '', other: true }, null), '');
    t.eq(L.lemPickerValue(null, 'x'), '');
};
