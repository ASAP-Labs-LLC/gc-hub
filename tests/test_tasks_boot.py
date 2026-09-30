"""The running-now feed end to end (v4.0 lane E): the booted app's
``GET /api/live`` carries ``tasks``. A reprocess batch shows as a task with
progress, then its outcome; an admin job shows its title, progress and who
started it to every signed-in user, never its folder; nothing but a task's
owner gets a download link."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

from bootapp import (TEST_USER, booted, cookie_header, get, post, setup_admin,  # noqa: E402
                     sign_in, wait_for)
from hub_boot import build_hub  # noqa: E402


def _tasks(port, headers=None):
    code, body = get(port, "/api/live", headers=headers)
    assert code == 200, body
    return body["tasks"]


def test_tasks_in_the_live_feed(tmp_path):
    hub = build_hub(tmp_path)
    with booted(tmp_path) as (port, _proc, data, _home):
        assert _tasks(port) == []
        other = cookie_header(port, sign_in(port, data, "Jo Bloggs", remember=False))

        # a reprocess batch: a task, then its outcome
        final, rerun = hub.ids["final"], hub.ids["rerun"]
        code, body = post(port, "/api/reprocess", {"sample_ids": [final, rerun]})
        assert code == 200 and body["count"] == 2, body
        [t] = _tasks(port)
        assert t["kind"] == "reprocess" and t["title"] == "Re-process · 2 samples"
        assert t["by"] == TEST_USER and t["mine"] is True
        assert wait_for(lambda: _tasks(port)[0]["state"] != "running", timeout=60)
        t = _tasks(port)[0]
        assert t["state"] == "done", t
        assert t["progress"]["done"] == 2 and t["progress"]["total"] == 2
        assert t["ended_at"]
        # somebody else sees it too, as not theirs
        [seen] = _tasks(port, other)
        assert seen["id"] == t["id"] and seen["mine"] is False

        # an admin job: title and progress for everyone, never the folder
        pw = setup_admin(port, data)
        empty = tmp_path / "SECRET-FOLDER"
        empty.mkdir()
        code, body = post(port, "/api/admin/load-folder",
                          {"password": pw, "instrument": "gc1", "folder": str(empty)})
        assert code == 202, body
        assert wait_for(lambda: any(x["kind"] == "load-folder" and x["state"] == "done"
                                    for x in _tasks(port, other)), timeout=30)
        job = [x for x in _tasks(port, other) if x["kind"] == "load-folder"][0]
        assert job["title"].startswith("Loading CDFs into ")
        assert job["by"] == TEST_USER and job["open_url"] == "/admin/hub#load-folder"
        feed = repr(_tasks(port, other))
        assert "SECRET-FOLDER" not in feed and "127.0.0.1" not in feed
        assert all(x["download_url"] is None for x in _tasks(port, other))

        # the sample list's rows say where their injection time came from
        code, files = get(port, f"/api/files?ids={final}")
        assert files["samples"][0]["injection_dt_source"] == "cdf"

        # the hub's queue counts and pause state ride along
        code, live = get(port, "/api/live")
        assert live["hub"]["processing_paused"] is False
        assert set(live["hub"]["queue"]) == {"waiting", "running"}
