"""The tray's pure logic: status parsing, colour, status text, menu labels,
the busy rule, config and the relaunch decision."""
from __future__ import annotations

import json

import pytest

from gc_tray import logic


def _body(**kw):
    b = {"version": "v3.0.0", "pid": 42, "state": "running", "processing_paused": False,
         "updater_paused": False, "cpu_percent": 12.3, "rss_bytes": 190 * 1024 * 1024,
         "cpu_count": 8, "uptime_seconds": 3600,
         "queue": {"jobs_due": 0, "jobs_queued": 3, "jobs_running": 0, "received_samples": 1},
         "exporter": {"pending_rows": 0, "alive": True}, "staged_update": None,
         "worker_alive": True}
    b.update(kw)
    return b


# ── parse ─────────────────────────────────────────────────────────────────

def test_parse_running():
    v = logic.parse_status(_body(), marker_present=False)
    assert v["reachable"] is True and v["state"] == "running"
    assert v["version"] == "v3.0.0" and v["cpu_percent"] == 12.3
    assert v["queue"] == 3 and v["jobs_due"] == 0 and v["pending_rows"] == 0
    assert v["rss_bytes"] == 190 * 1024 * 1024 and v["staged_update"] is None


@pytest.mark.parametrize("server,state", [
    ("running", "running"), ("processing-paused", "paused"), ("starting", "starting"),
    ("stopping", "stopping"), ("something-new", "running"),
])
def test_parse_maps_server_states(server, state):
    assert logic.parse_status(_body(state=server), marker_present=False)["state"] == state


def test_parse_unreachable_is_stopped_with_the_marker_else_down():
    assert logic.parse_status(None, marker_present=True)["state"] == "stopped"
    v = logic.parse_status(None, marker_present=False)
    assert v["state"] == "down" and v["reachable"] is False and v["version"] is None


def test_parse_tolerates_junk():
    v = logic.parse_status({"state": 5, "queue": "x", "cpu_percent": "hot"}, marker_present=False)
    assert v["reachable"] is True and v["cpu_percent"] is None and v["queue"] is None
    assert logic.parse_status(["not", "a", "dict"], marker_present=False)["reachable"] is False


# ── busy / colour ─────────────────────────────────────────────────────────

def test_cpu_busy_needs_a_full_minute_above_the_threshold():
    b = logic.BusyTracker(cpu_percent=50, seconds=60)
    assert b.update(0, 80) is False
    assert b.update(30, 90) is False
    assert b.update(59, 70) is False
    assert b.update(60, 70) is True
    assert b.update(65, 20) is False            # dips reset it
    assert b.update(70, 80) is False
    assert b.update(129, 80) is False
    assert b.update(130, 80) is True
    assert b.update(135, None) is False         # unknown resets too


@pytest.mark.parametrize("state,busy,due,colour", [
    ("running", False, 0, "green"),
    ("running", True, 0, "amber"),
    ("running", False, 26, "amber"),
    ("running", False, 25, "green"),
    ("paused", False, 0, "amber"),
    ("starting", False, 0, "amber"),
    ("stopping", False, 0, "red"),
    ("stopped", False, 0, "red"),
    ("down", False, 0, "red"),
])
def test_colour(state, busy, due, colour):
    v = {"state": state, "reachable": state not in ("stopped", "down"), "jobs_due": due}
    assert logic.colour(v, cpu_busy=busy, queue_threshold=25) == colour
    assert colour in logic.COLOURS


# ── text ──────────────────────────────────────────────────────────────────

def test_status_text_running():
    v = logic.parse_status(_body(), marker_present=False)
    assert logic.status_text(v) == "GC hub v3.0.0 · running · CPU 12% · RAM 190 MB · queue 3"


def test_status_text_paused_and_down():
    v = logic.parse_status(_body(state="processing-paused", processing_paused=True),
                           marker_present=False)
    assert "processing paused" in logic.status_text(v)
    assert logic.status_text(logic.parse_status(None, marker_present=True)) == \
        "GC hub · stopped (updater paused)"
    assert logic.status_text(logic.parse_status(None, marker_present=False)) == \
        "GC hub · not responding"


def test_status_text_unknowns_and_length():
    v = logic.parse_status(_body(cpu_percent=None, rss_bytes=None, queue=None),
                           marker_present=False)
    t = logic.status_text(v)
    assert "CPU ?" in t and "RAM ?" in t and "queue ?" in t
    long = logic.parse_status(_body(version="v" + "9" * 200), marker_present=False)
    assert len(logic.tooltip(long)) <= 127


@pytest.mark.parametrize("n,text", [
    (None, "?"), (0, "0 MB"), (512 * 1024, "1 MB"), (190 * 1024 * 1024, "190 MB"),
    (3 * 1024 ** 3, "3.0 GB"),
])
def test_format_bytes(n, text):
    assert logic.format_bytes(n) == text


# ── menu ──────────────────────────────────────────────────────────────────

def test_menu_running():
    m = logic.menu_state(logic.parse_status(_body(), marker_present=False))
    assert m["pause_label"] == "Pause processing" and m["pause_enabled"]
    assert m["restart_label"] == "Restart" and m["restart_enabled"]
    assert m["stop_enabled"] and m["open_enabled"]
    assert not m["start_enabled"]


def test_menu_paused_and_staged():
    v = logic.parse_status(_body(state="processing-paused", processing_paused=True,
                                 staged_update="v3.1.0"), marker_present=False)
    m = logic.menu_state(v)
    assert m["pause_label"] == "Resume processing"
    assert m["restart_label"] == "Restart & install v3.1.0"


def test_menu_stopped_offers_only_start():
    m = logic.menu_state(logic.parse_status(None, marker_present=True))
    assert m["start_enabled"]
    assert not (m["pause_enabled"] or m["restart_enabled"] or m["stop_enabled"]
                or m["open_enabled"])


def test_menu_down_without_marker_still_offers_start():
    # the updater should restart it on its own; Start is harmless (resume
    # only removes a marker that is not there)
    m = logic.menu_state(logic.parse_status(None, marker_present=False))
    assert m["start_enabled"]


# ── config ────────────────────────────────────────────────────────────────

def test_config_defaults_and_overrides(tmp_path):
    cfg = logic.load_config(tmp_path / "missing.json")
    assert cfg["port"] == 5560 and cfg["poll_seconds"] == 5
    assert cfg["updater_config"].endswith("config.json")
    p = tmp_path / "tray.json"
    p.write_text(json.dumps({"port": 5570, "updater_config": r"D:\u\config.json",
                             "unknown": 1, "_comment": "x"}))
    cfg = logic.load_config(p)
    assert cfg["port"] == 5570 and cfg["updater_config"] == r"D:\u\config.json"
    assert "unknown" not in cfg
    assert logic.status_url(cfg) == "http://127.0.0.1:5570"
    assert logic.browser_url(cfg) == "http://localhost:5570"


@pytest.mark.parametrize("bad", [{"port": "x"}, {"port": 0}, {"port": 70000},
                                 {"poll_seconds": 0}, {"remember_password": "yes"}])
def test_config_rejects_bad_values(tmp_path, bad):
    p = tmp_path / "tray.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(logic.ConfigError):
        logic.load_config(p)


def test_config_that_is_not_json(tmp_path):
    p = tmp_path / "tray.json"
    p.write_text("{nope")
    with pytest.raises(logic.ConfigError):
        logic.load_config(p)


def test_the_shipped_example_is_the_defaults():
    from pathlib import Path
    example = Path(__file__).resolve().parents[2] / "tray" / "tray.example.json"
    assert logic.load_config(example) == logic.DEFAULTS


def test_start_command():
    cfg = dict(logic.DEFAULTS, updater_python=r"C:\Py314\python.exe")
    assert logic.start_command(cfg, fallback_python="x") == [
        r"C:\Py314\python.exe", r"C:\ASAPApps\updater\updater.py", "resume", "--app", "gc",
        "--config", r"C:\ASAPApps\updater\config.json"]
    cfg["updater_python"] = ""
    assert logic.start_command(cfg, fallback_python="py.exe")[0] == "py.exe"


def test_marker_path():
    assert str(logic.marker_path(dict(logic.DEFAULTS, data_dir="/d"))).replace("\\", "/") \
        == "/d/paused"
    assert logic.marker_path(dict(logic.DEFAULTS, data_dir="")) is None


# ── relaunch after an update ──────────────────────────────────────────────

@pytest.mark.parametrize("own,disk,hub,yes", [
    ("v3.0.0", "v3.1.0", "v3.1.0", True),      # the junction moved; new code on disk
    ("v3.0.0", "v3.0.0", "v3.1.0", False),     # launched from a fixed release: never loop
    ("v3.0.0", "v3.1.0", "v3.0.0", False),     # hub not switched yet
    ("v3.0.0", "v3.1.0", None, False),         # hub down
    ("dev", "dev", "v3.1.0", False),
    ("v3.1.0", "v3.1.0", "v3.1.0", False),
])
def test_should_relaunch(own, disk, hub, yes):
    assert logic.should_relaunch(own, disk, hub) is yes
