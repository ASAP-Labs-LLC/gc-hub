"""comments_api.py routes (phase 4), on the real app booted in a subprocess
(``tests/bootapp.py``): sample comments, the preset list, the admin preset
editor, the limits, soft delete, the cross-site guard, and (sign-in, D6 rev 2)
the author: the session's name, with initials derived from it and any
``initials`` in the body ignored.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")

import store  # noqa: E402
from bootapp import (booted, cookie_header, get, post, send, setup_admin,  # noqa: E402
                     sign_in, wait_for)

JSON = {"Content-Type": "application/json"}


@pytest.fixture(scope="module")
def hub():
    tmp = Path(tempfile.mkdtemp(prefix="gc-p4-comments-"))
    try:
        with booted(tmp) as (port, _proc, data, _home):
            db = data / store.DB_FILENAME
            assert wait_for(lambda: db.is_file()
                            and store.instruments.get("gc1", db=db) is not None, timeout=30)
            pw = setup_admin(port, data)
            yield port, db, pw, data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_n = [0]


def _sample(db) -> int:
    _n[0] += 1
    return store.samples.insert_received("gc1", f"C{_n[0]}", "2026-09-25 00:24:50", "cdf",
                                         cdf_sha256=f"sha-api-{_n[0]}",
                                         cdf_path=f"cdf/C{_n[0]}.CDF", method_name="SIMDISB.M",
                                         db=db)


def _raw(port, path, body: dict, headers=None):
    return send(port, path, json.dumps(body).encode(), dict(JSON, **(headers or {})))


def test_add_list_and_delete_a_free_comment(hub):
    port, db, _pw, data = hub
    sid = _sample(db)
    assert get(port, f"/api/samples/{sid}/comments") == (200, {"comments": []})
    # the body's initials are ignored: the session (Test Operator) decides
    code, body = post(port, f"/api/samples/{sid}/comments", {"text": "Odd hump", "initials": "rb"})
    assert code == 201, body
    c = body["comment"]
    assert c["text"] == "Odd hump" and c["initials"] == "TO" and c["source"] == "free"
    assert c["name"] == "Test Operator"
    code, body = get(port, f"/api/samples/{sid}/comments")
    assert code == 200 and [x["id"] for x in body["comments"]] == [c["id"]]
    assert "127.0.0.1" not in json.dumps(body) and "author_ip" not in json.dumps(body)
    row = store.sample_comments.get(c["id"], db=db)
    assert row["author_ip"] == "127.0.0.1" and row["author_name"] == "Test Operator"

    jane = cookie_header(port, sign_in(port, data, "Jane Doe", remember=False))
    code, body = post(port, f"/api/samples/{sid}/comments/{c['id']}/delete", {"initials": "XX"},
                      headers=jane)
    assert code == 200, body
    row = store.sample_comments.get(c["id"], db=db)
    assert row["deleted_at"] and row["deleted_by_initials"] == "JD"
    assert row["deleted_by_name"] == "Jane Doe" and row["deleted_by_ip"] == "127.0.0.1"
    assert get(port, f"/api/samples/{sid}/comments")[1] == {"comments": []}
    assert post(port, f"/api/samples/{sid}/comments/{c['id']}/delete", {})[0] == 409


def test_comments_need_a_session(hub):
    port, db, _pw, _data = hub
    sid = _sample(db)
    url = f"/api/samples/{sid}/comments"
    assert get(port, url, auth=False)[0] == 401
    code, body = post(port, url, {"text": "anon", "initials": "RB"}, auth=False)
    assert code == 401 and body["login_required"] is True
    assert store.sample_comments.list(sid, db=db) == []


def test_refusals(hub):
    port, db, _pw, _data = hub
    sid = _sample(db)
    other = _sample(db)
    url = f"/api/samples/{sid}/comments"
    assert get(port, "/api/samples/987654/comments")[0] == 404
    assert post(port, "/api/samples/987654/comments", {"text": "x", "initials": "RB"})[0] == 404
    assert post(port, url, {"text": "x" * 501, "initials": "RB"})[0] == 400
    assert post(port, url, {"text": "   ", "initials": "RB"})[0] == 400
    assert post(port, url, ["x"])[0] == 400
    # JSON only, 64 KiB cap
    assert send(port, url, b'{"text": "x", "initials": "RB"}', {"Content-Type": "text/plain"})[0] \
        == 415
    assert send(port, url, b"text=x&initials=RB",
                {"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
    big = json.dumps({"text": "x", "initials": "RB", "pad": "y" * (65 * 1024)}).encode()
    assert send(port, url, big, JSON)[0] == 413
    # a comment of another sample can't be deleted through this one
    code, body = post(port, url, {"text": "mine", "initials": "RB"})
    assert code == 201
    assert post(port, f"/api/samples/{other}/comments/{body['comment']['id']}/delete",
                {"initials": "RB"})[0] == 404
    assert post(port, f"/api/samples/{sid}/comments/99999/delete", {"initials": "RB"})[0] == 404
    assert send(port, f"/api/samples/{sid}/comments/{body['comment']['id']}/delete",
                b'{"initials": "RB"}', {"Content-Type": "text/plain"})[0] == 415
    assert len(get(port, url)[1]["comments"]) == 1


def test_cross_site_writes_are_refused(hub):
    port, db, _pw, _data = hub
    sid = _sample(db)
    url = f"/api/samples/{sid}/comments"
    body = {"text": "x", "initials": "RB"}
    assert _raw(port, url, body, {"Origin": "http://evil.example"})[0] == 403
    assert _raw(port, url, body, {"Sec-Fetch-Site": "cross-site"})[0] == 403
    code, ok = _raw(port, url, body, {"Origin": f"http://127.0.0.1:{port}",
                                      "Sec-Fetch-Site": "same-origin"})
    assert code == 201, ok
    cid = ok["comment"]["id"]
    assert _raw(port, f"{url}/{cid}/delete", {"initials": "RB"},
                {"Origin": "http://evil.example"})[0] == 403
    admin = {"password": "x", "action": "list"}
    assert _raw(port, "/api/admin/comment-presets", admin, {"Origin": "http://evil.example"})[0] \
        == 403
    assert len(get(port, url)[1]["comments"]) == 1


def test_at_most_100_active_comments(hub):
    port, db, _pw, _data = hub
    sid = _sample(db)
    for i in range(99):
        store.sample_comments.add(sid, text=f"c{i}", source="free", author_initials="RB",
                                  author_ip=None, revision=None, db=db)
    url = f"/api/samples/{sid}/comments"
    assert post(port, url, {"text": "100th", "initials": "RB"})[0] == 201
    code, body = post(port, url, {"text": "101st", "initials": "RB"})
    assert code == 409 and "100" in body["error"]


def test_preset_comment_copies_the_text(hub):
    port, db, _pw, _data = hub
    sid = _sample(db)
    code, body = get(port, "/api/comment-presets")
    assert code == 200
    presets = body["presets"]
    assert [p["text"] for p in presets][:2] == [
        "Sample appears to be a renewable fuel, not conventional petroleum diesel.",
        "Sample appears to be gasoline."]
    assert set(presets[0]) == {"id", "text", "sort"}
    code, body = post(port, f"/api/samples/{sid}/comments",
                      {"preset_id": presets[1]["id"], "initials": "RB"})
    assert code == 201, body
    assert body["comment"]["text"] == presets[1]["text"]
    assert body["comment"]["source"] == "preset"
    assert post(port, f"/api/samples/{sid}/comments", {"preset_id": 99999, "initials": "RB"})[0] \
        == 404


def test_annotation_comment_route(hub):
    port, db, _pw, _data = hub
    sid = _sample(db)
    code, body = post(port, f"/api/samples/{sid}/comments",
                      {"text": "", "initials": "RB", "t0": 2.0, "t1": 1.5})
    assert code == 201, body
    c = body["comment"]
    assert c["source"] == "annotation" and (c["t0"], c["t1"]) == (1.5, 2.0)
    assert c["text"] == "Marked region 1.50–2.00 min"          # no revision → minutes
    assert post(port, f"/api/samples/{sid}/comments",
                {"text": "x", "initials": "RB", "t0": 1.0})[0] == 400


# ── admin presets ───────────────────────────────────────────────────────────

def test_admin_presets_need_json_and_the_password(hub):
    port, _db, _pw, _data = hub
    url = "/api/admin/comment-presets"
    assert send(port, url, b'{"password": "x", "action": "list"}',
                {"Content-Type": "text/plain"})[0] == 415
    assert post(port, url, {"password": "wrong", "action": "create", "text": "nope"})[0] == 403
    assert "nope" not in [p["text"] for p in get(port, "/api/comment-presets")[1]["presets"]]


def test_admin_presets_create_update_reorder_deactivate(hub):
    port, _db, pw, _data = hub
    url = "/api/admin/comment-presets"
    code, body = post(port, url, {"password": pw, "action": "list"})
    assert code == 200 and len(body["presets"]) >= 4
    assert all({"id", "text", "sort", "active"} <= set(p) for p in body["presets"])

    code, body = post(port, url, {"password": pw, "action": "create", "text": "Admin preset"})
    assert code == 201, body
    pid = body["preset"]["id"]
    assert get(port, "/api/comment-presets")[1]["presets"][-1]["text"] == "Admin preset"
    assert post(port, url, {"password": pw, "action": "create", "text": "x" * 201})[0] == 400

    assert post(port, url, {"password": pw, "action": "update", "id": pid,
                            "text": "Admin preset (edited)"})[0] == 200
    assert post(port, url, {"password": pw, "action": "update", "id": 99999, "text": "x"})[0] \
        == 404

    ids = [p["id"] for p in post(port, url, {"password": pw, "action": "list"})[1]["presets"]]
    new = [pid] + [i for i in ids if i != pid]
    code, body = post(port, url, {"password": pw, "action": "reorder", "ids": new})
    assert code == 200, body
    assert [p["id"] for p in get(port, "/api/comment-presets")[1]["presets"]][0] == pid
    assert post(port, url, {"password": pw, "action": "reorder", "ids": new[1:]})[0] == 400

    assert post(port, url, {"password": pw, "action": "deactivate", "id": pid})[0] == 200
    assert pid not in [p["id"] for p in get(port, "/api/comment-presets")[1]["presets"]]
    listed = post(port, url, {"password": pw, "action": "list"})[1]["presets"]
    assert [p["active"] for p in listed if p["id"] == pid] == [0]
    assert post(port, url, {"password": pw, "action": "activate", "id": pid})[0] == 200
    assert pid in [p["id"] for p in get(port, "/api/comment-presets")[1]["presets"]]
    assert post(port, url, {"password": pw, "action": "explode"})[0] == 400


# ── v6: conclusion presets (the same table under the name the UI uses) ──────

def test_conclusion_presets_are_the_comment_presets_under_their_v6_names(hub):
    """v6 merged comments into the conclusion: presets are *conclusion
    presets*. ``/api/conclusion-presets`` and ``/api/admin/conclusion-presets``
    serve the same rows as the v5 paths, which keep working (a v5 page still
    open in a browser, the classic page)."""
    port, _db, pw, _data = hub
    code, new = get(port, "/api/conclusion-presets")
    assert code == 200 and new == get(port, "/api/comment-presets")[1]
    assert len(new["presets"]) >= 4 and all(set(p) == {"id", "text", "sort"}
                                            for p in new["presets"])
    url = "/api/admin/conclusion-presets"
    code, body = post(port, url, {"password": pw, "action": "create",
                                  "text": "Consistent with ULSD."})
    assert code == 201, body
    pid = body["preset"]["id"]
    assert pid in [p["id"] for p in get(port, "/api/conclusion-presets")[1]["presets"]]
    assert pid in [p["id"] for p in post(port, "/api/admin/comment-presets",
                                         {"password": pw, "action": "list"})[1]["presets"]]
    assert post(port, url, {"password": "wrong", "action": "list"})[0] == 403
    assert post(port, url, {"password": pw, "action": "deactivate", "id": pid})[0] == 200
    assert get(port, "/api/conclusion-presets", auth=False)[0] == 401


# ── v6: the conclusion limit ────────────────────────────────────────────────

def test_conclusion_limit_is_one_constant_mirrored_in_compare_logic():
    import re
    import comments
    assert comments.CONCLUSION_MAX == 1500
    src = (TESTS.parent / "static" / "js" / "compare_logic.js").read_text(encoding="utf-8")
    assert re.search(r"const CONCLUSION_MAX = (\d+);", src).group(1) == str(comments.CONCLUSION_MAX)
    assert comments.conclusion_problem("x" * 1500) is None
    assert comments.conclusion_problem("  " + "x" * 1500 + "\n") is None     # trimmed
    assert comments.conclusion_problem(None) is None
    msg = comments.conclusion_problem("x" * 1501)
    assert msg and "1,500" in msg and "1,501" in msg


def test_every_report_path_refuses_a_conclusion_over_the_limit(hub):
    """``_report_request`` refuses a conclusion over 1,500 characters with a
    400 naming the limit, on every path: the analysis, the direct export,
    the ZIP (before a job starts) and the QBench queue (before anything is
    queued). Exactly 1,500 passes the check."""
    port, db, _pw, _data = hub
    sid = _sample(db)
    long = "x" * 1501
    item = {"sample_id": sid, "standard_name": "Base", "conclusion": long}
    for path, body in (("/api/analysis", item),
                       ("/api/export-analysis-report", item),
                       ("/api/export-analysis-reports-zip", {"items": [item]}),
                       ("/api/qbench-upload", {"queue": [item]})):
        code, answer = post(port, path, body)
        assert code == 400, (path, code, answer)
        assert "1,500" in answer["error"], (path, answer)
    # at the limit the conclusion check passes (this sample has no CDF/standard:
    # whatever refuses it, it is not the conclusion)
    code, answer = post(port, "/api/export-analysis-reports-zip",
                        {"items": [dict(item, conclusion="x" * 1500)]})
    assert "1,500" not in str(answer), answer
