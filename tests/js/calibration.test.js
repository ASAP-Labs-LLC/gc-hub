// calibration.js (v4.0 lane E2): the peak-assignment page's pure parts, moved
// out of the template (a shell page has no inline script).
const C = require('../../static/js/calibration.js');

module.exports = (t) => {
    const compounds = [{ carbon: 5, bp: 36 }, { carbon: 6, bp: 69 }];
    // a saved assignment lands on the detected peak within 0.02 min
    const peaks = [{ rt: 1.00, intensity: 10 }, { rt: 2.00, intensity: 20 }, { rt: 3.0, intensity: 5 }];
    const prior = [{ rt: 1.01, choice: '5' }, { rt: 2.5, choice: '6' }, { rt: 3.0, choice: 'ignore' }];
    t.eq(C.matchChoices(peaks, prior).map(s => s.choice), ['5', '', 'ignore']);
    t.eq(C.priorFromAssignments([{ rt: 1, carbon: 5 }, { rt: 2, ignore: true }, { rt: 3 }]),
        [{ rt: 1, choice: '5' }, { rt: 2, choice: 'ignore' }, { rt: 3, choice: '' }]);
    t.eq(C.bpFor('5', compounds), 36);
    t.eq(C.bpFor('ignore', compounds), null);
    t.eq(C.bpFor('', compounds), null);
    t.eq(C.assignmentsFor([{ rt: 1, choice: '5' }, { rt: 2, choice: 'ignore' }, { rt: 3, choice: '' }]),
        [{ rt: 1, carbon: 5 }, { rt: 2, ignore: true }, { rt: 3 }]);
    t.eq(C.choiceOptions(compounds), [['', '— Unassigned —'], ['ignore', 'Ignore (extra)'],
        ['5', 'C5 — 36°C'], ['6', 'C6 — 69°C']]);
    // what Save says
    t.eq(C.savedText({ anchors: 2 }), ['Saved, but only 2 carbons assigned: assign at least 4 ' +
        '(ideally the full ladder) for accurate distillation numbers.', 'err']);
    t.eq(C.savedText({ anchors: 9, queued: 3 }), ['Saved: 9 calibration anchors active; 3 waiting samples queued', 'ok']);
    t.eq(C.savedText({ anchors: 9, queued: 1 }), ['Saved: 9 calibration anchors active; 1 waiting sample queued', 'ok']);
    t.eq(C.savedText({ anchors: 9 }), ['Saved: 9 calibration anchors active', 'ok']);
    t.eq(C.counts([{ choice: '5' }, { choice: 'ignore' }, { choice: '' }]), { peaks: 3, assigned: 1 });
};
