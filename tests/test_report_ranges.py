"""Dynamic region resolution for the analysis report (analysis_core).

The exported PDF must show the operator's custom regions, not just the legacy
Gas/Oil pair.  Resolution order: explicit request ranges → saved overlay
defaults (settings JSON) → legacy gas/oil settings.
"""
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis_core import resolve_report_ranges  # noqa: E402


LEGACY_CONF = {
    "analysis_gas_c_start": "5", "analysis_gas_c_end": "11",
    "analysis_oil_c_start": "20", "analysis_oil_c_end": "44",
}


def test_explicit_ranges_win():
    body = [
        {"label": "Jet", "c_start": 9, "c_end": 16, "color": "#12ab34"},
        {"label": "Heavy", "c_start": 24, "c_end": 40},
    ]
    conf = dict(LEGACY_CONF, analysis_range_overlays=json.dumps(
        [{"label": "Saved", "c_start": 6, "c_end": 12, "color": "#ffffff"}]))
    ranges = resolve_report_ranges(body, conf)
    assert [r["label"] for r in ranges] == ["Jet", "Heavy"]
    assert ranges[0]["c_start"] == 9 and ranges[0]["c_end"] == 16
    assert ranges[0]["color"] == "#12ab34"
    assert ranges[1]["color"]  # missing color gets a default, never empty


def test_saved_overlays_used_when_no_explicit():
    conf = dict(LEGACY_CONF, analysis_range_overlays=json.dumps([
        {"label": "Gas", "c_start": 5, "c_end": 11, "color": "#f0a50044"},
        {"label": "Kero", "c_start": 12, "c_end": 18, "color": "#3498db44"},
    ]))
    ranges = resolve_report_ranges(None, conf)
    assert [r["label"] for r in ranges] == ["Gas", "Kero"]
    assert ranges[1]["c_start"] == 12


def test_legacy_gas_oil_fallback():
    ranges = resolve_report_ranges(None, dict(LEGACY_CONF))
    assert [r["label"] for r in ranges] == ["Gas", "Oil"]
    assert ranges[0]["c_start"] == 5 and ranges[0]["c_end"] == 11
    assert ranges[1]["c_start"] == 20 and ranges[1]["c_end"] == 44
    assert all(r["color"] for r in ranges)


def test_bad_saved_json_falls_back_to_legacy():
    conf = dict(LEGACY_CONF, analysis_range_overlays="{not json")
    ranges = resolve_report_ranges(None, conf)
    assert [r["label"] for r in ranges] == ["Gas", "Oil"]


def test_range_color_to_rgba_helper():
    from analysis_core import range_color_rgba
    assert range_color_rgba("#ff0000", 0.12) == "rgba(255,0,0,0.12)"
    # 8-digit hex (JS overlays append alpha) — alpha byte is ignored,
    # the requested alpha wins
    assert range_color_rgba("#00ff0044", 0.5) == "rgba(0,255,0,0.5)"
    # garbage falls back to a neutral grey, never raises
    assert range_color_rgba("nope", 0.2).startswith("rgba(")
