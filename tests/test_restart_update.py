"""Restart installs a staged update (spec §4-§6).

The ASAPSV1 updater stages each new release into ``<data>/staged.json``
(``updater.write_staged``). Restart with a newer healthy release staged
writes ``switch-requested``; the updater claims it by renaming it to
``switch-accepted`` (then kills us and starts the new release) or
``switch-refused``. Under the updater the app only ever *exits* on restart —
a replacement it spawned would race the updater for the port.

Unit tests drive ``restart_policy`` directly (stdlib only); route tests boot
the real app.py in a subprocess (never ``import app``).
"""
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootapp import ROOT, booted, get, post, wait_for  # noqa: E402

sys.path.insert(0, str(ROOT))

COA = ROOT.parent / "coa-reviewer"

try:
    import flask, netCDF4  # noqa: F401,E401
    HAVE_DEPS = True
except Exception:
    HAVE_DEPS = False


def _staged(data: Path, tag: str, healthy: bool = True) -> None:
    """``staged.json`` exactly as coa-reviewer's updater.write_staged writes it."""
    data.mkdir(parents=True, exist_ok=True)
    (data / "staged.json").write_text(json.dumps({
        "tag": tag,
        "staged_at": "2026-09-25T03:00:00+00:00",
        "healthy": healthy,
        "notes": "health check passed",
    }, indent=2), encoding="utf-8")


def _claim(data: Path, dest: str, extra: dict) -> None:
    """Do what updater._claim_marker does: rename the marker, then fill in
    the outcome (the original request's tag/by/at plus the verdict)."""
    marker = data / "switch-requested"
    doc = json.loads(marker.read_text(encoding="utf-8"))
    os.replace(marker, data / dest)
    (data / dest).write_text(json.dumps({"tag": doc["tag"], "by": doc["by"],
                                         "at": doc["at"], **extra}), encoding="utf-8")


class SharedModulesTests(unittest.TestCase):
    """The updater imports supervisor.py from whichever app it finds first,
    so every app must ship the identical file."""
    @unittest.skipUnless((COA / "supervisor.py").is_file(), "coa-reviewer checkout not beside gc-hub")
    def test_supervisor_identical_to_coa(self):
        for name in ("supervisor.py", "restart_update.py"):
            ours = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            theirs = hashlib.sha256((COA / name).read_bytes()).hexdigest()
            self.assertEqual(ours, theirs, f"{name} drifted from coa-reviewer")


class RestartDecisionTests(unittest.TestCase):
    def test_supervised_restart_exits_without_spawning(self):
        import restart_policy
        self.assertEqual(restart_policy.restart_mode(env={"GC_DATA_DIR": "C:/x"}), "exit")
        self.assertEqual(restart_policy.restart_mode(env={"GC_DATA_DIR": "  "}), "respawn")
        self.assertEqual(restart_policy.restart_mode(env={}), "respawn")

    def test_respawn_blocked_while_switching(self):
        import restart_policy
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(restart_policy.may_respawn(Path(t)))
            Path(t, "switching").write_text("")
            self.assertFalse(restart_policy.may_respawn(Path(t)))
            Path(t, "switching").unlink()
            Path(t, "switch-accepted").write_text("{}")
            self.assertFalse(restart_policy.may_respawn(Path(t)))

    def test_respawn_blocked_by_a_request_the_updater_could_still_take(self):
        import restart_policy
        with tempfile.TemporaryDirectory() as t:
            marker = Path(t, "switch-requested")
            marker.write_text(json.dumps({"tag": "v2.0.0", "by": "x", "at": time.time()}))
            self.assertFalse(restart_policy.may_respawn(Path(t)))
            # Older than the updater would ever honour: not a reason to stay down.
            marker.write_text(json.dumps({"tag": "v2.0.0", "by": "x", "at": time.time() - 600}))
            self.assertTrue(restart_policy.may_respawn(Path(t)))
            marker.write_text("garbage")
            self.assertTrue(restart_policy.may_respawn(Path(t)))

    def test_decide(self):
        import restart_policy
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self.assertEqual(restart_policy.decide(None, "v1.0.0"), ("restart", None))
            self.assertEqual(restart_policy.decide(d, "v1.0.0"), ("restart", None))
            _staged(d, "v1.1.0")
            self.assertEqual(restart_policy.decide(d, "v1.0.0"), ("switch", "v1.1.0"))
            self.assertEqual(restart_policy.decide(d, "v1.1.0"), ("restart", None))
            self.assertEqual(restart_policy.decide(d, "dev"), ("restart", None))
            _staged(d, "v1.1.0", healthy=False)
            self.assertEqual(restart_policy.decide(d, "v1.0.0"), ("restart", None))


class AwaitSwitchTests(unittest.TestCase):
    """``restart_policy.await_switch``: the watcher after a switch request.
    ``sleep`` is where the fake updater acts, as in COA's tests."""

    def setUp(self):
        import restart_policy
        import restart_update
        self.rp, self.ru = restart_policy, restart_update
        self._t = tempfile.TemporaryDirectory()
        self.data = Path(self._t.name)
        self.at = time.time()
        self.assertTrue(self.ru.write_switch_request(self.data, "v2.0.0", by="1.2.3.4", now=self.at))
        self.sleeps = 0

    def tearDown(self):
        self._t.cleanup()

    def _run(self, on_sleep=None, pickup=5.0, accepted_wait=4.0):
        self.taken = []

        def sleep(_s):
            self.sleeps += 1
            if on_sleep:
                on_sleep(self.sleeps)
        return self.rp.await_switch(self.data, "v2.0.0", self.at, pickup=pickup,
                                    accepted_wait=accepted_wait, poll=1.0, sleep=sleep,
                                    on_taken=lambda: self.taken.append(self.sleeps))

    def _no_switch_files(self):
        for n in ("switch-requested", "switch-accepted", "switch-refused"):
            self.assertFalse((self.data / n).exists(), n)

    def test_refused_falls_back_to_a_plain_restart(self):
        def updater(n):
            if n == 2:
                _claim(self.data, "switch-refused", {"why": "app is paused"})
        self.assertEqual(self._run(updater), "restart")
        self.assertEqual(self.sleeps, 2)
        self.assertEqual(self.taken, [])
        self._no_switch_files()

    def test_accepted_waits_to_be_killed_then_exits_without_respawn(self):
        def updater(n):
            if n == 1:
                _claim(self.data, "switch-accepted", {"accepted_at": time.time()})
        self.assertEqual(self._run(updater), "exit")
        self.assertEqual(self.sleeps, 1 + 4)   # pickup poll + accepted_wait polls
        # The results-CSV lock is taken before waiting to be killed.
        self.assertEqual(self.taken, [1])
        self._no_switch_files()

    def test_unclaimed_is_withdrawn_then_plain_restart(self):
        self.assertEqual(self._run(), "restart")
        self.assertEqual(self.taken, [])
        self.assertEqual(self.sleeps, 5)       # pickup / poll
        self._no_switch_files()

    def test_leftover_outcome_from_an_earlier_request_is_ignored(self):
        (self.data / "switch-accepted").write_text(json.dumps(
            {"tag": "v2.0.0", "by": "someone", "at": self.at - 3600}))
        self.assertEqual(self._run(), "restart")
        self.assertEqual(self.sleeps, 5)
        self._no_switch_files()

    def test_marker_taken_without_outcome_means_the_updater_has_it(self):
        def updater(n):
            if n == 1:
                (self.data / "switch-requested").unlink()
        self.assertEqual(self._run(updater), "exit")
        self.assertEqual(self.taken, [3])
        self._no_switch_files()


# ── Boot tests: the real app.py under the updater's environment ───────────

def _bootstrap(tag=None, min_uptime=0):
    """Launch app.py as __main__ through runpy, with
    two test-only overrides applied to the modules first:

    * ``version.APP_VERSION = tag`` — a checkout has no VERSION file, so it
      would otherwise report "dev", which is never older than anything;
    * ``restart_policy.MANUAL_RESTART_MIN_UPTIME_SECONDS = min_uptime`` — a
      fresh process refuses a manual restart for 5 minutes under the updater.
    """
    code = ("import sys, runpy, version, restart_policy; "
            + (f"version.APP_VERSION = {tag!r}; " if tag else "")
            + (f"restart_policy.MANUAL_RESTART_MIN_UPTIME_SECONDS = {min_uptime!r}; "
               if min_uptime is not None else "")
            + "sys.argv = ['app.py', '--no-tray']; "
            "runpy.run_path('app.py', run_name='__main__')")
    return [sys.executable, "-c", code]


def _bootstrap_as(tag: str):
    return _bootstrap(tag)


def _app_pids() -> set:
    out = subprocess.run(["pgrep", "-f", "app.py"], capture_output=True, text=True).stdout
    return {int(x) for x in out.split()}


def _assert_exits_without_replacement(tc, proc, port):
    """proc exits by itself, and nothing takes its port afterwards (a
    self-spawned replacement would bind within a few seconds)."""
    import supervisor
    try:
        proc.wait(timeout=20)
    except Exception:
        tc.fail("app did not exit after a restart under the updater")
    tc.assertEqual(proc.returncode, 0)
    deadline = time.time() + 10
    while time.time() < deadline:
        if supervisor.port_has_listener(port):
            try:
                code, body = get(port, "/healthz", timeout=2)
                os.kill(int(body["pid"]), signal.SIGTERM)
            except Exception:
                pass
            tc.fail("a replacement process took the port: the app respawned itself "
                    "under the updater")
        time.sleep(0.5)


@unittest.skipUnless(HAVE_DEPS, "needs flask + netCDF4")
class RestartRouteTests(unittest.TestCase):
    def test_dry_run_reports_the_decision_and_does_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            data = Path(t, "data")
            with booted(Path(t), cmd=_bootstrap_as("v1.0.0")) as (port, proc, data, home):
                code, body = post(port, "/api/restart", {"dry_run": True})
                self.assertEqual(code, 200)
                self.assertEqual((body["mode"], body["tag"]), ("restart", None))

                _staged(data, "v9.9.9")
                code, body = post(port, "/api/restart", {"dry_run": True})
                self.assertEqual((body["mode"], body["tag"]), ("switch", "v9.9.9"))
                self.assertEqual(body["pid"], proc.pid)

                _staged(data, "v9.9.9", healthy=False)
                _, body = post(port, "/api/restart", {"dry_run": True})
                self.assertEqual((body["mode"], body["tag"]), ("restart", None))

                _staged(data, "v0.9.0")
                _, body = post(port, "/api/restart", {"dry_run": True})
                self.assertEqual((body["mode"], body["tag"]), ("restart", None))

                time.sleep(1.5)
                self.assertIsNone(proc.poll(), "dry_run must not restart")
                self.assertFalse((data / "switch-requested").exists())

    def test_dev_build_never_switches(self):
        with tempfile.TemporaryDirectory() as t:
            _staged(Path(t, "data"), "v9.9.9")
            with booted(Path(t)) as (port, proc, data, home):
                _, body = post(port, "/api/restart", {"dry_run": True})
                self.assertEqual((body["mode"], body["tag"]), ("restart", None))
                # The Settings modal carries the Restart button and its helpers.
                html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
                self.assertIn('id="btn-restart"', html)
                self.assertIn("js/restart.js", html)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/static/js/restart.js",
                                            timeout=5) as r:
                    self.assertIn(b"restartLabel", r.read())

    def test_stale_switch_files_are_cleared_at_boot(self):
        with tempfile.TemporaryDirectory() as t:
            data = Path(t, "data")
            data.mkdir()
            for n in ("switch-requested", "switch-accepted", "switch-refused"):
                (data / n).write_text(json.dumps({"tag": "v1.0.0", "by": "x", "at": 1.0}))
            with booted(Path(t)) as (port, proc, data, home):
                self.assertTrue(wait_for(lambda: not any(
                    (data / n).exists()
                    for n in ("switch-requested", "switch-accepted", "switch-refused")),
                    timeout=10))

    def test_plain_restart_under_the_updater_exits_without_respawning(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t), cmd=_bootstrap()) as (port, proc, data, home):
                code, body = post(port, "/api/restart", {})
                self.assertEqual(code, 200)
                self.assertEqual(body["mode"], "restart")
                _assert_exits_without_replacement(self, proc, port)

    def test_switch_refused_by_the_updater_falls_back_to_exit(self):
        with tempfile.TemporaryDirectory() as t:
            _staged(Path(t, "data"), "v9.9.9")
            with booted(Path(t), cmd=_bootstrap_as("v1.0.0")) as (port, proc, data, home):
                code, body = post(port, "/api/restart", {})
                self.assertEqual((body["mode"], body["tag"]), ("switch", "v9.9.9"))
                marker = data / "switch-requested"
                self.assertTrue(wait_for(marker.exists, timeout=5))
                doc = json.loads(marker.read_text(encoding="utf-8"))
                self.assertEqual(doc["tag"], "v9.9.9")
                self.assertEqual(doc["by"], "127.0.0.1")
                self.assertLess(abs(time.time() - float(doc["at"])), 30)
                # A second click while one is pending changes nothing.
                _, again = post(port, "/api/restart", {})
                self.assertEqual((again["mode"], again["tag"]), ("switch", "v9.9.9"))

                _claim(data, "switch-refused", {"why": "app is paused",
                                                "refused_at": time.time()})
                _assert_exits_without_replacement(self, proc, port)
                self.assertFalse((data / "switch-refused").exists())

    def test_switch_accepted_waits_for_the_updater_to_stop_it(self):
        with tempfile.TemporaryDirectory() as t:
            _staged(Path(t, "data"), "v9.9.9")
            with booted(Path(t), cmd=_bootstrap_as("v1.0.0")) as (port, proc, data, home):
                post(port, "/api/restart", {})
                marker = data / "switch-requested"
                self.assertTrue(wait_for(marker.exists, timeout=5))
                _claim(data, "switch-accepted", {"accepted_at": time.time()})
                time.sleep(4)
                # The updater kills us; we must not exit (or respawn) first.
                self.assertIsNone(proc.poll())
                code, _ = get(port, "/healthz")
                self.assertEqual(code, 200)


    def test_fresh_process_refuses_a_manual_restart_under_the_updater(self):
        # Each exit spends one of the updater's 3-starts-per-15-min budget.
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                code, body = post(port, "/api/restart", {})
                self.assertEqual(code, 409)
                self.assertIn("just restarted", body["error"])
                self.assertRegex(body["error"], r"try again in \d+ s")
                code, body = post(port, "/api/restart", {"dry_run": True})
                self.assertEqual(code, 200)
                self.assertEqual(body["mode"], "restart")
                time.sleep(1.5)
                self.assertIsNone(proc.poll())

    def test_paused_updater_means_we_respawn_ourselves(self):
        import supervisor
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t), cmd=_bootstrap()) as (port, proc, data, home):
                (data / "paused").write_text("", encoding="utf-8")
                code, body = post(port, "/api/restart", {})
                self.assertEqual((code, body["mode"]), (200, "restart"))
                proc.wait(timeout=20)
                new_pid = None

                def replaced():
                    nonlocal new_pid
                    try:
                        c, b = get(port, "/healthz", timeout=1.0)
                    except Exception:
                        return False
                    if c == 200 and b["pid"] != proc.pid:
                        new_pid = b["pid"]
                        return True
                    return False
                try:
                    self.assertTrue(wait_for(replaced, timeout=60),
                                    "no replacement came up while the updater is paused")
                finally:
                    if new_pid is not None:
                        os.kill(new_pid, signal.SIGTERM)
                        self.assertTrue(supervisor.wait_until_free(port, timeout=15),
                                        "the replacement did not release the port")

    def test_second_instance_on_a_served_port_exits(self):
        from bootapp import child_env
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as t2:
            with booted(Path(t)) as (port, proc, data, home):
                env = child_env(Path(t2), port)
                with open(Path(t2, "dup.log"), "w", encoding="utf-8") as log:
                    dup = subprocess.Popen([sys.executable, "app.py", "--no-tray"], cwd=ROOT,
                                           env=env, stdout=log, stderr=subprocess.STDOUT)
                    try:
                        rc = dup.wait(timeout=40)
                    except subprocess.TimeoutExpired:
                        dup.kill()
                        dup.wait(10)
                        self.fail("a duplicate launch kept running on an occupied port")
                self.assertNotEqual(rc, 0)
                # Our guard refused it — not merely a failed bind, which on
                # Windows (SO_REUSEADDR) succeeds silently: the 2026-07-31
                # incident.
                dup_log = Path(t2, "dup.log").read_text(encoding="utf-8")
                self.assertIn("still in use", dup_log)
                # ...and before starting any background work (the watcher
                # would process files alongside the live instance).
                self.assertNotIn("Migrating CSV header", dup_log)
                code, body = get(port, "/healthz")
                self.assertEqual((code, body["pid"]), (200, proc.pid))


class PolicyDecisionTests(unittest.TestCase):
    def test_respawn_command_deployed_runs_via_the_junction(self):
        import restart_policy as rp
        args, cwd, flags = rp.respawn_command(
            "py.exe", ["app.py", "--no-tray"], deployed=True, app_dir=Path("C:/r/releases/v1"),
            cwd="C:/r/current", windows=True)
        self.assertEqual(args, ["py.exe", "app.py", "--no-tray"])
        self.assertEqual(cwd, "C:/r/current")
        self.assertEqual(flags, rp.CREATE_NO_WINDOW | rp.CREATE_NEW_PROCESS_GROUP)
        _, _, flags = rp.respawn_command("py", ["-c"], deployed=True, app_dir=Path("/a"),
                                         cwd="/c", windows=False)
        self.assertEqual(flags, 0)

    def test_respawn_command_legacy_uses_the_app_dir(self):
        import restart_policy as rp
        args, cwd, flags = rp.respawn_command(
            "py.exe", ["-c"], deployed=False, app_dir=Path("/share/gc"), cwd="/elsewhere",
            windows=True)
        self.assertEqual(args, ["py.exe", str(Path("/share/gc") / "app.py")])
        self.assertEqual(cwd, str(Path("/share/gc")))
        self.assertEqual(flags, rp.CREATE_NEW_PROCESS_GROUP)

    def test_respawn_decision_legacy(self):
        import restart_policy
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self.assertTrue(restart_policy.should_respawn(d, env={}))
            (d / "switching").write_text("")
            self.assertFalse(restart_policy.should_respawn(d, env={}))
            (d / "switching").unlink()
            (d / "switch-accepted").write_text("{}")
            self.assertFalse(restart_policy.should_respawn(d, env={}))

    def test_respawn_decision_under_the_updater(self):
        import restart_policy
        env = {"GC_DATA_DIR": "x"}
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self.assertFalse(restart_policy.should_respawn(d, env=env))
            (d / "paused").write_text("")
            self.assertTrue(restart_policy.should_respawn(d, env=env))
            (d / "switching").write_text("")
            self.assertFalse(restart_policy.should_respawn(d, env=env))
            (d / "switching").unlink()
            (d / "switch-requested").write_text(json.dumps({"at": time.time()}))
            self.assertFalse(restart_policy.should_respawn(d, env=env))

    def test_auto_restart_decision(self):
        import restart_policy as rp
        ok = dict(hour=rp.AUTO_RESTART_HOUR, today="2026-09-26", done_today="2026-09-25",
                  uptime_seconds=7200, idle=True)
        self.assertTrue(rp.should_auto_restart(**ok))
        self.assertFalse(rp.should_auto_restart(**{**ok, "hour": rp.AUTO_RESTART_HOUR + 1}))
        self.assertFalse(rp.should_auto_restart(**{**ok, "done_today": "2026-09-26"}))
        self.assertFalse(rp.should_auto_restart(**{**ok, "idle": False}))
        # The storm guard: a process that has just come back (its once-a-day
        # flag reset with it) must not restart again within the hour.
        self.assertEqual(rp.AUTO_RESTART_MIN_UPTIME_SECONDS, 3600)
        self.assertFalse(rp.should_auto_restart(**{**ok, "uptime_seconds": 700}))

    def test_manual_restart_uptime_guard(self):
        import restart_policy as rp
        self.assertEqual(rp.MANUAL_RESTART_MIN_UPTIME_SECONDS, 300)
        self.assertEqual(rp.manual_restart_wait("exit", 100), 200)
        self.assertEqual(rp.manual_restart_wait("exit", 299.5), 1)
        self.assertIsNone(rp.manual_restart_wait("exit", 300))
        self.assertIsNone(rp.manual_restart_wait("respawn", 5))


if __name__ == "__main__":
    unittest.main()
