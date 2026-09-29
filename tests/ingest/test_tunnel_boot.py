"""The booted app behind the Cloudflare tunnel (https://gc.asaplabs.net).

cloudflared forwards to http://localhost:5560, so a tunnel request is
simulated as a request to 127.0.0.1 with ``Host: gc.asaplabs.net`` and
Cloudflare's headers. Covers app.py's cross-site guard + admin_auth's (both
on /api/admin/setup), the installer's hub URL, and /healthz unchanged.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_helpers import admin_post  # noqa: E402

pytest.importorskip("flask")

import store  # noqa: E402
from bootapp import booted, get, send  # noqa: E402

PW = "tunnel-admin-2026"
PUBLIC = "gc.asaplabs.net"
INSTALLER = "/api/admin/instruments/gc1/installer"


def tunnel(**extra):
    h = {"Host": PUBLIC, "CF-Connecting-IP": "203.0.113.7", "CF-Ray": "8c0ffee-AMS",
         "X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https",
         "Origin": f"https://{PUBLIC}", "Sec-Fetch-Site": "same-origin",
         "Content-Type": "application/json"}
    h.update(extra)
    return h


def _raw(port, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_tunnel_setup_guard_and_installer(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    store.migrate(data / "gc.db")
    store.instruments.upsert({"id": "gc1", "name": "GC-1", "live_since": datetime(2020, 1, 1)},
                             db=data / "gc.db")
    with booted(tmp_path) as (port, _proc, data, _home):
        code, health = get(port, "/healthz")
        assert code == 200 and health["status"] == "ok"
        assert {"version", "pid", "active_sessions", "idle_seconds"} <= set(health)

        setup_code = (data / "admin-setup-code.txt").read_text(encoding="utf-8").strip()
        good = {"password": PW, "setup_code": setup_code}

        # a foreign page or a foreign Host through the tunnel: refused
        code, _h, _b = _raw(port, "/api/admin/setup", good, tunnel(Origin="https://evil.example"))
        assert code == 403
        code, _h, _b = _raw(port, "/api/admin/setup", good,
                            tunnel(Host="evil.example", Origin="https://evil.example"))
        assert code == 403
        # the public name without https (no X-Forwarded-Proto): refused
        h = tunnel(Origin=f"http://{PUBLIC}")
        del h["X-Forwarded-Proto"]
        code, _h, _b = _raw(port, "/api/admin/setup", good, h)
        assert code == 403
        assert store.settings_kv.get("admin_password", db=data / "gc.db") is None

        # the browser through the tunnel passes both guards
        code, _h, body = _raw(port, "/api/admin/setup", good, tunnel())
        assert code == 201, body

        # an operator POST through the tunnel is not refused as cross-site
        code, _h, body = _raw(port, "/api/settings", {}, tunnel())
        assert code != 403, body

        # the installer through the tunnel gets the public https URL
        code, headers, raw = _raw(port, INSTALLER, {"password": PW}, tunnel())
        assert code == 200, raw
        assert headers["X-GC-Hub-URL"] == f"https://{PUBLIC}"
        inst = json.loads(zipfile.ZipFile(io.BytesIO(raw)).read("install.json"))
        assert inst["hub_url"] == f"https://{PUBLIC}"

        # a loopback proxy request under a LAN name never derives a URL from Host
        code, _h, raw = _raw(port, INSTALLER, {"password": PW, "confirm_revoke": True},
                             tunnel(Host="127.0.0.1", Origin="https://127.0.0.1"))
        assert code == 409 and json.loads(raw).get("needs_hub_url") is True

        # LAN/localhost unchanged: same-origin http from 127.0.0.1 still works,
        # and the loopback Host still needs a configured hub URL
        code, body = admin_post(port, INSTALLER, {"password": PW, "confirm_revoke": True},
                                {"Origin": f"http://127.0.0.1:{port}",
                                 "Sec-Fetch-Site": "same-origin"})
        assert code == 409 and body.get("needs_hub_url") is True
        code, body = admin_post(port, "/api/admin/setup", good,
                                {"Origin": "http://evil.example"})
        assert code == 403

        code, health2 = get(port, "/healthz")
        assert code == 200 and set(health2) == set(health)
