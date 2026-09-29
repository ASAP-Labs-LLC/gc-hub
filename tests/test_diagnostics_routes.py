"""The diagnostics routes in hub_admin, in-process: hub_admin's Blueprint on a
bare Flask app over a seeded hub layout (app.py is never imported). The
booted end-to-end download is in tests/test_diagnostics_boot.py.

POST /api/admin/diagnostics/bundle answers 202 {job} at once and builds the
zip in the background (v3.0.1: a build takes minutes, and Cloudflare ends a
request after 100 s); POST /api/admin/diagnostics/status gives the job, whose
``result.summary`` carries the one-time download URL; GET
/api/admin/diagnostics/download/<token> streams it once and deletes it (M3).
The estimate is a POST with the password in the JSON body (M1)."""
from __future__ import annotations

import io
import json
import sys
import time
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
from test_diagnostics import MARKER, Seeded, _hits  # noqa: E402

PW = "diag-admin-pw-é中"          # outside Latin-1: fine in a JSON body


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GC_UPDATER_DIR", raising=False)
    monkeypatch.delenv("GC_APP_ROOT", raising=False)
    s = Seeded(tmp_path, monkeypatch)
    monkeypatch.setenv("GC_DATA_DIR", str(s.data))
    store.settings_kv.set("admin_password", admin_auth._encode(PW), db=s.db)
    admin_auth.reset_throttle()
    diagnostics.clear_estimate_cache()
    import hub_admin
    monkeypatch.setattr(hub_admin, "DIAG_JOBS", hub_admin.AdminJobs())
    app = flask.Flask(__name__)
    app.register_blueprint(hub_admin.bp)
    yield app.test_client(), s
    admin_auth.reset_throttle()
    with diagnostics._state_lock:
        diagnostics._downloads.clear()


def _bundle(c, body, **kw):
    return c.post("/api/admin/diagnostics/bundle", json=body, **kw)


def _status(c):
    r = c.post("/api/admin/diagnostics/status", json={"password": PW})
    assert r.status_code == 200, r.get_data(as_text=True)[:500]
    assert "no-store" in r.headers["Cache-Control"]
    return r.get_json()["job"]


def _finished(c, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = _status(c)
        if job["state"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"the build did not finish: {job}")


def _built(c, body):
    """Start a build (202 at once), wait for its job, return the result."""
    r = _bundle(c, body)
    assert r.status_code == 202, r.get_data(as_text=True)[:500]
    assert "no-store" in r.headers["Cache-Control"]
    job = r.get_json()["job"]
    assert job["kind"] == "diagnostics-bundle" and job["state"] == "running"
    assert "password" not in job["params"]
    done = _finished(c)
    assert done["id"] == job["id"]
    assert done["state"] == "done", done
    return done["result"]["summary"]


def _build_and_fetch(c, body):
    j = _built(c, body)
    g = c.get(j["download"])
    return j, g


def test_bundle_then_one_time_download(client):
    c, s = client
    j, g = _build_and_fetch(c, {"password": PW, "options": {"all_cdfs": True}})
    assert j["name"].startswith("gc-diagnostics-") and j["name"].endswith(".zip")
    assert j["size"] > 0 and j["files"] > 0
    assert g.status_code == 200
    assert g.mimetype == "application/zip"
    assert "no-store" in g.headers["Cache-Control"]
    cd = g.headers["Content-Disposition"]
    assert cd.startswith("attachment;") and j["name"] in cd
    data = g.get_data()
    assert len(data) == j["size"]
    g.close()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        manifest = json.loads(z.read("manifest.json"))
        members = {n: z.read(n) for n in z.namelist()}
    assert _hits(members, MARKER) == []
    assert _hits(members, PW) == []
    assert manifest["who"] == "127.0.0.1"    # no session on this bare app: the address alone
    assert manifest["options"]["all_cdfs"] is True
    # single use, and the file is gone once streamed
    assert c.get(j["download"]).status_code == 404
    tmp = s.data / diagnostics.TMP_DIRNAME
    assert not any(p.suffix in (".part", ".zip") for p in tmp.iterdir())
    assert diagnostics.busy() is False


def test_the_typed_password_is_redacted(client):
    c, s = client
    store.samples.update(s.error_id, review_note=f"pw was {PW}", db=s.db)
    with open(s.data / "app.log", "a", encoding="utf-8") as f:
        f.write(f"typed {PW} somewhere\n")
    _j, g = _build_and_fetch(c, {"password": PW})
    with zipfile.ZipFile(io.BytesIO(g.get_data())) as z:
        members = {n: z.read(n) for n in z.namelist()}
    g.close()
    assert _hits(members, PW) == []


def test_a_waiting_download_keeps_the_hub_busy_until_fetched(client):
    c, _s = client
    j = _built(c, {"password": PW})
    assert diagnostics.busy() is True
    g = c.get(j["download"])
    g.get_data()
    g.close()
    assert diagnostics.busy() is False


def test_download_tokens_expire(client, monkeypatch):
    c, s = client
    j = _built(c, {"password": PW})
    path = next((s.data / diagnostics.TMP_DIRNAME).glob("*.zip"))
    monkeypatch.setattr(diagnostics.time, "time", lambda: time.monotonic() + 10 ** 10)
    assert c.get(j["download"]).status_code == 404
    assert not path.exists()


def test_unknown_token_is_a_404(client):
    c, _ = client
    assert c.get("/api/admin/diagnostics/download/nope").status_code == 404


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


def test_not_enough_disk_fails_the_build_job(client, monkeypatch):
    """The disk check needs the estimate (a walk of every stored CDF), so it
    runs in the job: a refusal is a failed job with the reason, not a 507."""
    c, s = client
    import collections
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(diagnostics.shutil, "disk_usage", lambda p: usage(10 ** 12, 0, 1000))
    r = _bundle(c, {"password": PW})
    assert r.status_code == 202, r.get_data(as_text=True)[:500]
    job = _finished(c)
    assert job["state"] == "failed" and "free disk space" in job["error"], job
    assert job["result"] is None
    assert diagnostics.busy() is False
    tmp = s.data / diagnostics.TMP_DIRNAME
    assert not any(p.suffix in (".part", ".zip") for p in tmp.iterdir())


def test_the_bundle_answers_at_once_and_builds_in_the_background(client, monkeypatch):
    c, _s = client
    import threading
    release = threading.Event()
    real = diagnostics.build_bundle

    def slow(options, **kw):
        kw["progress"]({"phase": "database"})
        assert release.wait(10), "the test never released the build"
        return real(options, **kw)

    monkeypatch.setattr(diagnostics, "build_bundle", slow)
    t0 = time.monotonic()
    r = _bundle(c, {"password": PW})
    assert r.status_code == 202, r.get_data(as_text=True)[:500]
    assert time.monotonic() - t0 < 5
    try:
        deadline = time.time() + 10
        while (_status(c)["progress"] or {}).get("phase") != "database":
            assert time.time() < deadline, _status(c)
            time.sleep(0.01)
        running = _status(c)
        assert running["state"] == "running" and running["result"] is None
        assert diagnostics.busy() is True             # holds off the 3 AM restart and Stop
        second = _bundle(c, {"password": PW})         # one build at a time
        assert second.status_code == 409 and "already" in second.get_json()["error"]
    finally:
        release.set()
    done = _finished(c)
    assert done["state"] == "done", done
    res = done["result"]["summary"]
    assert set(res) == {"download", "name", "size", "files", "skipped"}
    g = c.get(res["download"])
    assert g.status_code == 200 and len(g.get_data()) == res["size"]
    g.close()


def test_status_needs_the_password(client):
    c, _ = client
    assert c.post("/api/admin/diagnostics/status", json={"password": "wrong"}).status_code == 403
    assert c.post("/api/admin/diagnostics/status", data="{}",
                  content_type="text/plain").status_code == 415
    assert _status(c) is None                          # no build yet


def test_estimate_is_a_post_with_the_password_in_json(client):
    c, _ = client
    assert c.post("/api/admin/diagnostics/estimate", json={}).status_code == 403
    assert c.post("/api/admin/diagnostics/estimate",
                  json={"password": "wrong"}).status_code == 403
    assert c.post("/api/admin/diagnostics/estimate", data="{}",
                  content_type="text/plain").status_code == 415
    r = c.post("/api/admin/diagnostics/estimate", json={"password": PW})
    assert r.status_code == 200
    assert "no-store" in r.headers["Cache-Control"]
    rows = r.get_json()["options"]
    assert [o["key"] for o in rows] == list(diagnostics.OPTION_KEYS)
    opts = {o["key"]: o for o in rows}
    assert opts["all_cdfs"]["files"] == 9 and opts["all_cdfs"]["default"] is False
    assert c.get("/api/admin/diagnostics/estimate").status_code == 405


def test_leftover_temp_files_are_cleaned(client):
    import os
    c, s = client
    tmp = s.data / diagnostics.TMP_DIRNAME
    tmp.mkdir(exist_ok=True)
    old = tmp / "gc-diagnostics-old.zip.part"
    old.write_bytes(b"x")
    past = time.time() - 2 * 3600
    os.utime(old, (past, past))
    _built(c, {"password": PW})
    assert not old.exists()


def test_two_builds_in_the_same_second_do_not_collide(client):
    import hub_admin
    names = {hub_admin._bundle_name() for _ in range(50)}
    assert len(names) == 50 and all(n.startswith("gc-diagnostics-") for n in names)
