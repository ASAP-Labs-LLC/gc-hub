"""Shared helpers for the 2B1 ingest tests (not a conftest: see
tests/pipeline/pipeline_helpers.py for why).

``prepared(tmp)`` builds the hub data folder *before* the app is booted
(``tests/bootapp.py`` uses ``tmp/data``): a migrated store with ``gc1``
(calibrated from the synthetic ladder, live since 2020), ``gc2`` (enabled,
no calibration) and ``gc3`` (disabled), and a token minted for each.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent
for _p in (TESTS, TESTS / "pipeline"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from bootapp import send  # noqa: E402
from pipeline_helpers import Hub  # noqa: E402

import ingest_api  # noqa: E402
import instruments  # noqa: E402
import store  # noqa: E402


def prepared(tmp: Path):
    hub = Hub(Path(tmp))                         # tmp/data, tmp/src
    hub.gc1(live_since=datetime(2020, 1, 1))
    hub.gc2()
    store.instruments.upsert({"id": "gc3", "name": "GC-3", "enabled": 0,
                              "method_map": json.dumps(instruments.DEFAULT_METHOD_MAP),
                              "live_since": datetime(2020, 1, 1)}, db=hub.db)
    hub.tokens = {i: ingest_api.mint_token(i, db=hub.db) for i in ("gc1", "gc2", "gc3")}
    return hub


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def ingest(port, token, body: bytes, *, filename="Sample 1.CDF", mtime="2026-09-25T14:30:00",
           sha=None, extra=None):
    headers = {"Content-Type": "application/octet-stream",
               "X-GC-SHA256": sha if sha is not None else hashlib.sha256(body).hexdigest(),
               "X-GC-Mtime": mtime,
               "X-GC-Filename": urllib.parse.quote(filename)}
    if token is not None:
        headers.update(auth(token))
    headers.update(extra or {})
    return send(port, "/api/ingest", body, headers)


def get_json(port, path, token):
    return send(port, path, None, auth(token), method="GET")


def heartbeat(port, token, **over):
    payload = {"version": "v9.9.9", "package_sha256": "ab" * 32, "state": "idle",
               "queue_size": 2, "rejected_count": 1, "last_file": "x.CDF", "last_error": None,
               "host": "GC1-PC", "agent_time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
               "results_seq": 0}
    payload.update(over)
    return send(port, "/api/agent/heartbeat", json.dumps(payload).encode(),
                {**auth(token), "Content-Type": "application/json"})


def admin_post(port, path, body, headers=None):
    return send(port, path, json.dumps(body).encode(),
                {"Content-Type": "application/json", **(headers or {})})
