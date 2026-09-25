"""VERSION file contract and the updater's /healthz health check.

See docs/superpowers/specs/2026-09-25-phase1-deploy-infrastructure-design.md
("Contract the updater imposes", "3. Versioning and health"). The updater
polls GET /healthz on a scratch port before switching traffic to a release;
it must answer 200 with {"status": "ok", "version": <tag>} with no auth and
no outbound calls, and must not itself count as activity.
"""
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootapp import ROOT, booted, get  # noqa: E402

sys.path.insert(0, str(ROOT))
import version  # noqa: E402

try:
    import flask, netCDF4  # noqa: F401,E401
    HAVE_DEPS = True
except Exception:
    HAVE_DEPS = False


class VersionFileTests(unittest.TestCase):
    def test_reads_first_line(self):
        with tempfile.TemporaryDirectory() as t:
            Path(t, "VERSION").write_text("v1.2.3\nbuilt by CI\n")
            self.assertEqual(version.read_version(Path(t)), "v1.2.3")

    def test_missing_is_dev(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(version.read_version(Path(t)), "dev")


@unittest.skipUnless(HAVE_DEPS, "needs flask + netCDF4")
class HealthzTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                code, body = get(port, "/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["version"], version.read_version(ROOT))
        for k in ("pid", "active_sessions", "idle_seconds"):
            self.assertIn(k, body)
        self.assertIsInstance(body["idle_seconds"], (int, float))

    def test_healthz_does_not_count_as_activity(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                time.sleep(2.2)
                get(port, "/healthz")
                _, body = get(port, "/healthz")
        self.assertGreaterEqual(body["idle_seconds"], 2)
        self.assertEqual(body["active_sessions"], 0)

    def test_page_shows_version_badge(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
        self.assertIn('id="app-version"', html)
        self.assertIn(version.read_version(ROOT), html)


if __name__ == "__main__":
    unittest.main()
