"""Shared helpers for the 2A2 tests (not a conftest: see
tests/pipeline/pipeline_helpers.py for why). ``hub`` is the pipeline tests'
fixture: a data folder with a migrated store and the synthetic calibration."""
from __future__ import annotations

import json
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent
for _p in (TESTS.parent, TESTS, TESTS / "pipeline", TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from pipeline_helpers import SIMDIS, Hub, hub  # noqa: E402,F401

import corrections  # noqa: E402
import store  # noqa: E402

ELEVEN = {cut: float(i) / 10 for i, cut in enumerate(corrections.D86_CUTS)}


def cal_entries(hub) -> list:
    """The synthetic calibration's saved entries (a plain list)."""
    amap = json.loads(hub.conf["calibration_assignments"])
    return next(iter(amap.values()))


def set_corrections(hub, inst, values=ELEVEN, by="t"):
    with store.connection(hub.db) as conn:
        with store.write_txn(conn):
            store.corrections.set_all(conn, inst, values, by=by, reason="test")
