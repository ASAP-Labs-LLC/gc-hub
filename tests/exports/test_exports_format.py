"""exports.format_line: the frozen row bytes are byte-identical to v1's append.

v1 (``distill._append_csv_row``) opens the results CSV with
``open("a", newline="", encoding="utf-8")`` and writes with a default-dialect
``csv.writer``. The hub stores the line in ``export_rows`` and later writes
``line.encode("utf-8")`` verbatim, so the two must match byte for byte.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import distill

TESTS_DIR = Path(__file__).resolve().parent.parent
for _p in (TESTS_DIR, TESTS_DIR / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import exports  # noqa: E402

CSV_HEADER = distill.CSV_HEADER


def _v1_bytes(tmp_path: Path, row_data: list, name: str = "v1.csv") -> bytes:
    dest = tmp_path / name
    distill._append_csv_row(dest, row_data)
    return dest.read_bytes()


def _row(**overrides) -> dict:
    row = {col: round(10.0 + i * 1.37, 2) for i, col in enumerate(CSV_HEADER)}
    row.update({"Lab ID": "40305", "InjectionDateTime": "2026-09-25 00:24:50",
                "Best Fit": "Diesel", "Fit Score": 0.9731})
    row.update(overrides)
    return row


CASES = {
    "plain floats": _row(),
    "empty D86 cells": _row(**{c: "" for c in CSV_HEADER if c.startswith("D86")},
                            **{"Best Fit": "", "Fit Score": ""}),
    "comma and quotes": _row(**{"Lab ID": 'A,"B"', "Best Fit": 'Mix: Diesel + "Jet" (80/20)'}),
    "non-ASCII": _row(**{"Lab ID": "Échantillon µ 日本", "Best Fit": "Gazole – été"}),
    "embedded newline": _row(**{"Best Fit": "line1\nline2", "Lab ID": "cr\rhere"}),
    "odd numbers": _row(**{"2887 IBP": 1e-05, "2887 T5": -0.0, "2887 T10": 123456789.125,
                           "2887 T20": float("nan"), "2887 T30": 7, "2887 T40": 0.1 + 0.2}),
    "leading/trailing spaces": _row(**{"Lab ID": " 40305 ", "Best Fit": "  "}),
    "None cells": _row(**{"Best Fit": None, "Fit Score": None}),
}


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("as_json", [True, False], ids=["json", "dict"])
def test_line_is_byte_identical_to_v1_append(tmp_path, name, as_json):
    results = CASES[name]
    source = r"\\ASAPServer\Labsharedrive\processed_cdf\40305_09252026_002450.CDF"
    row_data = [results[c] for c in CSV_HEADER[:-1]] + [source]
    v1 = _v1_bytes(tmp_path, row_data)

    given = json.dumps(results) if as_json else results
    line = exports.format_line(given, source)

    assert isinstance(line, str)
    assert line.endswith("\r\n")
    assert v1 == exports.header_line().encode("utf-8") + line.encode("utf-8")


def test_header_line_is_v1s_header(tmp_path):
    v1 = _v1_bytes(tmp_path, [""] * len(CSV_HEADER))
    header = exports.header_line()
    assert header.endswith("\r\n")
    assert v1.startswith(header.encode("utf-8"))
    assert header.encode("utf-8").count(b"\n") == 1


def test_source_file_argument_wins_over_results(tmp_path):
    results = _row(**{"Source File": "/tmp/somewhere/else.CDF"})
    line = exports.format_line(results, "cdf/gc1/2026/09/40305_1.CDF")
    assert line.endswith(",cdf/gc1/2026/09/40305_1.CDF\r\n")
    assert "/tmp/somewhere" not in line


def test_path_source_file_is_stringified(tmp_path):
    p = Path("cdf") / "gc1" / "x.CDF"
    assert exports.format_line(_row(), p).endswith(f",{p}\r\n")


def test_missing_columns_are_empty_cells(tmp_path):
    results = {"Lab ID": "40305", "InjectionDateTime": "2026-09-25 00:24:50"}
    row_data = ["40305", "2026-09-25 00:24:50"] + [""] * (len(CSV_HEADER) - 3) + ["src"]
    v1 = _v1_bytes(tmp_path, row_data)
    assert v1 == (exports.header_line() + exports.format_line(results, "src")).encode("utf-8")


def test_rejects_something_that_is_not_a_results_row():
    with pytest.raises(ValueError):
        exports.format_line({"row": _row(), "d2887": {}}, "src")   # compute()'s whole dict
    with pytest.raises(ValueError):
        exports.format_line("[1, 2, 3]", "src")


def test_compute_row_end_to_end_matches_v1(tmp_path):
    """The real numbers: compute()'s row (floats), stored as JSON, round-trips
    to exactly the bytes v1 appended for the same result."""
    make_golden = pytest.importorskip("make_golden")
    inputs = make_golden.build_inputs(tmp_path)
    name = "sample_40304_blank"
    conf = make_golden.case_conf(tmp_path, inputs, name)
    src, blank = make_golden.case_paths(inputs, name)
    make_golden._clear_distill_caches()
    try:
        result = distill.compute(src, conf, blank)
    finally:
        make_golden._clear_distill_caches()
    source = str(tmp_path / "processed" / "40304_09252026_142300.CDF")
    row = dict(result["row"], **{"Source File": source})
    v1 = _v1_bytes(tmp_path, [row[c] for c in CSV_HEADER])

    stored = json.dumps(result["row"])          # what sample_results.results holds
    line = exports.format_line(stored, source)
    assert v1 == (exports.header_line() + line).encode("utf-8")
