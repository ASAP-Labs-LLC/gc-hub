"""Synthetic ANDI/netCDF chromatograms for tests. Deterministic: same args,
same data (no randomness).

``write_cdf`` writes exactly what ``distill`` reads:

* ``_read_cdf_unlocked``: the intensity variable ``ordinate_values`` (the
  ANDI name; ``total_intensity``/``intensity_values``/``intensity`` are
  also accepted), no explicit time variable, so the time axis is rebuilt as
  ``arange(n) * actual_sampling_interval + actual_delay_time`` (seconds,
  divided by 60 into minutes).
* ``cdf_metadata``: the global attributes ``sample_name`` and
  ``injection_date_time_stamp`` (ANDI compact ``YYYYMMDDHHMMSS+0000``,
  parsed by ``parse_injection_datetime``).

Real Agilent files store these as float32; the fixtures use float64 so the
arrays read back bit for bit.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import netCDF4
import numpy as np

DT_MIN = 0.001            # 0.06 s sampling
RUN_MIN = 8.0


def gaussian(t, centre, height, width):
    return height * np.exp(-0.5 * ((t - centre) / width) ** 2)


def ladder_times(n=20, first=0.50, step=0.30):
    return [round(first + i * step, 4) for i in range(n)]


def write_cdf(path, times, signal, sample_name, injected: datetime, method_name=None,
              raw_stamp=None, dtype="f8", fmt="NETCDF3_CLASSIC"):
    """Write the variables/attributes distill reads (see the module docstring).

    ``times`` must be evenly spaced minutes; it is stored as a sampling
    interval plus a delay, the way ANDI files store it. ``method_name``, when
    given, is written as the ANDI global ``detection_method_name`` (real
    Agilent files carry e.g. ``SIMDISB.M``); by default it is omitted, so the
    fixtures the golden rows were captured from stay exactly as they were.
    distill does not read it. ``raw_stamp`` overrides the injection stamp text
    verbatim (``""`` leaves the stamp out); ``sample_name=None`` leaves the
    name out. ``dtype`` is the intensity variable's type (``"f4"`` stores it
    as real Agilent files do); ``fmt`` the netCDF file format."""
    path = Path(path)
    times = np.asarray(times, float)
    signal = np.asarray(signal, float)
    if times.size != signal.size:
        raise ValueError("times and signal differ in length")
    interval_s = DT_MIN * 60.0 if times.size < 2 else float(times[1] - times[0]) * 60.0
    with netCDF4.Dataset(path, "w", format=fmt) as ds:
        ds.dataset_completeness = "C1+C2"
        if sample_name is not None:
            ds.sample_name = sample_name
        stamp = injected.strftime("%Y%m%d%H%M%S") + "+0000" if raw_stamp is None else raw_stamp
        if stamp:
            ds.injection_date_time_stamp = stamp
        if method_name is not None:
            ds.detection_method_name = method_name
        ds.createDimension("point_number", signal.size)
        v = ds.createVariable("actual_sampling_interval", "f8")
        v.assignValue(interval_s)
        v = ds.createVariable("actual_delay_time", "f8")
        v.assignValue(float(times[0]) * 60.0 if times.size else 0.0)
        v = ds.createVariable("actual_run_time_length", "f8")
        v.assignValue(float(times[-1] - times[0]) * 60.0 if times.size else 0.0)
        v = ds.createVariable("ordinate_values", dtype, ("point_number",))
        v[:] = signal
    return path


def _axis():
    return np.arange(0, RUN_MIN, DT_MIN)


def calibration_cdf(path, injected=datetime(2026, 9, 16, 9, 0, 0), method_name=None):
    t = _axis()
    y = gaussian(t, 0.30, 50000, 0.01)                   # CS2 solvent
    for i, c in enumerate(ladder_times()):
        y += gaussian(t, c, 8000 - 150 * i, 0.012)       # C5.. ladder
    y += gaussian(t, 1.13, 900, 0.01)                    # impurity
    return write_cdf(path, t, y, "CAL 9.16", injected, method_name=method_name)


# Injection times are chosen so their compact stamps parse correctly: on
# Python 3.11+ ``datetime.fromisoformat`` (tried first by
# ``parse_injection_datetime``) accepts any character as the date/time
# separator, so a stamp like ``20260925002300+0000`` (00:23) is misread as
# 02:30 instead of falling through to the compact-format branch. Hours whose
# second digit is 3 or more make the ISO attempt fail and avoid that.
def sample_cdf(path, name="40304", injected=datetime(2026, 9, 25, 14, 23, 0), shift=0.0,
               method_name=None):
    t = _axis()
    y = (gaussian(t, 0.30, 50000, 0.01)
         + gaussian(t, 3.2 + shift, 1200, 0.9)
         + gaussian(t, 4.6 + shift, 600, 0.6))
    y += 40 + 5 * t                                      # bleed ramp
    return write_cdf(path, t, y, name, injected, method_name=method_name)


def blank_cdf(path, injected=datetime(2026, 9, 24, 15, 30, 27), name="Blank", method_name=None):
    t = _axis()
    y = gaussian(t, 0.30, 50000, 0.01) + 40 + 5 * t
    return write_cdf(path, t, y, name, injected, method_name=method_name)


def truncated_copy(src, dest, keep_fraction=0.95):
    """A copy of ``src`` cut short, as an interrupted copy leaves it: the
    NetCDF header is intact but the data runs out (netCDF4 zero-fills the
    rest when read)."""
    data = Path(src).read_bytes()
    Path(dest).write_bytes(data[:int(len(data) * keep_fraction)])
    return Path(dest)
