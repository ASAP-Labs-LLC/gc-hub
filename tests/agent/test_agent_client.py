import hashlib
import socket
import time

import pytest

from gc_agent.client import HubClient, NetworkError


def _client(hub, token=None):
    return HubClient(hub.url, token or hub.token, timeout=5)


def test_ingest_headers_exactly_per_contract(hub):
    body = b"\x89CDF data"
    sha = hashlib.sha256(body).hexdigest()
    mtime = time.mktime((2026, 9, 28, 14, 3, 7, 0, 0, -1))
    r = _client(hub).ingest("Sé #1 50%.CDF", body, sha, mtime)
    assert r.status == 201 and r.json["sha256"] == sha
    req = hub.by_path("/api/ingest")[0]
    h = {k.lower(): v for k, v in req.headers.items()}
    assert h["authorization"] == "Bearer " + hub.token
    assert h["x-gc-sha256"] == sha and sha == sha.lower()
    assert h["x-gc-mtime"] == "2026-09-28T14:03:07"
    assert h["x-gc-filename"] == "S%C3%A9%20%231%2050%25.CDF"
    assert h["content-type"] == "application/octet-stream"
    assert "origin" not in h
    assert req.body == body


def test_http_error_statuses_return_response_with_json(hub):
    hub.ingest_script.append((415, {"error": "not a CDF"}))
    r = _client(hub).ingest("a.CDF", b"x", hashlib.sha256(b"x").hexdigest(), time.time())
    assert r.status == 415 and r.json == {"error": "not a CDF"}
    assert r.error_text() == "not a CDF"


def test_bad_token_is_401(hub):
    r = _client(hub, token="wrong").heartbeat({"state": "idle"})
    assert r.status == 401


def test_non_json_body_gives_json_none(hub):
    hub.ingest_script.append((502, b"<html>bad gateway</html>"))
    r = _client(hub).ingest("a.CDF", b"x", hashlib.sha256(b"x").hexdigest(), time.time())
    assert r.status == 502 and r.json is None
    assert "502" in r.error_text()


def test_connection_refused_is_network_error():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    c = HubClient("http://127.0.0.1:%d" % port, "t", timeout=2)
    with pytest.raises(NetworkError):
        c.heartbeat({})


def test_other_routes(hub):
    hub.rows = [{"seq": i, "line": "r%d\r\n" % i} for i in range(1, 4)]
    hub.package_version = "v2.0.0"
    hub.package_zip = b"PK-zip"
    c = _client(hub)
    r = c.results(after=1, limit=1)
    assert r.status == 200 and r.json["rows"] == [{"seq": 2, "line": "r2\r\n"}] and r.json["more"]
    assert hub.by_path("/api/agent/results")[0].path == "/api/agent/results?after=1&limit=1"
    hb = c.heartbeat({"state": "idle"})
    assert hb.status == 200 and "command" in hb.json
    assert hub.heartbeats[-1] == {"state": "idle"}
    assert c.package_info().json == {"version": "v2.0.0",
                                     "sha256": hashlib.sha256(b"PK-zip").hexdigest()}
    z = c.package_zip()
    assert z.status == 200 and z.body == b"PK-zip"
