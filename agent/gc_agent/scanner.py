"""Find finished ``*.CDF`` files in the watch folder and queue them in the
ledger. Files on the GC PC are only ever read, never modified or deleted.

Cheap by design: the walk uses ``os.scandir`` and ``DirEntry.stat()`` (no
extra stat per file on Windows), known versions are checked against the
ledger's in-memory set (no SQL), and one pass hashes at most ``max_new``
files or ``max_seconds`` of work before returning, so a first scan of a
folder with years of CDFs never starves heartbeats, sends or tray actions.
The next poll resumes where the cap stopped (already-queued files are skipped
in memory).

The time budget is charged only for probing and hashing new files: the clock
starts at the first unknown file, so a slow walk over thousands of known
files (antivirus on a GC PC) cannot use it up. And every pass queues at least
one ready file before a cap can stop it, so a pass always makes progress. If
passes keep stopping at a cap without queuing anything, ``behind`` says so
(shown in the tray and the heartbeat's last_error).
"""
from __future__ import annotations

import logging
import os
import stat as stat_mod
import time

from . import util, winutil

log = logging.getLogger("gc_agent.scanner")

MAX_NEW_PER_PASS = 200
MAX_SECONDS_PER_PASS = 2.0
FALLING_BEHIND_PASSES = 10


def _walk(watch_dir, include_subdirs):
    """Yield (path, DirEntry) for every ``*.cdf`` (any case). Sorted per
    folder so the order is stable. Raises FileNotFoundError when the watch
    folder is missing; unreadable subfolders are skipped."""
    if not os.path.isdir(watch_dir):
        raise FileNotFoundError(watch_dir)
    stack = [watch_dir]
    first = True
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            if first:
                raise
            log.warning("cannot list %s: %s", d, exc)
            continue
        first = False
        subdirs = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if include_subdirs:
                        subdirs.append(e.path)
                elif e.name.lower().endswith(".cdf") and e.is_file():
                    yield e.path, e
            except OSError:
                continue
        stack.extend(reversed(subdirs))


def find_cdfs(watch_dir, include_subdirs):
    for path, _e in _walk(watch_dir, include_subdirs):
        yield path


class Scanner:
    """A file is ready once its (size, mtime) has not changed for
    ``stable_seconds`` - judged by its mtime when that is in the past, else by
    how long this scanner has watched it unchanged - **and** it can be opened
    exclusively (Windows)."""

    def __init__(self, ledger, clock=time.time, can_open=winutil.can_open_exclusively,
                 hasher=util.sha256_file, timer=time.monotonic):
        self.ledger = ledger
        self.clock = clock
        self.can_open = can_open
        self.hasher = hasher
        self.timer = timer
        self.more = False            # the last pass stopped at a cap
        self.behind = None           # "scan falling behind: ..." or None
        self._stalled = 0
        self._seen = {}   # path -> (size, mtime_ns, first seen with that size/mtime)

    def scan(self, watch_dir, include_subdirs, stable_seconds,
             max_new=MAX_NEW_PER_PASS, max_seconds=MAX_SECONDS_PER_PASS):
        """One pass. Returns how many file versions were newly queued."""
        watch_dir = os.path.normpath(watch_dir)
        now = self.clock()
        t0 = None                    # starts at the first unknown file
        batch = []
        present = set()
        self.more = False
        for path, entry in _walk(watch_dir, include_subdirs):
            present.add(path)
            try:
                st = entry.stat()
            except OSError:
                continue
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            size, mtime_ns = st.st_size, st.st_mtime_ns
            if self.ledger.known(path, size, mtime_ns):
                if path in self._seen:
                    del self._seen[path]
                continue
            if t0 is None:
                t0 = self.timer()
            elif batch and self.timer() - t0 >= max_seconds:
                self.more = True
                break
            if len(batch) >= max_new:
                self.more = True
                break
            prev = self._seen.get(path)
            if prev is None or prev[0] != size or prev[1] != mtime_ns:
                self._seen[path] = (size, mtime_ns, now)
                since = now
            else:
                since = prev[2]
            mtime = mtime_ns / 1e9
            age = now - mtime if mtime <= now else -1.0
            if max(age, now - since) < stable_seconds:
                continue
            if not self.can_open(path):
                continue
            try:
                sha = self.hasher(path)
                st2 = os.stat(path)
            except OSError as exc:
                log.warning("cannot read %s: %s", path, exc)
                continue
            if (st2.st_size, st2.st_mtime_ns) != (size, mtime_ns):
                self._seen.pop(path, None)   # changed while hashing: start over
                continue
            batch.append((path, size, mtime_ns, sha))
            self._seen.pop(path, None)
        self.ledger.add_queued_many(batch)
        if self.more and not batch:
            self._stalled += 1
            if self._stalled >= FALLING_BEHIND_PASSES:
                self.behind = ("scan falling behind: %d passes in a row stopped without queuing "
                               "a file in %s" % (self._stalled, watch_dir))
                if self._stalled == FALLING_BEHIND_PASSES:
                    log.error("%s", self.behind)
        else:
            self._stalled = 0
            self.behind = None
        if not self.more:
            for gone in set(self._seen) - present:
                del self._seen[gone]
        elif batch:
            log.info("queued %d file(s); more to scan next poll", len(batch))
        return len(batch)
