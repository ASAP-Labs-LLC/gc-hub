"""The diagnostics routes in hub_admin, in-process: hub_admin's Blueprint on a
bare Flask app over a seeded data folder (app.py is never imported). The
booted end-to-end download is in tests/test_diagnostics_boot.py."""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
for _p in (ROOT, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

flask = pytest.importorskip("flask")

import admin_auth  # noqa: E402
import diagnostics  # noqa: E402
import store  # noqa: E402
from test_diagnostics import MARKER, Seeded  # noqa: E402

PW = "diag-admin-pw"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    s = Seeded(tmp_path, monkeypatch)
    monkeypatch.setenv("GC_DATA_DIR", str(s.data))
    store.settings_kv.set("admin_password", admin_auth._encode(PW), db=s.db)
    admin_auth.reset_throttle()
    import hub_admin
    app = flask.Flask(__name__)
    app.register_blueprint(hub_admin.bp)
    yield app.test_client(), s
    admin_auth.reset_throttle()


def _bundle(c, body, **kw):
    return c.post("/api/admin/diagnostics/bundle", json=body, **kw)


def test_bundle_downloads_a_zip(client):
    c, s = client
    r = _bundle(c, {"password": PW, "options": {"all_cdfs": True}})
    assert r.status_code == 200, r.get_data(as_text=True)[:500]
    assert r.mimetype == "application/zip"
    assert "no-store" in r.headers["Cache-Control"]
    cd = r.headers["Content-Disposition"]
    assert cd.startswith("attachment;") and ".zip" in cd
    data = r.get_data()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        manifest = json.loads(z.read("manifest.json"))
        for n in z.namelist():
            assert MARKER.encode() not in z.read(n), n
    assert manifest["who"] == "admin@127.0.0.1"
    assert manifest["options"]["all_cdfs"] is True
    # the temp file is gone once streamed
    tmp = s.data / diagnostics.TMP_DIRNAME
    assert not any(p.suffix in (".part", ".zip") for p in tmp.iterdir())


def test_bundle_needs_json_and_the_password(client):
    c, _ = client
    assert c.post("/api/admin/diagnostics/bundle", data="{}",
                  content_type="text/plain").status_code == 415
    assert _bundle(c, {"password": "wrong"}).status_code == 403


def test_bad_options_are_a_400(client):
    c, _ = client
    assert _bundle(c, {"password": PW, "options": {"nope": True}}).status_code == 400
    assert _bundle(c, {"password": PW, "options": {"logs": 1}}).status_code == 400


def test_a_second_concurrent_build_is_a_409(client):
    c, _ = client
    with diagnostics.exclusive():
        r = _bundle(c, {"password": PW})
    assert r.status_code == 409
    assert "already" in r.get_json()["error"]


def test_estimate_needs_the_password_header(client):
    c, s = client
    assert c.get("/api/admin/diagnostics/estimate").status_code == 403
    assert c.get("/api/admin/diagnostics/estimate",
                 headers={"X-Admin-Password": "wrong"}).status_code == 403
    r = c.get("/api/admin/diagnostics/estimate", headers={"X-Admin-Password": PW})
    assert r.status_code == 200
    assert "no-store" in r.headers["Cache-Control"]
    rows = r.get_json()["options"]
    assert [o["key"] for o in rows] == list(diagnostics.OPTION_KEYS)
    opts = {o["key"]: o for o in rows}
    assert opts["all_cdfs"]["files"] == 9 and opts["all_cdfs"]["default"] is False


def test_estimate_refuses_cross_site(client):
    c, _ = client
    r = c.get("/api/admin/diagnostics/estimate",
              headers={"X-Admin-Password": PW, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_leftover_temp_files_are_cleaned(client):
    import os
    import time
    c, s = client
    tmp = s.data / diagnostics.TMP_DIRNAME
    tmp.mkdir(exist_ok=True)
    old = tmp / "gc-diagnostics-old.zip.part"
    old.write_bytes(b"x")
    past = time.time() - 2 * 3600
    os.utime(old, (past, past))
    assert _bundle(c, {"password": PW}).status_code == 200
    assert not old.exists()
