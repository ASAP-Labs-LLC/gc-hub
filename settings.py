r"""settings.py (Web version)
~~~~~~~~~~~~~
Persistent path/config helper — web-compatible (no PyQt dependencies).

Stores JSON in:
    • Windows  →  %USERPROFILE%\.gc_viewer_settings.json
    • macOS/*nix →  ~/.gc_viewer_settings.json
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict

import instance

LOGGER = logging.getLogger("settings")

# Derived from GC_PORT at import time so several instances can run side by
# side. Stays a module-level Path (not a call) so tests can redirect it.
CONFIG_PATH: Path = instance.settings_path()

# ---------------------------------------------------------------------------
# Default values — update here when adding new settings keys
# ---------------------------------------------------------------------------
DEFAULTS: Dict[str, str] = {
    "watch_dir": str(Path.cwd()),
    "processed_cdf_dir": str(Path.cwd() / "processed_cdf"),
    "distill_output": str(Path.cwd() / "distill_results.csv"),
    "calibration_cdf": "",
    "calibration_assignments": "",
    "calibration_sensitivity": "50",
    "theme_css": "",
    "series_colors": "",
    "calibration_labels": "",
    "blank_cache_file": str(Path.cwd() / "processed_cdf" / ".blank_cache.json"),
    "export_folder": str(Path.cwd() / "exports"),
    "splash_image_file": str(Path.cwd() / "splash.png"),
    "export_graph_qss": "",
    "comparison_defaults_dir": str(CONFIG_PATH.parent / "gc_comparison_standards"),
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
# two instances sharing distill_output would both append to one results CSV.
PER_INSTANCE_KEYS = ("watch_dir", "processed_cdf_dir", "distill_output")


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _seed_new_instance() -> None:
    """First run on a non-default port: copy the primary instance's config,
    minus the per-instance paths. No-op in every other case.
    """
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

    proc = Path(conf.get("processed_cdf_dir", Path.cwd()))
    conf["blank_cache_file"] = str(proc / ".blank_cache.json")
    conf.setdefault("export_folder", str(Path.cwd() / "exports"))
    conf.setdefault("splash_image_file", str(Path.cwd() / "splash.png"))

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


def save_settings(conf: Dict[str, str]) -> None:
    try:
        with CONFIG_PATH.open("w", encoding="utf-8") as fh:
            json.dump(conf, fh, indent=2)
    except Exception as exc:
        LOGGER.error("Settings save failed: %s", exc)
