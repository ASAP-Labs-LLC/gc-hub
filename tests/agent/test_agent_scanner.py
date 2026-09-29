import os
import sys

import pytest

from gc_agent import winutil
from gc_agent.ledger import Ledger
from gc_agent.scanner import Scanner, find_cdfs


def _mk(p, data=b"x", mtime=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(str(p), (mtime, mtime))
    return p


def test_find_cdfs_case_insensitive_and_recursion(tmp_path):
    for n in ("a.CDF", "b.cdf", "c.Cdf", "x.txt", "cdf", "sub/d.CDF", "sub/deeper/e.cdf"):
        _mk(tmp_path / n)
    top = sorted(os.path.basename(p) for p in find_cdfs(str(tmp_path), False))
    assert top == ["a.CDF", "b.cdf", "c.Cdf"]
    allf = sorted(os.path.basename(p) for p in find_cdfs(str(tmp_path), True))
    assert allf == ["a.CDF", "b.cdf", "c.Cdf", "d.CDF", "e.cdf"]


def test_missing_watch_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(find_cdfs(str(tmp_path / "nope"), True))


def _scanner(tmp_path, clock, **kw):
    lg = Ledger(tmp_path / "ledger.db")
    return lg, Scanner(lg, clock=clock, **kw)


def test_young_file_waits_for_stability(tmp_path, clock):
    w = tmp_path / "w"
    f = _mk(w / "s.CDF", b"1", mtime=clock() - 1)
    lg, sc = _scanner(tmp_path, clock)
    assert sc.scan(str(w), True, 30) == 0
    clock.advance(10)
    _mk(f, b"12", mtime=clock() - 1)   # still being written
    assert sc.scan(str(w), True, 30) == 0
    clock.advance(28)                  # 29 s since the last write
    assert sc.scan(str(w), True, 30) == 0
    clock.advance(3)
    assert sc.scan(str(w), True, 30) == 1
    assert lg.next_queued().path == str(f)


def test_old_mtime_is_ready_at_once(tmp_path, clock):
    w = tmp_path / "w"
    _mk(w / "old.CDF", b"1", mtime=clock() - 3600)
    lg, sc = _scanner(tmp_path, clock)
    assert sc.scan(str(w), True, 30) == 1


def test_future_mtime_falls_back_to_observation(tmp_path, clock):
    w = tmp_path / "w"
    _mk(w / "f.CDF", b"1", mtime=clock() + 3600)
    lg, sc = _scanner(tmp_path, clock)
    assert sc.scan(str(w), True, 30) == 0
    clock.advance(31)
    assert sc.scan(str(w), True, 30) == 1


def test_exclusive_open_probe_blocks(tmp_path, clock):
    w = tmp_path / "w"
    _mk(w / "l.CDF", b"1", mtime=clock() - 3600)
    locked = {"v": True}
    lg, sc = _scanner(tmp_path, clock, can_open=lambda p: not locked["v"])
    assert sc.scan(str(w), True, 30) == 0
    locked["v"] = False
    assert sc.scan(str(w), True, 30) == 1


def test_known_files_are_not_rehashed_and_changes_requeue(tmp_path, clock):
    w = tmp_path / "w"
    f = _mk(w / "k.CDF", b"1", mtime=clock() - 3600)
    calls = []

    def hasher(p):
        calls.append(p)
        with open(p, "rb") as fh:
            import hashlib
            return hashlib.sha256(fh.read()).hexdigest()

    lg, sc = _scanner(tmp_path, clock, hasher=hasher)
    assert sc.scan(str(w), True, 30) == 1
    assert sc.scan(str(w), True, 30) == 0
    assert len(calls) == 1
    _mk(f, b"changed", mtime=clock() - 1000)
    assert sc.scan(str(w), True, 30) == 1
    assert len(calls) == 2
    assert lg.counts()["queued"] == 1          # the superseded version was dropped
    assert lg.next_queued().size == len(b"changed")


def test_can_open_exclusively_posix_is_true(tmp_path):
    f = _mk(tmp_path / "a.CDF")
    assert winutil.can_open_exclusively(str(f)) is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing semantics")
def test_can_open_exclusively_windows_false_while_held(tmp_path):
    f = _mk(tmp_path / "a.CDF")
    with open(str(f), "ab"):
        assert winutil.can_open_exclusively(str(f)) is False
    assert winutil.can_open_exclusively(str(f)) is True
