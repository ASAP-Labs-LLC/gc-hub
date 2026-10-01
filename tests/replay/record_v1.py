#!/usr/bin/env python3
"""Record v1's side of the synthetic replay for CI (``v1_recorded_synthetic.json``).

CI has no share snapshot, so it cannot run the v1 code; it replays the hub
against what v1 produced for the same synthetic CDFs, recorded here. Needs
the snapshot (``GC_SNAPSHOT_DIR`` or ``gc-share-snapshot-2026-09-25`` beside
the checkout). From the repo root::

    .venv/bin/python tests/replay/record_v1.py

It refuses to write a recording while the live replay shows an unexplained
difference. ``test_replay_v1.py`` checks, whenever the snapshot is present,
that the recording still equals what v1 produces.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

for _name in ("PORT", "GC_PORT", "GC_DATA_DIR", "GC_CAL_CDF"):
    os.environ.pop(_name, None)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_harness as rh  # noqa: E402

RECORDING = rh.REPLAY_DIR / "v1_recorded_synthetic.json"


def main() -> int:
    if not rh.snapshot_available():
        print("the share snapshot is not available; nothing recorded", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="gc-replay-record-") as tmp:
        work = Path(tmp)
        findings, raw = rh.run_suite(rh.synthetic_suite(work), work)
        print(rh.report(findings))
        if rh.unexplained(findings):
            print("unexplained differences: not recorded", file=sys.stderr)
            return 1
        RECORDING.write_text(json.dumps(rh.record(raw), separators=(",", ":"), sort_keys=True)
                             + "\n", encoding="utf-8")
    print(f"wrote {RECORDING} ({RECORDING.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
