"""setup_state (v3.1, spec "GC setup guide"): the eight setup steps are derived
from existing rows by one pure function, ``steps(instrument_row, facts)``.

Every step is covered in each state it can be in (done / current / waiting /
blocked), plus the cases the spec names: ``live_since`` unset (samples count
as backfill), an unmapped method (samples wait as other_method/review_method),
a calibration that is not usable, and a missing agent. ``gather`` (the facts
from a real store) is tested against a temporary database.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import setup_state as ss  # noqa: E402

KEYS = ["create", "corrections", "installer", "checkin", "calibration", "method", "go_live",
        "first_result"]


def row(**kw) -> dict:
    base = {"id": "gc2", "name": "GC-2", "method": "D2887", "enabled": 1, "lem_machine_uid": None,
            "token_issued_at": None, "live_since": None, "export_path": None}
    base.update(kw)
    return base


def facts(**kw) -> dict:
    base = {
        "corrections_count": 0,
        "agent": None,
        "calibration": {"usable": False, "problem": "No calibration CDF is set.",
                        "calibration_cdf": "", "assigned": 0},
        "samples_received": 0,
        "backfill_unreleased": 0,
        "methods": {"mapped_seen": [], "unmapped_seen": [], "held": 0},
        "export": {"path": "results/gc2_results.csv", "configured": False, "refused": None},
        "first_result": False,
        "events": {},
    }
    base.update(kw)
    return base


AGENT = {"last_seen": "2026-09-30T12:00:00+00:00", "version": "2.4.1", "host": "GC2-PC"}
USABLE = {"usable": True, "problem": None, "calibration_cdf": "cdf/gc2/cal.CDF", "assigned": 13}


def complete_facts(**kw) -> dict:
    base = facts(corrections_count=11, agent=AGENT, calibration=USABLE, samples_received=5,
                 methods={"mapped_seen": ["SIMDISB.M"], "unmapped_seen": [], "held": 0},
                 first_result=True)
    base.update(kw)
    return base


def by_key(result) -> dict:
    return {s["key"]: s for s in result}


def status_of(result) -> dict:
    return {s["key"]: s["status"] for s in result}


# ── shape ───────────────────────────────────────────────────────────────────

def test_eight_steps_in_the_spec_order_with_the_contract_fields():
    out = ss.steps(row(), facts())
    assert [s["key"] for s in out] == KEYS
    assert [s["n"] for s in out] == list(range(1, 9))
    for s in out:
        assert {"key", "n", "title", "status", "detail", "blocker", "action"} <= set(s)
        assert s["status"] in ("done", "current", "waiting", "blocked")
        assert isinstance(s["title"], str) and s["title"]
        assert isinstance(s["detail"], str) and s["detail"]
        if s["status"] == "blocked":
            assert s["blocker"]
        else:
            assert s["blocker"] is None


def test_exactly_one_current_step_until_everything_is_done():
    out = ss.steps(row(), facts())
    assert [s["status"] for s in out].count("current") == 1
    done = ss.steps(row(token_issued_at="t", live_since="2026-10-01 08:00:00"), complete_facts())
    assert all(s["status"] == "done" for s in done)


def test_steps_is_pure_and_does_not_mutate_its_inputs():
    r, f = row(), facts()
    before = (json.dumps(r, sort_keys=True), json.dumps(f, sort_keys=True))
    a = ss.steps(r, f)
    b = ss.steps(r, f)
    assert a == b
    assert (json.dumps(r, sort_keys=True), json.dumps(f, sort_keys=True)) == before


# ── a brand-new instrument ──────────────────────────────────────────────────

def test_a_new_instrument_is_on_step_2():
    st = status_of(ss.steps(row(), facts()))
    assert st == {"create": "done", "corrections": "current", "installer": "waiting",
                  "checkin": "blocked", "calibration": "blocked", "method": "blocked",
                  "go_live": "waiting", "first_result": "blocked"}


# ── 1 create ────────────────────────────────────────────────────────────────

def test_create_is_always_done_and_prompts_for_the_lem_machine():
    s = by_key(ss.steps(row(), facts()))["create"]
    assert s["status"] == "done" and s["action"] == "edit"
    assert "LEM machine" in s["detail"] and "optional" in s["detail"].lower()
    s2 = by_key(ss.steps(row(lem_machine_uid="m-17"), facts()))["create"]
    assert "m-17" in s2["detail"]


def test_create_carries_who_did_it_from_the_events():
    ev = {"created": {"by": "Ryan C (10.0.0.5)", "at": "2026-09-30T12:20:00+00:00"}}
    s = by_key(ss.steps(row(), facts(events=ev)))["create"]
    assert s["done_by"] == "Ryan C (10.0.0.5)" and s["done_at"] == "2026-09-30T12:20:00+00:00"


# ── 2 corrections ───────────────────────────────────────────────────────────

def test_corrections_done_with_eleven_rows():
    ev = {"corrections_saved": {"by": "Ryan C (x)", "at": "2026-09-30T12:48:00+00:00"}}
    s = by_key(ss.steps(row(), facts(corrections_count=11, events=ev)))["corrections"]
    assert s["status"] == "done" and s["done_by"] == "Ryan C (x)"


def test_corrections_partial_is_current_and_says_how_many():
    s = by_key(ss.steps(row(), facts(corrections_count=4)))["corrections"]
    assert s["status"] == "current" and "4 of 11" in s["detail"]
    assert s["action"] == "corrections"


def test_corrections_missing_later_in_the_order_is_waiting_not_blocked():
    # the installer is done and the agent has checked in, but no corrections:
    # step order is advice, so corrections are still the current step
    out = status_of(ss.steps(row(token_issued_at="t"), facts(agent=AGENT)))
    assert out["corrections"] == "current"
    assert out["installer"] == "done" and out["checkin"] == "done"


def test_corrections_explain_that_samples_wait_and_are_requeued():
    s = by_key(ss.steps(row(), facts(samples_received=3)))["corrections"]
    assert "wait" in s["detail"].lower()


# ── 3 installer ─────────────────────────────────────────────────────────────

def test_installer_done_when_a_token_was_issued():
    s = by_key(ss.steps(row(token_issued_at="2026-09-30T13:02:00+00:00"),
                        facts(corrections_count=11)))["installer"]
    assert s["status"] == "done"


def test_installer_current_after_corrections():
    s = by_key(ss.steps(row(), facts(corrections_count=11)))["installer"]
    assert s["status"] == "current" and s["action"] == "installer"


def test_installer_waiting_while_an_earlier_step_is_current():
    assert by_key(ss.steps(row(), facts()))["installer"]["status"] == "waiting"


def test_a_revoked_key_after_check_ins_asks_for_a_new_installer():
    s = by_key(ss.steps(row(token_issued_at=None), facts(corrections_count=11, agent=AGENT)))
    assert s["installer"]["status"] == "current"
    assert "new installer" in s["installer"]["detail"].lower()
    assert s["checkin"]["status"] == "done"


# ── 4 agent checks in (missing agent) ───────────────────────────────────────

def test_missing_agent_without_a_key_is_blocked_by_the_installer():
    s = by_key(ss.steps(row(), facts(corrections_count=11)))["checkin"]
    assert s["status"] == "blocked" and "step 3" in s["blocker"].lower()
    assert s["action"] is None


def test_missing_agent_with_a_key_is_current_and_waits_for_the_check_in():
    s = by_key(ss.steps(row(token_issued_at="t"), facts(corrections_count=11)))["checkin"]
    assert s["status"] == "current" and s["blocker"] is None
    assert "check" in s["detail"].lower() and s["action"] is None


def test_missing_agent_with_a_key_is_waiting_when_an_earlier_step_is_current():
    s = by_key(ss.steps(row(token_issued_at="t"), facts()))["checkin"]
    assert s["status"] == "waiting"


def test_agent_checked_in_is_done_and_names_the_computer():
    s = by_key(ss.steps(row(token_issued_at="t"), facts(agent=AGENT)))["checkin"]
    assert s["status"] == "done" and "GC2-PC" in s["detail"]
    assert s["done_at"] == AGENT["last_seen"]


def test_an_agent_row_without_last_seen_is_not_a_check_in():
    # set_agent_command inserts a row with only pending_command
    s = by_key(ss.steps(row(token_issued_at="t"), facts(
        corrections_count=11, agent={"last_seen": None, "version": None, "host": None})))["checkin"]
    assert s["status"] == "current"


# ── 5 calibration (not usable) ──────────────────────────────────────────────

def test_calibration_blocked_until_a_run_has_arrived():
    s = by_key(ss.steps(row(), facts()))["calibration"]
    assert s["status"] == "blocked" and "n-alkane" in s["blocker"]
    # the agent is in but nothing has arrived: still nothing to pick from
    s = by_key(ss.steps(row(token_issued_at="t"), facts(corrections_count=11, agent=AGENT)))["calibration"]
    assert s["status"] == "blocked"
    s = by_key(ss.steps(row(token_issued_at="t"), facts(corrections_count=11, agent=AGENT,
                                                        samples_received=1)))["calibration"]
    assert s["status"] == "current" and s["blocker"] is None


def test_calibration_not_usable_shows_the_problem():
    cal = {"usable": False, "problem": "Only 1 peak is assigned (at least 2 are needed).",
           "calibration_cdf": "cdf/gc2/cal.CDF", "assigned": 1}
    s = by_key(ss.steps(row(token_issued_at="t"), facts(
        corrections_count=11, agent=AGENT, calibration=cal, samples_received=2)))["calibration"]
    assert s["status"] == "current" and s["blocker"] is None
    assert "Only 1 peak" in s["detail"] and s["action"] == "calibration"


def test_a_set_but_unusable_calibration_is_not_blocked_even_without_runs():
    # a path-picked calibration CDF exists; nothing blocks assigning its peaks
    cal = {"usable": False, "problem": "No peaks are assigned.", "calibration_cdf": "/x/cal.CDF",
           "assigned": 0}
    s = by_key(ss.steps(row(), facts(calibration=cal)))["calibration"]
    assert s["status"] == "waiting" and "No peaks are assigned." in s["detail"]


def test_calibration_usable_is_done_and_says_how_many_peaks():
    ev = {"calibration_saved": {"by": "Ryan C (x)", "at": "2026-09-30T12:31:00+00:00"}}
    s = by_key(ss.steps(row(), facts(calibration=USABLE, events=ev)))["calibration"]
    assert s["status"] == "done" and "13" in s["detail"] and s["done_by"] == "Ryan C (x)"


def test_calibration_waiting_behind_an_earlier_current_step():
    s = by_key(ss.steps(row(), facts(samples_received=2)))["calibration"]
    assert s["status"] == "waiting"


# ── 6 method (unmapped) ─────────────────────────────────────────────────────

def test_method_blocked_until_the_first_run_arrives():
    s = by_key(ss.steps(row(), facts()))["method"]
    assert s["status"] == "blocked" and "first run" in s["blocker"].lower()


def test_unmapped_method_names_the_method_and_the_waiting_samples():
    m = {"mapped_seen": [], "unmapped_seen": [{"name": "SIMDIS_NEW.M", "count": 3}], "held": 3}
    s = by_key(ss.steps(row(token_issued_at="t"), facts(
        corrections_count=11, agent=AGENT, calibration=USABLE, samples_received=3,
        methods=m)))["method"]
    assert s["status"] == "current" and s["action"] == "methods"
    assert "SIMDIS_NEW.M" in s["detail"] and "3" in s["detail"]
    assert "other method" in s["detail"].lower() or "waiting" in s["detail"].lower()
    assert s["unmapped"] == ["SIMDIS_NEW.M"]


def test_unmapped_run_with_no_method_name_is_described():
    m = {"mapped_seen": [], "unmapped_seen": [{"name": "", "count": 1}], "held": 1}
    s = by_key(ss.steps(row(), facts(samples_received=1, methods=m)))["method"]
    assert "no method name" in s["detail"].lower()
    assert s["unmapped"] == []


def test_method_done_once_a_seen_method_is_mapped():
    m = {"mapped_seen": ["SIMDISB.M"], "unmapped_seen": [], "held": 0}
    s = by_key(ss.steps(row(), facts(samples_received=1, methods=m)))["method"]
    assert s["status"] == "done" and "SIMDISB.M" in s["detail"]


def test_method_waiting_behind_an_earlier_step():
    m = {"mapped_seen": [], "unmapped_seen": [{"name": "X.M", "count": 1}], "held": 1}
    assert by_key(ss.steps(row(), facts(samples_received=1, methods=m)))["method"]["status"] == "waiting"


# ── 7 results file and go live (live_since unset) ───────────────────────────

def test_live_since_unset_explains_backfill_in_plain_words():
    s = by_key(ss.steps(row(), facts(samples_received=4, backfill_unreleased=4)))["go_live"]
    assert s["status"] == "waiting" and s["action"] == "go_live"
    assert "4" in s["detail"] and "backfill" in s["detail"].lower()
    assert "never" in s["detail"].lower()


def test_live_since_unset_is_current_when_everything_before_is_done():
    s = by_key(ss.steps(row(token_issued_at="t"), complete_facts(first_result=False)))["go_live"]
    assert s["status"] == "current"


def test_go_live_names_the_results_file_default_or_configured():
    s = by_key(ss.steps(row(live_since="2026-10-01 08:00:00"), facts()))["go_live"]
    assert s["status"] == "done"
    assert "results/gc2_results.csv" in s["detail"] and "2026-10-01 08:00" in s["detail"]
    ex = {"path": r"\\asapserver\share\gc2.csv", "configured": True, "refused": None}
    s = by_key(ss.steps(row(live_since="2026-10-01 08:00:00"), facts(export=ex)))["go_live"]
    assert r"\\asapserver\share\gc2.csv" in s["detail"]


def test_go_live_done_by_comes_from_the_live_since_event():
    ev = {"live_since": {"by": "Ryan C (x)", "at": "2026-09-30T14:00:00+00:00"}}
    s = by_key(ss.steps(row(live_since="2026-10-01 08:00:00"), facts(events=ev)))["go_live"]
    assert s["done_by"] == "Ryan C (x)"


# ── 8 first result ──────────────────────────────────────────────────────────

def test_first_result_done():
    s = by_key(ss.steps(row(token_issued_at="t", live_since="2026-10-01 08:00:00"),
                        complete_facts()))["first_result"]
    assert s["status"] == "done"


def test_first_result_blocked_names_the_first_missing_prerequisite():
    s = by_key(ss.steps(row(), facts()))["first_result"]
    assert s["status"] == "blocked" and "step 2" in s["blocker"].lower()
    s = by_key(ss.steps(row(token_issued_at="t"), complete_facts(first_result=False)))["first_result"]
    assert s["status"] == "blocked" and "step 7" in s["blocker"].lower()
    assert "backfill" in s["blocker"].lower()


def test_first_result_blocked_by_an_unusable_calibration():
    cal = {"usable": False, "problem": "x", "calibration_cdf": "c", "assigned": 1}
    s = by_key(ss.steps(row(token_issued_at="t", live_since="2026-10-01 08:00:00"),
                        complete_facts(first_result=False, calibration=cal)))["first_result"]
    assert s["status"] == "blocked" and "step 5" in s["blocker"].lower()


def test_first_result_does_not_need_the_agent_when_runs_came_another_way():
    # a folder load: runs arrived with no agent
    f = complete_facts(first_result=False, agent=None)
    s = by_key(ss.steps(row(token_issued_at="t", live_since="2026-10-01 08:00:00"), f))
    assert s["checkin"]["status"] == "current"
    assert s["first_result"]["status"] == "waiting"


def test_first_result_current_when_everything_is_ready():
    s = by_key(ss.steps(row(token_issued_at="t", live_since="2026-10-01 08:00:00"),
                        complete_facts(first_result=False)))["first_result"]
    assert s["status"] == "current" and "Written to results CSV".lower() not in s["detail"].lower()
    assert "GC-2" in s["detail"]


def test_first_result_blocked_by_a_refused_results_file():
    ex = {"path": "p.csv", "configured": True, "refused": "foreign"}
    s = by_key(ss.steps(row(token_issued_at="t", live_since="2026-10-01 08:00:00"),
                        complete_facts(first_result=False, export=ex)))["first_result"]
    assert s["status"] == "blocked" and "refused" in s["blocker"].lower()


# ── summary ─────────────────────────────────────────────────────────────────

def test_summary_step_n_of_8_is_the_current_step():
    sm = ss.summary(ss.steps(row(), facts()))
    assert sm == {"total": 8, "done": 1, "step": 2, "ready": False}
    sm = ss.summary(ss.steps(row(token_issued_at="t", live_since="2026-10-01 08:00:00"),
                             complete_facts()))
    assert sm == {"total": 8, "done": 8, "step": None, "ready": True}


# ── gather: the facts from a real store ─────────────────────────────────────

store = pytest.importorskip("store")


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "gc.db"
    store.migrate(path)
    store.instruments.upsert({"id": "gc2", "name": "GC-2",
                              "method_map": json.dumps({"SIMDISB.M": "D2887"})}, db=path)
    return path


def _sample(db, lab, status, method="SIMDISB.M", backfill=0, released=None):
    sid = store.samples.insert_received("gc2", lab, f"2026-09-30 0{len(lab) % 9}:{lab[-2:]}:00", "cdf",
                                        cdf_sha256="sha" + lab, cdf_path=f"cdf/{lab}.CDF",
                                        method_name=method, backfill=backfill, status=status, db=db)
    if released:
        store.samples.update(sid, released_at=released, db=db)
    return sid


def test_gather_on_an_empty_instrument(db):
    f = ss.gather("gc2", {}, db=db, data_dir=db.parent)
    assert f["corrections_count"] == 0 and f["agent"] is None
    assert f["samples_received"] == 0 and f["backfill_unreleased"] == 0
    assert f["first_result"] is False and f["events"] == {}
    assert f["calibration"]["usable"] is False
    assert f["export"]["path"].replace("\\", "/").endswith("results/gc2_results.csv")
    assert f["export"]["configured"] is False
    assert f["methods"] == {"mapped_seen": [], "unmapped_seen": [], "held": 0}


def test_gather_counts_methods_backfill_and_the_first_result(db):
    a = _sample(db, "40301", "final")
    _sample(db, "40302", "other_method", method="NEW.M", backfill=1)
    _sample(db, "40303", "review_method", method="")
    with store.connection(db) as conn:
        with store.write_txn(conn):
            store.add_revision(conn, a, {"IBP": 1}, reason="processed", by="w")
            seq = store.export_rows.append_pending(conn, "gc2", a, 1, "row")
    f = ss.gather("gc2", {}, db=db, data_dir=db.parent)
    assert f["samples_received"] == 3 and f["backfill_unreleased"] == 1
    assert f["methods"]["mapped_seen"] == ["SIMDISB.M"]
    assert {m["name"] for m in f["methods"]["unmapped_seen"]} == {"NEW.M", ""}
    assert f["methods"]["held"] == 2
    assert f["first_result"] is False                 # not appended yet
    store.export_rows.mark_hub_appended(seq, db=db)
    assert ss.gather("gc2", {}, db=db, data_dir=db.parent)["first_result"] is True


def test_gather_reads_the_agent_corrections_and_latest_events(db):
    with store.connection(db) as conn:
        conn.execute("INSERT INTO agents(instrument_id, last_seen, version, host) "
                     "VALUES ('gc2', '2026-09-30T12:00:00+00:00', '2.4.1', 'GC2-PC')")
        with store.write_txn(conn):
            store.corrections.set_all(conn, "gc2", {c: 0.0 for c in (
                "IBP", "5%", "10%", "20%", "30%", "50%", "70%", "80%", "90%", "95%", "FBP")},
                by="Ryan C (x)", reason="r")
            store.instrument_events.add(conn, "gc2", "calibration_saved", by="A (1)", detail={"n": 1})
            store.instrument_events.add(conn, "gc2", "calibration_saved", by="B (2)", detail={"n": 2})
    f = ss.gather("gc2", {}, db=db, data_dir=db.parent)
    assert f["agent"]["host"] == "GC2-PC" and f["agent"]["last_seen"].startswith("2026-09-30")
    assert f["corrections_count"] == 11
    assert f["events"]["calibration_saved"]["by"] == "B (2)"
    assert f["events"]["calibration_saved"]["detail"] == {"n": 2}
