"""Security review 1 of 2B1, in the booted app (the critic's probe, as tests):
C1 (setup under a foreign Host), I1 (concurrent burst), I2 (never 5xx on bad
input), I3/I4 (small bodies, chunked too), I5 (installer hub URL), M1
(revoke, disabled instruments), M4 (no server paths in errors)."""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_helpers import admin_post, auth, get_json, heartbeat, ingest, prepared  # noqa: E402

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

import store  # noqa: E402
from bootapp import (TEST_ADMIN_PASSWORD as PW, booted, cookie_header, send,  # noqa: E402
                     setup_admin)


def raw(port, request_bytes: bytes, timeout=5.0):
    """Send raw HTTP; return (status line, seconds). A server that waits for a
    body it will never get shows up as '<timeout>'."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    t = time.time()
    try:
        s.sendall(request_bytes)
        try:
            data = s.recv(65536)
        except socket.timeout:
            data = b"<timeout>"
    finally:
        s.close()
    return data.split(b"\r\n")[0].decode("latin-1"), time.time() - t


def post_host(port, path, host, body: dict, extra=""):
    b = json.dumps(body).encode()
    req = (f"POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
           f"{extra}Connection: close\r\nContent-Length: {len(b)}\r\n\r\n").encode() + b
    s = socket.create_connection(("127.0.0.1", port), timeout=30)
    try:
        s.sendall(req)
        resp = b""
        while True:
            d = s.recv(65536)
            if not d:
                break
            resp += d
    finally:
        s.close()
    head, _, payload = resp.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    try:
        return status, json.loads(payload)
    except ValueError:
        return status, payload


def test_review_findings(tmp_path):
    hub = prepared(tmp_path)
    t1, t3 = hub.tokens["gc1"], hub.tokens["gc3"]
    with booted(tmp_path) as (port, _proc, data, _home):
        code_file = data / "admin-setup-code.txt"
        setup_code = code_file.read_text(encoding="utf-8").strip()

        # ── C1: DNS rebinding: a foreign Host is refused even with same-origin headers
        status, body = post_host(port, "/api/admin/setup", "evil.example",
                                 {"password": "attacker-pw", "setup_code": setup_code},
                                 "Origin: http://evil.example\r\nSec-Fetch-Site: same-origin\r\n")
        assert status == 403, body
        status, body = post_host(port, "/api/admin/setup", "evil.example:6666",
                                 {"password": "attacker-pw", "setup_code": "guess"})
        assert status == 403
        assert store.settings_kv.get("admin_password", db=hub.db) is None
        assert code_file.exists()

        # ── I3/I4: bodies are capped before they are read
        line, dt = raw(port, (b"POST /api/admin/setup HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                              b"Content-Type: application/json\r\nContent-Length: 150000000\r\n\r\n"))
        assert " 413 " in line + " ", line
        cookie = cookie_header(port)["Cookie"].encode()
        line, _ = raw(port, (b"POST /api/save-analysis-defaults HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                             b"Cookie: " + cookie + b"\r\n"
                             b"Content-Type: application/json\r\nContent-Length: 150000000\r\n\r\n"))
        assert " 413 " in line + " ", line
        # signed out, the gate answers first (401), still without reading the body
        line, _ = raw(port, (b"POST /api/save-analysis-defaults HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                             b"Content-Type: application/json\r\nContent-Length: 150000000\r\n\r\n"))
        assert " 401 " in line + " ", line
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            def gen():
                yield b'{"host": "'
                for _ in range(1024):
                    yield b"x" * 1024
                yield b'"}'
            c.request("POST", "/api/agent/heartbeat", body=gen(), encode_chunked=True,
                      headers={**auth(t1), "Content-Type": "application/json"})
            r = c.getresponse()
            assert r.status == 413, r.status
            assert "error" in json.loads(r.read())
        finally:
            c.close()

        setup_admin(port, data)

        # ── I2: never 5xx on bad input
        for q in (f"after={2 ** 70}", f"after=0&limit={2 ** 70}", "after=" + "9" * 5000):
            code, body = get_json(port, f"/api/agent/results?{q}", t1)
            assert code == 400, (q, code, body)
        for over in ({"queue_size": 2 ** 70}, {"results_seq": -2 ** 64}, {"rejected_count": -1},
                     {"results_seq": 2 ** 63}):
            code, body = heartbeat(port, t1, **over)
            assert code == 400, (over, code, body)
        code, body = send(port, "/api/agent/heartbeat", b"[" * 30000 + b"]" * 30000,
                          {**auth(t1), "Content-Type": "application/json"})
        assert code in (400, 413), code

        # ── M4: no server paths in a 400
        code, body = ingest(port, t1, b"\x89HDF\r\n\x1a\n" + os.urandom(5000))
        assert code == 400
        assert str(data) not in body["error"] and "/" not in body["error"], body
        assert ".incoming" not in body["error"]

        # ── I5 (rev 2): the installer's hub URL never comes from the request's Host:
        # it is the admin-set hub URL, else https://gc.asaplabs.net
        import io
        import zipfile
        ck = "Cookie: " + cookie_header(port)["Cookie"] + "\r\n"
        for host in ("evil.example:6666", f"localhost:{port}"):
            status, body = post_host(port, "/api/admin/instruments/gc2/installer", host,
                                     {"password": PW, "confirm_revoke": True}, ck)
            assert status == 200, body
            inst = json.loads(zipfile.ZipFile(io.BytesIO(body)).read("install.json"))
            assert inst["hub_url"] == "https://gc.asaplabs.net", inst
            assert "evil" not in json.dumps(inst) and "localhost" not in json.dumps(inst)

        # ── M1: revoke a token; disabled instruments' tokens get no results/package
        code, body = admin_post(port, "/api/admin/instruments/gc1/revoke-token", {"password": "nope"})
        assert code == 403
        code, body = admin_post(port, "/api/admin/instruments/gc1/revoke-token", {"password": PW})
        assert code == 200, body
        assert store.instruments.get("gc1", db=hub.db)["token_hash"] is None
        assert heartbeat(port, t1)[0] == 401
        assert admin_post(port, "/api/admin/instruments/gc9/revoke-token",
                          {"password": PW})[0] == 404
        assert get_json(port, "/api/agent/results?after=0", t3)[0] == 403
        assert get_json(port, "/api/agent/package", t3)[0] == 403
        assert get_json(port, "/api/agent/package.zip", t3)[0] == 403
        code, _ = admin_post(port, "/api/admin/instruments/gc3/agent-command",
                             {"password": PW, "command": "restart"})
        assert code == 200
        code, body = heartbeat(port, t3)                    # accepted, but no command
        assert code == 200 and body["command"] is None
        with store.connection(hub.db) as c:
            pending = c.execute("SELECT pending_command FROM agents WHERE instrument_id='gc3'"
                                ).fetchone()[0]
        assert pending == "restart"                          # kept for when it is enabled

        # ── I1: a concurrent burst of wrong passwords from one address
        results = []

        def guess():
            results.append(admin_post(port, "/api/admin/hub-url",
                                      {"password": "wrongwrong", "hub_url": ""}))
        threads = [threading.Thread(target=guess) for _ in range(30)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert all(code == 403 for code, _ in results)
        wrong = [b for _, b in results if b["error"] == "Incorrect password"]
        assert len(wrong) <= 5, len(wrong)
