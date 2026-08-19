"""
run.pyw  –  system-tray launcher for the GC Viewer webapp.

Run this file with pythonw (or double-click it) to start the server with
no terminal window.  A tray icon appears in the Windows notification area;
right-click it to:

  • Show / Hide the server console window  (Flask log output)
  • Restart Server
  • Open the app in your default browser
  • Quit

The server automatically restarts whenever a .py file inside webapp/ is saved.
Stale Python instances are cleaned up on start and on every restart.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem as Item
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

PORT  = 5560
BASE  = Path(__file__).resolve().parent   # …/webapp/
_PIDFILE = BASE / ".gc_server.pid"        # tracks the Flask subprocess PID

# ── Windows API ────────────────────────────────────────────────────────────
_user32   = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_user32.FindWindowW.restype          = ctypes.c_void_p
_user32.ShowWindow.argtypes          = [ctypes.c_void_p, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]

SW_HIDE = 0
SW_SHOW = 5


# ── Interpreter helper ─────────────────────────────────────────────────────

def _console_python() -> str:
    """Return the console ``python.exe``.

    When this launcher runs as ``run.pyw`` it is hosted by ``pythonw.exe``,
    a GUI-subsystem binary that won't surface a real console window even with
    ``CREATE_NEW_CONSOLE``. The Flask server must be spawned with the console
    interpreter so its log window exists and can be shown/hidden.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return sys.executable


# ── Process-tree helpers ───────────────────────────────────────────────────

def _taskkill(pid: int) -> None:
    """Force-kill *pid* and every child process it spawned."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def _cleanup_stale_server() -> None:
    """Kill any Flask subprocess left over from a previous launcher session."""
    if not _PIDFILE.exists():
        return
    try:
        data = json.loads(_PIDFILE.read_text())
        old_pid = int(data.get("pid", 0))
        if old_pid:
            _taskkill(old_pid)
    except Exception:
        pass
    finally:
        try:
            _PIDFILE.unlink(missing_ok=True)
        except Exception:
            pass


def _write_pidfile(pid: int) -> None:
    try:
        _PIDFILE.write_text(json.dumps({"pid": pid}))
    except Exception:
        pass


def _delete_pidfile() -> None:
    try:
        _PIDFILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── Window-finding helper ──────────────────────────────────────────────────

def _find_window_by_title(title: str, timeout: float = 6.0) -> int | None:
    """Poll until a top-level window with *title* appears, return its HWND."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hwnd = _user32.FindWindowW(None, title)
        if hwnd:
            return hwnd
        time.sleep(0.15)
    return None


# ── Flask subprocess manager ───────────────────────────────────────────────

class ServerManager:
    def __init__(self) -> None:
        self._proc:    subprocess.Popen | None = None
        self._hwnd:    int | None = None
        self._visible: bool = False
        self._lock   = threading.Lock()
        self._serial = 0

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            self._launch()

    def restart(self) -> None:
        with self._lock:
            self._kill()
            time.sleep(0.4)
            self._launch()

    def stop(self) -> None:
        with self._lock:
            self._kill()

    def _launch(self) -> None:
        self._serial += 1
        title  = f"GCViewer_{os.getpid()}_{self._serial}"
        script = str(BASE / "app.py")

        bootstrap = (
            "import sys, ctypes, runpy\n"
            f"ctypes.windll.kernel32.SetConsoleTitleW({title!r})\n"
            f"sys.path.insert(0, {str(BASE)!r})\n"
            f"runpy.run_path({script!r}, run_name='__main__')\n"
        )

        self._proc = subprocess.Popen(
            [_console_python(), "-c", bootstrap],
            cwd=str(BASE),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )

        # Persist PID so a future launcher session can clean this up
        _write_pidfile(self._proc.pid)

        # Locate the console window by title (polls up to 6 s)
        self._hwnd = _find_window_by_title(title)
        if self._hwnd and not self._visible:
            _user32.ShowWindow(self._hwnd, SW_HIDE)

    def _kill(self) -> None:
        """Kill the Flask subprocess and its entire process tree."""
        if self._proc is not None:
            _taskkill(self._proc.pid)
            try:
                self._proc.wait(timeout=5)
            except Exception:
                pass
        self._proc = None
        self._hwnd = None
        _delete_pidfile()

    # ── console visibility ────────────────────────────────────────────────

    @property
    def console_visible(self) -> bool:
        return self._visible

    def show_console(self) -> None:
        self._visible = True
        if self._hwnd:
            _user32.ShowWindow(self._hwnd, SW_SHOW)
            _user32.SetForegroundWindow(self._hwnd)

    def hide_console(self) -> None:
        self._visible = False
        if self._hwnd:
            _user32.ShowWindow(self._hwnd, SW_HIDE)

    def toggle_console(self) -> None:
        if self._visible:
            self.hide_console()
        else:
            self.show_console()


# ── Watchdog – restart on .py changes ─────────────────────────────────────

class _ReloadHandler(FileSystemEventHandler):
    def __init__(self, server: ServerManager) -> None:
        super().__init__()
        self._server = server
        self._timer: threading.Timer | None = None
        self._lock   = threading.Lock()

    def on_modified(self, event):
        if not event.is_directory and str(event.src_path).endswith(".py"):
            self._debounce()

    def on_created(self, event):
        if not event.is_directory and str(event.src_path).endswith(".py"):
            self._debounce()

    def _debounce(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(1.0, self._server.restart)
            self._timer.daemon = True
            self._timer.start()


# ── Tray icon ──────────────────────────────────────────────────────────────

def _make_icon_image() -> Image.Image:
    try:
        img = Image.open(BASE / "static" / "splash.png").resize((64, 64))
        return img.convert("RGBA")
    except Exception:
        img = Image.new("RGBA", (64, 64), (30, 90, 160, 255))
        d = ImageDraw.Draw(img)
        d.text((12, 18), "GC", fill="white")
        return img


def _run_tray(server: ServerManager) -> None:
    def toggle_console(icon, item):
        server.toggle_console()
        icon.update_menu()

    def restart_server(icon, item):
        # Run in a thread — restart holds the lock for several seconds while
        # killing and relaunching, which would otherwise freeze the tray menu.
        threading.Thread(
            target=server.restart, daemon=True, name="manual-restart"
        ).start()

    def open_browser(icon, item):
        webbrowser.open(f"http://localhost:{PORT}")

    def quit_app(icon, item):
        server.stop()
        icon.stop()
        os._exit(0)

    menu = Menu(
        Item(
            lambda item: "Hide Console" if server.console_visible else "Show Console",
            toggle_console,
        ),
        Item("Restart Server", restart_server),
        Item("Open in Browser", open_browser),
        Menu.SEPARATOR,
        Item("Quit", quit_app),
    )
    icon = pystray.Icon(
        "GC Viewer",
        _make_icon_image(),
        f"GC Viewer  :{PORT}",
        menu,
    )
    icon.run()


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    # Kill any Flask process left over from a previous launcher session
    _cleanup_stale_server()

    server = ServerManager()
    server.start()

    observer = Observer()
    observer.schedule(_ReloadHandler(server), str(BASE), recursive=False)
    observer.start()

    _run_tray(server)   # blocks until Quit

    # Graceful shutdown
    observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
