"""The tray and gc.asaplabs.net (spec D5, amendment 22): "Open in browser"
opens the hub's effective hub URL from ``GET /api/hub/status``
(``hub_url``), else the tray config's ``hub_url``, else localhost; the
control and status calls stay on 127.0.0.1 and name themselves with a
User-Agent."""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from gc_tray import client as client_mod
from gc_tray import controller as controller_mod
from gc_tray import logic

from .test_tray_actions import ScriptedUI, fake  # noqa: F401  (fake is a fixture)

EXAMPLE = Path(logic.__file__).resolve().parents[1] / "tray.example.json"


def _status(**kw):
    b = {"version": "v3.0.0", "state": "running"}
    b.update(kw)
    return b


def test_parse_status_carries_a_valid_hub_url():
    v = logic.parse_status(_status(hub_url="https://gc.asaplabs.net"), marker_present=False)
    assert v["hub_url"] == "https://gc.asaplabs.net"
    v = logic.parse_status(_status(hub_url="http://asapsv1:5560/"), marker_present=False)
    assert v["hub_url"] == "http://asapsv1:5560"


@pytest.mark.parametrize("junk", [None, 5, "", "gc.asaplabs.net", "javascript:alert(1)",
                                  "ftp://x", "https://", "https://a b", "https://x\n",
                                  "https://u:p@gc.asaplabs.net"])
def test_parse_status_drops_a_bad_hub_url(junk):
    assert logic.parse_status(_status(hub_url=junk), marker_present=False)["hub_url"] is None


def test_unreachable_view_has_no_hub_url():
    assert logic.parse_status(None, marker_present=False)["hub_url"] is None


def test_browser_url_prefers_the_hub_then_the_config_then_localhost():
    cfg = dict(logic.DEFAULTS, port=5570)
    view = logic.parse_status(_status(hub_url="https://gc.asaplabs.net"), marker_present=False)
    assert logic.browser_url(cfg, view) == "https://gc.asaplabs.net"
    down = logic.parse_status(None, marker_present=False)
    assert logic.browser_url(dict(cfg, hub_url="https://gc.example.net"), down) \
        == "https://gc.example.net"
    assert logic.browser_url(cfg, down) == "http://localhost:5570"
    assert logic.browser_url(cfg) == "http://localhost:5570"
    # control never follows it: it stays on loopback
    assert logic.status_url(dict(cfg, hub_url="https://gc.example.net")) \
        == "http://127.0.0.1:5570"


def test_config_hub_url_default_and_validation(tmp_path):
    assert logic.DEFAULTS["hub_url"] == ""
    p = tmp_path / "tray.json"
    p.write_text(json.dumps({"hub_url": "https://gc.asaplabs.net/"}))
    assert logic.load_config(p)["hub_url"] == "https://gc.asaplabs.net"
    for bad in (5, "gc.asaplabs.net", "ftp://x"):
        p.write_text(json.dumps({"hub_url": bad}))
        with pytest.raises(logic.ConfigError):
            logic.load_config(p)


def test_the_example_config_names_hub_url():
    ex = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert ex["hub_url"] == ""
    assert logic.load_config(EXAMPLE)["hub_url"] == ""


def _ctl(fake, **cfg):
    conf = dict(logic.DEFAULTS, port=fake.port, **cfg)
    return controller_mod.Controller(conf, client_mod.HubClient(logic.status_url(conf)),
                                     ScriptedUI(), python="py.exe", user="ryan")


def test_open_browser_uses_the_hub_url_from_the_last_status(fake):  # noqa: F811
    opened = []
    ctl = _ctl(fake)
    ctl.open_url = opened.append
    fake.status["hub_url"] = "https://gc.asaplabs.net"
    view = logic.parse_status(ctl.client.status(), marker_present=False)
    ctl.open_browser(view)
    ctl.open_browser()
    assert opened == ["https://gc.asaplabs.net", f"http://localhost:{fake.port}"]


def test_the_menu_passes_the_view_to_open_browser():
    from gc_tray import ui
    assert "action(ctl.open_browser, True)" in inspect.getsource(ui.build_menu)


def test_tray_requests_send_a_user_agent(fake):  # noqa: F811
    c = client_mod.HubClient(f"http://127.0.0.1:{fake.port}", user_agent="gc-hub-tray/v3.0.0")
    c.status()
    c.post("/api/restart", {})
    assert {r[3].get("User-Agent") for r in fake.requests} == {"gc-hub-tray/v3.0.0"}
    client_mod.HubClient(f"http://127.0.0.1:{fake.port}").status()
    assert fake.requests[-1][3].get("User-Agent") == "gc-hub-tray/dev"


def test_main_names_the_tray_with_its_release_version():
    from gc_tray import main
    src = inspect.getsource(main)
    assert "user_agent=" in src and "gc-hub-tray/" in src
