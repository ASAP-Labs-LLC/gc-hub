"""One busy guard for Stop, Restart and the updater's idle signal (v3.1).

``hub_control.busy_reasons()`` lists the background work a Stop or a restart
would cut short (an admin job of any kind, a diagnostics bundle, a report ZIP,
the QBench upload thread, running Worker jobs); ``blocking_reasons()`` is what
even ``force`` may not interrupt: a purge.

* in process: the lists, Stop refusing a purge even with ``force``, and the
  cached variant never opening SQLite;
* booted: ``POST /api/restart`` answers 409 ``{error, busy, blocked}`` while a
  job runs (``force`` gets past it), never during a purge (not even with
  ``force``), its dry run reports both lists, and local ``/healthz`` says
  ``idle_seconds: 0`` while work runs (the contract keys unchanged).
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")

import hub_control  # noqa: E402
import store  # noqa: E402
from test_hub_control import PW, _post, env  # noqa: E402,F401  (env: the fixture)


class _Jobs:
    def __init__(self, kind):
        self.kind = kind

    def current(self):
        return {"state": "running", "kind": self.kind, "params": {"instrument": "gc1"}}


@pytest.mark.parametrize("kind", ["load-folder", "import-history", "import-history-dry-run"])
def test_any_admin_job_is_busy_but_overridable(env, monkeypatch, kind):
    import hub_admin
    monkeypatch.setattr(hub_admin, "JOBS", _Jobs(kind))
    assert any(kind in b for b in hub_control.busy_reasons())
    assert hub_control.blocking_reasons() == []


def test_a_purge_is_busy_and_blocking(env, monkeypatch):
    import hub_admin
    monkeypatch.setattr(hub_admin, "JOBS", _Jobs("purge"))
    assert any("purge of gc1" in b for b in hub_control.busy_reasons())
    assert hub_control.blocking_reasons() and "cannot be interrupted" in \
        hub_control.blocking_reasons()[0]


def test_stop_refuses_a_purge_even_with_force(env, monkeypatch):
    import hub_admin
    monkeypatch.setattr(hub_admin, "JOBS", _Jobs("purge"))
    code, body = _post(env["client"], "/api/admin/hub/stop", {"password": PW, "force": True})
    assert code == 409 and body["blocked"] and "purge" in body["error"]
    assert not env["shut"].is_set() and not (env["data"] / "paused").exists()


def test_the_cached_variant_never_opens_the_store(env, monkeypatch):
    store.jobs.enqueue("process", {"x": 1}, db=env["db"])
    with store.connection(env["db"]) as conn:
        conn.execute("UPDATE jobs SET state='running'")
    assert hub_control.refresh_cache()

    def boom(*_a, **_k):
        raise AssertionError("SQLite on the /healthz path")
    monkeypatch.setattr(hub_control, "_read_store", boom)
    assert any("processing job" in b for b in hub_control.busy_reasons(cached=True))


def test_the_export_pass_can_be_left_out(env):
    env["rt"].exporter.ticking = True
    assert any("export" in b for b in hub_control.busy_reasons())
    assert not any("export" in b for b in hub_control.busy_reasons(export_pass=False))


# ── booted ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def booted_hub():
    pytest.importorskip("netCDF4")
    import purge_helpers as ph
    from bootapp import booted, setup_admin, wait_for
    tmp = Path(tempfile.mkdtemp(prefix="gc-restart-guard-"))
    try:
        h = ph.build_two_gc_hub(tmp)
        with booted(tmp) as (port, proc, data, _home):
            pw = setup_admin(port, data)
            assert wait_for(lambda: not store.jobs.list(state="queued", db=h.db)
                            and not store.jobs.list(state="running", db=h.db), timeout=60)
            yield port, proc, h, pw
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _running_job(h, key="g2_final"):
    sid = h.ids[key]
    job = store.jobs.enqueue("process", {"sample_id": sid}, sample_id=sid, db=h.db)
    with store.connection(h.db) as conn:
        conn.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))
    return job


def test_restart_waits_for_work_unless_forced_and_healthz_is_not_idle(booted_hub):
    from bootapp import get, post, wait_for
    port, proc, h, _pw = booted_hub
    job = _running_job(h)
    try:
        code, body = post(port, "/api/restart", {})
        assert code == 409, body
        assert any("processing job" in b for b in body["busy"]) and body["blocked"] == []
        code, dry = post(port, "/api/restart", {"dry_run": True})
        assert code == 200 and dry["busy"] and dry["blocked"] == []
        assert wait_for(lambda: get(port, "/healthz")[1]["idle_seconds"] == 0, timeout=15)
        health = get(port, "/healthz")[1]
        assert {"status", "version", "pid", "active_sessions", "idle_seconds"} <= set(health)
        # force gets past the busy guard (the fresh process then refuses for its
        # own reason: it has just started, and each exit spends an updater start)
        code, body = post(port, "/api/restart", {"force": True})
        assert code == 409 and "just restarted" in body["error"], body
        assert proc.poll() is None
    finally:
        store.jobs.complete(job, db=h.db)
    assert wait_for(lambda: get(port, "/healthz")[1]["idle_seconds"] > 0, timeout=15)


def test_restart_is_never_allowed_during_a_purge(booted_hub):
    from bootapp import post, wait_for
    port, proc, h, pw = booted_hub
    job = _running_job(h, "final")        # a gc1 job: the purge waits on it
    try:
        code, body = post(port, "/api/admin/purge/start", {
            "password": pw, "instrument": "gc1", "scope": "all", "confirm_text": "PURGE GC-1"})
        assert code == 202, body
        for payload in ({}, {"force": True}):
            code, body = post(port, "/api/restart", payload)
            assert code == 409, body
            assert body["blocked"] and "purge" in body["error"], body
        code, dry = post(port, "/api/restart", {"dry_run": True})
        assert code == 200 and dry["blocked"]
        assert proc.poll() is None
    finally:
        store.jobs.complete(job, db=h.db)
    assert wait_for(lambda: post(port, "/api/admin/jobs/status", {"password": pw})[1]["job"]
                    ["state"] != "running", timeout=60)
