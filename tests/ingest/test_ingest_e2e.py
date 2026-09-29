"""2B1 T7, end to end: the booted hub + the REAL agent (``agent/agent_main.py
--no-tray``, as tests/agent/test_agent_e2e.py runs it).

The admin password is set and the token minted through the admin API (the
installer download). A synthetic CDF dropped into the agent's watch folder is
sent to ``/api/ingest``, processed to ``final`` (gc1 is calibrated, has hub
corrections and is live since 2020), served by ``GET /api/agent/results`` and
mirrored by the agent. Finally a ``restart`` command queued through the admin
API reaches the agent on a heartbeat and it exits with code 3.

The hub's own Worker is started by the hub start-up (2A1 T4/T5). This test
runs one on the same store as well, so it passes before and after that
wiring (jobs are claimed atomically; two workers are safe).
"""
from __future__ import annotations

import functools
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_helpers import admin_post, get_json  # noqa: E402

pytest.importorskip("flask")
pytest.importorskip("netCDF4")

import corrections  # noqa: E402
import distill  # noqa: E402
import store  # noqa: E402
from bootapp import booted, cookie_header, setup_admin  # noqa: E402
from pipeline_helpers import Hub  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
AGENT_MAIN = REPO / "agent" / "agent_main.py"


def _wait(pred, timeout=60.0, proc=None, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(step)
    return bool(pred())


def _installer(port, password):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/admin/instruments/gc1/installer",
                                 data=json.dumps({"password": password}).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **cookie_header(port)})
    with urllib.request.urlopen(req, timeout=60) as r:
        return zipfile.ZipFile(io.BytesIO(r.read()))


def test_real_agent_to_final_sample_and_results(tmp_path):
    hub = Hub(tmp_path)
    hub.gc1(live_since=datetime(2020, 1, 1))
    with store.connection(hub.db) as conn:
        with store.write_txn(conn):
            store.corrections.set_all(conn, "gc1", corrections.seed_from_file(str(hub.corrections)),
                                      by="test", reason="e2e seed")
    provider = corrections.StoreProvider(read_fn=functools.partial(store.corrections.read,
                                                                   db=hub.db))
    worker = hub.worker(corrections_provider=provider)

    watch = tmp_path / "chem" / "2026-09"
    watch.mkdir(parents=True)
    mirror = tmp_path / "lem" / "distill_results.csv"
    mirror.parent.mkdir()
    root = tmp_path / "agent-root"
    root.mkdir()

    with booted(tmp_path) as (port, _hub_proc, data, _home):
        assert data == hub.data
        pw = setup_admin(port, data)
        code, _ = admin_post(port, "/api/admin/hub-url",
                             {"password": pw, "hub_url": f"http://127.0.0.1:{port}"})
        assert code == 200
        z = _installer(port, pw)
        install = json.loads(z.read("install.json"))
        assert install["hub_url"] == f"http://127.0.0.1:{port}"
        token = install["token"]

        (root / "agent.json").write_text(json.dumps({
            "hub_url": install["hub_url"], "token": token, "watch_dir": str(tmp_path / "chem"),
            "include_subdirs": True, "poll_seconds": 0.2, "stable_seconds": 1,
            "results_mirror_path": str(mirror), "paused": False, "python": sys.executable}))
        env = dict(os.environ, GC_AGENT_HEARTBEAT_SECONDS="0.5", GC_AGENT_FAST_POLL="1")
        for k in ("GC_DATA_DIR", "PORT"):
            env.pop(k, None)
        agent = subprocess.Popen([sys.executable, str(AGENT_MAIN), "--root", str(root), "--no-tray"],
                                 cwd=str(tmp_path), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            # the agent heartbeats with the minted token
            def seen():
                with store.connection(hub.db) as c:
                    r = c.execute("SELECT * FROM agents WHERE instrument_id='gc1'").fetchone()
                return dict(r) if r and r["last_seen"] else None
            assert _wait(seen, proc=agent), "no heartbeat reached the hub"

            # drop a CDF (written elsewhere, then moved in, as ChemStation finishes a run)
            src = hub.cdf(name="E2E-40304", injected=datetime(2026, 9, 25, 14, 23, 0))
            body = src.read_bytes()
            sha = hashlib.sha256(body).hexdigest()
            tmpfile = tmp_path / "Run 7.CDF.tmp"
            shutil.copyfile(src, tmpfile)
            os.replace(tmpfile, watch / "Run 7.CDF")

            assert _wait(lambda: store.samples.find_by_sha(sha, db=hub.db) is not None,
                         proc=agent), "the CDF never reached the hub"
            sample = store.samples.find_by_sha(sha, db=hub.db)
            assert sample["instrument_id"] == "gc1" and sample["source_name"] == "Run 7.CDF"
            assert sample["backfill"] == 0

            def final():
                worker.run_until_idle()
                return store.samples.get(sample["id"], db=hub.db)["status"] == "final"
            assert _wait(final, timeout=60), store.samples.get(sample["id"], db=hub.db)

            ledger = store.export_rows.rows_after("gc1", 0, db=hub.db)
            assert [r["sample_id"] for r in ledger] == [sample["id"]]
            code, res = get_json(port, "/api/agent/results?after=0&limit=500", token)
            assert code == 200
            assert res["header"] == list(distill.CSV_HEADER)
            assert res["rows"] == [{"seq": ledger[0]["seq"], "line": ledger[0]["line"]}]
            assert res["rows"][0]["line"].startswith("E2E-40304,2026-09-25 14:23:00,")
            rev = store.get_revision(sample["id"], db=hub.db)
            assert json.loads(rev["corrections_used"])["source"] == "hub"

            # the agent pulls the row into its (optional) mirror, byte for byte
            import csv
            hdr = io.StringIO()
            csv.writer(hdr).writerow(distill.CSV_HEADER)
            expect = (hdr.getvalue() + ledger[0]["line"]).encode("utf-8")
            assert _wait(lambda: mirror.exists() and mirror.read_bytes() == expect, proc=agent), \
                "row never mirrored"
            assert _wait(lambda: (seen() or {}).get("results_seq") == ledger[0]["seq"]
                         and (seen() or {}).get("last_file") == "Run 7.CDF", proc=agent)

            # an admin command reaches the agent once, via the heartbeat
            code, _ = admin_post(port, "/api/admin/instruments/gc1/agent-command",
                                 {"password": pw, "command": "restart"})
            assert code == 200
            assert agent.wait(timeout=30) == 3
        finally:
            if agent.poll() is None:
                agent.kill()
                agent.wait()
            out = agent.stdout.read().decode("utf-8", "replace")
            agent.stdout.close()
        assert "Traceback" not in out, out
        assert token not in (root / "agent.log").read_text(encoding="utf-8")
