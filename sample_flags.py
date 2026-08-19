"""Operator-defined sample flag rules.

Generalizes the old Early High-Signal detection into a rule list stored as
JSON in the ``sample_flag_rules`` setting.  Each rule:

```json
{"name": "Early High-Signal", "condition": "above", "threshold": 7500,
 "t_start": 0.0, "t_end": 0.5, "color": "#e67e22", "enabled": true}
```

Semantics:

* ``above`` — flag if **any** point in ``[t_start, t_end]`` exceeds
  ``threshold`` (spike-style detection).
* ``below`` — flag if **all** points in ``[t_start, t_end]`` stay under
  ``threshold`` (no-signal detection over a sustained window).

Importable without app.py's side effects; numpy only.
"""
from __future__ import annotations

import hashlib
import json
import logging

import numpy as np

LOGGER = logging.getLogger(__name__)

#: Seeded companion rule created during legacy migration.
DEFAULT_NO_SIGNAL_RULE = {
    "name": "No Signal",
    "condition": "below",
    "threshold": 500.0,
    "t_start": 0.0,
    "t_end": 6.5,
    "color": "#3498db",
    "enabled": True,
}


def _clean_rule(raw: dict) -> dict | None:
    """Validate/coerce one raw rule dict; return None if unusable."""
    try:
        rule = {
            "name": str(raw.get("name", "Rule")).strip() or "Rule",
            "condition": str(raw.get("condition", "above")).strip().lower(),
            "threshold": float(raw.get("threshold")),
            "t_start": float(raw.get("t_start", 0.0)),
            "t_end": float(raw.get("t_end", 0.0)),
            "color": str(raw.get("color", "") or "#e67e22"),
            "enabled": bool(raw.get("enabled", True)),
        }
    except (TypeError, ValueError):
        return None
    if rule["condition"] not in ("above", "below"):
        return None
    if rule["t_end"] <= rule["t_start"]:
        return None
    return rule


def migrate_legacy(conf: dict) -> list[dict]:
    """Build a rule list from the legacy ``early_signal_*`` settings, plus a
    seeded blue No-Signal rule."""
    try:
        threshold = float(conf.get("early_signal_intensity_threshold", "7500"))
    except (TypeError, ValueError):
        threshold = 7500.0
    try:
        t_end = float(conf.get("early_signal_time_min", "0.5"))
    except (TypeError, ValueError):
        t_end = 0.5
    enabled = str(conf.get("early_signal_enabled", "true")).lower() == "true"
    return [
        {
            "name": "Early High-Signal",
            "condition": "above",
            "threshold": threshold,
            "t_start": 0.0,
            "t_end": t_end,
            "color": "#e67e22",
            "enabled": enabled,
        },
        dict(DEFAULT_NO_SIGNAL_RULE),
    ]


def load_rules(conf: dict) -> list[dict]:
    """Load the rule list from settings, migrating legacy keys when the
    ``sample_flag_rules`` setting is absent, empty, or unparsable."""
    raw = conf.get("sample_flag_rules", "")
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            rules = [r for r in (_clean_rule(x) for x in parsed) if r]
            if rules:
                return rules
        except Exception as exc:
            LOGGER.warning("Bad sample_flag_rules setting (%s) — migrating legacy", exc)
    return migrate_legacy(conf)


def evaluate_rules(t, y, rules: list[dict]) -> list[dict]:
    """Evaluate *rules* against a chromatogram; return matched flags as
    ``[{"name", "color"}]`` in rule order.  Never raises on bad rules."""
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    hits: list[dict] = []
    for raw in rules:
        rule = _clean_rule(raw) if raw else None
        if rule is None or not rule["enabled"]:
            continue
        mask = (t >= rule["t_start"]) & (t <= rule["t_end"])
        if not mask.any():
            continue
        window = y[mask]
        if rule["condition"] == "above":
            matched = bool(np.max(window) > rule["threshold"])
        else:  # below
            matched = bool(np.max(window) < rule["threshold"])
        if matched:
            hits.append({"name": rule["name"], "color": rule["color"]})
    return hits


def rules_fingerprint(rules: list[dict]) -> str:
    """Stable hash of a rule list — used to invalidate per-file flag caches
    when the operator edits the rules."""
    canon = json.dumps(rules, sort_keys=True, default=str)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]
