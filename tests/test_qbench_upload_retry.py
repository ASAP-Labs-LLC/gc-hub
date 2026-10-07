"""v6.0.0: a failed QBench upload says why, and can be sent again.

Ryan: "I can't retry an upload if it fails, I have to clear the queue and
resubmit" and "Upload is failing for some reason". End to end on a booted
hub (``tests/qbench_stub_boot.py``: the real uploader with Chrome stubbed
out, its outcome per lab ID from a plan file):

* the saved sign-in file can't be read / is empty → the hub says exactly
  that before anything is queued, and asks for the password;
* no QBench API key → said before anything is queued;
* an item that fails carries the uploader's real reason (one line), never
  "Upload returned false", and app.log has it with the traceback;
* after a finished or stopped upload, a new POST with just the failed item
  starts cleanly, and only an uploaded item is recorded as sent;
* a stream opened after the run ended still learns how it ended (snapshot),
  so the sheet never waits forever.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")
pytest.importorskip("selenium")

import bootapp  # noqa: E402
from bootapp import get, post, wait_for  # noqa: E402


@pytest.fixture(scope="module")
def hub():
    import hub_boot
    tmp = Path(tempfile.mkdtemp(prefix="gc-v6-qb-"))
    try:
        built = hub_boot.build_hub(tmp)
        home = tmp / "home"
        home.mkdir(exist_ok=True)
        plan = tmp / "plan.json"
        plan.write_text("{}", encoding="utf-8")
        login_file = home / "qbenchlogin.txt"            # absent at first
        env = {"GC_TEST_QBENCH_PLAN": str(plan),
               "QBENCH_LOGIN_FILE": str(login_file),
               "QBENCH_TOKEN_URL": "http://127.0.0.1:9/oauth2/v1/token"}
        cmd = [sys.executable, str(TESTS / "qbench_stub_boot.py"), "--no-tray"]
        with bootapp.booted(tmp, cmd=cmd, extra_env=env) as (port, _proc, data, home_dir):
            bootapp.sign_in(port, data)
            yield {"port": port, "data": data, "home": home_dir, "plan": plan,
                   "login_file": login_file, "ids": built.ids, "tmp": tmp}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _plan(h, **actions):
    h["plan"].write_text(json.dumps(actions), encoding="utf-8")


def _api_key(h, present: bool):
    store = h["home"] / "qbench.json"
    if present:
        store.write_text(json.dumps({"client_id": "test-client-id",
                                     "client_secret": "test-client-secret"}), encoding="utf-8")
    elif store.exists():
        store.unlink()


def _item(h, key="final"):
    return {"sample_id": h["ids"][key], "standard_name": "Diesel", "lab_id": "ignored",
            "doc_name": "GC Analysis", "conclusion": "", "overlay_standards": [], "ranges": []}


def _finished(h, timeout=60.0):
    out = {}

    def done():
        code, body = get(h["port"], "/api/qbench-upload-status")
        out.update(body or {})
        return code == 200 and not body["active"]
    assert wait_for(done, timeout=timeout, interval=0.3), out
    return out


def _uploaded_at(h, key="final"):
    import store
    return store.samples.get(h["ids"][key], db=h["data"] / store.DB_FILENAME)["qbench_uploaded_at"]


def _app_log(h) -> str:
    return (h["data"] / "app.log").read_text(encoding="utf-8", errors="replace")


def test_an_unreadable_sign_in_file_is_named_and_the_password_asked_for(hub):
    _api_key(hub, True)
    code, body = get(hub["port"], "/api/qbench-credentials")
    assert code == 200
    assert body["has_password"] is False
    assert body["problem"].startswith("Can't read the saved QBench sign-in file")
    assert "qbenchlogin.txt" in body["problem"]

    code, body = post(hub["port"], "/api/qbench-upload", {"queue": [_item(hub)]})
    assert code == 400, body
    assert body["need_password"] is True
    assert body["error"].startswith("Can't read the saved QBench sign-in file")
    code, st = get(hub["port"], "/api/qbench-upload-status")
    assert st["active"] is False and st["items"] == []        # nothing was queued

    hub["login_file"].write_text("\n\n", encoding="utf-8")
    code, body = get(hub["port"], "/api/qbench-credentials")
    assert "is empty" in body["problem"]
    code, body = post(hub["port"], "/api/qbench-upload", {"queue": [_item(hub)]})
    assert code == 400 and body["need_password"] is True
    assert "is empty" in body["error"]
    hub["login_file"].unlink()


def test_a_missing_api_key_is_named_before_anything_is_queued(hub):
    _api_key(hub, False)
    try:
        code, body = post(hub["port"], "/api/qbench-upload",
                          {"queue": [_item(hub)], "username": "lab@example.com", "password": "pw"})
        assert code == 400, body
        assert body.get("need_password") is not True
        assert "QBench API" in body["error"] and "Settings" in body["error"]
    finally:
        _api_key(hub, True)


def test_a_failed_item_says_why_and_a_retry_starts_cleanly(hub):
    _api_key(hub, True)
    _plan(hub)                                   # 'real': the API lookup fails (closed port)
    code, body = post(hub["port"], "/api/qbench-upload",
                      {"queue": [_item(hub)], "username": "lab@example.com", "password": "pw"})
    assert code == 200 and body["status"] == "started", body
    st = _finished(hub)
    row = st["items"][0]
    assert row["status"] == "failed", row
    assert row["sample_id"] == hub["ids"]["final"]
    assert row["msg"].startswith("QBench API:"), row
    assert "Upload returned false" not in row["msg"]
    assert "\n" not in row["msg"]
    assert st["overall"]["status"] == "allfailed"
    assert _uploaded_at(hub) is None                 # failed: never recorded as sent
    log = _app_log(hub)
    assert "40304" in log and "QBench API" in log

    # the retry: the same item, after the thread finished → a fresh run
    _plan(hub, **{"40304": "ok"})
    code, body = post(hub["port"], "/api/qbench-upload",
                      {"queue": [_item(hub)], "username": "lab@example.com", "password": "pw"})
    assert code == 200 and body["status"] == "started", body
    st = _finished(hub)
    assert [r["status"] for r in st["items"]] == ["ok"], st
    assert st["overall"]["status"] == "done"
    assert _uploaded_at(hub) is not None

    # the typed sign-in was saved (QBENCH_LOGIN_FILE), so the next one needs none
    assert hub["login_file"].read_text(encoding="utf-8").split() == ["lab@example.com", "pw"]
    code, body = get(hub["port"], "/api/qbench-credentials")
    assert body["problem"] is None and body["has_password"] is True


def test_an_uploader_exception_is_one_line_in_the_sheet_and_a_traceback_in_the_log(hub):
    _plan(hub, **{"40304": "boom"})
    code, body = post(hub["port"], "/api/qbench-upload", {"queue": [_item(hub, "rerun")]})
    assert code == 200, body
    st = _finished(hub)
    row = st["items"][0]
    assert row["status"] == "failed"
    assert row["msg"] == "RuntimeError: kaboom in the uploader", row
    log = _app_log(hub)
    assert "kaboom in the uploader" in log and "Traceback" in log

    _plan(hub, **{"40304": "false:QBench has no sample with lab ID '40304'."})
    post(hub["port"], "/api/qbench-upload", {"queue": [_item(hub, "rerun")]})
    st = _finished(hub)
    assert st["items"][0]["msg"] == "QBench has no sample with lab ID '40304'."


def test_a_stream_opened_after_the_run_ended_learns_how_it_ended(hub):
    _plan(hub, **{"40304": "ok"})
    post(hub["port"], "/api/qbench-upload", {"queue": [_item(hub)]})
    _finished(hub)
    req = urllib.request.Request(f"http://127.0.0.1:{hub['port']}/api/qbench-upload/stream",
                                 headers=bootapp.cookie_header(hub["port"]))
    with urllib.request.urlopen(req, timeout=10) as r:
        first = None
        deadline = time.time() + 10
        while time.time() < deadline:
            line = r.readline().decode()
            if line.startswith("data: "):
                first = json.loads(line[6:])
                break
    assert first is not None
    assert first["t"] == "snapshot"
    assert first["active"] is False
    assert first["overall"]["status"] == "done"
    assert first["items"][0]["sample_id"] == hub["ids"]["final"]


def test_a_new_upload_right_after_stop_starts_cleanly(hub):
    """Stop, then Retry at once: the stopping thread must not swallow it
    (it used to be 'appended' to a thread on its way out, and never sent)."""
    _plan(hub, **{"40304": "slow"})
    code, body = post(hub["port"], "/api/qbench-upload", {"queue": [_item(hub)]})
    assert code == 200 and body["status"] == "started"
    assert wait_for(lambda: get(hub["port"], "/api/qbench-upload-status")[1]["items"][0]["status"]
                    == "uploading", timeout=30)
    code, _ = post(hub["port"], "/api/qbench-cancel", {})
    assert code == 200
    _plan(hub, **{"40304": "ok"})
    code, body = post(hub["port"], "/api/qbench-upload", {"queue": [_item(hub)]}, timeout=30)
    assert code == 200, body
    assert body["status"] == "started", body
    st = _finished(hub)
    assert [r["status"] for r in st["items"]] == ["ok"], st
    assert st["overall"]["status"] == "done"


def test_item_events_carry_the_sample_id():
    """The sheet matches a row to its queued report by sample_id (re-injections
    share a lab ID), so every item event names it."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    start = src.index("def _emit_item(")
    body = src[start:src.index("def _emit_overall(", start)]
    assert '"sample_id": ' in body
