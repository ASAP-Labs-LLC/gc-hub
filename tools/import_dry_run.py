#!/usr/bin/env python3
"""Read-only dry run of the phase-2 history import for one instrument.

Reads every CDF's metadata under --processed-dir and (optionally) the v1
results CSV, matches them with ``import_match.match`` and prints what the
hub's importer would do: attached samples, revisions, orphans, result-only
rows, identical-bytes duplicates, CDFs without an injection time, and rows
that belong to another instrument's folder. Nothing under the source folder
or the CSV is ever written.

    python tools/import_dry_run.py \\
        --processed-dir D:\\import\\gc1\\processed_cdf \\
        --results-csv   D:\\import\\gc1\\distill_results.csv \\
        --instrument-folder "\\\\asapserver\\Labsharedrive\\Ryan C\\GC Data\\GC2025\\GC2025.1\\processed_cdf" \\
        --json D:\\import\\gc1-dry-run.json

--instrument-folder is the folder the CSV's ``Source File`` column names for
this instrument (the original share path, not the local robocopy).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import import_match  # noqa: E402


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _format_index(summary: dict, path: Path) -> str:
    lines = [f"v1 processed index: {path}"]
    for key in ("entries", "distinct_lab_ids", "date_min", "date_max", "exact_duplicates",
                "duplicates_after_strip", "canonical_seconds", "canonical_microseconds",
                "non_canonical", "names_with_outer_whitespace", "empty_names",
                "lab_ids_with_several_times"):
        lines.append(f"  {key + ':':30s}{summary[key]}")
    lines.append(f"  {'blank_like_names:':30s}{summary['blank_like_names']}")
    if summary["canonical_microseconds_examples"]:
        lines.append(f"  microsecond (mtime) examples:  {summary['canonical_microseconds_examples']}")
    if summary["non_canonical_examples"]:
        lines.append(f"  non-canonical examples:        {summary['non_canonical_examples']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    ap.add_argument("--processed-dir", required=True, type=Path,
                    help="folder of processed CDFs (searched recursively)")
    ap.add_argument("--results-csv", type=Path, default=None,
                    help="v1 distill_results.csv; omit for CDF-only mode")
    ap.add_argument("--instrument-folder", required=True,
                    help="this instrument's folder as the CSV's Source File names it")
    ap.add_argument("--json", type=Path, default=None, help="also write the full report here")
    ap.add_argument("--examples", type=int, default=10,
                    help="examples printed per problem class (default 10)")
    ap.add_argument("--processed-index", type=Path, default=None,
                    help="also profile a v1 .processed_index.json")
    args = ap.parse_args(argv)

    if not args.processed_dir.is_dir():
        ap.error(f"--processed-dir is not a folder: {args.processed_dir}")
    if args.results_csv is not None and not args.results_csv.is_file():
        ap.error(f"--results-csv is not a file: {args.results_csv}")
    if args.processed_index is not None and not args.processed_index.is_file():
        ap.error(f"--processed-index is not a file: {args.processed_index}")
    if args.json is not None:
        if _inside(args.json, args.processed_dir):
            ap.error("--json must not be inside --processed-dir (the source is read-only)")
        for src in (args.results_csv, args.processed_index):
            if src is not None and args.json.resolve() == src.resolve():
                ap.error(f"--json would overwrite a source file: {src}")

    def progress(done: int, total: int) -> None:
        print(f"  read {done}/{total} CDFs", file=sys.stderr, flush=True)

    report = import_match.dry_run(args.processed_dir, args.results_csv,
                                  instrument_folder=args.instrument_folder,
                                  on_progress=progress)
    sys.stdout.write(import_match.format_summary(report, examples=args.examples))

    index_summary = None
    if args.processed_index is not None:
        index_summary = import_match.summarize_processed_index(args.processed_index)
        sys.stdout.write("\n" + _format_index(index_summary, args.processed_index))

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {"report": import_match.report_to_dict(report),
                   "processed_index": index_summary}
        args.json.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"\nFull report written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
