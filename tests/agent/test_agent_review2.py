"""Second review: the scan budget cannot starve new files, ledger writes are
all-or-nothing, the ledger schema migrates, unpack OSErrors are recorded,
and the installer's in-place agent.json update does not clobber the agent."""
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

from gc_agent import config, updater
from gc_agent.client import HubClient
from gc_agent.core import Agent
from gc_agent.ledger import SCHEMA_VERSION, Ledger
from gc_agent.scanner import FALLING_BEHIND_PASSES, Scanner

REPO = Path(__file__).resolve().parents[2]


def _files(w, names, mtime=1000):
    w.mkdir(parents=True, exist_ok=True)
    for n in names:
        p = w / n
        p.write_bytes(n.encode())
        os.utime(str(p), (mtime, mtime))


class JumpTimer:
    """Every read is 10 s after the previous one: any budget is spent at once."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 10.0
        return self.t


# ── BLOCKER: the budget must not starve new files ────────────────────────
def test_budget_spent_walking_known_files_still_queues_a_new_one(tmp_path, clock):
    w = tmp_path / "w"
    _files(w, ["a%03d.CDF" % i for i in range(100)])
    lg = Ledger(tmp_path / "l.db")
    Scanner(lg, clock=clock).scan(str(w), True, 0)          # all 100 known now
    _files(w, ["z-new.CDF"])                                 # walked last
    sc = Scanner(lg, clock=clock, timer=JumpTimer())
    assert sc.scan(str(w), True, 0) == 1
    assert lg.known(str(w / "z-new.CDF"), len(b"z-new.CDF"), 1000 * 10 ** 9)


def test_at_least_one_new_file_per_pass_even_over_budget(tmp_path, clock):
    w = tmp_path / "w"
    _files(w, ["n%d.CDF" % i for i in range(5)])
    lg = Ledger(tmp_path / "l.db")
    sc = Scanner(lg, clock=clock, timer=JumpTimer())
    got = [sc.scan(str(w), True, 0) for _ in range(5)]
    assert got == [1, 1, 1, 1, 1]
    assert lg.counts()["queued"] == 5 and sc.more is False


def test_falling_behind_is_reported_and_cleared(tmp_path, clock):
    w = tmp_path / "w"
    _files(w, ["n%d.CDF" % i for i in range(3)])
    lg = Ledger(tmp_path / "l.db")
    sc = Scanner(lg, clock=clock)
    for _ in range(FALLING_BEHIND_PASSES - 1):
        sc.scan(str(w), True, 0, max_new=0)                  # no progress at all
    assert sc.behind is None
    sc.scan(str(w), True, 0, max_new=0)
    assert sc.behind and sc.behind.startswith("scan falling behind")
    sc.scan(str(w), True, 0)
    assert sc.behind is None


def test_core_reports_falling_behind_in_last_error(tmp_path, hub, clock):
    root = tmp_path / "root"
    (root / "watch").mkdir(parents=True)
    config.save(root / "agent.json", {"hub_url": hub.url, "token": hub.token,
                                      "watch_dir": str(root / "watch")})
    a = Agent(str(root), running_dir=str(root / "dev"), clock=clock)
    a.scanner.behind = "scan falling behind: test"
    assert a.last_error() == "scan falling behind: test"
    assert a.snapshot()["last_error"] == "scan falling behind: test"


# ── MINOR 1: known set only after commit; sqlite errors do not stop the loop ──
class _FailingCommit:
    """Wraps a connection; leaving the transaction raises (commit fails)."""

    def __init__(self, db):
        self._db = db

    def execute(self, *a):
        return self._db.execute(*a)

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        self._db.rollback()
        if et is None:
            raise sqlite3.OperationalError("database is locked")
        return False


def test_known_set_untouched_when_commit_fails(tmp_path):
    lg = Ledger(tmp_path / "l.db")
    real = lg._db
    lg._db = _FailingCommit(real)
    with pytest.raises(sqlite3.OperationalError):
        lg.add_queued_many([("/w/a.CDF", 1, 1, "s")])
    lg._db = real
    assert not lg.known("/w/a.CDF", 1, 1)
    assert lg.counts()["queued"] == 0
    lg.add_queued_many([("/w/a.CDF", 1, 1, "s")])          # retried next pass
    assert lg.known("/w/a.CDF", 1, 1)


def test_sqlite_error_in_scan_does_not_stop_heartbeats(tmp_path, hub, clock):
    root = tmp_path / "root"
    _files(root / "watch", ["a.CDF"])
    config.save(root / "agent.json", {"hub_url": hub.url, "token": hub.token,
                                      "watch_dir": str(root / "watch"), "stable_seconds": 0})
    a = Agent(str(root), running_dir=str(root / "dev"), clock=clock)

    def boom(items):
        raise sqlite3.OperationalError("disk I/O error")

    a.ledger.add_queued_many = boom
    assert a.tick() is None
    assert len(hub.heartbeats) == 1
    assert "disk I/O error" in (a.last_error() or "")


# ── MINOR 2: schema version + migration ──────────────────────────────────
def test_new_ledger_has_schema_version(tmp_path):
    Ledger(tmp_path / "l.db").close()
    con = sqlite3.connect(str(tmp_path / "l.db"))
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_pre_pkey_ledger_is_migrated(tmp_path):
    db = tmp_path / "l.db"
    con = sqlite3.connect(str(db))
    con.executescript("""
        CREATE TABLE files (path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            sha256 TEXT, state TEXT NOT NULL, reason TEXT, sample_id INTEGER, first_seen TEXT,
            updated_at TEXT, PRIMARY KEY (path, size, mtime_ns));
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO files VALUES ('/w/sub/../a.CDF', 3, 7, 'abc', 'sent', NULL, 5, 't', 't');
        INSERT INTO files VALUES ('/w/b.CDF', 4, 8, 'def', 'queued', NULL, NULL, 't', 't');
        INSERT INTO kv VALUES ('results_seq', '12');
    """)
    con.commit()
    con.close()
    lg = Ledger(db)
    assert lg.known("/w/a.CDF", 3, 7)
    assert lg.counts() == {"queued": 1, "sent": 1, "rejected": 0}
    assert lg.sent_with_sha("abc").sample_id == 5
    assert lg.next_queued().path == "/w/b.CDF"
    assert lg.results_seq() == 12
    lg.close()
    con = sqlite3.connect(str(db))
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    cols = [r[1] for r in con.execute("PRAGMA table_info(files)")]
    assert "pkey" in cols


# ── MINOR 4: OSError during unpack is recorded like an UpdateError ───────
def test_oserror_during_unpack_is_recorded(tmp_path, hub, clock, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("bp_r2", str(REPO / "agent" / "build_package.py"))
    spec = importlib.util.spec_from_loader("bp_r2", loader)
    bp = importlib.util.module_from_spec(spec)
    loader.exec_module(bp)
    zp = tmp_path / "p.zip"
    sha = bp.build(REPO / "agent", "v3.0.0", zp)
    hub.package_version, hub.package_zip = "v3.0.0", zp.read_bytes()
    root = tmp_path / "root"
    (root / "versions" / "v1").mkdir(parents=True)
    (root / "current.txt").write_text("v1\n")

    def denied(data, dest):
        raise PermissionError(13, "Access is denied", str(dest))

    monkeypatch.setattr(updater, "safe_extract", denied)
    u = updater.Updater(str(root), HubClient(hub.url, hub.token), own_sha="0" * 64,
                        python="", clock=clock)
    res = u.check()
    assert res.startswith("refused") and "denied" in res
    assert sha in json.loads((root / "failed_packages.json").read_text())
    assert (root / "current.txt").read_text().strip() == "v1"


# ── MINOR 5: installer in-place update: retrying write, re-read and merge ──
def _load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


INSTALL = _load(REPO / "agent" / "install.pyw", "gc_install_r2")
LAUNCHER = _load(REPO / "agent" / "launcher.pyw", "gc_launcher_r2")


class _UI:
    def __init__(self):
        self.msgs = []

    def info(self, m):
        self.msgs.append(m)


def test_in_place_update_survives_the_agent_writing_paused(tmp_path, monkeypatch):
    root = tmp_path
    cfg_path = root / "agent.json"
    cfg_path.write_text(json.dumps({"hub_url": "http://old", "token": "old", "paused": False}))
    calls = {"n": 0}
    real_write = LAUNCHER.atomic_write_text

    def racing_write(path, text):
        real_write(path, text)
        calls["n"] += 1
        if calls["n"] == 1:
            # The agent (Pause from the tray) read the file before our write
            # and writes its own copy right after it.
            real_write(path, json.dumps({"hub_url": "http://old", "token": "old",
                                         "paused": True}))

    monkeypatch.setattr(LAUNCHER, "atomic_write_text", racing_write)
    monkeypatch.setattr(INSTALL.time, "sleep", lambda s: None)
    INSTALL.update_running(LAUNCHER, root, "http://new", "tok-new", _UI())
    got = json.loads(cfg_path.read_text())
    assert got["token"] == "tok-new" and got["hub_url"] == "http://new"
    assert got["paused"] is True                              # the agent's write is kept
    assert calls["n"] == 2


def test_launcher_atomic_write_retries_sharing_violations(tmp_path, monkeypatch):
    real = os.replace
    fails = {"n": 2}

    def flaky(a, b):
        if fails["n"]:
            fails["n"] -= 1
            raise PermissionError(32, "being used by another process")
        return real(a, b)

    monkeypatch.setattr(LAUNCHER.os, "replace", flaky)
    monkeypatch.setattr(LAUNCHER.time, "sleep", lambda s: None)
    LAUNCHER.atomic_write_text(tmp_path / "f.json", "{}")
    assert (tmp_path / "f.json").read_text() == "{}"
    assert sorted(os.listdir(tmp_path)) == ["f.json"]
