"""v3.0.1: admin routes that can outlast Cloudflare's 100 s (HTTP 524 through
https://gc.asaplabs.net) answer at once and do the work in the background.

In-process: hub_admin's Blueprint on a bare Flask app over a migrated store
(app.py is never imported), with the slow work replaced by fakes that block
until the test lets them go. That makes "the request returned before the work
finished" deterministic instead of a race against a fast fixture. The booted
end-to-end runs are in tests/test_hub_admin_import_history.py and
tests/test_diagnostics_boot.py.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
for _p in (ROOT, TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

flask = pytest.importorskip("flask")

import admin_auth  # noqa: E402
import store  # noqa: E402

PW = "long-requests-admin-pw"
FAST = 2.0          # seconds: "at once" (the fakes block for as long as the test wants)


@pytest.fixture
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    db = data / store.DB_FILENAME
    store.migrate(db)
    store.instruments.upsert({"id": "gc1", "name": "GC-1"}, db=db)
    store.settings_kv.set("admin_password", admin_auth._encode(PW), db=db)
    monkeypatch.setenv("GC_DATA_DIR", str(data))
    admin_auth.reset_throttle()
    import hub_admin
    monkeypatch.setattr(hub_admin, "JOBS", hub_admin.AdminJobs())
    app = flask.Flask(__name__)
    app.register_blueprint(hub_admin.bp)
    folder = tmp_path / "processed_cdfs2"
    folder.mkdir()
    yield app.test_client(), folder, db
    admin_auth.reset_throttle()


class Gate:
    """A fake long job: it reports progress until released or stopped."""

    def __init__(self, summary=None):
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = []
        self.summary = summary if summary is not None else {"dry_run": True,
                                                            "counts": {"new": 3}}

    def __call__(self, *args, progress=None, **kwargs):
        self.calls.append((args, dict(kwargs)))
        self.started.set()
        summary = dict(self.summary, partial=True)
        deadline = time.monotonic() + 8     # never hang a synchronous caller forever
        try:
            n = 0
            while not self.release.wait(0.01) and time.monotonic() < deadline:
                n += 1
                if progress is not None:
                    progress({"phase": "import", "done": n, "total": 10 ** 6})
        except BaseException as exc:
            exc.import_summary = summary
            raise
        return dict(self.summary)


def _post(c, path, body=None):
    r = c.post(path, json=dict({"password": PW}, **(body or {})))
    return r.status_code, r.get_json()


def _status(c):
    return _post(c, "/api/admin/jobs/status")[1]["job"]


def _wait(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


def _dry_body(folder):
    return {"instrument": "gc1", "processed_dir": str(folder)}


# ── the history dry run ─────────────────────────────────────────────────────

def test_dry_run_answers_202_at_once_and_the_job_carries_the_summary(client, monkeypatch):
    c, folder, db = client
    import jobs.import_history as ih
    gate = Gate()
    monkeypatch.setattr(ih, "import_history", gate)

    t0 = time.monotonic()
    code, body = _post(c, "/api/admin/import-history/dry-run", _dry_body(folder))
    elapsed = time.monotonic() - t0
    assert code == 202, body
    assert elapsed < FAST, elapsed
    job = body["job"]
    assert job["kind"] == "import-history-dry-run"
    assert job["state"] == "running"
    assert job["params"]["processed_dir"] == str(folder)
    assert "summary" not in body              # the summary comes with the finished job

    assert gate.started.wait(5)
    _args, kwargs = gate.calls[0]
    assert kwargs["dry_run"] is True           # never the real import

    # polled through the existing status route, with progress while it runs
    assert _wait(lambda: (_status(c)["progress"] or {}).get("done", 0) > 0)
    running = _status(c)
    assert running["state"] == "running" and running["progress"]["phase"] == "import"
    assert running["result"] is None

    gate.release.set()
    assert _wait(lambda: _status(c)["state"] != "running")
    done = _status(c)
    assert done["state"] == "done", done
    assert done["result"] == {"summary": gate.summary}
    assert done["summary"] == gate.summary


def test_dry_run_request_errors_are_answered_before_any_job(client, monkeypatch):
    c, folder, _db = client
    import jobs.import_history as ih
    gate = Gate()
    monkeypatch.setattr(ih, "import_history", gate)
    assert _post(c, "/api/admin/import-history/dry-run",
                 {"instrument": "gc9", "processed_dir": str(folder)})[0] == 404
    assert _post(c, "/api/admin/import-history/dry-run",
                 {"instrument": "gc1", "processed_dir": str(folder / "nope")})[0] == 400
    assert _post(c, "/api/admin/import-history/dry-run",
                 {"instrument": "gc1", "processed_dir": "relative/x"})[0] == 400
    assert _post(c, "/api/admin/import-history/dry-run",
                 dict(_dry_body(folder), aliases="not-a-list"))[0] == 400
    assert _post(c, "/api/admin/import-history/dry-run",
                 dict(_dry_body(folder), batch_size=0))[0] == 400
    assert c.post("/api/admin/import-history/dry-run", json={"password": "wrong"}
                  ).status_code == 403
    assert _status(c) is None                  # no job was ever started
    assert gate.calls == []


def test_a_dry_run_can_be_stopped(client, monkeypatch):
    c, folder, _db = client
    import jobs.import_history as ih
    gate = Gate()
    monkeypatch.setattr(ih, "import_history", gate)
    assert _post(c, "/api/admin/import-history/dry-run", _dry_body(folder))[0] == 202
    assert gate.started.wait(5)
    code, body = _post(c, "/api/admin/jobs/stop")
    assert code == 200, body
    assert _wait(lambda: _status(c)["state"] != "running")
    job = _status(c)
    assert job["state"] == "stopped", job
    assert job["result"] == {"summary": dict(gate.summary, partial=True)}


def test_a_dry_run_is_one_admin_job_at_a_time(client, monkeypatch):
    c, folder, _db = client
    import jobs.import_history as ih
    gate = Gate()
    monkeypatch.setattr(ih, "import_history", gate)
    assert _post(c, "/api/admin/import-history/dry-run", _dry_body(folder))[0] == 202
    try:
        code, body = _post(c, "/api/admin/import-history/dry-run", _dry_body(folder))
        assert code == 409 and body["job"]["kind"] == "import-history-dry-run"
        assert _post(c, "/api/admin/import-history/start",
                     dict(_dry_body(folder), confirm=True))[0] == 409
        assert _post(c, "/api/admin/load-folder",
                     {"instrument": "gc1", "folder": str(folder)})[0] == 409
    finally:
        gate.release.set()
    assert _wait(lambda: _status(c)["state"] == "done")
    # and a dry run waits its turn behind another job
    gate2 = Gate()
    monkeypatch.setattr(ih, "import_history", gate2)
    assert _post(c, "/api/admin/import-history/start",
                 dict(_dry_body(folder), confirm=True))[0] == 202
    try:
        assert _post(c, "/api/admin/import-history/dry-run", _dry_body(folder))[0] == 409
    finally:
        gate2.release.set()
    assert _wait(lambda: _status(c)["state"] == "done")


def test_a_failed_dry_run_reports_the_error(client, monkeypatch):
    c, folder, _db = client
    import jobs.import_history as ih

    def boom(*_a, **_k):
        raise OSError("the share went away")

    monkeypatch.setattr(ih, "import_history", boom)
    assert _post(c, "/api/admin/import-history/dry-run", _dry_body(folder))[0] == 202
    assert _wait(lambda: _status(c)["state"] != "running")
    job = _status(c)
    assert job["state"] == "failed" and "the share went away" in job["error"]
    assert job["result"] is None
