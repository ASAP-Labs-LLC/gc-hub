"""LemProvider over real HTTP, with its default (`requests`) getter.

The injectable-getter tests pin the rules; these pin the wire: that the
default getter turns a real sign-in page, a real 503, a real stall and a
refused connection into the right outcome. The fake serves exactly what LEM
serves (`LEM Web Server/web_app.py` `api_get_corrections` and
`_labcore_unreadable`), captured from the live server on 2026-09-28.
"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("requests")

import corrections as C  # noqa: E402

AGILENT_GC_1 = "bf8e64b59f12"
LONG = "ASTM D2887/D86 - Distillation in Petroleum Products, {}"
# Agilent GC 1's real methods in LEM (read-only, 2026-09-28).
REAL_METHODS = [LONG.format(s) for s in ("10% Recovery", "50% Recovery", "90% Recovery",
                                         "FBP", "IBP")]
REAL_MAP = {LONG.format("IBP"): "IBP", LONG.format("10% Recovery"): "10%",
            LONG.format("50% Recovery"): "50%", LONG.format("90% Recovery"): "90%",
            LONG.format("FBP"): "FBP"}


class FakeLem(BaseHTTPRequestHandler):
    machines = {}            # uid -> {"corrections": [...], "methods": [...]}
    mode = "ok"              # ok | signin | labcore-down | stall
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        FakeLem.seen.append(self.path)
        if FakeLem.mode == "stall":
            time.sleep(1.5)
        if FakeLem.mode == "signin":
            return self._send(200, "text/html; charset=utf-8",
                              b"<!doctype html><title>Sign in</title><form>...</form>")
        if FakeLem.mode == "labcore-down":
            return self._send(503, "application/json", json.dumps({
                "error": "This equipment's correction factors could not be read — "
                         "LabCore did not answer. This is not an empty result; try "
                         "again in a moment.",
                "detail": "timed out", "retry": True, "labcore": "unavailable"}).encode())
        parts = self.path.split("/")
        if len(parts) == 5 and parts[1:3] == ["api", "machines"] and parts[4] == "corrections":
            body = FakeLem.machines.get(parts[3], {"corrections": [], "methods": []})
            return self._send(200, "application/json", json.dumps(body).encode())
        return self._send(404, "text/html", b"<h1>Not Found</h1>")

    def _send(self, status, ctype, body):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
def server():
    FakeLem.machines = {}
    FakeLem.mode = "ok"
    FakeLem.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLem)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def gc(uid=AGILENT_GC_1, correction_map=None, inst="gc1"):
    return {"id": inst, "lem_machine_uid": uid, "correction_map": correction_map}


def test_default_map_with_a_saved_offset(server):
    FakeLem.machines[AGILENT_GC_1] = {
        "corrections": [{"test_name": LONG.format("IBP"), "correction": -12.08,
                         "units": "°C"}],
        "methods": REAL_METHODS}
    got = C.LemProvider(server, C.MemoryCacheStore()).get(gc())
    assert got.source == "lem"
    assert got.values == {"IBP": -12.08, "10%": 0.0, "50%": 0.0, "90%": 0.0, "FBP": 0.0}
    assert FakeLem.seen == [f"/api/machines/{AGILENT_GC_1}/corrections"]


def test_todays_real_agilent_answer_with_the_default_map(server):
    """Exactly what LEM serves for Agilent GC 1 today: no corrections, and the
    long method names, which are the default map's names."""
    FakeLem.machines[AGILENT_GC_1] = {"corrections": [], "methods": REAL_METHODS}
    got = C.LemProvider(server, C.MemoryCacheStore()).get(gc())
    assert got.values == {"IBP": 0.0, "10%": 0.0, "50%": 0.0, "90%": 0.0, "FBP": 0.0}
    assert C.DEFAULT_LEM_CORRECTION_MAP == REAL_MAP


def test_the_phase1_names_against_real_lem_are_config(server):
    FakeLem.machines[AGILENT_GC_1] = {"corrections": [], "methods": REAL_METHODS}
    with pytest.raises(C.CorrectionsUnavailable) as info:
        C.LemProvider(server, C.MemoryCacheStore()).get(
            gc(correction_map=json.dumps(C.PHASE1_FILE_MAP)))
    assert info.value.kind == "config"


def test_unknown_machine(server):
    with pytest.raises(C.CorrectionsUnavailable) as info:
        C.LemProvider(server, C.MemoryCacheStore()).get(gc(uid="nosuchmachine"))
    assert info.value.kind == "config"


def test_a_wrong_lem_url_is_config(server):
    with pytest.raises(C.CorrectionsUnavailable) as info:
        C.LemProvider(server + "/not-lem", C.MemoryCacheStore()).get(gc())
    assert info.value.kind == "config"


def _cached_provider(server, **kw):
    FakeLem.machines[AGILENT_GC_1] = {
        "corrections": [{"test_name": LONG.format("FBP"), "correction": -5.57, "units": "C"}],
        "methods": REAL_METHODS}
    store = C.MemoryCacheStore()
    first = C.LemProvider(server, store, **kw).get(gc())
    return C.LemProvider(server, store, **kw), first


@pytest.mark.parametrize("mode", ["signin", "labcore-down"])
def test_signin_page_and_503_fall_back_to_the_cache(server, mode):
    provider, first = _cached_provider(server)
    FakeLem.mode = mode
    got = provider.refresh(gc())
    assert got.source == "cache"
    assert got.values == first.values and got.fetched_at == first.fetched_at


@pytest.mark.parametrize("mode", ["signin", "labcore-down"])
def test_signin_page_and_503_without_a_cache(server, mode):
    FakeLem.mode = mode
    with pytest.raises(C.CorrectionsUnavailable) as info:
        C.LemProvider(server, C.MemoryCacheStore()).get(gc())
    assert info.value.kind == "unreachable"
    if mode == "labcore-down":
        assert "LabCore did not answer" in info.value.reason


def test_a_stall_past_the_timeout_is_unreachable(server):
    provider, _ = _cached_provider(server, timeout=0.3)
    FakeLem.mode = "stall"
    started = time.monotonic()
    got = provider.refresh(gc())
    assert got.source == "cache"
    assert time.monotonic() - started < 1.4


def test_connection_refused_is_unreachable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                  # nothing listens here now
    with pytest.raises(C.CorrectionsUnavailable) as info:
        C.LemProvider(f"http://127.0.0.1:{port}", C.MemoryCacheStore(), timeout=2).get(gc())
    assert info.value.kind == "unreachable"
