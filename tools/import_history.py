#!/usr/bin/env python3
"""Dry run of the phase-2 history import (plan 2D) for one instrument.

Prints what ``jobs.import_history.import_history`` would do with a v1
processed folder and results CSV: samples imported (attached, orphan,
result-only), revisions, conflicts, cross-instrument files, truncated CDFs,
rows not imported, whitespace-only sample names, v1's time misparse and the
method names. **Read-only:** nothing under the source folder, the CSV or the
hub's data folder is written.

    python tools/import_history.py --dry-run --instrument gc2 \\
        --processed-dir D:\\import\\gc2\\processed_cdfs2 \\
        --results-csv   D:\\import\\gc2\\distill_results.csv \\
        --alias processed_cdfs2 [--db C:\\ASAPApps\\gc\\data\\gc.db] [--json out.json]

``--db`` checks against the hub's store (opened read-only: what is already
imported, conflicts, other instruments, the instrument's method map);
without it an empty store and the default method map are assumed.

**The real import is not run from here.** It runs inside the hub (the admin
import page), because the hub is the store's single writer; this command
refuses without ``--dry-run`` (exit 2).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="required: this command only does dry runs")
    ap.add_argument("--instrument", required=True, help="instrument id, e.g. gc1")
    ap.add_argument("--processed-dir", required=True, help="the v1 processed CDF folder (a copy)")
    ap.add_argument("--results-csv", help="the v1 results CSV (omit for CDFs only)")
    ap.add_argument("--alias", action="append", default=[], metavar="FOLDER",
                    help="the folder the CSV's Source File names for this instrument "
                         "(full share path or bare folder name); repeatable")
    ap.add_argument("--db", help="the hub's gc.db, opened read-only")
    ap.add_argument("--json", help="write the summary as JSON here")
    ap.add_argument("--examples", type=int, default=5, help="examples per class in the text")
    args = ap.parse_args(argv)

    if not args.dry_run:
        print("The real history import runs inside the hub (admin import page), the store's "
              "single writer. This command only does --dry-run.", file=sys.stderr)
        return 2
    processed = Path(args.processed_dir)
    if not processed.is_dir():
        print(f"not a folder: {processed}", file=sys.stderr)
        return 2
    if args.results_csv and not Path(args.results_csv).is_file():
        print(f"no results CSV at {args.results_csv}", file=sys.stderr)
        return 2
    if args.json:
        out = Path(args.json).resolve()
        src = processed.resolve()
        if (out == src or src in out.parents
                or (args.results_csv and out == Path(args.results_csv).resolve())):
            print("--json must not point into the source folder or at the CSV", file=sys.stderr)
            return 2
    if not args.alias:
        print("warning: no --alias given; rows are not checked for another instrument's folder",
              file=sys.stderr)

    import store
    from jobs.import_history import format_summary, import_history

    conn = None
    if args.db:
        if not Path(args.db).is_file():
            print(f"no database at {args.db}", file=sys.stderr)
            return 2
        conn = store.open_db(args.db, readonly=True)
    try:
        summary = import_history(args.instrument, processed, args.results_csv,
                                 instrument_folder_aliases=args.alias, db=conn, data_dir=None,
                                 dry_run=True, conf={})
    finally:
        if conn is not None:
            conn.close()
    print(format_summary(summary, examples=args.examples), end="")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"JSON summary written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
