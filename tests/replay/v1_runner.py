#!/usr/bin/env python3
"""Run the v1 production code (the share copy, ``webapp-live``) over a replay
job, in its own process so its ``distill``/``fuel_fit``/``looker`` never
meet the hub's modules of the same names.

    python v1_runner.py job.json out.json

``job.json``::

    {"v1_dir": ".../webapp-live", "work": "<scratch dir>",
     "conf": {...v1 settings...},
     "files": [{"id": "...", "path": "..."}, ...],      # arrival order
     "direct": [{"id": "...", "path": "...", "blank": "<path>|null"}, ...]}

Two passes, each in a fresh scratch folder:

* ``looker``: every file in arrival order through v1's real ingestion,
  ``looker.Looker._handle_new`` (copy, ``cdf_metadata``, v1's blank choice
  and registration, ``distill.process_cdf``). One file at a time, so the
  row a file produced is the row the results CSV gained.
* ``direct``: ``distill.process_cdf(copy, blank_path=<blank>, reprocess=True)``
  with the blank the hub chose for that sample, so the hub and v1 are fed
  exactly the same inputs.

v1 reads its settings through ``settings.load_settings()``; a stand-in module
returning ``conf`` replaces the share's ``settings.py`` (which would read the
developer's ``~/.gc_viewer_settings.json``).

``out.json``: ``{"v1_distill": <path of the distill module imported>,
"looker": [{"id", "row" (dict of CSV strings) | null, "errors": [...],
"blank": "<id of the reference blank v1 used>" | null}], "direct": [{"id",
"row" | null, "errors": [...]}]}``.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import sys
import types
from pathlib import Path


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record):
        text = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            text += f": {type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
        self.records.append(f"{record.levelname} {text}")


def _rows(csv_path: Path) -> list[dict]:
    if not csv_path.is_file():
        return []
    with csv_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        return [dict(zip(header, r)) for r in reader]


def _size(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def _appended(path: Path, before: int) -> str:
    """The text appended to the results CSV since it was ``before`` bytes
    (the header too, for the first row)."""
    if not path.is_file():
        return ""
    with path.open("rb") as fh:
        fh.seek(before)
        text = fh.read().decode("utf-8")
    if before == 0:
        text = text.split("\r\n", 1)[1] if "\r\n" in text else ""
    return text


def main(job_path: str, out_path: str) -> int:
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    v1_dir = Path(job["v1_dir"]).resolve()
    work = Path(job["work"])
    os.environ.pop("GC_CAL_CDF", None)

    # v1 first on the path; nothing of the hub's.
    here = str(Path(__file__).resolve().parent)
    sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != Path(here)]
    sys.path.insert(0, str(v1_dir))

    conf: dict = {}
    fake = types.ModuleType("settings")
    fake.CONFIG_PATH = None
    fake.load_settings = lambda: dict(conf)
    sys.modules["settings"] = fake

    import distill   # noqa: E402  (v1's)
    import looker    # noqa: E402  (v1's)
    import fuel_fit  # noqa: E402  (v1's)
    for mod in (distill, looker, fuel_fit):
        if Path(mod.__file__).resolve().parent != v1_dir:
            raise SystemExit(f"imported {mod.__name__} from {mod.__file__}, not {v1_dir}")

    collect = _Collect()
    logging.getLogger().addHandler(collect)
    logging.getLogger().setLevel(logging.WARNING)

    def use_conf(**paths):
        conf.clear()
        conf.update(job["conf"])
        conf.update({k: str(v) for k, v in paths.items()})
        distill._SETTINGS_CACHE = None
        distill._SETTINGS_MTIME = None

    out: dict = {"v1_distill": str(Path(distill.__file__).resolve()), "looker": [], "direct": []}

    # ── pass 1: v1's own ingestion (Looker) ──────────────────────────────
    lw = work / "looker"
    watch, processed = lw / "watch", lw / "processed"
    watch.mkdir(parents=True)
    processed.mkdir(parents=True)
    results_csv = lw / "distill_results.csv"
    use_conf(distill_output=results_csv, processed_cdf_dir=processed)
    lk = looker.Looker(watch, processed, max_workers=1)
    blank_ids: dict[str, str] = {}          # processed path -> input id
    for i, item in enumerate(job["files"]):
        src = watch / f"{i:03d}" / Path(item["path"]).name     # the sender's own file name
        src.parent.mkdir()
        shutil.copy2(item["path"], src)
        before = len(_rows(results_csv))
        before_size = _size(results_csv)
        prev_blank = lk._latest_blank_path
        collect.records.clear()
        try:
            sample, _date = distill.cdf_metadata(src)
            used = None if lk._is_blank_sample(sample) else prev_blank
        except Exception:  # noqa: BLE001 - _handle_new logs it
            used = None
        try:
            lk._handle_new(src)
        except Exception as exc:  # noqa: BLE001
            collect.records.append(f"RAISED {type(exc).__name__}: {exc}")
        if lk._latest_blank_path is not None and lk._latest_blank_path != prev_blank:
            blank_ids[str(lk._latest_blank_path)] = item["id"]
        rows = _rows(results_csv)
        new = rows[before:]
        out["looker"].append({
            "id": item["id"],
            "row": new[-1] if new else None,
            "line": _appended(results_csv, before_size) if new else None,
            "rows_added": len(new),
            "errors": list(collect.records),
            "blank": blank_ids.get(str(used)) if used is not None else None,
        })
    lk.stop()

    # ── pass 2: the hub's blank choice, process_cdf directly ─────────────
    dw = work / "direct"
    din, dproc = dw / "in", dw / "processed"
    din.mkdir(parents=True)
    dproc.mkdir(parents=True)
    direct_csv = dw / "distill_results.csv"
    use_conf(distill_output=direct_csv, processed_cdf_dir=dproc)
    for i, item in enumerate(job["direct"]):
        src = din / f"{i:03d}" / Path(item["path"]).name
        src.parent.mkdir()
        shutil.copy2(item["path"], src)
        before = len(_rows(direct_csv))
        before_size = _size(direct_csv)
        collect.records.clear()
        try:
            distill.process_cdf(src, blank_path=Path(item["blank"]) if item.get("blank") else None,
                                reprocess=True)
        except Exception as exc:  # noqa: BLE001
            collect.records.append(f"RAISED {type(exc).__name__}: {exc}")
        new = _rows(direct_csv)[before:]
        out["direct"].append({"id": item["id"], "row": new[-1] if new else None,
                              "line": _appended(direct_csv, before_size) if new else None,
                              "rows_added": len(new), "errors": list(collect.records)})

    Path(out_path).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
