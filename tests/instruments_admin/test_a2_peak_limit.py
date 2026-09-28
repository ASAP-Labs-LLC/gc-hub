"""2A2 review minor: GET /api/instruments/<id>/calibration runs peak detection
for any caller on the LAN, so it runs one at a time; a caller that can't get
the slot within PEAK_WAIT_SECONDS gets 429 instead of queueing CPU work.
Tested on a bare Flask app with the Blueprint (never ``import app``)."""
from __future__ import annotations

from a2_helpers import hub  # noqa: F401

import pytest

flask = pytest.importorskip("flask")

import instruments_api  # noqa: E402


@pytest.fixture()
def client(hub, monkeypatch):
    monkeypatch.setenv("GC_DATA_DIR", str(hub.data))
    import json
    (hub.data / "settings.json").write_text(json.dumps(hub.conf), encoding="utf-8")
    hub.gc1()
    app = flask.Flask(__name__, template_folder=str(instruments_api.Path(instruments_api.__file__).parent / "templates"))
    app.register_blueprint(instruments_api.bp)
    return app.test_client()


def test_calibration_view_answers(client):
    r = client.get("/api/instruments/gc1/calibration?sensitivity=70")
    assert r.status_code == 200 and r.get_json()["sensitivity"] == 70.0


def test_busy_peak_detection_is_429(client, monkeypatch):
    monkeypatch.setattr(instruments_api, "PEAK_WAIT_SECONDS", 0.05)
    assert instruments_api._PEAK_SLOTS.acquire(timeout=1)
    try:
        r = client.get("/api/instruments/gc1/calibration")
        assert r.status_code == 429 and "busy" in r.get_json()["error"]
    finally:
        instruments_api._PEAK_SLOTS.release()
    assert client.get("/api/instruments/gc1/calibration").status_code == 200
