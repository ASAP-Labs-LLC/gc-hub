"""T5 review fixes (C1, I1-I4, minors), from the reviewer's probes
(probe.py, filefallback.py).

* C1: ``/api/settings`` changes only whitelisted keys (operator keys freely,
  result-affecting ones with the admin password, paths never); comparison
  standards are admin-gated, come only from a sample, and live in the data
  folder's fixed standards directory.
* I1: gc1's corrections come only from the hub store; an unseeded gc1 is
  re-seeded by Maintenance, never read per job from the file.
* I2: the hub start retries with backoff and notifies after repeated failures.
* I3/I4 and the idle check: pinned at source level (a restart can't be
  observed safely in a subprocess test).
"""
from __future__ import annotations

import ast
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (ROOT, TESTS, TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

import cdf_fixtures as fx  # noqa: E402
import corrections  # noqa: E402
import distill  # noqa: E402
import hub  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402
from bootapp import booted, get, post, send, setup_admin, wait_for  # noqa: E402
from hub_boot import SIMDIS, Hub  # noqa: E402


# ── C1 over HTTP ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def booted_hub():
    import hub_boot
    tmp = Path(tempfile.mkdtemp(prefix="gc-t5-review-"))
    try:
        h = hub_boot.build_hub(tmp)
        conf = json.loads((h.data / "settings.json").read_text())
        conf["comparison_defaults_dir"] = str(h.data)          # a stray value on disk
        conf["export_folder"] = str(tmp / "elsewhere")
        (h.data / "settings.json").write_text(json.dumps(conf))
        with booted(tmp) as (port, _proc, data, _home):
            pw = setup_admin(port, data)
            yield port, h, pw
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _disk(h):
    return json.loads((h.data / "settings.json").read_text())


def test_settings_paths_and_result_keys_are_not_changeable(booted_hub):
    port, h, pw = booted_hub
    before = _disk(h)
    for key, value in (("blank_max_intensity_pa", "1e12"),
                       ("comparison_defaults_dir", str(h.data)),
                       ("export_folder", "/tmp/x"),
                       ("analysis_report_logo", str(h.data / "admin-setup-code.txt")),
                       ("processed_cdf_dir", "/tmp/y"),
                       ("correction_factors_json", "/tmp/c.json"),
                       ("calibration_cdf", "/tmp/cal.CDF"),
                       ("no_such_key", "1")):
        for body in ({key: value}, {key: value, "password": pw}):
            code, resp = post(port, "/api/settings", body)
            assert code == 400 and key in resp["error"], (key, code, resp)
    assert _disk(h) == before


def test_settings_operator_keys_save_freely_and_result_keys_need_the_admin(booted_hub):
    port, h, pw = booted_hub
    rules = json.dumps([{"name": "Early", "above": True, "threshold": 5000, "t_start": 0.1,
                         "t_end": 0.6, "color": "#ff0000", "enabled": True}])
    code, resp = post(port, "/api/settings", {"sample_flag_rules": rules, "series_colors": "#abc"})
    assert code == 200, resp
    assert _disk(h)["sample_flag_rules"] == rules and _disk(h)["series_colors"] == "#abc"
    # Best Fit is an export column: admin only
    code, resp = post(port, "/api/settings", {"bestfit_threshold": "0.5"})
    assert code == 403, resp
    assert _disk(h).get("bestfit_threshold") != "0.5"
    code, resp = post(port, "/api/settings", {"bestfit_threshold": "0.5", "password": "wrong"})
    assert code == 403
    code, resp = post(port, "/api/settings", {"bestfit_threshold": "0.5", "password": pw})
    assert code == 200, resp
    assert _disk(h)["bestfit_threshold"] == "0.5"
    assert "password" not in _disk(h)
    # echoing a GET back unchanged is always fine
    _, conf = get(port, "/api/settings")
    assert post(port, "/api/settings", conf)[0] == 200


def test_standards_dir_and_export_folder_are_fixed_in_the_data_folder(booted_hub):
    port, h, _pw = booted_hub
    _, conf = get(port, "/api/settings")
    assert Path(conf["comparison_defaults_dir"]).resolve() == \
        (h.data / "gc_comparison_standards").resolve()
    assert Path(conf["export_folder"]).resolve() == (h.data / "exports").resolve()


def test_comparison_standards_are_admin_gated_and_come_from_a_sample(booted_hub):
    port, h, pw = booted_hub
    std = h.data / "gc_comparison_standards"
    victim = h.data / "victim.CDF"
    victim.write_bytes(b"CDF\x01xx")
    # add: a sample only, with the password
    code, _ = post(port, "/api/comparison-standard", {"sample_id": h.ids["rerun"], "name": "R2"})
    assert code == 403
    code, resp = post(port, "/api/comparison-standard",
                      {"source_path": str(h.data / "admin-setup-code.txt"), "name": "leak",
                       "password": pw})
    assert code == 400, resp
    assert not list(h.data.rglob("leak.CDF"))
    code, resp = post(port, "/api/comparison-standard",
                      {"sample_id": h.ids["rerun"], "name": "R2", "password": pw})
    assert code == 200, resp
    assert (std / "R2.CDF").is_file()
    # rename / delete need the password and stay inside the standards folder
    assert post(port, "/api/comparison-standard/rename",
                {"old_name": "R2", "new_name": "R3"})[0] == 403
    code, resp = post(port, "/api/comparison-standard/rename",
                      {"old_name": "R2", "new_name": "R3", "password": pw})
    assert code == 200, resp
    js = {"Content-Type": "application/json"}
    assert send(port, "/api/comparison-standard/R3", b"{}", js, method="DELETE")[0] == 403
    code, _ = send(port, "/api/comparison-standard/victim",
                   json.dumps({"password": pw}).encode(), js, method="DELETE")
    assert code == 404 and victim.exists()
    code, _ = send(port, "/api/comparison-standard/R3",
                   json.dumps({"password": pw}).encode(), js, method="DELETE")
    assert code == 200 and not (std / "R3.CDF").exists()


@pytest.mark.parametrize("method,path", [("POST", "/api/browse"),
                                         ("GET", "/api/open-folder?path=/tmp")])
def test_server_side_desktop_actions_are_gone(booted_hub, method, path):
    port, _h, _pw = booted_hub
    code = get(port, path)[0] if method == "GET" else post(port, path, {"type": "dir"})[0]
    assert code == 404


# ── I1: corrections only from the store; Maintenance re-seeds ───────────────

def test_unseeded_gc1_waits_for_the_seed_never_the_file(tmp_path):
    distill._CAL_CACHE.clear()
    h = Hub(tmp_path)
    real = Path(h.conf["correction_factors_json"])
    saved = real.read_text()
    real.unlink()                                   # the share is offline at first start
    notes = []
    rt = hub.start(h.conf, data_dir=h.data, notifier=lambda lvl, msg: notes.append((lvl, msg)),
                   conf_fn=lambda: h.conf, maintenance=False)
    try:
        assert store.corrections.read("gc1", db=h.db) is None
        assert [lvl for lvl, _ in notes] == ["error"]
        assert "retries" in notes[0][1]
        real.write_text(saved)                      # the share is back
        store.instruments.upsert({"id": "gc1", "live_since": datetime(2020, 1, 1)}, db=h.db)
        cdf = fx.sample_cdf(h.src / "p.CDF", name="40304",
                            injected=datetime(2026, 9, 25, 14, 23, 0), method_name=SIMDIS)
        sid = pipeline.submit("gc1", cdf, conf=h.conf, data_dir=h.data, db=h.db).sample_id
        rt.worker.wake()
        assert wait_for(lambda: store.samples.get(sid, db=h.db)["status"] != "received", timeout=30)
        assert store.samples.get(sid, db=h.db)["status"] == "pending_corrections"

        m = hub.Maintenance(h.db, h.data, notifier=lambda lvl, msg: notes.append((lvl, msg)),
                            conf_fn=lambda: h.conf)
        now = datetime(2026, 9, 28, 0, 30)          # before the backup hour: seed only
        assert m.run_once(now=now)["seeded"] is True
        rec = store.corrections.read("gc1", db=h.db)
        assert rec is not None and rec["updated_by"] == "startup"
        assert notes[-1][0] == "info"
        rt.worker.wake()
        assert wait_for(lambda: store.samples.get(sid, db=h.db)["status"] == "final", timeout=30)
        used = json.loads(store.get_revision(sid, db=h.db)["corrections_used"])
        assert used["source"] == "hub"
        assert m.run_once(now=now + timedelta(minutes=30))["seeded"] is None   # nothing to do
    finally:
        rt.stop()


def test_maintenance_retries_the_seed_every_ten_minutes_quietly(tmp_path):
    h = Hub(tmp_path)
    store.migrate(h.db)
    import instruments
    instruments.bootstrap_gc1(h.conf, db=h.db)
    conf = dict(h.conf, correction_factors_json=str(tmp_path / "missing.json"))
    notes = []
    m = hub.Maintenance(h.db, h.data, notifier=lambda lvl, msg: notes.append((lvl, msg)),
                        conf_fn=lambda: conf)
    t0 = datetime(2026, 9, 28, 0, 5)
    assert m.run_once(now=t0)["seeded"] is False
    assert m.run_once(now=t0 + timedelta(minutes=5))["seeded"] is None      # not due yet
    assert m.run_once(now=t0 + timedelta(minutes=11))["seeded"] is False
    assert notes == []                               # hub.start already said so once
    conf["correction_factors_json"] = h.conf["correction_factors_json"]
    assert m.run_once(now=t0 + timedelta(minutes=22))["seeded"] is True
    assert store.corrections.read("gc1", db=h.db) is not None


def test_the_hub_worker_never_reads_the_file(tmp_path):
    h = Hub(tmp_path)
    rt = hub.start(dict(h.conf, correction_factors_json=""), data_dir=h.data, notifier=None,
                   conf_fn=lambda: h.conf, maintenance=False)
    try:
        # gc1 unseeded (no path at start); the file is readable per job, but ignored
        with pytest.raises(corrections.CorrectionsUnavailable):
            rt.worker._provider(h.conf).get({"id": "gc1", "name": "GC-1"})
        assert isinstance(rt.worker._provider(h.conf), corrections.StoreProvider)
    finally:
        rt.stop()


# ── I2: start with retry ────────────────────────────────────────────────────

class _Flaky:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError(f"database is locked ({self.calls})")
        return "runtime"


def test_start_retries_with_backoff_and_notifies_after_repeated_failures():
    sleeps, notes = [], []
    flaky = _Flaky(2)
    assert hub.start_with_retry(flaky, sleep=sleeps.append,
                                notifier=lambda lvl, msg: notes.append((lvl, msg))) == "runtime"
    assert sleeps == [5, 10] and notes == []        # two failures: quiet
    sleeps, notes = [], []
    flaky = _Flaky(8)
    assert hub.start_with_retry(flaky, sleep=sleeps.append,
                                notifier=lambda lvl, msg: notes.append((lvl, msg))) == "runtime"
    assert sleeps == [5, 10, 20, 40, 80, 160, 300, 300]
    assert [lvl for lvl, _ in notes] == ["error", "info"]          # once, then "started"
    assert "database is locked" in notes[0][1]


def test_start_with_retry_can_be_stopped():
    import threading
    stop = threading.Event()
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        stop.set()
    assert hub.start_with_retry(_Flaky(99), sleep=sleep, stop=stop) is None
    assert sleeps == [5]


# ── the idle check (auto-restart) ───────────────────────────────────────────

def test_background_work_blocks_idle(tmp_path):
    db = tmp_path / "gc.db"
    store.migrate(db)
    now = datetime(2026, 9, 28, 3, 0).astimezone()
    assert hub.background_busy(db, now=now) is False
    with store.connection(db) as conn, store.write_txn(conn):
        conn.execute("INSERT INTO jobs(kind, state, payload, attempts, created_at, not_before) "
                     "VALUES ('process', 'queued', '{}', 0, ?, ?)",
                     (store.now_iso(), store._ts(now + timedelta(minutes=5))))
    assert hub.background_busy(db, now=now) is False   # a retry later is not work now
    assert hub.background_busy(db, now=now + timedelta(minutes=6)) is True
    with store.connection(db) as conn, store.write_txn(conn):
        conn.execute("UPDATE jobs SET state='running'")
    assert hub.background_busy(db, now=now) is True


# ── source guards: restart, idle ────────────────────────────────────────────

def _fn(name):
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def _names(fn):
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | \
        {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}


def test_the_csv_exit_lock_machinery_is_gone():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    for gone in ("_hold_csv_lock_for_exit", "_release_csv_lock_for_exit",
                 "CSV_LOCK_EXIT_TIMEOUT_SECONDS", "_CSV_LOCK"):
        assert gone not in src, gone


def test_an_accepted_switch_stops_the_hub_first():
    fn = _fn("_await_switch_then_restart")
    kws = [k for n in ast.walk(fn) if isinstance(n, ast.Call) for k in n.keywords
           if k.arg == "on_taken"]
    assert kws and isinstance(kws[0].value, ast.Name) and kws[0].value.id == "_stop_hub"


def test_restart_stops_the_hub_only_to_exit_and_restarts_it_if_the_respawn_fails():
    fn = _fn("_do_restart")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    names = [c.func.id for c in calls]
    assert "_stop_hub" in names and "_restart_hub" in names
    # every _exit() is preceded (in source order) by a _stop_hub()
    lines = sorted((c.lineno, c.func.id) for c in calls if c.func.id in ("_stop_hub", "_exit"))
    seen_stop = False
    for _line, name in lines:
        if name == "_stop_hub":
            seen_stop = True
        elif name == "_exit":
            assert seen_stop
    # the respawn failure path restarts the hub
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    assert any("_restart_hub" in _names(h) for h in handlers)


def test_idle_counts_admin_jobs_and_due_worker_jobs():
    names = _names(_fn("_is_server_idle"))
    assert "background_busy" in names and "JOBS" in names
