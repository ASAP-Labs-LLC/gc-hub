"""v3.1: the Instruments page's Activity feed (``instrument_activity.feed``):
``instrument_events`` rows plus entries synthesised from ``samples.received_at``,
``corrections_audit`` (one entry per save), ``report_log``, ``export_rows``
(written to the results CSV) and ``agents`` (the last check-in), newest
first, each with a stable ``key`` so the page can prepend only new ones."""
from __future__ import annotations

from a2_helpers import ELEVEN, hub  # noqa: F401

import pytest

pytest.importorskip("flask")

import instrument_activity as act  # noqa: E402
import instrument_admin as ia  # noqa: E402
import store  # noqa: E402

BY = "Ryan C (10.0.0.5)"


def _at(minute: int) -> str:
    return f"2026-09-30T12:{minute:02d}:00.000000+00:00"


def _seed(hub):
    hub.gc2(live_since=None)
    store.instrument_events.add(hub.db, "gc2", "installer", by=BY, at=_at(2))
    ia.save_corrections("gc2", ELEVEN, "EQM study", by=BY, db=hub.db)
    with store.connection(hub.db) as conn:
        conn.execute("UPDATE corrections_audit SET changed_at=?", (_at(3),))
        conn.execute("DELETE FROM instrument_events WHERE kind='corrections_saved'")
        sid = store.samples.insert_received("gc2", "40330", "2026-09-30 07:00:00", "cdf",
                                            cdf_sha256="s1", cdf_path="cdf/a.CDF",
                                            method_name="SIMDISB.M", status="final",
                                            received_at=_at(4), db=conn)
        with store.write_txn(conn):
            store.add_revision(conn, sid, {"IBP": 1}, reason="processed", by="worker")
            seq = store.export_rows.append_pending(conn, "gc2", sid, 1, "line")
        conn.execute("UPDATE export_rows SET hub_appended_at=? WHERE seq=?", (_at(5), seq))
        conn.execute("INSERT INTO agents(instrument_id, last_seen, host, version) "
                     "VALUES ('gc2', ?, 'GC2-PC', '2.4.1')", (_at(6),))
    store.report_log.add(sid, kind="qbench", user_name="Ann B", created_at=_at(7), db=hub.db)
    return sid, seq


def test_feed_merges_every_source_newest_first(hub):
    sid, seq = _seed(hub)
    feed = act.feed(50, db=hub.db)
    kinds = [e["kind"] for e in feed]
    assert kinds == ["report", "agent_seen", "export_written", "sample_received",
                     "corrections_saved", "installer"]
    by_kind = {e["kind"]: e for e in feed}
    assert by_kind["report"]["by"] == "Ann B" and by_kind["report"]["lab_id"] == "40330"
    assert by_kind["report"]["detail"] == {"report_kind": "qbench"}
    assert by_kind["agent_seen"]["detail"] == {"host": "GC2-PC", "version": "2.4.1"}
    assert by_kind["export_written"]["sample_id"] == sid
    assert by_kind["sample_received"]["lab_id"] == "40330"
    assert by_kind["corrections_saved"]["by"] == BY
    assert by_kind["corrections_saved"]["detail"] == {"changed": 11, "reason": "EQM study"}
    for e in feed:
        assert e["instrument_id"] == "gc2" and e["instrument_name"]
        assert {"key", "kind", "at", "instrument_id", "instrument_name", "by", "sample_id",
                "lab_id", "detail"} <= set(e)
    assert len({e["key"] for e in feed}) == len(feed)
    # entries about a sample carry its injection time (the page links /samples/<id>)
    for kind in ("report", "export_written", "sample_received"):
        assert by_kind[kind]["injection_dt"] == "2026-09-30 07:00:00", kind
    assert by_kind["installer"]["injection_dt"] is None


def test_keys_are_stable_between_calls(hub):
    _seed(hub)
    a = [e["key"] for e in act.feed(50, db=hub.db)]
    b = [e["key"] for e in act.feed(50, db=hub.db)]
    assert a == b


def test_limit_is_applied_after_the_merge(hub):
    _seed(hub)
    feed = act.feed(2, db=hub.db)
    assert [e["kind"] for e in feed] == ["report", "agent_seen"]


def test_corrections_saved_events_are_not_listed_twice(hub):
    hub.gc2(live_since=None)
    ia.save_corrections("gc2", ELEVEN, "first", by=BY, db=hub.db)
    feed = act.feed(50, db=hub.db)
    assert [e["kind"] for e in feed].count("corrections_saved") == 1


def test_an_agent_that_never_checked_in_is_not_listed(hub):
    hub.gc2(live_since=None)
    with store.connection(hub.db) as conn:
        conn.execute("INSERT INTO agents(instrument_id, pending_command) VALUES ('gc2', 'pause')")
    assert [e for e in act.feed(50, db=hub.db) if e["kind"] == "agent_seen"] == []


def test_samples_today_excludes_backfill(hub):
    """The cards' "samples today" counts live runs, not imported history."""
    import instruments_api
    hub.gc2(live_since=None)
    now = store.now_iso()
    for n, backfill in (("1", 0), ("2", 1), ("3", 1)):
        store.samples.insert_received("gc2", "L" + n, f"2026-09-30 0{n}:00:00", "cdf",
                                      cdf_sha256="t" + n, cdf_path=None, backfill=backfill,
                                      received_at=now, db=hub.db)
    assert instruments_api._counts(hub.db)["today"]["gc2"] == 1


def test_bad_limits_are_clamped():
    assert act.clamp_limit(None) == act.DEFAULT_LIMIT
    assert act.clamp_limit("5") == 5
    assert act.clamp_limit("0") == 1 and act.clamp_limit("100000") == act.MAX_LIMIT
    with pytest.raises(ValueError):
        act.clamp_limit("abc")


def test_reserved_ids_can_not_be_created(hub):
    for iid in ("activity", "classic"):
        with pytest.raises(ia.AdminError) as e:
            ia.create({"id": iid, "name": "X"}, db=hub.db)
        assert e.value.status == 400 and "reserved" in e.value.message
