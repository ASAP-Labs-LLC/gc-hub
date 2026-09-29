"""The pystray/tkinter layer, smoke-imported with a stub ``pystray`` (the
real one needs a desktop session), plus the Windows glue behind injectable
seams: single instance, HKCU Run autostart, the entry point's arguments."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from gc_tray import logic, winsys

REPO = Path(__file__).resolve().parents[2]


class _Item:
    def __init__(self, text, action=None, enabled=True, visible=True, default=False,
                 checked=None):
        self.text, self.action, self.enabled, self.visible = text, action, enabled, visible


class _Menu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


@pytest.fixture
def stub_pystray(monkeypatch):
    mod = types.ModuleType("pystray")
    mod.Menu = _Menu
    mod.MenuItem = _Item
    mod.Icon = object
    monkeypatch.setitem(sys.modules, "pystray", mod)
    sys.modules.pop("gc_tray.ui", None)
    yield mod
    sys.modules.pop("gc_tray.ui", None)


class FakeCtl:
    def toggle_pause(self, view):
        pass

    def restart(self, view):
        pass

    def stop(self):
        pass

    def start(self):
        pass

    def open_browser(self):
        pass


def _texts(menu, view):
    out = []
    for it in menu.items:
        if it is _Menu.SEPARATOR:
            out.append("---")
            continue
        text = it.text(it) if callable(it.text) else it.text
        out.append(text)
    return out


def test_menu_builds_and_every_item_dispatches(stub_pystray):
    from gc_tray import ui
    view = logic.parse_status({"version": "v3.0.0", "state": "running",
                               "staged_update": "v3.1.0"}, marker_present=False)
    queued = []
    ctl = FakeCtl()
    menu = ui.build_menu(stub_pystray, ctl, lambda: view, lambda fn, *a: queued.append((fn, a)),
                         on_exit=lambda: queued.append(("exit", ())))
    texts = _texts(menu, view)
    assert texts[0].startswith("GC hub v3.0.0")
    for want in ("Open in browser", "Pause processing", "Restart & install v3.1.0", "Stop hub",
                 "Start hub", "Exit tray"):
        assert want in texts, texts
    for it in menu.items:
        if it is _Menu.SEPARATOR or it.action is None:
            continue
        it.action(None, it)
    names = [q[0] if isinstance(q[0], str) else q[0].__name__ for q in queued]
    for want in ("toggle_pause", "restart", "stop", "start", "open_browser", "exit"):
        assert want in names, names


def test_menu_enabled_flags_follow_the_view(stub_pystray):
    from gc_tray import ui
    stopped = logic.parse_status(None, marker_present=True)
    menu = ui.build_menu(stub_pystray, FakeCtl(), lambda: stopped, lambda *a: None,
                         on_exit=lambda: None)
    by_text = {}
    for it in menu.items:
        if it is _Menu.SEPARATOR:
            continue
        text = it.text(it) if callable(it.text) else it.text
        by_text[text] = it.enabled(it) if callable(it.enabled) else it.enabled
    assert by_text["Start hub"] is True
    assert by_text["Stop hub"] is False and by_text["Open in browser"] is False


def test_menu_builds_with_the_real_pystray_on_windows():
    if sys.platform != "win32":
        pytest.skip("the real pystray backend is the Windows one")
    pystray = pytest.importorskip("pystray")
    sys.modules.pop("gc_tray.ui", None)
    from gc_tray import ui
    view = logic.parse_status({"version": "v3.0.0", "state": "running"}, marker_present=False)
    menu = ui.build_menu(pystray, FakeCtl(), lambda: view, lambda *a: None,
                         on_exit=lambda: None)
    texts = [it.text for it in menu.items if it is not pystray.Menu.SEPARATOR]
    assert texts[0].startswith("GC hub v3.0.0")
    assert "Pause processing" in texts and "Exit tray" in texts


def test_icon_image_when_pillow_present(stub_pystray):
    pytest.importorskip("PIL")
    from gc_tray import ui
    img = ui.make_image("amber")
    assert img.size == (64, 64)
    assert img.getpixel((32, 32))[:3] == logic.COLOURS["amber"]


# ── Windows glue ──────────────────────────────────────────────────────────

class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, root, path, reserved=0, access=0):
        fake = self

        class K:
            def __enter__(self):
                return (root, path)

            def __exit__(self, *a):
                return False
        self.last = (root, path)
        return K()

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[(key, name)] = value

    def DeleteValue(self, key, name):
        if (key, name) not in self.values:
            raise FileNotFoundError(name)
        del self.values[(key, name)]


def test_autostart_install_and_uninstall():
    reg = FakeWinreg()
    cmd = winsys.autostart_command(r"C:\ASAPApps\gc\current\.venv\Scripts\pythonw.exe",
                                   r"C:\ASAPApps\gc\current\tray\hub_tray.pyw")
    assert cmd == ('"C:\\ASAPApps\\gc\\current\\.venv\\Scripts\\pythonw.exe" '
                   '"C:\\ASAPApps\\gc\\current\\tray\\hub_tray.pyw"')
    assert winsys.install_autostart(cmd, winreg=reg) is True
    assert reg.values[(("HKCU", winsys.RUN_KEY), winsys.RUN_VALUE)] == cmd
    assert winsys.uninstall_autostart(winreg=reg) is True
    assert reg.values == {}
    assert winsys.uninstall_autostart(winreg=reg) is False      # already gone


def test_autostart_command_with_a_config():
    cmd = winsys.autostart_command("pw.exe", "t.pyw", config=r"D:\c\tray.json")
    assert cmd == '"pw.exe" "t.pyw" --config "D:\\c\\tray.json"'


def test_pythonw_for(tmp_path):
    exe = tmp_path / "python.exe"
    exe.write_text("")
    assert winsys.pythonw_for(str(exe)) == str(exe)          # no pythonw.exe beside it
    (tmp_path / "pythonw.exe").write_text("")
    assert winsys.pythonw_for(str(exe)) == str(tmp_path / "pythonw.exe")


def test_single_instance(tmp_path):
    first = winsys.acquire_single_instance(lock_dir=tmp_path)
    assert first is not None
    try:
        assert winsys.acquire_single_instance(lock_dir=tmp_path) is None
    finally:
        first.release()
    again = winsys.acquire_single_instance(lock_dir=tmp_path)
    assert again is not None
    again.release()


def test_entry_point_arguments():
    from gc_tray import main
    a = main.parse_args(["--install"])
    assert a.install and not a.uninstall
    a = main.parse_args(["--uninstall", "--config", "x.json"])
    assert a.uninstall and a.config == "x.json"
    with pytest.raises(SystemExit):
        main.parse_args(["--install", "--uninstall"])


def test_the_entry_script_parses_and_points_at_the_package():
    import ast
    src = (REPO / "tray" / "hub_tray.pyw").read_text(encoding="utf-8")
    ast.parse(src)
    assert "gc_tray" in src
