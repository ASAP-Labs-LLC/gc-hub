"""scripts/package_release.sh — the one place a release zip is built.

The GitHub workflow calls the script, so testing it here tests what ships.
What the updater (``coa-reviewer/deploy/updater/updater.py``) needs from the
archive: one top-level folder (``unpack`` flattens it), ``app.py``,
``requirements.txt``, ``VERSION`` = the tag, ``templates/``, ``static/``, and a
``.sha256`` asset that ``parse_sha256_file`` accepts and that names the zip by
its bare filename.

State planting happens in a **temporary copy** of the repo, never in the
checkout itself, so a failing or interrupted test cannot leave planted files
behind. The one run against the real checkout only reads it.
"""
import ast
import hashlib
import importlib.util
import os
import sys
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = Path("scripts") / "package_release.sh"
COA_UPDATER = ROOT.parent / "coa-reviewer" / "deploy" / "updater" / "updater.py"

# Verbatim from updater.py's _SHA256_LINE, so the format is checked even when
# the coa-reviewer checkout is absent (e.g. on CI).
_SHA256_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$")

HAVE_TOOLS = bool(shutil.which("bash") and shutil.which("rsync") and shutil.which("zip"))

# State that must never ship, relative to the repo root. Directories end in /.
PLANTED = [
    "distill_results.csv",
    "distill_results.csv.bak",
    "results_2026.csv",
    "processed_cdf/x.CDF",
    "processed_cdf/.processed_index.json",
    "processed_cdf_old/y.cdf",
    "CDF/z.CDF",
    "exports/report.pdf",
    ".gc_server.pid",
    ".gc_server-5561.pid",
    "app.log",
    "app.log.1",
    ".sample_flags_cache.json",
    ".bestfit_cache.json",
    "settings.json.bak_20260901",
    "switch-requested",
    "switch-accepted",
    "switch-refused",
    "switching",
    "staged.json",
    "held-tags.json",
    "paused",
    "qbench.json",
    ".env",
    ".DS_Store",
    "__pycache__/app.cpython-312.pyc",
    "static/__pycache__/junk.pyc",
    ".pytest_cache/README.md",
    ".venv/bin/python",
    "venv/bin/python",
    ".git/HEAD",
    ".github/workflows/release.yml",
    "dist/old.zip",
    "node_modules/x/index.js",
    ".claude/settings.local.json",
    ".env.local",
    ".secret_key",
    "credentials.json",
    "client_secret_123.json",
    "service_account_gc.json",
    "server.pem",
    "server.key",
]
# A stale VERSION in the tree must be replaced by the tag, never shipped.
STALE_VERSION = "v9.9.9-stale"


def _local_modules(root: Path) -> set:
    """Top-level modules and packages (folders with ``__init__.py``) of *root*."""
    return ({p.stem for p in root.glob("*.py")}
            | {p.parent.name for p in root.glob("*/__init__.py")})


def _module_files(root: Path, name: str) -> list:
    """The source files of local module/package *name* under *root*."""
    if (root / name / "__init__.py").is_file():
        return sorted((root / name).rglob("*.py"))
    return [root / f"{name}.py"]


def _imports_of(path: Path, local: set) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        else:
            continue
        found.update(n.split(".")[0] for n in names if n.split(".")[0] in local)
    return found


def _closure(root: Path, entry_points) -> set:
    """Every file of *root* reachable by import from *entry_points*, as
    paths relative to *root* (the entry points included)."""
    local = _local_modules(root)
    todo = list(entry_points)
    seen = set()
    while todo:
        f = todo.pop()
        if f in seen or not f.is_file():
            continue
        seen.add(f)
        for m in _imports_of(f, local):
            todo.extend(_module_files(root, m))
    return {f.relative_to(root).as_posix() for f in seen}


def runtime_closure(root: Path) -> set:
    """Every local module/package reachable by import from the import roots:
    the hub (app.py), its CLI tools (tools/*.py, run on the server) and the
    agent the hub packages (agent/*.py[w], resolved within agent/)."""
    files = _closure(root, [root / "app.py"])
    tools = root / "tools"
    for tool in sorted(tools.glob("*.py")):
        files |= _closure(root, [tool])
        files.add(tool.relative_to(root).as_posix())
    agent = root / "agent"
    files |= {f"agent/{rel}" for rel in _closure(agent, sorted(agent.glob("*.py"))
                                                    + sorted(agent.glob("*.pyw")))}
    return files


def _copy_repo(dest: Path) -> None:
    # .git and .venv are big and irrelevant; fake ones are planted instead so
    # their exclusion is still exercised.
    shutil.copytree(ROOT, dest, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", ".venv", "venv", "__pycache__",
                                                  ".pytest_cache", "processed_cdf*",
                                                  "*.pid", "*.log", "VERSION", "dist"))


def _package(src: Path, tag: str, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(src / SCRIPT), tag, str(out)],
                          cwd=src, capture_output=True, text=True, timeout=120)


@unittest.skipUnless(HAVE_TOOLS, "needs bash, rsync, zip")
class PackageTests(unittest.TestCase):
    TAG = "v0.0.0-test"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.src = base / "repo"
        cls.out = base / "out"
        _copy_repo(cls.src)
        for rel in PLANTED:
            p = cls.src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("state\n")
        (cls.src / "VERSION").write_text(STALE_VERSION + "\n")
        cls.result = _package(cls.src, cls.TAG, cls.out)
        cls.name = f"gc-hub-{cls.TAG}"
        cls.zip_path = cls.out / f"{cls.name}.zip"
        cls.sha_path = cls.out / f"{cls.name}.zip.sha256"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.assertEqual(self.result.returncode, 0,
                         f"stdout:\n{self.result.stdout}\nstderr:\n{self.result.stderr}")

    def _names(self):
        with zipfile.ZipFile(self.zip_path) as z:
            return z.namelist()

    def _rel(self):
        return {n.split("/", 1)[1] for n in self._names() if "/" in n and not n.endswith("/")}

    def test_single_top_level_folder(self):
        self.assertEqual({n.split("/")[0] for n in self._names()}, {self.name})

    def test_required_files_present(self):
        rel = self._rel()
        for must in ("app.py", "requirements.txt", "VERSION", "hub.py", "hub_admin.py",
                     "store.py", "pipeline.py", "exports.py", "methods/d2887.py",
                     "jobs/load_folder.py", "jobs/import_history.py", "tools/import_history.py",
                     "instruments_api.py", "instrument_admin.py", "standards.py",
                     "templates/instruments.html", "tools/parity_report.py", "agent/agent_main.py",
                     "agent/gc_agent/core.py", "agent/requirements-agent.txt",
                     "templates/index.html", "templates/calibration.html",
                     "templates/hub_admin.html", "static/js/hub_admin.js",
                     "static/js/app.js", "static/css/style.css", "static/css/badge.css"):
            self.assertIn(must, rel)

    def test_version_is_the_tag_not_the_stale_file(self):
        with zipfile.ZipFile(self.zip_path) as z:
            self.assertEqual(z.read(f"{self.name}/VERSION").decode().strip(), self.TAG)

    def test_every_runtime_module_ships(self):
        closure = runtime_closure(self.src)
        # Sanity: the closure really covers the hub, its tools and the agent
        # (guards a broken parser).
        self.assertTrue({"paths.py", "version.py", "supervisor.py", "restart_update.py",
                         "distill.py", "qbench_client.py", "hub.py", "hub_admin.py",
                         "store.py", "pipeline.py", "exports.py", "methods/d2887.py",
                         "jobs/load_folder.py", "import_match.py", "tools/parity_report.py",
                         "agent/agent_main.py", "agent/gc_agent/core.py"} <= closure, closure)
        rel = self._rel()
        missing = sorted(m for m in closure if m not in rel)
        self.assertEqual(missing, [])

    def test_the_v1_legacy_files_are_gone(self):
        rel = self._rel()
        for gone in ("run.pyw", "looker.py", "library_view.py"):
            self.assertNotIn(gone, rel)

    def test_every_tracked_static_and_template_file_ships(self):
        rel = self._rel()
        for sub in ("static", "templates"):
            for p in (self.src / sub).rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts:
                    self.assertIn(p.relative_to(self.src).as_posix(), rel)

    def test_no_state_or_dev_files(self):
        rel = self._rel()
        leaked = sorted(n for n in rel if n in PLANTED or any(
            n == d or n.startswith(d + "/") for d in
            ("tests", "docs", "scripts", ".git", ".github", ".venv", "venv", "dist",
             "exports", "CDF", "node_modules", ".claude", ".pytest_cache"))
            or n.startswith("processed_cdf")
            or "__pycache__" in n.split("/")
            or n.lower().endswith((".csv", ".pid", ".log", ".cdf", ".pyc", ".bak"))
            or re.search(r"\.log\.\d+$", n) or ".bak_" in n)
        self.assertEqual(leaked, [])

    def test_sha256_file_is_what_the_updater_parses(self):
        text = self.sha_path.read_text()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, text)
        m = _SHA256_LINE.match(lines[0].strip())
        self.assertIsNotNone(m, text)
        digest, fname = m.group(1).lower(), m.group(2)
        self.assertEqual(fname, f"{self.name}.zip")  # bare filename, no path
        self.assertEqual(digest, hashlib.sha256(self.zip_path.read_bytes()).hexdigest())

    @unittest.skipUnless(COA_UPDATER.is_file(), "needs ../coa-reviewer checkout")
    def test_sha256_file_parses_with_the_real_updater(self):
        spec = importlib.util.spec_from_file_location("_coa_updater", COA_UPDATER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        digest, fname = mod.parse_sha256_file(self.sha_path.read_text())
        self.assertEqual(fname, f"{self.name}.zip")
        self.assertEqual(digest, hashlib.sha256(self.zip_path.read_bytes()).hexdigest())

    def test_only_the_two_assets_are_written(self):
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         [f"{self.name}.zip", f"{self.name}.zip.sha256"])

    def test_source_tree_is_not_modified(self):
        # The stale VERSION is still there, i.e. the script stamped its staging
        # copy, not the source.
        self.assertEqual((self.src / "VERSION").read_text().strip(), STALE_VERSION)


@unittest.skipUnless(HAVE_TOOLS, "needs bash, rsync, zip")
class PackageCliTests(unittest.TestCase):
    def test_relative_outdir_and_rebuild_replace_the_zip(self):
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "repo"
            _copy_repo(src)
            for _ in range(2):  # a second run must replace, not append to, the zip
                r = subprocess.run(["bash", str(SCRIPT), "v1.2.3", "rel/out"],
                                   cwd=src, capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stderr)
            z = src / "rel" / "out" / "gc-hub-v1.2.3.zip"
            with zipfile.ZipFile(z) as zf:
                names = zf.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertFalse(any(n.split("/", 1)[-1].startswith("rel/") for n in names))

    def test_missing_args_fail(self):
        r = subprocess.run(["bash", str(ROOT / SCRIPT)], cwd=ROOT,
                           capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)

    def test_tag_with_slash_is_refused(self):
        # The tag becomes releases\<tag>\ on the server; a / breaks the unpack.
        with tempfile.TemporaryDirectory() as t:
            r = subprocess.run(["bash", str(ROOT / SCRIPT), "feat/x", t], cwd=ROOT,
                               capture_output=True, text=True, timeout=30)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(list(Path(t).iterdir()), [])

    def test_real_checkout_packages_cleanly(self):
        # Read-only against the checkout: output goes to a temp dir.
        with tempfile.TemporaryDirectory() as t:
            r = _package(ROOT, "v0.0.0-real", Path(t))
            self.assertEqual(r.returncode, 0, r.stderr)
            with zipfile.ZipFile(Path(t, "gc-hub-v0.0.0-real.zip")) as zf:
                rel = {n.split("/", 1)[1] for n in zf.namelist() if "/" in n}
            missing = sorted(m for m in runtime_closure(ROOT) if m not in rel)
            self.assertEqual(missing, [])


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@unittest.skipUnless(HAVE_TOOLS and shutil.which("git"), "needs bash, rsync, zip, git")
class PackageGitCheckoutTests(unittest.TestCase):
    """In a git checkout only tracked files ship; the exclude rules still apply."""

    TAG = "v1.2.3-rc.1"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.src = base / "repo"
        _copy_repo(cls.src)
        _git(cls.src, "init", "-q")
        _git(cls.src, "add", "-A")
        # Tracked by mistake: the exclude rules must still keep it out.
        (cls.src / "tracked_state.csv").write_text("state\n")
        _git(cls.src, "add", "-f", "tracked_state.csv")
        # Tracked but deleted from the working tree: must not break the build.
        (cls.src / "gone.py").write_text("x = 1\n")
        _git(cls.src, "add", "gone.py")
        (cls.src / "gone.py").unlink()
        # Untracked: a scratch module and a stray template must not ship.
        (cls.src / "scratch_untracked.py").write_text("print('wip')\n")
        (cls.src / "templates" / "untracked.html").write_text("<p>wip</p>\n")
        cls.out = base / "out"
        cls.result = _package(cls.src, cls.TAG, cls.out)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _rel(self):
        with zipfile.ZipFile(self.out / f"gc-hub-{self.TAG}.zip") as z:
            return {n.split("/", 1)[1] for n in z.namelist() if "/" in n and not n.endswith("/")}

    def test_builds(self):
        self.assertEqual(self.result.returncode, 0, self.result.stderr)

    def test_untracked_files_do_not_ship(self):
        rel = self._rel()
        self.assertNotIn("scratch_untracked.py", rel)
        self.assertNotIn("templates/untracked.html", rel)

    def test_tracked_state_is_still_excluded(self):
        self.assertNotIn("tracked_state.csv", self._rel())

    def test_tracked_runtime_files_ship(self):
        rel = self._rel()
        for must in ("app.py", "requirements.txt", "VERSION", "templates/index.html"):
            self.assertIn(must, rel)
        missing = sorted(m for m in runtime_closure(self.src) if m not in rel)
        self.assertEqual(missing, [])


@unittest.skipUnless(HAVE_TOOLS, "needs bash, rsync, zip")
class PackageLeakCheckTests(unittest.TestCase):
    def test_state_below_the_root_is_refused_not_shipped(self):
        # The exclude for updater state is anchored at the root; a copy deeper
        # down gets past rsync and must be caught by the leak check.
        for rel in ("static/staged.json", "static/paused", "templates/held-tags.json"):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as t:
                src = Path(t) / "repo"
                _copy_repo(src)
                (src / rel).write_text("state\n")
                r = _package(src, "v0.0.0-leak", Path(t) / "out")
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertIn("leaked", r.stderr)
                self.assertFalse((Path(t) / "out" / "gc-hub-v0.0.0-leak.zip").exists())


class TagValidationTests(unittest.TestCase):
    def _run(self, tag, out):
        return subprocess.run(["bash", str(ROOT / SCRIPT), tag, out], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)

    def test_non_semver_tags_are_refused(self):
        for tag in ("v1.2.", "vfoo", "1.2.3", "v1.2", "v1.2.3-", "v1.2.3-rc.", "v1.2.3/x"):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as t:
                r = self._run(tag, t)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("refusing tag", r.stderr)
                self.assertEqual(list(Path(t).iterdir()), [])

    # v1.2.3 and v1.2.3-rc.1 are built for real by PackageCliTests and
    # PackageGitCheckoutTests.


_REQ_LINE = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9_.\-]*)==(?P<ver>[A-Za-z0-9_.+\-]+)"
    r"(?:\s*;\s*(?P<marker>\S.*))?")


def _requirements():
    """(name, version, marker) for every requirement line, comments stripped."""
    out = []
    for raw in (ROOT / "requirements.txt").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            m = _REQ_LINE.fullmatch(line)
            out.append((m.group("name"), m.group("ver"), (m.group("marker") or "").strip())
                       if m else (line, None, None))
    return out


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


class RequirementsPinnedTests(unittest.TestCase):
    def test_every_requirement_is_pinned_exactly(self):
        reqs = _requirements()
        self.assertTrue(reqs)
        self.assertEqual([r[0] for r in reqs if r[1] is None], [])

    def test_markers_are_valid(self):
        try:
            from packaging.markers import Marker
        except ImportError:  # pragma: no cover - packaging ships with the pins
            self.skipTest("packaging not installed")
        for name, _ver, marker in _requirements():
            if marker:
                with self.subTest(name=name):
                    Marker(marker)

    def test_no_package_is_pinned_twice(self):
        names = [_norm(n) for n, _v, _m in _requirements()]
        self.assertEqual(sorted({n for n in names if names.count(n) > 1}), [])

    def test_transitive_dependencies_are_pinned(self):
        text = (ROOT / "requirements.txt").read_text()
        self.assertIn("# --- transitive", text)
        names = {_norm(n) for n, _v, _m in _requirements()}
        # The ones that break a deploy without anyone touching them.
        for dep in ("werkzeug", "jinja2", "click", "itsdangerous", "cftime", "urllib3",
                    "certifi", "reportlab", "tzdata"):
            self.assertIn(dep, names)

    def test_the_hub_neither_pins_nor_imports_the_tray_packages(self):
        # v2: run.pyw is gone; only the agent has a tray (it declares its
        # own deps in agent/requirements-agent.txt). Pillow stays, pinned as
        # reportlab/xhtml2pdf's dependency.
        names = {_norm(n) for n, _v, _m in _requirements()}
        self.assertFalse(names & {"pystray", "watchdog", "pyobjc-core", "python-xlib"}, names)
        self.assertIn("pillow", names)
        for rel in sorted(runtime_closure(ROOT)):
            if rel.startswith("agent/"):
                continue
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                        else [node.module] if isinstance(node, ast.ImportFrom) and node.module
                        else [])
                for m in mods:
                    self.assertNotIn(m.split(".")[0], {"pystray", "PIL", "watchdog"},
                                     f"{rel} imports {m}")

    def test_platform_only_packages_carry_markers(self):
        marks = {_norm(n): m for n, _v, m in _requirements()}
        for name, marker in marks.items():
            if name.startswith("pyobjc"):
                self.assertIn("darwin", marker, name)
        expected = {"python-xlib": "linux", "tzdata": "win32", "colorama": "win32"}
        for name, plat in expected.items():
            if name in marks:
                self.assertIn(plat, marks[name], name)


class RequireDepsConftestTests(unittest.TestCase):
    """GC_REQUIRE_DEPS=1 turns a missing dependency into a failed session
    instead of a run that silently skips everything that needs it."""

    def _pytest(self, env_extra, shim=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("GC_REQUIRE_DEPS", "PYTHONPATH")}
        env.update(env_extra)
        if shim:
            env["PYTHONPATH"] = shim
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "tests/test_qbench_import.py"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)

    def _broken_scipy(self, d):
        pkg = Path(d) / "scipy"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("raise ImportError('scipy hidden by test shim')\n")
        return d

    def test_missing_dep_fails_the_session_when_required(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._pytest({"GC_REQUIRE_DEPS": "1"}, shim=self._broken_scipy(d))
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("scipy", r.stdout + r.stderr)
        self.assertIn("GC_REQUIRE_DEPS", r.stdout + r.stderr)

    def test_missing_dep_is_tolerated_when_not_required(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._pytest({}, shim=self._broken_scipy(d))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_zero_means_off(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._pytest({"GC_REQUIRE_DEPS": "0"}, shim=self._broken_scipy(d))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


def _jobs(text):
    """{job id: its YAML block} for a workflow's top-level ``jobs:`` map.

    A text split, not a YAML parse (PyYAML is not a dependency); the workflows
    keep the conventional two-space indent that this relies on.
    """
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):[ \t]*$", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def _top(text):
    return text.split("\njobs:\n", 1)[0]


WF = ROOT / ".github" / "workflows"


class CiWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = (WF / "ci.yml").read_text()
        cls.jobs = _jobs(cls.wf)

    def test_triggers(self):
        top = _top(self.wf)
        for trig in ("push:", "pull_request:", "workflow_call:"):
            self.assertIn(trig, top)

    def test_read_only_token_and_no_persisted_credentials(self):
        top = _top(self.wf)
        self.assertRegex(top, r"permissions:\s*\n\s+contents: read")
        self.assertNotIn("contents: write", self.wf)
        for job in self.jobs.values():
            if "actions/checkout" in job:
                self.assertIn("persist-credentials: false", job)

    def test_concurrency_and_timeouts(self):
        self.assertIn("concurrency:", _top(self.wf))
        self.assertIn("github.ref", _top(self.wf))
        self.assertEqual(set(self.jobs), {"test", "windows-install"})
        for name, job in self.jobs.items():
            self.assertRegex(job, r"timeout-minutes: 20\b", name)

    def test_test_job(self):
        job = self.jobs["test"]
        self.assertIn("ubuntu-24.04", job)
        self.assertRegex(job, r"python-version: \[\s*'3\.12',\s*'3\.14'\s*\]")
        self.assertIn("pip install -r requirements.txt -r requirements-dev.txt", job)
        self.assertIn("GC_REQUIRE_DEPS: '1'", job)
        self.assertRegex(job, r"pytest tests/ -q -rs")
        self.assertIn("node tests/js/run.js", job)
        self.assertIn("bash scripts/package_release.sh v0.0.0-ci dist", job)
        # The identity tests against coa-reviewer run instead of skipping.
        self.assertIn("gh release view -R ASAP-Labs-LLC/coa-reviewer", job)
        self.assertIn("../coa-reviewer", job)
        self.assertLess(job.index("../coa-reviewer"), job.index("pytest tests/"))

    def test_windows_install_job(self):
        job = self.jobs["windows-install"]
        self.assertIn("windows-latest", job)
        self.assertIn("continue-on-error: true", job)
        self.assertIn("python-version: '3.14'", job)
        self.assertIn("-m pip install -r requirements.txt", job)
        self.assertIn("--no-tray", job)
        self.assertIn("15560", job)
        self.assertIn("/healthz", job)
        self.assertIn("GC_DATA_DIR", job)


class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = (WF / "release.yml").read_text()
        cls.jobs = _jobs(cls.wf)

    def test_tests_come_from_ci(self):
        self.assertIn("uses: ./.github/workflows/ci.yml", self.jobs["test"])
        self.assertRegex(self.jobs["publish"], r"needs: \[?test\b")

    def test_publish_also_needs_the_exports_and_agent_suites(self):
        # A red Windows export suite or GC-PC agent suite never releases either.
        self.assertIn("uses: ./.github/workflows/exports-ci.yml", self.jobs["exports"])
        self.assertIn("uses: ./.github/workflows/agent-ci.yml", self.jobs["agent"])
        m = re.search(r"needs: \[([^\]]*)\]", self.jobs["publish"])
        self.assertIsNotNone(m, "publish must list its needs")
        self.assertEqual({n.strip() for n in m.group(1).split(",")},
                         {"test", "exports", "agent"})
        for name in ("exports-ci.yml", "agent-ci.yml"):
            text = (WF / name).read_text()
            self.assertIn("workflow_call:", _top(text), name)
            self.assertRegex(_top(text), r"permissions:\s*\n\s+contents: read", name)
        for job in ("exports", "agent"):
            self.assertNotIn("contents: write", self.jobs[job])

    def test_write_token_only_in_publish(self):
        self.assertNotIn("contents: write", _top(self.wf))
        self.assertRegex(_top(self.wf), r"permissions:\s*\n\s+contents: read")
        self.assertNotIn("contents: write", self.jobs["test"])
        self.assertIn("contents: write", self.jobs["publish"])
        self.assertIn("persist-credentials: false", self.jobs["publish"])

    def test_publish_builds_with_the_script(self):
        pub = self.jobs["publish"]
        self.assertIn("scripts/package_release.sh", pub)
        self.assertIn("gc-hub-", pub)
        self.assertIn("ubuntu-24.04", pub)
        self.assertNotIn("ubuntu-latest", self.wf)
        self.assertRegex(pub, r"timeout-minutes: 20\b")

    def test_publish_is_idempotent(self):
        pub = self.jobs["publish"]
        self.assertIn('gc-hub-$TAG.zip.sha256', pub)
        self.assertIn("gh release delete", pub)
        self.assertNotIn("--cleanup-tag", pub)  # keep the tag
        self.assertIn("--verify-tag", pub)
        self.assertLess(pub.index("gh release view"), pub.index("gh release create"))

    def test_prerelease_tags_and_release_notes_file(self):
        pub = self.jobs["publish"]
        self.assertIn('"$TAG" == *-*', pub)
        self.assertIn("--prerelease", pub)
        self.assertIn('docs/release-notes/$TAG.md', pub)
        self.assertLess(pub.index("Write release notes"), pub.index("gh release create"))

    def test_one_release_at_a_time(self):
        top = _top(self.wf)
        self.assertRegex(top, r"concurrency:\s*\n\s+group: release")
        self.assertIn("cancel-in-progress: false", top)



class ActionVersionTests(unittest.TestCase):
    """Every workflow uses the same, Node-24-era major of each actions/* step."""

    MAJORS = {"actions/checkout": 7, "actions/setup-python": 7, "actions/setup-node": 7}

    def test_actions_are_on_the_current_majors_everywhere(self):
        seen = {}
        for wf in sorted(WF.glob("*.yml")):
            for action, ref in re.findall(r"uses: (actions/[\w-]+)@(\S+)", wf.read_text()):
                seen.setdefault(action, set()).add(ref)
                if action in self.MAJORS:
                    self.assertEqual(ref, f"v{self.MAJORS[action]}", f"{wf.name}: {action}@{ref}")
        for action in ("actions/checkout", "actions/setup-python", "actions/setup-node"):
            self.assertIn(action, seen)


def _step_script(job_text, step_name):
    """The ``run: |`` body of the step called ``step_name``, dedented."""
    lines = job_text.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.strip() == f"- name: {step_name}")
    run_i = next(i for i in range(start + 1, len(lines))
                 if lines[i].strip() == "run: |")
    indent = len(lines[run_i]) - len(lines[run_i].lstrip()) + 2
    body = []
    for l in lines[run_i + 1:]:
        if l.strip() and len(l) - len(l.lstrip()) < indent:
            break
        body.append(l[indent:])
    return "\n".join(body) + "\n"


_FAKE_GH = """#!/bin/sh
# Records every call; `release view` finds nothing, so publish creates.
printf '%s\\n' "$*" >> "$GH_LOG"
case "$1 $2" in
  "release view") exit 1 ;;
esac
exit 0
"""


class ReleaseWorkflowScriptTests(unittest.TestCase):
    """Run the publish job's own shell steps with a fake ``gh``."""

    @classmethod
    def setUpClass(cls):
        cls.pub = _jobs((WF / "release.yml").read_text())["publish"]

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.dir = Path(self._t.name)
        (self.dir / "bin").mkdir()
        gh = self.dir / "bin" / "gh"
        gh.write_text(_FAKE_GH)
        gh.chmod(0o755)
        (self.dir / "dist").mkdir()
        self.log = self.dir / "gh.log"

    def tearDown(self):
        self._t.cleanup()

    def _run(self, step, tag):
        env = dict(os.environ, TAG=tag, GH_LOG=str(self.log),
                   PATH=f"{self.dir / 'bin'}:{os.environ['PATH']}")
        script = _step_script(self.pub, step)
        r = subprocess.run(["bash", "-c", script], cwd=self.dir, env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def _create_call(self):
        calls = self.log.read_text().splitlines()
        return next(c for c in calls if c.startswith("release create"))

    def test_hyphenated_tag_is_published_as_prerelease(self):
        self._run("Publish release", "v1.2.3-rc.1")
        self.assertIn("--prerelease", self._create_call().split())

    def test_plain_tag_is_a_normal_release(self):
        self._run("Publish release", "v1.2.3")
        self.assertNotIn("--prerelease", self._create_call().split())

    def test_build_suffix_without_hyphen_is_a_normal_release(self):
        self._run("Publish release", "v1.2.3+build.7")
        self.assertNotIn("--prerelease", self._create_call().split())

    def test_notes_include_the_release_notes_file_for_the_tag(self):
        notes_dir = self.dir / "docs" / "release-notes"
        notes_dir.mkdir(parents=True)
        (notes_dir / "v1.2.3.md").write_text("Fixed `x` in C:\\ASAPApps.\n")
        self._run("Write release notes", "v1.2.3")
        notes = (self.dir / "dist" / "notes.md").read_text()
        self.assertTrue(notes.startswith("Fixed `x` in C:\\ASAPApps.\n"), notes)
        self.assertIn("C:\\ASAPApps\\gc\\releases\\v1.2.3\\", notes)
        self.assertIn("sha256sum -c gc-hub-v1.2.3.zip.sha256", notes)

    def test_notes_without_a_file_are_the_boilerplate(self):
        self._run("Write release notes", "v1.2.4")
        notes = (self.dir / "dist" / "notes.md").read_text()
        self.assertTrue(notes.startswith("Automated release of GC Hub."), notes)

    def test_v1_0_0_notes_exist(self):
        notes = (ROOT / "docs" / "release-notes" / "v1.0.0.md").read_text()
        self.assertIn("D2887", notes)
        self.assertIn("DEPLOY.md", notes)

if __name__ == "__main__":
    unittest.main()
