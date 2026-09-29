"""D10: GET /api/lem/machines, the Instruments page's LEM dropdown source.
Tested on a bare Flask app with the Blueprint (never ``import app``) against
a local LEM stub (tests/test_lem_machines.py), named by ``LEM_URL`` (the
lem_url setting refuses loopback hosts; conftest points LEM_URL at a closed
port for the session, so nothing here reaches the real LEM)."""
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
def lem(monkeypatch):
    s = Stub()
    monkeypatch.setenv("LEM_URL", s.url)
    yield s
    s.close()


@pytest.fixture()
def client(hub, monkeypatch):
    import settings
    monkeypatch.setenv("GC_DATA_DIR", str(hub.data))
    monkeypatch.setattr(settings, "CONFIG_PATH", hub.data / "settings.json")
    (hub.data / "settings.json").write_text(json.dumps(hub.conf), encoding="utf-8")
    monkeypatch.setattr(lem_machines, "CACHE", lem_machines.MachineCache())
    app = flask.Flask(__name__, template_folder=str(Path(instruments_api.__file__).parent / "templates"))
    app.register_blueprint(instruments_api.bp)
    return app.test_client()


def test_live(client, lem):
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


def test_a_loopback_setting_is_not_used(client, hub, lem, monkeypatch):
    (hub.data / "settings.json").write_text(
        json.dumps(dict(hub.conf, lem_url="http://127.0.0.1:1")), encoding="utf-8")
    assert client.get("/api/lem/machines").get_json()["source"] == "live"   # LEM_URL won
    assert lem_machines.resolve_url({"lem_url": "http://127.0.0.1:1"}, env={}) == \
        lem_machines.DEFAULT_URL


def test_unavailable_error_is_generic(client, lem):
    lem.mode = "500"
    body = client.get("/api/lem/machines").get_json()
    assert body["source"] == "unavailable" and body["machines"] == []
    assert body["age_seconds"] is None
    err = body["error"]
    assert err and "127.0.0.1" not in err and "http" not in err and "secret" not in err


def test_offline_by_default(client):
    body = client.get("/api/lem/machines").get_json()      # conftest's closed port
    assert body["source"] == "unavailable"


def test_cached_after_failure(client, lem, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(lem_machines, "CACHE", lem_machines.MachineCache(clock=lambda: clock[0]))
    assert client.get("/api/lem/machines").get_json()["source"] == "live"
    lem.mode = "500"
    clock[0] += 90
    body = client.get("/api/lem/machines").get_json()
    assert body["source"] == "cached" and len(body["machines"]) == 3
    assert body["age_seconds"] == 90 and body["error"]


def test_get_only(client):
    assert client.post("/api/lem/machines", json={}).status_code == 405
