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
              points=64, seed=0, mtime=None, method=None):
    """Write a small CDF shaped like ChemStation's export. ``stamp=None``
    leaves the injection time out entirely; ``sample_name=None`` likewise."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with Dataset(path, "w", format="NETCDF3_CLASSIC") as ds:
        ds.createDimension("point_number", points)
        ds.createDimension("_255_byte_string", 255)
        ds.dataset_completeness = "C1+C2"
        ds.retention_unit = "seconds"
        if method is not None:
            ds.detection_method_name = method
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


# v1's parse_injection_datetime tries datetime.fromisoformat first. On Python
# >= 3.11 that accepts some compact ANDI stamps and misreads them (the 9th
# character becomes the date/time separator). (stamp, v1 on >=3.11, correct)
MISPARSE_CASES = [
    ("20260924010203+0000", "2026-09-24 10:20:00", "2026-09-24 01:02:03"),
    ("20260924100000+0000", "2026-09-24 00:00:00", "2026-09-24 10:00:00"),
    ("20260925002450+0000", "2026-09-25 02:45:00", "2026-09-25 00:24:50"),
    ("20260925002450Z", "2026-09-25 02:45:00", "2026-09-25 00:24:50"),
]
UNAFFECTED_CASES = [
    ("20260924153027+0000", "2026-09-24 15:30:27"),
    ("20220224150925+0000", "2022-02-24 15:09:25"),
    ("20260924153027", "2026-09-24 15:30:27"),
    ("2026-09-24T15:30:27", "2026-09-24 15:30:27"),
    ("2026-09-24 15:30:27+05:00", "2026-09-24 15:30:27"),
    ("24-Feb-2022 15:09:25", "2022-02-24 15:09:25"),
    ("02/24/2022 15:09:25", "2022-02-24 15:09:25"),
]


@unittest.skipUnless(HAVE_DEPS and sys.version_info >= (3, 11),
                     "needs numpy + netCDF4, and the Python >= 3.11 fromisoformat")
class InjectionTimeParseTests(unittest.TestCase):
    def test_v1_parse_is_bug_for_bug(self) -> None:
        for stamp, legacy, _correct in MISPARSE_CASES:
            self.assertEqual(im._v1_parse(stamp).isoformat(sep=" "), legacy, stamp)
        for stamp, good in UNAFFECTED_CASES:
            self.assertEqual(im._v1_parse(stamp).isoformat(sep=" "), good, stamp)
        self.assertEqual(im._v1_parse("20260924").isoformat(sep=" "), "2026-09-24 00:00:00")
        self.assertIsNone(im._v1_parse("20260924153027Z"))    # v1 fell back to mtime
        self.assertIsNone(im._v1_parse(""))

    def test_correct_parse(self) -> None:
        for stamp, _legacy, correct in MISPARSE_CASES:
            self.assertEqual(im._correct_parse(stamp).isoformat(sep=" "), correct, stamp)
        for stamp, good in UNAFFECTED_CASES:
            self.assertEqual(im._correct_parse(stamp).isoformat(sep=" "), good, stamp)
        self.assertEqual(im._correct_parse("20260924153027Z").isoformat(sep=" "),
                         "2026-09-24 15:30:27")
        self.assertEqual(im._correct_parse("20260924100000 +0000").isoformat(sep=" "),
                         "2026-09-24 10:00:00")
        self.assertIsNone(im._correct_parse("20260924"))       # no '-': not ISO, no time
        self.assertIsNone(im._correct_parse("garbage"))
        self.assertIsNone(im._correct_parse(""))


@unittest.skipUnless(HAVE_DEPS and sys.version_info >= (3, 11),
                     "needs numpy + netCDF4, and the Python >= 3.11 fromisoformat")
class CdfMetaLegacyTimeTests(_TmpCase):
    def test_misparsed_stamps_carry_both_forms(self) -> None:
        for i, (stamp, legacy, correct) in enumerate(MISPARSE_CASES):
            meta = im.read_cdf_meta(write_cdf(self.tmp / f"m{i}.CDF", stamp=stamp))
            self.assertEqual(meta.injection_dt, correct, stamp)
            self.assertEqual(meta.legacy_injection_dt, legacy, stamp)
            self.assertEqual(meta.dt_source, "cdf")
            self.assertEqual(meta.raw_stamp, stamp)
            self.assertEqual(meta.v1_injection_dts, (legacy, correct))

    def test_unaffected_stamp_has_one_form(self) -> None:
        meta = im.read_cdf_meta(write_cdf(self.tmp / "u.CDF", stamp="20260924153027+0000"))
        self.assertEqual(meta.legacy_injection_dt, meta.injection_dt)
        self.assertEqual(meta.v1_injection_dts, ("2026-09-24 15:30:27",))

    def test_date_only_stamp(self) -> None:
        mt = datetime(2026, 9, 24, 8, 0, 1).timestamp()
        meta = im.read_cdf_meta(write_cdf(self.tmp / "d.CDF", stamp="20260924", mtime=mt))
        self.assertEqual(meta.legacy_injection_dt, "2026-09-24 00:00:00")
        self.assertEqual(meta.injection_dt, "2026-09-24 08:00:01")
        self.assertEqual(meta.dt_source, "mtime")
        self.assertEqual(meta.v1_injection_dts, ("2026-09-24 00:00:00", "2026-09-24 08:00:01"))

    def test_z_stamp_v1_used_mtime(self) -> None:
        mt = datetime(2026, 9, 24, 16, 0, 0).timestamp()
        meta = im.read_cdf_meta(write_cdf(self.tmp / "z.CDF", stamp="20260924153027Z", mtime=mt))
        self.assertEqual(meta.injection_dt, "2026-09-24 15:30:27")
        self.assertEqual(meta.dt_source, "cdf")
        self.assertEqual(meta.legacy_injection_dt, "2026-09-24 16:00:00")
    # The legacy form is pinned by literal values above, not by comparing
    # with distill: 2A1 fixes distill's parse, and v1's CSV keeps the bug.


@unittest.skipUnless(HAVE_DEPS, "needs numpy + netCDF4")
class MethodNameTests(_TmpCase):
    def test_method_name_forms(self) -> None:
        cases = [("SIMDISB.M", "SIMDISB.M"), ("  simdistb.m ", "SIMDISTB.M"),
                 (r"C:\CHEM32\1\METHODS\SimDisB.M", "SIMDISB.M"),
                 ("/methods/d7096.m", "D7096.M"), (None, "")]
        for i, (raw, want) in enumerate(cases):
            meta = im.read_cdf_meta(write_cdf(self.tmp / f"k{i}.CDF", method=raw))
            self.assertEqual(meta.method_name, want, raw)

    def test_normalise_lab_id_strips_only(self) -> None:
        self.assertEqual(im.normalise_lab_id("  D2887-12  Std "), "D2887-12  Std")
        self.assertEqual(im.normalise_lab_id("(Blank)"), "(Blank)")


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


# ── match() works on plain values; no files needed ─────────────────────────
FOLDER = r"\\ASAPServer\Labsharedrive\Ryan C\GC Data\GC2025\GC2025.1\processed_cdf"
OTHER = r"\\ASAPServer\Labsharedrive\Ryan C\GC Data\GC2025\GC 2026.5 2887 Advanced analysis\webapp\processed_cdfs2"


def mk_cdf(lab, dt, *, name=None, sha=None, source="cdf"):
    name = name or f"{lab}_{dt[:10]}.CDF"
    return im.CdfMeta(path=f"/local/copy/{name}", sha256=sha or hashlib.sha256(
        f"{lab}|{dt}|{name}".encode()).hexdigest(), lab_id=lab, injection_dt=dt,
        dt_source=source)


_LINE = [1]


def mk_row(lab, dt, *, source=None, line=None, ibp="1.0"):
    if line is None:
        _LINE[0] += 1
        line = _LINE[0]
    if source is None:
        source = FOLDER + "\\" + f"{lab}_x.CDF"
    values = {c: "" for c in distill.CSV_HEADER}
    values.update({"Lab ID": lab, "InjectionDateTime": dt, "2887 IBP": ibp,
                   "Source File": source})
    return im.CsvRow(line_no=line, lab_id=lab.strip(), injection_dt_raw=dt.strip(),
                     source_file=source.strip(), values=values)


def by_key(report):
    out = {}
    for s in report.samples:
        key = ((s.cdf.lab_id.strip(), s.cdf.injection_dt) if s.cdf
               else (s.rows[0].lab_id, s.rows[0].injection_dt_raw))
        out[key] = s
    return out


@unittest.skipUnless(HAVE_DEPS, "needs numpy + netCDF4")
class MatchTests(unittest.TestCase):
    def test_exact_match_attaches_the_row(self) -> None:
        c = mk_cdf("AF26", "2026-09-17 14:29:42")
        r = mk_row("AF26", "2026-09-17 14:29:42")
        rep = im.match([c], [r], instrument_folder=FOLDER)
        self.assertEqual(len(rep.samples), 1)
        self.assertIs(rep.samples[0].cdf, c)
        self.assertEqual(rep.samples[0].rows, [r])
        self.assertEqual((rep.unmatched_rows, rep.dup_sha, rep.no_injection_time,
                          rep.mixed_rows), ([], [], [], []))
        self.assertEqual(rep.stats["attached_samples"], 1)
        self.assertEqual(rep.stats["rows_attached"], 1)

    def test_cdf_lab_id_is_compared_stripped(self) -> None:
        c = mk_cdf("D2887-12  Std ", "2022-02-24 15:09:25")
        r = mk_row("D2887-12  Std", "2022-02-24 15:09:25")
        rep = im.match([c], [r], instrument_folder=FOLDER)
        self.assertEqual(rep.samples[0].rows, [r])

    def test_repeated_rows_become_revisions_in_csv_order(self) -> None:
        c = mk_cdf("S1", "2026-01-02 03:04:05")
        r1 = mk_row("S1", "2026-01-02 03:04:05", line=10, ibp="1")
        other = mk_row("S2", "2026-01-02 04:00:00", line=11)
        r2 = mk_row("S1", "2026-01-02 03:04:05", line=12, ibp="2")
        r3 = mk_row("S1", "2026-01-02 03:04:05", line=30, ibp="3")
        rep = im.match([c], [r1, other, r2, r3], instrument_folder=FOLDER)
        s = by_key(rep)[("S1", "2026-01-02 03:04:05")]
        self.assertEqual([r.values["2887 IBP"] for r in s.rows], ["1", "2", "3"])
        self.assertEqual(rep.stats["revisions_extra"], 2)

    def test_unmatched_rows_are_result_only_samples_and_listed(self) -> None:
        r1 = mk_row("GONE", "2025-05-05 05:05:05", line=2)
        r2 = mk_row("GONE", "2025-05-05 05:05:05", line=3)
        rep = im.match([], [r1, r2], instrument_folder=FOLDER)
        self.assertEqual(len(rep.samples), 1)
        self.assertIsNone(rep.samples[0].cdf)
        self.assertEqual(rep.samples[0].rows, [r1, r2])
        self.assertEqual(rep.unmatched_rows, [r1, r2])
        self.assertEqual(rep.stats["result_only_samples"], 1)

    def test_cdf_without_rows_is_an_orphan(self) -> None:
        c = mk_cdf("ORPH", "2024-01-01 00:00:00")
        rep = im.match([c], [], instrument_folder=FOLDER)
        self.assertEqual(len(rep.samples), 1)
        self.assertIs(rep.samples[0].cdf, c)
        self.assertEqual(rep.samples[0].rows, [])
        self.assertEqual(rep.stats["orphan_cdfs"], 1)

    def test_identical_bytes_are_reported_once_and_not_duplicated(self) -> None:
        a = mk_cdf("S1", "2026-01-01 00:00:00", name="S1_a.CDF", sha="ab" * 32)
        b = mk_cdf("S1", "2026-01-01 00:00:00", name="S1_b.CDF", sha="ab" * 32)
        r = mk_row("S1", "2026-01-01 00:00:00")
        rep = im.match([b, a], [r], instrument_folder=FOLDER)
        self.assertEqual(rep.dup_sha, [(a.path, b.path)])
        self.assertEqual(len(rep.samples), 1)
        self.assertEqual(rep.samples[0].cdf.path, a.path)
        self.assertEqual(rep.samples[0].rows, [r])

    def test_mtime_fallback_is_listed_and_matches_only_on_equal_string(self) -> None:
        c = mk_cdf("NOTIME", "2023-01-26 18:38:52.123456", source="mtime")
        near = mk_row("NOTIME", "2023-01-26 18:38:52")
        rep = im.match([c], [near], instrument_folder=FOLDER)
        self.assertEqual(rep.no_injection_time, [c.path])
        s = by_key(rep)
        self.assertEqual(s[("NOTIME", "2023-01-26 18:38:52.123456")].rows, [])
        self.assertEqual(rep.unmatched_rows, [near])

        exact = mk_row("NOTIME", "2023-01-26 18:38:52.123456")
        rep2 = im.match([c], [exact], instrument_folder=FOLDER)
        self.assertEqual(rep2.samples[0].rows, [exact])
        self.assertEqual(rep2.no_injection_time, [c.path])

    def test_prefix_trap_source_file_is_never_trusted(self) -> None:
        # app._migrate_csv_header back-filled Source File by filename prefix:
        # lab "123" got 1234's file. The CDF's own metadata says 1234.
        c = mk_cdf("1234", "2026-03-03 10:00:00", name="1234_03032026_100000.CDF")
        r = mk_row("123", "2026-03-03 10:00:00",
                   source=FOLDER + r"\1234_03032026_100000.CDF")
        rep = im.match([c], [r], instrument_folder=FOLDER)
        s = by_key(rep)
        self.assertEqual(s[("1234", "2026-03-03 10:00:00")].rows, [])
        self.assertIsNone(s[("123", "2026-03-03 10:00:00")].cdf)
        self.assertEqual(rep.unmatched_rows, [r])

    def test_same_lab_id_different_time_are_different_samples(self) -> None:
        c1 = mk_cdf("AF26", "2026-09-17 14:29:42")
        c2 = mk_cdf("AF26", "2026-09-17 14:50:18")
        r1 = mk_row("AF26", "2026-09-17 14:29:42")
        r2 = mk_row("AF26", "2026-09-17 14:50:18")
        r3 = mk_row("AF26", "2026-09-17 15:00:00")
        rep = im.match([c2, c1], [r2, r3, r1], instrument_folder=FOLDER)
        s = by_key(rep)
        self.assertEqual(s[("AF26", "2026-09-17 14:29:42")].rows, [r1])
        self.assertEqual(s[("AF26", "2026-09-17 14:50:18")].rows, [r2])
        self.assertIsNone(s[("AF26", "2026-09-17 15:00:00")].cdf)
        self.assertEqual(rep.stats["unmatched_same_lab_other_time"], 1)

    def test_rows_from_another_folder_are_mixed_not_attached(self) -> None:
        c = mk_cdf("AF26", "2026-09-17 14:29:42")
        mine_upper = mk_row("AF26", "2026-09-17 14:29:42",
                            source=FOLDER.upper() + r"\AF26_09172026_142942.CDF")
        theirs = mk_row("AF26", "2026-09-17 14:29:42",
                        source=OTHER + r"\AF26_09172026_142942.CDF")
        slashed = mk_row("X", "2026-01-01 00:00:00",
                         source=FOLDER.replace("\\", "/") + "/X.CDF")
        sibling = mk_row("Y", "2026-01-01 00:00:00", source=FOLDER + r"2\Y.CDF")
        no_src = mk_row("Z", "2026-01-01 00:00:00", source="")
        rep = im.match([c], [mine_upper, theirs, slashed, sibling, no_src],
                       instrument_folder=FOLDER + "\\")
        self.assertEqual(rep.mixed_rows, [theirs, sibling])
        s = by_key(rep)
        self.assertEqual(s[("AF26", "2026-09-17 14:29:42")].rows, [mine_upper])
        self.assertNotIn(theirs, rep.unmatched_rows)
        self.assertEqual(rep.unmatched_rows, [slashed, no_src])
        self.assertEqual(rep.stats["rows_without_source_file"], 1)

    def test_rows_without_a_key_are_listed_but_not_samples(self) -> None:
        r1 = mk_row("", "2026-01-01 00:00:00")
        r2 = mk_row("L", "")
        rep = im.match([], [r1, r2], instrument_folder=FOLDER)
        self.assertEqual(rep.samples, [])
        self.assertEqual(rep.unmatched_rows, [r1, r2])
        self.assertEqual(rep.stats["rows_missing_key"], 2)

    def test_two_different_cdfs_with_one_key_are_a_collision(self) -> None:
        a = mk_cdf("S", "2026-01-01 00:00:00", name="a.CDF")
        b = mk_cdf("S", "2026-01-01 00:00:00", name="b.CDF")
        r = mk_row("S", "2026-01-01 00:00:00")
        rep = im.match([b, a], [r], instrument_folder=FOLDER)
        self.assertEqual(len(rep.samples), 1)
        self.assertEqual(rep.samples[0].cdf.path, a.path)
        self.assertEqual(rep.samples[0].rows, [r])
        self.assertEqual(rep.stats["key_collisions"], [(a.path, b.path)])

    def test_output_is_deterministic_for_any_input_order(self) -> None:
        import random
        cdfs = [mk_cdf(f"L{i % 7}", f"2026-01-{1 + i % 9:02d} 00:00:{i:02d}") for i in range(30)]
        cdfs.append(mk_cdf("L0", "2026-01-01 00:00:00", name="dup.CDF", sha=cdfs[0].sha256))
        rows = [mk_row(c.lab_id, c.injection_dt, line=100 + i) for i, c in enumerate(cdfs[:20])]
        rows += [mk_row("R", f"2025-01-01 00:00:{i:02d}", line=200 + i) for i in range(5)]
        rows += [mk_row("M", "2025-01-01 00:00:00", source=OTHER + r"\M.CDF", line=300)]
        base = im.match(cdfs, rows, instrument_folder=FOLDER)
        rng = random.Random(7)
        for _ in range(5):
            c2, r2 = cdfs[:], rows[:]
            rng.shuffle(c2)
            rng.shuffle(r2)
            r2.sort(key=lambda r: r.line_no)  # CSV order is the file's order
            rep = im.match(c2, r2, instrument_folder=FOLDER)
            self.assertEqual(rep.samples, base.samples)
            self.assertEqual(rep.dup_sha, base.dup_sha)
            self.assertEqual(rep.unmatched_rows, base.unmatched_rows)
            self.assertEqual(rep.stats, base.stats)
        keys = [(s.cdf.injection_dt if s.cdf else s.rows[0].injection_dt_raw) for s in base.samples]
        self.assertEqual(keys, sorted(keys))

    def test_stats_add_up(self) -> None:
        cdfs = [mk_cdf("A", "2026-01-01 00:00:00"), mk_cdf("B", "2026-01-02 00:00:00")]
        rows = [mk_row("A", "2026-01-01 00:00:00"), mk_row("A", "2026-01-01 00:00:00"),
                mk_row("C", "2026-01-03 00:00:00"),
                mk_row("D", "2026-01-04 00:00:00", source=OTHER + r"\D.CDF")]
        st = im.match(cdfs, rows, instrument_folder=FOLDER).stats
        self.assertEqual(st["cdfs"], 2)
        self.assertEqual(st["rows"], 4)
        self.assertEqual(st["samples"], 3)
        self.assertEqual(st["attached_samples"], 1)
        self.assertEqual(st["orphan_cdfs"], 1)
        self.assertEqual(st["result_only_samples"], 1)
        self.assertEqual(st["rows_attached"], 2)
        self.assertEqual(st["unmatched_rows"], 1)
        self.assertEqual(st["mixed_rows"], 1)
        self.assertEqual(st["rows"], st["rows_attached"] + st["unmatched_rows"] + st["mixed_rows"])


def snapshot_tree(root: Path) -> dict:
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


class _FolderFixture(_TmpCase):
    def _fixture(self):
        proc = self.tmp / "processed_cdf"
        write_cdf(proc / "AF26_09172026_142942.CDF", sample_name="AF26",
                  stamp="20260917142942+0000", seed=1)
        write_cdf(proc / "AF26_copy.cdf", sample_name="AF26",
                  stamp="20260917142942+0000", seed=1)          # same bytes
        write_cdf(proc / "sub" / "ORPH.CDF", sample_name="ORPH",
                  stamp="20240101000000", seed=2)
        write_cdf(proc / "notime.CDF", sample_name="NT", stamp=None, seed=3,
                  mtime=datetime(2023, 1, 26, 18, 38, 52).timestamp())
        (proc / "broken.CDF").write_bytes(b"not a netcdf file")
        (proc / ".processed_index.json").write_text('{"entries": []}')
        text = (csv_line(distill.CSV_HEADER)
                + csv_line(full_row("AF26", "2026-09-17 14:29:42", FOLDER + r"\AF26_09172026_142942.CDF"))
                + csv_line(full_row("AF26", "2026-09-17 14:29:42", FOLDER + r"\AF26_09172026_142942.CDF", ibp="2"))
                + csv_line(full_row("NT", "2023-01-26 18:38:52", FOLDER + r"\NT.CDF"))
                + csv_line(full_row("GONE", "2022-01-01 00:00:00", FOLDER + r"\GONE.CDF"))
                + csv_line(full_row("THEIRS", "2026-09-17 17:28:12", OTHER + r"\THEIRS.CDF")))
        results = write_csv_text(self.tmp / "distill_results.csv", text)
        return proc, results


@unittest.skipUnless(HAVE_DEPS, "needs numpy + netCDF4")
class DryRunTests(_FolderFixture):
    def test_end_to_end(self) -> None:
        proc, results = self._fixture()
        rep = im.dry_run(proc, results, instrument_folder=FOLDER)
        st = rep.stats
        self.assertEqual(st["cdfs"], 4)                 # broken one is an error
        self.assertEqual(len(st["cdf_errors"]), 1)
        self.assertIn("broken.CDF", st["cdf_errors"][0][0])
        self.assertEqual(len(rep.dup_sha), 1)
        self.assertEqual(len(rep.no_injection_time), 1)
        self.assertTrue(rep.no_injection_time[0].endswith("notime.CDF"))
        s = by_key(rep)
        self.assertEqual(len(s[("AF26", "2026-09-17 14:29:42")].rows), 2)
        self.assertEqual(s[("ORPH", "2024-01-01 00:00:00")].rows, [])   # found in sub/
        self.assertEqual(s[("NT", "2023-01-26 18:38:52")].rows[0].lab_id, "NT")
        self.assertIsNone(s[("GONE", "2022-01-01 00:00:00")].cdf)
        self.assertEqual([r.lab_id for r in rep.mixed_rows], ["THEIRS"])
        self.assertEqual(st["dt_source"], {"cdf": 3, "mtime": 1})
        self.assertEqual(st["csv_issues"], [])

    def test_cdf_only_mode(self) -> None:
        proc, _ = self._fixture()
        rep = im.dry_run(proc, None, instrument_folder=FOLDER)
        self.assertEqual(rep.stats["rows"], 0)
        self.assertTrue(all(not s.rows for s in rep.samples))
        self.assertEqual(rep.stats["orphan_cdfs"], 3)

    def test_never_writes_to_the_source(self) -> None:
        proc, results = self._fixture()
        before = snapshot_tree(self.tmp)
        im.dry_run(proc, results, instrument_folder=FOLDER)
        self.assertEqual(snapshot_tree(self.tmp), before)

    def test_progress_callback(self) -> None:
        proc, results = self._fixture()
        seen = []
        im.dry_run(proc, results, instrument_folder=FOLDER,
                   on_progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen[-1], (5, 5))

    def test_name_from_filename_is_reported(self) -> None:
        proc = self.tmp / "p"
        write_cdf(proc / "NoName_01012026.CDF", sample_name=None, stamp="20260101000000")
        rep = im.dry_run(proc, None, instrument_folder=FOLDER)
        self.assertEqual(len(rep.stats["name_from_filename"]), 1)
        self.assertEqual(rep.samples[0].cdf.lab_id, "NoName_01012026")

    def test_summary_mentions_every_class(self) -> None:
        proc, results = self._fixture()
        rep = im.dry_run(proc, results, instrument_folder=FOLDER)
        text = im.format_summary(rep, examples=3)
        for needle in ("CDFs", "attached", "orphan", "result-only", "revisions",
                       "identical bytes", "no injection time", "mixed", "unreadable",
                       "AF26", "GONE", "THEIRS", "notime.CDF", "broken.CDF"):
            self.assertIn(needle, text)

    def test_report_serialises_to_json(self) -> None:
        import json
        proc, results = self._fixture()
        rep = im.dry_run(proc, results, instrument_folder=FOLDER)
        data = json.loads(json.dumps(im.report_to_dict(rep)))
        self.assertEqual(data["stats"]["cdfs"], 4)
        self.assertEqual(len(data["samples"]), rep.stats["samples"])


class ProcessedIndexTests(_TmpCase):
    def test_summary(self) -> None:
        import json
        entries = [["(Blank)", "2023-01-26 18:38:52"], ["(Blank)", "2023-01-26 18:54:55"],
                   ["001", "2024-04-10 16:57:21"], ["001 ", "2024-04-10 16:57:21"],
                   ["Blank", "2026-09-24 15:30:27"], ["cal std", "2026-07-21 12:58:07.123456"],
                   ["X", "07/21/2026"], ["001", "2024-04-10 16:57:21"]]
        p = self.tmp / ".processed_index.json"
        p.write_text(json.dumps({"entries": entries}))
        s = im.summarize_processed_index(p)
        self.assertEqual(s["entries"], 8)
        self.assertEqual(s["exact_duplicates"], 1)
        self.assertEqual(s["duplicates_after_strip"], 2)
        self.assertEqual(s["canonical_seconds"], 6)
        self.assertEqual(s["canonical_microseconds"], 1)
        self.assertEqual(s["non_canonical"], 1)
        self.assertEqual(s["date_min"], "2023-01-26 18:38:52")
        self.assertEqual(s["date_max"], "2026-09-24 15:30:27")
        self.assertEqual(s["blank_like_names"], {"(Blank)": 2, "Blank": 1})
        self.assertEqual(s["names_with_outer_whitespace"], 1)
        self.assertEqual(s["lab_ids_with_several_times"], 1)   # (Blank)


CLI = WEBAPP_DIR / "tools" / "import_dry_run.py"


@unittest.skipUnless(HAVE_DEPS, "needs numpy + netCDF4")
class CliTests(_FolderFixture):
    def _run(self, *args):
        import subprocess
        return subprocess.run([sys.executable, str(CLI), *map(str, args)],
                              capture_output=True, text=True, timeout=120,
                              cwd=str(self.tmp))

    def test_prints_summary_and_writes_json(self) -> None:
        import json
        proc, results = self._fixture()
        (proc / ".processed_index.json").write_text(
            json.dumps({"entries": [["(Blank)", "2023-01-26 18:38:52"]]}))
        before = snapshot_tree(proc)
        out_json = self.tmp / "out" / "report.json"
        res = self._run("--processed-dir", proc, "--results-csv", results,
                        "--instrument-folder", FOLDER, "--json", out_json,
                        "--examples", "2", "--processed-index", proc / ".processed_index.json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("History import dry run", res.stdout)
        self.assertIn("orphan", res.stdout)
        self.assertIn("processed index", res.stdout.lower())
        data = json.loads(out_json.read_text())
        self.assertEqual(data["report"]["stats"]["cdfs"], 4)
        self.assertEqual(data["processed_index"]["entries"], 1)
        self.assertEqual(snapshot_tree(proc), before)

    def test_cdf_only_mode(self) -> None:
        proc, _ = self._fixture()
        res = self._run("--processed-dir", proc, "--instrument-folder", FOLDER)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("CDF-only", res.stdout)

    def test_missing_folder_exits_2(self) -> None:
        res = self._run("--processed-dir", self.tmp / "nope", "--instrument-folder", FOLDER)
        self.assertEqual(res.returncode, 2)
        self.assertIn("nope", res.stderr)

    def test_json_must_not_overwrite_the_sources(self) -> None:
        proc, results = self._fixture()
        before = results.read_bytes()
        res = self._run("--processed-dir", proc, "--results-csv", results,
                        "--instrument-folder", FOLDER, "--json", results)
        self.assertEqual(res.returncode, 2)
        self.assertEqual(results.read_bytes(), before)
        res2 = self._run("--processed-dir", proc, "--instrument-folder", FOLDER,
                         "--json", proc / "report.json")
        self.assertEqual(res2.returncode, 2)
        self.assertFalse((proc / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
