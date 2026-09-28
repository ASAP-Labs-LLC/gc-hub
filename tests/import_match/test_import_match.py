"""Tests for the phase-2 history-import matcher (``import_match``, contract §3).

Synthetic ANDI/AIA-style CDFs are written with netCDF4 in each test's temp
folder: a small ``ordinate_values`` trace, ``actual_sampling_interval``, and
the metadata distill reads (``sample_name``, ``injection_date_time_stamp`` /
``injection_date`` / ``injection_time``) as global attributes or char
variables, like the real Agilent files.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

try:
    import numpy as np
    from netCDF4 import Dataset

    import distill
    HAVE_DEPS = True
except ImportError:  # pragma: no cover - bare interpreter
    HAVE_DEPS = False

if HAVE_DEPS:
    import import_match as im  # a missing module must fail, not skip


def write_cdf(path, *, sample_name="S1", stamp="20260924153027+0000",
              stamp_key="injection_date_time_stamp", as_variables=False,
              points=64, seed=0, mtime=None):
    """Write a small CDF shaped like ChemStation's export. ``stamp=None``
    leaves the injection time out entirely; ``sample_name=None`` likewise."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with Dataset(path, "w", format="NETCDF3_CLASSIC") as ds:
        ds.createDimension("point_number", points)
        ds.createDimension("_255_byte_string", 255)
        ds.dataset_completeness = "C1+C2"
        ds.retention_unit = "seconds"
        meta = {}
        if sample_name is not None:
            meta["sample_name"] = sample_name
        if stamp is not None:
            meta[stamp_key] = stamp
        for key, val in meta.items():
            if as_variables:
                v = ds.createVariable(key, "S1", ("_255_byte_string",))
                v.set_auto_chartostring(False)
                v[:] = np.frombuffer(val.encode().ljust(255, b"\x00"), dtype="S1")
            else:
                setattr(ds, key, val)
        interval = ds.createVariable("actual_sampling_interval", "f4")
        interval.assignValue(0.2)
        delay = ds.createVariable("actual_delay_time", "f4")
        delay.assignValue(0.0)
        y = ds.createVariable("ordinate_values", "f4", ("point_number",))
        rng = np.random.default_rng(seed)
        y[:] = rng.random(points).astype("f4") * 100.0
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


@unittest.skipUnless(HAVE_DEPS, "needs numpy + netCDF4")
class ReadCdfMetaTests(_TmpCase):
    def test_compact_stamp_with_zone_is_canonical_and_from_cdf(self) -> None:
        p = write_cdf(self.tmp / "a.CDF", sample_name="AF26", stamp="20260917142942+0000")
        meta = im.read_cdf_meta(p)
        self.assertEqual(meta.lab_id, "AF26")
        self.assertEqual(meta.injection_dt, "2026-09-17 14:29:42")
        self.assertEqual(meta.dt_source, "cdf")
        self.assertEqual(meta.sha256, sha(p))
        self.assertEqual(meta.path, str(p))

    def test_metadata_stored_as_char_variables(self) -> None:
        p = write_cdf(self.tmp / "v.CDF", sample_name="GAS test1",
                      stamp="20260629094842+0000", as_variables=True)
        meta = im.read_cdf_meta(p)
        self.assertEqual(meta.lab_id, "GAS test1")
        self.assertEqual(meta.injection_dt, "2026-06-29 09:48:42")
        self.assertEqual(meta.dt_source, "cdf")

    def test_other_stamp_keys_and_formats(self) -> None:
        p = write_cdf(self.tmp / "d.CDF", stamp="24-Feb-2022 15:09:25",
                      stamp_key="injection_date")
        self.assertEqual(im.read_cdf_meta(p).injection_dt, "2022-02-24 15:09:25")
        p2 = write_cdf(self.tmp / "t.CDF", stamp="2022-02-24T15:09:25",
                       stamp_key="injection_time")
        self.assertEqual(im.read_cdf_meta(p2).injection_dt, "2022-02-24 15:09:25")

    def test_missing_injection_time_falls_back_to_mtime(self) -> None:
        mt = datetime(2023, 1, 26, 18, 38, 52, 123456).timestamp()
        p = write_cdf(self.tmp / "m.CDF", stamp=None, mtime=mt)
        meta = im.read_cdf_meta(p)
        self.assertEqual(meta.dt_source, "mtime")
        self.assertEqual(meta.injection_dt, "2023-01-26 18:38:52.123456")

    def test_unparsable_injection_time_falls_back_to_mtime(self) -> None:
        mt = datetime(2024, 4, 10, 16, 57, 21).timestamp()
        p = write_cdf(self.tmp / "u.CDF", stamp="not a date", mtime=mt)
        meta = im.read_cdf_meta(p)
        self.assertEqual(meta.dt_source, "mtime")
        self.assertEqual(meta.injection_dt, "2024-04-10 16:57:21")

    def test_agrees_with_distill_cdf_metadata(self) -> None:
        for i, kw in enumerate([
            dict(sample_name="D2887-12  Std", stamp="20220224150925+0000"),
            dict(sample_name="(Blank)", stamp="20230126183852", as_variables=True),
            dict(sample_name=None, stamp="20230126183852"),
            dict(sample_name="x", stamp=None, mtime=1700000000.5),
        ]):
            p = write_cdf(self.tmp / f"cmp{i}.CDF", **kw)
            name, dt = distill.cdf_metadata(p)
            meta = im.read_cdf_meta(p)
            self.assertEqual((meta.lab_id, meta.injection_dt),
                             (name, dt.isoformat(sep=" ")), kw)

    def test_meta_is_frozen(self) -> None:
        meta = im.read_cdf_meta(write_cdf(self.tmp / "f.CDF"))
        with self.assertRaises(Exception):
            meta.lab_id = "other"  # type: ignore[misc]


# ── CSV helpers ────────────────────────────────────────────────────────────
# v1 before the Best Fit / Fit Score / Source File columns and the
# intermediate T40/T60 cuts (app._migrate_csv_header's "old header").
OLD_HEADER = (["Lab ID", "InjectionDateTime"]
              + ["2887 IBP", "2887 T5", "2887 T10", "2887 T20", "2887 T30", "2887 T50",
                 "2887 T70", "2887 T80", "2887 T90", "2887 T95", "2887 FBP"]
              + ["D86 IBP", "D86 T5", "D86 T10", "D86 T20", "D86 T30", "D86 T50",
                 "D86 T70", "D86 T80", "D86 T90", "D86 T95", "D86 FBP"])


def csv_line(cells) -> str:
    import csv as _csv
    import io
    buf = io.StringIO()
    _csv.writer(buf).writerow(cells)
    return buf.getvalue()


def full_row(lab_id, dt, source="", ibp="100.0", best_fit="Diesel"):
    vals = {c: "" for c in distill.CSV_HEADER}
    for i, c in enumerate(distill.CSV_HEADER[2:28]):
        vals[c] = f"{100 + i}.5"
    vals.update({"Lab ID": lab_id, "InjectionDateTime": dt, "2887 IBP": ibp,
                 "Best Fit": best_fit, "Fit Score": "0.950", "Source File": source})
    return [vals[c] for c in distill.CSV_HEADER]


def write_csv_text(path, text, *, bom=False) -> Path:
    path = Path(path)
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    return path


@unittest.skipUnless(HAVE_DEPS, "needs numpy + netCDF4")
class ReadResultsCsvTests(_TmpCase):
    def test_current_header(self) -> None:
        text = (csv_line(distill.CSV_HEADER)
                + csv_line(full_row("AF26", "2026-09-17 14:29:42", r"\\srv\p\AF26.CDF"))
                + csv_line(full_row(" AF27 ", "2026-09-17 14:50:18", ibp="99.99")))
        rows = im.read_results_csv(write_csv_text(self.tmp / "r.csv", text))
        self.assertEqual(len(rows), 2)
        r0, r1 = rows
        self.assertEqual((r0.line_no, r0.lab_id, r0.injection_dt_raw),
                         (2, "AF26", "2026-09-17 14:29:42"))
        self.assertEqual(r0.source_file, r"\\srv\p\AF26.CDF")
        self.assertEqual(r1.lab_id, "AF27")                 # stripped for identity
        self.assertEqual(r1.values["Lab ID"], " AF27 ")     # values verbatim
        self.assertEqual(r1.values["2887 IBP"], "99.99")
        self.assertEqual(r1.line_no, 3)
        self.assertEqual(list(r0.values), list(distill.CSV_HEADER))

    def test_old_header_rows_get_blank_new_columns(self) -> None:
        old = {c: f"{i}.0" for i, c in enumerate(OLD_HEADER)}
        old["Lab ID"], old["InjectionDateTime"] = "L1", "2022-03-07 15:28:09"
        text = csv_line(OLD_HEADER) + csv_line([old[c] for c in OLD_HEADER])
        (row,) = im.read_results_csv(write_csv_text(self.tmp / "o.csv", text))
        self.assertEqual(row.lab_id, "L1")
        self.assertEqual(row.values["2887 T50"], old["2887 T50"])
        self.assertEqual(row.values["D86 FBP"], old["D86 FBP"])
        for col in ("2887 T40", "2887 T60", "D86 T40", "D86 T60",
                    "Best Fit", "Fit Score", "Source File"):
            self.assertEqual(row.values[col], "", col)
        self.assertEqual(row.source_file, "")

    def test_new_rows_appended_under_an_old_header(self) -> None:
        old = ["L1", "2022-03-07 15:28:09"] + ["1.0"] * (len(OLD_HEADER) - 2)
        text = (csv_line(OLD_HEADER) + csv_line(old)
                + csv_line(full_row("L2", "2022-03-08 10:00:00", r"\\srv\p\L2.CDF")))
        rows, issues = im.read_results_csv_ex(write_csv_text(self.tmp / "m.csv", text))
        self.assertEqual([r.lab_id for r in rows], ["L1", "L2"])
        self.assertEqual(rows[1].source_file, r"\\srv\p\L2.CDF")
        self.assertEqual(rows[1].values["Best Fit"], "Diesel")
        self.assertTrue(any(i["kind"] == "full-width-row-under-old-header" for i in issues))

    def test_bom_blank_lines_and_repeated_header(self) -> None:
        text = (csv_line(distill.CSV_HEADER)
                + csv_line(full_row("A", "2026-01-01 00:00:01"))
                + "\r\n\r\n"
                + ",,,\r\n"
                + csv_line(distill.CSV_HEADER)
                + csv_line(full_row("B", "2026-01-01 00:00:02")))
        rows = im.read_results_csv(write_csv_text(self.tmp / "b.csv", text, bom=True))
        self.assertEqual([r.lab_id for r in rows], ["A", "B"])
        self.assertEqual([r.line_no for r in rows], [2, 7])
        self.assertNotIn("\ufeff", rows[0].values["Lab ID"])

    def test_old_header_repeated_mid_file_switches_columns(self) -> None:
        old = ["L1", "2022-03-07 15:28:09"] + ["1.0"] * (len(OLD_HEADER) - 2)
        text = (csv_line(distill.CSV_HEADER)
                + csv_line(full_row("A", "2026-01-01 00:00:01", "x.CDF"))
                + csv_line(OLD_HEADER) + csv_line(old))
        rows = im.read_results_csv(write_csv_text(self.tmp / "s.csv", text))
        self.assertEqual(rows[1].lab_id, "L1")
        self.assertEqual(rows[1].values["Source File"], "")
        self.assertEqual(rows[1].values["2887 T95"], "1.0")

    def test_headerless_file_uses_csv_header(self) -> None:
        text = csv_line(full_row("A", "2026-01-01 00:00:01", "a.CDF"))
        rows, issues = im.read_results_csv_ex(write_csv_text(self.tmp / "h.csv", text))
        self.assertEqual((rows[0].lab_id, rows[0].source_file), ("A", "a.CDF"))
        self.assertTrue(any(i["kind"] == "no-header" for i in issues))

    def test_short_and_long_records_are_padded_and_reported(self) -> None:
        text = (csv_line(distill.CSV_HEADER)
                + csv_line(["A", "2026-01-01 00:00:01", "1.0"])
                + csv_line(full_row("B", "2026-01-01 00:00:02") + ["extra"]))
        rows, issues = im.read_results_csv_ex(write_csv_text(self.tmp / "w.csv", text))
        self.assertEqual(rows[0].values["2887 IBP"], "1.0")
        self.assertEqual(rows[0].values["Source File"], "")
        self.assertEqual(rows[1].lab_id, "B")
        by_kind = {i["kind"]: i["line_no"] for i in issues}
        self.assertEqual(by_kind, {"short-row": 2, "long-row": 3})

    def test_empty_file(self) -> None:
        self.assertEqual(im.read_results_csv(write_csv_text(self.tmp / "e.csv", "")), [])


if __name__ == "__main__":
    unittest.main()
