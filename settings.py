r"""settings.py
~~~~~~~~~~~
The hub's global settings: one JSON file, ``GC_DATA_DIR/settings.json``
(``paths.settings_file()``), with every path default under ``GC_DATA_DIR``
(see ``paths.py``). Per-instrument configuration (calibration, corrections,
export path) lives in the store's ``instruments`` rows, not here.

Importable without ``GC_DATA_DIR`` (tools and tests): ``CONFIG_PATH`` is then
``None`` (``load_settings`` returns the defaults, ``save_settings`` refuses)
and the path defaults are empty. The app itself never runs that way.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

import paths

LOGGER = logging.getLogger("settings")


def _under_data(fn) -> str:
    """``str(fn())`` (a ``paths`` default), or ``""`` without GC_DATA_DIR."""
    try:
        return str(fn())
    except paths.DataDirMissing:
        return ""


# Resolved from GC_DATA_DIR at import time. Stays a module-level attribute
# (not a call) so tests can redirect it; None without GC_DATA_DIR.
CONFIG_PATH: Optional[Path] = paths.settings_file() if paths.data_dir() is not None else None

# ---------------------------------------------------------------------------
# Default values — update here when adding new settings keys
# ---------------------------------------------------------------------------
DEFAULTS: Dict[str, str] = {
    # v1 keys, still read by distill.process_cdf (the golden reference); the
    # hub neither watches a folder nor writes this CSV.
    "watch_dir": "",
    "processed_cdf_dir": _under_data(paths.default_processed_dir),
    "distill_output": _under_data(paths.default_results_csv),
    "calibration_cdf": "",
    "calibration_assignments": "",
    "calibration_sensitivity": "50",
    "theme_css": "",
    "series_colors": "",
    "calibration_labels": "",
    "blank_cache_file": "",
    "export_folder": _under_data(paths.default_export_dir),
    # Read-only, unused: no route or template reads this key back (grepped —
    # nothing else in the repo references "splash_image_file"). Left
    # cwd-relative by design rather than routed through paths.py: cwd is
    # wherever app.py's own files are, which is exactly where a shipped
    # splash asset would live, deployed or not. Revisit if this setting is
    # ever wired up to something that actually reads it.
    "splash_image_file": str(Path.cwd() / "splash.png"),
    "export_graph_qss": "",
    "comparison_defaults_dir": _under_data(paths.standards_dir),
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
    # Deviation bullets (phase 3): a trend run must be this wide (min) to
    # count; outside-range pieces closer than the merge gap (min) are shown
    # as one span; a spike counts when it reaches the report threshold
    # (empty = the moderate threshold in effect).
    "analysis_min_width_min": "0.05",
    "analysis_merge_gap_min": "0.10",
    "analysis_spike_report_threshold": "",
    # A counted spike must be sharp (full width at half height ≤ this, min)
    # and dominate the local peaks (|difference at the apex| ≥ this fraction
    # of the larger of the sample's and the standard's local peak height).
    "analysis_spike_max_fwhm_min": "0.20",
    "analysis_spike_min_dominance": "0.6",
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

# ---------------------------------------------------------------------------
# What /api/settings may change (T5 review C1). Everything else in
# settings.json is read-only there: paths (a LAN user could otherwise point
# the standards folder at the data folder and delete from it, or the logo at
# the admin setup code), the per-instrument calibration and corrections
# (the store owns those now), and blank_max_intensity_pa (which blank is
# genuine). Those change by editing settings.json on the server (DEPLOY.md).
# ---------------------------------------------------------------------------

# Anyone on the LAN: display only. Flag rules drive the list's flag badges
# (sample_cache, never a result or an export); series colours are a chart
# preference.
OPERATOR_KEYS = (
    "sample_flag_rules",
    "early_signal_enabled",             # legacy inputs sample_flag_rules migrates from
    "early_signal_time_min",
    "early_signal_intensity_threshold",
    "series_colors",
)

# The admin password: these change what is recorded or reported. Best fit is
# computed into every result's Best Fit / Fit Score export columns; the
# analysis_* values are the saved defaults of the Analysis tab and of the
# reports (and QBench PDFs) built from it, the same values "Set as Default"
# (/api/save-analysis-defaults, admin) saves.
ADMIN_KEYS = (
    "bestfit_enabled", "bestfit_threshold", "bestfit_shift_tolerance_min",
    "bestfit_mix_min_frac",
    "analysis_quantile", "analysis_window", "analysis_sigma",
    "analysis_thresh_marginal", "analysis_thresh_moderate", "analysis_thresh_significant",
    "analysis_gas_c_start", "analysis_gas_c_end", "analysis_oil_c_start", "analysis_oil_c_end",
    "analysis_x_max_min", "analysis_spike_min_width_min", "analysis_range_overlays",
    "analysis_min_width_min", "analysis_merge_gap_min", "analysis_spike_report_threshold",
    "analysis_spike_max_fwhm_min", "analysis_spike_min_dominance",
)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_settings() -> Dict[str, str]:
    conf = DEFAULTS.copy()
    if CONFIG_PATH is not None and CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            conf.update(data)
        except Exception as exc:
            LOGGER.warning("Settings read failed, using defaults: %s", exc)

    if paths.data_dir() is not None:
        # Fixed under the data folder in the hub, whatever settings.json says:
        # the standards folder is where standards are added and deleted, the
        # export folder where report PDFs are written.
        conf["comparison_defaults_dir"] = str(paths.standards_dir())
        conf["export_folder"] = str(paths.default_export_dir())
    proc = str(conf.get("processed_cdf_dir") or "").strip()
    conf["blank_cache_file"] = str(Path(proc) / ".blank_cache.json") if proc else ""
    conf.setdefault("export_folder", DEFAULTS["export_folder"])
    conf.setdefault("splash_image_file", str(Path.cwd() / "splash.png"))  # unused; see DEFAULTS comment above

    # Manage comparison standards directory
    comp_dir_str = (conf.get("comparison_defaults_dir") or "").strip()
    if not comp_dir_str:
        comp_dir_str = DEFAULTS["comparison_defaults_dir"]
    if not comp_dir_str:        # no GC_DATA_DIR and none configured: nothing to create
        return conf
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


def update_settings(changes: Dict[str, str]) -> None:
    """Write only ``changes`` into ``settings.json``: the file as it is on disk
    (not the defaults, nor the values ``load_settings`` computes or fixes)
    with those keys replaced."""
    on_disk: Dict[str, str] = {}
    if CONFIG_PATH is not None and CONFIG_PATH.is_file():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                on_disk = data
        except Exception as exc:
            LOGGER.warning("Settings read failed before an update: %s", exc)
            raise
    on_disk.update(changes)
    save_settings(on_disk)


def save_settings(conf: Dict[str, str]) -> None:
    """Write ``conf`` to ``CONFIG_PATH`` atomically (same pattern as
    ``qbench_secrets.save_default``): a temp file in the same folder, fsync,
    then ``os.replace``. A failure part-way through leaves the previous
    settings file untouched (it holds the operator's calibration assignments)
    and removes the temp file. Errors are logged, never raised."""
    tmp = None
    if CONFIG_PATH is None:
        LOGGER.error("Settings save failed: %s", paths.MISSING_TEXT)
        return
    try:
        directory = str(CONFIG_PATH.parent)
        # Named after the file, so a leftover from a hard kill is recognisable
        # next to it.
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
