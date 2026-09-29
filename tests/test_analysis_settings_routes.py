"""Phase 3 review: the deviation-bullet admin settings are validated when
saved (a bad value is a 400 and nothing is written), and "Set as Default"
only replaces the saved range overlays when the request carries them."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

pytest.importorskip("flask")

from bootapp import TEST_ADMIN_PASSWORD, booted, get, post, setup_admin  # noqa: E402

PW = {"password": TEST_ADMIN_PASSWORD}


@pytest.fixture(scope="module")
def hub():
    with tempfile.TemporaryDirectory() as t:
        with booted(Path(t)) as (port, _proc, data, _home):
            setup_admin(port, data)
            yield port


def _conf(port):
    return get(port, "/api/settings")[1]


def test_overlays_are_kept_when_the_request_has_none(hub):
    saved = [{"label": "Gas", "c_start": 5, "c_end": 11, "color": "#f0a50044"}]
    code, _ = post(hub, "/api/save-analysis-defaults",
                   dict(PW, params={"quantile": 0.2}, range_overlays=saved))
    assert code == 200
    before = _conf(hub)["analysis_range_overlays"]
    code, _ = post(hub, "/api/save-analysis-defaults", dict(PW, params={"quantile": 0.25}))
    assert code == 200
    conf = _conf(hub)
    assert conf["analysis_range_overlays"] == before
    assert conf["analysis_quantile"] == "0.25"
    # an explicit empty list is saved: no ranges
    code, _ = post(hub, "/api/save-analysis-defaults", dict(PW, params={}, range_overlays=[]))
    assert code == 200 and _conf(hub)["analysis_range_overlays"] == "[]"


@pytest.mark.parametrize("key,value", [
    ("min_width_min", "abc"), ("merge_gap_min", -1), ("spike_min_width_min", "nan"),
    ("spike_max_fwhm_min", "inf"), ("spike_min_dominance", "x"),
    ("spike_report_threshold", "lots"),
])
def test_save_defaults_refuses_bad_values(hub, key, value):
    before = _conf(hub)
    code, body = post(hub, "/api/save-analysis-defaults", dict(PW, params={key: value}))
    assert code == 400, body
    assert f"analysis_{key}" in body["error"]
    assert _conf(hub)[f"analysis_{key}"] == before[f"analysis_{key}"]


@pytest.mark.parametrize("key,value", [
    ("analysis_min_width_min", "abc"), ("analysis_spike_min_dominance", "-0.5"),
    ("analysis_spike_max_fwhm_min", ""), ("analysis_spike_report_threshold", "x"),
])
def test_settings_route_refuses_bad_values(hub, key, value):
    before = _conf(hub)
    code, body = post(hub, "/api/settings", dict(before, **PW, **{key: value}))
    assert code == 400, body
    assert key in body["error"]
    assert _conf(hub)[key] == before[key]


def test_good_values_are_saved(hub):
    conf = _conf(hub)
    code, _ = post(hub, "/api/settings", dict(conf, **PW, analysis_spike_report_threshold="",
                                               analysis_spike_min_dominance="0.7",
                                               analysis_merge_gap_min="0.2"))
    assert code == 200
    conf = _conf(hub)
    assert (conf["analysis_spike_min_dominance"], conf["analysis_merge_gap_min"]) == ("0.7", "0.2")
    code, _ = post(hub, "/api/save-analysis-defaults",
                   dict(PW, params={"spike_report_threshold": 800, "spike_max_fwhm_min": 0.15}))
    assert code == 200
    conf = _conf(hub)
    assert conf["analysis_spike_report_threshold"] == "800"
    assert conf["analysis_spike_max_fwhm_min"] == "0.15"
