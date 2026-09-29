"""D10: GET /api/lem/machines, the Instruments page's LEM dropdown source.
Tested on a bare Flask app with the Blueprint (never ``import app``) against
a local LEM stub (tests/test_lem_machines.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from a2_helpers import hub  # noqa: F401

import pytest

flask = pytest.importorskip("flask")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instruments_api  # noqa: E402
import lem_machines  # noqa: E402
from test_lem_machines import Stub  # noqa: E402


@pytest.fixture()
def lem():
    s = Stub()
    yield s
    s.close()


@pytest.fixture()
def client(hub, monkeypatch):
    import settings
    monkeypatch.setenv("GC_DATA_DIR", str(hub.data))
    # settings resolved CONFIG_PATH at import (None without GC_DATA_DIR);
    # point it at this hub's file so the route never falls back to the real LEM
    monkeypatch.setattr(settings, "CONFIG_PATH", hub.data / "settings.json")
    _use_setting(hub, "http://127.0.0.1:9")
    monkeypatch.setattr(lem_machines, "CACHE", lem_machines.MachineCache())
    app = flask.Flask(__name__, template_folder=str(Path(instruments_api.__file__).parent / "templates"))
    app.register_blueprint(instruments_api.bp)
    return app.test_client()


def _use_setting(hub, url):
    (hub.data / "settings.json").write_text(json.dumps(dict(hub.conf, lem_url=url)),
                                            encoding="utf-8")


def test_live(client, hub, lem):
    _use_setting(hub, lem.url)
    r = client.get("/api/lem/machines")
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) == {"machines", "source", "age_seconds", "stale", "labcore_online"}
    assert (body["stale"], body["labcore_online"]) == (False, True)
    assert body["source"] == "live" and body["age_seconds"] == 0
    assert [m["title"] for m in body["machines"]] == ["Agilent GC 1", "Agilent GC 2", "aquamax 1"]
    assert body["machines"][0] == {"uid": "bf8e64b59f12", "title": "Agilent GC 1",
                                   "status": "RED", "closed": False}
    assert r.headers.get("Cache-Control") == "no-store"
    assert lem.paths == ["/api/machines"]


def test_env_wins_over_the_setting(client, hub, lem, monkeypatch):
    _use_setting(hub, "http://127.0.0.1:9")
    monkeypatch.setenv("LEM_URL", lem.url)
    assert client.get("/api/lem/machines").get_json()["source"] == "live"


def test_unavailable_error_is_generic(client, hub, lem):
    lem.mode = "500"
    _use_setting(hub, lem.url)
    body = client.get("/api/lem/machines").get_json()
    assert body["source"] == "unavailable" and body["machines"] == []
    assert body["age_seconds"] is None
    err = body["error"]
    assert err and "127.0.0.1" not in err and "http" not in err and "secret" not in err


def test_cached_after_failure(client, hub, lem, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(lem_machines, "CACHE", lem_machines.MachineCache(clock=lambda: clock[0]))
    _use_setting(hub, lem.url)
    assert client.get("/api/lem/machines").get_json()["source"] == "live"
    lem.mode = "500"
    clock[0] += 90
    body = client.get("/api/lem/machines").get_json()
    assert body["source"] == "cached" and len(body["machines"]) == 3
    assert body["age_seconds"] == 90 and body["error"]


def test_get_only(client):
    assert client.post("/api/lem/machines", json={}).status_code == 405
