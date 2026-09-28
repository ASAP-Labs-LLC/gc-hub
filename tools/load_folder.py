#!/usr/bin/env python3
"""Load CDFs from a folder into the hub (phase 2, 2A1 T6), from the command line.

The same job as the admin page's **Load CDFs from a folder**
(``jobs.load_folder.load_folder``): every ``*.CDF`` under FOLDER is submitted
to INSTRUMENT in injection-time order. The source folder is only read. A
re-run is safe (already-loaded files answer "duplicate").

    python tools/load_folder.py --data-dir C:\\ASAPApps\\gc\\data gc1 D:\\robocopy\\gc1
    python tools/load_folder.py --data-dir ... --backfill --process --json gc1 D:\\robocopy\\gc1

--backfill   every created sample is backfill (computed, never exported until
             released); injections before the instrument's live_since are
             backfill anyway.
--process    also run the pipeline worker here until the queue is empty (use it
             only when the hub isn't running; a running hub processes the queue
             itself).

Exit codes: 0 loaded (duplicates and conflicts are normal outcomes);
1 loaded, but some files were rejected or unreadable; 2 nothing loaded
(unknown or disabled instrument, missing folder or data folder, bad arguments).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("instrument", help="instrument id, e.g. gc1")
    ap.add_argument("folder", type=Path, help="folder to load (read only)")
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="the hub's data folder (default: GC_DATA_DIR)")
    ap.add_argument("--backfill", action="store_true",
                    help="load every file as backfill (never exported until released)")
    ap.add_argument("--process", action="store_true",
                    help="run the pipeline worker here until the queue is empty")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    data_dir = args.data_dir or (Path(os.environ["GC_DATA_DIR"]) if os.environ.get("GC_DATA_DIR") else None)
    if data_dir is None or not data_dir.is_dir():
        print(f"error: the hub's data folder is not set or missing: {data_dir}", file=sys.stderr)
        return 2
    os.environ["GC_DATA_DIR"] = str(data_dir)     # before settings/paths are imported

    import pipeline
    import store
    from jobs.load_folder import format_summary, load_folder

    db = data_dir / store.DB_FILENAME
    if not db.is_file():
        print(f"error: no hub database at {db} (start the hub once first)", file=sys.stderr)
        return 2
    try:
        summary = load_folder(args.instrument, args.folder, backfill=args.backfill, db=db,
                              data_dir=data_dir)
    except (pipeline.UnknownInstrument, pipeline.InstrumentDisabled, NotADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.process:
        worker = pipeline.Worker(db=db, data_dir=data_dir)
        summary["processed_jobs"] = worker.run_until_idle()

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(format_summary(summary))
        for item in summary["rejected_files"]:
            print(f"  rejected: {item['file']}: {item['reason']}")
        for item in summary["failed_files"]:
            print(f"  unreadable: {item['file']}: {item['reason']}")
        for item in summary["conflicts"]:
            print(f"  conflict {item['conflict_id']}: {item['file']} "
                  f"(same lab ID and time as sample {item['existing_sample_id']})")
        for item in summary["cross_instrument_files"]:
            print(f"  on {item['instrument_id']}: {item['file']}")
        if "processed_jobs" in summary:
            print(f"processed {summary['processed_jobs']} job(s)")
    return 1 if summary["rejected"] or summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
