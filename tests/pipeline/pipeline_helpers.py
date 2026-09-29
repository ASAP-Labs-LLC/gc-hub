"""Shared fixture (``hub``) for the pipeline tests (2A1 T2). Test modules
import it (``from pipeline_helpers import hub``): a ``conftest.py`` here would
take the module name ``conftest`` from ``tests/conftest.py``, which other
tests import.

A hub data folder in ``tmp_path`` with a migrated store, a global conf whose
calibration is the synthetic ladder with saved assignments (as the
Calibration page writes them), and the phase-1 corrections file (the golden
values). CDFs come from ``tests/cdf_fixtures.py``.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent.parent
for _p in (TESTS_DIR.parent, TESTS_DIR, TESTS_DIR / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cdf_fixtures as fx  # noqa: E402
import distill  # noqa: E402
import make_golden  # noqa: E402
import store  # noqa: E402

SIMDIS = "SIMDISB.M"


def file_corrections(conf):
    import corrections
    return corrections.FileProvider(conf.get("correction_factors_json", ""))


class Hub:
    """One test hub: ``data`` folder, ``db`` path, ``conf`` (global settings)."""

    def __init__(self, root: Path):
        self.root = root
        self.data = root / "data"
        self.data.mkdir()
        self.src = root / "src"
        self.src.mkdir()
        self.db = self.data / "gc.db"
        store.migrate(self.db)
        self.cal = fx.calibration_cdf(self.src / "CAL_09162026_090000.CDF", method_name=SIMDIS)
        self.corrections = self.src / "correction_factors.json"
        self.corrections.write_text(json.dumps(make_golden.correction_factors_doc()), encoding="utf-8")
        self.conf = {
            "calibration_cdf": str(self.cal),
            "calibration_assignments": distill.upsert_assignments(
                "", self.cal, make_golden.calibration_entries()),
            "calibration_sensitivity": "50",
            "correction_factors_json": str(self.corrections),
            "comparison_defaults_dir": str(root / "no-standards"),
            "bestfit_enabled": "false",
        }
        self._n = 0

    def cdf(self, kind="sample", *, name=None, injected=None, method_name=SIMDIS, **kw) -> Path:
        """Write a fixture CDF into ``src`` (unique file name) and return its path."""
        self._n += 1
        if kind == "blank":
            injected = injected or datetime(2026, 9, 24, 15, 30, 27)
            return fx.blank_cdf(self.src / f"b{self._n}.CDF", injected=injected,
                                name=name or "Blank", method_name=method_name)
        injected = injected or datetime(2026, 9, 25, 14, 23, 0)
        return fx.sample_cdf(self.src / f"s{self._n}.CDF", name=name or "40304",
                             injected=injected, method_name=method_name, **kw)

    def gc1(self, live_since=datetime(2020, 1, 1)) -> dict:
        """The gc1 row bootstrapped from ``conf`` (live since 2020: nothing is backfill)."""
        import instruments
        return instruments.bootstrap_gc1(self.conf, db=self.db, now=live_since)

    def gc2(self, **fields) -> dict:
        import instruments
        return store.instruments.upsert(dict({
            "id": "gc2", "name": "GC-2", "method_map": json.dumps(instruments.DEFAULT_METHOD_MAP),
            "live_since": datetime(2020, 1, 1)}, **fields), db=self.db)

    def submit(self, cdf, instrument="gc1", **kw):
        import pipeline
        return pipeline.submit(instrument, cdf, conf=self.conf, data_dir=self.data, db=self.db, **kw)

    def worker(self, **kw):
        import pipeline
        kw.setdefault("conf_fn", lambda: self.conf)
        # The phase-1 file, explicitly (the Worker's default is the hub's own
        # corrections, which these fixtures don't seed).
        kw.setdefault("corrections_provider", file_corrections)
        return pipeline.Worker(db=self.db, data_dir=self.data, **kw)

    def sample(self, sample_id) -> dict:
        return store.samples.get(sample_id, db=self.db)


@pytest.fixture()
def hub(tmp_path) -> Hub:
    distill._CAL_CACHE.clear()
    yield Hub(tmp_path)
    distill._CAL_CACHE.clear()
