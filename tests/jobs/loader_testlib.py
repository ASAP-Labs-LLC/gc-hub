"""Shared helpers for the folder-loader and parity-report tests (2A1 T6).

Re-exports the pipeline tests' ``hub`` fixture (``tests/pipeline/pipeline_helpers.py``)
and adds what these tests need on top: a source folder of CDFs, a snapshot of
it (to prove the loader never writes there), and v1 results rows computed the
way v1's ``process_cdf`` computed them (non-strict ``distill.compute``, the
corrections file loaded leniently, an absolute ``Source File`` and v1's
possibly misparsed ``InjectionDateTime``).
"""
from __future__ import annotations

import csv
import hashlib
import os
import stat
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent
for _p in (TESTS_DIR.parent, TESTS_DIR, TESTS_DIR / "pipeline", TESTS_DIR / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from pipeline_helpers import SIMDIS, Hub, hub  # noqa: E402,F401  (hub: the fixture)

import cdf_fixtures as fx  # noqa: E402
import distill  # noqa: E402
import instruments  # noqa: E402
import store  # noqa: E402

V1_PROCESSED = "C:\\GC\\processed_cdf"


def snapshot(root: Path) -> dict:
    """Every path under ``root`` with its mode, size, mtime and content hash."""
    out = {}
    for p in sorted(Path(root).rglob("*")):
        st = p.lstat()
        entry = {"mode": stat.S_IMODE(st.st_mode), "mtime_ns": st.st_mtime_ns}
        if p.is_file():
            entry["sha"] = hashlib.sha256(p.read_bytes()).hexdigest()
            entry["size"] = st.st_size
        out[str(p.relative_to(root))] = entry
    return out


def make_read_only(root: Path) -> None:
    for p in sorted(Path(root).rglob("*"), reverse=True):
        os.chmod(p, 0o555 if p.is_dir() else 0o444)
    os.chmod(root, 0o555)


def make_writable(root: Path) -> None:
    os.chmod(root, 0o755)
    for p in sorted(Path(root).rglob("*")):
        os.chmod(p, 0o755 if p.is_dir() else 0o644)


def plain_blank(path, injected, *, ramp=5.0, offset=40.0, name="Blank", method_name=SIMDIS):
    """A genuine blank whose bleed ramp can differ from ``cdf_fixtures.blank_cdf``'s,
    so subtracting it gives different numbers."""
    t = fx._axis()
    y = fx.gaussian(t, 0.30, 50000, 0.01) + offset + ramp * t
    return fx.write_cdf(path, t, y, name, injected, method_name=method_name)


def v1_row(hub: Hub, cdf: Path, *, blank: Path | None = None, instrument: str = "gc1",
           injection_dt: str | None = None, corrections_file: Path | None = None,
           auto: bool = False) -> dict:
    """The row v1 would have written for ``cdf`` with ``blank`` subtracted:
    the numbers from a non-strict ``distill.compute`` (v1's), the corrections
    file read leniently (v1), ``InjectionDateTime`` as v1 parsed the stamp
    (unless given) and an absolute ``Source File``. ``auto``: v1 had no
    usable peak assignments and auto-detected the calibration peaks."""
    inst = store.instruments.get(instrument, db=hub.db) or store.instruments.get("gc1", db=hub.db)
    ctx = instruments.context(inst, hub.conf, data_dir=hub.data)
    if not (ctx.get("calibration_cdf") or "").strip():
        ctx = instruments.context(store.instruments.get("gc1", db=hub.db), hub.conf, data_dir=hub.data)
    if auto:
        ctx["calibration_assignments"] = ""
    corr = distill.load_d86_corrections(corrections_file or hub.conf["correction_factors_json"])
    result = distill.compute(cdf, ctx, blank, corrections=corr, honour_env=False, allow_auto=True)
    row = dict(result["row"])
    # v1 reported the corrected D86 as it fell (no v7.0.0 hold)
    v1_d86 = distill.apply_d86_corrections(result["d86_uncorrected"], corr)
    for cut, col in zip(distill.D86_ORDER, distill.CSV_HEADER[15:28]):
        row[col] = v1_d86.get(cut, "")
    if injection_dt is None:
        _name, raw_stamp, _method = distill.read_cdf_names(cdf)
        v1 = distill.v1_parse_injection_datetime(raw_stamp)
        injection_dt = v1.isoformat(sep=" ") if v1 is not None else row["InjectionDateTime"]
    row["InjectionDateTime"] = injection_dt
    row["Source File"] = f"{V1_PROCESSED}\\{Path(cdf).stem}_v1.CDF"
    return row


def write_v1_csv(path: Path, rows: list) -> Path:
    """The v1 results CSV: header, then ``rows`` (dicts keyed by CSV_HEADER) in order."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(distill.CSV_HEADER)
        for r in rows:
            w.writerow([r.get(c, "") for c in distill.CSV_HEADER])
    return Path(path)
