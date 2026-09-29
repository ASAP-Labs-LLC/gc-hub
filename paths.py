"""paths.py — where every piece of runtime state lives.

The hub keeps all of its state under one folder, ``GC_DATA_DIR`` (set by the
ASAPSV1 updater to ``C:\\ASAPApps\\gc\\data``), so a release directory stays
immutable and a deploy can never delete results. There is no other mode (v2,
spec D14): ``app.py`` refuses to start without it, and every function below
except ``data_dir`` raises ``DataDirMissing`` when it is unset. (The copies
on the share run v1.x, which has its own copy of this module.)

Stdlib only, no import-time side effects; every function takes ``env`` so
tests don't touch ``os.environ``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

DATA_ENV = "GC_DATA_DIR"
MISSING_TEXT = (f"{DATA_ENV} is not set. The GC hub keeps all of its state (the store, "
                f"settings.json, CDFs, exports, app.log) in that folder: set it to the data "
                f"folder (under the updater, C:\\ASAPApps\\gc\\data). See DEPLOY.md.")


class DataDirMissing(RuntimeError):
    """``GC_DATA_DIR`` is unset or blank."""

    def __init__(self) -> None:
        super().__init__(MISSING_TEXT)


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def data_dir(env=None) -> Optional[Path]:
    """The data folder, absolute, or ``None`` when ``GC_DATA_DIR`` is unset/blank."""
    raw = (_env(env).get(DATA_ENV) or "").strip()
    if not raw:
        return None
    # Resolve relative to absolute once, here: every consumer below joins or
    # compares this path, and a relative GC_DATA_DIR would otherwise make
    # those results depend on cwd (the updater's release folder).
    return Path(raw).resolve()


def require_data_dir(env=None) -> Path:
    d = data_dir(env)
    if d is None:
        raise DataDirMissing()
    return d


def settings_file(env=None) -> Path:
    return require_data_dir(env) / "settings.json"


def default_results_csv(env=None) -> Path:
    """v1's results CSV location (``distill.process_cdf``, kept as the golden
    reference); the hub's own export files are ``exports.HubExporter``'s."""
    return require_data_dir(env) / "distill_results.csv"


def default_processed_dir(env=None) -> Path:
    return require_data_dir(env) / "processed_cdf"


def default_export_dir(env=None) -> Path:
    return require_data_dir(env) / "exports"


def standards_dir(env=None) -> Path:
    return require_data_dir(env) / "gc_comparison_standards"


def notifications_file(env=None) -> Path:
    return require_data_dir(env) / "notifications.json"


def log_file(env=None) -> Path:
    return require_data_dir(env) / "app.log"
