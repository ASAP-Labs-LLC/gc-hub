/* Pure, DOM-free flag-rule helpers — shared by the browser (window globals)
   and Node tests (module.exports). Mirrors backend sample_flags semantics:
   above = any point in window exceeds threshold; below = all points under. */
(function (root) {
    const RULE_COLORS = ['#e67e22', '#3498db', '#27ae60', '#8e44ad', '#c0392b', '#16a085'];

    /** Coerce/validate one editor row into a rule object; null if unusable. */
    function cleanFlagRule(raw) {
        if (!raw) return null;
        const rule = {
            name: String(raw.name == null ? 'Rule' : raw.name).trim() || 'Rule',
            condition: String(raw.condition || 'above').trim().toLowerCase(),
            threshold: parseFloat(raw.threshold),
            t_start: parseFloat(raw.t_start),
            t_end: parseFloat(raw.t_end),
            color: raw.color || '#e67e22',
            enabled: !!raw.enabled,
        };
        if (rule.condition !== 'above' && rule.condition !== 'below') return null;
        if (!isFinite(rule.threshold) || !isFinite(rule.t_start) || !isFinite(rule.t_end)) return null;
        if (rule.t_end <= rule.t_start) return null;
        return rule;
    }

    /** The effective rule list for the given settings object: the saved JSON
        list, or (when empty/unparsable) rules migrated from the legacy
        early_signal_* keys — same as backend sample_flags.load_rules. */
    function effectiveFlagRules(settings) {
        const raw = settings.sample_flag_rules || '';
        if (raw) {
            try {
                const parsed = typeof raw === 'string' ? JSON.parse(raw) : raw;
                const rules = parsed.map(cleanFlagRule).filter(Boolean);
                if (rules.length) return rules;
            } catch (_) { /* fall through to migration */ }
        }
        const threshold = parseFloat(settings.early_signal_intensity_threshold) || 7500;
        const tEnd = parseFloat(settings.early_signal_time_min) || 0.5;
        const enabled = String(settings.early_signal_enabled || 'true').toLowerCase() === 'true';
        return [
            { name: 'Early High-Signal', condition: 'above', threshold: threshold,
              t_start: 0, t_end: tEnd, color: '#e67e22', enabled: enabled },
            { name: 'No Signal', condition: 'below', threshold: 500,
              t_start: 0, t_end: 6.5, color: '#3498db', enabled: true },
        ];
    }

    /** A fresh editable rule for the "+ Add rule" button. */
    function newFlagRule(index) {
        return {
            name: 'Rule ' + (index + 1),
            condition: 'above',
            threshold: 1000,
            t_start: 0,
            t_end: 1,
            color: RULE_COLORS[index % RULE_COLORS.length],
            enabled: true,
        };
    }

    root.cleanFlagRule = cleanFlagRule;
    root.effectiveFlagRules = effectiveFlagRules;
    root.newFlagRule = newFlagRule;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { cleanFlagRule, effectiveFlagRules, newFlagRule };
    }
})(typeof window !== 'undefined' ? window : globalThis);
