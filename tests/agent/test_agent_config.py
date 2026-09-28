import json
import os

import pytest

from gc_agent import config, util


def _good(tmp_path):
    return {"hub_url": "http://asapsv1:5560", "token": "t", "watch_dir": str(tmp_path)}


def test_defaults_are_filled_and_unknown_keys_kept(tmp_path):
    p = tmp_path / "agent.json"
    p.write_text(json.dumps(dict(_good(tmp_path), extra="keep")), encoding="utf-8")
    cfg = config.load(p)
    assert cfg["include_subdirs"] is True
    assert cfg["poll_seconds"] == 5
    assert cfg["stable_seconds"] == 30
    assert cfg["results_mirror_path"] == ""
    assert cfg["paused"] is False
    assert cfg["python"] == ""
    assert cfg["extra"] == "keep"


@pytest.mark.parametrize("key,value", [
    ("hub_url", "asapsv1:5560"), ("hub_url", ""), ("token", ""), ("token", 5),
    ("poll_seconds", 0), ("poll_seconds", "5"), ("stable_seconds", -1),
    ("include_subdirs", "yes"), ("paused", 1), ("watch_dir", None),
    ("results_mirror_path", 3),
])
def test_invalid_values_raise_naming_the_field(tmp_path, key, value):
    d = _good(tmp_path)
    d[key] = value
    with pytest.raises(config.ConfigError) as ei:
        config.validate(d)
    assert key in str(ei.value)


def test_missing_file_and_bad_json_raise(tmp_path):
    with pytest.raises(config.ConfigError):
        config.load(tmp_path / "nope.json")
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(tmp_path / "bad.json")


def test_save_is_atomic_and_round_trips(tmp_path):
    p = tmp_path / "agent.json"
    cfg = config.validate(dict(_good(tmp_path), paused=True))
    config.save(p, cfg)
    assert config.load(p)["paused"] is True
    assert sorted(os.listdir(tmp_path)) == ["agent.json"]


def test_local_iso_format():
    import time
    ts = time.mktime((2026, 9, 28, 7, 5, 9, 0, 0, -1))
    assert util.local_iso(ts) == "2026-09-28T07:05:09"


def test_atomic_write_bytes_replaces(tmp_path):
    p = tmp_path / "f"
    util.atomic_write_bytes(p, b"one")
    util.atomic_write_bytes(p, b"two")
    assert p.read_bytes() == b"two"
    assert os.listdir(tmp_path) == ["f"]
