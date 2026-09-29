"""2B1 T6: the real admin password (D13), first-use setup, the installer
download and agent commands, in the booted app."""
from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_helpers import admin_post, get_json, heartbeat  # noqa: E402

pytest.importorskip("flask")

import store  # noqa: E402
from bootapp import booted, cookie_header, get, send  # noqa: E402

PW = "lab-admin-2026"
INSTALLER = "/api/admin/instruments/gc1/installer"


def _prepare(tmp: Path):
    data = tmp / "data"
    data.mkdir()
    store.migrate(data / "gc.db")
    store.instruments.upsert({"id": "gc1", "name": "GC-1", "live_since": datetime(2020, 1, 1)},
                             db=data / "gc.db")


def _download(port, body, headers=None):
    """POST the installer route; ``(status, zipfile | json)``."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}{INSTALLER}",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **cookie_header(port), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            assert r.headers["Content-Type"].startswith("application/zip")
            assert "no-store" in r.headers.get("Cache-Control", "")
            return r.status, zipfile.ZipFile(io.BytesIO(r.read()))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_setup_gate_installer_and_commands(tmp_path):
    _prepare(tmp_path)
    with booted(tmp_path) as (port, _proc, data, _home):
        db = data / "gc.db"
        # ── before setup: every admin action is refused, pointing to /admin/setup
        code, body = admin_post(port, "/api/save-analysis-defaults", {"password": "admin", "params": {}})
        assert code == 403 and "/admin/setup" in body["error"], body
        code, body = admin_post(port, INSTALLER, {"password": "admin"})
        assert code == 403 and "/admin/setup" in body["error"], body

        # ── the setup page and API
        page = send(port, "/admin/setup", None, method="GET")
        assert page[0] == 200
        setup_code = (data / "admin-setup-code.txt").read_text(encoding="utf-8").strip()
        good = {"password": PW, "setup_code": setup_code}
        code, body = admin_post(port, "/api/admin/setup", good, {"Origin": "http://evil.example"})
        assert code == 403
        code, body = admin_post(port, "/api/admin/setup", good, {"Sec-Fetch-Site": "cross-site"})
        assert code == 403
        code, _ = send(port, "/api/admin/setup", json.dumps(good).encode(),
                       {"Content-Type": "text/plain"})
        assert code == 415
        code, body = admin_post(port, "/api/admin/setup", {**good, "password": "short"})
        assert code == 400 and "8" in body["error"]
        code, body = admin_post(port, "/api/admin/setup", {"password": PW})       # no code
        assert code == 403 and "admin-setup-code.txt" in body["error"]
        assert store.settings_kv.get("admin_password", db=db) is None
        code, body = admin_post(port, "/api/admin/setup", good,
                                {"Origin": f"http://127.0.0.1:{port}",
                                 "Sec-Fetch-Site": "same-origin"})
        assert code == 201, body
        assert store.settings_kv.get("admin_password", db=db).startswith("pbkdf2_sha256$")
        assert not (data / "admin-setup-code.txt").exists()
        code, body = admin_post(port, "/api/admin/setup", {**good, "password": "someone-else"})
        assert code == 409

        # ── the gate now uses it; "admin" is gone
        code, body = admin_post(port, "/api/save-analysis-defaults", {"password": "admin", "params": {}})
        assert (code, body) == (403, {"error": "Incorrect password"})
        code, body = admin_post(port, "/api/save-analysis-defaults", {"password": PW, "params": {}})
        assert code == 200, body

        # ── installer download: first mint
        code, z = _download(port, {"password": "wrong"})
        assert code == 403
        # rev 2: no hub URL set -> installers point at https://gc.asaplabs.net (the
        # tunnel), with this machine's LAN address as the agents' fallback; never
        # the request's own address (needs_hub_url is gone)
        code, z = _download(port, {"password": PW})
        assert code == 200, z
        inst0 = json.loads(z.read("install.json"))
        assert inst0["hub_url"] == "https://gc.asaplabs.net"
        assert inst0["lan_url"].startswith("http://") and inst0["lan_url"].endswith(f":{port}")
        assert "127.0.0.1" not in inst0["lan_url"]
        assert admin_post(port, "/api/admin/instruments/gc1/revoke-token",
                          {"password": PW})[0] == 200
        assert store.instruments.get("gc1", db=db)["token_hash"] is None
        code, body = admin_post(port, "/api/admin/hub-url",
                                {"password": PW, "hub_url": f"http://127.0.0.1:{port}"})
        assert code == 200, body
        code, z = _download(port, {"password": PW})
        assert code == 200, z
        names = set(z.namelist())
        assert names == {"install.pyw", "launcher.pyw", "install.json", "agent-package.zip",
                         "agent-package.json"}
        inst = json.loads(z.read("install.json"))
        assert inst["hub_url"] == f"http://127.0.0.1:{port}"
        assert inst["lan_url"] is None               # the hub URL already is a LAN address
        tok1 = inst["token"]
        meta = json.loads(z.read("agent-package.json"))
        assert hashlib.sha256(z.read("agent-package.zip")).hexdigest() == meta["sha256"]
        code, info = get_json(port, "/api/agent/package", tok1)
        assert (code, info) == (200, {"version": meta["version"], "sha256": meta["sha256"]})
        assert z.read("install.pyw") == (Path(__file__).resolve().parents[2] / "agent"
                                         / "install.pyw").read_bytes()
        assert tok1.encode() not in z.read("agent-package.zip")
        row = store.instruments.get("gc1", db=db)
        assert row["token_hash"] == hashlib.sha256(tok1.encode()).hexdigest()

        # a second download must confirm the revocation
        code, body = _download(port, {"password": PW})
        assert code == 409 and body.get("needs_confirm") is True
        assert body["hub_url"] == f"http://127.0.0.1:{port}"          # for the confirm dialog
        assert heartbeat(port, tok1)[0] == 200                     # still valid
        code, z = _download(port, {"password": PW, "confirm_revoke": True})
        assert code == 200
        tok2 = json.loads(z.read("install.json"))["token"]
        assert tok2 != tok1
        assert heartbeat(port, tok1)[0] == 401                     # revoked
        assert heartbeat(port, tok2)[0] == 200

        code, body = admin_post(port, "/api/admin/instruments/gc9/installer",
                                {"password": PW, "confirm_revoke": True})
        assert code == 404

        # hub URL override
        for bad in ("ftp://x", "http://user:pw@asapsv1:5560", "http://asapsv1:5560/gc",
                    "http://asapsv1:99999", "http://asapsv1:5560?x=1", "http://", 5560,
                    "http://gc.asaplabs.net", "https://gc.asaplabs.net/x"):   # https off the LAN
            code, body = admin_post(port, "/api/admin/hub-url", {"password": PW, "hub_url": bad})
            assert code == 400, bad
        code, body = admin_post(port, "/api/admin/hub-url",
                                {"password": PW, "hub_url": "http://asapsv1:5560/"})
        assert code == 200 and body["hub_url"] == "http://asapsv1:5560"
        code, z = _download(port, {"password": PW, "confirm_revoke": True})
        tok3 = json.loads(z.read("install.json"))["token"]
        assert json.loads(z.read("install.json"))["hub_url"] == "http://asapsv1:5560"
        code, body = admin_post(port, "/api/admin/hub-url", {"password": PW, "hub_url": ""})
        assert code == 200 and body["hub_url"] is None
        assert body["effective"] == "https://gc.asaplabs.net"
        code, body = admin_post(port, "/api/admin/hub-url",
                                {"password": PW, "hub_url": "https://gc.example.org:8443/"})
        assert code == 200 and body["hub_url"] == "https://gc.example.org:8443"
        admin_post(port, "/api/admin/hub-url", {"password": PW, "hub_url": ""})

        # ── agent commands
        code, body = admin_post(port, "/api/admin/instruments/gc1/agent-command",
                                {"password": PW, "command": "format-disk"})
        assert code == 400
        code, body = admin_post(port, "/api/admin/instruments/gc1/agent-command",
                                {"password": "nope", "command": "restart"})
        assert code == 403
        code, body = admin_post(port, "/api/admin/instruments/gc1/agent-command",
                                {"password": PW, "command": "restart"})
        assert code == 200, body
        assert heartbeat(port, tok3)[1]["command"] == "restart"
        assert heartbeat(port, tok3)[1]["command"] is None

        # ── change the password
        code, body = admin_post(port, "/api/admin/password",
                                {"password": "wrong-one", "new_password": "x" * 12})
        assert code == 403
        code, body = admin_post(port, "/api/admin/password",
                                {"password": PW, "new_password": "second-pass-1"})
        assert code == 200, body
        assert admin_post(port, "/api/save-analysis-defaults",
                          {"password": "second-pass-1", "params": {}})[0] == 200

        # ── backoff: after repeated failures even the right password waits
        for _ in range(6):
            admin_post(port, "/api/save-analysis-defaults", {"password": "guess", "params": {}})
        code, body = admin_post(port, "/api/save-analysis-defaults",
                                {"password": "second-pass-1", "params": {}})
        assert code == 403 and "try again" in body["error"].lower(), body

        # the setup page says it is done
        code, _ = get(port, "/healthz")
        assert code == 200


def test_setup_page_on_an_empty_data_dir(tmp_path):
    """The health check's empty data folder: the app boots and the setup page works."""
    with booted(tmp_path) as (port, _proc, data, _home):
        code, body = send(port, "/admin/setup", None, method="GET")
        assert code == 200
        setup_code = (data / "admin-setup-code.txt").read_text(encoding="utf-8").strip()
        assert setup_code in (data / "app.log").read_text(encoding="utf-8")   # logged at WARNING
        code, body = admin_post(port, "/api/admin/setup", {"password": PW, "setup_code": setup_code})
        assert code == 201, body
        assert (data / "gc.db").is_file()
