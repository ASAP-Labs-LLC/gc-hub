// Flag-rule editor helpers: row values ↔ rule objects, plus the effective
// rule list derived from settings (mirrors backend sample_flags.load_rules).
const {
    cleanFlagRule, effectiveFlagRules, newFlagRule,
} = require('../../static/js/flagrules.js');

module.exports = (t) => {
    // cleanFlagRule coerces DOM string values and validates
    t.eq(cleanFlagRule({
        name: ' Early High-Signal ', condition: 'above', threshold: '7500',
        t_start: '0', t_end: '0.5', color: '#e67e22', enabled: true,
    }), {
        name: 'Early High-Signal', condition: 'above', threshold: 7500,
        t_start: 0, t_end: 0.5, color: '#e67e22', enabled: true,
    });

    // invalid rows → null (bad threshold, inverted window, bad condition)
    t.eq(cleanFlagRule({ name: 'X', condition: 'above', threshold: 'abc',
        t_start: '0', t_end: '1', color: '#fff', enabled: true }), null);
    t.eq(cleanFlagRule({ name: 'X', condition: 'above', threshold: '10',
        t_start: '2', t_end: '1', color: '#fff', enabled: true }), null);
    t.eq(cleanFlagRule({ name: 'X', condition: 'sideways', threshold: '10',
        t_start: '0', t_end: '1', color: '#fff', enabled: true }), null);

    // effectiveFlagRules: parses the saved JSON list
    const saved = [{ name: 'A', condition: 'below', threshold: 100,
        t_start: 0, t_end: 5, color: '#3498db', enabled: true }];
    t.eq(effectiveFlagRules({ sample_flag_rules: JSON.stringify(saved) }), saved);

    // empty setting → migrated from legacy early-signal keys + seeded No Signal
    const migrated = effectiveFlagRules({
        sample_flag_rules: '',
        early_signal_enabled: 'true',
        early_signal_time_min: '0.8',
        early_signal_intensity_threshold: '6000',
    });
    t.eq(migrated.length, 2);
    t.eq(migrated[0].name, 'Early High-Signal');
    t.eq(migrated[0].threshold, 6000);
    t.eq(migrated[0].t_end, 0.8);
    t.eq(migrated[1].name, 'No Signal');
    t.eq(migrated[1].condition, 'below');

    // bad JSON also falls back to migration
    t.eq(effectiveFlagRules({ sample_flag_rules: '{oops' })[0].name,
        'Early High-Signal');

    // newFlagRule produces a valid enabled rule with a color
    const fresh = newFlagRule(3);
    t.eq(cleanFlagRule(fresh) !== null, true);
    t.eq(fresh.enabled, true);
    t.eq(fresh.color.startsWith('#'), true);
};
