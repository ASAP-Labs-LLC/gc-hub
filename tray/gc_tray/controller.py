"""What each tray menu item does, with the UI injected (``ui`` has
``ask_password(prompt)``, ``confirm(title, text)``, ``info(title, text)``,
``error(title, text)``), so the flows are tested without a desktop.

The admin password is asked for when an action needs it and, with
``remember_password`` (the default), kept **in memory** for the rest of the
tray's session; it is never written anywhere. A wrong one is forgotten and
asked for again once.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import webbrowser
from typing import Callable, Optional

from . import logic

log = logging.getLogger("gc_tray.controller")

TITLE = "GC hub"
PAUSE = "/api/admin/hub/pause-processing"
RESUME = "/api/admin/hub/resume-processing"
STOP = "/api/admin/hub/stop"
RESTART = "/api/restart"
PASSWORD_PROMPT = ("GC hub admin password\n(kept in memory until the tray exits, "
                   "never saved)")
STOP_TEXT = ("Stop the GC hub?\n\nNothing is received, processed or served until it is "
             "started again: agents keep their files and send them later.\n\nThe updater "
             "is paused so it does not restart the hub; use Start hub in this menu to "
             "start it again.")
START_DONE = ("Start requested: the updater starts the hub within about 20 s "
              "(the icon turns green when it answers).")


class Controller:
    def __init__(self, cfg: dict, client, ui, *, python: Optional[str] = None) -> None:
        self.cfg = cfg
        self.client = client
        self.ui = ui
        self.python = python or sys.executable
        self.run_cmd: Callable = subprocess.run
        self.open_url: Callable[[str], object] = webbrowser.open
        # run a command as administrator (Windows: a UAC prompt); None = can't
        self.elevate: Optional[Callable[[list], bool]] = None
        self._password: Optional[str] = None

    def __repr__(self) -> str:                       # never shows the password
        return f"<Controller {logic.status_url(self.cfg)}>"

    # ── admin calls ──

    def _admin_post(self, path: str) -> Optional[dict]:
        for _attempt in range(2):
            pw = self._password or self.ui.ask_password(PASSWORD_PROMPT)
            if not pw:
                return None
            code, body = self.client.post(path, {"password": pw})
            if code is not None and 200 <= code < 300:
                self._password = pw if self.cfg.get("remember_password", True) else None
                return body
            self._password = None
            msg = body.get("error") or f"HTTP {code}"
            self.ui.error(TITLE, msg)
            if not (code == 403 and msg == "Incorrect password"):
                return None
        return None

    # ── menu actions ──

    def toggle_pause(self, view: dict) -> bool:
        resume = bool(view.get("processing_paused"))
        body = self._admin_post(RESUME if resume else PAUSE)
        if body is None:
            return False
        log.info("processing %s", "resumed" if resume else "paused")
        return True

    def restart(self, view: dict) -> bool:
        staged = view.get("staged_update")
        text = (f"Restart the GC hub and install {staged}?" if staged else
                "Restart the GC hub?") + ("\n\nIt is back in well under a minute; the page "
                                          "reloads by itself.")
        if not self.ui.confirm(TITLE, text):
            return False
        code, body = self.client.post(RESTART, {})
        if code != 200:
            self.ui.error(TITLE, body.get("error") or f"HTTP {code}")
            return False
        return True

    def stop(self) -> bool:
        if not self.ui.confirm(TITLE, STOP_TEXT):
            return False
        body = self._admin_post(STOP)
        if body is None:
            return False
        log.warning("hub stopped from the tray")
        return True

    @staticmethod
    def unlink_marker(path) -> None:
        path.unlink(missing_ok=True)

    def start(self) -> bool:
        """The updater's ``resume`` (deletes ``<data>\\paused``; its
        supervise() then starts the hub within ~20 s). If the CLI cannot run
        (not found, or it fails, e.g. no write access to updater.log), the
        marker is removed directly, which is all ``resume`` does. If that is
        denied too (the data folder is Administrators-only and the tray runs
        unelevated), the same command is offered elevated (a UAC prompt)."""
        cmd = logic.start_command(self.cfg, fallback_python=self.python)
        why = None
        try:
            r = self.run_cmd(cmd, capture_output=True, text=True, timeout=120,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                log.info("updater resume: %s", (r.stdout or "").strip())
                self.ui.info(TITLE, START_DONE)
                return True
            why = ((r.stderr or r.stdout or "").strip().splitlines() or ["?"])[-1]
        except (OSError, subprocess.SubprocessError) as exc:
            why = str(exc)
        log.warning("updater resume failed (%s); removing the marker directly", why)
        marker = logic.marker_path(self.cfg)
        try:
            if marker is None:
                raise OSError("data_dir is not set in tray.json")
            self.unlink_marker(marker)
        except OSError as exc:
            if self.elevate is not None:
                if not self.ui.confirm(TITLE, "Starting the hub needs administrator rights "
                                              "here (the updater and its data folder are "
                                              "Administrators-only).\n\nRun the updater's "
                                              "resume as an administrator?"):
                    return False
                if self.elevate(cmd):
                    self.ui.info(TITLE, START_DONE)
                    return True
                self.ui.error(TITLE, "The updater was not started as an administrator.")
                return False
            self.ui.error(TITLE, f"Could not start the hub.\n\nThe updater said: {why}\n"
                                 f"Removing {marker} failed: {exc}\n\nRun as an administrator: "
                                 f"{' '.join(cmd)}")
            return False
        self.ui.info(TITLE, START_DONE)
        return True

    def open_browser(self) -> None:
        self.open_url(logic.browser_url(self.cfg))
