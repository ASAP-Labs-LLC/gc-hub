"""The desktop layer: the pystray icon and menu, tkinter dialogs, and the
poll loop. Everything that decides anything lives in ``logic`` and
``controller``; this only wires them to the screen.

Threads: pystray's own loop runs in the main thread; a poller refreshes the
view every ``poll_seconds``; menu clicks are queued to ONE action thread,
which runs the controller and owns every tkinter dialog (tkinter must stay
on one thread), so a slow action never freezes the menu.
"""
from __future__ import annotations

import logging
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import logic

log = logging.getLogger("gc_tray.ui")


def make_image(colour: str):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=logic.COLOURS[colour] + (255,),
              outline=(255, 255, 255, 255), width=3)
    d.text((18, 20), "HUB", fill=(255, 255, 255, 255))
    return img


def build_menu(pystray, ctl, get_view, submit, *, on_exit):
    """The right-click menu. ``get_view()`` is the latest view; ``submit(fn,
    *args)`` queues an action; ``on_exit()`` closes the icon only."""
    Menu, Item = pystray.Menu, pystray.MenuItem

    def m():
        return logic.menu_state(get_view())

    def action(fn, with_view=False):
        def run(_icon, _item):
            if with_view:
                submit(fn, get_view())
            else:
                submit(fn)
        return run

    return Menu(
        Item(lambda _i: logic.status_text(get_view()), None, enabled=False),
        Menu.SEPARATOR,
        Item("Open in browser", action(ctl.open_browser, True), default=True,
             enabled=lambda _i: m()["open_enabled"]),
        Item(lambda _i: m()["pause_label"], action(ctl.toggle_pause, True),
             enabled=lambda _i: m()["pause_enabled"]),
        Item(lambda _i: m()["restart_label"], action(ctl.restart, True),
             enabled=lambda _i: m()["restart_enabled"]),
        Item("Stop hub", action(ctl.stop), enabled=lambda _i: m()["stop_enabled"]),
        Item("Start hub", action(ctl.start, True), enabled=lambda _i: m()["start_enabled"]),
        Menu.SEPARATOR,
        Item("Exit tray", lambda _icon, _item: on_exit()),
    )


class TkUI:  # pragma: no cover - needs a desktop session
    """Modal dialogs, each on a fresh hidden, topmost root (always called
    from the tray's one action thread)."""

    def _root(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        return root

    def ask_password(self, prompt):
        from tkinter import simpledialog
        root = self._root()
        try:
            return simpledialog.askstring("GC hub", prompt, show="*", parent=root)
        finally:
            root.destroy()

    def confirm(self, title, text):
        from tkinter import messagebox
        root = self._root()
        try:
            return messagebox.askokcancel(title, text, parent=root)
        finally:
            root.destroy()

    def info(self, title, text):
        from tkinter import messagebox
        root = self._root()
        try:
            messagebox.showinfo(title, text, parent=root)
        finally:
            root.destroy()

    def error(self, title, text):
        from tkinter import messagebox
        root = self._root()
        try:
            messagebox.showerror(title, text, parent=root)
        finally:
            root.destroy()


MAX_BACKOFF_SECONDS = 60.0


class Poller:
    """One poll of the hub → the view, the icon's colour and tooltip, and the
    relaunch check. ``poll_once`` never raises (anything that goes wrong is
    logged and shown as red "not responding") and returns how long to wait:
    ``poll_seconds`` while the hub answers, doubling up to
    ``MAX_BACKOFF_SECONDS`` while it does not."""

    def __init__(self, cfg, client, icon, *, image=None, release_root: Path, relaunch,
                 clock=time.monotonic) -> None:
        self.cfg = cfg
        self.client = client
        self.icon = icon
        self.image = image or make_image
        self.release_root = release_root
        self.relaunch = relaunch
        self.clock = clock
        self.view = logic.parse_status(None, marker_present=False)
        self.busy = logic.BusyTracker(cfg["cpu_busy_percent"], cfg["cpu_busy_seconds"])
        self.own_version = logic.read_version(release_root)
        self.failures = 0
        self.stopped = False           # set after a successful relaunch
        self._colour = None

    def _marker_present(self) -> bool:
        marker = logic.marker_path(self.cfg)
        try:
            return bool(marker and marker.exists())
        except OSError:                 # an Administrators-only data folder
            return False

    def _show(self, view: dict, colour: str) -> None:
        try:
            if colour != self._colour:
                self.icon.icon = self.image(colour)
                self._colour = colour
            self.icon.title = logic.tooltip(view)
            self.icon.update_menu()
        except Exception:  # noqa: BLE001
            log.debug("icon update failed", exc_info=True)

    def _wait(self) -> float:
        base = float(self.cfg["poll_seconds"])
        if self.view.get("reachable"):
            self.failures = 0
            return base
        self.failures += 1
        return min(max(base, MAX_BACKOFF_SECONDS), base * 2 ** (self.failures - 1))

    def poll_once(self) -> float:
        try:
            view = logic.parse_status(self.client.status(),
                                      marker_present=self._marker_present())
            cpu_busy = self.busy.update(self.clock(), view.get("cpu_percent"))
            colour = logic.colour(view, cpu_busy=cpu_busy,
                                  queue_threshold=self.cfg["queue_busy_threshold"])
        except Exception:  # noqa: BLE001 - one bad poll never ends the loop
            log.exception("hub status poll failed")
            view = logic.parse_status(None, marker_present=False)
            colour = "red"
        self.view = view
        self._show(view, colour)
        try:
            if logic.should_relaunch(self.own_version, logic.read_version(self.release_root),
                                     view.get("version")):
                log.warning("hub is now %s; restarting the tray to run it", view["version"])
                try:
                    self.relaunch()
                    self.stopped = True
                except Exception:  # noqa: BLE001
                    log.exception("could not relaunch the tray; carrying on")
                    self.own_version = view["version"]      # don't retry every poll
        except Exception:  # noqa: BLE001
            log.exception("relaunch check failed")
        return self._wait()


def run(cfg, ctl, client, *, release_root: Path, relaunch) -> int:
    """Show the icon until Exit tray. ``relaunch()`` starts a fresh tray
    (after the hub switched release); this one then exits."""
    import pystray

    actions: "queue.Queue" = queue.Queue()
    stop = threading.Event()
    wake = threading.Event()
    holder = {}

    def submit(fn, *args):
        actions.put((fn, args))

    def worker():
        while True:
            item = actions.get()
            if item is None:
                return
            fn, args = item
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 - one bad click never ends the tray
                log.exception("tray action %s failed", getattr(fn, "__name__", fn))
            wake.set()

    icon = pystray.Icon("gc-hub-tray", make_image("red"), "GC hub",
                        build_menu(pystray, ctl, lambda: holder["poller"].view, submit,
                                   on_exit=lambda: icon.stop()))
    poller = holder["poller"] = Poller(cfg, client, icon, release_root=release_root,
                                       relaunch=relaunch)

    def poll():
        while not stop.is_set():
            try:
                wait = poller.poll_once()
            except Exception:  # noqa: BLE001 - belt and braces: poll_once never raises
                log.exception("poll loop error")
                wait = cfg["poll_seconds"]
            if poller.stopped:
                icon.stop()
                return
            wake.wait(wait)
            wake.clear()

    threading.Thread(target=worker, name="gc-tray-actions", daemon=True).start()
    threading.Thread(target=poll, name="gc-tray-poll", daemon=True).start()
    icon.run()
    stop.set()
    wake.set()
    actions.put(None)
    return 0


def spawn_detached(cmd, cwd=None):  # pragma: no cover - desktop only
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": cwd}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)
