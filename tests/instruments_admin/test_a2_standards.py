"""2A2 T8: comparison standards tagged by instrument (D12)."""
from __future__ import annotations

from a2_helpers import hub  # noqa: F401

import pytest

import standards  # noqa: E402
import store  # noqa: E402


def _dir(hub, *names):
    d = hub.root / "standards"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"CDF\x01 not really")
    (d / "notes.txt").write_text("ignored")
    return d


def test_first_use_migrates_existing_files_to_gc1_once(hub):
    hub.gc1()
    d = _dir(hub, "Diesel.CDF", "Jet A.cdf")
    out = standards.sync(d, db=hub.db)
    assert out == {"migrated": 2, "registered": 0}
    rows = standards.list_standards(d, db=hub.db)
    assert [(r["name"], r["instrument_id"]) for r in rows] == [("Diesel", "gc1"), ("Jet A", "gc1")]
    assert store.settings_kv.get(standards.MIGRATED_KEY, db=hub.db)
    (d / "Kerosene.CDF").write_bytes(b"x")                # added later (old route): untagged
    assert standards.sync(d, db=hub.db) == {"migrated": 0, "registered": 1}
    rows = {r["name"]: r for r in standards.list_standards(d, db=hub.db)}
    assert rows["Kerosene"]["instrument_id"] is None
    assert standards.sync(d, db=hub.db) == {"migrated": 0, "registered": 0}
    assert "cdf_path" not in rows["Diesel"] and rows["Diesel"]["file_name"] == "Diesel.CDF"


def test_no_gc1_yet_means_no_migration(hub):
    d = _dir(hub, "Diesel.CDF")
    assert standards.sync(d, db=hub.db) == {"migrated": 0, "registered": 0}
    assert standards.list_standards(d, db=hub.db) == []
    assert store.settings_kv.get(standards.MIGRATED_KEY, db=hub.db) is None
    hub.gc1()
    assert standards.sync(d, db=hub.db)["migrated"] == 1


def test_missing_folder_is_fine(hub):
    hub.gc1()
    assert standards.list_standards(hub.root / "nope", db=hub.db) == []


def test_retag_and_filter(hub):
    hub.gc1()
    hub.gc2()
    d = _dir(hub, "Diesel.CDF", "Gas.CDF")
    rows = {r["name"]: r for r in standards.list_standards(d, db=hub.db)}
    out = standards.set_instrument(rows["Gas"]["id"], "gc2", db=hub.db)
    assert out["instrument_id"] == "gc2"
    assert [r["name"] for r in standards.list_standards(d, "gc2", db=hub.db)] == ["Gas"]
    standards.set_instrument(rows["Gas"]["id"], None, db=hub.db)
    assert standards.list_standards(d, "gc2", db=hub.db) == []
    with pytest.raises(LookupError):
        standards.set_instrument(rows["Gas"]["id"], "gc9", db=hub.db)
    with pytest.raises(LookupError):
        standards.set_instrument(99999, "gc1", db=hub.db)


def test_missing_file_is_flagged(hub):
    hub.gc1()
    d = _dir(hub, "Diesel.CDF", "Gone.CDF")
    standards.sync(d, db=hub.db)
    (d / "Gone.CDF").unlink()
    rows = {r["name"]: r for r in standards.list_standards(d, db=hub.db)}
    assert rows["Gone"]["missing"] is True and rows["Diesel"]["missing"] is False
    assert [p.name for p in standards.paths_for(d, "gc1", db=hub.db)] == ["Diesel.CDF"]


def test_for_sample_orders_own_first_and_warns(hub):
    hub.gc1()
    hub.gc2()
    d = _dir(hub, "A.CDF", "B.CDF")
    rows = {r["name"]: r for r in standards.list_standards(d, db=hub.db)}
    standards.set_instrument(rows["B"]["id"], "gc2", db=hub.db)
    (d / "C.CDF").write_bytes(b"x")
    picker = standards.for_sample(d, "gc2", db=hub.db)
    assert [r["name"] for r in picker] == ["B", "A", "C"]
    b, a, c = picker
    assert b["cross_instrument"] is False and b["warning"] is None
    assert a["cross_instrument"] is True and "GC-1" in a["warning"] and "GC-2" in a["warning"]
    assert c["cross_instrument"] is True and "not tagged" in c["warning"]


def test_warning_text():
    w = standards.cross_instrument_warning("GC-2", "Diesel", "GC-1")
    assert w == ("Standard Diesel was run on GC-1, not GC-2: retention times can differ "
                 "between instruments, so compare with care.")
