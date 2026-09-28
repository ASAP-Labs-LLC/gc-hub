"""End to end: a real agent subprocess (--no-tray) against the fake hub."""
import csv
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote

from gc_agent import updater
from gc_agent.client import HubClient
from gc_agent.mirror import CSV_HEADER

REPO = Path(__file__).resolve().parents[2]
AGENT_MAIN = REPO / "agent" / "agent_main.py"


def _line(*v):
    b = io.StringIO()
    csv.writer(b).writerow(v)
    return b.getvalue()


def _wait(pred, timeout=30.0, proc=None):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.05)
    return False


def test_agent_sends_a_dropped_cdf_and_mirrors_its_row(tmp_path, hub):
    root = tmp_path / "root"
    watch = tmp_path / "chem" / "2026-09"
    watch.mkdir(parents=True)
    mirror = tmp_path / "lem" / "distill_results.csv"
    mirror.parent.mkdir()
    root.mkdir()
    (root / "agent.json").write_text(json.dumps({
        "hub_url": hub.url, "token": hub.token, "watch_dir": str(tmp_path / "chem"),
        "include_subdirs": True, "poll_seconds": 0.2, "stable_seconds": 1,
        "results_mirror_path": str(mirror), "paused": False, "python": sys.executable}))
    env = dict(os.environ, GC_AGENT_HEARTBEAT_SECONDS="0.5", GC_AGENT_FAST_POLL="1")
    proc = subprocess.Popen([sys.executable, str(AGENT_MAIN), "--root", str(root), "--no-tray"],
                            cwd=str(tmp_path), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        assert _wait(lambda: len(hub.heartbeats) >= 1, proc=proc), "no heartbeat"
        body = b"CDF\x01\x02 synthetic sample " + os.urandom(64)
        cdf = watch / "Sample 1.CDF"
        cdf.write_bytes(body)
        sha = hashlib.sha256(body).hexdigest()
        assert _wait(lambda: sha in hub.received, proc=proc), "CDF never reached the hub"
        assert hub.received[sha] == body
        req = hub.by_path("/api/ingest")[-1]
        assert req.header("X-GC-SHA256") == sha
        assert unquote(req.header("X-GC-Filename")) == "Sample 1.CDF"
        assert cdf.read_bytes() == body                     # never modified

        row = _line("S-1", "2026-09-28 10:00:00", *(["1.0"] * 28), "Diesel", "0.99", "Sample 1.CDF")
        hub.rows.append({"seq": 1, "line": row})
        expect = (_line(*CSV_HEADER) + row).encode("utf-8")
        assert _wait(lambda: mirror.exists() and mirror.read_bytes() == expect, proc=proc), \
            "row never mirrored"
        assert _wait(lambda: hub.heartbeats[-1]["results_seq"] == 1
                     and hub.heartbeats[-1]["last_file"] == "Sample 1.CDF", proc=proc)
        assert hub.heartbeats[-1]["state"] == "idle"

        hub.commands.append("restart")
        assert proc.wait(timeout=30) == 3
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        out = proc.stdout.read().decode("utf-8", "replace")
        proc.stdout.close()
    assert "Traceback" not in out, out
    assert "tok-123" not in (root / "agent.log").read_text(encoding="utf-8")


def _builder():
    p = REPO / "agent" / "build_package.py"
    loader = importlib.machinery.SourceFileLoader("build_agent_zip_e2e", str(p))
    spec = importlib.util.spec_from_loader("build_agent_zip_e2e", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_real_package_passes_the_update_smoke_test(tmp_path, hub):
    zp = tmp_path / "pkg.zip"
    sha = _builder().build(REPO / "agent", "v9.0.0", zp)
    hub.package_version, hub.package_zip = "v9.0.0", zp.read_bytes()
    root = tmp_path / "root"
    (root / "versions" / "v8.0.0").mkdir(parents=True)
    (root / "current.txt").write_text("v8.0.0\n")
    res = updater.Updater(str(root), HubClient(hub.url, hub.token), own_sha="0" * 64,
                          python=sys.executable).check()
    assert res == "switched"
    assert (root / "current.txt").read_text().strip() == "v9.0.0"
    assert (root / "versions" / "v9.0.0" / "PACKAGE_SHA256").read_text().strip() == sha
