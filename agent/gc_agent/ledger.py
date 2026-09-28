"""``ledger.db`` (stdlib sqlite3): which CDF versions were sent, rejected or
are queued, plus a small key/value table (``results_seq``).

A file version is keyed by (path, size, mtime_ns), where the path is compared
as ``normcase(normpath(path))`` (``pkey``) so re-picking the watch folder with
another spelling (or case, on Windows) does not queue everything again. The
original spelling is kept in ``path`` for opening the file. A changed file is
a new version and is queued again; identical bytes already sent are never
uploaded twice (see ``sent_with_sha``).

Every known key is also held in memory, so a poll with no changes does no SQL.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections import namedtuple

from . import util

Entry = namedtuple("Entry", "path size mtime_ns sha256 state reason sample_id updated_at")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    pkey       TEXT    NOT NULL,
    path       TEXT    NOT NULL,
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    sha256     TEXT,
    state      TEXT    NOT NULL CHECK (state IN ('queued', 'sent', 'rejected')),
    reason     TEXT,
    sample_id  INTEGER,
    first_seen TEXT,
    updated_at TEXT,
    PRIMARY KEY (pkey, size, mtime_ns)
);
CREATE INDEX IF NOT EXISTS files_state ON files (state, mtime_ns);
CREATE INDEX IF NOT EXISTS files_sha ON files (sha256);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

_COLS = "path, size, mtime_ns, sha256, state, reason, sample_id, updated_at"
_WHERE_KEY = "pkey=? AND size=? AND mtime_ns=?"


def norm(path):
    """The comparison form of a path (normpath; normcase on Windows)."""
    return os.path.normcase(os.path.normpath(path))


def _k(key):
    return (norm(key[0]), int(key[1]), int(key[2]))


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
        self._known = set(self._db.execute("SELECT pkey, size, mtime_ns FROM files").fetchall())

    def close(self):
        with self._lock:
            self._db.close()

    def _one(self, sql, args=()):
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return Entry(*row) if row else None

    def _all(self, sql, args=()):
        with self._lock:
            return [Entry(*r) for r in self._db.execute(sql, args).fetchall()]

    def _exec(self, sql, args=()):
        with self._lock, self._db:
            return self._db.execute(sql, args).rowcount

    # ── files ────────────────────────────────────────────────────────────
    def known(self, path, size, mtime_ns):
        """In memory only: no SQL per file."""
        return _k((path, size, mtime_ns)) in self._known

    def add_queued(self, path, size, mtime_ns, sha256):
        self.add_queued_many([(path, size, mtime_ns, sha256)])

    def add_queued_many(self, items):
        """Queue several file versions in one transaction, dropping queued
        rows each one supersedes (same path, other size/mtime)."""
        if not items:
            return
        now = util.local_iso()
        with self._lock, self._db:
            for path, size, mtime_ns, sha in items:
                k = _k((path, size, mtime_ns))
                self._db.execute(
                    "INSERT OR IGNORE INTO files (pkey, path, size, mtime_ns, sha256, state,"
                    " first_seen, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
                    (k[0], path, size, mtime_ns, sha, now, now))
                stale = self._db.execute(
                    "SELECT pkey, size, mtime_ns FROM files WHERE pkey=? AND state='queued'"
                    " AND NOT (size=? AND mtime_ns=?)", k).fetchall()
                for s in stale:
                    self._db.execute("DELETE FROM files WHERE " + _WHERE_KEY, s)
                    self._known.discard(tuple(s))
                self._known.add(k)

    def drop_queued_for_path(self, path, keep):
        k = _k(keep)
        with self._lock, self._db:
            stale = self._db.execute(
                "SELECT pkey, size, mtime_ns FROM files WHERE pkey=? AND state='queued'"
                " AND NOT (size=? AND mtime_ns=?)", (norm(path), k[1], k[2])).fetchall()
            for s in stale:
                self._db.execute("DELETE FROM files WHERE " + _WHERE_KEY, s)
                self._known.discard(tuple(s))

    def forget(self, key):
        k = _k(key)
        self._exec("DELETE FROM files WHERE " + _WHERE_KEY, k)
        self._known.discard(k)

    def next_queued(self):
        return self._one("SELECT %s FROM files WHERE state='queued'"
                         " ORDER BY mtime_ns, path LIMIT 1" % _COLS)

    def sent_with_sha(self, sha256):
        if not sha256:
            return None
        return self._one("SELECT %s FROM files WHERE sha256=? AND state='sent' LIMIT 1" % _COLS,
                         (sha256,))

    def mark_sent(self, key, sample_id=None):
        self._exec("UPDATE files SET state='sent', reason=NULL, sample_id=?, updated_at=?"
                   " WHERE " + _WHERE_KEY, (sample_id, util.local_iso()) + _k(key))

    def mark_rejected(self, key, reason):
        self._exec("UPDATE files SET state='rejected', reason=?, updated_at=?"
                   " WHERE " + _WHERE_KEY, (str(reason), util.local_iso()) + _k(key))

    def requeue_rejected(self):
        return self._exec("UPDATE files SET state='queued', reason=NULL, updated_at=?"
                          " WHERE state='rejected'", (util.local_iso(),))

    def rejected(self):
        return self._all("SELECT %s FROM files WHERE state='rejected' ORDER BY mtime_ns" % _COLS)

    def last_sent(self):
        return self._one("SELECT %s FROM files WHERE state='sent'"
                         " ORDER BY updated_at DESC, rowid DESC LIMIT 1" % _COLS)

    def counts(self):
        out = {"queued": 0, "sent": 0, "rejected": 0}
        with self._lock:
            for state, n in self._db.execute("SELECT state, COUNT(*) FROM files GROUP BY state"):
                out[state] = n
        return out

    # ── kv ───────────────────────────────────────────────────────────────
    def get_kv(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_kv(self, key, value):
        self._exec("INSERT INTO kv (key, value) VALUES (?, ?)"
                   " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def results_seq(self):
        try:
            return int(self.get_kv("results_seq", "0"))
        except ValueError:
            return 0

    def set_results_seq(self, seq):
        self.set_kv("results_seq", int(seq))
