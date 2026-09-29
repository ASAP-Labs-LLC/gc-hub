"""D10: ``lem_machines`` fetches LEM's public machine list for the Instruments
page's dropdown. Tested against a local HTTP server stub (never the real
LEM): live, the 60 s cache, the last good answer served stale on failure,
unavailable, malformed bodies, the 1 MB cap, the timeout, redirects and
concurrent callers sharing one fetch."""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import lem_machines  # noqa: E402

GOOD = {
    "labcore_online": True, "stale": False,
    "machines": [
        {"machine_uid": "3afa991a66e9", "title": "Agilent GC 2", "status": "UNKNOWN",
         "closed_reason": "", "effective_specs": [{"x": 1}]},
        {"machine_uid": "bf8e64b59f12", "title": "Agilent GC 1", "status": "RED",
         "closed_reason": ""},
        {"machine_uid": "5821e25b90d1", "title": "aquamax 1", "status": "GREEN",
         "closed_reason": "Sold"},
    ],
}


class Stub:
    """A local LEM stand-in. ``mode`` picks the answer; ``hits`` counts
    requests to /api/machines."""

    def __init__(self):
        self.mode = "good"
        self.body = GOOD
        self.delay = 0.0
        self.hits = 0
        self.paths = []
        self.headers = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                stub.paths.append(self.path)
                stub.headers.append(dict(self.headers))
                if "Python-urllib" in (self.headers.get("User-Agent") or ""):
                    # what Cloudflare in front of lem.asaplabs.net does
                    return self._send(403, b"error code: 1010", "text/plain")
                if self.path == "/elsewhere":
                    return self._send(200, json.dumps(GOOD).encode())
                stub.hits += 1
                if stub.delay:
                    time.sleep(stub.delay)
                mode = stub.mode
                if mode == "good":
                    return self._send(200, json.dumps(stub.body).encode())
                if mode == "500":
                    return self._send(500, b"boom at /internal/secret")
                if mode == "cf403":
                    return self._send(403, b"error code: 1010", "text/plain")
                if mode == "cf_mitigated":
                    return self._send(200, json.dumps(GOOD).encode(),
                                      extra={"cf-mitigated": "challenge"})
                if mode == "notjson":
                    return self._send(200, b"<html>nope</html>")
                if mode == "raw":
                    return self._send(200, stub.body)
                if mode == "big":
                    return self._send(200, b" " * (lem_machines.MAX_BYTES + 10))
                if mode == "big_nolength":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b" " * (lem_machines.MAX_BYTES + 10))
                    return None
                if mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                raise AssertionError(mode)

            def _send(self, code, data, ctype="application/json", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *a: None   # a client that hung up early
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,),
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def stub():
    s = Stub()
    yield s
    s.close()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _cache(clock=None, **kw):
    return lem_machines.MachineCache(clock=clock or Clock(), **kw)


def _no_internals(error: str, url: str):
    assert error and len(error) <= 80
    assert url not in error and "127.0.0.1" not in error and "http" not in error
    assert "secret" not in error and "Traceback" not in error


# ── live, cache, stale, unavailable ─────────────────────────────────────────

def test_live_answer_is_normalised_and_sorted_by_title(stub):
    out = _cache().get(stub.url)
    assert out["source"] == "live" and out["age_seconds"] == 0
    assert "error" not in out
    assert (out["stale"], out["labcore_online"]) == (False, True)
    assert out["machines"] == [
        {"uid": "bf8e64b59f12", "title": "Agilent GC 1", "status": "RED", "closed": False},
        {"uid": "3afa991a66e9", "title": "Agilent GC 2", "status": "UNKNOWN", "closed": False},
        {"uid": "5821e25b90d1", "title": "aquamax 1", "status": "GREEN", "closed": True},
    ]
    assert stub.paths == ["/api/machines"]


def test_request_headers(stub):
    import version
    _cache().get(stub.url)
    sent = stub.headers[0]
    assert sent["User-Agent"] == f"gc-hub/{version.APP_VERSION}"
    assert sent["Accept"] == "application/json"


def test_lems_flags_are_passed_through(stub):
    stub.body = dict(GOOD, stale=True, labcore_online=False)
    out = _cache().get(stub.url)
    assert (out["stale"], out["labcore_online"]) == (True, False)
    stub.body = {"machines": [], "stale": "yes", "labcore_online": 1}   # not booleans
    out = lem_machines.MachineCache().get(stub.url)
    assert (out["stale"], out["labcore_online"]) == (None, None)


@pytest.mark.parametrize("mode", ["cf403", "cf_mitigated"])
def test_cloudflare_refusals_are_unavailable(stub, mode):
    stub.mode = mode
    out = _cache().get(stub.url)
    assert out["source"] == "unavailable" and out["machines"] == []
    _no_internals(out["error"], stub.url)
    assert "1010" not in out["error"] and "challenge" not in out["error"]


def test_answer_is_cached_for_60_seconds(stub):
    clock = Clock()
    cache = _cache(clock)
    cache.get(stub.url)
    clock.t += 59
    out = cache.get(stub.url)
    assert stub.hits == 1
    assert out["source"] == "live" and out["age_seconds"] == 59
    clock.t += 2
    cache.get(stub.url)
    assert stub.hits == 2


def test_last_good_answer_is_served_stale_when_lem_fails(stub):
    clock = Clock()
    cache = _cache(clock)
    good = cache.get(stub.url)["machines"]
    stub.mode = "500"
    clock.t += 125
    out = cache.get(stub.url)
    assert stub.hits == 2
    assert out["source"] == "cached" and out["machines"] == good
    assert out["age_seconds"] == 125
    _no_internals(out["error"], stub.url)


def test_unavailable_when_lem_never_answered(stub):
    stub.mode = "500"
    out = _cache().get(stub.url)
    assert out["source"] == "unavailable" and out["machines"] == []
    assert out["age_seconds"] is None
    _no_internals(out["error"], stub.url)
    assert (out["stale"], out["labcore_online"]) == (None, None)


def test_unavailable_when_nothing_listens():
    s = Stub()
    url = s.url
    s.close()
    out = _cache().get(url)
    assert out["source"] == "unavailable"
    _no_internals(out["error"], url)


def test_a_changed_url_does_not_serve_the_other_lems_list(stub):
    cache = _cache()
    cache.get(stub.url)
    other = Stub()
    other.mode = "500"
    try:
        out = cache.get(other.url)
    finally:
        other.close()
    assert out["source"] == "unavailable" and out["machines"] == []


# ── malformed and hostile answers ───────────────────────────────────────────

@pytest.mark.parametrize("mode,body", [
    ("notjson", None),
    ("good", {"machines": "nope"}),
    ("good", ["not", "an", "object"]),
    ("good", {"no_machines": []}),
    ("raw", b"\xff\xfe not utf-8"),
])
def test_malformed_body_is_unavailable_and_never_cached(stub, mode, body):
    stub.mode = mode
    if body is not None:
        stub.body = body
    clock = Clock()
    cache = _cache(clock)
    out = cache.get(stub.url)
    assert out["source"] == "unavailable" and out["machines"] == []
    _no_internals(out["error"], stub.url)
    # a failure is not cached as an answer: after the 30 s back-off the next
    # call tries again
    stub.mode, stub.body = "good", GOOD
    clock.t += lem_machines.RETRY_AFTER_FAILURE_SECONDS + 1
    assert cache.get(stub.url)["source"] == "live"


def test_bad_entries_are_dropped_and_fields_capped(stub):
    stub.body = {"machines": [
        "text", None, 5,
        {"machine_uid": 12, "title": "numeric uid"},
        {"title": "no uid"},
        {"machine_uid": "", "title": "empty uid"},
        {"machine_uid": "u" * 129, "title": "too long a uid"},
        {"machine_uid": "has space", "title": "x"},
        {"machine_uid": "<b>x</b>", "title": "x"},
        {"machine_uid": " padded", "title": "x"},
        {"machine_uid": "u" * 128, "title": "t" * 500, "status": "S" * 100,
         "closed_reason": None},
        {"machine_uid": "ok1", "title": None, "status": 7, "closed_reason": 3},
        {"machine_uid": "ok1", "title": "duplicate uid"},
        {"machine_uid": "gone_1-A", "title": "Old GC", "status": "RED", "closed_reason": "Sold"},
        {"machine_uid": "st", "title": "status is not closure", "status": "CLOSED"},
    ]}
    out = _cache().get(stub.url)
    assert out["source"] == "live"
    by_uid = {m["uid"]: m for m in out["machines"]}
    assert set(by_uid) == {"u" * 128, "ok1", "gone_1-A", "st"}
    long = by_uid["u" * 128]
    assert (len(long["title"]), len(long["status"]), long["closed"]) == (200, 32, False)
    assert by_uid["ok1"] == {"uid": "ok1", "title": "", "status": "", "closed": True}
    assert by_uid["gone_1-A"]["closed"] is True
    assert by_uid["st"]["closed"] is False
    for m in out["machines"]:
        assert set(m) == {"uid", "title", "status", "closed"}


@pytest.mark.parametrize("mode", ["big", "big_nolength"])
def test_oversize_answer_is_refused(stub, mode):
    stub.mode = mode
    out = _cache().get(stub.url)
    assert out["source"] == "unavailable"
    _no_internals(out["error"], stub.url)


def test_redirects_are_not_followed(stub):
    stub.mode = "redirect"
    out = _cache().get(stub.url)
    assert out["source"] == "unavailable"
    assert "/elsewhere" not in stub.paths


def test_timeout(stub):
    stub.delay = 1.0
    t0 = time.monotonic()
    out = _cache(timeout=0.2).get(stub.url)
    assert time.monotonic() - t0 < 0.9
    assert out["source"] == "unavailable"
    _no_internals(out["error"], stub.url)


def test_defaults():
    assert lem_machines.TIMEOUT_SECONDS == 8
    assert lem_machines.MAX_BYTES == 1024 * 1024
    assert lem_machines.CACHE_SECONDS == 60
    assert lem_machines.DEFAULT_URL == "https://lem.asaplabs.net"


# ── concurrency: one fetch in flight ────────────────────────────────────────

def test_concurrent_callers_share_one_fetch(stub):
    stub.delay = 0.3
    cache = lem_machines.MachineCache()
    results = []
    barrier = threading.Barrier(8)

    def call():
        barrier.wait()
        results.append(cache.get(stub.url))

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert stub.hits == 1
    assert len(results) == 8
    assert all(r["source"] == "live" and len(r["machines"]) == 3 for r in results)


def test_concurrent_callers_share_one_failure(stub):
    stub.delay = 0.3
    stub.mode = "500"
    cache = lem_machines.MachineCache()
    results = []
    barrier = threading.Barrier(6)

    def call():
        barrier.wait()
        results.append(cache.get(stub.url))

    threads = [threading.Thread(target=call) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert stub.hits == 1
    assert [r["source"] for r in results] == ["unavailable"] * 6


# ── the lem_url setting ─────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://lem.asaplabs.net", "http://lem.asaplabs.net", "http://127.0.0.1:8080",
    "https://lem:443", "http://asapsv1",
])
def test_valid_urls(url):
    assert lem_machines.valid_url(url)


@pytest.mark.parametrize("url", [
    "", "lem.asaplabs.net", "ftp://lem.asaplabs.net", "https://lem.asaplabs.net/",
    "https://lem.asaplabs.net/api", "https://user:pw@lem.asaplabs.net",
    "https://lem.asaplabs.net?x=1", "https://lem.asaplabs.net#x", "https://lem:0",
    "https://lem:70000", "https://", "https://-bad-.net", "https://lem .net",
    " https://lem.asaplabs.net", "https://lem.asaplabs.net\n", None, 5,
])
def test_invalid_urls(url):
    assert not lem_machines.valid_url(url)


def test_resolve_url_env_wins_then_setting_then_default():
    assert lem_machines.resolve_url({}, env={}) == "https://lem.asaplabs.net"
    assert lem_machines.resolve_url({"lem_url": "http://a:1"}, env={}) == "http://a:1"
    assert lem_machines.resolve_url({"lem_url": "http://a:1"},
                                    env={"LEM_URL": "http://b:2"}) == "http://b:2"
    # an invalid value is ignored, never fetched
    assert lem_machines.resolve_url({"lem_url": "http://a/x"}, env={}) == "https://lem.asaplabs.net"
    assert lem_machines.resolve_url({"lem_url": "http://a:1"},
                                    env={"LEM_URL": "file:///etc"}) == "http://a:1"
    # the setting may not name this machine or a link-local host; LEM_URL
    # (set by whoever runs the hub) may
    assert lem_machines.resolve_url({"lem_url": "http://127.0.0.1:8080"},
                                    env={}) == "https://lem.asaplabs.net"
    assert lem_machines.resolve_url({"lem_url": "http://a:1"},
                                    env={"LEM_URL": "http://127.0.0.1:8080"}) == "http://127.0.0.1:8080"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1", "http://127.0.0.1:8080", "http://127.1", "http://2130706433",
    "http://0x7f.1", "http://localhost", "http://LOCALHOST:5560", "http://lem.localhost",
    "http://0", "http://0.0.0.0", "http://169.254.169.254", "http://169.254.1.1:80",
])
def test_setting_refuses_local_hosts(url):
    assert lem_machines.valid_url(url)          # well-formed ...
    assert not lem_machines.valid_setting_url(url)   # ... but not for the setting


@pytest.mark.parametrize("url", [
    "https://lem.asaplabs.net", "http://10.0.0.5:8080", "http://192.168.1.20", "http://asapsv1",
])
def test_setting_accepts_lan_and_public_hosts(url):
    assert lem_machines.valid_setting_url(url)


def test_setting_validator_rejects_malformed():
    assert not lem_machines.valid_setting_url("https://lem.asaplabs.net/x")
    assert not lem_machines.valid_setting_url(None)


def test_stdlib_only():
    import ast
    tree = ast.parse((ROOT / "lem_machines.py").read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    stdlib = set(sys.stdlib_module_names) | {"__future__", "version"}
    assert mods <= stdlib, mods - stdlib


# ── a hard deadline (critic: trickling servers) ─────────────────────────────
# Raw-socket servers that send one byte every 0.2 s: the socket timeout
# resets on every byte, so only a real deadline ends the fetch.

class RawServer:
    def __init__(self, handler):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.stop = threading.Event()
        self.connections = 0
        self.handler = handler
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            c.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = c.recv(4096)
                if not d:
                    return
                buf += d
            self.handler(self, c)
        except OSError:
            pass
        finally:
            c.close()

    def trickle(self, c, piece):
        while not self.stop.is_set():
            c.sendall(piece)
            time.sleep(0.2)

    def close(self):
        self.stop.set()
        self.sock.close()


def trickle_body(srv, c):
    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
              b"Content-Length: 100000\r\n\r\n")
    srv.trickle(c, b" ")


def trickle_chunked(srv, c):
    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
              b"Transfer-Encoding: chunked\r\n\r\n")
    srv.trickle(c, b"1\r\n \r\n")


def trickle_headers(srv, c):
    c.sendall(b"HTTP/1.1 200 OK\r\n")
    srv.trickle(c, b"X")


def send_body(body: bytes, extra: bytes = b""):
    def handler(srv, c):
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                  % len(body) + extra + b"\r\n" + body)
    return handler


@pytest.fixture()
def raw():
    servers = []

    def make(handler):
        s = RawServer(handler)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


TIMEOUT = 1.0
MARGIN = 0.8
TRICKLERS = pytest.mark.parametrize("handler", [trickle_body, trickle_chunked, trickle_headers],
                                    ids=["body-content-length", "body-chunked", "headers"])


@TRICKLERS
def test_fetch_ends_within_the_timeout(raw, handler):
    srv = raw(handler)
    t0 = time.monotonic()
    with pytest.raises(Exception):
        lem_machines.fetch(srv.url, timeout=TIMEOUT)
    assert time.monotonic() - t0 < TIMEOUT + MARGIN


@TRICKLERS
def test_cache_answers_within_the_timeout_and_is_not_stuck(raw, handler):
    srv = raw(handler)
    clock = Clock()
    cache = lem_machines.MachineCache(timeout=TIMEOUT, clock=clock)
    t0 = time.monotonic()
    out = cache.get(srv.url)
    assert time.monotonic() - t0 < TIMEOUT + MARGIN
    assert out["source"] == "unavailable"
    _no_internals(out["error"], srv.url)
    # the stuck fetch no longer blocks anyone: within the back-off the answer
    # is immediate; after it, a new fetch is tried (and ends in time too)
    t0 = time.monotonic()
    assert cache.get(srv.url)["source"] == "unavailable"
    assert time.monotonic() - t0 < 0.2
    clock.t += lem_machines.RETRY_AFTER_FAILURE_SECONDS + 1
    t0 = time.monotonic()
    assert cache.get(srv.url)["source"] == "unavailable"
    assert time.monotonic() - t0 < TIMEOUT + MARGIN
    assert srv.connections == 2


def test_many_callers_behind_a_stuck_fetch_all_return_in_time(raw):
    srv = raw(trickle_body)
    cache = lem_machines.MachineCache(timeout=TIMEOUT)
    times = []

    def call():
        t0 = time.monotonic()
        cache.get(srv.url)
        times.append(time.monotonic() - t0)

    threads = [threading.Thread(target=call, daemon=True) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(times) == 20 and max(times) < TIMEOUT + MARGIN
    assert srv.connections == 1


def test_a_caller_for_another_url_does_not_wait_forever(raw, stub):
    slow = raw(trickle_headers)
    cache = lem_machines.MachineCache(timeout=TIMEOUT)
    first = threading.Thread(target=cache.get, args=(slow.url,), daemon=True)
    first.start()
    time.sleep(0.1)                     # the slow fetch is in flight
    t0 = time.monotonic()
    out = cache.get(stub.url)
    assert time.monotonic() - t0 < TIMEOUT + MARGIN + 0.5
    assert out["source"] in ("live", "unavailable")
    first.join(5)


# ── stale while in flight; back-off after a failure ─────────────────────────

def test_cached_answer_is_served_at_once_while_a_refresh_is_in_flight(stub):
    clock = Clock()
    cache = lem_machines.MachineCache(clock=clock)
    assert cache.get(stub.url)["source"] == "live"
    clock.t += 61
    stub.delay = 0.6
    refresher = threading.Thread(target=cache.get, args=(stub.url,), daemon=True)
    refresher.start()
    time.sleep(0.15)                    # the refresh is in flight
    t0 = time.monotonic()
    out = cache.get(stub.url)
    assert time.monotonic() - t0 < 0.2
    assert out["source"] == "cached" and len(out["machines"]) == 3
    refresher.join(5)
    assert stub.hits == 2


def test_cached_answer_is_served_at_once_for_30_s_after_a_failure(stub):
    clock = Clock()
    cache = lem_machines.MachineCache(clock=clock)
    cache.get(stub.url)
    clock.t += 61
    stub.mode = "500"
    assert cache.get(stub.url)["source"] == "cached"
    assert stub.hits == 2
    clock.t += 29
    out = cache.get(stub.url)
    assert out["source"] == "cached" and out["error"] and stub.hits == 2
    clock.t += 2
    cache.get(stub.url)
    assert stub.hits == 3


def test_without_a_cache_a_failure_backs_off_for_30_s(stub):
    clock = Clock()
    cache = lem_machines.MachineCache(clock=clock)
    stub.mode = "500"
    assert cache.get(stub.url)["source"] == "unavailable"
    stub.mode = "good"
    clock.t += 29
    out = cache.get(stub.url)
    assert out["source"] == "unavailable" and out["error"] and stub.hits == 1
    clock.t += 2
    assert cache.get(stub.url)["source"] == "live" and stub.hits == 2


# ── hostile payloads (critic) ───────────────────────────────────────────────

@pytest.mark.parametrize("body", [b"[" * 200000, b'{"machines":' + b"[" * 200000],
                         ids=["deep-array", "deep-machines"])
def test_deeply_nested_json_could_not_be_read(raw, body):
    srv = raw(send_body(body))
    out = lem_machines.MachineCache().get(srv.url)
    assert out["source"] == "unavailable"
    assert out["error"] == lem_machines.ERR_UNREADABLE


def test_the_list_is_capped(raw):
    body = json.dumps({"machines": [{"machine_uid": "u%d" % i, "title": "t%05d" % i}
                                    for i in range(3000)]}).encode()
    srv = raw(send_body(body))
    out = lem_machines.MachineCache().get(srv.url)
    assert out["source"] == "live"
    assert len(out["machines"]) == lem_machines.MAX_MACHINES == 500


def test_control_format_and_surrogate_characters_are_stripped(raw):
    title = "‮evil​\ud800 <b>x</b>\x00\x1bé \U0001F600"
    body = json.dumps({"machines": [{"machine_uid": "x1", "title": title,
                                     "status": "RE‍D\x07"}]}).encode()
    srv = raw(send_body(body))
    m = lem_machines.MachineCache().get(srv.url)["machines"][0]
    assert m["title"] == "evil <b>x</b>é \U0001F600"
    assert m["status"] == "RED"
    m["title"].encode("utf-8")          # no lone surrogate left


def test_short_body_is_a_failure(raw):
    def handler(srv, c):
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{\"machines\": []}")
    srv = raw(handler)
    assert lem_machines.MachineCache().get(srv.url)["source"] == "unavailable"


def test_no_proxy_is_used(stub, monkeypatch):
    # a system proxy would see (or answer for) LEM's traffic; the hub goes direct
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    assert lem_machines.MachineCache().get(stub.url)["source"] == "live"


def test_tests_never_reach_the_real_lem():
    import os
    assert os.environ.get("LEM_URL") == "http://127.0.0.1:9"
