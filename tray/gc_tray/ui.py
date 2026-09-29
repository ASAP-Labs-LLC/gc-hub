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
        Item("Open in browser", action(ctl.open_browser), default=True,
             enabled=lambda _i: m()["open_enabled"]),
        Item(lambda _i: m()["pause_label"], action(ctl.toggle_pause, True),
             enabled=lambda _i: m()["pause_enabled"]),
        Item(lambda _i: m()["restart_label"], action(ctl.restart, True),
             enabled=lambda _i: m()["restart_enabled"]),
        Item("Stop hub", action(ctl.stop), enabled=lambda _i: m()["stop_enabled"]),
        Item("Start hub", action(ctl.start), enabled=lambda _i: m()["start_enabled"]),
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


def run(cfg, ctl, client, *, release_root: Path, relaunch) -> int:  # pragma: no cover
    """Show the icon until Exit tray. ``relaunch()`` starts a fresh tray
    (after the hub switched release) and is followed by this one exiting."""
    import pystray

    own_version = logic.read_version(release_root)
    state = {"view": logic.parse_status(None, marker_present=False)}
    busy = logic.BusyTracker(cfg["cpu_busy_percent"], cfg["cpu_busy_seconds"])
    actions: "queue.Queue" = queue.Queue()
    stop = threading.Event()

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

    wake = threading.Event()
    icon = pystray.Icon("gc-hub-tray", make_image("red"), "GC hub",
                        build_menu(pystray, ctl, lambda: state["view"], submit,
                                   on_exit=lambda: icon.stop()))

    def poll():
        last_colour = None
        while not stop.is_set():
            marker = logic.marker_path(cfg)
            try:
                present = bool(marker and marker.exists())
            except OSError:
                present = False
            view = logic.parse_status(client.status(), marker_present=present)
            state["view"] = view
            cpu_busy = busy.update(time.monotonic(), view.get("cpu_percent"))
            colour = logic.colour(view, cpu_busy=cpu_busy,
                                  queue_threshold=cfg["queue_busy_threshold"])
            try:
                if colour != last_colour:
                    icon.icon = make_image(colour)
                    last_colour = colour
                icon.title = logic.tooltip(view)
                icon.update_menu()
            except Exception:  # noqa: BLE001
                log.debug("icon update failed", exc_info=True)
            if logic.should_relaunch(own[0], logic.read_version(release_root),
                                     view.get("version")):
                log.warning("hub is now %s; restarting the tray to run it", view["version"])
                try:
                    relaunch()
                    icon.stop()
                    return
                except Exception:  # noqa: BLE001
                    log.exception("could not relaunch the tray; carrying on")
                    own[0] = view["version"]            # don't retry every poll
            wake.wait(cfg["poll_seconds"])
            wake.clear()

    own = [own_version]
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
