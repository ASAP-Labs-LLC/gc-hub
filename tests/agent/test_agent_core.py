import csv
import io
import json
import os
from pathlib import Path

import pytest

from gc_agent import config
from gc_agent.core import Agent
from gc_agent.mirror import CSV_HEADER

HB_KEYS = {"version", "package_sha256", "state", "queue_size", "rejected_count", "last_file",
           "last_error", "host", "agent_time", "results_seq"}
STATES = {"sending", "idle", "paused", "hub-unreachable", "auth-error", "config-error"}


def _line(*v):
    b = io.StringIO()
    csv.writer(b).writerow(v)
    return b.getvalue()


def _root(tmp_path, hub, **over):
    root = tmp_path / "root"
    (root / "watch").mkdir(parents=True)
    cfg = {"hub_url": hub.url, "token": hub.token, "watch_dir": str(root / "watch"),
           "stable_seconds": 0, "poll_seconds": 1,
           "results_mirror_path": str(root / "mirror.csv")}
    cfg.update(over)
    config.save(root / "agent.json", cfg)
    return root


def _agent(root, clock):
    return Agent(str(root), running_dir=str(root / "dev"), clock=clock)


def _drop(root, name, data=b"CDF"):
    p = root / "watch" / name
    p.write_bytes(data)
    os.utime(str(p), (1000, 1000))
    return p


def test_heartbeat_payload_has_exactly_the_contract_keys(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    a = _agent(root, clock)
    a.tick()
    hb = hub.heartbeats[0]
    assert set(hb) == HB_KEYS
    assert hb["state"] in STATES
    assert isinstance(hb["queue_size"], int) and isinstance(hb["rejected_count"], int)
    assert isinstance(hb["results_seq"], int)
    assert hb["version"] == "dev" and hb["package_sha256"] == ""
    assert len(hb["agent_time"]) == 19 and hb["agent_time"][10] == "T"
    assert hb["host"]


def test_sends_file_and_mirrors_after_heartbeat(tmp_path, hub, clock):
    hub.rows = [{"seq": 1, "line": _line("S1", "2026-09-28 10:00:00")}]
    root = _root(tmp_path, hub)
    _drop(root, "S1.CDF", b"bytes-1")
    a = _agent(root, clock)
    a.tick()
    assert list(hub.received.values()) == [b"bytes-1"]
    data = (root / "mirror.csv").read_bytes().decode()
    assert data == _line(*CSV_HEADER) + _line("S1", "2026-09-28 10:00:00")
    clock.advance(31)
    a.tick()
    assert hub.heartbeats[-1]["results_seq"] == 1
    assert hub.heartbeats[-1]["last_file"] == "S1.CDF"
    assert hub.heartbeats[-1]["state"] == "idle"


def test_pause_command_persists_and_stops_sending(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    hub.commands = ["pause"]
    a = _agent(root, clock)
    a.tick()                               # heartbeat delivers "pause"
    assert json.loads((root / "agent.json").read_text())["paused"] is True
    a.tick()                               # state change → immediate heartbeat
    assert hub.heartbeats[-1]["state"] == "paused"
    _drop(root, "x.CDF")
    clock.advance(31)
    a.tick()
    assert hub.by_path("/api/ingest") == []
    # survives a restart
    a2 = _agent(root, clock)
    assert a2.state() == "paused"
    hub.commands = ["resume"]
    a2.tick()
    assert json.loads((root / "agent.json").read_text())["paused"] is False
    a2.tick()
    assert len(hub.by_path("/api/ingest")) == 1


def test_retry_rejected_command(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    hub.ingest_script.append((415, {"error": "not a CDF"}))
    _drop(root, "r.CDF")
    a = _agent(root, clock)
    a.tick()
    assert a.ledger.counts()["rejected"] == 1
    a.tick()
    assert hub.heartbeats[-1]["rejected_count"] == 1
    hub.commands = ["retry-rejected"]
    clock.advance(31)
    a.tick()                               # command requeues
    a.tick()                               # and the file is sent
    assert a.ledger.counts() == {"queued": 0, "sent": 1, "rejected": 0}


def test_restart_command_exits_3(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    hub.commands = ["restart"]
    a = _agent(root, clock)
    assert a.tick() == 3


def test_unknown_command_is_ignored(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    hub.commands = ["format-c"]
    a = _agent(root, clock)
    assert a.tick() is None


def test_adopt_mirror_command(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    (root / "mirror.csv").write_bytes((_line(*CSV_HEADER) + _line("OLD", "x")).encode())
    hub.rows = [{"seq": 1, "line": _line("NEW", "y")}]
    a = _agent(root, clock)
    a.tick()
    assert "not adopted" in (a.mirror_error or "")
    assert (root / "mirror.csv").read_bytes() == (_line(*CSV_HEADER) + _line("OLD", "x")).encode()
    hub.commands = ["adopt-mirror"]
    clock.advance(31)
    a.tick()
    assert (root / "mirror.csv").read_bytes().endswith(_line("NEW", "y").encode())
    assert a.mirror_error is None


def test_state_change_triggers_immediate_heartbeat(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    a = _agent(root, clock)
    a.tick()
    n = len(hub.heartbeats)
    a.tick()
    assert len(hub.heartbeats) == n            # nothing changed, not due
    hub.ingest_script.append((401, {"error": "revoked"}))
    _drop(root, "a.CDF")
    clock.advance(1)                           # next scan due; heartbeat is not (30 s)
    a.tick()
    assert len(hub.heartbeats) == n + 1
    assert hub.heartbeats[-1]["state"] == "auth-error"
    assert "revoked" in hub.heartbeats[-1]["last_error"]


def test_config_edit_reloads_and_clears_hold(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    hub.ingest_script.append((401, {"error": "revoked"}))
    _drop(root, "a.CDF")
    a = _agent(root, clock)
    a.tick()
    assert a.state() == "auth-error"
    raw = json.loads((root / "agent.json").read_text())
    raw["token"] = hub.token          # "new" token
    raw["poll_seconds"] = 2
    config.save(root / "agent.json", raw)
    os.utime(str(root / "agent.json"), (5, 5))   # make sure the mtime differs
    a.tick()
    assert a.cfg["poll_seconds"] == 2
    assert a.ledger.counts()["sent"] == 1


def test_bad_config_is_config_error(tmp_path, hub, clock):
    root = _root(tmp_path, hub, watch_dir=str(tmp_path / "missing"))
    a = _agent(root, clock)
    a.tick()
    assert hub.heartbeats[-1]["state"] == "config-error"
    assert "missing" in hub.heartbeats[-1]["last_error"]


def test_unreadable_agent_json_is_config_error_without_crash(tmp_path, clock):
    root = tmp_path / "root"
    root.mkdir()
    (root / "agent.json").write_text("{", encoding="utf-8")
    a = _agent(root, clock)
    assert a.tick() is None
    assert a.state() == "config-error"


def test_hub_down_is_hub_unreachable(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    hub.heartbeat_status = 503
    a = _agent(root, clock)
    a.tick()
    assert a.state() == "hub-unreachable"


def test_revert_note_reported_then_cleared(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    (root / "revert.json").write_text(json.dumps(
        {"from": "v2.1.0", "to": "v2.0.0", "at": "2026-09-28T10:00:00"}))
    a = _agent(root, clock)
    a.tick()
    assert "reverted from v2.1.0 to v2.0.0" in hub.heartbeats[0]["last_error"]
    assert not (root / "revert.json").exists()


def test_package_sha_mismatch_requests_update_check(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    vd = root / "versions" / "v1"
    vd.mkdir(parents=True)
    (vd / "PACKAGE_SHA256").write_text("a" * 64)
    (vd / "VERSION").write_text("v1\n")
    (root / "current.txt").write_text("v1\n")
    hub.agent_package_sha256 = "b" * 64
    hub.package_version = "v2"
    hub.package_zip = b"not a zip"
    a = Agent(str(root), running_dir=str(vd), clock=clock)
    a.tick()
    hb = hub.heartbeats[0]
    assert hb["version"] == "v1" and hb["package_sha256"] == "a" * 64
    assert hub.by_path("/api/agent/package")          # checked (start or mismatch)
    n = len(hub.by_path("/api/agent/package"))
    clock.advance(31)
    a.tick()                                         # mismatch again, but rate-limited
    assert len(hub.by_path("/api/agent/package")) == n
    clock.advance(300)
    a.tick()
    assert len(hub.by_path("/api/agent/package")) == n + 1


def test_dev_layout_never_updates(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    hub.agent_package_sha256 = "b" * 64
    a = _agent(root, clock)
    a.tick()
    assert hub.by_path("/api/agent/package") == []


def test_tray_actions_queue(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    a = _agent(root, clock)
    a.tick()
    a.request("pause")
    a.tick()
    assert a.cfg["paused"] is True
    a.request("quit")
    assert a.tick() == 0
    snap = a.snapshot()
    for k in ("version", "state", "queued", "rejected", "last_sent", "mirror_seq", "hub_url", "log"):
        assert k in snap


def test_adopt_mirror_is_a_noop_when_mirroring_is_off(tmp_path, hub, clock):
    root = _root(tmp_path, hub, results_mirror_path="")
    hub.rows = [{"seq": 1, "line": _line("NEW", "y")}]
    hub.commands = ["adopt-mirror"]
    a = _agent(root, clock)
    assert a.tick() is None
    assert a.mirror is None and a.mirror_error is None
    assert hub.by_path("/api/agent/results") == []     # nothing pulled
    assert a.ledger.results_seq() == 0
    assert list(root.glob("*.gchub.json")) == []


def test_big_first_scan_does_not_block_heartbeat_or_quit(tmp_path, hub, clock):
    import time as _t
    root = _root(tmp_path, hub)
    for i in range(2500):
        p = root / "watch" / ("f%05d.CDF" % i)
        p.write_bytes(b"%d" % i)
        os.utime(str(p), (1000, 1000))
    a = _agent(root, clock)
    t0 = _t.monotonic()
    a.tick()
    assert _t.monotonic() - t0 < 10
    assert len(hub.heartbeats) == 1                    # the first tick still heartbeats
    c = a.ledger.counts()
    assert 0 < c["queued"] + c["sent"] <= 200          # one capped scan pass
    a.request("pause")
    clock.advance(1)
    a.tick()
    assert a.cfg["paused"] is True
    a.request("quit")
    assert a.tick() == 0


def test_poll_seconds_floor_outside_tests(monkeypatch):
    from gc_agent import core
    monkeypatch.delenv("GC_AGENT_FAST_POLL", raising=False)
    assert core.effective_poll({"poll_seconds": 0.2}) == 1.0
    assert core.effective_poll({"poll_seconds": 5}) == 5.0
    monkeypatch.setenv("GC_AGENT_FAST_POLL", "1")
    assert core.effective_poll({"poll_seconds": 0.2}) == 0.2


def test_hub_command_quit_is_not_accepted(tmp_path, hub, clock):
    root = _root(tmp_path, hub)
    hub.commands = ["quit"]
    a = _agent(root, clock)
    assert a.tick() is None
    assert a.tick() is None
