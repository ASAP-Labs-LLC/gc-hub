import pytest

from gc_agent import config, settings_ui, tray


@pytest.mark.parametrize("state,colour", [
    ("idle", "green"), ("sending", "green"), ("hub-unreachable", "amber"),
    ("auth-error", "red"), ("config-error", "red"), ("paused", "grey"),
])
def test_tray_colour(state, colour):
    assert tray.tray_colour(state) == colour
    assert len(tray.COLOURS[colour]) == 3


def test_status_lines():
    snap = {"version": "v2.3.0", "state": "sending", "queued": 4, "rejected": 1,
            "last_sent": "S1.CDF", "mirror_seq": 17, "last_error": None,
            "hub_url": "http://h", "log": "/x/agent.log", "paused": False}
    lines = tray.status_lines(snap)
    text = "\n".join(lines)
    for part in ("v2.3.0", "sending", "Queued: 4", "Rejected: 1", "Last sent: S1.CDF",
                 "Mirror seq: 17"):
        assert part in text
    snap["last_sent"], snap["last_error"] = None, "HTTP 401: bad token"
    text = "\n".join(tray.status_lines(snap))
    assert "Last sent: -" in text and "HTTP 401: bad token" in text


def test_menu_labels_follow_pause_state():
    assert tray.pause_label({"paused": False}) == "Pause"
    assert tray.pause_label({"paused": True}) == "Resume"


def test_icon_image_when_pillow_present():
    pytest.importorskip("PIL")
    img = tray.make_image("amber")
    assert img.size == (64, 64)
    assert img.getpixel((32, 32))[:3] == tray.COLOURS["amber"]


def _cfg():
    return config.validate({"hub_url": "http://h:5560", "token": "old", "watch_dir": "C:/w"})


def test_apply_settings_blank_token_keeps_old():
    new = settings_ui.apply_settings(_cfg(), {
        "hub_url": "http://h2:5560", "token": "", "watch_dir": "C:/w2", "include_subdirs": False,
        "poll_seconds": "7", "stable_seconds": "45", "results_mirror_path": ""})
    assert new["token"] == "old" and new["hub_url"] == "http://h2:5560"
    assert new["poll_seconds"] == 7 and new["stable_seconds"] == 45
    assert new["include_subdirs"] is False


def test_apply_settings_normalises_watch_dir():
    import os
    new = settings_ui.apply_settings(_cfg(), {"watch_dir": "C:/w/sub/../x/"})
    assert new["watch_dir"] == os.path.normpath("C:/w/x")


def test_apply_settings_new_token_and_validation():
    new = settings_ui.apply_settings(_cfg(), {"token": " new "})
    assert new["token"] == "new"
    with pytest.raises(config.ConfigError):
        settings_ui.apply_settings(_cfg(), {"poll_seconds": "soon"})
    with pytest.raises(config.ConfigError):
        settings_ui.apply_settings(_cfg(), {"hub_url": "ftp://x"})
