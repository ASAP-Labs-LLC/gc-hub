"""2A2 T3: an instrument's export path (exports.HubExporter.new_path/adopt),
with the exporter's refusal reasons surfaced."""
from __future__ import annotations

from a2_helpers import hub  # noqa: F401

import pytest

pytest.importorskip("flask")

import exports  # noqa: E402
import instrument_admin as ia  # noqa: E402
import store  # noqa: E402


def _exp(hub):
    return exports.HubExporter(hub.db, data_dir=hub.data)


def test_status_defaults_to_the_data_folder(hub):
    hub.gc1()
    st = ia.export_status("gc1", _exp(hub))
    assert st["path"] == str(hub.data / "results" / "gc1_results.csv")
    assert st["configured"] is False and st["pending"] == 0


def test_set_export_path(hub, tmp_path):
    hub.gc1()
    target = tmp_path / "share" / "distill_results.csv"
    st = ia.set_export_path("gc1", str(target), _exp(hub))
    assert st["path"] == str(target) and st["configured"] is True
    assert store.instruments.get("gc1", db=hub.db)["export_path"] == str(target)


@pytest.mark.parametrize("bad", ["", "   ", "relative/x.csv", None, 3])
def test_set_export_path_refuses_non_absolute(hub, bad):
    hub.gc1()
    with pytest.raises(ia.AdminError) as e:
        ia.set_export_path("gc1", bad, _exp(hub))
    assert e.value.status == 400


@pytest.mark.parametrize("name", ["qbenchlogin.txt", "updater.json", "results.CSV.bak", "noext"])
def test_set_export_path_refuses_a_non_csv(hub, tmp_path, name):
    """Like hub_admin's new-path route: only a .csv (the diagnostics bundle reads
    an export file's tail, so the path must never name an arbitrary file)."""
    hub.gc1()
    with pytest.raises(ia.AdminError) as e:
        ia.set_export_path("gc1", str(tmp_path / name), _exp(hub))
    assert e.value.status == 400
    assert store.instruments.get("gc1", db=hub.db)["export_path"] in (None, "")


def test_set_export_path_takes_an_upper_case_csv(hub, tmp_path):
    hub.gc1()
    st = ia.set_export_path("gc1", str(tmp_path / "RESULTS.CSV"), _exp(hub))
    assert st["configured"] is True


def test_set_export_path_in_use_is_surfaced(hub, tmp_path):
    hub.gc1()
    hub.gc2()
    target = tmp_path / "one.csv"
    ia.set_export_path("gc1", str(target), _exp(hub))
    with pytest.raises(ia.AdminError) as e:
        ia.set_export_path("gc2", str(target), _exp(hub))
    assert e.value.status == 409 and e.value.extra["reason"] == "in-use"
    assert "gc1" in e.value.extra["detail"]


def test_adopt_refusals_are_surfaced(hub, tmp_path):
    hub.gc1()
    target = tmp_path / "v1.csv"
    ia.set_export_path("gc1", str(target), _exp(hub))
    with pytest.raises(ia.AdminError) as e:
        ia.adopt_export("gc1", _exp(hub), by="admin@x")
    assert e.value.status == 409 and e.value.extra["reason"] == "missing"
    target.write_text("not,the,header\r\n", encoding="utf-8")
    with pytest.raises(ia.AdminError) as e:
        ia.adopt_export("gc1", _exp(hub), by="admin@x")
    assert e.value.extra["reason"] == "header-mismatch"


def test_adopt_a_v1_file(hub, tmp_path):
    hub.gc1()
    target = tmp_path / "v1.csv"
    target.write_text(exports.header_line(), encoding="utf-8", newline="")
    exp = _exp(hub)
    ia.set_export_path("gc1", str(target), exp)
    out = ia.adopt_export("gc1", exp, by="admin@x")
    assert out["adopted"]["adopted_by"] == "admin@x"
    assert exports.sidecar_path(target).is_file()
    assert out["status"]["refused"] is None


def test_unknown_instrument(hub):
    with pytest.raises(ia.AdminError) as e:
        ia.export_status("gc9", _exp(hub))
    assert e.value.status == 404
