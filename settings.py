r"""settings.py (Web version)
~~~~~~~~~~~~~
Persistent path/config helper — web-compatible (no PyQt dependencies).

Where the JSON lives, and what the path/dir *values* inside it default to,
both come from ``paths.py`` and split into two modes:

* **legacy** (``GC_DATA_DIR`` unset — a copy still running from the share):
  unchanged from before this module existed —
      • Windows  →  %USERPROFILE%\.gc_viewer_settings[-port].json
      • macOS/*nix →  ~/.gc_viewer_settings[-port].json
  with ``processed_cdf_dir``/``distill_output``/``export_folder`` defaulting
  cwd-relative and ``watch_dir`` defaulting to cwd itself.
* **deployed** (``GC_DATA_DIR`` set by the ASAPSV1 updater): one file at
  ``GC_DATA_DIR/settings.json``, and every path default lives under
  ``GC_DATA_DIR`` too — see ``paths.py`` for the full table.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Dict

import instance
import paths

LOGGER = logging.getLogger("settings")

# Derived from GC_PORT (legacy) or GC_DATA_DIR (deployed) at import time so
# several instances can run side by side. Stays a module-level Path (not a
# call) so tests can redirect it.
CONFIG_PATH: Path = paths.settings_file()

# ---------------------------------------------------------------------------
# Default values — update here when adding new settings keys
# ---------------------------------------------------------------------------
DEFAULTS: Dict[str, str] = {
    "watch_dir": paths.default_watch_dir(),
    "processed_cdf_dir": str(paths.default_processed_dir()),
    "distill_output": str(paths.default_results_csv()),
    "calibration_cdf": "",
    "calibration_assignments": "",
    "calibration_sensitivity": "50",
    "theme_css": "",
    "series_colors": "",
    "calibration_labels": "",
    "blank_cache_file": str(paths.default_processed_dir() / ".blank_cache.json"),
    "export_folder": str(paths.default_export_dir()),
    # Read-only, unused: no route or template reads this key back (grepped —
    # nothing else in the repo references "splash_image_file"). Left
    # cwd-relative by design rather than routed through paths.py: cwd is
    # wherever app.py's own files are, which is exactly where a shipped
    # splash asset would live, deployed or not. Revisit if this setting is
    # ever wired up to something that actually reads it.
    "splash_image_file": str(Path.cwd() / "splash.png"),
    "export_graph_qss": "",
    "comparison_defaults_dir": str(paths.standards_dir()),
    "comparison_export_template": "",
    "correction_factors_json": "//asapserver/Labsharedrive/ASAP Lab Results/EQM_Correction Factor/correction_factors.json",
    "analysis_quantile": "0.20",
    "analysis_window": "301",
    "analysis_sigma": "34.0",
    "analysis_thresh_marginal": "100",
    "analysis_thresh_moderate": "500",
    "analysis_thresh_significant": "2000",
    "analysis_gas_c_start": "5",
    "analysis_gas_c_end": "11",
    "analysis_oil_c_start": "20",
    "analysis_oil_c_end": "44",
    "analysis_x_max_min": "7.0",
    "analysis_spike_min_width_min": "0.02",
    "analysis_range_overlays": "",
    "analysis_report_logo": "",
    "analysis_export_last_dir": "",
    "early_signal_enabled": "true",
    "early_signal_time_min": "0.5",
    "early_signal_intensity_threshold": "7500",
    # Operator-defined flag rules (JSON list); empty → migrated from the
    # legacy early_signal_* keys above (see sample_flags.load_rules)
    "sample_flag_rules": "",
    # Fuel-type best-fit classification (fuel_fit.py)
    "bestfit_enabled": "true",
    "bestfit_threshold": "0.93",
    "bestfit_shift_tolerance_min": "0.05",
    "bestfit_mix_min_frac": "0.10",
}

# Settings that MUST differ between concurrently running instances. A new
# instance inherits everything else from the primary (port 5560) config, but
# these fall back to DEFAULTS so the operator is forced to choose them —
# two instances sharing distill_output would both append to one results CSV,
# and a shared export_folder mixes two workstations' reports together.
PER_INSTANCE_KEYS = (
    "watch_dir",
    "processed_cdf_dir",
    "distill_output",
    "export_folder",
)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _seed_new_instance() -> None:
    """First run on a non-default port: copy the primary instance's config,
    minus the per-instance paths. No-op in every other case.
    """
    if paths.data_dir() is not None:
        # Deployed mode has one settings file per data dir, not per port —
        # there is nothing to seed from.
        return
    if instance.active_port() == instance.DEFAULT_PORT:
        return
    if CONFIG_PATH.exists():
        return

    primary = instance.settings_path(instance.DEFAULT_PORT)
    if primary == CONFIG_PATH or not primary.exists():
        return

    try:
        data = json.loads(primary.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Could not seed instance settings from %s: %s", primary, exc)
        return
    if not isinstance(data, dict):
        return

    for key in PER_INSTANCE_KEYS:
        data.pop(key, None)

    try:
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        LOGGER.info("Seeded new instance settings at %s from %s", CONFIG_PATH, primary)
    except Exception as exc:
        LOGGER.warning("Could not write seeded settings to %s: %s", CONFIG_PATH, exc)


def load_settings() -> Dict[str, str]:
    _seed_new_instance()   # no-op unless this is a fresh non-default port
    conf = DEFAULTS.copy()
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            conf.update(data)
        except Exception as exc:
            LOGGER.warning("Settings read failed, using defaults: %s", exc)

    proc = Path(conf.get("processed_cdf_dir", paths.default_processed_dir()))
    conf["blank_cache_file"] = str(proc / ".blank_cache.json")
    conf.setdefault("export_folder", str(paths.default_export_dir()))
    conf.setdefault("splash_image_file", str(Path.cwd() / "splash.png"))  # unused; see DEFAULTS comment above

    # Manage comparison standards directory
    comp_dir_str = (conf.get("comparison_defaults_dir") or "").strip()
    if not comp_dir_str:
        comp_dir_str = DEFAULTS["comparison_defaults_dir"]
    comp_dir = Path(comp_dir_str).expanduser()
    try:
        comp_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        LOGGER.warning("Unable to create comparison standards directory %s: %s", comp_dir, exc)
    conf["comparison_defaults_dir"] = str(comp_dir.resolve())

    return conf


# On Windows os.replace fails with PermissionError while another handle without
# delete-sharing has settings.json open (a concurrent load_settings, antivirus),
# usually for a moment only. Same idea as distill._replace_retrying.
SAVE_REPLACE_ATTEMPTS = 5
SAVE_REPLACE_BACKOFF_SECONDS = 0.1


def _replace_retrying(tmp: str, path: Path) -> None:
    for attempt in range(SAVE_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == SAVE_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(SAVE_REPLACE_BACKOFF_SECONDS)


def save_settings(conf: Dict[str, str]) -> None:
    """Write ``conf`` to ``CONFIG_PATH`` atomically (same pattern as
    ``qbench_secrets.save_default``): a temp file in the same folder, fsync,
    then ``os.replace``. A failure part-way through leaves the previous
    settings file untouched (it holds the operator's calibration assignments)
    and removes the temp file. Errors are logged, never raised."""
    tmp = None
    try:
        directory = str(CONFIG_PATH.parent)
        # Named after the file, so a leftover from a hard kill is recognisable
        # next to it (legacy mode keeps settings in the home folder).
        fd, tmp = tempfile.mkstemp(prefix=f".{CONFIG_PATH.name}.", suffix=".tmp",
                                   dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(conf, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        try:  # keep the existing file's permissions (mkstemp makes it 0600)
            os.chmod(tmp, stat.S_IMODE(CONFIG_PATH.stat().st_mode))
        except OSError:
            pass
        _replace_retrying(tmp, CONFIG_PATH)
        tmp = None
    except Exception as exc:
        LOGGER.error("Settings save failed: %s", exc)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
