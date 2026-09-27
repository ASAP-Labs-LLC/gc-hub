"""QBench API credentials entered from the Settings modal.

See docs/superpowers/specs/2026-09-25-phase1-deploy-infrastructure-design.md
section 8. The app must boot with no credential store (the updater's health
check runs on a fresh box), so ``qbench_client`` resolves credentials lazily;
the UI then tests a submitted pair against the token endpoint and saves it to
the local store only when QBench accepts it. The secret is never returned,
logged, or echoed in an error.
"""
import ast
import contextlib
import http.server
import importlib
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootapp import ROOT, booted, get, post  # noqa: E402

sys.path.insert(0, str(ROOT))
import qbench_secrets  # noqa: E402

try:
    import flask, netCDF4, jwt, requests  # noqa: F401,E401
    HAVE_DEPS = True
except Exception:
    HAVE_DEPS = False

ROUTE = "/api/qbench-api-credentials"
GOOD_ID = "client-id-good-9f3e"
# Distinctive, so a leak anywhere is unambiguous.
GOOD_SECRET = "S3CR3T-good-zq8w7x6v-DO-NOT-LEAK"
BAD_SECRET = "S3CR3T-bad-kk4j3h2g-DO-NOT-LEAK"


# ─────────────────────────── qbench_secrets ────────────────────────────── #

class SaveDefaultTests(unittest.TestCase):
    def test_writes_and_preserves_profiles(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t, "sub", "qbench.json")
            p.parent.mkdir()
            p.write_text(json.dumps({
                "client_id": "old", "client_secret": "old", "other_key": 7,
                "profiles": {"legacy": {"client_id": "L", "client_secret": "LS"}}}))
            with mock.patch.dict(os.environ, {"QBENCH_STORE_PATH": str(p)}):
                qbench_secrets.save_default("new-id-1234", "new-secret")
                data = json.loads(p.read_text())
            self.assertEqual(data["client_id"], "new-id-1234")
            self.assertEqual(data["client_secret"], "new-secret")
            self.assertEqual(data["profiles"]["legacy"],
                             {"client_id": "L", "client_secret": "LS"})
            self.assertEqual(data["other_key"], 7)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(sorted(os.listdir(p.parent)), ["qbench.json"],
                             "no temp file left behind")

    def test_creates_missing_store_and_parent(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t, "new", "deeper", "qbench.json")
            with mock.patch.dict(os.environ, {"QBENCH_STORE_PATH": str(p)}):
                qbench_secrets.save_default("id-abcd", "s")
                self.assertTrue(p.is_file())
                self.assertEqual(qbench_secrets.get_client_id(), "id-abcd")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_strips_whitespace(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t, "qbench.json")
            with mock.patch.dict(os.environ, {"QBENCH_STORE_PATH": str(p)}):
                qbench_secrets.save_default("  id-1 \n", "\tsec ")
                data = json.loads(p.read_text())
            self.assertEqual((data["client_id"], data["client_secret"]), ("id-1", "sec"))

    def test_failed_write_leaves_existing_store_intact(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t, "qbench.json")
            original = json.dumps({"client_id": "keep", "client_secret": "keep"})
            p.write_text(original)
            with mock.patch.dict(os.environ, {"QBENCH_STORE_PATH": str(p)}), \
                 mock.patch.object(qbench_secrets.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    qbench_secrets.save_default("new", "new")
            self.assertEqual(p.read_text(), original)
            self.assertEqual(os.listdir(t), ["qbench.json"], "temp file cleaned up")

    def test_corrupt_store_is_backed_up_then_replaced(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t, "qbench.json")
            p.write_text("{not json")
            with mock.patch.dict(os.environ, {"QBENCH_STORE_PATH": str(p)}):
                qbench_secrets.save_default("id-x", "sec-x")
                self.assertEqual(qbench_secrets.get_client_secret(), "sec-x")
            backups = [n for n in os.listdir(t) if n.startswith("qbench.json.corrupt-")]
            self.assertEqual(len(backups), 1)
            self.assertEqual(Path(t, backups[0]).read_text(), "{not json")

    def test_rejects_blank(self):
        with self.assertRaises(ValueError):
            qbench_secrets.save_default("", "x")
        with self.assertRaises(ValueError):
            qbench_secrets.save_default("x", "  ")
        with self.assertRaises(ValueError):
            qbench_secrets.save_default(None, None)


class DescribeTests(unittest.TestCase):
    def test_never_returns_secret(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t, "qbench.json")
            with mock.patch.dict(os.environ, {"QBENCH_STORE_PATH": str(p)}):
                d = qbench_secrets.describe()
                self.assertIs(d["configured"], False)
                self.assertEqual(d["client_id_hint"], "")
                self.assertEqual(d["store_path"], str(p))
                qbench_secrets.save_default("id-abcd", "TOPSECRET")
                d = qbench_secrets.describe()
            self.assertIs(d["configured"], True)
            self.assertEqual(d["client_id_hint"], "…abcd")
            self.assertEqual(d["source"], "store")
            self.assertNotIn("TOPSECRET", json.dumps(d))

    def test_reports_environment_override(self):
        # An env pair wins over the store, so a UI save would not take effect;
        # describe() must say so.
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {
                "QBENCH_STORE_PATH": str(Path(t, "q.json")),
                "QBENCH_CLIENT_ID": "env-id-wxyz", "QBENCH_CLIENT_SECRET": "ENVSECRET"}):
            d = qbench_secrets.describe()
        self.assertIs(d["configured"], True)
        self.assertEqual(d["client_id_hint"], "…wxyz")
        self.assertEqual(d["source"], "environment")
        self.assertNotIn("ENVSECRET", json.dumps(d))

    def test_corrupt_store_reads_as_unconfigured(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t, "q.json")
            p.write_text("{nope")
            with mock.patch.dict(os.environ, {"QBENCH_STORE_PATH": str(p)}):
                self.assertIs(qbench_secrets.describe()["configured"], False)


# ─────────────────────────── qbench_client ─────────────────────────────── #

@unittest.skipUnless(HAVE_DEPS, "jwt/requests not installed")
class LazyClientTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"QBENCH_STORE_PATH": str(Path(self._tmp.name, "q.json"))})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("QBENCH_CLIENT_ID", None)
        os.environ.pop("QBENCH_CLIENT_SECRET", None)
        sys.modules.pop("qbench_client", None)
        self.addCleanup(sys.modules.pop, "qbench_client", None)

    def test_importing_client_does_not_need_credentials(self):
        mod = importlib.import_module("qbench_client")  # must not raise
        self.assertFalse(hasattr(mod, "CLIENT_ID"))
        self.assertFalse(hasattr(mod, "CLIENT_SECRET"))
        with self.assertRaises(qbench_secrets.QBenchSecretMissing):
            mod.QBenchAPIClient()

    def test_explicit_pair_needs_no_store(self):
        mod = importlib.import_module("qbench_client")
        c = mod.QBenchAPIClient(client_id="i", client_secret="s")
        self.assertEqual((c.client_id, c.client_secret), ("i", "s"))

    def test_resolved_at_construction_so_a_new_save_is_picked_up(self):
        mod = importlib.import_module("qbench_client")
        qbench_secrets.save_default("first-id", "first-secret")
        self.assertEqual(mod.QBenchAPIClient().client_id, "first-id")
        qbench_secrets.save_default("second-id", "second-secret")
        c = mod.QBenchAPIClient()
        self.assertEqual((c.client_id, c.client_secret), ("second-id", "second-secret"))

    def test_token_url_env_override(self):
        mod = importlib.import_module("qbench_client")
        with mock.patch.dict(os.environ, {"QBENCH_TOKEN_URL": "http://127.0.0.1:9/tok"}):
            c = mod.QBenchAPIClient(client_id="i", client_secret="s")
        self.assertEqual(c.token_url, "http://127.0.0.1:9/tok")
        self.assertTrue(mod.QBenchAPIClient(client_id="i", client_secret="s")
                        .token_url.startswith("https://asaplabs.qbench.net/"))


# ─────────────────────────── app.py (static) ───────────────────────────── #

class AdminGateShapeTests(unittest.TestCase):
    """The admin gate is shared and constant-time (spec: open item, not a
    redesign)."""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "app.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)

    def _func(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f"{name} not defined in app.py")

    def test_check_admin_uses_compare_digest(self):
        body = ast.unparse(self._func("_check_admin"))
        self.assertIn("compare_digest", body)

    def test_both_gated_routes_use_check_admin(self):
        for name in ("api_save_analysis_defaults", "api_qbench_api_credentials_post"):
            self.assertIn("_check_admin(", ast.unparse(self._func(name)), name)

    def test_no_inline_admin_comparison_left(self):
        self.assertNotIn('!= "admin"', self.src)
        self.assertNotIn('== "admin"', self.src)


# ─────────────────────────── routes (booted) ───────────────────────────── #

class _FakeTokenHandler(http.server.BaseHTTPRequestHandler):
    """Accepts a JWT-bearer assertion only if it was signed with GOOD_SECRET
    for GOOD_ID. On rejection it echoes the request body back, as a hostile
    or chatty server might, so an app that forwards the upstream text leaks
    the assertion."""

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        form = urllib.parse.parse_qs(raw.decode())
        assertion = (form.get("assertion") or [""])[0]
        import jwt as _jwt
        try:
            claims = _jwt.decode(assertion, GOOD_SECRET, algorithms=["HS256"])
            ok = claims.get("sub") == GOOD_ID
        except Exception:
            ok = False
        if ok:
            payload, code = {"access_token": "tok-123", "expires_in": 3600}, 200
        else:
            payload, code = {"error": "invalid_client", "echo": raw.decode()}, 401
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@contextlib.contextmanager
def fake_token_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeTokenHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/token"
    finally:
        srv.shutdown()
        srv.server_close()


def _logs(tmp: Path) -> str:
    text = ""
    for p in (tmp / "boot.log", tmp / "data" / "app.log"):
        if p.is_file():
            text += p.read_text(encoding="utf-8", errors="replace")
    return text


@unittest.skipUnless(HAVE_DEPS, "flask/netCDF4/jwt/requests not installed")
class CredentialRouteTests(unittest.TestCase):
    def assert_no_secret(self, *objs):
        for o in objs:
            s = json.dumps(o)
            self.assertNotIn(GOOD_SECRET, s)
            self.assertNotIn(BAD_SECRET, s)

    def test_flow_against_fake_token_server(self):
        with tempfile.TemporaryDirectory() as t, fake_token_server() as url:
            tmp = Path(t)
            with booted(tmp, extra_env={"QBENCH_TOKEN_URL": url}) as (port, _p, _data, home):
                store = home / "qbench.json"
                seen = []

                code, body = get(port, ROUTE)
                seen.append(body)
                self.assertEqual(code, 200)
                self.assertIs(body["configured"], False)
                self.assertEqual(body["store_path"], str(store))
                self.assertNotIn("client_secret", body)

                # Wrong admin password: 403, nothing written, no token call.
                code, body = post(port, ROUTE, {"client_id": GOOD_ID,
                                                "client_secret": GOOD_SECRET,
                                                "password": "nope"})
                seen.append(body)
                self.assertEqual(code, 403)
                self.assertFalse(store.exists())

                # Non-string password must not crash the constant-time compare.
                code, body = post(port, ROUTE, {"client_id": GOOD_ID,
                                                "client_secret": GOOD_SECRET,
                                                "password": 12345})
                seen.append(body)
                self.assertEqual(code, 403)

                # Blank fields: 400.
                code, body = post(port, ROUTE, {"client_id": " ", "client_secret": GOOD_SECRET,
                                                "password": "admin"})
                seen.append(body)
                self.assertEqual(code, 400)
                self.assertFalse(store.exists())

                # QBench rejects the pair: 400, generic message + status,
                # nothing written, upstream echo not forwarded.
                code, body = post(port, ROUTE, {"client_id": GOOD_ID,
                                                "client_secret": BAD_SECRET,
                                                "password": "admin"})
                seen.append(body)
                self.assertEqual(code, 400)
                self.assertIn("401", body["error"])
                self.assertNotIn("assertion", body["error"])
                self.assertNotIn("echo", json.dumps(body))
                self.assertFalse(store.exists())

                # Accepted: 200, store written, status reflects it.
                code, body = post(port, ROUTE, {"client_id": GOOD_ID,
                                                "client_secret": GOOD_SECRET,
                                                "password": "admin"})
                seen.append(body)
                self.assertEqual(code, 200, body)
                self.assertIs(body["configured"], True)
                self.assertEqual(body["client_id_hint"], "…" + GOOD_ID[-4:])
                data = json.loads(store.read_text())
                self.assertEqual((data["client_id"], data["client_secret"]),
                                 (GOOD_ID, GOOD_SECRET))

                code, body = get(port, ROUTE)
                seen.append(body)
                self.assertIs(body["configured"], True)

                # A later rejected attempt leaves the saved pair alone.
                code, body = post(port, ROUTE, {"client_id": "other-id",
                                                "client_secret": BAD_SECRET,
                                                "password": "admin"})
                seen.append(body)
                self.assertEqual(code, 400)
                self.assertEqual(json.loads(store.read_text())["client_id"], GOOD_ID)

                # The existing "Set as Default" gate behaves as before.
                code, body = post(port, "/api/save-analysis-defaults",
                                  {"password": "wrong", "params": {}})
                seen.append(body)
                self.assertEqual(code, 403)

                self.assert_no_secret(*seen)
            logs = _logs(tmp)
            self.assertIn("QBench API credentials updated", logs)
            self.assertNotIn(GOOD_SECRET, logs)
            self.assertNotIn(BAD_SECRET, logs)

    def test_unreachable_token_endpoint_writes_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            with booted(tmp, extra_env={"QBENCH_TOKEN_URL": "http://127.0.0.1:1/token"}) \
                    as (port, _p, _data, home):
                code, body = post(port, ROUTE, {"client_id": GOOD_ID,
                                                "client_secret": GOOD_SECRET,
                                                "password": "admin"})
                self.assertEqual(code, 400)
                self.assertIn("error", body)
                self.assert_no_secret(body)
                self.assertFalse((home / "qbench.json").exists())
            logs = _logs(tmp)
            self.assertNotIn(GOOD_SECRET, logs)


if __name__ == "__main__":
    unittest.main()
