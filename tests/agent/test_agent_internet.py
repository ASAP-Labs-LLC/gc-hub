"""The agent over the internet (spec D5, amendments 3 and 16): its own
User-Agent (Cloudflare answers ``403 error code: 1010`` to
``Python-urllib/*``), separate connect/read timeouts, Cloudflare challenge
detection surfaced in the tray, a retried 524, and the LAN fallback used only
after a network/TLS error."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time

import pytest

from gc_agent import client as client_mod
from gc_agent import config
from gc_agent.client import CLOUDFLARE_BLOCKED, HubClient, HubResponse, NetworkError
from gc_agent.core import Agent
from gc_agent.ledger import Ledger
from gc_agent.sender import Sender

CHALLENGE_HTML = (b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
                  b"<body>challenge</body></html>")


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _sha(b):
    return hashlib.sha256(b).hexdigest()


# ── User-Agent ─────────────────────────────────────────────────────────────

def test_every_request_sends_the_agent_user_agent(hub):
    c = HubClient(hub.url, hub.token, timeout=5, version="v3.0.0")
    c.heartbeat({"state": "idle"})
    c.ingest("a.CDF", b"x", _sha(b"x"), time.time())
    c.results(0)
    c.package_info()
    c.package_zip()
    agents = {r.header("User-Agent") for r in hub.requests}
    assert agents == {"gc-agent/v3.0.0"}


def test_user_agent_defaults_to_unknown(hub):
    HubClient(hub.url, hub.token, timeout=5).heartbeat({})
    assert hub.requests[-1].header("User-Agent") == "gc-agent/unknown"


def test_core_passes_its_version_to_the_client(tmp_path, hub, clock):
    root = tmp_path / "root"
    (root / "watch").mkdir(parents=True)
    (root / "dev").mkdir()
    (root / "dev" / "VERSION").write_text("v4.5.6\n")
    config.save(root / "agent.json", {"hub_url": hub.url, "token": hub.token,
                                      "watch_dir": str(root / "watch")})
    Agent(str(root), running_dir=str(root / "dev"), clock=clock).tick()
    assert hub.by_path("/api/agent/heartbeat")[0].header("User-Agent") == "gc-agent/v4.5.6"


# ── timeouts and TLS ───────────────────────────────────────────────────────

def test_timeouts_suit_an_internet_hop():
    assert client_mod.CONNECT_TIMEOUT == 10
    assert client_mod.DEFAULT_TIMEOUT == 30
    assert client_mod.INGEST_TIMEOUT == 95


def test_connect_uses_the_connect_timeout_then_the_read_timeout(hub, monkeypatch):
    seen = []
    real = socket.create_connection

    def spy(address, timeout=None, *a, **kw):
        seen.append(timeout)
        return real(address, timeout, *a, **kw)

    monkeypatch.setattr(client_mod.http.client.socket, "create_connection", spy)
    port = int(hub.url.rsplit(":", 1)[1])
    conn = client_mod._HTTPConnection("127.0.0.1", port, timeout=95)
    conn.connect()
    try:
        assert seen == [client_mod.CONNECT_TIMEOUT]
        assert conn.sock.gettimeout() == 95
        assert conn.timeout == 95
    finally:
        conn.close()


def test_https_is_verified_with_the_default_context():
    c = HubClient("https://gc.example", "t")
    handlers = [h for h in c._opener.handlers if isinstance(h, client_mod._HTTPSHandler)]
    assert len(handlers) == 1
    ctx = handlers[0]._context
    import ssl
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


def test_proxies_are_still_ignored():
    import urllib.request
    c = HubClient("https://gc.example", "t")
    proxies = [h for h in c._opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    # ProxyHandler({}) replaces the environment-reading default and, having no
    # proxies, registers nothing: no handler may carry a proxy
    assert all(not h.proxies for h in proxies)
    assert not any(k.endswith("_open") and "proxy" in k for k in c._opener.handle_open)


def test_a_silent_server_times_out_as_a_network_error():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        c = HubClient("http://127.0.0.1:%d" % srv.getsockname()[1], "t", timeout=0.5)
        t0 = time.time()
        with pytest.raises(NetworkError):
            c.heartbeat({})
        assert time.time() - t0 < 5
    finally:
        srv.close()


# ── Cloudflare detection ───────────────────────────────────────────────────

@pytest.mark.parametrize("status,headers,body,blocked", [
    (403, {"cf-mitigated": "challenge", "Content-Type": "text/html"}, CHALLENGE_HTML, True),
    (200, {"cf-mitigated": "challenge"}, b"", True),
    (403, {"cf-ray": "8a1b-LAX", "Content-Type": "text/html"}, CHALLENGE_HTML, True),
    (503, {"cf-ray": "8a1b-LAX", "Content-Type": "text/html; charset=UTF-8"}, b"<html>", True),
    (403, {"cf-ray": "8a1b-LAX", "Content-Type": "text/plain"}, b"error code: 1010", True),
    (403, {"cf-ray": "8a1b-LAX"}, b"<!doctype html>", True),
    # a hub answer passing through the tunnel carries cf-ray too: JSON is the hub's
    (403, {"cf-ray": "8a1b-LAX", "Content-Type": "application/json"},
     b'{"error": "instrument gc2 is disabled"}', False),
    (401, {"cf-ray": "8a1b-LAX", "Content-Type": "text/html"}, CHALLENGE_HTML, False),
    (403, {"Content-Type": "text/html"}, CHALLENGE_HTML, False),       # no cf-ray: not CF
    (502, {"cf-ray": "x", "Content-Type": "text/html"}, b"<html>", False),
])
def test_cloudflare_block_classification(status, headers, body, blocked):
    r = HubResponse(status, body, headers)
    assert r.cloudflare_blocked is blocked
    if blocked:
        assert r.error_text() == CLOUDFLARE_BLOCKED
    assert CLOUDFLARE_BLOCKED == ("Cloudflare blocked the agent: add the WAF skip rule in "
                                  "DEPLOY.md")


def test_header_names_are_case_insensitive():
    assert HubResponse(403, b"<html>", {"CF-RAY": "x", "content-type": "text/html"}) \
        .cloudflare_blocked
    assert HubResponse(200, b"", {"CF-Mitigated": "challenge"}).cloudflare_blocked


def test_client_remembers_the_last_block_and_clears_it(hub):
    c = HubClient(hub.url, hub.token, timeout=5)
    assert c.cloudflare_blocked is False
    hub.raw_script.append((403, {"cf-mitigated": "challenge", "Content-Type": "text/html"},
                           CHALLENGE_HTML))
    r = c.heartbeat({})
    assert r.cloudflare_blocked and c.cloudflare_blocked
    assert c.heartbeat({}).status == 200
    assert c.cloudflare_blocked is False


def _sender(tmp_path, hub, clock):
    lg = Ledger(tmp_path / "ledger.db")
    return lg, Sender(lg, HubClient(hub.url, hub.token, timeout=5), clock=clock)


def _queue(tmp_path, lg, data=b"AAA"):
    p = tmp_path / "w" / "a.CDF"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(str(p), (1000, 1000))
    st = os.stat(str(p))
    lg.add_queued(str(p), st.st_size, st.st_mtime_ns, _sha(data))


@pytest.mark.parametrize("raw", [
    (403, {"cf-mitigated": "challenge", "Content-Type": "text/html"}, CHALLENGE_HTML),
    (403, {"cf-ray": "abc", "Content-Type": "text/plain"}, b"error code: 1010"),
    (503, {"cf-ray": "abc", "Content-Type": "text/html"}, b"<html>busy</html>"),
])
def test_a_blocked_ingest_backs_off_and_never_rejects(tmp_path, hub, clock, raw):
    lg, s = _sender(tmp_path, hub, clock)
    _queue(tmp_path, lg)
    hub.raw_script.append(raw)
    assert s.send_next() == "backoff"
    assert lg.counts()["queued"] == 1 and lg.counts()["rejected"] == 0
    assert s.status() == "hub-unreachable"
    assert CLOUDFLARE_BLOCKED in s.last_error
    clock.advance(10)
    assert s.send_next() == "sent"           # transient: the next try goes through


def test_a_524_from_ingest_is_retried(tmp_path, hub, clock):
    lg, s = _sender(tmp_path, hub, clock)
    _queue(tmp_path, lg)
    hub.raw_script.append((524, {"cf-ray": "abc", "Content-Type": "text/html"},
                           b"<html>A timeout occurred</html>"))
    assert s.send_next() == "backoff"
    assert lg.counts()["rejected"] == 0 and s.status() == "hub-unreachable"
    clock.advance(10)
    assert s.send_next() == "sent"


def _root(tmp_path, hub, **over):
    root = tmp_path / "root"
    (root / "watch").mkdir(parents=True)
    cfg = {"hub_url": hub.url, "token": hub.token, "watch_dir": str(root / "watch"),
           "stable_seconds": 0, "poll_seconds": 1}
    cfg.update(over)
    config.save(root / "agent.json", cfg)
    return root


def test_a_blocked_heartbeat_is_unreachable_not_auth_error_and_shows_why(tmp_path, hub,
                                                                          clock, caplog):
    root = _root(tmp_path, hub)
    a = Agent(str(root), running_dir=str(root / "dev"), clock=clock)
    hub.raw_script.append((403, {"cf-mitigated": "challenge", "Content-Type": "text/html"},
                           CHALLENGE_HTML))
    with caplog.at_level("WARNING"):
        a.tick()
    snap = a.snapshot()
    assert snap["state"] == "hub-unreachable"
    assert CLOUDFLARE_BLOCKED in snap["last_error"]
    assert CLOUDFLARE_BLOCKED in caplog.text
    clock.advance(31)
    a.tick()                                  # the edge lets it through again
    snap = a.snapshot()
    assert snap["state"] == "idle" and not snap["last_error"]


def test_a_blocked_results_pull_shows_in_the_tray(tmp_path, hub, clock):
    root = _root(tmp_path, hub, results_mirror_path=str(tmp_path / "root" / "m.csv"))
    a = Agent(str(root), running_dir=str(root / "dev"), clock=clock)
    # heartbeat passes, the results pull right after it is challenged
    hub.raw_script.extend([
        (200, {"Content-Type": "application/json"},
         json.dumps({"command": None, "agent_package_sha256": ""}).encode()),
        (403, {"cf-ray": "abc", "Content-Type": "text/plain"}, b"error code: 1010"),
    ])
    a.tick()
    snap = a.snapshot()
    assert CLOUDFLARE_BLOCKED in (snap["last_error"] or "")
    assert snap["state"] == "hub-unreachable"


def test_a_blocked_package_check_is_refused_not_installed(tmp_path, hub):
    from gc_agent import updater
    c = HubClient(hub.url, hub.token, timeout=5)
    hub.raw_script.append((403, {"cf-mitigated": "challenge", "Content-Type": "text/html"},
                           CHALLENGE_HTML))
    res = updater.Updater(tmp_path, c, "0" * 64, "").check()
    assert res.startswith("refused") and CLOUDFLARE_BLOCKED in res
    assert c.cloudflare_blocked


# ── LAN fallback ───────────────────────────────────────────────────────────

def test_lan_url_is_used_after_a_network_error(hub):
    c = HubClient("http://127.0.0.1:%d" % _closed_port(), hub.token, timeout=3,
                  lan_url=hub.url)
    r = c.heartbeat({"state": "idle"})
    assert r.status == 200
    assert hub.heartbeats == [{"state": "idle"}]
    body = b"CDF-bytes"
    assert c.ingest("a.CDF", body, _sha(body), time.time()).status == 201
    assert hub.received[_sha(body)] == body
    assert c.using_lan is True


def test_the_switch_to_the_lan_is_logged_once(hub, caplog):
    c = HubClient("http://127.0.0.1:%d" % _closed_port(), hub.token, timeout=3,
                  lan_url=hub.url)
    with caplog.at_level("WARNING", logger="gc_agent.client"):
        for _ in range(3):
            assert c.heartbeat({}).status == 200
    assert caplog.text.count("using the LAN address") == 1


def test_lan_url_is_never_used_after_an_http_status(hub):
    other = type(hub)(token=hub.token).start()
    try:
        c = HubClient(hub.url, hub.token, timeout=5, lan_url=other.url)
        hub.raw_script.append((403, {"cf-mitigated": "challenge"}, CHALLENGE_HTML))
        assert c.heartbeat({}).cloudflare_blocked
        hub.heartbeat_status = 503
        assert c.heartbeat({}).status == 503
        assert c.heartbeat({}).status == 503
        assert other.requests == []
    finally:
        other.stop()


def test_both_down_is_a_network_error_naming_both():
    c = HubClient("http://127.0.0.1:%d" % _closed_port(), "t", timeout=2,
                  lan_url="http://127.0.0.1:%d" % _closed_port())
    with pytest.raises(NetworkError) as ei:
        c.heartbeat({})
    assert "LAN" in str(ei.value)


def test_no_lan_url_means_no_second_try():
    c = HubClient("http://127.0.0.1:%d" % _closed_port(), "t", timeout=2)
    with pytest.raises(NetworkError):
        c.heartbeat({})


def test_agent_json_lan_url_is_optional_and_validated(tmp_path):
    base = {"hub_url": "https://gc.asaplabs.net/", "token": "t"}
    assert config.validate(base)["lan_url"] == ""
    cfg = config.validate(dict(base, lan_url="http://asapsv1:5560/"))
    assert cfg["lan_url"] == "http://asapsv1:5560"
    for bad in ("asapsv1:5560", 5, "ftp://x"):
        with pytest.raises(config.ConfigError) as ei:
            config.validate(dict(base, lan_url=bad))
        assert "lan_url" in str(ei.value)


def test_core_gives_the_client_the_lan_url(tmp_path, hub, clock):
    root = _root(tmp_path, hub, hub_url="http://127.0.0.1:%d" % _closed_port(),
                 lan_url=hub.url)
    a = Agent(str(root), running_dir=str(root / "dev"), clock=clock)
    a.tick()
    assert len(hub.heartbeats) == 1
    assert a.snapshot()["state"] == "idle"
