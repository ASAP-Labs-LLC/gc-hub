"""VERSION file contract and the updater's /healthz health check.

See docs/superpowers/specs/2026-09-25-phase1-deploy-infrastructure-design.md
("Contract the updater imposes", "3. Versioning and health"). The updater
polls GET /healthz on a scratch port before switching traffic to a release;
it must answer 200 with {"status": "ok", "version": <tag>} with no auth and
no outbound calls, and must not itself count as activity. Neither may an open
tab's background polling (notifications, restart-wait, scan/reprocess
progress) — only a real user route may.
"""
import contextlib
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

# Endpoints app.js hits on a timer, never from a click: loadNotifications()
# polls every 30s for the life of a tab, and _waitForServerAndReload /
# startScanStatusPolling / _pollReprocessStatus poll every 1-2s while a
# restart/scan/reprocess is in flight. api_qbench_upload_status is the
# one-shot "reconnect to an in-progress upload" check init() makes on every
# page load, alongside other init calls — but it (like the rest) is the app
# checking on itself, not a person doing something, so it is excluded too.
POLLING_ENDPOINTS = (
    "/api/notifications",
    "/api/server-status",
    "/api/scan/status",
    "/api/reprocess/status",
    "/api/qbench-upload-status",
)


@contextlib.contextmanager
def _temp_version(tag: str):
    """Write ``tag`` to ROOT/VERSION for the duration of the block, unless a
    real VERSION file already exists there (never clobber one)."""
    path = ROOT / "VERSION"
    if path.exists():
        yield None
        return
    path.write_text(tag + "\n", encoding="utf-8")
    try:
        yield tag
    finally:
        path.unlink()


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
        self.assertIsInstance(body["pid"], int)
        self.assertIsInstance(body["active_sessions"], int)

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

    def test_version_tag_is_reported_and_badged_on_both_pages(self):
        # "dev"=="dev" proves nothing about whether the tag is actually
        # plumbed through; use a real, distinctive tag.
        with _temp_version("v9.9.9") as tag:
            if tag is None:
                self.skipTest("a real VERSION file already exists at ROOT")
            with tempfile.TemporaryDirectory() as t:
                with booted(Path(t)) as (port, proc, data, home):
                    code, body = get(port, "/healthz")
                    self.assertEqual(body["version"], "v9.9.9")
                    for path in ("/", "/calibration"):
                        html = urllib.request.urlopen(
                            f"http://127.0.0.1:{port}{path}", timeout=5).read().decode()
                        self.assertIn("v9.9.9", html, path)


@unittest.skipUnless(HAVE_DEPS, "needs flask + netCDF4")
class BadgeAssetTests(unittest.TestCase):
    def test_badge_css_positions_the_badge(self):
        text = (ROOT / "static" / "css" / "badge.css").read_text(encoding="utf-8")
        self.assertIn("#app-version", text)
        self.assertIn("position: fixed", text)

    def test_both_pages_link_the_badge_stylesheet(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                for path in ("/", "/calibration"):
                    html = urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{path}", timeout=5).read().decode()
                    self.assertIn("css/badge.css", html, path)


@unittest.skipUnless(HAVE_DEPS, "needs flask + netCDF4")
class ActivitySemanticsTests(unittest.TestCase):
    """Mirrors COA's rule: a session doing something is activity; a machine
    (or a tab's background timer) checking on the app is not."""

    def test_app_initiated_polling_does_not_count_as_activity(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                time.sleep(1.2)
                for path in POLLING_ENDPOINTS:
                    code, _ = get(port, path)
                    self.assertEqual(code, 200, path)
                _, body = get(port, "/healthz")
        self.assertGreaterEqual(body["idle_seconds"], 1.1)
        self.assertEqual(body["active_sessions"], 0)

    def test_a_real_user_route_counts_as_activity(self):
        with tempfile.TemporaryDirectory() as t:
            with booted(Path(t)) as (port, proc, data, home):
                time.sleep(1.2)
                code, _ = get(port, "/api/settings")
                self.assertEqual(code, 200)
                _, body = get(port, "/healthz")
        self.assertLess(body["idle_seconds"], 1)
        self.assertEqual(body["active_sessions"], 1)


if __name__ == "__main__":
    unittest.main()
