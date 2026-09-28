"""A hub data folder with real samples, for booting app.py against (T4).

``build_hub(root)`` creates ``root/data`` the way the hub runs it: the store
is created and ``gc1`` bootstrapped by ``instruments.startup`` (its Worker is
stopped at once and the jobs run here, in the test, with
``run_until_idle``), CDFs go in through ``pipeline.submit``, and
``settings.json`` holds the calibration, the corrections file and a
comparison-standards folder. ``bootapp.booted(root)`` then launches app.py
with ``GC_DATA_DIR=root/data``.

The samples (``Hub.ids``):

* ``blank``: a genuine blank (gc1), final;
* ``final``: ``40304`` on gc1, injected after ``live_since``: final and gated,
  blank-subtracted with ``blank``;
* ``rerun``: a second injection of ``40304`` (final, gated);
* ``backfill``: ``40299``, injected before ``live_since``: final, not released,
  so it fails the gate;
* ``other``: a ``D7096.M`` run: ``other_method`` (no revision);
* ``held``: ``50001`` on ``gc2``, which has no calibration:
  ``awaiting_calibration`` with its hold reason in ``samples.error``.

Not a test module (no ``test_`` prefix); needs numpy, netCDF4, scipy.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
for _p in (TESTS_DIR.parent, TESTS_DIR, TESTS_DIR / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cdf_fixtures as fx  # noqa: E402
import distill  # noqa: E402
import instruments  # noqa: E402
import make_golden  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402

SIMDIS = "SIMDISB.M"
LIVE_SINCE = datetime(2026, 9, 22, 0, 0, 0)


class Hub:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.data = self.root / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.src = self.root / "src"
        self.src.mkdir(exist_ok=True)
        self.db = self.data / store.DB_FILENAME
        self.standards = self.data / "standards"
        self.standards.mkdir(exist_ok=True)
        self.cal = fx.calibration_cdf(self.src / "CAL_09162026_090000.CDF", method_name=SIMDIS)
        corr = self.src / "correction_factors.json"
        corr.write_text(json.dumps(make_golden.correction_factors_doc()), encoding="utf-8")
        self.conf = {
            "calibration_cdf": str(self.cal),
            "calibration_assignments": distill.upsert_assignments(
                "", self.cal, make_golden.calibration_entries()),
            "calibration_sensitivity": "50",
            "correction_factors_json": str(corr),
            "comparison_defaults_dir": str(self.standards),
            "export_folder": str(self.data / "exports"),
            "bestfit_enabled": "false",
        }
        (self.data / "settings.json").write_text(json.dumps(self.conf, indent=1), encoding="utf-8")
        self.ids: dict[str, int] = {}
        self._n = 0

    def _cdf(self, kind: str, **kw) -> Path:
        self._n += 1
        path = self.src / f"{kind}{self._n}.CDF"
        if kind == "blank":
            return fx.blank_cdf(path, **kw)
        return fx.sample_cdf(path, **kw)

    def submit(self, key: str, cdf: Path, instrument: str = "gc1") -> int:
        res = pipeline.submit(instrument, cdf, conf=self.conf, data_dir=self.data, db=self.db)
        assert res.outcome == "created", res
        self.ids[key] = res.sample_id
        return res.sample_id

    def worker(self):
        return pipeline.Worker(db=self.db, data_dir=self.data, conf_fn=lambda: self.conf)

    def sample(self, key: str) -> dict:
        return store.samples.get(self.ids[key], db=self.db)


def build_hub(root: Path) -> Hub:
    distill._CAL_CACHE.clear()
    hub = Hub(root)
    w = instruments.startup(hub.conf, None, db=hub.db, data_dir=hub.data,
                            conf_fn=lambda: hub.conf, poll_seconds=60)
    w.stop()
    store.instruments.upsert({"id": "gc1", "live_since": LIVE_SINCE}, db=hub.db)
    store.instruments.upsert({"id": "gc2", "name": "GC-2",
                              "method_map": json.dumps(instruments.DEFAULT_METHOD_MAP),
                              "live_since": LIVE_SINCE}, db=hub.db)

    hub.submit("blank", hub._cdf("blank", injected=datetime(2026, 9, 24, 15, 30, 27),
                                 method_name=SIMDIS))
    hub.submit("final", hub._cdf("sample", name="40304", injected=datetime(2026, 9, 25, 14, 23, 0),
                                 method_name=SIMDIS))
    hub.submit("rerun", hub._cdf("sample", name="40304", injected=datetime(2026, 9, 25, 16, 45, 10),
                                 shift=0.01, method_name=SIMDIS))
    hub.submit("backfill", hub._cdf("sample", name="40299", injected=datetime(2026, 9, 20, 13, 30, 0),
                                    shift=0.02, method_name=SIMDIS))
    hub.submit("other", hub._cdf("sample", name="G7096", injected=datetime(2026, 9, 25, 17, 40, 0),
                                 shift=0.03, method_name="D7096.M"))
    hub.submit("held", hub._cdf("sample", name="50001", injected=datetime(2026, 9, 25, 18, 50, 0),
                                   shift=0.04, method_name=SIMDIS), instrument="gc2")
    hub.worker().run_until_idle()

    # A comparison standard for the Analysis routes: a copy of a sample run.
    shutil.copy2(hub.src / "sample2.CDF", hub.standards / "Diesel.CDF")
    return hub
