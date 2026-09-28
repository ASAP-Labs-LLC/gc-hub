"""2A2 T9: the Instruments Blueprint in the booted app: every state change is
admin-gated, same-origin JSON with a 64 KiB body cap; the happy paths; the
token hash never leaves the hub."""
from __future__ import annotations

from a2_helpers import ELEVEN, Hub, cal_entries, set_corrections  # noqa: F401

import json
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("flask")

import ingest_api  # noqa: E402
import instruments  # noqa: E402
import store  # noqa: E402
from bootapp import booted, get, send, setup_admin  # noqa: E402

ADMIN_ROUTES = [
    "/api/admin/instruments",
    "/api/admin/instruments/gc1",
    "/api/admin/instruments/gc1/export-path",
    "/api/admin/instruments/gc1/export-adopt",
    "/api/admin/instruments/gc1/calibration-cdf",
    "/api/admin/instruments/gc1/calibration",
    "/api/admin/instruments/gc1/corrections",
    "/api/admin/instruments/gc1/corrections/seed",
    "/api/admin/instruments/gc1/methods",
    "/api/admin/instruments/gc1/review-method",
    "/api/admin/instruments/gc1/backfill/release",
    "/api/admin/conflicts/1/keep",
    "/api/admin/conflicts/1/replace",
    "/api/admin/standards/1/instrument",
]


def _prepare(tmp: Path):
    hub = Hub(tmp)
    hub.gc1()                                           # live since 2020
    hub.gc2(live_since=None, calibration_cdf=str(hub.cal),
            calibration_assignments=json.dumps(cal_entries(hub)))
    set_corrections(hub, "gc2")
    hub.a = hub.submit(hub.cdf(name="A1", injected=datetime(2026, 9, 10, 9))).sample_id
    hub.gas = hub.submit(hub.cdf(name="G1", method_name="D7096.M")).sample_id
    hub.b = hub.submit(hub.cdf(name="B2"), instrument="gc2").sample_id
    hub.worker(corrections_provider=instruments.corrections_provider(hub.db)).run_until_idle()
    assert store.samples.get(hub.b, db=hub.db)["status"] == "final"
    hub.conflict = hub.submit(hub.cdf(name="A1", injected=datetime(2026, 9, 10, 9), shift=0.3)).conflict_id
    std = tmp / "standards"
    std.mkdir()
    (std / "Diesel.CDF").write_bytes(hub.cal.read_bytes())
    settings = dict(hub.conf, comparison_defaults_dir=str(std))
    (hub.data / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    ingest_api.mint_token("gc1", db=hub.db)
    hub.token_hash = store.instruments.get("gc1", db=hub.db)["token_hash"]
    return hub


class Client:
    """Posts admin JSON and remembers every response body (for the token check)."""

    def __init__(self, port, password):
        self.port, self.password, self.seen = port, password, []

    def get(self, path):
        code, body = get(self.port, path, timeout=30)
        self.seen.append(json.dumps(body))
        return code, body

    def post(self, path, body=None, *, password=True, headers=None):
        payload = dict(body or {})
        if password:
            payload["password"] = self.password
        code, out = send(self.port, path, json.dumps(payload).encode(),
                         {"Content-Type": "application/json", **(headers or {})})
        self.seen.append(json.dumps(out))
        return code, out


def test_instruments_blueprint(tmp_path):
    hub = _prepare(tmp_path)
    with booted(tmp_path) as (port, _proc, data, _home):
        pw = setup_admin(port, data)
        c = Client(port, pw)

        # ── the page
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/instruments", timeout=10) as r:
            html = r.read().decode()
        assert r.status == 200 and "instruments.js" in html and "instruments_logic.js" in html

        # ── list and detail
        code, body = c.get("/api/instruments")
        assert code == 200 and [i["id"] for i in body["instruments"]] == ["gc1", "gc2"]
        gc1 = body["instruments"][0]
        assert gc1["has_token"] is True and "token_hash" not in gc1
        assert gc1["calibration"]["usable"] is True and gc1["counts"]["final"] == 1
        assert gc1["counts"]["other_method"] == 1 and gc1["open_conflicts"] == 1
        assert body["instruments"][1]["backfill_unreleased"] == 1
        assert body["hub_methods"] == ["D2887"] and "hub_url" in body
        code, body = c.get("/api/instruments/gc2")
        assert code == 200 and body["instrument"]["id"] == "gc2"
        assert body["corrections"]["source"] == "hub" and body["export"]["configured"] is False
        assert c.get("/api/instruments/nope")[0] == 404

        # ── create / update
        code, body = c.post("/api/admin/instruments", {"id": "gc3", "name": "GC-3"})
        assert code == 201 and body["instrument"]["live_since"] is None
        assert c.post("/api/admin/instruments", {"id": "gc3", "name": "x"})[0] == 409
        assert c.post("/api/admin/instruments", {"id": "GC 3", "name": "x"})[0] == 400
        code, body = c.post("/api/admin/instruments/gc3", {"live_since": "2026-10-01T08:00"})
        assert code == 200 and body["instrument"]["live_since"] == "2026-10-01 08:00:00"
        assert any("has not reported" in w for w in body["warnings"])
        assert c.post("/api/admin/instruments/gc3", {"live_since": "2026-10-01T08:00Z"})[0] == 400
        assert c.post("/api/admin/instruments/gc9", {"name": "x"})[0] == 404

        # ── export path
        target = tmp_path / "share" / "gc3.csv"
        code, body = c.post("/api/admin/instruments/gc3/export-path", {"path": str(target)})
        assert code == 200 and body["export"]["path"] == str(target)
        code, body = c.post("/api/admin/instruments/gc2/export-path", {"path": str(target)})
        assert code == 409 and body["reason"] == "in-use"
        code, body = c.post("/api/admin/instruments/gc3/export-adopt", {})
        assert code == 409 and body["reason"] == "missing"

        # ── calibration
        code, body = c.get("/api/instruments/gc1/calibration")
        assert code == 200 and body["peaks"] and body["usable"] is True
        assert c.get("/api/instruments/gc3/calibration")[0] == 409
        code, body = c.get("/api/instruments/gc2/calibration-candidates")
        assert code == 200 and [x["sample_id"] for x in body["candidates"]] == [hub.b]
        code, body = c.post("/api/admin/instruments/gc3/calibration-cdf", {"path": str(hub.cal)})
        assert code == 200 and body["calibration"]["usable"] is False
        code, body = c.post("/api/admin/instruments/gc3/calibration",
                            {"assignments": cal_entries(hub), "sensitivity": 55})
        assert code == 200 and body["usable"] is True and body["anchors"] >= 2
        code, body = c.post("/api/admin/instruments/gc3/calibration",
                            {"assignments": [{"rt": 2, "carbon": 14}, {"rt": 3, "carbon": 12}]})
        assert code == 400

        # ── corrections
        code, body = c.post("/api/admin/instruments/gc3/corrections",
                            {"values": dict(ELEVEN, IBP=99), "reason": "x"})
        assert code == 400 and body["errors"]
        assert c.post("/api/admin/instruments/gc3/corrections", {"values": ELEVEN})[0] == 400
        code, body = c.post("/api/admin/instruments/gc3/corrections",
                            {"values": ELEVEN, "reason": "first entry"})
        assert code == 200 and body["changed"] == 11
        code, body = c.get("/api/instruments/gc3/corrections")
        assert body["values"] == ELEVEN and body["audit"][0]["changed_by"].startswith("admin@")
        # T5: hub.start seeds gc1 from the phase-1 file at the first start, so
        # the one-time admin seed finds it done.
        rec = store.corrections.read("gc1", db=hub.db)
        assert rec is not None and rec["updated_by"] == "startup" and len(rec["values"]) == 11
        assert c.post("/api/admin/instruments/gc1/corrections/seed", {})[0] == 409
        assert c.post("/api/admin/instruments/gc3/corrections/seed", {})[0] == 400

        # ── methods
        code, body = c.get("/api/instruments/gc1/methods")
        seen = {r["method_name"]: r for r in body["seen"]}
        assert seen["D7096.M"]["mapped_to"] is None
        code, body = c.post("/api/admin/instruments/gc1/methods",
                            {"method_name": "d7096.m", "hub_method": "D2887"})
        assert code == 200 and body["queued"] == 1
        code, body = c.post("/api/admin/instruments/gc1/methods",
                            {"method_name": "D7096.M", "hub_method": None})
        assert code == 200 and "D7096.M" not in body["method_map"]
        code, body = c.post("/api/admin/instruments/gc1/review-method", {})
        assert code == 200 and body["marked"] == 0

        # ── backfill
        code, body = c.get("/api/instruments/gc2/backfill?released=false")
        assert code == 200 and [s["sample_id"] for s in body["samples"]] == [hub.b]
        assert c.get("/api/instruments/gc2/backfill?limit=x")[0] == 400
        code, body = c.post("/api/admin/instruments/gc2/backfill/release",
                            {"sample_ids": [hub.b, hub.a]})
        assert code == 200
        res = {r["sample_id"]: r for r in body["results"]}
        assert res[hub.b]["ok"] is True and res[hub.a]["ok"] is False

        # ── conflicts
        code, body = c.get("/api/conflicts")
        assert code == 200 and [x["id"] for x in body["conflicts"]] == [hub.conflict]
        assert body["conflicts"][0]["existing"]["sample_id"] == hub.a
        code, body = c.post(f"/api/admin/conflicts/{hub.conflict}/replace", {})
        assert code == 200 and body["job_id"]
        code, body = c.post(f"/api/admin/conflicts/{hub.conflict}/keep", {})
        assert code == 200
        assert c.post(f"/api/admin/conflicts/{hub.conflict}/keep", {})[0] == 409
        assert c.post("/api/admin/conflicts/999/keep", {})[0] == 404
        assert c.get("/api/conflicts")[1]["conflicts"] == []

        # ── standards (D12)
        code, body = c.get("/api/standards?for_instrument=gc2")
        assert code == 200 and [s["name"] for s in body["standards"]] == ["Diesel"]
        std = body["standards"][0]
        assert std["instrument_id"] == "gc1" and std["cross_instrument"] is True and std["warning"]
        code, body = c.post(f"/api/admin/standards/{std['id']}/instrument", {"instrument_id": "gc2"})
        assert code == 200 and body["standard"]["instrument_id"] == "gc2"
        code, body = c.get("/api/standards?instrument=gc2")
        assert [s["name"] for s in body["standards"]] == ["Diesel"]
        assert c.post(f"/api/admin/standards/{std['id']}/instrument", {"instrument_id": "gc9"})[0] == 404

        # ── the gate on every state change (last: the failures throttle this client)
        for path in ADMIN_ROUTES:
            code, body = c.post(path, {}, password=False)
            assert code == 403, (path, code, body)
            code, _ = send(port, path, json.dumps({"password": pw}).encode(),
                           {"Content-Type": "text/plain"})
            assert code == 415, path
            code, _ = send(port, path, json.dumps({"password": pw, "x": "a" * 70000}).encode(),
                           {"Content-Type": "application/json"})
            assert code == 413, path
            code, _ = c.post(path, {}, headers={"Origin": "http://evil.example"})
            assert code == 403, path

        # ── the token hash never leaves the hub
        assert all(hub.token_hash not in text for text in c.seen)
