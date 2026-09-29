"""The diagnostics download, booted for real (tests/bootapp.py) over a hub
with real samples (tests/hub_boot.py), and the admin page's Diagnostics panel
in headless Chrome.

Secrets are seeded the way production holds them (the admin password hash set
through first-use setup, an agent token hash, secret-looking settings.json
keys, the QBench store the child resolves, a Selenium login file in the data
folder); the downloaded zip must contain none of them, the database copy's
bytes included.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

import store  # noqa: E402
from bootapp import (booted, browser_sign_in, cookie_header, get, setup_admin,  # noqa: E402
                     wait_for)
from hub_boot import build_hub  # noqa: E402

MARKER = "BOOTSEKRITq9z"


def _post(port, path, body, timeout=120):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **cookie_header(port)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _signed(port, path):
    return urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=cookie_header(port))


def _download(port, body, timeout=120):
    """Build (POST) then fetch the one-time download (GET)."""
    code, headers, data = _post(port, "/api/admin/diagnostics/bundle", body, timeout)
    if code != 200:
        return code, headers, data
    link = json.loads(data)["download"]
    try:
        with urllib.request.urlopen(_signed(port, link), timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.fixture(scope="module")
def diag_hub(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("diag-boot")
    hub = build_hub(tmp)
    conf = json.loads((hub.data / "settings.json").read_text(encoding="utf-8"))
    conf["qbench_api_secret"] = f"s-{MARKER}"
    (hub.data / "settings.json").write_text(json.dumps(conf), encoding="utf-8")
    (hub.data / "qbenchlogin.txt").write_text(f"u-{MARKER}\np-{MARKER}\n", encoding="utf-8")
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    (home / "qbench.json").write_text(json.dumps({"client_id": f"id-{MARKER}",
                                                  "client_secret": f"sec-{MARKER}"}),
                                      encoding="utf-8")
    store.instruments.upsert({"id": "gc1", "token_hash": f"th-{MARKER}"}, db=hub.db)
    leftover = hub.data / "diagnostics-tmp" / "gc-diagnostics-killed.zip.part"
    leftover.parent.mkdir()
    leftover.write_bytes(b"left by a killed build")
    with booted(tmp) as (port, _proc, data, _home):
        assert not leftover.exists()                  # cleaned at start-up (M4)
        pw = setup_admin(port, data)
        admin_hash = store.settings_kv.get("admin_password", db=hub.db)
        assert admin_hash
        yield port, hub, pw, admin_hash


def test_download_validates_and_holds_no_secret(diag_hub):
    port, hub, pw, admin_hash = diag_hub
    code, headers, data = _download(port, {"password": pw, "options": {"all_cdfs": True}})
    assert code == 200, data[:500]
    assert headers["Content-Type"] == "application/zip"
    assert "no-store" in headers["Cache-Control"]
    assert headers["Content-Disposition"].startswith('attachment; filename="gc-diagnostics-')

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = {n: z.read(n) for n in z.namelist()}
    for name, raw in members.items():
        assert MARKER.encode() not in raw, name
        assert admin_hash.encode() not in raw, name
        for part in admin_hash.split("$")[2:]:
            assert part.encode() not in raw, name
        assert name.rsplit("/", 1)[-1] not in ("qbenchlogin.txt", "admin-setup-code.txt",
                                               "qbench.json"), name

    manifest = json.loads(members["manifest.json"])
    status, health = get(port, "/healthz")
    assert manifest["app_version"] == health["version"]
    assert manifest["who"] == "Test Operator (127.0.0.1)"      # the signed-in name
    assert manifest["runtime"]["running"] is True
    assert manifest["runtime"]["worker_alive"] is True
    assert manifest["runtime"]["exporter_alive"] is True
    assert "status_snapshot" in manifest
    listed = {f["name"]: f for f in manifest["files"]}
    assert set(listed) == set(members) - {"manifest.json"}
    for name, f in listed.items():
        assert f["sha256"] == hashlib.sha256(members[name]).hexdigest(), name
    for need in ("summary.txt", "logs/app.log", "tables/instruments.json",
                 "settings/settings.json", "database/gc.db", "exports/health.json"):
        assert need in members, need
    held = store.samples.get(hub.ids["held"], db=hub.db)
    assert held["cdf_path"] in members          # awaiting_calibration: a problem CDF
    final = store.samples.get(hub.ids["final"], db=hub.db)
    assert final["cdf_path"] in members         # all_cdfs was asked for
    settings = json.loads(members["settings/settings.json"])
    assert settings["qbench_api_secret"] == "[REDACTED]"
    # the temp file is gone
    assert wait_for(lambda: not any((hub.data / "diagnostics-tmp").glob("*.part")), timeout=10)


def test_estimate_and_gating(diag_hub):
    port, _hub, pw, _h = diag_hub
    code, headers, raw = _post(port, "/api/admin/diagnostics/estimate", {"password": pw})
    assert code == 200 and "no-store" in headers["Cache-Control"]
    body = json.loads(raw)
    keys = [o["key"] for o in body["options"]]
    assert keys[0] == "summary" and "all_cdfs" in keys
    assert next(o for o in body["options"] if o["key"] == "all_cdfs")["files"] >= 8
    assert _post(port, "/api/admin/diagnostics/bundle", {"password": "wrong"})[0] == 403
    code, _h2, _d = _post(port, "/api/admin/diagnostics/bundle", {"password": pw})
    link = json.loads(_d)["download"]
    # the download link needs a session too (spec D3)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}{link}", timeout=30)
        raise AssertionError("a download worked without a session")
    except urllib.error.HTTPError as e:
        assert e.code == 401
    with urllib.request.urlopen(_signed(port, link), timeout=60) as r:
        r.read()
    try:
        urllib.request.urlopen(_signed(port, link), timeout=30)
        raise AssertionError("a download token worked twice")
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_idle_check_waits_for_diagnostics():
    """M4: the 3 AM auto-restart does not kill a build or a download."""
    import ast
    src = (TESTS.parent / "app.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_is_server_idle")
    assert "diagnostics.busy()" in ast.unparse(fn)


def test_the_app_keeps_serving_during_a_build(diag_hub):
    import threading
    port, _hub, pw, _h = diag_hub
    result = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "r", _download(port, {"password": pw, "options": {"all_cdfs": True}})))
    t.start()
    started = time.time()
    while t.is_alive() and time.time() - started < 60:
        code, body = get(port, "/healthz")
        assert code == 200 and body["status"] == "ok"
        time.sleep(0.05)
    t.join(120)
    assert result["r"][0] == 200


# ── the admin page ──────────────────────────────────────────────────────────

def _driver():
    webdriver = pytest.importorskip("selenium.webdriver")
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1400,1000",
                "--disable-dev-shm-usage"):
        opts.add_argument(arg)
    try:
        return webdriver.Chrome(options=opts)
    except Exception as exc:  # noqa: BLE001 - no Chrome / driver here
        pytest.skip(f"headless Chrome unavailable: {exc}")


def _wait(pred, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = pred()
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return v
        time.sleep(0.2)
    return pred()


def test_admin_page_diagnostics_panel(diag_hub):
    port, _hub, pw, _h = diag_hub
    drv = _driver()
    browser_sign_in(drv, port)
    try:
        drv.get(f"http://127.0.0.1:{port}/admin/hub")
        text = drv.find_element("id", "diag-panel").text
        assert "Diagnostics" in text and "Claude" in text
        assert "soft-deleted comments" in text and "IP addresses" in text     # M6
        assert "qbenchlogin.txt on the share" in text
        boxes = drv.execute_script(
            "return Array.from(document.querySelectorAll('#diag-options input[type=checkbox]'))"
            ".map(b => [b.dataset.key, b.checked]);")
        assert dict(boxes) == {"summary": True, "logs": True, "tables": True, "settings": True,
                               "database": True, "exports": True, "reports": True,
                               "problem_cdfs": True, "calibration": True, "updater": True,
                               "environment": True, "all_cdfs": False}
        drv.find_element("id", "pw").send_keys(pw)
        drv.find_element("id", "btn-diag-estimate").click()
        assert _wait(lambda: "B" in drv.find_element("id", "diag-total").text), \
            drv.find_element("id", "diag-total").text
        sizes = drv.execute_script(
            "return Array.from(document.querySelectorAll('#diag-options .diag-size'))"
            ".map(td => td.textContent);")
        assert all(s and s != "?" for s in sizes), sizes
        # ticking all CDFs shows the size warning
        drv.execute_script("const b = document.querySelector('#diag-options input[data-key=all_cdfs]');"
                           "b.click();")
        assert _wait(lambda: "All raw CDFs" in drv.find_element("id", "diag-warning").text), \
            drv.find_element("id", "diag-warning").text
        drv.execute_script("const b = document.querySelector('#diag-options input[data-key=all_cdfs]');"
                           "b.click();")
        drv.find_element("id", "btn-diag-download").click()
        assert _wait(lambda: "Download started" in drv.find_element("id", "diag-msg").text, 90), \
            drv.find_element("id", "diag-msg").text
    finally:
        drv.quit()
