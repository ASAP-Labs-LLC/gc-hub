"""2B1 T5: heartbeat, results pull and package routes (contract §1), booted."""
from __future__ import annotations

import hashlib
import io
import re
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_helpers import auth, get_json, heartbeat, prepared  # noqa: E402

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

import distill  # noqa: E402
import store  # noqa: E402
from bootapp import booted, get, send  # noqa: E402

SERVER_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def _final_samples(hub, n):
    ids = []
    for i in range(n):
        sub = hub.submit(hub.cdf(name=f"5000{i}", injected=datetime(2026, 9, 25, 14, 23, i)))
        assert sub.outcome == "created"
        ids.append(sub.sample_id)
    w = hub.worker()
    w.run_until_idle()
    for sid in ids:
        assert hub.sample(sid)["status"] == "final", hub.sample(sid)
    return ids


def test_heartbeat_results_package(tmp_path):
    hub = prepared(tmp_path)
    _final_samples(hub, 3)
    ledger = store.export_rows.rows_after("gc1", 0, db=hub.db)
    assert len(ledger) == 3
    t1, t2 = hub.tokens["gc1"], hub.tokens["gc2"]

    with booted(tmp_path) as (port, _proc, _data, _home):
        # ── package ───────────────────────────────────────────────────────
        code, info = get_json(port, "/api/agent/package", t1)
        assert code == 200, info
        assert info["version"] == "dev"               # the hub's VERSION ("dev" in a checkout)
        assert re.fullmatch(r"[0-9a-f]{64}", info["sha256"])
        assert get_json(port, "/api/agent/package", "bad")[0] == 401
        zbytes = _get_bytes(port, "/api/agent/package.zip", t1)
        assert hashlib.sha256(zbytes).hexdigest() == info["sha256"]
        assert _get_bytes(port, "/api/agent/package.zip", t1) == zbytes          # cached / deterministic
        z = zipfile.ZipFile(io.BytesIO(zbytes))
        names = set(z.namelist())
        assert {"agent_main.py", "gc_agent/__init__.py", "gc_agent/client.py", "VERSION",
                "requirements-agent.txt"} <= names
        assert z.read("VERSION") == b"dev\n"
        assert not any(n.endswith(("install.json", "agent.json", ".pyw")) for n in names)
        assert t1.encode() not in zbytes
        assert get_json(port, "/api/agent/package.zip", "bad")[0] == 401

        # ── heartbeat ─────────────────────────────────────────────────────
        code, body = heartbeat(port, t1)
        assert code == 200, body
        assert set(body) == {"command", "agent_package_sha256", "server_time"}
        assert body["command"] is None
        assert body["agent_package_sha256"] == info["sha256"]
        assert SERVER_TIME.match(body["server_time"])
        hub_local = datetime.strptime(body["server_time"], "%Y-%m-%dT%H:%M:%S")
        assert abs((hub_local - datetime.now()).total_seconds()) < 5          # LOCAL, not UTC
        with store.connection(hub.db) as c:
            ag = dict(c.execute("SELECT * FROM agents WHERE instrument_id='gc1'").fetchone())
        assert ag["version"] == "v9.9.9" and ag["package_sha256"] == "ab" * 32
        assert (ag["host"], ag["queue_size"], ag["rejected_count"], ag["state"]) == \
            ("GC1-PC", 2, 1, "idle")
        assert ag["last_seen"]

        # a pending command is delivered once
        with store.connection(hub.db) as c:
            c.execute("UPDATE agents SET pending_command='retry-rejected' WHERE instrument_id='gc1'")
        assert heartbeat(port, t1)[1]["command"] == "retry-rejected"
        assert heartbeat(port, t1)[1]["command"] is None

        # clock skew against hub LOCAL time
        ahead = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        assert heartbeat(port, t1, agent_time=ahead)[0] == 200
        code, agents = get(port, "/api/agents")
        assert code == 200
        g1 = next(a for a in agents["agents"] if a["instrument_id"] == "gc1")
        assert 3590 <= g1["clock_skew_seconds"] <= 3610, g1
        assert "pending_command" in g1 and "token_hash" not in g1
        assert t1 not in str(agents) and store.instruments.get("gc1", db=hub.db)["token_hash"] \
            not in str(agents)

        # bad input: 400, never 5xx; no token: 401
        assert heartbeat(port, t1, queue_size="lots")[0] == 400
        assert heartbeat(port, t1, agent_time="noon")[0] == 400
        code, _ = send(port, "/api/agent/heartbeat", b"{not json",
                       {**auth(t1), "Content-Type": "application/json"})
        assert code == 400
        assert heartbeat(port, "bad")[0] == 401

        # ── results ───────────────────────────────────────────────────────
        code, res = get_json(port, "/api/agent/results?after=0&limit=2", t1)
        assert code == 200, res
        assert res["header"] == list(distill.CSV_HEADER) and len(res["header"]) == 31
        assert [r["seq"] for r in res["rows"]] == [ledger[0]["seq"], ledger[1]["seq"]]
        assert [r["line"] for r in res["rows"]] == [ledger[0]["line"], ledger[1]["line"]]
        assert all(r["line"].endswith("\r\n") for r in res["rows"])
        assert res["more"] is True
        code, res = get_json(port, f"/api/agent/results?after={ledger[1]['seq']}&limit=2", t1)
        assert [r["seq"] for r in res["rows"]] == [ledger[2]["seq"]] and res["more"] is False
        code, res = get_json(port, "/api/agent/results?after=0&limit=500", t1)
        assert len(res["rows"]) == 3 and res["more"] is False
        code, res = get_json(port, "/api/agent/results?after=0", t1)        # defaults
        assert len(res["rows"]) == 3
        code, res = get_json(port, "/api/agent/results?after=0&limit=5000", t1)   # clamped
        assert code == 200 and len(res["rows"]) == 3
        # the token's own instrument only
        code, res = get_json(port, "/api/agent/results?after=0&limit=500", t2)
        assert (code, res["rows"], res["more"]) == (200, [], False)
        for q in ("after=-1", "after=abc", "after=0&limit=0", "after=0&limit=x"):
            code, res = get_json(port, f"/api/agent/results?{q}", t1)
            assert code == 400 and "error" in res, q
        assert get_json(port, "/api/agent/results?after=0", "bad")[0] == 401


def _get_bytes(port, path, token):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=auth(token))
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 200
        assert r.headers.get("Content-Type", "").startswith("application/zip")
        return r.read()
