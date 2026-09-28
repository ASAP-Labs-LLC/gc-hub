"""Shared helpers for the history-import tests (plan 2D).

Re-exports the pipeline tests' ``hub`` fixture (``tests/pipeline/pipeline_helpers.py``)
and adds: a v1 processed folder (``processed_cdfs2``-style, file names as v1's
Looker renamed them), v1 results rows written with ``csv.writer`` exactly as
v1 appended them, and store read-outs.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import stat
import sys
from datetime import datetime
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent
for _p in (TESTS_DIR.parent, TESTS_DIR, TESTS_DIR / "pipeline", TESTS_DIR / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from pipeline_helpers import SIMDIS, Hub, hub  # noqa: E402,F401  (hub: the fixture)

import cdf_fixtures as fx  # noqa: E402
import distill  # noqa: E402
import store  # noqa: E402

SHARE = ("\\\\ASAPServer\\Labsharedrive\\Ryan C\\GC Data\\GC2025\\"
         "GC 2026.5 2887 Advanced analysis\\webapp\\processed_cdfs2")
OTHER_SHARE = "\\\\ASAPServer\\Labsharedrive\\Ryan C\\GC Data\\GC2025\\GC2025.1\\processed_cdf"
ALIASES = ["processed_cdfs2"]


def v1_name(lab: str, dt: datetime) -> str:
    """v1's processed file name: ``<lab>_<MMDDYYYY>_<HHMMSS>.CDF``."""
    return f"{lab}_{dt.strftime('%m%d%Y_%H%M%S')}.CDF"


def sample(folder: Path, lab: str, dt: datetime, *, method=SIMDIS, shift=0.0, file_name=None,
           raw_stamp=None, sample_name=...) -> Path:
    """A sample CDF in ``folder`` (v1's file name unless given)."""
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / (file_name or v1_name(lab, dt))
    t = fx._axis()
    y = (fx.gaussian(t, 0.30, 50000, 0.01) + fx.gaussian(t, 3.2 + shift, 1200, 0.9)
         + fx.gaussian(t, 4.6 + shift, 600, 0.6)) + 40 + 5 * t
    name = lab if sample_name is ... else sample_name
    return fx.write_cdf(p, t, y, name, dt, method_name=method, raw_stamp=raw_stamp)


def blank(folder: Path, dt: datetime, *, name="Blank", method=SIMDIS) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return fx.blank_cdf(folder / v1_name(name, dt), injected=dt, name=name, method_name=method)


def row(lab: str, dt: str, *, source: str | None = None, ibp="251.63", best_fit="Mix",
        fit_score="0.647", **cells) -> dict:
    """A v1 results row (every CSV_HEADER cell a string, as v1's CSV holds it)."""
    out = {c: "" for c in distill.CSV_HEADER}
    nums = ["251.63", "327.01", "528.49", "685.41", "810.79", "951.73", "1121.58", "1256.02",
            "1363.61", "1507.82", "1649.9", "1806.3", "1993.72", "304.82", "498.8", "568.11",
            "705.19", "855.66", "974.13", "1088.54", "1212.09", "1331.59", "1428.02", "1567.16",
            "1696.78", "1698.84"]
    for col, v in zip(distill.CSV_HEADER[2:28], nums):
        out[col] = v
    out.update({"Lab ID": lab, "InjectionDateTime": dt, "2887 IBP": ibp, "Best Fit": best_fit,
                "Fit Score": fit_score})
    out["Source File"] = source if source is not None else ""
    out.update(cells)
    return out


def src(file_name: str, share: str = SHARE) -> str:
    return f"{share}\\{file_name}"


def csv_line(values: dict) -> str:
    buf = io.StringIO(newline="")
    csv.writer(buf).writerow([values.get(c, "") for c in distill.CSV_HEADER])
    return buf.getvalue()


def write_csv(path: Path, rows: list, *, header=True) -> Path:
    """The v1 results CSV: header, then ``rows`` in order (v1's ``_append_csv_row``)."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if header:
            w.writerow(distill.CSV_HEADER)
        for r in rows:
            w.writerow([r.get(c, "") for c in distill.CSV_HEADER])
    return Path(path)


def append_csv(path: Path, rows: list) -> None:
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for r in rows:
            w.writerow([r.get(c, "") for c in distill.CSV_HEADER])


def csv_lines(path: Path) -> list[str]:
    """Every physical record of the CSV (after the header) as v1 wrote it."""
    text = Path(path).read_bytes().decode("utf-8")
    return [ln + "\r\n" for ln in text.split("\r\n")[1:] if ln]


def samples(hub: Hub, instrument="gc1") -> list[dict]:
    return store.samples.search(instrument=instrument, limit=100000, db=hub.db)


def by_lab(hub: Hub, instrument="gc1") -> dict:
    out: dict = {}
    for s in samples(hub, instrument):
        out.setdefault(s["lab_id"], []).append(s)
    return out


def one(hub: Hub, lab: str, instrument="gc1") -> dict:
    got = by_lab(hub, instrument).get(lab, [])
    assert len(got) == 1, f"{lab}: {len(got)} samples"
    return got[0]


def revisions(hub: Hub, sample_id: int) -> list[dict]:
    return store.list_revisions(sample_id, db=hub.db)


def table_counts(hub: Hub) -> dict:
    with store.connection(hub.db) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("samples", "sample_results", "conflicts", "export_rows", "jobs")}


def db_dump(hub: Hub) -> dict:
    """Every row of the tables the importer may touch (for 'nothing changed')."""
    with store.connection(hub.db) as conn:
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
                for t in ("samples", "sample_results", "conflicts", "export_rows", "jobs")}


def tree(root: Path) -> dict:
    """Every file under ``root`` with its content hash, size and mtime."""
    out = {}
    root = Path(root)
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name not in ("gc.db", "gc.db-wal", "gc.db-shm"):
            st = p.stat()
            out[str(p.relative_to(root))] = (hashlib.sha256(p.read_bytes()).hexdigest(),
                                             st.st_size, st.st_mtime_ns,
                                             stat.S_IMODE(st.st_mode))
    return out


def results(rev: dict) -> dict:
    return json.loads(rev["results"])
