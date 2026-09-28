"""``ledger.db`` (stdlib sqlite3): which CDF versions were sent, rejected or
are queued, plus a small key/value table (``results_seq``).

A file version is keyed by (path, size, mtime_ns). A changed file is a new
version and is queued again; the hub dedupes identical bytes by sha256.
"""
from __future__ import annotations

import sqlite3
import threading
from collections import namedtuple

from . import util

Entry = namedtuple("Entry", "path size mtime_ns sha256 state reason sample_id updated_at")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path       TEXT    NOT NULL,
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    sha256     TEXT,
    state      TEXT    NOT NULL CHECK (state IN ('queued', 'sent', 'rejected')),
    reason     TEXT,
    sample_id  INTEGER,
    first_seen TEXT,
    updated_at TEXT,
    PRIMARY KEY (path, size, mtime_ns)
);
CREATE INDEX IF NOT EXISTS files_state ON files (state, mtime_ns);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

_COLS = "path, size, mtime_ns, sha256, state, reason, sample_id, updated_at"


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

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
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM files WHERE path=? AND size=? AND mtime_ns=?",
                (path, size, mtime_ns)).fetchone() is not None

    def add_queued(self, path, size, mtime_ns, sha256):
        now = util.local_iso()
        self._exec("INSERT OR IGNORE INTO files (path, size, mtime_ns, sha256, state, first_seen,"
                   " updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                   (path, size, mtime_ns, sha256, now, now))

    def drop_queued_for_path(self, path, keep):
        """Drop queued rows for *path* other than *keep* (superseded versions)."""
        self._exec("DELETE FROM files WHERE path=? AND state='queued'"
                   " AND NOT (size=? AND mtime_ns=?)", (path, keep[1], keep[2]))

    def forget(self, key):
        self._exec("DELETE FROM files WHERE path=? AND size=? AND mtime_ns=?", tuple(key))

    def next_queued(self):
        return self._one("SELECT %s FROM files WHERE state='queued'"
                         " ORDER BY mtime_ns, path LIMIT 1" % _COLS)

    def mark_sent(self, key, sample_id=None):
        self._exec("UPDATE files SET state='sent', reason=NULL, sample_id=?, updated_at=?"
                   " WHERE path=? AND size=? AND mtime_ns=?",
                   (sample_id, util.local_iso()) + tuple(key))

    def mark_rejected(self, key, reason):
        self._exec("UPDATE files SET state='rejected', reason=?, updated_at=?"
                   " WHERE path=? AND size=? AND mtime_ns=?",
                   (str(reason), util.local_iso()) + tuple(key))

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
