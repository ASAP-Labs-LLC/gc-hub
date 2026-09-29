"""GC hub tray: a system-tray control for the hub on ASAPSV1 (DEPLOY.md,
"Hub tray on ASAPSV1").

The hub runs under the COA updater in the background (session 0, no
desktop), so its tray lives in this separate process in the logged-on
admin's session and talks to the hub over http://127.0.0.1:5560.

    C:\\ASAPApps\\gc\\current\\.venv\\Scripts\\pythonw.exe C:\\ASAPApps\\gc\\current\\tray\\hub_tray.pyw --install
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gc_tray.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
