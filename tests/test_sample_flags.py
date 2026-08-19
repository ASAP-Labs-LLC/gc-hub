"""Operator-defined sample flag rules (sample_flags module).

Generalizes the old Early High-Signal detection: each rule is
{name, condition: above|below, threshold, t_start, t_end, color, enabled}.
- above: flag if ANY point in the window exceeds the threshold
- below: flag if ALL points in the window stay under the threshold
"""
import json
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sample_flags  # noqa: E402


def _trace(n=1000, dt=0.01, level=100.0):
    t = np.arange(n) * dt  # 0 .. 10 min
    y = np.full(n, level)
    return t, y


HIGH_RULE = {"name": "Early High-Signal", "condition": "above",
             "threshold": 7500.0, "t_start": 0.0, "t_end": 0.5,
             "color": "#e67e22", "enabled": True}
NOSIG_RULE = {"name": "No Signal", "condition": "below",
              "threshold": 500.0, "t_start": 0.0, "t_end": 6.5,
              "color": "#3498db", "enabled": True}


class TestEvaluate:
    def test_above_any_point_triggers(self):
        t, y = _trace()
        y[20] = 9000.0  # single early spike above threshold
        hits = sample_flags.evaluate_rules(t, y, [HIGH_RULE])
        assert hits == [{"name": "Early High-Signal", "color": "#e67e22"}]

    def test_above_outside_window_ignored(self):
        t, y = _trace()
        y[200] = 9000.0  # at 2.0 min — outside the 0–0.5 window
        assert sample_flags.evaluate_rules(t, y, [HIGH_RULE]) == []

    def test_below_all_points_triggers(self):
        t, y = _trace(level=100.0)  # flat 100 counts < 500 for entire window
        hits = sample_flags.evaluate_rules(t, y, [NOSIG_RULE])
        assert hits == [{"name": "No Signal", "color": "#3498db"}]

    def test_below_one_high_point_defuses(self):
        t, y = _trace(level=100.0)
        y[300] = 600.0  # one point above threshold inside window → has signal
        assert sample_flags.evaluate_rules(t, y, [NOSIG_RULE]) == []

    def test_disabled_rule_skipped(self):
        t, y = _trace(level=100.0)
        rule = dict(NOSIG_RULE, enabled=False)
        assert sample_flags.evaluate_rules(t, y, [rule]) == []

    def test_multiple_rules_all_reported(self):
        t, y = _trace(level=100.0)
        y[20] = 9000.0  # spike inside the high-signal window
        late_nosig = dict(NOSIG_RULE, t_start=2.0, t_end=6.5)  # after the spike
        hits = sample_flags.evaluate_rules(t, y, [HIGH_RULE, late_nosig])
        assert [h["name"] for h in hits] == ["Early High-Signal", "No Signal"]

    def test_empty_window_never_flags(self):
        t, y = _trace()
        rule = dict(HIGH_RULE, t_start=20.0, t_end=30.0)  # beyond trace end
        assert sample_flags.evaluate_rules(t, y, [rule]) == []

    def test_bad_rule_values_skipped_not_raised(self):
        t, y = _trace()
        bad = {"name": "Broken", "condition": "above", "threshold": "nan?",
               "t_start": "x", "t_end": None, "color": "", "enabled": True}
        assert sample_flags.evaluate_rules(t, y, [bad]) == []


class TestLoadRules:
    def test_load_from_json_setting(self):
        conf = {"sample_flag_rules": json.dumps([HIGH_RULE, NOSIG_RULE])}
        rules = sample_flags.load_rules(conf)
        assert [r["name"] for r in rules] == ["Early High-Signal", "No Signal"]

    def test_migrates_legacy_early_signal_settings(self):
        conf = {
            "sample_flag_rules": "",
            "early_signal_enabled": "true",
            "early_signal_time_min": "0.8",
            "early_signal_intensity_threshold": "6000",
        }
        rules = sample_flags.load_rules(conf)
        names = [r["name"] for r in rules]
        assert "Early High-Signal" in names
        hi = next(r for r in rules if r["name"] == "Early High-Signal")
        assert hi["condition"] == "above"
        assert hi["threshold"] == 6000.0
        assert hi["t_end"] == 0.8
        assert hi["enabled"] is True
        # seeded companion rule for the no-signal case
        assert "No Signal" in names
        ns = next(r for r in rules if r["name"] == "No Signal")
        assert ns["condition"] == "below"

    def test_legacy_disabled_carries_over(self):
        conf = {
            "sample_flag_rules": "",
            "early_signal_enabled": "false",
            "early_signal_time_min": "0.5",
            "early_signal_intensity_threshold": "7500",
        }
        rules = sample_flags.load_rules(conf)
        hi = next(r for r in rules if r["name"] == "Early High-Signal")
        assert hi["enabled"] is False

    def test_bad_json_falls_back_to_migration(self):
        conf = {"sample_flag_rules": "{broken",
                "early_signal_intensity_threshold": "7500"}
        rules = sample_flags.load_rules(conf)
        assert any(r["name"] == "Early High-Signal" for r in rules)


def test_rules_fingerprint_changes_with_rules():
    a = sample_flags.rules_fingerprint([HIGH_RULE])
    b = sample_flags.rules_fingerprint([HIGH_RULE, NOSIG_RULE])
    c = sample_flags.rules_fingerprint([dict(HIGH_RULE, threshold=1.0)])
    assert a != b and a != c
    assert a == sample_flags.rules_fingerprint([dict(HIGH_RULE)])
