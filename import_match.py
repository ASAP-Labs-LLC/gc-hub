"""History-import matcher (phase 2, plan 2D; contract §3).

Pairs the v1 results CSV with the processed CDFs of **one** instrument, by
each CDF's own metadata (sample name + injection time), never by filename.

Pure: no database, no Flask, no settings. It only reads files and never
writes to the source folder or the CSV. The hub's importer job (Lane A)
turns a ``MatchReport`` into samples and revisions; ``tools/import_dry_run.py``
prints it.

Identity rule (spec "History import"): a CSV row attaches to a CDF only when
``row.lab_id == cdf.lab_id.strip()`` **and** ``row.injection_dt_raw ==
cdf.injection_dt``, where ``injection_dt`` is canonical
``datetime.isoformat(sep=" ")``, exactly as v1's ``distill.process_cdf``
wrote it. ``Source File`` values are used only to spot rows that belong to
another instrument's folder, never to attach a row.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from netCDF4 import Dataset

import distill

_HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class CdfMeta:
    path: str
    sha256: str
    lab_id: str               # sample name verbatim (display); compare via normalise_lab_id
    injection_dt: str         # the CORRECT time, canonical naive isoformat(sep=" ")
    dt_source: str            # 'cdf' | 'mtime' (how injection_dt was obtained)
    method_name: str = ""     # detection_method_name: trimmed, basename, upper-cased; '' if absent
    legacy_injection_dt: str = ""   # what v1 wrote on Python >= 3.11 ('' = same as injection_dt)
    raw_stamp: str = ""       # the CDF's raw injection stamp text
    v1_injection_dts: tuple = ()    # distinct strings v1 may have written: (>=3.11 form, <3.11 form)


def normalise_lab_id(s: str) -> str:
    """Identity form of a lab ID: outer whitespace stripped, nothing else
    (inner spaces and case are significant). Keep the original for display."""
    return (s or "").strip()


def _v1_strings(c: CdfMeta) -> tuple:
    """Every InjectionDateTime string v1 could have written for ``c``."""
    out: list[str] = []
    for s in (*c.v1_injection_dts, c.legacy_injection_dt or c.injection_dt, c.injection_dt):
        if s and s not in out:
            out.append(s)
    return tuple(out)


# ── Injection time parsing ─────────────────────────────────────────────────
# v1 (distill.parse_injection_datetime as of phase 1) tried fromisoformat
# FIRST. On Python >= 3.11 fromisoformat accepts some compact ANDI stamps
# ("20260925002450+0000") and misreads them: 02:45:00 instead of 00:24:50.
# The hub (2A1) fixes distill; the import still has to reproduce the bug to
# find v1's CSV rows, so a bug-for-bug copy is pinned here.
_V1_COMPACT = re.compile(r"^(\d{14})(?:\s*[+-]\d{2}:?\d{2})?$")
_CORRECT_COMPACT = re.compile(r"^(\d{14})(?:Z|\s*[+-]\d{2}:?\d{2})?$")
_ISO_DATE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")
_OTHER_FORMATS = ("%d-%b-%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")


def _v1_parse(raw: str) -> datetime | None:
    """Bug-for-bug copy of v1's ``distill.parse_injection_datetime``. Do not
    fix: it must return what v1 returned, on the running Python (>= 3.11 is
    what the hub runs, and what reproduces v1's CSV)."""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        pass
    m = _V1_COMPACT.match(text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    for fmt in _OTHER_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _correct_parse(raw: str) -> datetime | None:
    """The fixed parse: the ANDI compact stamp (``YYYYMMDDHHMMSS`` with an
    optional ``Z`` or ``±HH[:]MM`` zone, dropped) is matched explicitly
    before anything else; ISO is tried only when the text has ``-`` or ``/``
    date separators. Naive wall-clock result, or None."""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    m = _CORRECT_COMPACT.match(text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            return None
    if _ISO_DATE.match(text):
        try:
            return datetime.fromisoformat(text.replace("/", "-")).replace(tzinfo=None)
        except ValueError:
            pass
    for fmt in _OTHER_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _method_basename(raw: str) -> str:
    text = (raw or "").strip()
    return re.split(r"[\\/]", text)[-1].strip().upper() if text else ""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_cdf_names(path: Path) -> tuple[str, str, str, str]:
    """(sample_name as distill reads it, how the name was found, raw stamp,
    raw detection_method_name).

    Mirrors ``distill.cdf_metadata`` exactly (variables before global
    attributes; ``injection_date_time_stamp`` → ``injection_date`` →
    ``injection_time``) but reads no chromatogram arrays."""
    with distill._NETCDF_LOCK:
        with Dataset(path) as ds:
            vars_lc = {n.lower(): n for n in ds.variables}
            attrs_lc = {n.lower(): n for n in ds.ncattrs()}

            def _get(key: str) -> str:
                k = key.lower()
                if k in vars_lc:
                    return distill._read_text(ds.variables[vars_lc[k]])
                if k in attrs_lc:
                    return str(getattr(ds, attrs_lc[k]))
                return ""

            name = _get("sample_name")
            raw_date = (
                _get("injection_date_time_stamp")
                or _get("injection_date")
                or _get("injection_time")
            )
            method = _get("detection_method_name")
    if name:
        return name, "cdf", raw_date, method
    return (path.stem or "Unknown"), "filename", raw_date, method


def _read_cdf_meta_ex(path) -> tuple[CdfMeta, str]:
    """``read_cdf_meta`` plus where the lab ID came from ('cdf'|'filename')."""
    p = Path(path)
    name, name_source, raw_date, method = _read_cdf_names(p)
    mtime_str = None

    def _mtime() -> str:
        nonlocal mtime_str
        if mtime_str is None:
            mtime_str = datetime.fromtimestamp(p.stat().st_mtime).isoformat(sep=" ")
        return mtime_str

    correct = _correct_parse(raw_date)
    dt_source = "cdf" if correct is not None else "mtime"
    injection_dt = correct.isoformat(sep=" ") if correct is not None else _mtime()
    v1 = _v1_parse(raw_date)
    legacy = v1.isoformat(sep=" ") if v1 is not None else _mtime()
    # (>= 3.11 form, < 3.11 form); the < 3.11 form is the correct parse.
    forms = tuple(dict.fromkeys((legacy, injection_dt)))
    meta = CdfMeta(path=str(path), sha256=_sha256_file(p), lab_id=name,
                   injection_dt=injection_dt, dt_source=dt_source,
                   method_name=_method_basename(method), legacy_injection_dt=legacy,
                   raw_stamp=raw_date, v1_injection_dts=forms)
    return meta, name_source


@dataclass(frozen=True)
class CsvRow:
    line_no: int            # 1-based physical line the record starts on
    lab_id: str             # "Lab ID", stripped
    injection_dt_raw: str   # "InjectionDateTime" as written (whitespace stripped)
    source_file: str        # "Source File", stripped ("" on pre-Source-File rows)
    values: dict            # every CSV_HEADER name -> the cell verbatim ("" if absent)


def _is_header(cells: list[str]) -> bool:
    stripped = [c.strip() for c in cells]
    return bool(stripped) and stripped[0] == "Lab ID" and "InjectionDateTime" in stripped


def read_results_csv_ex(path) -> tuple[list[CsvRow], list[dict]]:
    """``read_results_csv`` plus a list of anomalies ``{"line_no", "kind",
    "detail"}`` for the dry-run report.

    The v1 CSV's layout changed over time (the T40/T60 cuts, then ``Best
    Fit``/``Fit Score``/``Source File`` were added) and v1 appended rows with
    whatever layout it had at the time, so this reader is driven by headers,
    not positions:

    * a record whose first cell is ``Lab ID`` and that contains
      ``InjectionDateTime`` is a header and defines the columns from there on
      (a header repeated mid-file is never a row);
    * a record with exactly ``len(CSV_HEADER)`` cells under an older, shorter
      header is read with ``CSV_HEADER`` (a new-format row appended to an
      unmigrated file);
    * short records are padded with ``""``, extra cells are ignored, both
      reported;
    * blank and all-empty records are skipped; a BOM is ignored;
    * a file that starts without a header is read with ``CSV_HEADER``.
    """
    current = list(distill.CSV_HEADER)
    rows: list[CsvRow] = []
    issues: list[dict] = []
    seen_header = False
    prev_end = 0

    def _issue(line_no: int, kind: str, detail: str = "") -> None:
        issues.append({"line_no": line_no, "kind": kind, "detail": detail})

    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        for cells in reader:
            line_no = prev_end + 1
            prev_end = reader.line_num
            if cells:
                cells[0] = cells[0].lstrip("﻿")
            if not any(c.strip() for c in cells):
                continue
            if _is_header(cells):
                current = [c.strip() for c in cells]
                unknown = [c for c in current if c and c not in distill.CSV_HEADER]
                if unknown:
                    _issue(line_no, "unknown-columns", ", ".join(unknown))
                if seen_header or rows:
                    _issue(line_no, "repeated-header",
                           f"{len(current)} columns")
                seen_header = True
                continue
            if not seen_header and not rows:
                _issue(line_no, "no-header", "read with the current CSV_HEADER")
            if any("�" in c for c in cells):
                _issue(line_no, "decode-error", "not valid UTF-8")

            cols = current
            if len(cells) != len(current):
                if (len(cells) == len(distill.CSV_HEADER)
                        and current != list(distill.CSV_HEADER)):
                    cols = list(distill.CSV_HEADER)
                    _issue(line_no, "full-width-row-under-old-header",
                           f"header has {len(current)} columns")
                elif len(cells) < len(current):
                    _issue(line_no, "short-row", f"{len(cells)} of {len(current)} cells")
                else:
                    _issue(line_no, "long-row", f"{len(cells)} of {len(current)} cells")

            by_name = {}
            for name, cell in zip(cols, cells):
                if name and name not in by_name:
                    by_name[name] = cell
            values = {c: by_name.get(c, "") for c in distill.CSV_HEADER}
            rows.append(CsvRow(
                line_no=line_no,
                lab_id=values["Lab ID"].strip(),
                injection_dt_raw=values["InjectionDateTime"].strip(),
                source_file=values["Source File"].strip(),
                values=values,
            ))
    return rows, issues


def read_results_csv(path) -> list[CsvRow]:
    """Every data row of a v1 results CSV, in file order (see
    ``read_results_csv_ex`` for the layouts it accepts)."""
    return read_results_csv_ex(path)[0]


@dataclass
class MatchedSample:
    cdf: CdfMeta | None         # None = result-only (legacy_unverified)
    rows: list[CsvRow]          # CSV order; last = current revision; [] = orphan CDF


@dataclass
class MatchReport:
    samples: list[MatchedSample]
    unmatched_rows: list[CsvRow]    # also result-only samples above (unless keyless)
    dup_sha: list[tuple[str, str]]  # (kept path, duplicate path), identical bytes
    no_injection_time: list[str]    # CDF paths that needed the mtime fallback
    mixed_rows: list[CsvRow]        # rows whose Source File is outside instrument_folder
    stats: dict


def _norm_path(p: str) -> str:
    """Windows-style, case-insensitive form for folder comparison: the CSV
    holds UNC paths whose server name varies in case (``ASAPServer`` /
    ``asapserver``)."""
    return p.strip().replace("/", "\\").rstrip("\\").casefold()


def _parent_dir(p: str) -> str:
    s = p.strip().replace("/", "\\")
    return s.rsplit("\\", 1)[0] if "\\" in s else ""


def _cdf_key(c: CdfMeta) -> tuple[str, str]:
    return (c.lab_id.strip(), c.injection_dt)


def _sample_sort_key(s: MatchedSample):
    if s.cdf is not None:
        return (s.cdf.injection_dt, s.cdf.lab_id.strip(), s.cdf.path, 0)
    first = s.rows[0]
    return (first.injection_dt_raw, first.lab_id, "", first.line_no)


def match(cdfs, rows, *, instrument_folder: str) -> MatchReport:
    """Pair one instrument's CDFs with its CSV rows (see the module docstring
    for the identity rule). Deterministic for any order of ``cdfs``; ``rows``
    must be in CSV order (revision order comes from it).

    * identical bytes: the first path (sorted) is kept, the rest go to
      ``dup_sha`` and are not samples;
    * two different CDFs with one (lab ID, time) key: the first path is kept,
      the other is listed in ``stats['key_collisions']`` and is not a sample
      (the store's UNIQUE key would refuse it; the importer raises a conflict);
    * a row whose ``Source File`` is set and not inside ``instrument_folder``
      goes to ``mixed_rows`` only. An empty ``instrument_folder`` disables the
      check;
    * rows with no CDF become one result-only sample per (lab ID, time) and are
      listed in ``unmatched_rows``; rows with an empty lab ID or time are
      listed there but can't form a sample;
    * CDFs with no rows are orphan samples.
    """
    folder = _norm_path(instrument_folder or "")

    # 1. CDFs: dedupe identical bytes, then one CDF per key.
    by_sha: dict[str, CdfMeta] = {}
    dup_sha: list[tuple[str, str]] = []
    by_key: dict[tuple[str, str], CdfMeta] = {}
    collisions: list[tuple[str, str]] = []
    for c in sorted(cdfs, key=lambda c: c.path):
        kept = by_sha.get(c.sha256)
        if kept is not None:
            dup_sha.append((kept.path, c.path))
            continue
        by_sha[c.sha256] = c
        key = _cdf_key(c)
        if key in by_key:
            collisions.append((by_key[key].path, c.path))
            continue
        by_key[key] = c
    no_injection_time = [c.path for c in sorted(cdfs, key=lambda c: c.path)
                         if c.dt_source == "mtime"]
    labs_with_cdf = {k[0] for k in by_key}

    # 2. Rows: split off other instruments' rows, group the rest by key.
    mixed_rows: list[CsvRow] = []
    groups: dict[tuple[str, str], list[CsvRow]] = {}
    keyless: list[CsvRow] = []
    rows_without_source = 0
    source_folders: dict[str, int] = {}
    for r in rows:
        if r.source_file:
            parent = _parent_dir(r.source_file)
            source_folders[parent] = source_folders.get(parent, 0) + 1
        else:
            rows_without_source += 1
        if (folder and r.source_file
                and not _norm_path(r.source_file).startswith(folder + "\\")):
            mixed_rows.append(r)
            continue
        if not r.lab_id or not r.injection_dt_raw:
            keyless.append(r)
            continue
        groups.setdefault((r.lab_id, r.injection_dt_raw), []).append(r)

    # 3. Attach.
    samples: list[MatchedSample] = []
    unmatched_rows: list[CsvRow] = []
    attached = orphans = result_only = rows_attached = revisions_extra = 0
    for key, c in by_key.items():
        grp = groups.pop(key, [])
        samples.append(MatchedSample(cdf=c, rows=grp))
        if grp:
            attached += 1
            rows_attached += len(grp)
            revisions_extra += len(grp) - 1
        else:
            orphans += 1
    same_lab_other_time = 0
    for key, grp in groups.items():
        samples.append(MatchedSample(cdf=None, rows=grp))
        result_only += 1
        revisions_extra += len(grp) - 1
        unmatched_rows.extend(grp)
        if key[0] in labs_with_cdf:
            same_lab_other_time += len(grp)
    unmatched_rows.extend(keyless)
    unmatched_rows.sort(key=lambda r: r.line_no)
    # Not mixed and has a Source File: v1 said its CDF is in this folder, but
    # no CDF there carries this (lab ID, time). Mostly mtime-derived times.
    source_in_folder_unmatched = sum(1 for r in unmatched_rows if r.source_file)
    samples.sort(key=_sample_sort_key)

    stats = {
        "cdfs": len(cdfs),
        "cdfs_unique": len(by_sha),
        "dup_sha": len(dup_sha),
        "key_collisions": collisions,
        "no_injection_time": len(no_injection_time),
        "rows": len(rows),
        "samples": len(samples),
        "attached_samples": attached,
        "orphan_cdfs": orphans,
        "result_only_samples": result_only,
        "rows_attached": rows_attached,
        "revisions_extra": revisions_extra,
        "unmatched_rows": len(unmatched_rows),
        "rows_missing_key": len(keyless),
        "unmatched_same_lab_other_time": same_lab_other_time,
        "unmatched_source_in_folder": source_in_folder_unmatched,
        "mixed_rows": len(mixed_rows),
        "rows_without_source_file": rows_without_source,
        "source_folders": dict(sorted(source_folders.items(),
                                      key=lambda kv: (-kv[1], kv[0]))),
    }
    return MatchReport(samples=samples, unmatched_rows=unmatched_rows,
                       dup_sha=dup_sha, no_injection_time=no_injection_time,
                       mixed_rows=mixed_rows, stats=stats)


def read_cdf_meta(path) -> CdfMeta:
    """Metadata of one CDF: the identity v1 used, plus the file's sha256.

    ``lab_id`` is the sample name exactly as ``distill.cdf_metadata`` returns
    it (the value v1 wrote to the CSV's ``Lab ID``). When the CDF has no
    parsable injection time, v1 used the file's mtime; so does this, with
    ``dt_source='mtime'`` so the caller can report it."""
    return _read_cdf_meta_ex(path)[0]


# ── Dry run (read-only) ─────────────────────────────────────────────────────
def iter_cdf_paths(processed_dir) -> list[Path]:
    """Every ``*.cdf`` (any case) under ``processed_dir``, sorted, recursive."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(Path(processed_dir)):
        dirnames.sort()
        for name in sorted(filenames):
            if name.lower().endswith(".cdf"):
                found.append(Path(dirpath) / name)
    return found


def dry_run(processed_dir, results_csv, *, instrument_folder: str,
            on_progress: Callable[[int, int], None] | None = None) -> MatchReport:
    """Read-only: read every CDF's metadata under ``processed_dir`` (never
    the chromatogram arrays; sha256 is streamed) and the results CSV, then
    ``match``. ``results_csv=None`` runs in CDF-only mode.

    Extra ``stats`` keys: ``cdf_errors`` [(path, message)], ``csv_issues``
    (from ``read_results_csv_ex``), ``name_from_filename`` [paths whose CDF
    has no sample name], ``dt_source`` {'cdf': n, 'mtime': n}."""
    paths = iter_cdf_paths(processed_dir)
    total = len(paths)
    metas: list[CdfMeta] = []
    errors: list[tuple[str, str]] = []
    name_from_filename: list[str] = []
    for i, p in enumerate(paths, 1):
        try:
            meta, name_source = _read_cdf_meta_ex(p)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            errors.append((str(p), f"{type(exc).__name__}: {exc}"))
        else:
            metas.append(meta)
            if name_source != "cdf":
                name_from_filename.append(meta.path)
        if on_progress is not None and (i % 500 == 0 or i == total):
            on_progress(i, total)

    rows: list[CsvRow] = []
    csv_issues: list[dict] = []
    if results_csv:
        rows, csv_issues = read_results_csv_ex(results_csv)

    report = match(metas, rows, instrument_folder=instrument_folder)
    dt_source = {"cdf": 0, "mtime": 0}
    for m in metas:
        dt_source[m.dt_source] = dt_source.get(m.dt_source, 0) + 1
    report.stats.update({
        "processed_dir": str(processed_dir),
        "results_csv": str(results_csv) if results_csv else None,
        "instrument_folder": instrument_folder,
        "files_seen": total,
        "cdf_errors": errors,
        "csv_issues": csv_issues,
        "name_from_filename": name_from_filename,
        "dt_source": dt_source,
    })
    return report


def report_to_dict(report: MatchReport) -> dict:
    """A JSON-ready copy of ``report`` (dataclasses → dicts, tuples → lists)."""
    return asdict(report)


def _row_brief(r: CsvRow) -> str:
    src = f"  <- {r.source_file}" if r.source_file else ""
    return f"line {r.line_no}: {r.lab_id!r} @ {r.injection_dt_raw!r}{src}"


def format_summary(report: MatchReport, *, examples: int = 5) -> str:
    """Human summary: counts, then the first ``examples`` of each class."""
    st = report.stats
    n = max(0, int(examples))
    out: list[str] = []
    add = out.append

    add("History import dry run (read-only)")
    for label, key in (("Processed folder", "processed_dir"), ("Results CSV", "results_csv"),
                       ("Instrument folder", "instrument_folder")):
        if key in st:
            add(f"  {label}: {st[key] or '(none: CDF-only mode)'}")
    add("")
    add("Counts")
    add(f"  CDFs read:                 {st['cdfs']}"
        + (f" of {st['files_seen']} files" if "files_seen" in st else ""))
    dts = st.get("dt_source")
    if dts:
        add(f"    injection time from CDF: {dts.get('cdf', 0)}; from file mtime: {dts.get('mtime', 0)}")
    add(f"  unreadable CDFs:           {len(st.get('cdf_errors', []))}")
    add(f"  identical bytes (dups):    {st['dup_sha']}")
    add(f"  same key, different bytes: {len(st['key_collisions'])}")
    add(f"  no injection time (mtime): {st['no_injection_time']}")
    add(f"  CSV rows:                  {st['rows']}")
    add(f"  samples:                   {st['samples']}")
    add(f"    attached (CDF + rows):   {st['attached_samples']}  ({st['rows_attached']} rows)")
    add(f"    orphan CDFs (no row):    {st['orphan_cdfs']}")
    add(f"    result-only (no CDF):    {st['result_only_samples']}")
    add(f"  extra revisions:           {st['revisions_extra']}")
    add(f"  unmatched rows:            {st['unmatched_rows']}"
        f"  (same lab ID on a CDF at another time: {st['unmatched_same_lab_other_time']};"
        f" Source File in this folder: {st['unmatched_source_in_folder']};"
        f" no lab ID/time: {st['rows_missing_key']})")
    add(f"  mixed rows (other folder): {st['mixed_rows']}")
    add(f"  rows without Source File:  {st['rows_without_source_file']}")
    if st.get("csv_issues"):
        kinds: dict[str, int] = {}
        for i in st["csv_issues"]:
            kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        add("  CSV layout issues:         "
            + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    if st.get("source_folders"):
        add("")
        add("Source File folders in the CSV")
        for folder, count in list(st["source_folders"].items())[: max(n, 10)]:
            add(f"  {count:6d}  {folder}")

    def section(title: str, items: list, fmt) -> None:
        if not items or n == 0:
            return
        add("")
        add(f"{title} (first {min(n, len(items))} of {len(items)})")
        for item in items[:n]:
            add("  " + fmt(item))

    add("")
    add("Examples")
    section("Unreadable CDFs", st.get("cdf_errors", []), lambda e: f"{e[0]}: {e[1]}")
    section("Identical bytes (kept, duplicate)", report.dup_sha,
            lambda d: f"{d[0]}  ==  {d[1]}")
    section("Same key, different bytes (kept, other)", st["key_collisions"],
            lambda d: f"{d[0]}  vs  {d[1]}")
    section("No injection time in the CDF (mtime used)", report.no_injection_time, str)
    section("CDF with no sample name (file stem used)", st.get("name_from_filename", []), str)
    section("Attached samples", [s for s in report.samples if s.cdf is not None and s.rows],
            lambda s: f"{s.cdf.lab_id!r} @ {s.cdf.injection_dt}: {len(s.rows)} row(s)"
                      f"  [{s.cdf.path}]")
    section("Samples with revisions", [s for s in report.samples if len(s.rows) > 1],
            lambda s: f"{s.rows[0].lab_id!r} @ {s.rows[0].injection_dt_raw}: {len(s.rows)} rows,"
                      f" lines {', '.join(str(r.line_no) for r in s.rows)}")
    section("Orphan CDFs", [s for s in report.samples if s.cdf is not None and not s.rows],
            lambda s: f"{s.cdf.lab_id!r} @ {s.cdf.injection_dt} ({s.cdf.dt_source})"
                      f"  [{s.cdf.path}]")
    section("Result-only samples / unmatched rows", report.unmatched_rows, _row_brief)
    section("Mixed rows (Source File outside the instrument folder)", report.mixed_rows,
            _row_brief)
    section("CSV layout issues", st.get("csv_issues", []),
            lambda i: f"line {i['line_no']}: {i['kind']} {i['detail']}".rstrip())
    return "\n".join(out) + "\n"


# ── v1 .processed_index.json (the Looker's set of (sample, str(datetime))) ──
_CANON_S = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_CANON_US = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}$")


def summarize_processed_index(path, *, examples: int = 10) -> dict:
    """Profile v1's ``.processed_index.json``: count, date range, duplicates,
    how many times are canonical (and how many carry microseconds, which only
    the mtime fallback produces: CDF stamps have whole seconds), blank-like
    names and names with outer whitespace."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data.get("entries", []) if isinstance(data, dict) else data
    pairs = [(str(e[0]), str(e[1])) for e in entries
             if isinstance(e, (list, tuple)) and len(e) == 2]

    exact: dict[tuple[str, str], int] = {}
    stripped: dict[tuple[str, str], int] = {}
    times_per_lab: dict[str, set] = {}
    blank_like: dict[str, int] = {}
    canon_s = canon_us = padded = empty = 0
    micro: list[list[str]] = []
    non_canonical: list[list[str]] = []
    canon_times: list[str] = []
    for name, dt in pairs:
        exact[(name, dt)] = exact.get((name, dt), 0) + 1
        key = (name.strip(), dt.strip())
        stripped[key] = stripped.get(key, 0) + 1
        times_per_lab.setdefault(name.strip(), set()).add(dt.strip())
        if "blank" in name.lower():
            blank_like[name] = blank_like.get(name, 0) + 1
        if name != name.strip():
            padded += 1
        if not name.strip():
            empty += 1
        if _CANON_S.match(dt):
            canon_s += 1
            canon_times.append(dt)
        elif _CANON_US.match(dt):
            canon_us += 1
            canon_times.append(dt[:19])
            micro.append([name, dt])
        else:
            non_canonical.append([name, dt])

    return {
        "entries": len(pairs),
        "distinct_lab_ids": len(times_per_lab),
        "date_min": min(canon_times) if canon_times else None,
        "date_max": max(canon_times) if canon_times else None,
        "exact_duplicates": sum(c - 1 for c in exact.values()),
        "duplicates_after_strip": sum(c - 1 for c in stripped.values()),
        "canonical_seconds": canon_s,
        "canonical_microseconds": canon_us,
        "canonical_microseconds_examples": micro[:examples],
        "non_canonical": len(non_canonical),
        "non_canonical_examples": non_canonical[:examples],
        "blank_like_names": dict(sorted(blank_like.items(), key=lambda kv: (-kv[1], kv[0]))),
        "names_with_outer_whitespace": padded,
        "empty_names": empty,
        "lab_ids_with_several_times": sum(1 for s in times_per_lab.values() if len(s) > 1),
    }
