"""Review fixes 1-3: a big first scan is capped, a no-change poll does no
per-file SQL, folder spelling does not re-queue, same bytes are not re-sent."""
import os
import sys

import pytest

from gc_agent import config
from gc_agent.client import HubClient
from gc_agent.ledger import Ledger
from gc_agent.scanner import Scanner
from gc_agent.sender import Sender


def _many(w, n):
    w.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        p = w / ("s%05d.CDF" % i)
        p.write_bytes(b"x%d" % i)
        os.utime(str(p), (1000, 1000))


def test_scan_pass_is_capped_by_count_and_batched(tmp_path, clock):
    w = tmp_path / "w"
    _many(w, 450)
    lg = Ledger(tmp_path / "l.db")
    sc = Scanner(lg, clock=clock)
    commits = []
    lg._db.set_trace_callback(lambda s: commits.append(s) if s.strip().upper() == "COMMIT" else None)
    assert sc.scan(str(w), True, 0, max_new=200) == 200
    assert sc.more is True
    assert len(commits) == 1                       # one transaction for the batch
    assert sc.scan(str(w), True, 0, max_new=200) == 200
    assert sc.scan(str(w), True, 0, max_new=200) == 50
    assert sc.more is False
    assert lg.counts()["queued"] == 450


def test_scan_pass_is_capped_by_time(tmp_path, clock):
    w = tmp_path / "w"
    _many(w, 50)
    t = {"now": 0.0}

    def timer():
        return t["now"]

    def hasher(p):
        t["now"] += 0.5                            # each hash "takes" 0.5 s
        return "h" + os.path.basename(p)

    lg = Ledger(tmp_path / "l.db")
    sc = Scanner(lg, clock=clock, hasher=hasher, timer=timer)
    n = sc.scan(str(w), True, 0, max_seconds=2.0)
    assert 1 <= n <= 5 and sc.more is True


def test_no_change_poll_does_no_sql(tmp_path, clock):
    w = tmp_path / "w"
    _many(w, 300)
    lg = Ledger(tmp_path / "l.db")
    sc = Scanner(lg, clock=clock)
    while True:
        sc.scan(str(w), True, 0)
        if not sc.more:
            break
    stmts = []
    lg._db.set_trace_callback(stmts.append)
    assert sc.scan(str(w), True, 0) == 0
    assert stmts == []


def test_ledger_known_set_survives_reopen_and_forget(tmp_path):
    lg = Ledger(tmp_path / "l.db")
    lg.add_queued("/w/a.CDF", 1, 1, "s")
    lg.close()
    lg = Ledger(tmp_path / "l.db")
    assert lg.known("/w/a.CDF", 1, 1)
    lg.forget(("/w/a.CDF", 1, 1))
    assert not lg.known("/w/a.CDF", 1, 1)


def test_watch_dir_is_normalised_in_config(tmp_path):
    raw = {"hub_url": "http://h", "token": " t \n", "watch_dir": str(tmp_path) + "/x/../w/./"}
    cfg = config.validate(raw)
    assert cfg["watch_dir"] == os.path.normpath(str(tmp_path / "w"))
    assert cfg["token"] == "t"


def test_repicked_folder_spelling_does_not_requeue(tmp_path, clock):
    w = tmp_path / "w"
    _many(w, 3)
    lg = Ledger(tmp_path / "l.db")
    sc = Scanner(lg, clock=clock)
    assert sc.scan(str(w), True, 0) == 3
    assert sc.scan(str(tmp_path) + "/x/../w/", True, 0) == 0
    assert lg.counts()["queued"] == 3


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive paths")
def test_repicked_folder_case_does_not_requeue_on_windows(tmp_path, clock):
    w = tmp_path / "w"
    _many(w, 3)
    lg = Ledger(tmp_path / "l.db")
    sc = Scanner(lg, clock=clock)
    assert sc.scan(str(w), True, 0) == 3
    assert sc.scan(str(w).upper(), True, 0) == 0


def test_reexported_same_bytes_are_not_uploaded_again(tmp_path, hub, clock):
    w = tmp_path / "w"
    w.mkdir()
    (w / "a.CDF").write_bytes(b"same bytes")
    lg = Ledger(tmp_path / "l.db")
    sc = Scanner(lg, clock=clock)
    s = Sender(lg, HubClient(hub.url, hub.token, timeout=5), clock=clock)
    sc.scan(str(w), True, 0)
    assert s.send_next() == "sent"
    (w / "copy of a.CDF").write_bytes(b"same bytes")
    sc.scan(str(w), True, 0)
    assert s.send_next() == "sent"
    assert len(hub.by_path("/api/ingest")) == 1
    assert lg.counts() == {"queued": 0, "sent": 2, "rejected": 0}
    assert s.last_sent == "copy of a.CDF"
