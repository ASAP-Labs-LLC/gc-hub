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
  what the proxy said (None when it said nothing: never guessed);
  ``scheme_problem()`` says why a maybe-https request isn't taken for https
  (no scheme header, or an untrusted proxy), for the refusal's wording only.
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
* ``log_safe(text)`` — request-derived text with its control characters
  escaped, for a log line (``request.path`` is percent-decoded: ``%0A``).

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


def _claimed_scheme(req) -> Optional[str]:
    """What ``X-Forwarded-Proto`` (else ``CF-Visitor``) says, whoever sent
    it. Believed only from a trusted proxy (``forwarded_scheme``); from anyone
    else it only words a refusal (``scheme_problem``)."""
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


def forwarded_scheme(req=None) -> Optional[str]:
    """The scheme a trusted proxy says the client used: ``X-Forwarded-Proto``
    (first value), else Cloudflare's ``CF-Visitor: {"scheme": ...}``; None when
    the peer isn't a trusted proxy or says nothing usable (never guessed)."""
    req = _req(req)
    if not is_trusted_proxy(req.remote_addr):
        return None
    return _claimed_scheme(req)


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


def scheme_problem(req=None) -> Optional[str]:
    """Why a request that may well be https is not taken for https, or None:

    * ``"missing"`` — through a trusted proxy that sent no usable
      ``X-Forwarded-Proto`` or ``CF-Visitor`` (cloudflared not passing them);
    * ``"untrusted"`` — a peer that is not a trusted proxy sent forwarding
      headers claiming https (a cloudflared on another machine missing from
      ``settings.json`` ``trusted_proxies``): rightly ignored.

    Wording only: no decision is made from it."""
    req = _req(req)
    if is_https(req):
        return None
    if is_proxied(req):
        return "missing" if forwarded_scheme(req) is None else None
    if (_has_proxy_headers(req) and not is_trusted_proxy(req.remote_addr)
            and _claimed_scheme(req) == "https"):
        return "untrusted"
    return None


def https_refusal_message(req=None) -> str:
    """What to tell someone who reached the hub through the proxy without an
    https scheme the hub believes: ``Sign in over https: open https://<Host>``,
    plus, when the person may already be on https, what the hub was missing
    and what to check on the server (``scheme_problem``)."""
    req = _req(req)
    name = hostname(req.host) or "gc.asaplabs.net"
    base = f"Sign in over https: open https://{name}"
    problem = scheme_problem(req)
    if problem == "missing":
        return (f"{base}. If you already did, the tunnel did not tell the hub: no "
                f"X-Forwarded-Proto or CF-Visitor header reached it. Check the cloudflared "
                f"tunnel on the server (it must forward those headers to the hub).")
    if problem == "untrusted":
        peer = log_safe(req.remote_addr or "?")
        return (f"{base}. If you already did, the proxy at {peer} is not a trusted proxy, so "
                f"the hub ignored its https header. Add {peer} to trusted_proxies in the hub's "
                f"settings.json on the server.")
    return base


def _only_the_scheme_differs(req) -> bool:
    """The hub takes the request for http but the browser's ``Origin`` is
    exactly ``https://<this Host>``: not another site, just a missing (or
    untrusted) scheme header (``scheme_problem``)."""
    if scheme_problem(req) is None:
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
    scheme header (or through an untrusted proxy claiming https) whose
    ``Origin`` is this host over https is told to use https and what to check
    (``https_refusal_message``), not "Cross-site request refused"."""
    req = _req(req)
    if not is_cross_site(req):
        return None
    if _only_the_scheme_differs(req):
        return https_refusal_message(req)
    return CROSS_SITE_MESSAGE


# ── helpers ─────────────────────────────────────────────────────────────────

def _log_escape(ch: str) -> str:
    code = ord(ch)
    if code < 32 or 0x7F <= code <= 0x9F:
        return f"\\x{code:02x}"
    if code in (0x2028, 0x2029):
        return f"\\u{code:04x}"
    return ch


def log_safe(value: Any) -> str:
    """Request-derived text (a path, a ``Host``, an error text that may echo
    the request) made safe for one log line: C0/C1 control characters, DEL and
    U+2028/U+2029 become ``\\xNN``/``\\uNNNN`` escapes, so a percent-decoded
    ``%0A`` can never start a forged line in app.log. Anything else is kept
    as is; a value that is not text is logged as its ``repr``."""
    if not isinstance(value, str):
        value = repr(value)
    return "".join(_log_escape(ch) for ch in value)


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
