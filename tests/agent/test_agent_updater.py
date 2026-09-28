import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from gc_agent import updater
from gc_agent.client import HubClient
from gc_agent.updater import UpdateError, Updater, safe_extract

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "scripts" / "build_agent_zip.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_agent_zip", str(BUILD))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_agent_src(tmp_path, body="VALUE = 1\n", name="src"):
    d = tmp_path / name
    (d / "gc_agent").mkdir(parents=True)
    (d / "gc_agent" / "__init__.py").write_text("", encoding="utf-8")
    (d / "gc_agent" / "__pycache__").mkdir()
    (d / "gc_agent" / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"junk")
    (d / "agent_main.py").write_text("import gc_agent\n" + body, encoding="utf-8")
    (d / "requirements-agent.txt").write_text("pystray\nPillow\n", encoding="utf-8")
    (d / "agent.json").write_text('{"token": "secret"}', encoding="utf-8")
    (d / "launcher.pyw").write_text("", encoding="utf-8")
    return d


def _build(tmp_path, version="v2.0.0", body="VALUE = 1\n", name="src"):
    b = _load_builder()
    out = tmp_path / ("%s-%s.zip" % (name, version))
    sha = b.build(_fake_agent_src(tmp_path, body, name), version, out)
    return out, sha


# ── build_agent_zip ──────────────────────────────────────────────────────
def test_build_zip_layout_and_no_token(tmp_path):
    out, sha = _build(tmp_path)
    assert sha == hashlib.sha256(out.read_bytes()).hexdigest()
    names = sorted(zipfile.ZipFile(str(out)).namelist())
    assert names == ["VERSION", "agent_main.py", "gc_agent/__init__.py", "requirements-agent.txt"]
    assert zipfile.ZipFile(str(out)).read("VERSION") == b"v2.0.0\n"


def test_build_zip_is_deterministic(tmp_path):
    b = _load_builder()
    src = _fake_agent_src(tmp_path)
    s1 = b.build(src, "v1.0.0", tmp_path / "a.zip")
    os.utime(str(src / "agent_main.py"), (1, 1))
    s2 = b.build(src, "v1.0.0", tmp_path / "b.zip")
    assert s1 == s2


def test_build_real_agent_and_cli_prints_sha(tmp_path):
    out = tmp_path / "agent.zip"
    r = subprocess.run([sys.executable, str(BUILD), "--version", "v9.9.9", "--out", str(out)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    assert r.stdout.strip().split()[0] == sha
    names = zipfile.ZipFile(str(out)).namelist()
    assert "agent_main.py" in names and "gc_agent/__init__.py" in names
    assert "VERSION" in names and "requirements-agent.txt" in names
    assert not any(n.endswith((".json", ".pyc", ".pyw")) for n in names)
    for n in names:
        assert b"Bearer tok" not in zipfile.ZipFile(str(out)).read(n)


# ── safe_extract ─────────────────────────────────────────────────────────
def _zip_with(names):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n in names:
            z.writestr(n, "x")
    return b.getvalue()


@pytest.mark.parametrize("bad", ["/etc/passwd", "\\\\srv\\x", "C:x.py", "C:\\x.py", "c:/x.py",
                                 "../x.py", "a/../../x.py", "a\\..\\..\\x.py", "..", "a/.."])
def test_safe_extract_rejects_zip_slip(tmp_path, bad):
    dest = tmp_path / "d"
    with pytest.raises(UpdateError):
        safe_extract(_zip_with(["ok.py", bad]), dest)
    assert not dest.exists() or list(dest.rglob("*")) == []


def test_safe_extract_ok(tmp_path):
    safe_extract(_zip_with(["a.py", "pkg/b.py"]), tmp_path / "d")
    assert (tmp_path / "d" / "pkg" / "b.py").read_text() == "x"


# ── check / switch / keep 3 ──────────────────────────────────────────────
def _root(tmp_path, running="v1.0.0", sha="old"):
    root = tmp_path / "root"
    vd = root / "versions" / running
    vd.mkdir(parents=True)
    (vd / "PACKAGE_SHA256").write_text(sha)
    (root / "current.txt").write_text(running + "\n")
    return root


def _updater(root, hub, own_sha="old"):
    return Updater(str(root), HubClient(hub.url, hub.token, timeout=10), own_sha=own_sha,
                   python=sys.executable)


def test_same_sha_is_a_noop(tmp_path, hub):
    out, sha = _build(tmp_path)
    hub.package_version, hub.package_zip = "v2.0.0", out.read_bytes()
    root = _root(tmp_path, sha=sha)
    assert _updater(root, hub, own_sha=sha).check() == "current"
    assert hub.by_path("/api/agent/package.zip") == []


def test_success_switches_and_keeps_previous(tmp_path, hub):
    out, sha = _build(tmp_path)
    hub.package_version, hub.package_zip = "v2.0.0", out.read_bytes()
    root = _root(tmp_path)
    assert _updater(root, hub).check() == "switched"
    assert (root / "current.txt").read_text().strip() == "v2.0.0"
    assert (root / "previous.txt").read_text().strip() == "v1.0.0"
    assert (root / "versions" / "v2.0.0" / "PACKAGE_SHA256").read_text().strip() == sha
    assert (root / "versions" / "v2.0.0" / "agent_main.py").exists()
    assert [p.name for p in (root / "versions").iterdir() if p.name.startswith(".")] == []


def test_sha_mismatch_refuses(tmp_path, hub):
    out, sha = _build(tmp_path)
    hub.package_version, hub.package_zip = "v2.0.0", out.read_bytes()
    hub.package_sha_override = "f" * 64
    root = _root(tmp_path)
    res = _updater(root, hub).check()
    assert res.startswith("refused") and "sha256" in res
    assert (root / "current.txt").read_text().strip() == "v1.0.0"
    assert not (root / "versions" / "v2.0.0").exists()


@pytest.mark.parametrize("v", ["../evil", "a/b", "", "C:x", ".hidden", "x" * 80])
def test_bad_version_string_refused(tmp_path, hub, v):
    out, sha = _build(tmp_path)
    hub.package_version, hub.package_zip = v, out.read_bytes()
    root = _root(tmp_path)
    assert _updater(root, hub).check().startswith("refused")
    assert (root / "current.txt").read_text().strip() == "v1.0.0"


def test_zip_slip_package_refused(tmp_path, hub):
    hub.package_version, hub.package_zip = "v2.0.0", _zip_with(["agent_main.py", "../x.py"])
    root = _root(tmp_path)
    assert _updater(root, hub).check().startswith("refused")
    assert not (tmp_path / "x.py").exists() and not (root / "versions" / "x.py").exists()


def test_package_without_agent_main_refused(tmp_path, hub):
    hub.package_version, hub.package_zip = "v2.0.0", _zip_with(["VERSION"])
    root = _root(tmp_path)
    assert _updater(root, hub).check().startswith("refused")


def test_smoke_failure_refuses_and_removes(tmp_path, hub):
    out, sha = _build(tmp_path, body="raise SystemExit('broken build')\n")
    hub.package_version, hub.package_zip = "v2.0.0", out.read_bytes()
    root = _root(tmp_path)
    res = _updater(root, hub).check()
    assert res.startswith("refused") and "smoke" in res
    assert not (root / "versions" / "v2.0.0").exists()
    assert (root / "current.txt").read_text().strip() == "v1.0.0"


def test_bad_package_list_is_skipped(tmp_path, hub):
    out, sha = _build(tmp_path)
    hub.package_version, hub.package_zip = "v2.0.0", out.read_bytes()
    root = _root(tmp_path)
    (root / "bad_packages.json").write_text(json.dumps([sha]))
    assert _updater(root, hub).check() == "skipped"
    assert hub.by_path("/api/agent/package.zip") == []


def test_existing_dir_with_other_sha_gets_suffixed(tmp_path, hub):
    out, sha = _build(tmp_path)
    hub.package_version, hub.package_zip = "v1.0.0", out.read_bytes()   # same name as running
    root = _root(tmp_path)
    assert _updater(root, hub).check() == "switched"
    cur = (root / "current.txt").read_text().strip()
    assert cur == "v1.0.0-" + sha[:12]
    assert (root / "versions" / "v1.0.0" / "PACKAGE_SHA256").read_text() == "old"


def test_keep_three_versions(tmp_path):
    root = _root(tmp_path, running="v5")
    for i, v in enumerate(["v1", "v2", "v3", "v4"]):
        d = root / "versions" / v
        d.mkdir()
        os.utime(str(d), (1000 + i, 1000 + i))
    (root / "versions" / ".tmp-abc").mkdir()
    (root / "previous.txt").write_text("v1\n")
    updater.prune(str(root), keep=3)
    left = sorted(p.name for p in (root / "versions").iterdir())
    assert left == ["v1", "v4", "v5"]   # current, previous, newest other


def test_smoke_python_prefers_python_exe_beside_pythonw(tmp_path):
    d = tmp_path / "py"
    d.mkdir()
    (d / "pythonw.exe").write_text("")
    assert updater.smoke_python(str(d / "pythonw.exe")) == str(d / "pythonw.exe")
    (d / "python.exe").write_text("")
    assert updater.smoke_python(str(d / "pythonw.exe")) == str(d / "python.exe")
    assert updater.smoke_python("") == sys.executable
