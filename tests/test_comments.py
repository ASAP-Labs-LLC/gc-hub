"""comments.py (phase 4): the comment and preset rules, ``for_report`` (the
frozen seam P3's report builder reads) and ``log_report`` (the report_log
writer P3 calls). In-process, stdlib + store only: no Flask, no app.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import comments
import store


@pytest.fixture()
def db(tmp_path) -> Path:
    path = tmp_path / "gc.db"
    store.migrate(path)
    store.instruments.upsert({"id": "gc1", "name": "GC-1"}, db=path)
    return path


def _sample(db, lab_id="40305", anchors=None):
    sid = store.samples.insert_received("gc1", lab_id, "2026-09-25 00:24:50", "cdf",
                                        cdf_sha256=f"sha-{lab_id}", cdf_path=f"cdf/{lab_id}.CDF",
                                        method_name="SIMDISB.M", db=db)
    if anchors is not None:
        with store.connection(db) as conn:
            with store.write_txn(conn):
                store.add_revision(conn, sid, {"IBP": 1}, reason="processed",
                                   calibration_used={"anchors": anchors})
    return sid


ANCHORS = [[1.0, 10], [2.0, 12], [3.0, 14]]


def _err(fn, status):
    with pytest.raises(comments.CommentError) as ei:
        fn()
    assert ei.value.status == status, ei.value
    return ei.value


# ── initials ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,norm", [("ab", "AB"), (" rb ", "RB"), ("A", "A"), ("abcd", "ABCD")])
def test_initials_are_upper_cased(raw, norm):
    assert comments.normalize_initials(raw) == norm


@pytest.mark.parametrize("raw", ["", "   ", "ABCDE", "A1", "A.B", "é", None, 12, "A B"])
def test_bad_initials_are_refused(raw):
    _err(lambda: comments.normalize_initials(raw), 400)


# ── adding ──────────────────────────────────────────────────────────────────

def test_free_comment(db):
    sid = _sample(db, anchors=ANCHORS)
    c = comments.add_comment(sid, initials="rb", text="  Looks odd  ", author_ip="10.0.0.5", db=db)
    assert c["text"] == "Looks odd" and c["source"] == "free" and c["initials"] == "RB"
    assert c["revision"] == 1 and c["t0"] is None and c["t1"] is None
    assert "author_ip" not in c and "ip" not in json.dumps(c)
    row = store.sample_comments.get(c["id"], db=db)
    assert row["author_ip"] == "10.0.0.5" and row["author_initials"] == "RB"


def test_unknown_sample_is_404(db):
    _err(lambda: comments.add_comment(424242, initials="RB", text="x", db=db), 404)


def test_text_limits(db):
    sid = _sample(db)
    assert comments.add_comment(sid, initials="RB", text="x" * 500, db=db)["text"] == "x" * 500
    _err(lambda: comments.add_comment(sid, initials="RB", text="x" * 501, db=db), 400)
    _err(lambda: comments.add_comment(sid, initials="RB", text="   ", db=db), 400)
    _err(lambda: comments.add_comment(sid, initials="RB", db=db), 400)
    _err(lambda: comments.add_comment(sid, initials="RB", text=["x"], db=db), 400)
    _err(lambda: comments.add_comment(sid, initials="", text="x", db=db), 400)


def test_at_most_100_active_comments(db):
    sid = _sample(db)
    first = None
    for i in range(100):
        c = comments.add_comment(sid, initials="RB", text=f"c{i}", db=db)
        first = first or c
    _err(lambda: comments.add_comment(sid, initials="RB", text="one too many", db=db), 409)
    comments.delete_comment(sid, first["id"], initials="RB", db=db)   # deleted ones don't count
    assert comments.add_comment(sid, initials="RB", text="fits again", db=db)


def test_preset_text_is_copied(db):
    sid = _sample(db)
    preset = store.comment_presets.list(db=db)[1]
    c = comments.add_comment(sid, initials="JD", preset_id=preset["id"], db=db)
    assert c["text"] == preset["text"] and c["source"] == "preset" and c["preset_id"] == preset["id"]
    comments.update_preset(preset["id"], text="Edited later", db=db)
    assert comments.list_comments(sid, db=db)[0]["text"] == preset["text"]


def test_preset_refusals(db):
    sid = _sample(db)
    p = store.comment_presets.list(db=db)[0]
    _err(lambda: comments.add_comment(sid, initials="RB", preset_id=99999, db=db), 404)
    _err(lambda: comments.add_comment(sid, initials="RB", preset_id=p["id"], text="x", db=db), 400)
    _err(lambda: comments.add_comment(sid, initials="RB", preset_id=True, db=db), 400)
    _err(lambda: comments.add_comment(sid, initials="RB", preset_id=p["id"], t0=1, t1=2, db=db),
         400)
    comments.set_preset_active(p["id"], False, db=db)
    _err(lambda: comments.add_comment(sid, initials="RB", preset_id=p["id"], db=db), 400)


def test_annotation_comment(db):
    sid = _sample(db, anchors=ANCHORS)
    c = comments.add_comment(sid, initials="RB", text="Hump", t0=2.5, t1=1.5, db=db)
    assert c["source"] == "annotation" and (c["t0"], c["t1"]) == (1.5, 2.5)
    assert c["text"] == "Hump"


def test_annotation_default_text_uses_the_revision_ladder(db):
    sid = _sample(db, anchors=ANCHORS)
    c = comments.add_comment(sid, initials="RB", text="", t0=1.5, t1=2.5, db=db)
    assert c["text"] == "Marked region C11–C13"
    # beyond the ladder: linear extrapolation from the end pairs
    c = comments.add_comment(sid, initials="RB", t0=3.0, t1=4.0, db=db)
    assert c["text"] == "Marked region C14–C16"


def test_annotation_default_text_without_a_ladder(db):
    sid = _sample(db)                                   # no revision, no anchors
    c = comments.add_comment(sid, initials="RB", t0=1.234, t1=1.5, db=db)
    assert c["text"] == "Marked region 1.23–1.50 min"


@pytest.mark.parametrize("t0,t1", [(1.0, None), (None, 2.0), ("1", 2.0), (1.0, float("nan")),
                                   (1.0, float("inf")), (True, 2.0), (1.0, 1.0)])
def test_bad_annotation_spans(db, t0, t1):
    sid = _sample(db)
    _err(lambda: comments.add_comment(sid, initials="RB", text="x", t0=t0, t1=t1, db=db), 400)


# ── deleting ────────────────────────────────────────────────────────────────

def test_delete_is_soft_and_recorded(db):
    sid = _sample(db)
    c = comments.add_comment(sid, initials="RB", text="x", author_ip="10.0.0.5", db=db)
    out = comments.delete_comment(sid, c["id"], initials="jd", author_ip="10.0.0.9", db=db)
    assert out["id"] == c["id"]
    row = store.sample_comments.get(c["id"], db=db)
    assert row["deleted_at"] and row["deleted_by_initials"] == "JD"
    assert row["deleted_by_ip"] == "10.0.0.9" and row["text"] == "x"
    assert comments.list_comments(sid, db=db) == []
    _err(lambda: comments.delete_comment(sid, c["id"], initials="JD", db=db), 409)


def test_delete_refusals(db):
    sid = _sample(db)
    other = _sample(db, lab_id="40400")
    c = comments.add_comment(sid, initials="RB", text="x", db=db)
    _err(lambda: comments.delete_comment(other, c["id"], initials="RB", db=db), 404)
    _err(lambda: comments.delete_comment(sid, 99999, initials="RB", db=db), 404)
    _err(lambda: comments.delete_comment(sid, c["id"], initials="", db=db), 400)
    assert len(comments.list_comments(sid, db=db)) == 1


# ── the report seam ─────────────────────────────────────────────────────────

def test_for_report_shape(db):
    sid = _sample(db, anchors=ANCHORS)
    a = comments.add_comment(sid, initials="RB", text="first <img src=x>", author_ip="1.2.3.4",
                             db=db)
    b = comments.add_comment(sid, initials="JD", text="span", t0=1.0, t1=2.0, db=db)
    d = comments.add_comment(sid, initials="JD", text="gone", db=db)
    comments.delete_comment(sid, d["id"], initials="JD", db=db)
    rows = comments.for_report(sid, db)
    assert [set(r) for r in rows] == [{"id", "text", "initials", "created_at", "t0", "t1"}] * 2
    assert [r["id"] for r in rows] == [a["id"], b["id"]]              # time order, no deleted
    assert rows[0] == {"id": a["id"], "text": "first <img src=x>", "initials": "RB",
                       "created_at": a["created_at"], "t0": None, "t1": None}
    assert (rows[1]["t0"], rows[1]["t1"]) == (1.0, 2.0)
    assert "1.2.3.4" not in json.dumps(rows)
    assert comments.for_report(sid, db=db) == rows
    assert comments.for_report(424242, db) == []


def test_log_report_writes_the_spec_fields(db):
    sid = _sample(db, anchors=ANCHORS)
    rid = comments.log_report(
        sid, kind="qbench", revision=1, standard_name="Diesel #2",
        params={"quantile": 0.2, "window": 301}, ranges=[{"label": "Gas", "c_start": 5,
                                                           "c_end": 11}],
        windows=[{"label": "Gas", "t0": 0.4, "t1": 1.2}], bullets=[{"kind": "none"}],
        bullets_text="No deviations above the marginal threshold.", conclusion="Fine.",
        conclusion_edited=True, comment_ids=[3, 4], pdf_sha256="ab" * 32,
        author_initials="rb", author_ip="10.0.0.5", db=db)
    row = store.report_log.list(sid, db=db)[0]
    assert row["id"] == rid and row["kind"] == "qbench" and row["revision"] == 1
    assert json.loads(row["params_json"]) == {"quantile": 0.2, "window": 301}
    assert json.loads(row["ranges_json"])[0]["label"] == "Gas"
    assert json.loads(row["windows_json"])[0]["t1"] == 1.2
    assert json.loads(row["bullets_json"]) == [{"kind": "none"}]
    assert json.loads(row["comment_ids_json"]) == [3, 4]
    assert row["conclusion_edited"] == 1 and row["bullets_text"].startswith("No deviations")
    assert row["author_initials"] == "RB" and row["author_ip"] == "10.0.0.5"
    assert row["app_version"] and row["created_at"]
    with pytest.raises(ValueError):
        comments.log_report(sid, kind="email", db=db)


def test_log_report_minimal(db):
    sid = _sample(db)
    comments.log_report(sid, kind="download", db=db)
    row = store.report_log.list(sid, db=db)[0]
    assert row["params_json"] is None and row["conclusion_edited"] is None
    assert row["author_initials"] is None


# ── presets ─────────────────────────────────────────────────────────────────

def test_preset_crud_and_limits(db):
    p = comments.create_preset("  Needs review  ", by="admin@1.2.3.4", db=db)
    assert p["text"] == "Needs review" and p["active"] == 1
    _err(lambda: comments.create_preset("x" * 201, by="a", db=db), 400)
    _err(lambda: comments.create_preset("", by="a", db=db), 400)
    assert comments.create_preset("y" * 200, by="a", db=db)
    comments.update_preset(p["id"], text="Renamed", db=db)
    assert store.comment_presets.get(p["id"], db=db)["text"] == "Renamed"
    _err(lambda: comments.update_preset(99999, text="x", db=db), 404)
    _err(lambda: comments.update_preset(p["id"], text="x" * 201, db=db), 400)
    comments.set_preset_active(p["id"], False, db=db)
    assert p["id"] not in [q["id"] for q in comments.list_presets(db=db)]

    while store.comment_presets.count_active(db=db) < comments.MAX_PRESETS:
        comments.create_preset("more", by="a", db=db)
    _err(lambda: comments.create_preset("one too many", by="a", db=db), 409)
    _err(lambda: comments.set_preset_active(p["id"], True, db=db), 409)   # would be 51


def test_reorder_must_name_every_preset(db):
    ids = [q["id"] for q in comments.list_presets(include_inactive=True, db=db)]
    comments.reorder_presets(list(reversed(ids)), db=db)
    assert [q["id"] for q in comments.list_presets(include_inactive=True, db=db)] == \
        list(reversed(ids))
    _err(lambda: comments.reorder_presets(ids[:-1], db=db), 400)
    _err(lambda: comments.reorder_presets(ids + [99999], db=db), 400)
    _err(lambda: comments.reorder_presets(ids + ids[:1], db=db), 400)
    _err(lambda: comments.reorder_presets("1,2", db=db), 400)
