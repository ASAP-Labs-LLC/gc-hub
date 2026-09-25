"""Production-safe startup: how app.py boots under the ASAPSV1 updater.

The updater runs ``python app.py --no-tray`` with cwd = the immutable release
folder, ``GC_DATA_DIR`` = an *empty* folder and ``PORT`` = a scratch port, and
waits for ``GET /healthz`` → 200. These tests boot the real app the same way
(see ``tests/bootapp.py``).
"""
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

UNCONFIGURED = "not set or missing"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


@unittest.skipUnless(HAVE_DEPS, "needs flask + netCDF4")
class StartupTests(unittest.TestCase):
    def test_boots_with_empty_data_dir_and_writes_nothing_to_home(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                code, body = get(port, "/healthz")
                self.assertEqual(code, 200)
                self.assertEqual(body["status"], "ok")
                self.assertTrue((data / "app.log").is_file())
                self.assertTrue((data / "processed_cdf").is_dir())
                self.assertTrue((data / "exports").is_dir())
            leaked = [p.relative_to(home) for p in home.rglob("*") if p.is_file()]
            self.assertEqual(leaked, [], f"state leaked into HOME: {leaked}")

    def test_debugger_is_off_by_default(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                pass
            log = (Path(t) / "boot.log").read_text()
            # Flask's own banner. (Werkzeug's "Debugger is active!" line only
            # prints under the reloader, which is off — so it proves nothing.)
            self.assertIn("Debug mode: off", log)
            self.assertNotIn("Debugger is active", log)

    def test_unknown_flag_fails_fast(self):
        with tempfile.TemporaryDirectory() as t:
            env = dict(os.environ, GC_DATA_DIR=t, PORT=str(free_port()), HOME=t)
            r = subprocess.run([sys.executable, "app.py", "--no-such-flag"], cwd=ROOT,
                               env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--no-such-flag", r.stderr + r.stdout)

    def test_runpy_bootstrap_like_run_pyw_is_not_rejected(self):
        # run.pyw launches ``python -c "...runpy.run_path(app.py, run_name='__main__')"``,
        # so sys.argv is ['-c'] — the argv check must let that through.
        boot = ("import sys, runpy; sys.path.insert(0, %r); "
                "runpy.run_path(%r, run_name='__main__')" % (str(ROOT), str(ROOT / "app.py")))
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t), cmd=[sys.executable, "-c", boot]) as (port, proc, data, home):
                self.assertEqual(get(port, "/healthz")[0], 200)

    def test_empty_watch_dir_never_scans_the_release_folder(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            with booted(tmp) as (port, proc, data, home):
                self.assertTrue(
                    wait_for(lambda: UNCONFIGURED in _read(data / "app.log")),
                    "startup should log that the watch folder is not configured:\n"
                    + _read(data / "app.log")[-2000:])
                # Give the watcher loop (5 s poll) a full cycle to misbehave.
                wait_for(lambda: "[WATCHER] Scanning" in _read(tmp / "boot.log"), timeout=7)

                # Everything that goes through the Looker refuses cleanly.
                for path, body in (("/api/scan", {}), ("/api/rebuild-db", {}),
                                   ("/api/reprocess", {"samples": ["2025-0001"]})):
                    code, resp = post(port, path, body)
                    self.assertEqual(code, 409, f"{path}: {code} {resp}")
                    self.assertIn("Settings", resp["error"], path)
                code, _ = post(port, "/api/stop-scan", {})
                self.assertEqual(code, 200)
            self.assertNotIn("[WATCHER] Scanning", _read(tmp / "boot.log"))

    def test_saving_a_watch_dir_starts_the_watcher_without_restart(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            watch = tmp / "instrument_share"
            watch.mkdir()
            with booted(tmp) as (port, proc, data, home):
                self.assertTrue(wait_for(lambda: UNCONFIGURED in _read(data / "app.log")))
                code, conf = get(port, "/api/settings")
                self.assertEqual(code, 200)
                conf["watch_dir"] = str(watch)
                code, saved = post(port, "/api/settings", conf, timeout=30)
                self.assertEqual(code, 200, saved)
                self.assertEqual(saved["watch_dir"], str(watch))
                self.assertTrue(
                    wait_for(lambda: f"Scanning {watch}" in _read(tmp / "boot.log"), timeout=20),
                    "watcher did not start on the saved folder:\n" + _read(tmp / "boot.log")[-3000:])
                code, _ = post(port, "/api/scan", {})
                self.assertEqual(code, 200)


if __name__ == "__main__":
    unittest.main()
