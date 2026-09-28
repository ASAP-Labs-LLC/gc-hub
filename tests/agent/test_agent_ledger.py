import sqlite3

from gc_agent.ledger import Ledger


def test_schema_and_known(tmp_path):
    db = tmp_path / "ledger.db"
    lg = Ledger(db)
    tables = {r[0] for r in sqlite3.connect(str(db)).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"files", "kv"} <= tables
    assert not lg.known("C:/d/a.CDF", 10, 111)
    lg.add_queued("C:/d/a.CDF", 10, 111, "aa")
    assert lg.known("C:/d/a.CDF", 10, 111)
    # the key is (path, size, mtime): a change is a different file version
    assert not lg.known("C:/d/a.CDF", 11, 111)
    assert not lg.known("C:/d/a.CDF", 10, 112)


def test_next_queued_oldest_first_and_states(tmp_path):
    lg = Ledger(tmp_path / "l.db")
    lg.add_queued("b", 1, 300, "b1")
    lg.add_queued("a", 1, 200, "a1")
    lg.add_queued("c", 1, 100, "c1")
    assert lg.next_queued().path == "c"
    lg.mark_sent(("c", 1, 100), sample_id=5)
    assert lg.next_queued().path == "a"
    lg.mark_rejected(("a", 1, 200), "bad CDF")
    assert lg.next_queued().path == "b"
    c = lg.counts()
    assert (c["queued"], c["sent"], c["rejected"]) == (1, 1, 1)
    assert lg.rejected()[0].reason == "bad CDF"
    assert lg.last_sent().path == "c"
    assert lg.requeue_rejected() == 1
    assert lg.counts()["rejected"] == 0
    assert lg.next_queued().path == "a"


def test_drop_stale_queued_and_forget(tmp_path):
    lg = Ledger(tmp_path / "l.db")
    lg.add_queued("a", 1, 100, "x")
    lg.add_queued("a", 2, 200, "y")
    lg.drop_queued_for_path("a", keep=("a", 2, 200))
    assert not lg.known("a", 1, 100)
    assert lg.known("a", 2, 200)
    lg.forget(("a", 2, 200))
    assert lg.next_queued() is None


def test_sent_rows_are_never_dropped_as_stale(tmp_path):
    lg = Ledger(tmp_path / "l.db")
    lg.add_queued("a", 1, 100, "x")
    lg.mark_sent(("a", 1, 100), sample_id=1)
    lg.drop_queued_for_path("a", keep=("a", 2, 200))
    assert lg.known("a", 1, 100)


def test_kv_and_persistence(tmp_path):
    lg = Ledger(tmp_path / "l.db")
    assert lg.results_seq() == 0
    lg.set_results_seq(42)
    lg.add_queued("a", 1, 1, "s")
    lg.close()
    lg2 = Ledger(tmp_path / "l.db")
    assert lg2.results_seq() == 42
    assert lg2.known("a", 1, 1)
