"""paths.py — where every piece of runtime state lives.

Two modes, decided by ``GC_DATA_DIR``:

* **deployed** (set, e.g. by the ASAPSV1 updater to ``C:\\ASAPApps\\gc\\data``):
  all state under that one folder, so a release directory stays immutable and
  a deploy can never delete results.
* **legacy** (unset): exactly the historic locations — home-dir settings per
  port, cwd-relative results — so copies still running from the share are
  unaffected.

Stdlib only, no import-time side effects; every function takes ``env`` so
tests don't touch ``os.environ``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

import instance

DATA_ENV = "GC_DATA_DIR"


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def data_dir(env=None) -> Optional[Path]:
    raw = (_env(env).get(DATA_ENV) or "").strip()
    return Path(raw) if raw else None


def settings_file(env=None) -> Path:
    d = data_dir(env)
    return d / "settings.json" if d else instance.settings_path(env=_env(env))


def default_results_csv(env=None) -> Path:
    d = data_dir(env)
    return (d if d else Path.cwd()) / "distill_results.csv"


def default_processed_dir(env=None) -> Path:
    d = data_dir(env)
    return (d if d else Path.cwd()) / "processed_cdf"


def default_export_dir(env=None) -> Path:
    d = data_dir(env)
    return (d if d else Path.cwd()) / "exports"


def standards_dir(env=None) -> Path:
    d = data_dir(env)
    return (d if d else Path.home()) / "gc_comparison_standards"


def dir_cache_file(env=None) -> Path:
    d = data_dir(env)
    return d / "dircache.json" if d else Path.home() / ".gc_viewer_dircache.json"


def notifications_file(env=None) -> Path:
    d = data_dir(env)
    return d / "notifications.json" if d else Path.home() / ".gc_viewer_notifications.json"


def log_file(env=None) -> Optional[Path]:
    d = data_dir(env)
    return d / "app.log" if d else None
