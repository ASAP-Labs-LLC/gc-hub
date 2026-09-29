"""hub_tray.pyw's entry point.

    pythonw hub_tray.pyw                 run the tray (one per logged-on user)
    pythonw hub_tray.pyw --install       autostart at this user's logon (HKCU Run), start it
    pythonw hub_tray.pyw --uninstall     remove the autostart (a running tray keeps running
                                         until Exit tray)
    ... [--config PATH]                  default %APPDATA%\\ASAPLabs\\gc-hub-tray.json

Install it from the ``current`` junction
(``C:\\ASAPApps\\gc\\current\\.venv\\Scripts\\pythonw.exe
C:\\ASAPApps\\gc\\current\\tray\\hub_tray.pyw --install``) so autostart keeps
following the release the updater switches to. Paths are kept as given
(``os.path.abspath``, never resolved), for the same reason.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from . import logic, winsys

log = logging.getLogger("gc_tray")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="hub_tray.pyw", description="GC hub tray",
                                 allow_abbrev=False)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true",
                   help="start the tray at logon for this user (HKCU Run) and start it now")
    g.add_argument("--uninstall", action="store_true", help="remove the logon autostart")
    ap.add_argument("--config", help="tray.json (default %%APPDATA%%\\ASAPLabs\\gc-hub-tray.json)")
    return ap.parse_args(argv)


def _setup_logging() -> None:
    path = winsys.default_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2,
                                                 encoding="utf-8")
    except OSError:
        h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)


def _script() -> str:
    return os.path.abspath(sys.argv[0])      # not resolve(): keep the `current` junction


def _command(config) -> list:
    cmd = [winsys.pythonw_for(sys.executable), _script()]
    if config:
        cmd += ["--config", os.path.abspath(config)]
    return cmd


def _tell(title: str, text: str, error: bool = False) -> None:  # pragma: no cover - desktop
    print(text, file=sys.stderr if error else sys.stdout)
    try:
        from .ui import TkUI
        (TkUI().error if error else TkUI().info)(title, text)
    except Exception:  # noqa: BLE001 - no display: the print is enough
        pass


def main(argv=None) -> int:  # pragma: no cover - desktop
    args = parse_args(argv)
    _setup_logging()
    from . import ui
    if args.uninstall:
        removed = winsys.uninstall_autostart()
        _tell("GC hub tray", "Autostart removed." if removed else "Autostart was not set up.")
        return 0
    if args.install:
        cmd = _command(args.config)
        cfg_arg = os.path.abspath(args.config) if args.config else None
        if not winsys.install_autostart(winsys.autostart_command(cmd[0], cmd[1],
                                                                 config=cfg_arg)):
            _tell("GC hub tray", "Autostart needs Windows (HKCU Run).", error=True)
            return 1
        ui.spawn_detached(cmd, cwd=str(Path(cmd[1]).parent))
        _tell("GC hub tray", "Installed: the GC hub tray starts at your logon, and is starting "
                             "now (look for the round HUB icon by the clock; you may need to "
                             "drag it out of the hidden icons).")
        return 0

    held = winsys.acquire_single_instance()
    if held is None:
        log.info("another GC hub tray is already running for this user; exiting")
        return 0
    try:
        cfg = logic.load_config(args.config or winsys.default_config_path())
    except logic.ConfigError as exc:
        held.release()
        _tell("GC hub tray", f"Bad tray settings: {exc}", error=True)
        return 2
    from .client import HubClient
    from .controller import Controller
    client = HubClient(logic.status_url(cfg))
    ctl = Controller(cfg, client, ui.TkUI(), python=sys.executable)
    if sys.platform == "win32":
        ctl.elevate = winsys.run_elevated

    def relaunch():
        held.release()
        ui.spawn_detached(_command(args.config), cwd=str(Path(_script()).parent))

    log.info("GC hub tray started (hub %s)", logic.status_url(cfg))
    try:
        return ui.run(cfg, ctl, client, release_root=Path(_script()).parent.parent,
                      relaunch=relaunch)
    finally:
        held.release()
