"""Send queued CDFs to the hub, oldest first, per the §1 status table.

| status                         | action                                              |
|--------------------------------|-----------------------------------------------------|
| 201 / 200 / 202, sha matches   | sent                                                |
| 201 / 200 / 202, sha differs   | rejected ("hub acknowledged a different sha256")   |
| 400, 409, 413, 415             | rejected with the hub's reason (retry-rejected only)|
| 401, 403, 404, 405, other 4xx  | hold: queue kept, retry after 300 s or config change|
| 429, 5xx, network error, bad 2xx body | back off 5 s doubling to 300 s              |
| blocked by Cloudflare (any status)    | back off, like a network error (transient)  |

A 524 (Cloudflare gave up waiting for the hub) is a 5xx: retried, and the hub
dedupes the re-upload by sha256. A Cloudflare block (``cf-mitigated``, or a
403/503 challenge page) is never the hub's verdict on the file, so it never
rejects or holds.
"""
from __future__ import annotations

import logging
import os
import time

from . import util
from .client import NetworkError

log = logging.getLogger("gc_agent.sender")

MAX_BYTES = 25 * 1024 * 1024
HOLD_SECONDS = 300
BACKOFF_START = 5
BACKOFF_MAX = 300
REJECT_STATUSES = (400, 409, 413, 415)
OK_STATUSES = (200, 201, 202)


class Sender:
    def __init__(self, ledger, client, clock=time.time, max_bytes=MAX_BYTES):
        self.ledger = ledger
        self.client = client
        self.clock = clock
        self.max_bytes = max_bytes
        self.hold_until = None
        self.backoff_delay = 0
        self.backoff_until = 0.0
        self.last_error = None
        self.last_sent = None      # basename of the last file the hub accepted

    # ── state ────────────────────────────────────────────────────────────
    def status(self):
        """'auth-error' while held, 'hub-unreachable' while backing off, else None."""
        if self.hold_until is not None:
            return "auth-error"
        if self.backoff_delay:
            return "hub-unreachable"
        return None

    def blocked(self):
        now = self.clock()
        if self.hold_until is not None and now < self.hold_until:
            return True
        return bool(self.backoff_delay) and now < self.backoff_until

    def config_changed(self, client=None):
        """agent.json changed: retry at once with the (possibly new) client."""
        if client is not None:
            self.client = client
        self.hold_until = None
        self.backoff_delay = 0
        self.backoff_until = 0.0

    def _hold(self, why):
        self.hold_until = self.clock() + HOLD_SECONDS
        self.last_error = why
        log.error("holding the queue for %d s: %s", HOLD_SECONDS, why)
        return "hold"

    def _backoff(self, why):
        self.backoff_delay = BACKOFF_START if not self.backoff_delay \
            else min(self.backoff_delay * 2, BACKOFF_MAX)
        self.backoff_until = self.clock() + self.backoff_delay
        self.last_error = why
        log.warning("hub unavailable (%s); retrying in %d s", why, self.backoff_delay)
        return "backoff"

    def _ok(self):
        self.hold_until = None
        self.backoff_delay = 0
        self.backoff_until = 0.0

    def _reject(self, key, reason):
        self.ledger.mark_rejected(key, reason)
        log.warning("rejected %s: %s", key[0], reason)
        return "rejected"

    # ── one step ─────────────────────────────────────────────────────────
    def send_next(self):
        """Try the oldest queued file. Returns one of: idle, blocked, sent,
        rejected, dropped, hold, backoff."""
        if self.blocked():
            return "blocked"
        e = self.ledger.next_queued()
        if e is None:
            return "idle"
        key = (e.path, e.size, e.mtime_ns)
        try:
            st = os.stat(e.path)
            if (st.st_size, st.st_mtime_ns) != (e.size, e.mtime_ns):
                raise FileNotFoundError("changed since it was queued")
            if e.size > self.max_bytes:
                return self._reject(key, "too large to send (%d bytes > 25 MB)" % e.size)
            dup = self.ledger.sent_with_sha(e.sha256)
            if dup is not None:
                # The same bytes were already accepted (a re-export or a copy):
                # the hub would only answer "duplicate", so do not upload again.
                self.ledger.mark_sent(key, sample_id=dup.sample_id)
                self.last_sent = os.path.basename(e.path)
                log.info("%s has the same bytes as %s, already sent; not uploading",
                         e.path, dup.path)
                return "sent"
            with open(e.path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            # Gone or changed: the scanner queues the new version, if any.
            log.info("dropping %s from the queue: %s", e.path, exc)
            self.ledger.forget(key)
            return "dropped"
        sha = util.sha256_bytes(data)
        if len(data) != e.size or sha != e.sha256:
            self.ledger.forget(key)
            return "dropped"
        try:
            r = self.client.ingest(e.path, data, sha, e.mtime_ns / 1e9)
        except NetworkError as exc:
            return self._backoff("network error: %s" % exc)
        return self._handle(key, sha, r)

    def _handle(self, key, sha, r):
        name = os.path.basename(key[0])
        if getattr(r, "cloudflare_blocked", False):
            return self._backoff("HTTP %d: %s" % (r.status, r.error_text()))
        if r.status in OK_STATUSES:
            if not isinstance(r.json, dict) or not isinstance(r.json.get("sha256"), str):
                return self._backoff("HTTP %d with an unreadable body" % r.status)
            self._ok()
            got = r.json["sha256"].lower()
            if got != sha:
                return self._reject(key, "hub acknowledged sha256 %s but %s was sent (HTTP %d)"
                                    % (got, sha, r.status))
            sid = r.json.get("sample_id")
            self.ledger.mark_sent(key, sample_id=sid if isinstance(sid, int) else None)
            self.last_sent = name
            self.last_error = None
            log.info("sent %s (HTTP %d, sample %s)", name, r.status, sid)
            return "sent"
        if r.status in REJECT_STATUSES:
            self._ok()
            return self._reject(key, "HTTP %d: %s" % (r.status, r.error_text()))
        if r.status == 429 or r.status >= 500:
            return self._backoff("HTTP %d: %s" % (r.status, r.error_text()))
        return self._hold("HTTP %d: %s" % (r.status, r.error_text()))
