"""Tray icon (pystray + Pillow, both optional: without them the agent runs
headless and says so in the log).

Colours: green healthy, amber hub unreachable, red auth or config error,
grey paused. Menu: status lines (version, state, queued, rejected, last
sent, mirror seq, last error), Open hub, Open log, Settings…, Pause/Resume,
Retry rejected, Restart, Quit.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

log = logging.getLogger("gc_agent.tray")

COLOURS = {
    "green": (46, 160, 67),
    "amber": (230, 160, 0),
    "red": (200, 40, 40),
    "grey": (140, 140, 140),
}
_STATE_COLOUR = {
    "idle": "green", "sending": "green", "hub-unreachable": "amber",
    "auth-error": "red", "config-error": "red", "paused": "grey",
}


def tray_colour(state):
    return _STATE_COLOUR.get(state, "red")


def status_lines(snap):
    lines = [
        "GC agent %s: %s" % (snap.get("version"), snap.get("state")),
        "Queued: %d" % snap.get("queued", 0),
        "Rejected: %d" % snap.get("rejected", 0),
        "Last sent: %s" % (snap.get("last_sent") or "-"),
        "Mirror seq: %d" % snap.get("mirror_seq", 0),
    ]
    if snap.get("last_error"):
        err = str(snap["last_error"])
        lines.append("Error: " + (err if len(err) <= 90 else err[:87] + "..."))
    return lines


def pause_label(snap):
    return "Resume" if snap.get("paused") else "Pause"


def make_image(colour):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=COLOURS[colour] + (255,), outline=(255, 255, 255, 255), width=3)
    d.text((22, 20), "GC", fill=(255, 255, 255, 255))
    return img


def _open_path(path):  # pragma: no cover - desktop only
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        webbrowser.open(Path(path).resolve().as_uri())


def run_with_tray(agent, agent_main, root):  # pragma: no cover - needs a desktop session
    """Run the agent loop in a thread and the tray in this (main) thread.
    Returns the agent's exit code."""
    try:
        import pystray
        from pystray import Menu, MenuItem as Item
        make_image("green")
    except Exception as exc:
        log.warning("tray unavailable (%s); running headless", exc)
        return agent.run()

    stop = threading.Event()
    result = {"code": None}

    def loop():
        result["code"] = agent.run(stop)
        try:
            icon.stop()
        except Exception:
            pass

    def status_item(i):
        return Item(lambda _item: (status_lines(agent.snapshot()) + [""] * 6)[i],
                    lambda _icon, _item: None, enabled=False,
                    visible=lambda _item: bool((status_lines(agent.snapshot()) + [""] * 6)[i]))

    def open_hub(_icon, _item):
        url = agent.snapshot().get("hub_url")
        if url:
            webbrowser.open(url)

    def open_log(_icon, _item):
        _open_path(agent.snapshot()["log"])

    def settings(_icon, _item):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([sys.executable, agent_main, "--root", root, "--settings"],
                         creationflags=flags)

    def toggle_pause(_icon, _item):
        agent.request("resume" if agent.snapshot().get("paused") else "pause")

    menu = Menu(
        *[status_item(i) for i in range(6)],
        Menu.SEPARATOR,
        Item("Open hub", open_hub),
        Item("Open log", open_log),
        Item("Settings…", settings),
        Item(lambda _item: pause_label(agent.snapshot()), toggle_pause),
        Item("Retry rejected", lambda _i, _t: agent.request("retry-rejected")),
        Item("Restart", lambda _i, _t: agent.request("restart")),
        Item("Quit", lambda _i, _t: agent.request("quit")),
    )
    icon = pystray.Icon("gc-agent", make_image("green"), "GC agent", menu)

    def refresh():
        last = None
        while not stop.wait(2.0):
            snap = agent.snapshot()
            colour = tray_colour(snap["state"])
            if colour != last:
                icon.icon = make_image(colour)
                last = colour
            icon.title = ("GC agent %s: %s, %d queued" % (snap["version"], snap["state"],
                                                          snap["queued"]))[:127]
            try:
                icon.update_menu()
            except Exception:
                pass

    worker = threading.Thread(target=loop, name="gc-agent-loop", daemon=True)
    worker.start()
    threading.Thread(target=refresh, name="gc-agent-tray", daemon=True).start()
    icon.run()
    stop.set()
    worker.join(timeout=30)
    return result["code"] if result["code"] is not None else 0
