"""Integration seam I5/M3: the agent mirror and the hub export must never
share a file. The agent keeps its own sidecar (<file>.gcagent.json), refuses
any file the hub owns (<file>.gchub.json with the hub schema), migrates a
sidecar an older agent wrote as .gchub.json, and names its temp files so v1's
start-up sweep (``.<csv>.*.tmp``) can never match them."""
import csv
import fnmatch
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gc_agent import config, mirror, util
from gc_agent.core import Agent
from gc_agent.ledger import Ledger
from gc_agent.mirror import CSV_HEADER, Mirror, MirrorError

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "agent"


def _line(*v):
    b = io.StringIO()
    csv.writer(b).writerow(v)
    return b.getvalue()


def _rows(*seqs):
    return [{"seq": s, "line": _line("L%d" % s, "x")} for s in seqs]


HUB_SIDECAR = {"instrument": "gc1", "db_id": "3f2a", "size": 0, "sha256": "0" * 64, "seq": 4,
               "last_line_sha256": None, "adopted_at": "2026-09-28T10:00:00"}


def _mirror(tmp_path):
    lg = Ledger(tmp_path / "l.db")
    p = tmp_path / "out" / "results.csv"
    p.parent.mkdir()
    return lg, Mirror(str(p), lg), p


def test_agent_writes_its_own_sidecar_name(tmp_path):
    lg, mr, p = _mirror(tmp_path)
    mr.append(CSV_HEADER, _rows(1))
    assert (p.parent / "results.csv.gcagent.json").is_file()
    assert not (p.parent / "results.csv.gchub.json").exists()
    assert mirror.sidecar_path(p).name == "results.csv.gcagent.json"


def test_file_with_hub_sidecar_is_refused(tmp_path):
    lg, mr, p = _mirror(tmp_path)
    p.write_bytes(_line(*CSV_HEADER).encode())
    hub_sc = p.parent / "results.csv.gchub.json"
    hub_sc.write_text(json.dumps(HUB_SIDECAR))
    with pytest.raises(MirrorError, match="hub owns"):
        mr.append(CSV_HEADER, _rows(1))
    assert p.read_bytes() == _line(*CSV_HEADER).encode()
    assert json.loads(hub_sc.read_text()) == HUB_SIDECAR
    assert not (p.parent / "results.csv.gcagent.json").exists()


def test_new_file_next_to_hub_sidecar_is_refused(tmp_path):
    lg, mr, p = _mirror(tmp_path)
    (p.parent / "results.csv.gchub.json").write_text(json.dumps(HUB_SIDECAR))
    with pytest.raises(MirrorError, match="hub owns"):
        mr.append(CSV_HEADER, _rows(1))
    assert not p.exists()


def test_unreadable_gchub_sidecar_is_refused(tmp_path):
    lg, mr, p = _mirror(tmp_path)
    (p.parent / "results.csv.gchub.json").write_text("{not json")
    with pytest.raises(MirrorError, match="hub owns"):
        mr.append(CSV_HEADER, _rows(1))


def test_agent_sidecar_with_hub_keys_is_refused(tmp_path):
    lg, mr, p = _mirror(tmp_path)
    p.write_bytes(_line(*CSV_HEADER).encode())
    (p.parent / "results.csv.gcagent.json").write_text(json.dumps(HUB_SIDECAR))
    with pytest.raises(MirrorError, match="hub owns"):
        mr.append(CSV_HEADER, _rows(1))


def test_older_agent_gchub_sidecar_is_migrated(tmp_path):
    lg, mr, p = _mirror(tmp_path)
    data = (_line(*CSV_HEADER) + _line("A", "1")).encode()
    p.write_bytes(data)
    old = p.parent / "results.csv.gchub.json"
    old.write_text(json.dumps({"size": len(data), "sha256": util.sha256_bytes(data), "seq": 3,
                               "adopted_at": "2026-09-01T00:00:00"}))
    assert mr.append(CSV_HEADER, _rows(3, 4)) == 1
    assert not old.exists()
    sc = json.loads((p.parent / "results.csv.gcagent.json").read_text())
    assert sc["seq"] == 4
    assert p.read_bytes() == data + _rows(4)[0]["line"].encode()


def test_adopt_refuses_hub_owned_file(tmp_path):
    p = tmp_path / "results.csv"
    p.write_bytes(_line(*CSV_HEADER).encode())
    (tmp_path / "results.csv.gchub.json").write_text(json.dumps(HUB_SIDECAR))
    with pytest.raises(MirrorError, match="hub owns"):
        mirror.adopt(str(p), 0)
    assert not (tmp_path / "results.csv.gcagent.json").exists()
    info = mirror.inspect(str(p))
    assert info["hub_owned"] and "hub owns" in info["hub_owned"]


def test_core_reports_hub_owned_mirror_in_last_error(tmp_path, hub, clock):
    root = tmp_path / "root"
    (root / "watch").mkdir(parents=True)
    m = root / "share.csv"
    (root / "share.csv.gchub.json").write_text(json.dumps(HUB_SIDECAR))
    config.save(root / "agent.json", {"hub_url": hub.url, "token": hub.token,
                                      "watch_dir": str(root / "watch"),
                                      "results_mirror_path": str(m)})
    hub.rows = _rows(1)
    a = Agent(str(root), running_dir=str(root / "dev"), clock=clock)
    a.tick()
    assert not m.exists()
    err = a.last_error() or ""
    assert err.startswith("mirror:") and "hub owns" in err


def test_sidecar_temp_names_cannot_match_v1_sweep(tmp_path, monkeypatch):
    lg, mr, p = _mirror(tmp_path)
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append(os.path.basename(str(src)))
        return real(src, dst)

    monkeypatch.setattr(util.os, "replace", spy)
    mr.append(CSV_HEADER, _rows(1))
    mr.append(CSV_HEADER, _rows(2))
    assert seen, "no sidecar write observed"
    v1_glob = "." + p.name + ".*.tmp"
    for name in seen:
        assert not fnmatch.fnmatch(name, v1_glob), name
        assert name.startswith(".gcagent-") and name.endswith(".part"), name


# ── installer --mirror-path guards ───────────────────────────────────────
def _load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _download(tmp_path):
    bp = _load(AGENT / "build_package.py", "bp_integ")
    src = tmp_path / "download"
    src.mkdir()
    shutil.copy(str(AGENT / "install.pyw"), str(src / "install.pyw"))
    shutil.copy(str(AGENT / "launcher.pyw"), str(src / "launcher.pyw"))
    (src / "install.json").write_text(json.dumps({"hub_url": "http://h:5560", "token": "t"}))
    bp.build(AGENT, "v9.9.9", src / "agent-package.zip")
    home = tmp_path / "home"
    home.mkdir()
    return src, home


def _install(src, home, root, *extra):
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    return subprocess.run([sys.executable, str(src / "install.pyw"), "--dry-run", "--yes",
                           "--root", str(root), "--watch-dir", str(home)] + list(extra),
                          capture_output=True, text=True, timeout=120, env=env)


@pytest.mark.parametrize("unc", [r"\\asapserver\Labsharedrive\GC\distill_results.csv",
                                 "//asapserver/Labsharedrive/GC/distill_results.csv"])
def test_installer_refuses_unc_mirror_path(tmp_path, unc):
    src, home = _download(tmp_path)
    r = _install(src, home, tmp_path / "root", "--mirror-path", unc)
    assert r.returncode != 0
    assert "UNC" in r.stdout + r.stderr
    assert not (tmp_path / "root" / "agent.json").exists()


def test_installer_refuses_mirror_with_hub_sidecar(tmp_path):
    src, home = _download(tmp_path)
    m = tmp_path / "lem" / "distill_results.csv"
    m.parent.mkdir()
    m.write_bytes(_line(*CSV_HEADER).encode())
    (m.parent / "distill_results.csv.gchub.json").write_text(json.dumps(HUB_SIDECAR))
    r = _install(src, home, tmp_path / "root", "--mirror-path", str(m))
    assert r.returncode != 0
    assert "hub owns" in (r.stdout + r.stderr).lower()
    assert not (m.parent / "distill_results.csv.gcagent.json").exists()
