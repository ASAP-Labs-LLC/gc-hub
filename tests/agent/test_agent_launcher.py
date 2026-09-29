import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = REPO / "agent" / "launcher.pyw"


def load_launcher():
    loader = importlib.machinery.SourceFileLoader("gc_launcher", str(LAUNCHER))
    spec = importlib.util.spec_from_loader("gc_launcher", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


L = load_launcher()

CHILD = r'''
import json, os, sys
root = sys.argv[sys.argv.index("--root") + 1]
v = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
plan = json.load(open(os.path.join(root, "plan.json")))
rp = os.path.join(root, "runs.txt")
runs = open(rp).read().split() if os.path.exists(rp) else []
n = runs.count(v)
with open(rp, "a") as fh:
    fh.write(v + "\n")
act = plan[v][min(n, len(plan[v]) - 1)]
if isinstance(act, dict):
    open(os.path.join(root, "previous.txt"), "w").write(v + "\n")
    open(os.path.join(root, "current.txt"), "w").write(act["switch"] + "\n")
    sys.exit(3)
sys.exit(act)
'''


def _root(tmp_path, plan, current="v1"):
    root = tmp_path / "root"
    for v in plan:
        d = root / "versions" / v
        d.mkdir(parents=True)
        (d / "agent_main.py").write_text(CHILD, encoding="utf-8")
        (d / "PACKAGE_SHA256").write_text("sha-" + v + "\n")
    (root / "plan.json").write_text(json.dumps(plan))
    (root / "current.txt").write_text(current + "\n")
    (root / "agent.json").write_text(json.dumps({"python": sys.executable}))
    return root


def _runs(root):
    return (root / "runs.txt").read_text().split()


def _launcher(root, run_seconds=None):
    sleeps = []
    kw = {}
    if run_seconds is not None:
        # The launcher reads the clock once at each child's start and once at
        # its exit, so each child "runs" exactly run_seconds.
        ticks = iter(range(10 ** 6))
        kw["clock"] = lambda: next(ticks) * run_seconds
    lc = L.Launcher(str(root), sleep=sleeps.append, **kw)
    return lc, sleeps


def test_exit_0_quits(tmp_path):
    root = _root(tmp_path, {"v1": [0]})
    lc, sleeps = _launcher(root)
    assert lc.run() == 0
    assert _runs(root) == ["v1"] and sleeps == []


def test_exit_3_restarts_and_rereads_current(tmp_path):
    root = _root(tmp_path, {"v1": [{"switch": "v2"}], "v2": [0]})
    lc, sleeps = _launcher(root, run_seconds=0.25)
    assert lc.run() == 0
    assert _runs(root) == ["v1", "v2"]
    assert sleeps == [0.75]                               # 1 s floor between starts


def test_rapid_exit_3_loop_backs_off(tmp_path):
    root = _root(tmp_path, {"v1": [3, 3, 3, 3, 3, 3, 3, 3, 0]})
    lc, sleeps = _launcher(root, run_seconds=0.25)
    assert lc.run() == 0
    assert _runs(root) == ["v1"] * 9
    assert sleeps == [0.75] * 5 + [2, 4, 8]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_lock_name_is_resolved(tmp_path):
    (tmp_path / "a").mkdir()
    os.symlink(str(tmp_path / "a"), str(tmp_path / "link"))
    assert L._lock_name(str(tmp_path / "a")) == L._lock_name(str(tmp_path) + "/b/../a/")
    assert L._lock_name(str(tmp_path / "a")) == L._lock_name(str(tmp_path / "link"))


def test_crash_restarts_with_backoff(tmp_path):
    root = _root(tmp_path, {"v1": [1, 7, 0]})
    lc, sleeps = _launcher(root)
    assert lc.run() == 0
    assert _runs(root) == ["v1", "v1", "v1"]
    assert sleeps == [1, 2]
    assert not (root / "revert.json").exists()


def test_new_version_crashing_three_times_reverts(tmp_path):
    root = _root(tmp_path, {"v1": [{"switch": "v2"}, 0], "v2": [1]})
    lc, sleeps = _launcher(root)
    assert lc.run() == 0
    assert _runs(root) == ["v1", "v2", "v2", "v2", "v1"]
    assert (root / "current.txt").read_text().strip() == "v1"
    rv = json.loads((root / "revert.json").read_text())
    assert rv["from"] == "v2" and rv["to"] == "v1" and rv["package_sha256"] == "sha-v2"
    assert json.loads((root / "bad_packages.json").read_text()) == ["sha-v2"]


def test_old_version_crashes_never_revert(tmp_path):
    root = _root(tmp_path, {"v1": [1, 1, 1, 1, 0], "v0": [0]})
    (root / "previous.txt").write_text("v0\n")
    lc, sleeps = _launcher(root)
    assert lc.run() == 0
    assert _runs(root) == ["v1"] * 5
    assert not (root / "revert.json").exists()


def test_new_version_crash_after_30s_is_not_counted(tmp_path, monkeypatch):
    root = _root(tmp_path, {"v1": [{"switch": "v2"}, 0], "v2": [1, 1, 1, 0]})
    t = {"now": 0.0}

    def clock():
        t["now"] += 40.0          # each child "runs" 40 s (one read at start, one at exit)
        return t["now"]

    lc = L.Launcher(str(root), sleep=lambda s: None, clock=clock)
    assert lc.run() == 0
    assert _runs(root) == ["v1", "v2", "v2", "v2", "v2"]
    assert not (root / "revert.json").exists()


def test_missing_current_waits_then_uses_it(tmp_path):
    root = _root(tmp_path, {"v1": [0]})
    (root / "current.txt").unlink()
    calls = []

    def sleep(s):
        calls.append(s)
        (root / "current.txt").write_text("v1\n")

    lc = L.Launcher(str(root), sleep=sleep)
    assert lc.run() == 0
    assert calls and _runs(root) == ["v1"]


def test_single_instance_posix_lock(tmp_path):
    a = L.acquire_single_instance(str(tmp_path))
    assert a is not None
    assert L.acquire_single_instance(str(tmp_path)) is None
    a.release()
    b = L.acquire_single_instance(str(tmp_path))
    assert b is not None
    b.release()


def test_second_launcher_process_exits_at_once(tmp_path):
    root = _root(tmp_path, {"v1": [0]})
    held = L.acquire_single_instance(str(root))
    try:
        t0 = time.time()
        r = subprocess.run([sys.executable, str(LAUNCHER), "--root", str(root)],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0
        assert time.time() - t0 < 20
        assert not (root / "runs.txt").exists()      # never started a child
    finally:
        held.release()
    r = subprocess.run([sys.executable, str(LAUNCHER), "--root", str(root)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and _runs(root) == ["v1"]
