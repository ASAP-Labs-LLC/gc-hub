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


class DataDirMissing(RuntimeError):
    """``GC_DATA_DIR`` is unset or blank."""


def require_data_dir(env=None) -> Path:
    d = data_dir(env)
    if d is None:
        raise DataDirMissing(f"{DATA_ENV} is not set; see DEPLOY.md")
    return d


def data_dir(env=None) -> Optional[Path]:
    raw = (_env(env).get(DATA_ENV) or "").strip()
    if not raw:
        return None
    # Resolve relative to absolute once, here: every consumer below joins or
    # compares this path, and a relative GC_DATA_DIR would otherwise make
    # those results depend on cwd — exactly what deployed mode exists to
    # avoid (cwd is the updater's release folder, not the data folder).
    return Path(raw).resolve()


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


def default_watch_dir(env=None) -> str:
    """Default ``watch_dir``: cwd in legacy mode, empty when deployed.

    In deployed mode cwd is the updater's immutable release folder —
    watching it would treat the app's own files as instrument data.
    ``app._get_looker()``'s guard (Task 3) keeps the watcher off while this
    is empty, until the operator sets a real folder.

    Returns ``str`` (not ``Path``) because this only ever lands in a
    settings dict, never joined onto another path.
    """
    return "" if data_dir(env) is not None else str(Path.cwd())


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
