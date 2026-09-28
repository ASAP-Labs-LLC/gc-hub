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


def read_cdf_meta(path) -> CdfMeta:
    """Metadata of one CDF: the identity v1 used, plus the file's sha256.

    ``lab_id`` is the sample name exactly as ``distill.cdf_metadata`` returns
    it (the value v1 wrote to the CSV's ``Lab ID``). When the CDF has no
    parsable injection time, v1 used the file's mtime; so does this, with
    ``dt_source='mtime'`` so the caller can report it."""
    return _read_cdf_meta_ex(path)[0]
