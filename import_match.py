"""History-import matcher (phase 2, plan 2D; contract §3).

Pairs the v1 results CSV with the processed CDFs of **one** instrument, by
each CDF's own metadata (sample name + injection time), never by filename.

Pure: no database, no Flask, no settings. It only reads files and never
writes to the source folder or the CSV. The hub's importer job (Lane A)
turns a ``MatchReport`` into samples and revisions; ``tools/import_dry_run.py``
prints it.

Identity rule (spec "History import" and "Injection time"): a CSV row
attaches to a CDF only when ``normalise_lab_id`` of both are equal **and**
the row's ``InjectionDateTime`` equals, as a string, one of the forms v1
could have written for that CDF (``CdfMeta.v1_injection_dts``: v1's
fromisoformat-first misparse on Python >= 3.11, and the correct parse,
which is also what v1 wrote on older Pythons). The sample is stored under
the **correct** time (``CdfMeta.injection_dt``). ``Source File`` values only
spot rows from another instrument's folder and break ties between CDFs that
already match by metadata; they never attach a row on their own.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
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
    """A header is any record whose first cell is ``Lab ID``."""
    return bool(cells) and cells[0].strip() == "Lab ID"


def _decode_csv(data: bytes) -> tuple[str, str]:
    """(text, encoding): strict UTF-8 (BOM dropped) first, else the whole
    file as cp1252 (undefined bytes become U+FFFD and flag their rows)."""
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "cp1252"


def read_results_csv_ex(path) -> tuple[list[CsvRow], list[dict]]:
    """``read_results_csv`` plus a list of anomalies ``{"line_no", "kind",
    "detail"}`` for the dry-run report.

    The v1 CSV's layout changed over time (the T40/T60 cuts, then ``Best
    Fit``/``Fit Score``/``Source File`` were added) and v1 appended rows with
    whatever layout it had at the time, so this reader is driven by headers,
    not positions:

    * a record whose first cell is ``Lab ID`` is a header and defines the
      columns from there on (a header repeated mid-file is never a row);
      ``CSV_HEADER`` columns it lacks are reported (``missing-columns``); a
      name it repeats is reported and the **last** one wins, like
      ``csv.DictReader``;
    * a record with exactly ``len(CSV_HEADER)`` cells under an older, shorter
      header is read with ``CSV_HEADER`` (a new-format row appended to an
      unmigrated file);
    * short records are padded with ``""``, extra cells are ignored; both are
      reported (``short-row``/``long-row``) and ``match`` holds those rows;
    * the file is decoded as strict UTF-8, else as cp1252 (reported as
      ``{"line_no": 0, "kind": "encoding", "detail": "cp1252"}``); a row with
      undecodable bytes is reported as ``decode-error``;
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

    with open(path, "rb") as fh:
        text, encoding = _decode_csv(fh.read())
    if encoding != "utf-8":
        _issue(0, "encoding", encoding)
    reader = csv.reader(io.StringIO(text, newline=""))
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
            missing = [c for c in distill.CSV_HEADER if c not in current]
            if missing:
                _issue(line_no, "missing-columns", ", ".join(missing))
            dups = sorted({c for c in current if c and current.count(c) > 1})
            if dups:
                _issue(line_no, "duplicate-columns", ", ".join(dups) + " (last wins)")
            if seen_header or rows:
                _issue(line_no, "repeated-header", f"{len(current)} columns")
            seen_header = True
            continue
        if not seen_header and not rows:
            _issue(line_no, "no-header", "read with the current CSV_HEADER")
        if any("�" in c for c in cells):
            _issue(line_no, "decode-error", f"undecodable bytes ({encoding})")

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
            if name:
                by_name[name] = cell            # last wins, like csv.DictReader
        values = {c: by_name.get(c, "") for c in distill.CSV_HEADER}
        rows.append(CsvRow(
            line_no=line_no,
            lab_id=normalise_lab_id(values["Lab ID"]),
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
    # The sample's identity as the store gets it (derived, not passed):
    lab_id: str = field(init=False)        # CDF sample name verbatim, or the CSV's lab ID
    injection_dt: str = field(init=False)  # CDF: the CORRECT time; result-only: the CSV string
    dt_source: str = field(init=False)     # 'cdf' | 'mtime' | 'csv' (result-only)

    def __post_init__(self) -> None:
        if self.cdf is not None:
            self.lab_id = self.cdf.lab_id
            self.injection_dt = self.cdf.injection_dt
            self.dt_source = self.cdf.dt_source
        elif self.rows:
            self.lab_id = self.rows[0].lab_id
            self.injection_dt = self.rows[0].injection_dt_raw
            self.dt_source = "csv"
        else:
            self.lab_id = self.injection_dt = ""
            self.dt_source = "csv"


@dataclass
class MatchReport:
    samples: list[MatchedSample]
    unmatched_rows: list[CsvRow]    # also result-only samples above (unless keyless)
    dup_sha: list[tuple[str, str]]  # (kept path, duplicate path), identical bytes
    no_injection_time: list[str]    # kept CDFs whose correct time needed the mtime fallback
    mixed_rows: list[CsvRow]        # rows whose Source File is outside instrument_folder
    stats: dict
    # (kept, other): two CDFs with one identity, or one CSV key matching both
    key_collisions: list[tuple[CdfMeta, CdfMeta]] = field(default_factory=list)
    # rows with a layout problem (short/long record, undecodable): never attached
    held_rows: list[CsvRow] = field(default_factory=list)


# CSV issues that make a row's cells untrustworthy: such rows are held.
HOLDING_ISSUES = frozenset({"short-row", "long-row", "decode-error"})

_CANON_DT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d{6})?$")
_EXAMPLES = 20
_NEAR_SECONDS = 2
_NEAR_MAX_HOURS = 14


def _norm_path(p: str) -> str:
    """Windows-style, case-insensitive form for folder comparison: the CSV
    holds UNC paths whose server name varies in case (``ASAPServer`` /
    ``asapserver``)."""
    return (p or "").strip().replace("/", "\\").rstrip("\\").casefold()


def _parent_dir(p: str) -> str:
    s = p.strip().replace("/", "\\")
    return s.rsplit("\\", 1)[0] if "\\" in s else ""


def _basename(p: str) -> str:
    return _norm_path(p).rsplit("\\", 1)[-1]


def _folder_aliases(instrument_folder) -> list[str]:
    if not instrument_folder:
        return []
    items = [instrument_folder] if isinstance(instrument_folder, str) else list(instrument_folder)
    return [n for n in (_norm_path(a) for a in items) if n]


def _in_folder(source: str, aliases: list[str]) -> bool:
    """True when ``source`` lies in one of the aliases. A full path alias
    matches as a prefix; a bare folder name (no separator, no drive) matches
    the file's immediate parent folder name."""
    s = _norm_path(source)
    parent = s.rsplit("\\", 1)[0] if "\\" in s else ""
    for a in aliases:
        if "\\" in a or ":" in a:
            if s.startswith(a + "\\"):
                return True
        elif parent.rsplit("\\", 1)[-1] == a:
            return True
    return False


def _identity(c: CdfMeta) -> tuple[str, str]:
    return (normalise_lab_id(c.lab_id), c.injection_dt)


def _sample_sort_key(s: MatchedSample):
    if s.cdf is not None:
        return (s.injection_dt, normalise_lab_id(s.lab_id), s.cdf.path, 0)
    return (s.injection_dt, s.lab_id, "", s.rows[0].line_no)


def _parse_canonical(s: str) -> datetime | None:
    if not _CANON_DT.match(s or ""):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _timezone_info() -> dict:
    offset = datetime.now().astimezone().utcoffset()
    return {"tzname": list(time.tzname),
            "utc_offset_seconds": int(offset.total_seconds()) if offset else 0,
            "note": "mtime fallbacks are this machine's local wall-clock time"}


def match(cdfs, rows, *, instrument_folder, csv_issues=None) -> MatchReport:
    """Pair one instrument's CDFs with its CSV rows. Deterministic for any
    order of ``cdfs``; ``rows`` must be in CSV order (revision order).

    * **Identity rule.** A row attaches to a CDF when
      ``normalise_lab_id`` of both are equal and the row's InjectionDateTime
      equals any string v1 could have written for that CDF
      (``v1_injection_dts``: the Python >= 3.11 misparse and the correct
      form). ``Source File`` never attaches a row; its basename only breaks
      ties between CDFs that already match by metadata.
    * **Identical bytes:** one path is kept (the one a matching row's Source
      File names, else the first sorted); the rest go to ``dup_sha``.
    * **Two CDFs with one identity** (lab ID + correct time), or one CSV key
      matching several CDFs: the same tie-break picks one; the pairs go to
      ``key_collisions`` and the rows involved to ``stats['collided_rows']``.
      A CDF that loses an identity collision is not a sample (the store's
      UNIQUE key would refuse it).
    * **Held rows:** rows with a ``HOLDING_ISSUES`` entry in ``csv_issues``
      are never attached and never result-only.
    * **Mixed rows:** a row whose Source File is set and not inside any alias
      of ``instrument_folder`` (str or sequence; case-insensitive, ``/`` and
      ``\\`` alike, trailing separator ignored; a bare folder name matches the
      parent folder's name) goes to ``mixed_rows`` only. Empty disables it.
    * Rows with no CDF become one result-only sample per (lab ID, time),
      ``dt_source='csv'``, and are listed in ``unmatched_rows``; rows with an
      empty lab ID or time are listed there but form no sample.
    * CDFs with no rows are orphan samples.
    """
    aliases = _folder_aliases(instrument_folder)
    held_lines = {i.get("line_no") for i in (csv_issues or ())
                  if i.get("kind") in HOLDING_ISSUES}

    # 1. Rows: held, mixed, keyless, then grouped by key in CSV order.
    held: list[CsvRow] = []
    mixed: list[CsvRow] = []
    keyless: list[CsvRow] = []
    groups: dict[tuple[str, str], list[CsvRow]] = {}
    basenames: dict[tuple[str, str], set] = {}
    rows_without_source = rows_in_folder = 0
    source_folders: dict[str, int] = {}
    noncanonical: list[list] = []
    noncanonical_n = 0
    for r in rows:
        if r.source_file:
            parent = _parent_dir(r.source_file)
            source_folders[parent] = source_folders.get(parent, 0) + 1
        else:
            rows_without_source += 1
        if r.injection_dt_raw and not _CANON_DT.match(r.injection_dt_raw):
            noncanonical_n += 1
            if len(noncanonical) < _EXAMPLES:
                noncanonical.append([r.line_no, r.injection_dt_raw])
        if r.line_no in held_lines:
            held.append(r)
            continue
        if aliases and r.source_file:
            if not _in_folder(r.source_file, aliases):
                mixed.append(r)
                continue
            rows_in_folder += 1
        key = (normalise_lab_id(r.lab_id), r.injection_dt_raw)
        if not key[0] or not key[1]:
            keyless.append(r)
            continue
        groups.setdefault(key, []).append(r)
        if r.source_file:
            basenames.setdefault(key, set()).add(_basename(r.source_file))

    def names_for(c: CdfMeta) -> set:
        lab = normalise_lab_id(c.lab_id)
        out: set = set()
        for s in _v1_strings(c):
            out |= basenames.get((lab, s), set())
        return out

    def preferred(cands: list, names: set) -> CdfMeta:
        for c in cands:
            if _basename(c.path) in names:
                return c
        return cands[0]

    ordered = sorted(cdfs, key=lambda c: c.path)

    # 2. Identical bytes.
    by_sha: dict[str, list[CdfMeta]] = {}
    for c in ordered:
        by_sha.setdefault(c.sha256, []).append(c)
    dup_sha: list[tuple[str, str]] = []
    unique: list[CdfMeta] = []
    for grp in by_sha.values():
        kept = preferred(grp, set().union(*(names_for(c) for c in grp)))
        unique.append(kept)
        dup_sha.extend((kept.path, c.path) for c in grp if c is not kept)
    unique.sort(key=lambda c: c.path)
    dup_sha.sort()

    # 3. One CDF per identity.
    by_identity: dict[tuple[str, str], list[CdfMeta]] = {}
    for c in unique:
        by_identity.setdefault(_identity(c), []).append(c)
    key_collisions: list[tuple[CdfMeta, CdfMeta]] = []
    collided_keys: set = set()
    kept_cdfs: list[CdfMeta] = []
    for grp in by_identity.values():
        kept = preferred(grp, set().union(*(names_for(c) for c in grp)))
        kept_cdfs.append(kept)
        for c in grp:
            if c is not kept:
                key_collisions.append((kept, c))
            lab = normalise_lab_id(c.lab_id)
            if len(grp) > 1:
                collided_keys.update((lab, s) for s in _v1_strings(c))
    kept_cdfs.sort(key=lambda c: c.path)

    # 4. Attach row groups through every v1 form.
    index: dict[tuple[str, str], list[CdfMeta]] = {}
    for c in kept_cdfs:
        lab = normalise_lab_id(c.lab_id)
        for s in _v1_strings(c):
            index.setdefault((lab, s), []).append(c)
    attached: dict[int, list[CsvRow]] = {}  # id(CdfMeta) -> rows
    result_only: list[tuple[tuple[str, str], list[CsvRow]]] = []
    for key, grp in groups.items():
        cands = index.get(key, [])
        if not cands:
            result_only.append((key, grp))
            continue
        chosen = preferred(cands, basenames.get(key, set()))
        if len(cands) > 1:
            collided_keys.add(key)
            for c in cands:
                if c is not chosen and (chosen, c) not in key_collisions:
                    key_collisions.append((chosen, c))
        attached.setdefault(id(chosen), []).extend(grp)
    collided_rows = sorted(r.line_no for key, grp in groups.items()
                           if key in collided_keys for r in grp)

    # 5. Samples.
    samples: list[MatchedSample] = []
    n_attached = orphans = rows_attached = revisions_extra = via_v1 = 0
    for c in kept_cdfs:
        grp = sorted(attached.get(id(c), []), key=lambda r: r.line_no)
        samples.append(MatchedSample(cdf=c, rows=grp))
        if grp:
            n_attached += 1
            rows_attached += len(grp)
            revisions_extra += len(grp) - 1
            via_v1 += sum(1 for r in grp if r.injection_dt_raw != c.injection_dt)
        else:
            orphans += 1

    cdfs_by_lab: dict[str, list[CdfMeta]] = {}
    for c in kept_cdfs:
        cdfs_by_lab.setdefault(normalise_lab_id(c.lab_id), []).append(c)
    unmatched_rows: list[CsvRow] = []
    same_lab_other_time = 0
    near: list[dict] = []
    for key, grp in result_only:
        samples.append(MatchedSample(cdf=None, rows=grp))
        revisions_extra += len(grp) - 1
        unmatched_rows.extend(grp)
        others = cdfs_by_lab.get(key[0], [])
        if others:
            same_lab_other_time += len(grp)
        row_dt = _parse_canonical(key[1])
        if row_dt is None:
            continue
        hit = None
        for c in others:
            for s in _v1_strings(c):
                cdt = _parse_canonical(s)
                if cdt is None:
                    continue
                delta = int(round(abs((cdt - row_dt).total_seconds())))
                if (0 < delta <= _NEAR_SECONDS
                        or (delta % 3600 == 0 and 0 < delta <= _NEAR_MAX_HOURS * 3600)):
                    hit = (c, s, delta)
                    break
            if hit:
                break
        if hit:
            for r in grp:
                near.append({"line_no": r.line_no, "lab_id": r.lab_id,
                             "csv_dt": r.injection_dt_raw, "cdf_dt": hit[1],
                             "cdf_path": hit[0].path, "delta_seconds": hit[2]})
    unmatched_rows.extend(keyless)
    unmatched_rows.sort(key=lambda r: r.line_no)
    near.sort(key=lambda e: e["line_no"])
    samples.sort(key=_sample_sort_key)

    method_names: dict[str, int] = {}
    for c in kept_cdfs:
        method_names[c.method_name] = method_names.get(c.method_name, 0) + 1
    misparsed = [c for c in kept_cdfs if (c.legacy_injection_dt or c.injection_dt) != c.injection_dt]
    no_injection_time = [c.path for c in kept_cdfs if c.dt_source == "mtime"]
    mixed_here = [r.line_no for r in mixed
                  if (normalise_lab_id(r.lab_id), r.injection_dt_raw) in index]

    stats = {
        "cdfs": len(cdfs),
        "cdfs_unique": len(unique),
        "dup_sha": len(dup_sha),
        "key_collisions": len(key_collisions),
        "collided_rows": collided_rows,
        "no_injection_time": len(no_injection_time),
        "v1_misparsed_cdfs": len(misparsed),
        "v1_misparsed_examples": [[c.path, c.raw_stamp, c.legacy_injection_dt, c.injection_dt]
                                  for c in misparsed[:_EXAMPLES]],
        "method_names": dict(sorted(method_names.items(), key=lambda kv: (-kv[1], kv[0]))),
        "rows": len(rows),
        "samples": len(samples),
        "attached_samples": n_attached,
        "orphan_cdfs": orphans,
        "result_only_samples": len(result_only),
        "rows_attached": rows_attached,
        "rows_matched_via_v1_form": via_v1,
        "revisions_extra": revisions_extra,
        "unmatched_rows": len(unmatched_rows),
        "rows_missing_key": len(keyless),
        "unmatched_same_lab_other_time": same_lab_other_time,
        # Not mixed and has a Source File: v1 said its CDF is in this folder,
        # but no CDF here carries this (lab ID, time).
        "unmatched_source_in_folder": sum(1 for r in unmatched_rows if r.source_file),
        "near_misses": {"count": len(near), "examples": near[:_EXAMPLES]},
        "held_rows": len(held),
        "mixed_rows": len(mixed),
        "mixed_but_key_matches_here": mixed_here,
        "rows_in_folder": rows_in_folder,
        "mixed_warning": bool(rows) and len(mixed) * 2 >= len(rows) and rows_in_folder == 0,
        "rows_without_source_file": rows_without_source,
        "rows_noncanonical_dt": {"count": noncanonical_n, "examples": noncanonical},
        "source_folders": dict(sorted(source_folders.items(), key=lambda kv: (-kv[1], kv[0]))),
        "timezone": _timezone_info(),
    }
    return MatchReport(samples=samples, unmatched_rows=unmatched_rows, dup_sha=dup_sha,
                       no_injection_time=no_injection_time, mixed_rows=mixed, stats=stats,
                       key_collisions=key_collisions, held_rows=held)


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


def dry_run(processed_dir, results_csv, *, instrument_folder,
            on_progress: Callable[[int, int], None] | None = None) -> MatchReport:
    """Read-only: read every CDF's metadata under ``processed_dir`` (never
    the chromatogram arrays; sha256 is streamed) and the results CSV, then
    ``match`` (with the CSV's issues, so badly laid-out rows are held).
    ``results_csv=None`` runs in CDF-only mode. ``instrument_folder`` is a
    str or a sequence of aliases (see ``match``).

    Extra ``stats`` keys: ``cdf_errors`` [(path, message)], ``csv_issues``,
    ``csv_encoding``, ``name_from_filename`` [paths whose CDF has no sample
    name], ``dt_source`` {'cdf': n, 'mtime': n} over every CDF read,
    ``cdfs_per_folder`` and ``method_names_per_folder`` keyed by the folder
    relative to ``processed_dir`` ('.' for the top)."""
    root = Path(processed_dir)
    paths = iter_cdf_paths(root)
    total = len(paths)
    metas: list[CdfMeta] = []
    errors: list[tuple[str, str]] = []
    name_from_filename: list[str] = []
    per_folder: dict[str, int] = {}
    methods_per_folder: dict[str, dict[str, int]] = {}
    for i, p in enumerate(paths, 1):
        folder = p.parent.relative_to(root).as_posix() if p.parent != root else "."
        per_folder[folder] = per_folder.get(folder, 0) + 1
        try:
            meta, name_source = _read_cdf_meta_ex(p)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            errors.append((str(p), f"{type(exc).__name__}: {exc}"))
        else:
            metas.append(meta)
            hist = methods_per_folder.setdefault(folder, {})
            hist[meta.method_name] = hist.get(meta.method_name, 0) + 1
            if name_source != "cdf":
                name_from_filename.append(meta.path)
        if on_progress is not None and (i % 500 == 0 or i == total):
            on_progress(i, total)

    rows: list[CsvRow] = []
    csv_issues: list[dict] = []
    csv_encoding = None
    if results_csv:
        rows, csv_issues = read_results_csv_ex(results_csv)
        csv_encoding = next((i["detail"] for i in csv_issues if i["kind"] == "encoding"),
                            "utf-8")

    report = match(metas, rows, instrument_folder=instrument_folder, csv_issues=csv_issues)
    dt_source = {"cdf": 0, "mtime": 0}
    for m in metas:
        dt_source[m.dt_source] = dt_source.get(m.dt_source, 0) + 1

    def _sorted_hist(h: dict) -> dict:
        return dict(sorted(h.items(), key=lambda kv: (-kv[1], kv[0])))

    report.stats.update({
        "processed_dir": str(processed_dir),
        "results_csv": str(results_csv) if results_csv else None,
        "instrument_folder": instrument_folder if isinstance(instrument_folder, (str, type(None)))
        else list(instrument_folder),
        "files_seen": total,
        "cdf_errors": errors,
        "csv_issues": csv_issues,
        "csv_encoding": csv_encoding,
        "name_from_filename": name_from_filename,
        "dt_source": dt_source,
        "cdfs_per_folder": dict(sorted(per_folder.items())),
        "method_names_per_folder": {k: _sorted_hist(v)
                                    for k, v in sorted(methods_per_folder.items())},
    })
    return report


def report_to_dict(report: MatchReport) -> dict:
    """A JSON-ready copy of ``report`` (dataclasses → dicts, tuples → lists)."""
    return asdict(report)


def _row_brief(r: CsvRow) -> str:
    src = f"  <- {r.source_file}" if r.source_file else ""
    return f"line {r.line_no}: {r.lab_id!r} @ {r.injection_dt_raw!r}{src}"


def _method_label(name: str) -> str:
    return name if name else "(absent)"


def format_summary(report: MatchReport, *, examples: int = 5) -> str:
    """Human summary: warnings, counts, then the first ``examples`` of each
    problem class."""
    st = report.stats
    n = max(0, int(examples))
    out: list[str] = []
    add = out.append

    add("History import dry run (read-only)")
    if "processed_dir" in st:
        add(f"  Processed folder: {st['processed_dir']}")
    if "results_csv" in st:
        add(f"  Results CSV: {st['results_csv'] or '(none: CDF-only mode)'}"
            + (f"  [decoded as {st['csv_encoding']}]" if st.get("csv_encoding") else ""))
    if "instrument_folder" in st:
        folder = st["instrument_folder"]
        if isinstance(folder, list):
            folder = " | ".join(folder)
        add(f"  Instrument folder: {folder or '(none: mixed-row check disabled)'}")
    tz = st.get("timezone") or {}
    if tz:
        add(f"  Local time zone: {'/'.join(tz.get('tzname', []))} "
            f"(UTC offset {tz.get('utc_offset_seconds', 0) / 3600:+.1f} h; "
            "mtime fallbacks use it)")

    if st.get("mixed_warning"):
        add("")
        add("!" * 72)
        add(f"WARNING: {st['mixed_rows']} of {st['rows']} CSV rows name a Source File outside the")
        add("instrument folder and none name a file inside it. The folder or its")
        add("aliases are probably wrong; check the 'Source File folders' list below.")
        add("!" * 72)

    add("")
    add("Counts")
    add(f"  CDFs read:                 {st['cdfs']}"
        + (f" of {st['files_seen']} files" if "files_seen" in st else ""))
    dts = st.get("dt_source")
    if dts:
        add(f"    injection time from CDF: {dts.get('cdf', 0)}; from file mtime: {dts.get('mtime', 0)}")
    add(f"  unreadable CDFs:           {len(st.get('cdf_errors', []))}")
    add(f"  identical bytes (dups):    {st['dup_sha']}")
    add(f"  same key, different bytes: {st['key_collisions']}"
        f"  (rows involved: {len(st['collided_rows'])})")
    add(f"  no injection time (mtime): {st['no_injection_time']}")
    add(f"  v1 misparsed time (CDFs):  {st['v1_misparsed_cdfs']}"
        f"  (rows matched only through v1's misparsed form: {st['rows_matched_via_v1_form']})")
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
    add(f"  near misses:               {st['near_misses']['count']}"
        "  (same lab ID, time off by <= 2 s or by whole hours)")
    add(f"  held rows (bad layout):    {st['held_rows']}")
    add(f"  mixed rows (other folder): {st['mixed_rows']}"
        f"  (of which a CDF here has the key: {len(st['mixed_but_key_matches_here'])})")
    add(f"  rows without Source File:  {st['rows_without_source_file']}")
    add(f"  non-canonical CSV times:   {st['rows_noncanonical_dt']['count']}")
    if st.get("csv_issues"):
        kinds: dict[str, int] = {}
        for i in st["csv_issues"]:
            kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        add("  CSV layout issues:         "
            + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))

    add("")
    add("Method names (detection_method_name) of the CDFs kept")
    for name, count in st.get("method_names", {}).items():
        add(f"  {count:6d}  {_method_label(name)}")
    if st.get("method_names_per_folder"):
        add("Per folder (CDFs read; method names)")
        for folder, count in st.get("cdfs_per_folder", {}).items():
            hist = st["method_names_per_folder"].get(folder, {})
            add(f"  {folder}: {count} CDFs; "
                + ", ".join(f"{_method_label(k)} {v}" for k, v in hist.items()))
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
    section("Same key, different bytes (kept, other)", report.key_collisions,
            lambda d: f"{d[0].path}  vs  {d[1].path}")
    section("No injection time in the CDF (mtime used)", report.no_injection_time, str)
    section("v1 misparsed injection time (path, stamp, v1 wrote, correct)",
            st.get("v1_misparsed_examples", []),
            lambda e: f"{e[0]}: {e[1]!r} -> v1 {e[2]}, correct {e[3]}")
    section("CDF with no sample name (file stem used)", st.get("name_from_filename", []), str)
    section("Attached samples", [s for s in report.samples if s.cdf is not None and s.rows],
            lambda s: f"{s.lab_id!r} @ {s.injection_dt}: {len(s.rows)} row(s)"
                      f"  [{s.cdf.path}]")
    section("Samples with revisions", [s for s in report.samples if len(s.rows) > 1],
            lambda s: f"{s.lab_id!r} @ {s.injection_dt}: {len(s.rows)} rows,"
                      f" lines {', '.join(str(r.line_no) for r in s.rows)}")
    section("Orphan CDFs", [s for s in report.samples if s.cdf is not None and not s.rows],
            lambda s: f"{s.lab_id!r} @ {s.injection_dt} ({s.dt_source}; "
                      f"{_method_label(s.cdf.method_name)})  [{s.cdf.path}]")
    section("Result-only samples / unmatched rows", report.unmatched_rows, _row_brief)
    section("Near misses (row, CDF)", st["near_misses"]["examples"],
            lambda e: f"line {e['line_no']}: {e['lab_id']!r} CSV {e['csv_dt']} vs CDF "
                      f"{e['cdf_dt']} ({e['delta_seconds']} s)  [{e['cdf_path']}]")
    section("Held rows (layout problems; not imported)", report.held_rows, _row_brief)
    section("Mixed rows (Source File outside the instrument folder)", report.mixed_rows,
            _row_brief)
    section("Non-canonical CSV times (line, value)", st["rows_noncanonical_dt"]["examples"],
            lambda e: f"line {e[0]}: {e[1]!r}")
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
