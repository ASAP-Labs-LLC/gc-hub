"""Tests for the phase-2 history-import matcher (``import_match``, contract §3).

Synthetic ANDI/AIA-style CDFs are written with netCDF4 in each test's temp
folder: a small ``ordinate_values`` trace, ``actual_sampling_interval``, and
the metadata distill reads (``sample_name``, ``injection_date_time_stamp`` /
``injection_date`` / ``injection_time``) as global attributes or char
variables, like the real Agilent files.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

try:
    import numpy as np
    from netCDF4 import Dataset

    import distill
    HAVE_DEPS = True
except ImportError:  # pragma: no cover - bare interpreter
    HAVE_DEPS = False

if HAVE_DEPS:
    import import_match as im  # a missing module must fail, not skip


def write_cdf(path, *, sample_name="S1", stamp="20260924153027+0000",
              stamp_key="injection_date_time_stamp", as_variables=False,
              points=64, seed=0, mtime=None):
    """Write a small CDF shaped like ChemStation's export. ``stamp=None``
    leaves the injection time out entirely; ``sample_name=None`` likewise."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with Dataset(path, "w", format="NETCDF3_CLASSIC") as ds:
        ds.createDimension("point_number", points)
        ds.createDimension("_255_byte_string", 255)
        ds.dataset_completeness = "C1+C2"
        ds.retention_unit = "seconds"
        meta = {}
        if sample_name is not None:
            meta["sample_name"] = sample_name
        if stamp is not None:
            meta[stamp_key] = stamp
        for key, val in meta.items():
            if as_variables:
                v = ds.createVariable(key, "S1", ("_255_byte_string",))
                v.set_auto_chartostring(False)
                v[:] = np.frombuffer(val.encode().ljust(255, b"\x00"), dtype="S1")
            else:
                setattr(ds, key, val)
        interval = ds.createVariable("actual_sampling_interval", "f4")
        interval.assignValue(0.2)
        delay = ds.createVariable("actual_delay_time", "f4")
        delay.assignValue(0.0)
        y = ds.createVariable("ordinate_values", "f4", ("point_number",))
        rng = np.random.default_rng(seed)
        y[:] = rng.random(points).astype("f4") * 100.0
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


@unittest.skipUnless(HAVE_DEPS, "needs numpy + netCDF4")
class ReadCdfMetaTests(_TmpCase):
    def test_compact_stamp_with_zone_is_canonical_and_from_cdf(self) -> None:
        p = write_cdf(self.tmp / "a.CDF", sample_name="AF26", stamp="20260917142942+0000")
        meta = im.read_cdf_meta(p)
        self.assertEqual(meta.lab_id, "AF26")
        self.assertEqual(meta.injection_dt, "2026-09-17 14:29:42")
        self.assertEqual(meta.dt_source, "cdf")
        self.assertEqual(meta.sha256, sha(p))
        self.assertEqual(meta.path, str(p))

    def test_metadata_stored_as_char_variables(self) -> None:
        p = write_cdf(self.tmp / "v.CDF", sample_name="GAS test1",
                      stamp="20260629094842+0000", as_variables=True)
        meta = im.read_cdf_meta(p)
        self.assertEqual(meta.lab_id, "GAS test1")
        self.assertEqual(meta.injection_dt, "2026-06-29 09:48:42")
        self.assertEqual(meta.dt_source, "cdf")

    def test_other_stamp_keys_and_formats(self) -> None:
        p = write_cdf(self.tmp / "d.CDF", stamp="24-Feb-2022 15:09:25",
                      stamp_key="injection_date")
        self.assertEqual(im.read_cdf_meta(p).injection_dt, "2022-02-24 15:09:25")
        p2 = write_cdf(self.tmp / "t.CDF", stamp="2022-02-24T15:09:25",
                       stamp_key="injection_time")
        self.assertEqual(im.read_cdf_meta(p2).injection_dt, "2022-02-24 15:09:25")

    def test_missing_injection_time_falls_back_to_mtime(self) -> None:
        mt = datetime(2023, 1, 26, 18, 38, 52, 123456).timestamp()
        p = write_cdf(self.tmp / "m.CDF", stamp=None, mtime=mt)
        meta = im.read_cdf_meta(p)
        self.assertEqual(meta.dt_source, "mtime")
        self.assertEqual(meta.injection_dt, "2023-01-26 18:38:52.123456")

    def test_unparsable_injection_time_falls_back_to_mtime(self) -> None:
        mt = datetime(2024, 4, 10, 16, 57, 21).timestamp()
        p = write_cdf(self.tmp / "u.CDF", stamp="not a date", mtime=mt)
        meta = im.read_cdf_meta(p)
        self.assertEqual(meta.dt_source, "mtime")
        self.assertEqual(meta.injection_dt, "2024-04-10 16:57:21")

    def test_agrees_with_distill_cdf_metadata(self) -> None:
        for i, kw in enumerate([
            dict(sample_name="D2887-12  Std", stamp="20220224150925+0000"),
            dict(sample_name="(Blank)", stamp="20230126183852", as_variables=True),
            dict(sample_name=None, stamp="20230126183852"),
            dict(sample_name="x", stamp=None, mtime=1700000000.5),
        ]):
            p = write_cdf(self.tmp / f"cmp{i}.CDF", **kw)
            name, dt = distill.cdf_metadata(p)
            meta = im.read_cdf_meta(p)
            self.assertEqual((meta.lab_id, meta.injection_dt),
                             (name, dt.isoformat(sep=" ")), kw)

    def test_meta_is_frozen(self) -> None:
        meta = im.read_cdf_meta(write_cdf(self.tmp / "f.CDF"))
        with self.assertRaises(Exception):
            meta.lab_id = "other"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
