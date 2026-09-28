"""``agent.json``: defaults, validation, load and atomic save (contract §1)."""
from __future__ import annotations

import json
from pathlib import Path

from . import util

DEFAULTS = {
    "hub_url": "",
    "token": "",
    "watch_dir": "",
    "include_subdirs": True,
    "poll_seconds": 5,
    "stable_seconds": 30,
    "results_mirror_path": "",
    "paused": False,
    "python": "",
}


class ConfigError(Exception):
    pass


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(raw):
    """Return a copy of *raw* with defaults filled; raise ConfigError naming
    every bad field. Unknown keys are kept untouched."""
    if not isinstance(raw, dict):
        raise ConfigError("agent.json must hold a JSON object")
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    bad = []
    url = cfg["hub_url"]
    if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")) \
            or len(url) <= len("http://"):
        bad.append("hub_url (must start with http:// or https://)")
    if not isinstance(cfg["token"], str) or not cfg["token"].strip():
        bad.append("token (missing)")
    for key in ("watch_dir", "results_mirror_path", "python"):
        if not isinstance(cfg[key], str):
            bad.append("%s (must be text)" % key)
    for key in ("include_subdirs", "paused"):
        if not isinstance(cfg[key], bool):
            bad.append("%s (must be true or false)" % key)
    if not _is_num(cfg["poll_seconds"]) or cfg["poll_seconds"] <= 0:
        bad.append("poll_seconds (must be a number > 0)")
    if not _is_num(cfg["stable_seconds"]) or cfg["stable_seconds"] < 0:
        bad.append("stable_seconds (must be a number >= 0)")
    if bad:
        raise ConfigError("agent.json: bad " + "; ".join(bad))
    cfg["hub_url"] = cfg["hub_url"].rstrip("/")
    return cfg


def read_raw(path):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError("agent.json not found at %s" % path)
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ConfigError("agent.json is not valid JSON: %s" % exc)


def load(path):
    return validate(read_raw(path))


def save(path, cfg):
    util.atomic_write_text(path, json.dumps(cfg, indent=2, sort_keys=True) + "\n")


def set_key(path, key, value):
    """Update one key in the file, keeping everything else as written."""
    raw = read_raw(path)
    raw[key] = value
    save(path, raw)
