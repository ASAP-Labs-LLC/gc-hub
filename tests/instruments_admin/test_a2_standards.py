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
    assert store.settings_kv.get(standards.MIGRATED_KEY, db=hub.db) is None


def test_offline_first_use_still_migrates_later(hub):
    """Review I2: the folder is unreachable (a share offline) at first use; the
    migration must still tag the files gc1 once it comes back."""
    hub.gc1()
    d = hub.root / "standards"
    assert standards.sync(d, db=hub.db) == {"migrated": 0, "registered": 0}
    assert store.settings_kv.get(standards.MIGRATED_KEY, db=hub.db) is None
    _dir(hub, "Diesel.CDF")
    assert standards.sync(d, db=hub.db) == {"migrated": 1, "registered": 0}
    assert standards.list_standards(d, db=hub.db)[0]["instrument_id"] == "gc1"


def test_empty_folder_first_use_still_migrates_later(hub):
    hub.gc1()
    d = _dir(hub)                                  # exists, no CDFs
    assert standards.sync(d, db=hub.db) == {"migrated": 0, "registered": 0}
    assert store.settings_kv.get(standards.MIGRATED_KEY, db=hub.db) is None
    (d / "Jet.CDF").write_bytes(b"x")
    assert standards.sync(d, db=hub.db)["migrated"] == 1


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


def test_register_tags_a_new_file_at_once_over_the_first_use_migration(hub):
    """v8.1: a standard added from a run is tagged as it is added; on the
    folder's first use the migration tags the other files gc1, never it."""
    hub.gc1()
    hub.gc2()
    d = _dir(hub, "Diesel.CDF", "Gas.CDF")
    row = standards.register(d, d / "Gas.CDF", "gc2", db=hub.db)
    assert (row["name"], row["instrument_id"], row["instrument_name"]) == ("Gas", "gc2", "GC-2")
    assert row["file_name"] == "Gas.CDF" and row["missing"] is False and "cdf_path" not in row
    rows = {r["name"]: r for r in standards.list_standards(d, db=hub.db)}
    assert {n: r["instrument_id"] for n, r in rows.items()} == {"Diesel": "gc1", "Gas": "gc2"}
    assert rows["Gas"]["id"] == row["id"]
    (d / "Jet.CDF").write_bytes(b"x")                     # after the migration, untagged on purpose
    assert standards.register(d, d / "Jet.CDF", None, db=hub.db)["instrument_id"] is None
    assert standards.list_standards(d, "gc1", db=hub.db)[0]["name"] == "Diesel"


def test_register_retags_a_known_file_and_refuses_an_unknown_instrument(hub):
    hub.gc1()
    hub.gc2()
    d = _dir(hub, "Diesel.CDF")
    first = standards.register(d, d / "Diesel.CDF", "gc1", db=hub.db)
    again = standards.register(d, d / "Diesel.CDF", "gc2", db=hub.db)      # replaced from a gc2 run
    assert again["id"] == first["id"] and again["instrument_id"] == "gc2"
    assert [r["instrument_id"] for r in standards.list_standards(d, db=hub.db)] == ["gc2"]
    with pytest.raises(LookupError):
        standards.register(d, d / "Diesel.CDF", "gc9", db=hub.db)
    assert standards.list_standards(d, db=hub.db)[0]["instrument_id"] == "gc2"


def test_register_without_gc1_still_records_the_tag(hub):
    """No gc1 yet (no migration possible): the added file is still listed with
    its tag, and the later migration leaves it alone."""
    hub.gc2()
    d = _dir(hub, "Old.CDF", "New.CDF")
    assert standards.register(d, d / "New.CDF", "gc2", db=hub.db)["instrument_id"] == "gc2"
    assert [r["name"] for r in standards.list_standards(d, db=hub.db)] == ["New"]
    hub.gc1()
    rows = {r["name"]: r["instrument_id"] for r in standards.list_standards(d, db=hub.db)}
    assert rows == {"New": "gc2", "Old": "gc1"}


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


def test_moved_carries_the_row_and_its_tag_to_the_new_name(hub):
    """v8.1.1: a rename keeps the one row (and its GC): the old name is gone,
    never left as a missing file; a stale row already at the new name (its
    file gone) is dropped."""
    hub.gc1()
    hub.gc2()
    d = _dir(hub, "Diesel.CDF", "Gas.CDF")
    first = standards.register(d, d / "Gas.CDF", "gc2", db=hub.db)
    (d / "Ghost.CDF").write_bytes(b"x")
    standards.sync(d, db=hub.db)
    (d / "Ghost.CDF").unlink()                            # an old stale row, "missing"
    (d / "Gas.CDF").rename(d / "Ghost.CDF")
    row = standards.moved(d, d / "Gas.CDF", d / "Ghost.CDF", db=hub.db)
    assert (row["id"], row["name"], row["instrument_id"], row["missing"]) == \
        (first["id"], "Ghost", "gc2", False)
    rows = standards.list_standards(d, db=hub.db)
    assert sorted((r["name"], r["instrument_id"], r["missing"]) for r in rows) == \
        [("Diesel", "gc1", False), ("Ghost", "gc2", False)]
    # a file the table never knew: nothing to move (sync registers it later)
    (d / "Loose.CDF").write_bytes(b"x")
    (d / "Loose.CDF").rename(d / "Looser.CDF")
    assert standards.moved(d, d / "Loose.CDF", d / "Looser.CDF", db=hub.db) is None


def test_forget_drops_a_deleted_files_row_and_a_missing_ones(hub):
    hub.gc1()
    d = _dir(hub, "Diesel.CDF", "Gas.CDF", "Jet.CDF")
    standards.sync(d, db=hub.db)
    (d / "Gas.CDF").unlink()
    assert standards.forget(d, d / "Gas.CDF", db=hub.db) == 1
    (d / "Jet.CDF").unlink()                              # gone before v8.1.1: a "missing" row
    assert [r["name"] for r in standards.list_standards(d, db=hub.db) if r["missing"]] == ["Jet"]
    assert standards.forget(d, d / "Jet.CDF", db=hub.db) == 1
    assert [r["name"] for r in standards.list_standards(d, db=hub.db)] == ["Diesel"]
    assert standards.forget(d, d / "Nope.CDF", db=hub.db) == 0
