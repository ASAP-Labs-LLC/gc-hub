"""GC agent entry point, run by launcher.pyw as a child:

    pythonw versions/<v>/agent_main.py --root <agent root> [--no-tray]
    pythonw versions/<v>/agent_main.py --root <agent root> --settings

Exit codes (the launcher's contract): 0 quit, 3 restart (re-read
current.txt), anything else is a crash.

Importing this module has no side effects: the self-update smoke test runs
``python -c "import agent_main"`` in the new version's folder, so the import
must succeed exactly when the package is whole.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Imported at module level on purpose: the smoke test must catch a broken package.
from gc_agent import config, core, mirror, updater  # noqa: E402,F401
from gc_agent.core import Agent  # noqa: E402


def default_root():
    """``versions/<v>/agent_main.py`` → its root; otherwise the per-user
    install folder %LOCALAPPDATA%\\ASAPLabs\\gc-agent."""
    if HERE.parent.name == "versions":
        return HERE.parent.parent
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ASAPLabs" / "gc-agent"


def setup_logging(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(str(root / "agent.log"), maxBytes=1_000_000,
                                             backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    lg = logging.getLogger()
    lg.setLevel(logging.INFO)
    lg.addHandler(h)
    if sys.stderr is not None and not str(sys.executable).lower().endswith("pythonw.exe"):
        s = logging.StreamHandler()
        s.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        lg.addHandler(s)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent_main.py")
    ap.add_argument("--root", default=None)
    ap.add_argument("--no-tray", action="store_true", help="headless (tests, servers)")
    ap.add_argument("--settings", action="store_true", help="open the Settings dialog and exit")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else default_root()
    setup_logging(root)
    if args.settings:
        from gc_agent import settings_ui
        return settings_ui.run(root)
    agent = Agent(str(root), running_dir=str(HERE))
    if args.no_tray:
        try:
            return agent.run()
        except KeyboardInterrupt:
            return core.EXIT_QUIT
    from gc_agent import tray
    return tray.run_with_tray(agent, agent_main=str(Path(__file__).resolve()), root=str(root))


if __name__ == "__main__":
    sys.exit(main())
