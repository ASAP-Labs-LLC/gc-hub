"""Production-safe startup: how app.py boots under the ASAPSV1 updater.

The updater runs ``python app.py --no-tray`` with cwd = the immutable release
folder, ``GC_DATA_DIR`` = an *empty* folder and ``PORT`` = a scratch port, and
waits for ``GET /healthz`` → 200. These tests boot the real app the same way
(see ``tests/bootapp.py``). A few source-level (AST) checks cover behaviour a
subprocess can't observe.
"""
import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootapp import ROOT, booted, free_port, get, post, wait_for  # noqa: E402

try:
    import flask, netCDF4  # noqa: F401,E401
    HAVE_DEPS = True
except Exception:
    HAVE_DEPS = False



def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _git_status() -> str:
    return subprocess.run(["git", "status", "--porcelain", "--ignored"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout


def _run_with_flag(flag: str) -> subprocess.CompletedProcess:
    """Run ``app.py <flag>``; a flag that is (wrongly) accepted boots the
    server, which never exits — report that as a failure, not a hang."""
    with tempfile.TemporaryDirectory() as t:
        env = dict(os.environ, GC_DATA_DIR=t, PORT=str(free_port()), HOME=t, USERPROFILE=t)
        try:
            return subprocess.run([sys.executable, "app.py", flag], cwd=ROOT, env=env,
                                  capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"{flag!r} was accepted: the app booted instead of exiting")


@unittest.skipUnless(HAVE_DEPS, "needs flask + netCDF4")
class StartupTests(unittest.TestCase):
    def test_boots_with_empty_data_dir_and_writes_nothing_outside_it(self):
        before = _git_status()
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                code, body = get(port, "/healthz")
                self.assertEqual(code, 200)
                self.assertEqual(body["status"], "ok")
                self.assertTrue((data / "app.log").is_file())
                self.assertTrue((data / "processed_cdf").is_dir())
                self.assertTrue((data / "exports").is_dir())
            leaked = [str(p.relative_to(home)) for p in home.rglob("*")]
            self.assertEqual(leaked, [], f"state leaked into HOME: {leaked}")
        self.assertEqual(_git_status(), before, "a boot changed the release (repo) folder")

    def test_debugger_is_off_by_default(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                pass
            log = _read(Path(t) / "boot.log")
            # Flask's own banner. (Werkzeug's "Debugger is active!" line only
            # prints under the reloader, which is off — so it proves nothing.)
            self.assertIn("Debug mode: off", log)
            self.assertNotIn("Debugger is active", log)

    def test_unknown_flag_fails_fast(self):
        r = _run_with_flag("--no-such-flag")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--no-such-flag", r.stderr + r.stdout)

    def test_abbreviated_flags_are_rejected(self):
        # argparse would otherwise expand --d to --dev (debugger on!) and
        # --no to --no-tray: a typo must fail loudly, not guess.
        for flag in ("--d", "--no"):
            r = _run_with_flag(flag)
            self.assertNotEqual(r.returncode, 0, flag)
            self.assertIn(flag, r.stderr + r.stdout)

    def test_runpy_bootstrap_like_run_pyw_is_not_rejected(self):
        # run.pyw launches ``python -c "...runpy.run_path(app.py, run_name='__main__')"``,
        # so sys.argv is ['-c'] — the argv check must let that through.
        boot = ("import sys, runpy; sys.path.insert(0, %r); "
                "runpy.run_path(%r, run_name='__main__')" % (str(ROOT), str(ROOT / "app.py")))
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t), cmd=[sys.executable, "-c", boot]) as (port, proc, data, home):
                self.assertEqual(get(port, "/healthz")[0], 200)

    def test_empty_data_dir_starts_the_hub_and_never_scans(self):
        # Phase 2: there is no watcher. An empty data folder gets a store and
        # gc1 from the start-up; the scan routes are gone.
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            with booted(tmp) as (port, proc, data, home):
                self.assertTrue(wait_for(lambda: (data / "gc.db").is_file()
                                         and "hub: started" in _read(data / "app.log")),
                                _read(data / "app.log")[-2000:])
                for path in ("/api/scan", "/api/rebuild-db", "/api/stop-scan"):
                    self.assertEqual(post(port, path, {})[0], 404, path)
                for path, body in (("/api/reprocess", {"sample_ids": [1]}),
                                   ("/api/export-lims", {"sample_ids": [1]})):
                    code, resp = post(port, path, body)
                    self.assertEqual(code, 404, f"{path}: {code} {resp}")
            self.assertNotIn("[WATCHER]", _read(tmp / "boot.log"))


def _app_function(name: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"app.{name} not found")


def _calls(fn: ast.FunctionDef) -> set:
    return {n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


class SourceGuards(unittest.TestCase):
    """Behaviour a black-box boot can't observe, pinned at source level."""

    def test_distillation_curve_does_not_depend_on_the_looker(self):
        # Phase 2 T4: the curve uses the revision's own recorded blank, never
        # the Looker's "current blank" (and so never waits on the watch folder).
        fn = _app_function("api_sample_distillation_curve")
        self.assertNotIn("_get_looker", _calls(fn))
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        self.assertNotIn("_looker", names)


if __name__ == "__main__":
    unittest.main()
