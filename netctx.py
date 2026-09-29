"""Who is asking, and over what: the hub behind the Cloudflare tunnel.

Production is also reached as https://gc.asaplabs.net: ``cloudflared`` on the
server forwards to http://localhost:5560, so such a request arrives from
loopback, over plain http, with ``Host: gc.asaplabs.net`` and Cloudflare's
``CF-Connecting-IP`` / ``CF-Ray`` / ``X-Forwarded-For`` /
``X-Forwarded-Proto: https`` headers. The only trusted proxy is loopback:
forwarding headers from any other address are ignored.

Every security decision about the client's address, the scheme or "is this
the server's own console" goes through here. Stdlib + flask's ``request``.
"""
from __future__ import annotations

import ipaddress
import urllib.parse
from typing import Optional

from flask import request

# Public names the hub answers to through the tunnel (https only).
PUBLIC_HOSTS = frozenset({"gc.asaplabs.net"})

PROXY_HEADERS = ("CF-Connecting-IP", "CF-Ray", "X-Forwarded-For", "X-Forwarded-Proto",
                 "Forwarded")

_DEFAULT_PORTS = {"http": 80, "https": 443}
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


def _ip(value: Optional[str]):
    """An ``ip_address`` (IPv4-mapped IPv6 unwrapped) or ``None``."""
    try:
        ip = ipaddress.ip_address((value or "").strip().split("%")[0])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def is_loopback_addr(addr: Optional[str]) -> bool:
    ip = _ip(addr)
    return ip is not None and ip.is_loopback


def hostname(host: Optional[str]) -> Optional[str]:
    """The lower-case name of a ``Host`` value, or ``None``."""
    if not isinstance(host, str) or not host.strip():
        return None
    try:
        parts = urllib.parse.urlsplit("//" + host.strip())
        name = parts.hostname
        parts.port   # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return None
    return name.lower().rstrip(".") if name else None


def _from_trusted_proxy() -> bool:
    return is_loopback_addr(request.remote_addr)


def client_ip() -> Optional[str]:
    """The real client: ``CF-Connecting-IP`` when the request came from the
    loopback proxy and the header is a valid IP, else ``remote_addr``."""
    remote = request.remote_addr
    if is_loopback_addr(remote):
        ip = _ip(request.headers.get("CF-Connecting-IP"))
        if ip is not None:
            return str(ip)
    return remote


def is_https() -> bool:
    """TLS to the hub, or ``X-Forwarded-Proto: https`` from the loopback proxy."""
    if request.is_secure:
        return True
    proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return proto == "https" and _from_trusted_proxy()


def via_proxy() -> bool:
    """Any forwarding header is present (from anywhere)."""
    return any(request.headers.get(h) is not None for h in PROXY_HEADERS)


def host_is_loopback(host: Optional[str]) -> bool:
    name = hostname(host)
    return name is not None and (name == "localhost" or is_loopback_addr(name))


def is_local() -> bool:
    """The server's own console: loopback client, no proxy headers, loopback Host."""
    return (is_loopback_addr(request.remote_addr) and not via_proxy()
            and host_is_loopback(request.host))


def public_host() -> Optional[str]:
    """The request's Host when it is one of ``PUBLIC_HOSTS`` over https, else None."""
    name = hostname(request.host)
    return name if name in PUBLIC_HOSTS and is_https() else None


def scheme() -> str:
    """The scheme the browser used: https behind the tunnel."""
    return "https" if is_https() else request.scheme


def _host_port(netloc: str, sch: str):
    try:
        parts = urllib.parse.urlsplit(f"{sch}://{netloc}")
        host = (parts.hostname or "").lower()
        port = parts.port or _DEFAULT_PORTS.get(sch)
    except ValueError:
        return None
    return (host, port) if host else None


def is_cross_site() -> bool:
    """The cross-site rule shared by app's write guard and admin_auth:
    ``Sec-Fetch-Site`` other than same-origin/none, or an ``Origin`` whose
    (host, port) is not this request's Host under the browser's scheme.
    Requests with neither header pass (not a browser page)."""
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None and site.strip().lower() not in _ALLOWED_FETCH_SITES:
        return True
    origin = request.headers.get("Origin")
    if origin is not None:
        try:
            parts = urllib.parse.urlsplit(origin.strip())
        except ValueError:
            return True
        if parts.scheme not in _DEFAULT_PORTS or not parts.netloc:
            return True  # includes the opaque origin "null"
        mine = _host_port(request.host, scheme())
        return mine is None or _host_port(parts.netloc, parts.scheme) != mine
    return False
