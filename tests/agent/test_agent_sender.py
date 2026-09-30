import hashlib
import os
import socket

import pytest

from gc_agent.client import HubClient
from gc_agent.ledger import Ledger
from gc_agent.sender import BACKOFF_MAX, HOLD_SECONDS, Sender


def _queue(tmp_path, lg, name, data, mtime):
    p = tmp_path / "w" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(str(p), (mtime, mtime))
    st = os.stat(str(p))
    lg.add_queued(str(p), st.st_size, st.st_mtime_ns, hashlib.sha256(data).hexdigest())
    return str(p)


@pytest.fixture
def env(tmp_path, hub, clock):
    lg = Ledger(tmp_path / "ledger.db")
    s = Sender(lg, HubClient(hub.url, hub.token, timeout=5), clock=clock)
    return lg, s


def test_201_marks_sent_with_sample_id(tmp_path, hub, clock, env):
    lg, s = env
    p = _queue(tmp_path, lg, "a.CDF", b"AAA", 1000)
    assert s.send_next() == "sent"
    assert lg.counts()["sent"] == 1 and lg.last_sent().path == p
    assert lg.last_sent().sample_id == 1
    assert s.send_next() == "idle"


@pytest.mark.parametrize("status", [200, 201, 202])
def test_success_statuses_mark_sent(tmp_path, hub, env, status):
    lg, s = env
    hub.ingest_script.append((status, None))
    _queue(tmp_path, lg, "a.CDF", b"AAA", 1000)
    assert s.send_next() == "sent"
    assert lg.counts()["sent"] == 1


@pytest.mark.parametrize("status", [200, 201, 202])
def test_success_with_other_sha_is_rejected_not_sent(tmp_path, hub, env, status):
    lg, s = env
    body = {"sample_id": 1, "sha256": "0" * 64, "status": "queued", "duplicate": True,
            "conflict_id": 1}
    hub.ingest_script.append((status, body))
    _queue(tmp_path, lg, "a.CDF", b"AAA", 1000)
    assert s.send_next() == "rejected"
    r = lg.rejected()[0]
    assert "0" * 64 in r.reason and hashlib.sha256(b"AAA").hexdigest() in r.reason
    assert lg.counts()["sent"] == 0


@pytest.mark.parametrize("status", [400, 409, 413, 415])
def test_reject_statuses_mark_rejected_and_continue(tmp_path, hub, env, status):
    lg, s = env
    hub.ingest_script.append((status, {"error": "nope %d" % status}))
    _queue(tmp_path, lg, "a.CDF", b"A", 1000)
    _queue(tmp_path, lg, "b.CDF", b"B", 2000)
    assert s.send_next() == "rejected"
    assert lg.rejected()[0].reason == "HTTP %d: nope %d" % (status, status)
    assert s.send_next() == "sent"                 # the next file still goes
    assert s.status() is None


@pytest.mark.parametrize("status", [401, 403, 404, 405, 418, 451])
def test_hold_statuses_keep_queue_and_wait_300s(tmp_path, hub, clock, env, status):
    lg, s = env
    hub.ingest_script.append((status, {"error": "denied"}))
    _queue(tmp_path, lg, "a.CDF", b"A", 1000)
    assert s.send_next() == "hold"
    assert s.status() == "auth-error" and "denied" in s.last_error
    assert lg.counts()["queued"] == 1
    n = len(hub.by_path("/api/ingest"))
    clock.advance(HOLD_SECONDS - 1)
    assert s.send_next() == "blocked"
    assert len(hub.by_path("/api/ingest")) == n    # nothing sent while held
    clock.advance(1)
    assert s.send_next() == "sent"
    assert s.status() is None


def test_config_change_ends_hold_at_once(tmp_path, hub, env):
    lg, s = env
    hub.ingest_script.append((401, {"error": "bad token"}))
    _queue(tmp_path, lg, "a.CDF", b"A", 1000)
    assert s.send_next() == "hold"
    s.config_changed(HubClient(hub.url, hub.token, timeout=5))
    assert s.send_next() == "sent"


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_backoff_statuses_double_from_5_to_300(tmp_path, hub, clock, env, status):
    lg, s = env
    _queue(tmp_path, lg, "a.CDF", b"A", 1000)
    delays = []
    for _ in range(9):
        hub.ingest_script.append((status, {"error": "busy"}))
        assert s.send_next() == "backoff"
        assert s.status() == "hub-unreachable"
        delays.append(s.backoff_delay)
        assert s.send_next() == "blocked"
        clock.advance(s.backoff_delay)
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert BACKOFF_MAX == 300
    assert lg.counts()["queued"] == 1
    assert s.send_next() == "sent"                 # success resets
    assert s.backoff_delay == 0 and s.status() is None


def test_network_error_backs_off(tmp_path, clock):
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()
    lg = Ledger(tmp_path / "l.db")
    s = Sender(lg, HubClient("http://127.0.0.1:%d" % port, "t", timeout=2), clock=clock)
    _queue(tmp_path, lg, "a.CDF", b"A", 1000)
    assert s.send_next() == "backoff"
    assert s.backoff_delay == 5 and s.status() == "hub-unreachable"


def test_malformed_2xx_backs_off(tmp_path, hub, env):
    lg, s = env
    hub.ingest_script.append((201, b"<html>proxy</html>"))
    _queue(tmp_path, lg, "a.CDF", b"A", 1000)
    assert s.send_next() == "backoff"
    assert lg.counts()["queued"] == 1


def test_oldest_first(tmp_path, hub, env):
    lg, s = env
    _queue(tmp_path, lg, "new.CDF", b"N", 3000)
    _queue(tmp_path, lg, "old.CDF", b"O", 1000)
    _queue(tmp_path, lg, "mid.CDF", b"M", 2000)
    for _ in range(3):
        assert s.send_next() == "sent"
    got = [r.header("X-GC-Filename") for r in hub.by_path("/api/ingest")]
    assert got == ["old.CDF", "mid.CDF", "new.CDF"]


def test_oversize_rejected_locally(tmp_path, hub, clock):
    lg = Ledger(tmp_path / "l.db")
    s = Sender(lg, HubClient(hub.url, hub.token, timeout=5), clock=clock, max_bytes=10)
    _queue(tmp_path, lg, "big.CDF", b"x" * 11, 1000)
    assert s.send_next() == "rejected"
    assert "25 MB" in lg.rejected()[0].reason
    assert hub.by_path("/api/ingest") == []


def test_changed_or_missing_file_is_dropped(tmp_path, hub, env):
    lg, s = env
    p = _queue(tmp_path, lg, "a.CDF", b"A", 1000)
    with open(p, "ab") as fh:
        fh.write(b"more")
    assert s.send_next() == "dropped"
    q = _queue(tmp_path, lg, "b.CDF", b"B", 1000)
    os.unlink(q)
    assert s.send_next() == "dropped"
    assert hub.by_path("/api/ingest") == []
    assert lg.next_queued() is None


def test_retry_rejected_requeues_and_sends(tmp_path, hub, env):
    lg, s = env
    hub.ingest_script.append((400, {"error": "bad CDF"}))
    _queue(tmp_path, lg, "a.CDF", b"A", 1000)
    assert s.send_next() == "rejected"
    assert lg.requeue_rejected() == 1
    assert s.send_next() == "sent"
    assert lg.counts() == {"queued": 0, "sent": 1, "rejected": 0}


def test_a_purge_503_backs_off_and_resends_the_same_file(tmp_path, hub, clock, env):
    """v3.1: while an admin purges the instrument the hub answers ingest 503
    "purge in progress; retry". That is never a verdict on the file: the agent
    backs off (5 s doubling), keeps it queued and sends it again afterwards."""
    lg, s = env
    p = _queue(tmp_path, lg, "a.CDF", b"AAA", 1000)
    body = {"error": "GC-1: purge in progress; retry in a minute"}
    for expected in (5, 10, 20):
        hub.ingest_script.append((503, body))
        assert s.send_next() == "backoff"
        assert s.backoff_delay == expected and "purge in progress" in s.last_error
        assert lg.counts()["queued"] == 1 and lg.rejected() == []
        clock.advance(s.backoff_delay)
    assert s.send_next() == "sent"                  # the purge is over: 201
    assert lg.last_sent().path == p and s.status() is None
    assert len(hub.by_path("/api/ingest")) == 4     # the same file, four times
