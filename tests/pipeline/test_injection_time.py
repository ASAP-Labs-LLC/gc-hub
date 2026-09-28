"""Injection-time parsing (2A1 T2, MAJOR): the ANDI compact stamp is matched
explicitly **before** any ISO attempt, so ``20260925002450+0000`` is 00:24:50,
not v1's 02:45:00 (Python >= 3.11 ``fromisoformat`` took the 9th character as
the date/time separator). ``v1_parse_injection_datetime`` keeps v1's answer
for the legacy string; ``cdf_identity`` is what the pipeline reads.

No ``__init__.py`` in this folder: a ``tests/pipeline`` package would shadow
the top-level ``pipeline`` module.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent.parent
for _p in (TESTS_DIR.parent, TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cdf_fixtures as fx  # noqa: E402
import distill  # noqa: E402
import import_match  # noqa: E402

D = datetime

# raw stamp -> the correct parse (None = unparseable, caller falls back to mtime)
GOLDEN = [
    # ANDI compact, with a zone: the bug's forms
    ("20260925002450+0000", D(2026, 9, 25, 0, 24, 50)),     # v1 on 3.11+: 02:45:00
    ("20260925002300+0000", D(2026, 9, 25, 0, 23, 0)),      # v1: 02:30:00
    ("20260925012345+0000", D(2026, 9, 25, 1, 23, 45)),     # v1: 12:34:00
    ("20260925101010-0500", D(2026, 9, 25, 10, 10, 10)),    # v1: 01:01:00
    ("20260925235959+05:30", D(2026, 9, 25, 23, 59, 59)),
    ("20250224134500-0500", D(2025, 2, 24, 13, 45, 0)),
    ("20260925142300+0000", D(2026, 9, 25, 14, 23, 0)),     # the golden fixtures' form
    ("  20250224134500-0500 ", D(2025, 2, 24, 13, 45, 0)),
    ("20260925002450 +0000", D(2026, 9, 25, 0, 24, 50)),
    ("20260925002450Z", D(2026, 9, 25, 0, 24, 50)),
    # ANDI compact without a zone
    ("20260925002450", D(2026, 9, 25, 0, 24, 50)),
    ("20250224134500", D(2025, 2, 24, 13, 45, 0)),
    # ISO, with T and with a space, with and without an offset
    ("2025-02-24T13:45:00", D(2025, 2, 24, 13, 45, 0)),
    ("2025-02-24 13:45:00", D(2025, 2, 24, 13, 45, 0)),
    ("2025-02-24T13:45:00+05:00", D(2025, 2, 24, 13, 45, 0)),
    ("2025-02-24 13:45:00.250000", D(2025, 2, 24, 13, 45, 0, 250000)),
    # DD-Mon-YYYY and US
    ("24-Feb-2025 13:45:00", D(2025, 2, 24, 13, 45, 0)),
    ("02/24/2025 13:45:00", D(2025, 2, 24, 13, 45, 0)),
    # garbage and impossible values
    ("", None),
    ("   ", None),
    ("garbage", None),
    ("20261325002450+0000", None),          # month 13
    ("20260925256000+0000", None),          # 25:60
    ("2026092500245", None),                # 13 digits
    ("20260925T002450", None),              # ISO basic: not a form GC files use
]


@pytest.mark.parametrize("raw,expected", GOLDEN)
def test_parse_injection_datetime_golden_table(raw, expected):
    assert distill.parse_injection_datetime(raw) == expected


@pytest.mark.parametrize("raw,expected", GOLDEN)
def test_parser_agrees_with_the_import_matcher(raw, expected):
    """The hub stores the same injection time the history importer computes,
    so a CDF loaded by either path lands on one (lab ID, injection time) key."""
    assert distill.parse_injection_datetime(raw) == import_match._correct_parse(raw)


def test_the_bug_stamp_is_now_read_correctly():
    assert distill.parse_injection_datetime("20260925002450+0000").isoformat(sep=" ") \
        == "2026-09-25 00:24:50"


def test_none_is_tolerated():
    assert distill.parse_injection_datetime(None) is None


# ── the frozen v1 parse (for legacy_injection_dt) ───────────────────────────

def test_v1_parse_reproduces_the_bug_on_any_interpreter():
    got = distill.v1_parse_injection_datetime("20260925002450+0000")
    assert got == D(2026, 9, 25, 2, 45, 0)


@pytest.mark.parametrize("raw,v1", [
    ("20260925002300+0000", D(2026, 9, 25, 2, 30, 0)),
    ("20260925142300+0000", D(2026, 9, 25, 14, 23, 0)),   # hour 42 invalid: v1 fell back
    ("20260925002450", D(2026, 9, 25, 0, 24, 50)),        # no zone: fromisoformat failed
    ("2025-02-24T13:45:00", D(2025, 2, 24, 13, 45, 0)),
    ("24-Feb-2025 13:45:00", D(2025, 2, 24, 13, 45, 0)),
    ("garbage", None),
    ("", None),
])
def test_v1_parse_table(raw, v1):
    assert distill.v1_parse_injection_datetime(raw) == v1


def test_v1_parse_is_the_matchers_py311_313_family():
    for raw, _ in GOLDEN:
        assert distill.v1_parse_injection_datetime(raw) == import_match._v1_parse(raw, "py311_313")


# ── cdf_identity ────────────────────────────────────────────────────────────

def _cdf(path, name="40305", injected=D(2026, 9, 25, 0, 24, 50), method_name=None):
    t = [0.0, 0.001, 0.002]
    return fx.write_cdf(path, t, [0.0, 1.0, 0.0], name, injected, method_name=method_name)


def test_cdf_identity_reads_the_fixed_time_and_the_method(tmp_path):
    p = _cdf(tmp_path / "a.CDF", method_name="  C:\\Chem32\\1\\METHODS\\simdisb.m ")
    sample, dt, source, method, raw = distill.cdf_identity(p)
    assert sample == "40305"
    assert dt == D(2026, 9, 25, 0, 24, 50)
    assert source == "cdf"
    assert method == "SIMDISB.M"
    assert raw == "20260925002450+0000"


def test_cdf_identity_without_a_method_name(tmp_path):
    p = _cdf(tmp_path / "a.CDF")
    assert distill.cdf_identity(p)[3] == ""


def test_cdf_identity_falls_back_to_the_file_mtime(tmp_path):
    from netCDF4 import Dataset
    p = tmp_path / "nostamp.CDF"
    with Dataset(p, "w", format="NETCDF3_CLASSIC") as ds:
        ds.sample_name = "X1"
        ds.createDimension("point_number", 3)
        ds.createVariable("ordinate_values", "f8", ("point_number",))[:] = [0, 1, 0]
    os.utime(p, (1_790_000_000, 1_790_000_000))
    sample, dt, source, method, raw = distill.cdf_identity(p)
    assert (sample, source, method, raw) == ("X1", "mtime", "", "")
    assert dt == datetime.fromtimestamp(1_790_000_000)
    # an explicit mtime (the sender's X-GC-Mtime) wins over the file's
    when = D(2026, 9, 1, 8, 0, 0)
    assert distill.cdf_identity(p, mtime=when)[1:3] == (when, "mtime")


def test_cdf_metadata_keeps_its_signature_and_uses_the_fixed_parser(tmp_path):
    p = _cdf(tmp_path / "a.CDF")
    assert distill.cdf_metadata(p) == ("40305", D(2026, 9, 25, 0, 24, 50))


def test_normalise_method_name():
    assert distill.normalise_method_name(None) == ""
    assert distill.normalise_method_name("  ") == ""
    assert distill.normalise_method_name("simdistb.m") == "SIMDISTB.M"
    assert distill.normalise_method_name("C:/Chem32/1/METHODS/SimDisB.M ") == "SIMDISB.M"
    assert distill.normalise_method_name("D:\\M\\x.m") == "X.M"
