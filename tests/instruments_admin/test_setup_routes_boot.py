"""v3.1 in the booted app: the new pages (/instruments, /instruments/<id>,
/setup; the old page at /instruments/classic), ``GET /api/instruments/<id>/setup``
and ``GET /api/instruments/activity`` (session), the setup summary on
``/api/instruments``, and events written by the admin routes with
``by`` = ``web_auth.actor()``."""
from __future__ import annotations

from a2_helpers import Hub, cal_entries  # noqa: F401

import json
import urllib.error
from pathlib import Path

import pytest

pytest.importorskip("flask")

from bootapp import TEST_USER, booted, get, get_text, post, setup_admin  # noqa: E402

ACTOR = f"{TEST_USER} (127.0.0.1)"


def _prepare(tmp: Path):
    hub = Hub(tmp)
    hub.gc1()
    return hub


def _status(port, path, **kw):
    try:
        get_text(port, path, timeout=15, **kw)
        return 200
    except urllib.error.HTTPError as e:
        return e.code


def test_the_new_pages_and_the_classic_page(tmp_path):
    _prepare(tmp_path)
    with booted(tmp_path) as (port, _proc, _data, _home):
        home = get_text(port, "/instruments", timeout=15)
        assert 'data-testid="instruments-page"' in home
        assert "instruments_home.js" in home and "tokens.css" in home
        # session.js is the first script on the page, as on every page
        first = home.index("<script")
        assert "js/session.js" in home[first:first + 200]

        classic = get_text(port, "/instruments/classic", timeout=15)
        assert "instruments.js" in classic and 'id="inst-list"' in classic

        detail = get_text(port, "/instruments/gc1", timeout=15)
        assert 'data-testid="instrument-detail-page"' in detail and 'data-instrument="gc1"' in detail
        assert _status(port, "/instruments/nope") == 404

        guide = get_text(port, "/setup?instrument=gc1", timeout=15)
        assert 'data-testid="setup-page"' in guide
        assert 'data-testid="setup-page"' in get_text(port, "/setup?new=1", timeout=15)

        # the shell: nav links go to the classic pages that aren't redesigned yet
        for href in ('href="/"', 'href="/admin/hub"', 'href="/instruments"'):
            assert href in home
        assert 'id="app-version"' in home

        # every page needs a session
        for path in ("/instruments", "/instruments/gc1", "/setup", "/instruments/classic"):
            assert _status(port, path, auth=False) in (302, 200)   # urllib follows to /login
            code, _ = get(port, path.replace("/instruments", "/api/instruments", 1)
                          if path == "/instruments" else "/api/instruments/gc1/setup", auth=False)
            assert code == 401


def test_setup_and_activity_endpoints(tmp_path):
    _prepare(tmp_path)
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)

        code, body = get(port, "/api/instruments/gc1/setup", timeout=30)
        assert code == 200 and body["instrument_id"] == "gc1"
        assert [s["key"] for s in body["steps"]] == [
            "create", "corrections", "installer", "checkin", "calibration", "method", "go_live",
            "first_result"]
        assert body["summary"]["total"] == 8
        assert get(port, "/api/instruments/nope/setup", timeout=30)[0] == 404

        code, body = get(port, "/api/instruments", timeout=30)
        gc1 = body["instruments"][0]
        assert gc1["setup"]["total"] == 8 and "step" in gc1["setup"]
        assert {"today", "held", "export_pending"} <= set(gc1)

        code, body = post(port, "/api/admin/instruments",
                          {"id": "gc2", "name": "GC-2", "password": pw})
        assert code == 201
        code, body = post(port, "/api/admin/instruments/gc2",
                          {"live_since": "2026-10-01T08:00", "password": pw})
        assert code == 200
        code, body = post(port, "/api/admin/instruments/gc2/revoke-token", {"password": pw})
        assert code == 200

        code, body = get(port, "/api/instruments/activity?limit=10", timeout=30)
        assert code == 200
        kinds = [(e["kind"], e["instrument_id"], e["by"]) for e in body["entries"]]
        assert ("created", "gc2", ACTOR) in kinds
        assert ("live_since", "gc2", ACTOR) in kinds
        assert ("token_revoked", "gc2", ACTOR) in kinds
        assert get(port, "/api/instruments/activity?limit=abc", timeout=30)[0] == 400
        assert get(port, "/api/instruments/activity", auth=False)[0] == 401

        code, body = get(port, "/api/instruments/gc2/setup", timeout=30)
        steps = {s["key"]: s for s in body["steps"]}
        assert steps["create"]["done_by"] == ACTOR
        assert steps["go_live"]["status"] == "done" and steps["go_live"]["done_by"] == ACTOR

        # a reserved id can't be created (it would shadow a page)
        code, body = post(port, "/api/admin/instruments",
                          {"id": "activity", "name": "X", "password": pw})
        assert code == 400
