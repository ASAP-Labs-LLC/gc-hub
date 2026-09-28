"""Find finished ``*.CDF`` files in the watch folder and queue them in the
ledger. Files on the GC PC are only ever read, never modified or deleted."""
from __future__ import annotations

import logging
import os
import time

from . import util, winutil

log = logging.getLogger("gc_agent.scanner")


def find_cdfs(watch_dir, include_subdirs):
    """Yield the full path of every ``*.cdf`` (any case) under *watch_dir*.
    Raises FileNotFoundError when the folder is missing."""
    if not os.path.isdir(watch_dir):
        raise FileNotFoundError(watch_dir)
    if include_subdirs:
        for dirpath, dirnames, filenames in os.walk(watch_dir):
            dirnames.sort()
            for name in sorted(filenames):
                if name.lower().endswith(".cdf"):
                    yield os.path.join(dirpath, name)
    else:
        for name in sorted(os.listdir(watch_dir)):
            p = os.path.join(watch_dir, name)
            if name.lower().endswith(".cdf") and os.path.isfile(p):
                yield p


class Scanner:
    """A file is ready once its (size, mtime) has not changed for
    ``stable_seconds`` - judged by its mtime when that is in the past, else by
    how long this scanner has watched it unchanged - **and** it can be opened
    exclusively (Windows)."""

    def __init__(self, ledger, clock=time.time, can_open=winutil.can_open_exclusively,
                 hasher=util.sha256_file):
        self.ledger = ledger
        self.clock = clock
        self.can_open = can_open
        self.hasher = hasher
        self._seen = {}   # path -> (size, mtime_ns, first seen with that size/mtime)

    def scan(self, watch_dir, include_subdirs, stable_seconds):
        """One pass. Returns how many file versions were newly queued."""
        now = self.clock()
        queued = 0
        present = set()
        for path in find_cdfs(watch_dir, include_subdirs):
            present.add(path)
            try:
                st = os.stat(path)
            except OSError:
                continue
            size, mtime_ns = st.st_size, st.st_mtime_ns
            if self.ledger.known(path, size, mtime_ns):
                self._seen.pop(path, None)
                continue
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
            self.ledger.add_queued(path, size, mtime_ns, sha)
            self.ledger.drop_queued_for_path(path, keep=(path, size, mtime_ns))
            self._seen.pop(path, None)
            queued += 1
        for gone in set(self._seen) - present:
            del self._seen[gone]
        return queued
