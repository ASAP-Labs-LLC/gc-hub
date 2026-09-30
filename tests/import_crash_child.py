"""Run a real v1 history import and SIGKILL this process part-way through a
batch (tests/test_import_interrupted.py). Not a test module.

usage: import_crash_child.py <root> <kill_at_item>
``<root>/case.json`` holds {data, processed, csv, conf, aliases, batch_size}.
The process kills itself inside the write transaction of the batch that holds
item ``kill_at_item`` (1-based), after the items before it in that batch were
applied (their files already placed), as a power cut would.
"""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import jobs.import_history as ih  # noqa: E402
import store  # noqa: E402


def main() -> None:
    root, kill_at = Path(sys.argv[1]), int(sys.argv[2])
    case = json.loads((root / "case.json").read_text(encoding="utf-8"))
    data = Path(case["data"])
    orig = ih._apply_one
    count = {"n": 0}

    def apply(conn, ctx, item, placed):
        count["n"] += 1
        if count["n"] == kill_at:
            print("killed at item", kill_at, "placed", [str(p) for p in placed], flush=True)
            os.kill(os.getpid(), signal.SIGKILL)
        return orig(conn, ctx, item, placed)

    ih._apply_one = apply
    ih.import_history("gc1", case["processed"], case["csv"],
                      instrument_folder_aliases=case["aliases"], db=data / store.DB_FILENAME,
                      data_dir=data, conf=case["conf"], batch_size=case["batch_size"],
                      by="Test Operator (127.0.0.1)")
    print("finished without being killed", flush=True)


if __name__ == "__main__":
    main()
