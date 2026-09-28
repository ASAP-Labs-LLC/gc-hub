"""v2.0.0 RC: a backfill release writes export rows outside the Worker, so it
wakes the running exporter at once instead of leaving them for the next
60-second tick. Tested on a bare Flask app with the Blueprint (never
``import app``); the admin check is stubbed."""
from __future__ import annotations

from a2_helpers import hub  # noqa: F401

import json
from datetime import datetime

import pytest

flask = pytest.importorskip("flask")

import hub as hub_mod  # noqa: E402
import instruments_api  # noqa: E402
import store  # noqa: E402


class FakeRuntime:
    def __init__(self):
        self.wakes = 0

    def wake_exports(self):
        self.wakes += 1


@pytest.fixture()
def client(hub, monkeypatch):
    monkeypatch.setenv("GC_DATA_DIR", str(hub.data))
    (hub.data / "settings.json").write_text(json.dumps(hub.conf), encoding="utf-8")
    monkeypatch.setattr(instruments_api, "_db", lambda: hub.db)
    monkeypatch.setattr(instruments_api, "_admin", lambda: (flask.request.get_json(), None))
    rt = FakeRuntime()
    monkeypatch.setattr(hub_mod, "running", lambda: rt)
    app = flask.Flask(__name__)
    app.register_blueprint(instruments_api.bp)
    return app.test_client(), rt


def _backfill_sample(hub) -> int:
    hub.gc1(live_since=datetime(2030, 1, 1))
    sid = hub.submit(hub.cdf(name="A1", injected=datetime(2026, 9, 1, 8))).sample_id
    hub.worker().run_until_idle()
    s = store.samples.get(sid, db=hub.db)
    assert s["status"] == "final" and s["backfill"] == 1
    return sid


def test_a_backfill_release_wakes_the_exporter(hub, client):
    c, rt = client
    sid = _backfill_sample(hub)
    r = c.post("/api/admin/instruments/gc1/backfill/release", json={"sample_ids": [sid]})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["results"][0]["ok"] is True
    assert rt.wakes == 1


def test_a_release_that_released_nothing_does_not_wake(hub, client):
    c, rt = client
    _backfill_sample(hub)
    r = c.post("/api/admin/instruments/gc1/backfill/release", json={"sample_ids": [999999]})
    assert r.status_code == 200
    assert r.get_json()["results"][0]["ok"] is False
    assert rt.wakes == 0


def test_no_running_hub_is_fine(hub, client, monkeypatch):
    c, _rt = client
    monkeypatch.setattr(hub_mod, "running", lambda: None)
    sid = _backfill_sample(hub)
    r = c.post("/api/admin/instruments/gc1/backfill/release", json={"sample_ids": [sid]})
    assert r.status_code == 200 and r.get_json()["results"][0]["ok"] is True
