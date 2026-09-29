"""The agent and installer name themselves in User-Agent.

Cloudflare, in front of https://gc.asaplabs.net, answers urllib's default
``Python-urllib/x.y`` User-Agent with 403 "error code: 1010" (checked live),
so every agent/installer request must send ``gc-agent/<version>``.
"""
import ast
import importlib.machinery
import importlib.util
from pathlib import Path

from gc_agent.client import HubClient

AGENT = Path(__file__).resolve().parents[2] / "agent"


def _ua(rec):
    return {k.lower(): v for k, v in rec.headers.items()}.get("user-agent", "")


def test_hub_client_sends_gc_agent_user_agent(hub):
    HubClient(hub.url, hub.token, timeout=5, version="v3.0.0").heartbeat({"version": "v3.0.0"})
    rec = hub.by_path("/api/agent/heartbeat")[0]
    assert _ua(rec) == "gc-agent/v3.0.0"
    assert "python-urllib" not in _ua(rec).lower()


def test_hub_client_default_user_agent_is_not_urllibs(hub):
    HubClient(hub.url, hub.token, timeout=5).heartbeat({})
    assert _ua(hub.by_path("/api/agent/heartbeat")[0]) == "gc-agent/dev"


def test_agent_core_passes_its_version():
    tree = ast.parse((AGENT / "gc_agent" / "core.py").read_text(encoding="utf-8"))
    calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
             and ast.unparse(c.func) == "HubClient"]
    assert calls and all(any(k.arg == "version" and ast.unparse(k.value) == "self.version"
                             for k in c.keywords) for c in calls)


def test_installer_download_sends_gc_agent_user_agent(hub):
    loader = importlib.machinery.SourceFileLoader("gc_install_ua", str(AGENT / "install.pyw"))
    spec = importlib.util.spec_from_loader("gc_install_ua", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    try:
        mod._http_get(hub.url + "/api/agent/package", hub.token)
    except mod.InstallError:
        pass                      # the fake hub's answer doesn't matter here
    rec = hub.by_path("/api/agent/package")[0]
    assert _ua(rec).startswith("gc-agent/")
    assert "python-urllib" not in _ua(rec).lower()
