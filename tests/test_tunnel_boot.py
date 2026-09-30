"""gc.asaplabs.net end to end, on the real app booted in a subprocess, with
every request shaped as the Cloudflare tunnel delivers it (cloudflared on the
same machine: a loopback peer, ``Host: gc.asaplabs.net``, ``CF-Connecting-IP``,
``CF-Ray``, ``X-Forwarded-Proto: https``) and LabCore replaced by the local
stub (``tests/labcore_stub.py``).

The walk is Ryan's first minutes after installing: sign in with LabLink,
set the admin password at /admin/setup (a session and the setup code),
create GC-2, download its agent installer (it points at
https://gc.asaplabs.net) and dry-run the v1 history import. Along the way:
the things the tunnel must never allow.
"""
from __future__ import annotations

import http.client
import io
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")

import store  # noqa: E402
from bootapp import booted, wait_for  # noqa: E402
from labcore_stub import LabCoreStub  # noqa: E402

PW = "tunnel-admin-2026"
ORIGIN = "https://gc.asaplabs.net"


def tunnel_headers(ip="203.0.113.9", proto="https"):
    return {"Host": "gc.asaplabs.net", "CF-Connecting-IP": ip, "CF-Ray": "8c1d2e3f4a5b-DFW",
            "X-Forwarded-Proto": proto, "X-Forwarded-For": ip, "CDN-Loop": "cloudflare"}


def call(port, method, path, body=None, *, cookie=None, headers=None, tunnel=True, raw=False):
    """One request, never following redirects: ``(status, headers, json|bytes)``."""
    h = dict(tunnel_headers() if tunnel else {"Host": f"127.0.0.1:{port}"})
    if body is not None:
        h["Content-Type"] = "application/json"
        h["Origin"] = ORIGIN if tunnel else f"http://127.0.0.1:{port}"
        h["Sec-Fetch-Site"] = "same-origin"
    if cookie:
        h["Cookie"] = cookie
    h.update(headers or {})
    h = {k: v for k, v in h.items() if v is not None}     # headers={name: None} drops one
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers=h)
        r = c.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        set_cookies = [v for k, v in r.getheaders() if k.lower() == "set-cookie"]
        hdrs["set-cookie-all"] = set_cookies
    finally:
        c.close()
    if raw:
        return r.status, hdrs, data
    try:
        return r.status, hdrs, json.loads(data or b"null")
    except ValueError:
        return r.status, hdrs, data


def _cookie(hdrs, name="__Host-gc_session"):
    for v in hdrs["set-cookie-all"]:
        if v.startswith(name + "="):
            return v.split(";")[0]
    return None


@pytest.fixture(scope="module")
def hub():
    tmp = Path(tempfile.mkdtemp(prefix="gc-tunnel-"))
    stub = LabCoreStub()
    try:
        with booted(tmp, sign_in_as=None, extra_env={"LABCORE_URL": stub.url}) as (
                port, _proc, data, _home):
            db = data / store.DB_FILENAME
            assert wait_for(lambda: db.is_file()
                            and store.instruments.get("gc1", db=db) is not None, timeout=30)
            yield port, data, db, stub, tmp
    finally:
        stub.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_first_minutes_through_gc_asaplabs_net(hub):
    port, data, db, stub, tmp = hub

    # nothing without a session, setup included (through the tunnel)
    code, h, _ = call(port, "GET", "/")
    assert code == 302 and h["location"] == "/login?next=/"
    code, h, _ = call(port, "GET", "/admin/setup")
    assert code == 302 and h["location"] == "/login?next=/admin/setup"
    code, _, body = call(port, "GET", "/api/files")
    assert code == 401 and body["login_required"] is True
    code, _, _ = call(port, "GET", "/login")
    assert code == 200

    # the admin password is not a way in through the tunnel
    code, _, body = call(port, "POST", "/api/login/admin", {"password": PW})
    assert code == 403

    # sign in with LabLink (the stub)
    code, h, body = call(port, "POST", "/api/login",
                         {"username": "ryan c", "password": "labpass-1", "next": "/admin/setup"})
    assert code == 200, body
    assert body["name"] == "Ryan C" and body["next"] == "/admin/setup"
    cookie = _cookie(h)
    assert cookie, h["set-cookie-all"]
    set_cookie = next(v for v in h["set-cookie-all"] if v.startswith("__Host-gc_session="))
    assert "Secure" in set_cookie and "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie
    assert h["strict-transport-security"] == "max-age=31536000"
    assert stub.requests[-1][0]["User-Agent"].startswith("gc-hub/")
    row = store.web_sessions.list_active(db=db)[0]
    assert row["name"] == "Ryan C" and row["ip"] == "203.0.113.9"

    # admin setup: the page, then the setup code (still needed with a session)
    code, _, page = call(port, "GET", "/admin/setup", cookie=cookie, raw=True)
    assert code == 200 and b"Setup code" in page and b"Ryan C" in page
    setup_code = (data / "admin-setup-code.txt").read_text(encoding="utf-8").strip()
    code, _, body = call(port, "POST", "/api/admin/setup",
                         {"password": PW, "setup_code": "not-it"}, cookie=cookie)
    assert code == 403
    code, _, body = call(port, "POST", "/api/admin/setup",
                         {"password": PW, "setup_code": setup_code}, cookie=cookie)
    assert code == 201, body
    assert store.settings_kv.get("admin_password", db=db).startswith("pbkdf2_sha256$")
    code, _, _ = call(port, "POST", "/api/admin/setup",
                      {"password": "someone-else-1", "setup_code": setup_code}, cookie=cookie)
    assert code == 409                                           # closed for good

    # create GC-2
    code, _, body = call(port, "POST", "/api/admin/instruments",
                         {"password": PW, "id": "gc2", "name": "GC-2"}, cookie=cookie)
    assert code == 201, body
    assert store.instruments.get("gc2", db=db)["name"] == "GC-2"

    # its agent installer points at https://gc.asaplabs.net, LAN address as the fallback
    code, h, blob = call(port, "POST", "/api/admin/instruments/gc2/installer",
                         {"password": PW}, cookie=cookie, raw=True)
    assert code == 200, blob[:300]
    inst = json.loads(zipfile.ZipFile(io.BytesIO(blob)).read("install.json"))
    assert inst["hub_url"] == "https://gc.asaplabs.net"
    assert inst["lan_url"] and inst["lan_url"].startswith("http://")
    assert "gc.asaplabs.net" not in inst["lan_url"]
    assert h["x-gc-hub-url"] == "https://gc.asaplabs.net"

    # the v1 history import, dry run
    processed = tmp / "v1-processed"
    processed.mkdir()
    code, _, body = call(port, "POST", "/api/admin/import-history/dry-run",
                         {"password": PW, "instrument": "gc2", "processed_dir": str(processed)},
                         cookie=cookie)
    # an admin job since v3.0.1 (Cloudflare ends a request after 100 s): 202 at
    # once, the summary on the finished job, polled through the tunnel too
    assert code == 202, body
    assert body["job"]["kind"] == "import-history-dry-run"
    import time
    deadline = time.time() + 30
    while True:
        code, _, status = call(port, "POST", "/api/admin/jobs/status", {"password": PW},
                               cookie=cookie)
        assert code == 200, status
        if status["job"]["state"] != "running" or time.time() > deadline:
            break
        time.sleep(0.05)
    assert status["job"]["state"] == "done", status
    assert status["job"]["result"]["summary"]["dry_run"] is True

    # every action above is logged with the signed-in name and the real address
    log = (data / "app.log").read_text(encoding="utf-8")
    assert "Ryan C (203.0.113.9)" in log
    assert "labpass-1" not in log and setup_code not in log.split("admin password set")[-1]


def test_what_the_tunnel_never_allows(hub):
    port, data, db, _stub, _tmp = hub
    # plain http through Cloudflare: 308 to https, for any method
    for method in ("GET", "POST"):
        code, h, _ = call(port, method, "/api/files", {} if method == "POST" else None,
                          headers={"X-Forwarded-Proto": "http"})
        assert code == 308 and h["location"] == "https://gc.asaplabs.net/api/files"
    # the health check gives the internet nothing but the contract minimum
    code, _, body = call(port, "GET", "/healthz")
    assert code == 200 and set(body) == {"status", "version", "pid"}
    code, _, body = call(port, "GET", "/healthz", tunnel=False)
    assert "hub" in body and "active_sessions" in body
    # the tray's controls are never reachable, even signed in
    code, h, body = call(port, "POST", "/api/login", {"username": "jane doe",
                                                      "password": "labpass-2"})
    cookie = _cookie(h)
    for path in ("/api/admin/hub/stop", "/api/admin/hub/pause-processing"):
        code, _, _ = call(port, "POST", path, {"password": PW, "force": True}, cookie=cookie)
        assert code == 403
    assert not (data / "paused").exists()
    # another site's page can't post through the user's session
    code, _, _ = call(port, "POST", "/api/reprocess", {"sample_ids": [1]}, cookie=cookie,
                      headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert code == 403
    # no scheme header from the proxy: sign-in says to use https, not "cross-site"
    no_scheme = {"X-Forwarded-Proto": None}
    code, _, body = call(port, "POST", "/api/login", {"username": "jane doe",
                                                      "password": "labpass-2"},
                         headers=no_scheme)
    assert code == 403 and body["error"] == "Sign in over https: open https://gc.asaplabs.net"
    # a spoofed CF-Connecting-IP from a LAN host is ignored: covered in process
    # (test_netctx); here, a forged sign-in cookie is just signed out
    code, _, _ = call(port, "GET", "/api/files", cookie="__Host-gc_session=forged")
    assert code == 401
    # sign out ends it
    code, _, _ = call(port, "POST", "/api/logout", {}, cookie=cookie)
    assert code == 200
    code, _, _ = call(port, "GET", "/api/session", cookie=cookie)
    assert code == 401


def test_the_tray_still_works_locally(hub):
    port, _data, _db, _stub, _tmp = hub
    code, _, body = call(port, "GET", "/api/hub/status", tunnel=False)
    assert code == 200 and body["hub_url"] == "https://gc.asaplabs.net"
    code, _, body = call(port, "POST", "/api/admin/hub/pause-processing",
                         {"password": "wrong-password"}, tunnel=False)
    assert code == 403 and "password" in body["error"].lower()
