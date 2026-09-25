"""TDD characterization + regression tests for the QBench upload integration.

These pin down the bug that took the live server down: the uploader resolved
``qbench_client`` from a hard-coded shared-drive path
``\\ASAPServer\\...\\COA Reviewer\\V2\\Past Data Manager\\API`` that no longer
exists, so every upload failed at the API-lookup step with
``ModuleNotFoundError: qbench_client`` before Chrome even opened.

The fix vendors ``qbench_client.py`` next to ``qbench_pdf_uploader.py`` inside
``webapp/`` and imports it locally (webapp/ is already on ``sys.path`` because
``run.pyw`` launches ``app.py`` from there).

Layer 1 — source/structure only. Runs under any Python with no third-party
deps installed (selenium / flask / netCDF4 are NOT needed to assert these
invariants), which is exactly the property that lets this suite catch the
"module not importable on the server" class of bug.
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent

# Markers that the code DEPENDS ON the dead network path (vs. merely mentioning
# the project name in prose). Any of these reappearing means the server is one
# missing-share away from the original outage again. We match the actual
# filesystem path tail and the sys.path-injection variable — not bare prose.
_DEAD_FRAGMENTS = (
    "_PM_API_DIR",                 # the sys.path-injection variable
    r"COA Reviewer\V2",            # the dead path tail (backslash form)
    r"Past Data Manager\API",      # the dead API dir that no longer exists
)


def _read(name: str) -> str:
    return (WEBAPP_DIR / name).read_text(encoding="utf-8", errors="ignore")


def _module_ast(name: str) -> ast.Module:
    return ast.parse(_read(name))


# qbench_client resolves credentials at *import* time. conftest points HOME
# and QBENCH_STORE_PATH at a throwaway dir, so the developer's real store is
# (rightly) invisible here: supply dummy credentials for the import tests.
_DUMMY_CREDS = {"QBENCH_CLIENT_ID": "test-id", "QBENCH_CLIENT_SECRET": "test-secret"}


@mock.patch.dict(os.environ, _DUMMY_CREDS)
class QBenchClientVendoringTests(unittest.TestCase):
    """The fix vendors qbench_client.py into the webapp folder."""

    def test_qbench_client_file_present_in_webapp(self) -> None:
        target = WEBAPP_DIR / "qbench_client.py"
        self.assertTrue(
            target.is_file(),
            f"qbench_client.py must live in webapp/ so the uploader does not "
            f"depend on a network-share path that no longer exists. "
            f"Expected at: {target}",
        )

    def test_qbench_client_importable_without_external_path_hack(self) -> None:
        """``import qbench_client`` resolves from webapp/ with sys.path scrubbed
        of any reference to the dead 'COA Reviewer\\V2\\Past Data Manager' path.
        The original bug was a ModuleNotFoundError naming ``qbench_client``
        itself; we tolerate missing transitive deps (jwt, requests) since those
        are declared in requirements.txt and installed on the production host."""
        sys.modules.pop("qbench_client", None)
        saved_path = list(sys.path)
        try:
            sys.path[:] = [
                p for p in sys.path
                if "COA Reviewer" not in p and "Past Data Manager" not in p
            ]
            if str(WEBAPP_DIR) not in sys.path:
                sys.path.insert(0, str(WEBAPP_DIR))
            try:
                importlib.import_module("qbench_client")
            except ModuleNotFoundError as exc:
                self.assertNotEqual(
                    exc.name, "qbench_client",
                    f"qbench_client still unreachable from webapp/: {exc}",
                )
        finally:
            sys.path[:] = saved_path
            sys.modules.pop("qbench_client", None)


@mock.patch.dict(os.environ, _DUMMY_CREDS)
class QBenchClientRuntimeTests(unittest.TestCase):
    """End-to-end proof of the fix: the exact line that used to blow up
    (`from qbench_client import QBenchAPIClient`) now resolves AND the class
    constructs. Skips if the production deps (jwt/requests) aren't installed,
    so a bare interpreter still runs the rest of the suite."""

    def test_client_imports_and_constructs(self) -> None:
        try:
            import jwt  # noqa: F401
            import requests  # noqa: F401
        except ModuleNotFoundError as exc:
            self.skipTest(f"transitive dep not installed: {exc.name}")

        if str(WEBAPP_DIR) not in sys.path:
            sys.path.insert(0, str(WEBAPP_DIR))
        from qbench_client import QBenchAPIClient

        client = QBenchAPIClient()  # __init__ must not require network access
        self.assertTrue(hasattr(client, "fetch_samples_by_lab_id"))


class NoDeadSharedDrivePathTests(unittest.TestCase):
    """Neither the uploader nor the Flask app may depend on the dead path."""

    def test_uploader_has_no_dead_shared_drive_path(self) -> None:
        src = _read("qbench_pdf_uploader.py")
        for frag in _DEAD_FRAGMENTS:
            self.assertNotIn(
                frag, src,
                f"qbench_pdf_uploader.py still references the dead shared-drive "
                f"path fragment {frag!r}; resolve qbench_client locally instead.",
            )

    def test_app_has_no_dead_shared_drive_path(self) -> None:
        src = _read("app.py")
        for frag in _DEAD_FRAGMENTS:
            self.assertNotIn(
                frag, src,
                f"app.py still injects the dead shared-drive path fragment "
                f"{frag!r} onto sys.path; the vendored qbench_client makes that "
                f"crutch unnecessary.",
            )


class DiagnosticScreenshotPathTests(unittest.TestCase):
    """The login-failure diagnostic screenshot must use a real temp dir.

    ``os.environ.get("TEMP", ".")`` falls back to ``"."`` (cwd) when TEMP is
    unset — on the ASAPSV1 updater, cwd is the immutable release folder, so a
    login-failure screenshot would try to write inside it (and TEMP isn't
    guaranteed to be set for a service-launched process either way).
    ``tempfile.gettempdir()`` is the correct cross-platform source of truth.
    """

    def test_uses_tempfile_gettempdir(self) -> None:
        src = _read("qbench_pdf_uploader.py")
        self.assertIn(
            "tempfile.gettempdir()", src,
            "the login-failure screenshot path must use tempfile.gettempdir() "
            "instead of os.environ.get(\"TEMP\", \".\")",
        )
        self.assertNotIn(
            'os.environ.get("TEMP"', src,
            "found the old TEMP-env-with-cwd-fallback pattern — replace it "
            "with tempfile.gettempdir()",
        )

    def test_imports_tempfile(self) -> None:
        tree = _module_ast("qbench_pdf_uploader.py")
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertIn("tempfile", imported)


class UploaderContractTests(unittest.TestCase):
    """Guards the uploader API that app.py depends on. These pass on the live
    (feature-rich) uploader and MUST stay green through consolidation — they
    prevent regressing to the simplified copy that dropped LoginFailedError and
    the post-upload verification helpers."""

    def setUp(self) -> None:
        self.tree = _module_ast("qbench_pdf_uploader.py")
        self.classes = {
            n.name for n in ast.walk(self.tree) if isinstance(n, ast.ClassDef)
        }
        self.funcs = {
            n.name for n in ast.walk(self.tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_defines_login_failed_error(self) -> None:
        # app.py does `except qbench_pdf_uploader.LoginFailedError as login_exc`
        self.assertIn(
            "LoginFailedError", self.classes,
            "qbench_pdf_uploader must define LoginFailedError; app.py catches it.",
        )

    def test_defines_required_callables(self) -> None:
        for fn in ("attach_pdf_to_sample", "cancel_upload", "prime_chromedriver"):
            self.assertIn(
                fn, self.funcs,
                f"qbench_pdf_uploader must define {fn}(); app.py calls it.",
            )

    def test_preserves_post_upload_verification(self) -> None:
        # The live uploader verifies the attachment landed even if the browser
        # session dies; the simplified copy dropped this. Keep it.
        self.assertIn(
            "_verify_attachment_fresh", self.funcs,
            "Post-upload verification (_verify_attachment_fresh) must be kept.",
        )

    def test_steps_per_sample_constant_present(self) -> None:
        src = _read("qbench_pdf_uploader.py")
        self.assertRegex(
            src, r"(?m)^STEPS_PER_SAMPLE\s*=",
            "STEPS_PER_SAMPLE must remain a module constant; app.py reads it.",
        )


if __name__ == "__main__":
    unittest.main()
