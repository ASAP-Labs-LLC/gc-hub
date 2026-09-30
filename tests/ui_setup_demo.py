"""A hub data folder for the v3.1 pages (Instruments, one instrument, the
setup guide): ``hub_boot.build_hub`` plus setup progress, so the smoke test
and the screenshots show real states.

* ``gc1`` ("GC-1"): live, calibrated, corrections from the phase-1 file (the
  booted app seeds them), an installer downloaded and an agent that checked in
  (``agents.last_seen`` = now), a LEM machine uid.
* ``gc2`` ("GC-2"): created by Ryan C, correction factors saved, calibration
  set from its own run, installer downloaded, **no check-in yet**: step 4 of 8.
  One run is held (the ``held`` sample).

Not a test module; needs numpy, netCDF4, scipy (like hub_boot).
"""
from __future__ import annotations

import json
from pathlib import Path

import corrections
import distill
import ingest_api
import instrument_admin as ia
import make_golden
import store
from hub_boot import build_hub

RYAN = "Ryan C (10.0.0.5)"


def build(root: Path):
    hub = build_hub(root)
    db = hub.db
    share = Path(root) / "share"                     # stands in for the file LEM tails
    share.mkdir(exist_ok=True)
    store.instruments.upsert({"id": "gc1", "name": "GC-1", "lem_machine_uid": "gc1-agilent-7890b",
                              "export_path": str(share / "gc1_results.csv")}, db=db)
    store.instruments.upsert({"id": "gc2", "name": "GC-2", "live_since": None,
                              "lem_machine_uid": "gc2-agilent-8890"}, db=db)
    store.instrument_events.add(db, "gc2", "created", by=RYAN, detail={"name": "GC-2"})
    values = {c: float(v) for c, v in zip(corrections.D86_CUTS,
                                          (-11.4, 0, -4.9, 0, 0, -3.85, 0, 0, -3.1, 0, -5.02))}
    ia.save_corrections("gc2", values, "EQM study Sep 2026", by=RYAN, db=db)
    cal = hub.src / "CAL_09162026_090000.CDF"
    store.instruments.upsert({"id": "gc2", "calibration_cdf": str(cal),
                              "calibration_assignments": json.dumps(make_golden.calibration_entries())},
                             db=db)
    distill._CAL_CACHE.clear()
    store.instrument_events.add(db, "gc2", "calibration_saved", by=RYAN,
                                detail={"file": cal.name, "assigned": 13, "usable": True, "queued": 1})
    ingest_api.mint_token("gc2", db=db, by=RYAN)
    ingest_api.mint_token("gc1", db=db, by=RYAN)
    touch_agent(db, "gc1")
    return hub


def touch_agent(db, instrument_id: str = "gc1") -> None:
    """The agent checked in just now (the smoke test and screenshots call it
    right before looking, so it reads "Live")."""
    ingest_api.record_heartbeat(instrument_id, {
        "version": "2.4.1", "state": "running", "queue_size": 0, "rejected_count": 0,
        "host": "GC1-PC", "agent_time": None, "results_seq": 3}, db=db)
