"""2A2 T7: the backfill release screen (D11) and the conflicts screen."""
from __future__ import annotations

from a2_helpers import hub  # noqa: F401

from datetime import datetime, timedelta

import pytest

pytest.importorskip("flask")

import instrument_admin as ia  # noqa: E402
import store  # noqa: E402


def _backfill(hub):
    """gc1 live since 2030: every sample is backfill. Two final, one other_method."""
    hub.gc1(live_since=datetime(2030, 1, 1))
    a = hub.submit(hub.cdf(name="A1", injected=datetime(2026, 9, 1, 8))).sample_id
    b = hub.submit(hub.cdf(name="B2", injected=datetime(2026, 9, 2, 8))).sample_id
    g = hub.submit(hub.cdf(name="G3", injected=datetime(2026, 9, 3, 8), method_name="D7096.M")).sample_id
    hub.worker().run_until_idle()
    for sid in (a, b):
        s = store.samples.get(sid, db=hub.db)
        assert s["status"] == "final" and s["backfill"] == 1
    return a, b, g


def test_list_and_filters(hub):
    a, b, g = _backfill(hub)
    out = ia.backfill_list("gc1", db=hub.db)
    assert out["total"] == 3 and [r["sample_id"] for r in out["samples"]] == [g, b, a]
    assert {"lab_id", "injection_dt", "status", "released_at", "method_name"} <= set(out["samples"][0])
    assert "cdf_path" not in out["samples"][0]
    assert [r["sample_id"] for r in ia.backfill_list("gc1", status="final", db=hub.db)["samples"]] == [b, a]
    assert [r["sample_id"] for r in ia.backfill_list("gc1", q="b2", db=hub.db)["samples"]] == [b]
    page = ia.backfill_list("gc1", limit=1, offset=1, db=hub.db)
    assert page["total"] == 3 and [r["sample_id"] for r in page["samples"]] == [b]
    ia.release("gc1", [a], by="admin@x", db=hub.db, data_dir=hub.data)
    assert [r["sample_id"] for r in ia.backfill_list("gc1", released=True, db=hub.db)["samples"]] == [a]
    assert a not in [r["sample_id"] for r in ia.backfill_list("gc1", released=False, db=hub.db)["samples"]]


@pytest.mark.parametrize("kw", [{"status": "bogus"}, {"limit": 0}, {"limit": 1001},
                                {"offset": -1}, {"released": "maybe"}])
def test_list_refusals(hub, kw):
    _backfill(hub)
    with pytest.raises(ia.AdminError):
        ia.backfill_list("gc1", db=hub.db, **kw)


def test_release_per_id_results(hub):
    a, b, g = _backfill(hub)
    hub.gc2(live_since=datetime(2020, 1, 1))
    other = hub.submit(hub.cdf(name="X", injected=datetime(2026, 9, 5)), instrument="gc2").sample_id
    out = ia.release("gc1", [a, a, g, 999999, other], by="admin@x", db=hub.db, data_dir=hub.data)
    by_id = {r["sample_id"]: r for r in out}
    assert by_id[a]["ok"] is True and by_id[a]["seq"] >= 1
    assert by_id[g]["ok"] is False and "other_method" in by_id[g]["error"]
    assert by_id[999999]["ok"] is False
    assert by_id[other]["ok"] is False and "GC-1" in by_id[other]["error"]
    assert len(out) == 4                                   # the repeat is dropped
    s = store.samples.get(a, db=hub.db)
    assert s["released_by"] == "admin@x" and s["released_at"]
    rows = store.export_rows.rows_after("gc1", 0, db=hub.db)
    assert [r["sample_id"] for r in rows] == [a]
    again = ia.release("gc1", [a], by="admin@x", db=hub.db, data_dir=hub.data)
    assert again[0]["ok"] is False and "already released" in again[0]["error"]


@pytest.mark.parametrize("ids", [None, "1", [], ["x"], [True], list(range(501))])
def test_release_refusals(hub, ids):
    _backfill(hub)
    with pytest.raises(ia.AdminError):
        ia.release("gc1", ids, by="a", db=hub.db, data_dir=hub.data)


# ── conflicts ───────────────────────────────────────────────────────────────

def _conflict(hub):
    hub.gc1()
    sid = hub.submit(hub.cdf(name="C1", injected=datetime(2026, 9, 10, 9))).sample_id
    hub.worker().run_until_idle()
    res = hub.submit(hub.cdf(name="C1", injected=datetime(2026, 9, 10, 9), shift=0.3))
    assert res.outcome == "conflict"
    return sid, res.conflict_id


def test_conflicts_list_shows_both_files(hub):
    sid, cid = _conflict(hub)
    out = ia.conflicts_list(db=hub.db, data_dir=hub.data)
    assert len(out) == 1
    c = out[0]
    s = store.samples.get(sid, db=hub.db)
    assert c["id"] == cid and c["instrument_id"] == "gc1" and c["instrument_name"] == "GC-1"
    assert c["held"]["lab_id"] == "C1" and c["held"]["injection_dt"] == s["injection_dt"]
    assert len(c["held"]["cdf_sha256"]) == 64 and c["held"]["file_name"].endswith(".CDF")
    assert c["held"]["size"] > 0
    assert c["existing"]["sample_id"] == sid and c["existing"]["cdf_sha256"] == s["cdf_sha256"]
    assert c["existing"]["status"] == "final" and c["existing"]["current_revision"] == 1
    assert c["held"]["cdf_sha256"] != c["existing"]["cdf_sha256"]
    assert c["replace_pending"] is False and c["error"] is None and c["resolved"] is None
    assert "cdf_path" not in c["held"] and "cdf_path" not in c["existing"]
    assert ia.conflicts_list("gc2", db=hub.db, data_dir=hub.data) == []


def test_keep_existing(hub):
    sid, cid = _conflict(hub)
    ia.keep_existing(cid, by="admin@x", db=hub.db)
    c = store.conflicts.get(cid, db=hub.db)
    assert c["resolved"] == "kept-existing" and c["resolved_by"] == "admin@x"
    assert ia.conflicts_list(db=hub.db, data_dir=hub.data) == []
    assert len(ia.conflicts_list(include_resolved=True, db=hub.db, data_dir=hub.data)) == 1
    with pytest.raises(ia.AdminError) as e:
        ia.keep_existing(cid, by="admin@x", db=hub.db)
    assert e.value.status == 409


def test_replace_queues_and_shows_pending_then_error(hub):
    sid, cid = _conflict(hub)
    job = ia.replace(cid, by="admin@x", db=hub.db, data_dir=hub.data)
    assert store.jobs.get(job, db=hub.db)["payload"]["reason"] == "replace"
    assert ia.conflicts_list(db=hub.db, data_dir=hub.data)[0]["replace_pending"] is True
    assert ia.replace(cid, by="admin@x", db=hub.db, data_dir=hub.data) == job
    hub.worker().run_until_idle()
    c = store.conflicts.get(cid, db=hub.db)
    assert c["resolved"] == "replaced"


def test_conflict_error_is_shown(hub):
    sid, cid = _conflict(hub)
    store.conflicts.set_error(cid, "replace failed: boom", db=hub.db)
    assert ia.conflicts_list(db=hub.db, data_dir=hub.data)[0]["error"] == "replace failed: boom"


def test_unknown_conflict(hub):
    hub.gc1()
    with pytest.raises(ia.AdminError) as e:
        ia.keep_existing(42, by="a", db=hub.db)
    assert e.value.status == 404
    with pytest.raises(ia.AdminError) as e:
        ia.replace(42, by="a", db=hub.db, data_dir=hub.data)
    assert e.value.status == 404
