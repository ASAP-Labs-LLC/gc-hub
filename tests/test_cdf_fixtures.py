"""The synthetic CDF fixtures read back through distill exactly as written."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import distill  # noqa: E402
import cdf_fixtures as fx  # noqa: E402


class WriteCdfTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trips_arrays_through_gc_xy_from_cdf(self) -> None:
        t = np.arange(0, 2.0, fx.DT_MIN)
        y = fx.gaussian(t, 1.0, 1000.0, 0.05) + 3.0
        path = fx.write_cdf(self.dir / "a.CDF", t, y, "S1", datetime(2026, 9, 1, 8, 30, 5))
        t_read, y_read = distill.gc_xy_from_cdf(path)
        np.testing.assert_allclose(t_read, t, rtol=0, atol=1e-9)
        np.testing.assert_array_equal(y_read, y)

    def test_metadata_round_trips(self) -> None:
        t = np.arange(0, 1.0, fx.DT_MIN)
        injected = datetime(2026, 9, 25, 14, 23, 0)
        path = fx.write_cdf(self.dir / "b.CDF", t, np.ones_like(t), "40304", injected)
        self.assertEqual(distill.cdf_metadata(path), ("40304", injected))

    def test_method_name_is_written_only_when_given(self) -> None:
        import netCDF4
        t = np.arange(0, 1.0, fx.DT_MIN)
        named = fx.write_cdf(self.dir / "m.CDF", t, np.ones_like(t), "S", datetime(2026, 9, 1, 15, 0),
                             method_name="SIMDISTB.M")
        plain = fx.write_cdf(self.dir / "p.CDF", t, np.ones_like(t), "S", datetime(2026, 9, 1, 15, 0))
        with netCDF4.Dataset(named) as ds:
            self.assertEqual(ds.detection_method_name, "SIMDISTB.M")
        with netCDF4.Dataset(plain) as ds:
            self.assertNotIn("detection_method_name", ds.ncattrs())

    def test_builders_are_deterministic(self) -> None:
        a = fx.sample_cdf(self.dir / "s1.CDF")
        b = fx.sample_cdf(self.dir / "s2.CDF")
        np.testing.assert_array_equal(distill.gc_xy_from_cdf(a)[1], distill.gc_xy_from_cdf(b)[1])
        for build in (fx.calibration_cdf, fx.blank_cdf):
            p = build(self.dir / f"{build.__name__}.CDF")
            t, y = distill.gc_xy_from_cdf(p)
            self.assertEqual(t.size, y.size)
            self.assertGreater(t.size, 1000)


if __name__ == "__main__":
    unittest.main()
