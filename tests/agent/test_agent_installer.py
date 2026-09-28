import csv
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from gc_agent.mirror import CSV_HEADER

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "agent"


def _load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


BUILDER = _load(REPO / "scripts" / "build_agent_zip.py", "build_agent_zip_i")
INSTALL = _load(AGENT / "install.pyw", "gc_install")


def _line(*v):
    b = io.StringIO()
    csv.writer(b).writerow(v)
    return b.getvalue()


def _download(tmp_path, hub_url="http://asapsv1:5560", token="tok-123", package=True,
              version="v9.9.9"):
    src = tmp_path / "download"
    src.mkdir()
    shutil.copy(str(AGENT / "install.pyw"), str(src / "install.pyw"))
    shutil.copy(str(AGENT / "launcher.pyw"), str(src / "launcher.pyw"))
    (src / "install.json").write_text(json.dumps({"hub_url": hub_url, "token": token}))
    sha = None
    if package:
        sha = BUILDER.build(AGENT, version, src / "agent-package.zip")
        (src / "agent-package.json").write_text(json.dumps({"version": version, "sha256": sha}))
    return src, sha


def _home(tmp_path, mirror_text=None):
    home = tmp_path / "home"
    home.mkdir()
    watch = tmp_path / "chem"
    watch.mkdir()
    mirror = tmp_path / "old" / "distill_results.csv"
    mirror.parent.mkdir()
    if mirror_text is not None:
        mirror.write_bytes(mirror_text.encode("utf-8"))
    (home / ".gc_viewer_settings.json").write_text(json.dumps(
        {"watch_dir": str(watch), "distill_output": str(mirror)}))
    return home, watch, mirror


def _run(src, root, home, *extra):
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    return subprocess.run([sys.executable, str(src / "install.pyw"), "--dry-run", "--yes",
                           "--root", str(root)] + list(extra),
                          capture_output=True, text=True, timeout=120, env=env)


def test_dry_run_installs_everything_but_autostart(tmp_path):
    src, sha = _download(tmp_path)
    home, watch, mirror = _home(tmp_path, _line(*CSV_HEADER) + _line("A1", "2026-09-01 10:00:00"))
    root = tmp_path / "root"
    r = _run(src, root, home)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / "launcher.pyw").read_bytes() == (AGENT / "launcher.pyw").read_bytes()
    vd = root / "versions" / "v9.9.9"
    assert (vd / "agent_main.py").is_file() and (vd / "gc_agent" / "core.py").is_file()
    assert (vd / "PACKAGE_SHA256").read_text().strip() == sha
    assert (root / "current.txt").read_text().strip() == "v9.9.9"
    cfg = json.loads((root / "agent.json").read_text())
    assert cfg["hub_url"] == "http://asapsv1:5560" and cfg["token"] == "tok-123"
    assert cfg["python"] == sys.executable
    assert cfg["watch_dir"] == str(watch) and cfg["results_mirror_path"] == str(mirror)
    assert cfg["paused"] is False and cfg["include_subdirs"] is True
    sc = json.loads((mirror.parent / "distill_results.csv.gchub.json").read_text())
    assert sc["size"] == mirror.stat().st_size and sc["seq"] == 0
    assert "A1" in r.stdout                        # the last row was shown
    assert "dry run" in r.stdout.lower() and "launcher.pyw" in r.stdout
    assert not (root / "launcher.log").exists()    # launcher not started
    assert "tok-123" not in r.stdout


def test_wrong_mirror_header_stops_without_sidecar(tmp_path):
    src, _ = _download(tmp_path)
    home, watch, mirror = _home(tmp_path, _line("Lab ID", "Something else"))
    r = _run(src, tmp_path / "root", home)
    assert r.returncode != 0
    assert "header" in (r.stdout + r.stderr)
    assert not (mirror.parent / "distill_results.csv.gchub.json").exists()


def test_new_mirror_path_needs_no_adoption(tmp_path):
    src, _ = _download(tmp_path)
    home, watch, mirror = _home(tmp_path, None)
    r = _run(src, tmp_path / "root", home)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not mirror.exists()


def test_explicit_paths_override_defaults(tmp_path):
    src, _ = _download(tmp_path)
    home, watch, mirror = _home(tmp_path, None)
    other = tmp_path / "other"
    other.mkdir()
    r = _run(src, tmp_path / "root", home, "--watch-dir", str(other), "--mirror", "")
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((tmp_path / "root" / "agent.json").read_text())
    assert cfg["watch_dir"] == str(other) and cfg["results_mirror_path"] == ""


def test_missing_install_json_fails(tmp_path):
    src, _ = _download(tmp_path)
    (src / "install.json").unlink()
    home, _, _ = _home(tmp_path)
    r = _run(src, tmp_path / "root", home)
    assert r.returncode != 0 and "install.json" in (r.stdout + r.stderr)


def test_zip_slip_package_refused(tmp_path):
    src, _ = _download(tmp_path, package=False)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("agent_main.py", "")
        z.writestr("gc_agent/__init__.py", "")
        z.writestr("../evil.py", "boom")
    (src / "agent-package.zip").write_bytes(buf.getvalue())
    home, _, _ = _home(tmp_path)
    r = _run(src, tmp_path / "root", home)
    assert r.returncode != 0
    assert not (tmp_path / "evil.py").exists() and not (tmp_path / "root" / "evil.py").exists()


def test_package_sha_mismatch_refused(tmp_path):
    src, sha = _download(tmp_path)
    (src / "agent-package.json").write_text(json.dumps({"version": "v9.9.9", "sha256": "0" * 64}))
    home, _, _ = _home(tmp_path)
    r = _run(src, tmp_path / "root", home)
    assert r.returncode != 0 and "sha256" in (r.stdout + r.stderr)


def test_downloads_package_from_hub_when_not_bundled(tmp_path, hub):
    zp = tmp_path / "pkg.zip"
    sha = BUILDER.build(AGENT, "v7.0.0", zp)
    hub.package_version, hub.package_zip = "v7.0.0", zp.read_bytes()
    src, _ = _download(tmp_path, hub_url=hub.url, token=hub.token, package=False)
    home, _, _ = _home(tmp_path)
    r = _run(src, tmp_path / "root", home)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "root" / "versions" / "v7.0.0" / "PACKAGE_SHA256").read_text().strip() == sha
    assert hub.by_path("/api/agent/package.zip")[0].header("Authorization") == "Bearer " + hub.token


def test_reinstall_keeps_settings_and_ledger(tmp_path):
    src, _ = _download(tmp_path)
    home, watch, mirror = _home(tmp_path, None)
    root = tmp_path / "root"
    assert _run(src, root, home).returncode == 0
    cfg = json.loads((root / "agent.json").read_text())
    cfg["poll_seconds"] = 9
    (root / "agent.json").write_text(json.dumps(cfg))
    (root / "ledger.db").write_bytes(b"keep")
    assert _run(src, root, home).returncode == 0
    assert json.loads((root / "agent.json").read_text())["poll_seconds"] == 9
    assert (root / "ledger.db").read_bytes() == b"keep"


def test_python_version_check():
    assert INSTALL.check_python((3, 8, 10)) is not None
    assert INSTALL.check_python((3, 9, 0)) is None
    assert INSTALL.check_python((3, 14, 0)) is None


def test_missing_tray_deps_stop_the_install(tmp_path, capsys):
    assert INSTALL.check_deps(find_spec=lambda n: None) == ["pystray", "PIL"]
    src, _ = _download(tmp_path)
    home, _, _ = _home(tmp_path)
    rc = INSTALL.main(["--dry-run", "--yes", "--root", str(tmp_path / "root"),
                       "--source", str(src)], find_spec=lambda n: None)
    assert rc != 0
    assert "pystray" in capsys.readouterr().out
    assert not (tmp_path / "root" / "agent.json").exists()


def test_autostart_command_quotes_paths():
    cmd = INSTALL.autostart_command("C:\\Py 3\\pythonw.exe", "C:\\Users\\a b\\gc-agent")
    assert cmd == '"C:\\Py 3\\pythonw.exe" "C:\\Users\\a b\\gc-agent\\launcher.pyw"'
