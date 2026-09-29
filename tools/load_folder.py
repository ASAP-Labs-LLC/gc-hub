#!/usr/bin/env python3
"""Load CDFs from a folder into the hub (phase 2, 2A1 T6), from the command line.

The same job as the admin page's **Load CDFs from a folder**
(``jobs.load_folder.load_folder``): every ``*.CDF`` under FOLDER is submitted
to INSTRUMENT in injection-time order. The source folder is only read. A
re-run is safe (already-loaded files answer "duplicate").

    python tools/load_folder.py --data-dir C:\\ASAPApps\\gc\\data gc1 D:\\robocopy\\gc1
    python tools/load_folder.py --data-dir ... --backfill --process --json gc1 D:\\robocopy\\gc1

Before loading it does what the hub's start-up does, without starting a
worker: migrates the store and, for ``gc1``, creates the instrument from
``settings.json`` if it doesn't exist yet (``instruments.bootstrap_gc1``).

The parity run (DEPLOY.md, "Parity check before cutover") uses a SCRATCH
data folder, never production:

    python tools/load_folder.py --data-dir C:\\ASAPApps\\gc\\parity-gc1 ^
        --copy-instrument-from C:\\ASAPApps\\gc\\data --process --json gc1 C:\\parity\\gc1 > load.json

--copy-instrument-from DATA
             copy INSTRUMENT from the production data folder DATA (its gc.db
             is opened read-only): the row (name, live_since, method map),
             its calibration CDF and assignments, its hub correction factors,
             plus settings.json and the comparison standards the scratch
             folder lacks (jobs.load_folder.copy_instrument). Creates the
             instrument if the scratch store lacks it (GC-2) and the scratch
             folder itself if it doesn't exist. Refused on a folder a hub has
             served (it has app.log).
--backfill   every created sample is backfill (computed, never exported until
             released); injections before the instrument's live_since are
             backfill anyway.
--process    also run the pipeline worker here until the queue is empty.
             Only on a folder no hub is processing: it holds
             <data>/.gc-load-folder.lock (refused if another loader holds
             it; under the lock it requeues interrupted jobs), and it refuses
             a folder a hub has served (app.log in it) unless --hub-port
             names the port of that folder's hub and nothing answers there
             (the hub is stopped). A running hub on some other folder (the
             live hub on this machine) doesn't matter.
--hub-port   the port of the hub that serves --data-dir, checked before
             --process (default: none; a scratch folder has no hub).

Exit codes: 0 loaded (duplicates and conflicts are normal outcomes);
1 loaded, but some files were rejected or unreadable; 2 nothing loaded
(unknown or disabled instrument, missing folder or data folder, a running
hub or loader, a refused instrument copy, bad arguments); 3 the load stopped partway (the summary of
what was done is printed; re-run to resume).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _port_answers(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _print_summary(summary: dict, as_json: bool, format_summary) -> None:
    if as_json:
        print(json.dumps(summary, indent=2))
        return
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
    if "copied_instrument" in summary:
        c = summary["copied_instrument"]
        print(f"copied {c['instrument']} from {c['source']}: calibration {c['calibration_cdf']} "
              f"({c['calibration_assignments']} assignment(s)), {c['corrections']} correction "
              f"factor(s), settings.json {'copied' if c['settings_copied'] else 'kept'}, "
              f"{c['standards_copied']} standard(s) copied")
    if "processed_jobs" in summary:
        print(f"processed {summary['processed_jobs']} job(s)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("instrument", help="instrument id, e.g. gc1")
    ap.add_argument("folder", type=Path, help="folder to load (read only)")
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="the hub's data folder (default: GC_DATA_DIR)")
    ap.add_argument("--backfill", action="store_true",
                    help="load every file as backfill (never exported until released)")
    ap.add_argument("--process", action="store_true",
                    help="run the pipeline worker here until the queue is empty (hub not running)")
    ap.add_argument("--hub-port", type=int, default=None,
                    help="the port of the hub serving --data-dir, checked before --process "
                         "(default: none; a folder a hub has served needs it)")
    ap.add_argument("--copy-instrument-from", type=Path, default=None, metavar="DATA",
                    help="production data folder to copy INSTRUMENT's calibration, method map "
                         "and corrections from (read-only), into a scratch --data-dir")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    data_dir = args.data_dir or (Path(os.environ["GC_DATA_DIR"]) if os.environ.get("GC_DATA_DIR") else None)
    copy_from = args.copy_instrument_from
    if copy_from is not None:
        if data_dir is None:
            print("error: --copy-instrument-from needs --data-dir (a scratch folder)", file=sys.stderr)
            return 2
        if not (copy_from / "gc.db").is_file():
            print(f"error: no hub store (gc.db) in {copy_from}", file=sys.stderr)
            return 2
        if data_dir.exists() and data_dir.resolve() == copy_from.resolve():
            print("error: --copy-instrument-from is the --data-dir itself; the parity run needs "
                  "a separate scratch folder", file=sys.stderr)
            return 2
        if (data_dir / "app.log").exists():
            print(f"error: a hub has served {data_dir} (it has app.log); copy an instrument only "
                  f"into a fresh scratch folder", file=sys.stderr)
            return 2
        data_dir.mkdir(parents=True, exist_ok=True)
    if data_dir is None or not data_dir.is_dir():
        print(f"error: the hub's data folder is not set or missing: {data_dir}", file=sys.stderr)
        return 2
    os.environ["GC_DATA_DIR"] = str(data_dir)     # before settings/paths are read

    import instruments
    import pipeline
    import settings
    import store
    from jobs.load_folder import (CopyInstrumentError, ProcessLocked, copy_instrument,
                                  format_summary, load_folder, process_lock)

    if args.process:
        if args.hub_port is not None:
            if _port_answers(args.hub_port):
                print(f"error: something answers on port {args.hub_port}: the hub looks running, "
                      f"and it processes the queue itself. Run without --process.", file=sys.stderr)
                return 2
        elif (data_dir / "app.log").exists():
            print(f"error: a hub has served {data_dir} (it has app.log), and a running hub "
                  f"processes the queue itself. Run without --process, or stop that hub and "
                  f"name its port with --hub-port. The parity run uses a scratch folder "
                  f"(DEPLOY.md).", file=sys.stderr)
            return 2
    if not args.folder.is_dir():
        print(f"error: not a folder: {args.folder}", file=sys.stderr)
        return 2

    db = data_dir / store.DB_FILENAME
    with contextlib.ExitStack() as stack:
        if args.process:
            try:
                stack.enter_context(process_lock(data_dir))
            except ProcessLocked as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        store.migrate(db)
        copied = None
        if copy_from is not None:
            try:
                copied = copy_instrument(args.instrument, copy_from, data_dir=data_dir, db=db)
            except CopyInstrumentError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        conf = settings.load_settings()
        if args.instrument == instruments.GC1:
            instruments.bootstrap_gc1(conf, db=db)
            import hub
            hub.seed_gc1_corrections(conf, db)     # as the hub's start-up does
        try:
            summary = load_folder(args.instrument, args.folder, backfill=args.backfill, db=db,
                                  data_dir=data_dir, conf=conf)
        except (pipeline.UnknownInstrument, pipeline.InstrumentDisabled, NotADirectoryError) as exc:
            partial = getattr(exc, "load_summary", None)
            if partial is not None and (partial["created"] or partial["duplicate"]):
                _print_summary(partial, args.json, format_summary)
                print(f"stopped: {exc}", file=sys.stderr)
                return 3
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except BaseException as exc:  # noqa: BLE001 - report what was done, then exit
            partial = getattr(exc, "load_summary", None)
            if partial is None:
                raise
            _print_summary(partial, args.json, format_summary)
            print(f"stopped: the load stopped partway ({partial['stopped']}); re-run to resume",
                  file=sys.stderr)
            return 3
        if copied is not None:
            summary["copied_instrument"] = copied
        if args.process:
            summary["requeued"] = pipeline.requeue_on_start(db=db)
            worker = pipeline.Worker(db=db, data_dir=data_dir)
            summary["processed_jobs"] = worker.run_until_idle()

    _print_summary(summary, args.json, format_summary)
    return 1 if summary["rejected"] or summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
