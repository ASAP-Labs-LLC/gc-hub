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
]
# A stale VERSION in the tree must be replaced by the tag, never shipped.
STALE_VERSION = "v9.9.9-stale"


def _local_modules(root: Path) -> set:
    return {p.stem for p in root.glob("*.py")}


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


def runtime_closure(root: Path) -> set:
    """Every local module reachable by import from app.py and run.pyw."""
    local = _local_modules(root)
    todo = [root / "app.py", root / "run.pyw"]
    seen_files, mods = set(), set()
    while todo:
        f = todo.pop()
        if f in seen_files:
            continue
        seen_files.add(f)
        for m in _imports_of(f, local):
            mods.add(m)
            todo.append(root / f"{m}.py")
    return mods


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
        for must in ("app.py", "requirements.txt", "VERSION", "run.pyw",
                     "templates/index.html", "templates/calibration.html",
                     "static/js/app.js", "static/css/style.css", "static/css/badge.css"):
            self.assertIn(must, rel)

    def test_version_is_the_tag_not_the_stale_file(self):
        with zipfile.ZipFile(self.zip_path) as z:
            self.assertEqual(z.read(f"{self.name}/VERSION").decode().strip(), self.TAG)

    def test_every_runtime_module_ships(self):
        closure = runtime_closure(self.src)
        # Sanity: the closure really covers the app (guards a broken parser).
        self.assertTrue({"paths", "version", "supervisor", "restart_update",
                         "distill", "qbench_client"} <= closure, closure)
        rel = self._rel()
        missing = sorted(m for m in closure if f"{m}.py" not in rel)
        self.assertEqual(missing, [])

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
            missing = sorted(m for m in runtime_closure(ROOT) if f"{m}.py" not in rel)
            self.assertEqual(missing, [])


class RequirementsPinnedTests(unittest.TestCase):
    def test_every_requirement_is_pinned_exactly(self):
        lines = [ln.split("#", 1)[0].strip()
                 for ln in (ROOT / "requirements.txt").read_text().splitlines()]
        reqs = [ln for ln in lines if ln]
        self.assertTrue(reqs)
        unpinned = [r for r in reqs if not re.fullmatch(r"[A-Za-z0-9_.\-]+==[A-Za-z0-9_.\-]+", r)]
        self.assertEqual(unpinned, [])


class WorkflowTests(unittest.TestCase):
    def test_workflow_uses_the_script_and_tests_first(self):
        wf = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        self.assertIn("scripts/package_release.sh", wf)
        self.assertIn("pytest", wf)
        self.assertIn("node tests/js/run.js", wf)
        self.assertLess(wf.index("pytest tests/"), wf.index("scripts/package_release.sh"))
        self.assertIn("contents: write", wf)
        self.assertIn("gc-hub-", wf)


if __name__ == "__main__":
    unittest.main()
