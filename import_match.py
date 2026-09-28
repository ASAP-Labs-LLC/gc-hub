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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from netCDF4 import Dataset

import distill

_HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class CdfMeta:
    path: str
    sha256: str
    lab_id: str
    injection_dt: str   # canonical naive isoformat(sep=" ")
    dt_source: str      # 'cdf' | 'mtime'


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_cdf_names(path: Path) -> tuple[str, str, str]:
    """(sample_name as distill reads it, how the name was found, raw stamp).

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
    if name:
        return name, "cdf", raw_date
    return (path.stem or "Unknown"), "filename", raw_date


def _read_cdf_meta_ex(path) -> tuple[CdfMeta, str]:
    """``read_cdf_meta`` plus where the lab ID came from ('cdf'|'filename')."""
    p = Path(path)
    name, name_source, raw_date = _read_cdf_names(p)
    inj_dt = distill.parse_injection_datetime(raw_date)
    dt_source = "cdf"
    if inj_dt is None:
        inj_dt = datetime.fromtimestamp(p.stat().st_mtime)
        dt_source = "mtime"
    meta = CdfMeta(path=str(path), sha256=_sha256_file(p), lab_id=name,
                   injection_dt=inj_dt.isoformat(sep=" "), dt_source=dt_source)
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


def read_cdf_meta(path) -> CdfMeta:
    """Metadata of one CDF: the identity v1 used, plus the file's sha256.

    ``lab_id`` is the sample name exactly as ``distill.cdf_metadata`` returns
    it (the value v1 wrote to the CSV's ``Lab ID``). When the CDF has no
    parsable injection time, v1 used the file's mtime; so does this, with
    ``dt_source='mtime'`` so the caller can report it."""
    return _read_cdf_meta_ex(path)[0]
