"""netctx.py: the one place the hub decides a request's client address,
whether it is https, whether it came through Cloudflare (a trusted proxy)
and whether it is local (spec D4, rev 2).

In process, on a bare Flask app: ``environ_base`` sets the WSGI
``REMOTE_ADDR`` (what ``request.remote_addr`` reads), so the tunnel is
simulated exactly as cloudflared presents it: loopback peer, ``Host:
gc.asaplabs.net``, ``CF-Connecting-IP``, ``CF-Ray``, ``X-Forwarded-Proto``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
if str(TESTS.parent) not in sys.path:
    sys.path.insert(0, str(TESTS.parent))

pytest.importorskip("flask")

from flask import Flask  # noqa: E402

import netctx  # noqa: E402

APP = Flask(__name__)

TUNNEL_HEADERS = {"Host": "gc.asaplabs.net", "CF-Connecting-IP": "203.0.113.9",
                  "CF-Ray": "8c1d2e3f4a5b6c7d-DFW", "X-Forwarded-Proto": "https",
                  "X-Forwarded-For": "203.0.113.9"}
LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}
LAN = {"REMOTE_ADDR": "10.0.0.25"}


def ctx(environ=None, headers=None, url="/", base_url=None, method="GET"):
    kw = {"environ_base": dict(environ or LOOPBACK), "headers": dict(headers or {}),
          "method": method}
    if base_url:
        kw["base_url"] = base_url
    return APP.test_request_context(url, **kw)


@pytest.fixture(autouse=True)
def _no_extra_proxies(monkeypatch, tmp_path):
    monkeypatch.setenv("GC_DATA_DIR", str(tmp_path))
    netctx._proxy_cache.clear()
    yield
    netctx._proxy_cache.clear()


def _trust(tmp_path, value):
    (tmp_path / "settings.json").write_text(json.dumps({"trusted_proxies": value}),
                                            encoding="utf-8")
    netctx._proxy_cache.clear()


# ── client_ip ───────────────────────────────────────────────────────────────

def test_client_ip_is_remote_addr_without_a_proxy():
    with ctx(LAN):
        assert netctx.client_ip() == "10.0.0.25"


def test_client_ip_through_the_tunnel_is_cf_connecting_ip():
    with ctx(LOOPBACK, TUNNEL_HEADERS):
        assert netctx.client_ip() == "203.0.113.9"


def test_client_ip_normalises_the_cf_address():
    with ctx(LOOPBACK, {"CF-Connecting-IP": " 2001:DB8::1 "}):
        assert netctx.client_ip() == "2001:db8::1"


def test_spoofed_cf_connecting_ip_from_a_lan_host_is_ignored():
    with ctx(LAN, {"CF-Connecting-IP": "8.8.8.8", "CF-Ray": "x", "X-Forwarded-Proto": "https"}):
        assert netctx.client_ip() == "10.0.0.25"
        assert not netctx.is_https()
        assert not netctx.is_proxied()


def test_an_invalid_cf_connecting_ip_falls_back_to_remote_addr():
    with ctx(LOOPBACK, {"CF-Connecting-IP": "not-an-ip"}):
        assert netctx.client_ip() == "127.0.0.1"


def test_trusted_proxies_setting_adds_a_cloudflared_on_another_machine(tmp_path):
    _trust(tmp_path, ["10.0.0.7"])
    with ctx({"REMOTE_ADDR": "10.0.0.7"}, TUNNEL_HEADERS):
        assert netctx.client_ip() == "203.0.113.9"
        assert netctx.is_https() and netctx.is_proxied()
    with ctx(LAN, TUNNEL_HEADERS):          # another LAN host is still not trusted
        assert netctx.client_ip() == "10.0.0.25"


def test_trusted_proxies_accepts_a_comma_separated_string_and_cidrs(tmp_path):
    _trust(tmp_path, "10.0.0.7, 192.168.5.0/24, junk")
    assert netctx.is_trusted_proxy("10.0.0.7")
    assert netctx.is_trusted_proxy("192.168.5.44")
    assert not netctx.is_trusted_proxy("10.0.0.8")
    assert netctx.is_trusted_proxy("127.0.0.1") and netctx.is_trusted_proxy("::1")


def test_trusted_proxies_without_a_data_folder(monkeypatch):
    monkeypatch.delenv("GC_DATA_DIR", raising=False)
    assert netctx.is_trusted_proxy("127.0.0.1") and not netctx.is_trusted_proxy("10.0.0.7")


# ── is_https / is_proxied ───────────────────────────────────────────────────

def test_is_https_through_the_tunnel():
    with ctx(LOOPBACK, TUNNEL_HEADERS):
        assert netctx.is_https() and netctx.is_proxied()


def test_is_https_false_for_plain_lan_http():
    with ctx(LAN):
        assert not netctx.is_https() and not netctx.is_proxied()


def test_is_https_true_for_a_direct_tls_request():
    with ctx(LAN, base_url="https://asapsv1:5560"):
        assert netctx.is_https()


def test_x_forwarded_proto_takes_the_first_value():
    with ctx(LOOPBACK, {"X-Forwarded-Proto": "https, http"}):
        assert netctx.is_https()
    with ctx(LOOPBACK, {"X-Forwarded-Proto": "http"}):
        assert not netctx.is_https() and netctx.is_proxied()


# ── is_local ────────────────────────────────────────────────────────────────

def test_is_local_for_the_servers_own_console():
    with ctx(LOOPBACK, {"Host": "localhost:5560"}):
        assert netctx.is_local()
    with ctx(LOOPBACK, {"Host": "127.0.0.1:5560"}):
        assert netctx.is_local()
    with ctx({"REMOTE_ADDR": "::1"}, {"Host": "[::1]:5560"}):
        assert netctx.is_local()


@pytest.mark.parametrize("header", ["CF-Connecting-IP", "CF-Ray", "X-Forwarded-For",
                                    "Forwarded", "X-Forwarded-Proto"])
def test_any_proxy_header_makes_it_not_local(header):
    with ctx(LOOPBACK, {"Host": "localhost:5560", header: "203.0.113.9"}):
        assert not netctx.is_local()


def test_the_tunnel_is_never_local_even_when_cloudflared_rewrites_host():
    with ctx(LOOPBACK, dict(TUNNEL_HEADERS, Host="localhost:5560")):
        assert not netctx.is_local()


def test_a_foreign_host_on_loopback_is_not_local():
    with ctx(LOOPBACK, {"Host": "evil.example"}):
        assert not netctx.is_local()


def test_a_lan_client_is_not_local():
    with ctx(LAN, {"Host": "localhost:5560"}):
        assert not netctx.is_local()


def test_ipv4_mapped_loopback_is_loopback():
    assert netctx.is_loopback_ip("::ffff:127.0.0.1")
    assert not netctx.is_loopback_ip("localhost") and not netctx.is_loopback_ip(None)


# ── the cross-site rule ─────────────────────────────────────────────────────

def test_a_browser_post_through_the_tunnel_is_same_origin():
    h = dict(TUNNEL_HEADERS, Origin="https://gc.asaplabs.net", **{"Sec-Fetch-Site": "same-origin"})
    with ctx(LOOPBACK, h, method="POST"):
        assert not netctx.is_cross_site()


def test_a_foreign_origin_through_the_tunnel_is_refused():
    h = dict(TUNNEL_HEADERS, Origin="https://evil.example")
    with ctx(LOOPBACK, h, method="POST"):
        assert netctx.is_cross_site()


def test_http_origin_through_the_tunnel_is_refused():
    h = dict(TUNNEL_HEADERS, Origin="http://gc.asaplabs.net")
    with ctx(LOOPBACK, h, method="POST"):
        assert netctx.is_cross_site()


def test_origin_on_another_host_is_refused_even_if_it_is_the_hub_url():
    """Rev 2 dropped the "Origin == public_url on another Host" acceptance."""
    h = {"Host": "asapsv1:5560", "Origin": "https://gc.asaplabs.net"}
    with ctx(LAN, h, method="POST"):
        assert netctx.is_cross_site()


def test_lan_same_origin_passes_and_default_ports_normalise():
    with ctx(LAN, {"Host": "asapsv1:5560", "Origin": "http://ASAPSV1:5560"}, method="POST"):
        assert not netctx.is_cross_site()
    with ctx(LAN, {"Host": "asapsv1", "Origin": "http://asapsv1:80"}, method="POST"):
        assert not netctx.is_cross_site()


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_sec_fetch_site_cross_is_refused(site):
    with ctx(LAN, {"Host": "asapsv1:5560", "Sec-Fetch-Site": site}, method="POST"):
        assert netctx.is_cross_site()


def test_null_origin_and_no_headers():
    with ctx(LAN, {"Host": "asapsv1:5560", "Origin": "null"}, method="POST"):
        assert netctx.is_cross_site()
    with ctx(LAN, {"Host": "asapsv1:5560"}, method="POST"):
        assert not netctx.is_cross_site()


# ── throttle keys and the https redirect ────────────────────────────────────

def test_throttle_key_groups_ipv6_by_64():
    assert netctx.throttle_key("2001:db8:1:2:aaaa::1") == netctx.throttle_key("2001:db8:1:2::ffff")
    assert netctx.throttle_key("2001:db8:1:3::1") != netctx.throttle_key("2001:db8:1:2::1")
    assert netctx.throttle_key("203.0.113.9") == "203.0.113.9"
    assert netctx.throttle_key("::ffff:10.0.0.1") == "10.0.0.1"
    assert netctx.throttle_key(None) == "?"


def test_https_redirect_url():
    with ctx(LOOPBACK, {"Host": "gc.asaplabs.net", "CF-Ray": "x", "X-Forwarded-Proto": "http"},
             url="/instruments?x=1"):
        assert netctx.https_url() == "https://gc.asaplabs.net/instruments?x=1"


# ── the forwarded scheme: X-Forwarded-Proto, else CF-Visitor; never guessed ──

def test_cf_visitor_says_https_when_x_forwarded_proto_is_missing():
    with ctx(LOOPBACK, {"Host": "gc.asaplabs.net", "CF-Ray": "x",
                        "CF-Visitor": '{"scheme":"https"}'}):
        assert netctx.forwarded_scheme() == "https" and netctx.is_https()
    with ctx(LOOPBACK, {"Host": "gc.asaplabs.net", "CF-Ray": "x",
                        "CF-Visitor": '{"scheme":"http"}'}):
        assert netctx.forwarded_scheme() == "http" and not netctx.is_https()


def test_an_unknown_forwarded_scheme_is_none_not_http():
    """No scheme header at all: not https (no Secure cookie), but not known to
    be http either, so the 308 rule can't loop."""
    with ctx(LOOPBACK, {"Host": "gc.asaplabs.net", "CF-Ray": "x"}):
        assert netctx.forwarded_scheme() is None and not netctx.is_https()
    with ctx(LOOPBACK, {"Host": "gc.asaplabs.net", "CF-Ray": "x", "CF-Visitor": "garbage"}):
        assert netctx.forwarded_scheme() is None


def test_forwarded_scheme_is_ignored_from_an_untrusted_peer():
    with ctx(LAN, {"X-Forwarded-Proto": "https", "CF-Visitor": '{"scheme":"https"}'}):
        assert netctx.forwarded_scheme() is None and not netctx.is_https()


@pytest.mark.parametrize("header", ["CF-Visitor", "CDN-Loop"])
def test_more_cloudflare_headers_make_it_not_local(header):
    with ctx(LOOPBACK, {"Host": "localhost:5560", header: "cloudflare"}):
        assert not netctx.is_local() and netctx.is_proxied()
