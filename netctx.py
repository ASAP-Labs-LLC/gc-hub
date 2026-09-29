"""netctx.py: who is asking, and how they reached the hub (spec D4, rev 2).

The one place the hub decides these things about a request. Every decision
or record that used ``request.remote_addr``, a loopback test,
``request.scheme`` or ``request.host`` goes through here.

``https://gc.asaplabs.net`` reaches the hub through a Cloudflare tunnel:
cloudflared (on ASAPSV1) connects to ``http://localhost:5560``, so the peer
address is loopback and the scheme is ``http``. Cloudflare tells us the real
client (``CF-Connecting-IP``) and scheme (``X-Forwarded-Proto``), but those
headers are believed only when the peer is a **trusted proxy**: loopback, plus
the addresses/CIDRs in ``settings.json``'s ``trusted_proxies`` (a list, or a
comma-separated string) for a cloudflared on another machine. Anyone else
sending them is just a LAN client with odd headers.

* ``client_ip()``   — ``CF-Connecting-IP`` (normalised) from a trusted proxy,
  else ``remote_addr``.
* ``is_https()``    — the request is TLS, or a trusted proxy says https
  (``X-Forwarded-Proto``, else ``CF-Visitor``); ``forwarded_scheme()`` is
  what the proxy said (None when it said nothing: never guessed).
* ``is_proxied()``  — "through Cloudflare": a trusted proxy peer that sent
  any of ``CF-Connecting-IP``, ``CF-Ray``, ``CF-Visitor``, ``CDN-Loop``,
  ``X-Forwarded-For``, ``Forwarded``, ``X-Forwarded-Proto``.
* ``is_local()``    — the server's own console: loopback peer, none of those
  headers at all, and a loopback ``Host`` (``localhost``/127.x/::1).
  Anything that came through Cloudflare is never local.
* ``is_cross_site()`` — the cross-site write rule (``Sec-Fetch-Site`` other
  than ``same-origin``/``none``, or an ``Origin`` that isn't this request's
  own ``scheme://host:port``, the scheme being ``https`` when ``is_https()``).
  ``app``'s guard and ``admin_auth`` both use it: one origin rule.
* ``throttle_key(ip)`` — IPv6 grouped by /64; IPv4-mapped unwrapped.
* ``https_url()``   — this request's URL with ``https://`` (the 308 target).

Stdlib + Flask's ``request`` only; reads ``settings.json`` directly (cached on
its mtime) so it has no import-time side effects and no store access.
"""
from __future__ import annotations

import ipaddress
import json
import threading
import urllib.parse
from typing import Any, Optional

import paths

PROXY_HEADERS = ("CF-Connecting-IP", "CF-Ray", "CF-Visitor", "CDN-Loop", "X-Forwarded-For",
                 "Forwarded", "X-Forwarded-Proto")
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
DEFAULT_PORTS = {"http": 80, "https": 443}

_proxy_lock = threading.Lock()
_proxy_cache: dict = {}      # {"key": (path, mtime_ns), "nets": [...]}


def _req(req):
    if req is not None:
        return req
    from flask import request
    return request


# ── addresses ───────────────────────────────────────────────────────────────

def parse_ip(value: Any):
    """An ``ipaddress`` object (zone id dropped, IPv4-mapped unwrapped) or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ip = ipaddress.ip_address(value.strip().split("%")[0])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def is_loopback_ip(value: Any) -> bool:
    ip = parse_ip(value)
    return bool(ip is not None and ip.is_loopback)


def _parse_nets(raw: Any) -> list:
    if isinstance(raw, str):
        items = [p for p in raw.replace(";", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [p for p in raw if isinstance(p, str)]
    else:
        items = []
    nets = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return nets


def trusted_proxies() -> list:
    """The extra trusted proxies from ``settings.json`` (``trusted_proxies``),
    re-read when the file changes. Loopback is always trusted on top."""
    d = paths.data_dir()
    if d is None:
        return []
    path = paths.settings_file()
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return []
    with _proxy_lock:
        if _proxy_cache.get("key") == key:
            return _proxy_cache["nets"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        nets = _parse_nets(data.get("trusted_proxies") if isinstance(data, dict) else None)
    except (OSError, ValueError):
        nets = []
    with _proxy_lock:
        _proxy_cache.update(key=key, nets=nets)
    return nets


def is_trusted_proxy(addr: Any) -> bool:
    ip = parse_ip(addr)
    if ip is None:
        return False
    if ip.is_loopback:
        return True
    return any(ip.version == n.version and ip in n for n in trusted_proxies())


def _has_proxy_headers(req) -> bool:
    return any(req.headers.get(h) is not None for h in PROXY_HEADERS)


# ── the four questions ──────────────────────────────────────────────────────

def is_proxied(req=None) -> bool:
    """Through Cloudflare (or another trusted proxy): a trusted peer that
    sent any forwarding header."""
    req = _req(req)
    return is_trusted_proxy(req.remote_addr) and _has_proxy_headers(req)


def client_ip(req=None) -> Optional[str]:
    req = _req(req)
    addr = req.remote_addr
    if is_trusted_proxy(addr):
        ip = parse_ip(req.headers.get("CF-Connecting-IP"))
        if ip is not None:
            return str(ip)
    return addr


def forwarded_scheme(req=None) -> Optional[str]:
    """The scheme a trusted proxy says the client used: ``X-Forwarded-Proto``
    (first value), else Cloudflare's ``CF-Visitor: {"scheme": ...}``; None when
    the peer isn't a trusted proxy or says nothing usable (never guessed)."""
    req = _req(req)
    if not is_trusted_proxy(req.remote_addr):
        return None
    proto = (req.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto in ("http", "https"):
        return proto
    raw = req.headers.get("CF-Visitor")
    if raw:
        try:
            scheme = json.loads(raw).get("scheme")
        except (ValueError, AttributeError):
            scheme = None
        if isinstance(scheme, str) and scheme.lower() in ("http", "https"):
            return scheme.lower()
    return None


def is_https(req=None) -> bool:
    req = _req(req)
    return bool(req.is_secure) or forwarded_scheme(req) == "https"


def hostname(host: Any) -> Optional[str]:
    """The lower-case host name of a ``Host`` header value, or None."""
    if not isinstance(host, str) or not host.strip():
        return None
    try:
        parts = urllib.parse.urlsplit("//" + host.strip())
        name = parts.hostname
        parts.port   # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return None
    return name.lower().rstrip(".") if name else None


def host_is_loopback(host: Any) -> bool:
    name = hostname(host)
    return name is not None and (name == "localhost" or is_loopback_ip(name))


def is_local(req=None) -> bool:
    """The server's own console (RDP + http://localhost:5560, the hub tray):
    loopback peer, no forwarding header at all, loopback Host."""
    req = _req(req)
    return (is_loopback_ip(req.remote_addr) and not _has_proxy_headers(req)
            and host_is_loopback(req.host))


# ── origins ─────────────────────────────────────────────────────────────────

def host_port(netloc: str, scheme: str):
    """``(hostname, port)`` of a ``host[:port]`` string, or None."""
    try:
        parts = urllib.parse.urlsplit(f"{scheme}://{netloc}")
        host = (parts.hostname or "").lower()
        port = parts.port or DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None
    return (host, port) if host else None


def my_scheme(req=None) -> str:
    req = _req(req)
    return "https" if is_https(req) else req.scheme


def is_cross_site(req=None) -> bool:
    req = _req(req)
    site = req.headers.get("Sec-Fetch-Site")
    if site is not None and site.strip().lower() not in ALLOWED_FETCH_SITES:
        return True
    origin = req.headers.get("Origin")
    if origin is None:
        return False
    try:
        parts = urllib.parse.urlsplit(origin.strip())
    except ValueError:
        return True
    if parts.scheme not in DEFAULT_PORTS or not parts.netloc:
        return True      # includes the opaque origin "null"
    mine = host_port(req.host, my_scheme(req))
    return mine is None or host_port(parts.netloc, parts.scheme) != mine


CROSS_SITE_MESSAGE = "Cross-site request refused"


def https_refusal_message(req=None) -> str:
    """What to tell someone who reached the hub through the proxy without an
    https scheme header: ``Sign in over https: open https://<Host>``."""
    req = _req(req)
    name = hostname(req.host) or "gc.asaplabs.net"
    return f"Sign in over https: open https://{name}"


def _only_the_scheme_differs(req) -> bool:
    """Through a trusted proxy that named no scheme (so the hub takes the
    request for http) and the browser's ``Origin`` is exactly
    ``https://<this Host>``: not another site, just a missing header."""
    if not is_proxied(req) or is_https(req) or forwarded_scheme(req) is not None:
        return False
    site = req.headers.get("Sec-Fetch-Site")
    if site is not None and site.strip().lower() not in ALLOWED_FETCH_SITES:
        return False
    origin = req.headers.get("Origin")
    if origin is None:
        return False
    try:
        parts = urllib.parse.urlsplit(origin.strip())
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.netloc:
        return False
    mine = host_port(req.host, "https")
    return mine is not None and host_port(parts.netloc, "https") == mine


def cross_site_refusal(req=None) -> Optional[str]:
    """The cross-site guard's answer: None when the request may proceed, else
    the message for its 403. A request through Cloudflare without an https
    scheme header whose ``Origin`` is this host over https is told to use
    https (``https_refusal_message``), not "Cross-site request refused"."""
    req = _req(req)
    if not is_cross_site(req):
        return None
    if _only_the_scheme_differs(req):
        return https_refusal_message(req)
    return CROSS_SITE_MESSAGE


# ── helpers ─────────────────────────────────────────────────────────────────

def throttle_key(addr: Any) -> str:
    """A throttle bucket for an address: IPv6 by /64, IPv4 as is."""
    ip = parse_ip(addr)
    if ip is None:
        return "?"
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def https_url(req=None) -> str:
    req = _req(req)
    qs = req.query_string.decode("latin-1") if req.query_string else ""
    return f"https://{req.host}{req.path}" + (f"?{qs}" if qs else "")
