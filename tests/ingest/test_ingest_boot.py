"""2B1 T4: ``POST /api/ingest`` in the real app (booted in a subprocess).

One boot covers every outcome of contract §1's table, in order, against a
store prepared before the boot (``ingest_helpers.prepared``).
"""
from __future__ import annotations

import hashlib
import http.client
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_helpers import get_json, ingest, prepared  # noqa: E402

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

import cdf_fixtures as fx  # noqa: E402
import store  # noqa: E402
from bootapp import booted, get  # noqa: E402


def _raw_post(port, path, headers, body=b""):
    """A POST that announces ``Content-Length`` itself and may send less (the
    server must answer without reading the body)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.putrequest("POST", path)
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders()
        if body:
            c.send(body)
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"null")
    finally:
        c.close()


def test_ingest_outcomes(tmp_path):
    hub = prepared(tmp_path)
    t1, t2, t3 = (hub.tokens[i] for i in ("gc1", "gc2", "gc3"))
    sample = hub.cdf(name="40304").read_bytes()
    sha = hashlib.sha256(sample).hexdigest()
    other = hub.cdf(name="40304", shift=0.05).read_bytes()        # same key, other bytes
    stampless = fx.write_cdf(hub.src / "nostamp.CDF", *_axis_signal(), "40999",
                             datetime(2026, 9, 25, 14, 23), method_name="SIMDISB.M",
                             raw_stamp="").read_bytes()
    truncated = fx.truncated_copy(hub.cdf(name="41000"), hub.src / "trunc.CDF").read_bytes()

    with booted(tmp_path) as (port, _proc, data, _home):
        assert data == hub.data
        time.sleep(1.2)                                   # let boot-time requests age out

        # 401: no token, a bad token (checked before anything else)
        assert ingest(port, None, sample)[0] == 401
        code, body = ingest(port, "not-a-token", sample)
        assert code == 401 and "error" in body
        assert ingest(port, "not-a-token", b"x", sha="nope")[0] == 401

        # 201 created; the percent-encoded name arrives decoded as source_name
        code, body = ingest(port, t1, sample, filename="Sample 1 (a)%.CDF")
        assert code == 201, body
        assert body["sha256"] == sha and body["status"] == "received"
        sid = body["sample_id"]
        row = store.samples.get(sid, db=hub.db)
        assert row["instrument_id"] == "gc1"
        assert row["source_name"] == "Sample 1 (a)%.CDF"

        # 200 duplicate
        code, body = ingest(port, t1, sample)
        assert (code, body) == (200, {"sample_id": sid, "sha256": sha, "duplicate": True})

        # 409 cross-instrument
        code, body = ingest(port, t2, sample)
        assert code == 409 and "GC-1" in body["error"]

        # 202 conflict: sha is the received body's
        code, body = ingest(port, t1, other)
        assert code == 202, body
        assert body["sha256"] == hashlib.sha256(other).hexdigest()
        assert isinstance(body["conflict_id"], int)

        # 403 disabled instrument (the agent holds)
        code, body = ingest(port, t3, hub.cdf(name="42000").read_bytes())
        assert code == 403 and "disabled" in body["error"]

        # 400: sha mismatch, malformed sha, malformed mtime
        assert ingest(port, t1, sample, sha="0" * 64)[0] == 400
        assert ingest(port, t1, sample, sha="xyz")[0] == 400
        assert ingest(port, t1, sample, mtime="yesterday")[0] == 400
        assert ingest(port, t1, sample, mtime="2026-09-25 14:30:00+02:00")[0] == 400

        # 415: not a CDF at all; 400: a CDF that is truncated
        code, body = ingest(port, t1, b"hello, not a chromatogram")
        assert code == 415, body
        assert ingest(port, t1, b"")[0] in (400, 415)
        code, body = ingest(port, t1, truncated)
        assert code == 400 and "truncated" in body["error"]

        # A CDF without a stamp takes X-GC-Mtime (whole seconds)
        code, body = ingest(port, t1, stampless, mtime="2026-09-20T08:15:42")
        assert code == 201, body
        row = store.samples.get(body["sample_id"], db=hub.db)
        assert (row["injection_dt"], row["injection_dt_source"]) == ("2026-09-20 08:15:42", "mtime")

        # 413 before the body is read: a 30 MB Content-Length, 10 bytes sent
        big = {"Authorization": f"Bearer {t1}", "Content-Type": "application/octet-stream",
               "Content-Length": str(30 * 1024 * 1024), "X-GC-SHA256": "0" * 64,
               "X-GC-Mtime": "2026-09-25T14:30:00", "X-GC-Filename": "big.CDF"}
        code, body = _raw_post(port, "/api/ingest", big, b"CDF\x01xxxxxx")
        assert code == 413 and "error" in body
        # ...but a bad token still answers 401 first
        code, _ = _raw_post(port, "/api/ingest", {**big, "Authorization": "Bearer nope"})
        assert code == 401

        # The phase 1 cross-site guard still refuses a browser page; agents send no Origin
        code, body = ingest(port, t1, hub.cdf(name="43000").read_bytes(),
                            extra={"Origin": "http://evil.example"})
        assert (code, body) == (403, {"error": "Cross-site request refused"})

        # Every non-2xx body is JSON with an error; GET is not allowed
        code, body = get_json(port, "/api/ingest", t1)
        assert code == 405

        # Agent traffic is not activity
        code, hz = get(port, "/healthz")
        assert code == 200 and hz["active_sessions"] == 0, hz
        assert hz["idle_seconds"] >= 1.0, hz


def _axis_signal():
    t = fx._axis()
    y = fx.gaussian(t, 0.30, 50000, 0.01) + fx.gaussian(t, 3.2, 1200, 0.9) + 40
    return t, y
