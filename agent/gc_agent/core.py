"""The agent's main loop: scan → send → heartbeat (+ commands) → results
mirror → self-update, plus the state the tray and the hub see.

``tick()`` does whatever is due and returns an exit code when the agent
should stop (0 quit, 3 restart) or None. ``run()`` loops over it. The tray
talks to the loop only through ``request()`` and ``snapshot()``.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import time
from pathlib import Path

from . import config, mirror, updater, util
from .client import HubClient, NetworkError
from .ledger import Ledger
from .mirror import Mirror, MirrorError, MirrorFetchError
from .scanner import Scanner
from .sender import Sender

log = logging.getLogger("gc_agent.core")

EXIT_QUIT = 0
EXIT_RESTART = 3
UPDATE_EVERY = 3600
UPDATE_MISMATCH_MIN_GAP = 300
SEND_BATCH = 50
COMMANDS = ("restart", "pause", "resume", "retry-rejected", "adopt-mirror")


def heartbeat_seconds(env=None):
    env = os.environ if env is None else env
    try:
        v = float(env.get("GC_AGENT_HEARTBEAT_SECONDS", "30"))
        return v if v > 0 else 30.0
    except ValueError:
        return 30.0


def read_version(directory):
    try:
        line = (Path(directory) / "VERSION").read_text(encoding="utf-8").splitlines()[0].strip()
        return line or "dev"
    except (OSError, IndexError):
        return "dev"


class Agent:
    def __init__(self, root, running_dir, clock=time.time, hostname=None):
        self.root = Path(root)
        self.running_dir = Path(running_dir)
        self.clock = clock
        self.host = hostname or socket.gethostname()
        self.cfg_path = self.root / "agent.json"
        self.log_path = self.root / "agent.log"
        self.version = read_version(self.running_dir)
        self.package_sha = updater.package_sha_of(self.running_dir)
        try:
            self.updates_enabled = (self.running_dir.resolve().parent
                                    == (self.root / "versions").resolve())
        except OSError:
            self.updates_enabled = False

        self.ledger = Ledger(self.root / "ledger.db")
        self.scanner = Scanner(self.ledger, clock=clock)
        self.cfg = None
        self.cfg_error = None
        self.watch_error = None
        self.mirror_error = None
        self.client = None
        self.sender = Sender(self.ledger, None, clock=clock)
        self.mirror = None
        self.hb_problem = None          # None | "auth-error" | "hub-unreachable"
        self.hb_error = None
        self._cfg_sig = None
        self._actions = queue.Queue()
        self._lock = threading.Lock()
        self._last_reported_state = None
        self._revert_note = None
        self._revert_pending = False
        self.exit_code = None
        now = clock()
        self._next_scan = now
        self._next_hb = now
        self._next_update = now          # checked at start
        self._last_update_check = None
        self._load_config(initial=True)
        self._read_revert_note()

    # ── config ───────────────────────────────────────────────────────────
    def _sig(self):
        try:
            st = os.stat(str(self.cfg_path))
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load_config(self, initial=False):
        self._cfg_sig = self._sig()
        try:
            cfg = config.load(self.cfg_path)
        except config.ConfigError as exc:
            self.cfg_error = str(exc)
            log.error("%s", exc)
            return
        self.cfg = cfg
        self.cfg_error = None
        self.client = HubClient(cfg["hub_url"], cfg["token"])
        self.sender.config_changed(self.client)
        self.hb_problem = None
        mp = cfg.get("results_mirror_path") or ""
        self.mirror = Mirror(mp, self.ledger) if mp else None
        self.mirror_error = None
        self._next_scan = self.clock()
        if not initial:
            log.info("agent.json reloaded")
            self._next_hb = self.clock()

    def _maybe_reload(self):
        if self._sig() != self._cfg_sig:
            self._load_config()

    def _set_paused(self, paused):
        if self.cfg is None:
            return
        try:
            config.set_key(self.cfg_path, "paused", bool(paused))
        except (config.ConfigError, OSError) as exc:
            log.error("cannot save paused=%s: %s", paused, exc)
        self.cfg["paused"] = bool(paused)
        self._cfg_sig = self._sig()
        log.info("paused" if paused else "resumed")

    def _read_revert_note(self):
        p = self.root / "revert.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._revert_note = ("launcher reverted from %s to %s at %s after 3 quick crashes"
                             % (d.get("from"), d.get("to"), d.get("at")))
        self._revert_pending = True

    # ── state ────────────────────────────────────────────────────────────
    @property
    def paused(self):
        return bool(self.cfg and self.cfg.get("paused"))

    def state(self):
        if self.cfg is None or self.cfg_error or self.watch_error:
            return "config-error"
        if self.paused:
            return "paused"
        s = self.sender.status()
        if s == "auth-error" or self.hb_problem == "auth-error":
            return "auth-error"
        if s == "hub-unreachable" or self.hb_problem == "hub-unreachable":
            return "hub-unreachable"
        if self.ledger.counts()["queued"]:
            return "sending"
        return "idle"

    def last_error(self):
        for e in (self._revert_note, self.cfg_error, self.watch_error, self.sender.last_error,
                  self.hb_error, ("mirror: " + self.mirror_error) if self.mirror_error else None):
            if e:
                return e
        return None

    def last_file(self):
        if self.sender.last_sent:
            return self.sender.last_sent
        e = self.ledger.last_sent()
        return os.path.basename(e.path) if e else None

    def snapshot(self):
        c = self.ledger.counts()
        return {"version": self.version, "state": self.state(), "queued": c["queued"],
                "rejected": c["rejected"], "last_sent": self.last_file(),
                "mirror_seq": self.ledger.results_seq(), "last_error": self.last_error(),
                "hub_url": self.cfg["hub_url"] if self.cfg else "",
                "log": str(self.log_path), "paused": self.paused}

    # ── actions from the tray / commands from the hub ────────────────────
    def request(self, action):
        self._actions.put(action)

    def _do(self, action, source):
        log.info("%s: %s", source, action)
        if action == "quit":
            self.exit_code = EXIT_QUIT
        elif action == "restart":
            self.exit_code = EXIT_RESTART
        elif action == "pause":
            self._set_paused(True)
        elif action == "resume":
            self._set_paused(False)
        elif action == "retry-rejected":
            n = self.ledger.requeue_rejected()
            log.info("requeued %d rejected file(s)", n)
        elif action == "adopt-mirror":
            if self.mirror is None:
                log.warning("adopt-mirror: no results_mirror_path set")
            else:
                try:
                    mirror.adopt(self.mirror.path, self.ledger.results_seq())
                    self.mirror_error = None
                except (MirrorError, OSError) as exc:
                    self.mirror_error = str(exc)
        else:
            log.warning("ignoring unknown %s %r", source, action)
            return
        self._next_hb = self.clock()      # the next heartbeat reflects it

    # ── work ─────────────────────────────────────────────────────────────
    def _scan_and_send(self, now):
        if self.cfg is None or self.cfg_error or self.paused:
            return
        if now >= self._next_scan:
            self._next_scan = now + float(self.cfg["poll_seconds"])
            wd = self.cfg["watch_dir"]
            try:
                if not wd:
                    raise FileNotFoundError("")
                self.scanner.scan(wd, self.cfg["include_subdirs"], self.cfg["stable_seconds"])
                self.watch_error = None
            except FileNotFoundError:
                self.watch_error = "watch folder not found: %s" % (wd or "(not set)")
            except OSError as exc:
                self.watch_error = "cannot read watch folder %s: %s" % (wd, exc)
        if self.watch_error:
            return
        for _ in range(SEND_BATCH):
            if not self._actions.empty():
                break
            if self.sender.send_next() not in ("sent", "rejected", "dropped"):
                break

    def _heartbeat(self, now):
        if self.client is None:
            return
        self._next_hb = now + heartbeat_seconds()
        state = self.state()
        c = self.ledger.counts()
        payload = {"version": self.version, "package_sha256": self.package_sha, "state": state,
                   "queue_size": c["queued"], "rejected_count": c["rejected"],
                   "last_file": self.last_file(), "last_error": self.last_error(),
                   "host": self.host, "agent_time": util.local_iso(),
                   "results_seq": self.ledger.results_seq()}
        self._last_reported_state = state
        try:
            r = self.client.heartbeat(payload)
        except NetworkError as exc:
            self.hb_problem, self.hb_error = "hub-unreachable", "heartbeat: %s" % exc
            return
        if r.status != 200 or not isinstance(r.json, dict):
            self.hb_error = "heartbeat: HTTP %d %s" % (r.status, r.error_text())
            self.hb_problem = "hub-unreachable" if (r.status == 429 or r.status >= 500
                                                    or r.status == 200) else "auth-error"
            return
        self.hb_problem = self.hb_error = None
        if self._revert_pending:
            self._revert_pending = False
            self._revert_note = None
            try:
                os.unlink(str(self.root / "revert.json"))
            except OSError:
                pass
        cmd = r.json.get("command")
        if cmd:
            self._do(cmd, "hub command")
        hub_sha = r.json.get("agent_package_sha256")
        if (isinstance(hub_sha, str) and hub_sha and hub_sha.lower() != self.package_sha.lower()
                and self.updates_enabled):
            last = self._last_update_check
            if last is None or now - last >= UPDATE_MISMATCH_MIN_GAP:
                self._next_update = now
        if self.exit_code is None:
            self._sync_mirror()

    def _sync_mirror(self):
        if self.mirror is None:
            return
        try:
            self.mirror.sync(self.client)
            self.mirror_error = None
        except MirrorFetchError as exc:
            log.warning("%s", exc)
        except (MirrorError, OSError) as exc:
            if str(exc) != self.mirror_error:
                log.error("mirror: %s", exc)
            self.mirror_error = str(exc)

    def _update(self, now):
        self._next_update = now + UPDATE_EVERY
        if not self.updates_enabled or self.client is None:
            return
        self._last_update_check = now
        res = updater.Updater(self.root, self.client, self.package_sha,
                              self.cfg.get("python", "") if self.cfg else "").check()
        if res == "switched":
            self.exit_code = EXIT_RESTART

    def tick(self):
        now = self.clock()
        while True:
            try:
                self._do(self._actions.get_nowait(), "tray")
            except queue.Empty:
                break
        if self.exit_code is not None:
            return self.exit_code
        self._maybe_reload()
        if now >= self._next_update:
            self._update(now)
            if self.exit_code is not None:
                return self.exit_code
        self._scan_and_send(now)
        if now >= self._next_hb or self.state() != self._last_reported_state:
            self._heartbeat(now)
        if self.exit_code is None and now >= self._next_update:
            self._update(now)
        return self.exit_code

    def run(self, stop_event=None):
        """Loop until an exit code is set (or *stop_event*). Returns it."""
        stop_event = stop_event or threading.Event()
        log.info("agent %s starting (root %s, updates %s)", self.version, self.root,
                 "on" if self.updates_enabled else "off")
        while not stop_event.is_set():
            try:
                code = self.tick()
            except Exception:
                log.exception("unexpected error in the agent loop")
                code = None
            if code is not None:
                log.info("agent exiting with code %d", code)
                return code
            wait = 1.0
            if self.cfg:
                wait = min(1.0, float(self.cfg["poll_seconds"]))
            stop_event.wait(wait)
        return self.exit_code if self.exit_code is not None else EXIT_QUIT
