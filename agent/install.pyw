"""GC agent installer. Double-click it (so it runs under the interpreter the
PC's .pyw association uses, the same one run.pyw uses) from the folder the
hub's "Download installer" produced:

    install.pyw  launcher.pyw  install.json  [agent-package.zip  agent-package.json]

``install.json`` = ``{"hub_url": ..., "token": ...}`` (minted for this PC by the
hub). Without ``agent-package.zip`` the package is downloaded from the hub.

Steps: check Python >= 3.9 and that pystray / PIL are importable (no pip);
unpack the package into ``%LOCALAPPDATA%\\ASAPLabs\\gc-agent\\versions\\<v>\\``;
copy launcher.pyw; write agent.json (hub_url, token, the recorded pythonw and
watch_dir, which defaults from the PC's ~/.gc_viewer_settings.json); register
autostart (HKCU Run); start the launcher.

The results mirror is optional and off by default (spec D9b: the hub appends
to the CSVs LEM tails). The installer never asks for it; only an explicit
``--mirror-path`` sets it, adopting an existing file (header check, last row
shown, sidecar). A reinstall keeps a mirror path already in agent.json.

Tests / scripted use:
    python install.pyw --dry-run --yes --root <dir> [--source <dir>]
                       [--watch-dir <dir>] [--mirror-path <csv>]
``--dry-run`` does everything except the autostart registration and starting
the launcher; ``--yes`` takes every default and confirms every question
(except deleting install.json: ``--delete-install-json``).

When the agent is already running (its launcher holds the single-instance
lock), only hub_url and token are updated in agent.json, in place; the agent
reloads them. After a successful run the installer offers to delete
install.json, which holds the token.
"""
import argparse
import importlib.machinery
import importlib.util
import json
import ntpath
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

MIN_PY = (3, 9)
REQUIRED = ("pystray", "PIL")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ASAPLabs GC Agent"
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_IS_WIN = sys.platform == "win32"


class InstallError(Exception):
    pass


# ── checks ───────────────────────────────────────────────────────────────
def check_python(version_info):
    if tuple(version_info[:2]) < MIN_PY:
        return ("The GC agent needs Python %d.%d or newer; this is Python %s. "
                "Install a newer Python, then run install.pyw again."
                % (MIN_PY + (".".join(str(x) for x in version_info[:3]),)))
    return None


def check_deps(find_spec=importlib.util.find_spec):
    missing = []
    for name in REQUIRED:
        try:
            if find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return missing


def default_root():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ASAPLabs" / "gc-agent"


def record_python():
    """The interpreter the launcher and autostart run: pythonw.exe when it
    sits beside a console python.exe (no console window)."""
    exe = Path(sys.executable)
    if _IS_WIN and exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists():
        return str(exe.with_name("pythonw.exe"))
    return str(exe)


def legacy_defaults(home=None):
    """watch_dir and distill_output from this PC's v1 settings, if present."""
    p = Path(home or Path.home()) / ".gc_viewer_settings.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    if not isinstance(d, dict):
        return "", ""
    w, o = d.get("watch_dir"), d.get("distill_output")
    return (w if isinstance(w, str) else ""), (o if isinstance(o, str) else "")


def autostart_command(python, root):
    return '"%s" "%s"' % (python, ntpath.join(str(root), "launcher.pyw"))


# ── UI ───────────────────────────────────────────────────────────────────
class ConsoleUI:
    def info(self, msg):
        print(msg)

    def error(self, msg):
        print("ERROR: " + msg)

    def ask_dir(self, title, initial):
        return initial

    def confirm(self, msg):
        print(msg + " -> yes")
        return True


class TkUI:  # pragma: no cover - interactive
    def __init__(self):
        import tkinter
        from tkinter import filedialog, messagebox
        self._tk = tkinter.Tk()
        self._tk.withdraw()
        self._fd, self._mb = filedialog, messagebox

    def info(self, msg):
        self._mb.showinfo("GC agent installer", msg)

    def error(self, msg):
        self._mb.showerror("GC agent installer", msg)

    def ask_dir(self, title, initial):
        self._mb.showinfo("GC agent installer", title)
        got = self._fd.askdirectory(title=title, initialdir=initial or None, mustexist=True)
        if not got:
            raise InstallError("cancelled")
        return os.path.normpath(got)

    def confirm(self, msg):
        return self._mb.askyesno("GC agent installer", msg)


# ── package ──────────────────────────────────────────────────────────────
def _load_module(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def read_install_json(src):
    p = Path(src) / "install.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError("cannot read install.json beside the installer (%s). Download the "
                           "installer again from the hub's Instruments page." % exc)
    url, tok = d.get("hub_url") if isinstance(d, dict) else None, d.get("token") if isinstance(d, dict) else None
    if not isinstance(url, str) or not url.startswith(("http://", "https://")) \
            or not isinstance(tok, str) or not tok.strip():
        raise InstallError("install.json must hold hub_url and token")
    return url.rstrip("/"), tok.strip()


def _http_get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=120) as r:
            return r.read()
    except Exception as exc:
        raise InstallError("cannot download %s: %s" % (url, exc))


def obtain_package(src, hub_url, token):
    """Returns (zip bytes, version, sha256), verified."""
    import hashlib
    src = Path(src)
    zp = src / "agent-package.zip"
    if zp.is_file():
        data = zp.read_bytes()
        meta = {}
        mp = src / "agent-package.json"
        if mp.is_file():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except ValueError:
                raise InstallError("agent-package.json is not valid JSON")
    else:
        try:
            meta = json.loads(_http_get(hub_url + "/api/agent/package", token).decode("utf-8"))
        except ValueError:
            raise InstallError("the hub's /api/agent/package did not return JSON")
        data = _http_get(hub_url + "/api/agent/package.zip", token)
    sha = hashlib.sha256(data).hexdigest()
    want = meta.get("sha256") if isinstance(meta, dict) else None
    if want and want.lower() != sha:
        raise InstallError("agent package sha256 %s does not match the expected %s" % (sha, want))
    version = meta.get("version") if isinstance(meta, dict) else None
    if not version:
        import zipfile
        from io import BytesIO
        try:
            version = zipfile.ZipFile(BytesIO(data)).read("VERSION").decode("utf-8").strip()
        except Exception:
            version = ""
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise InstallError("agent package has no usable version (%r)" % (version,))
    return data, version, sha


def agent_modules(zip_bytes, workdir):
    """Import gc_agent from the package zip itself (zipimport), so the
    installer uses the package's own safe unzip, config and mirror code."""
    zp = Path(workdir) / "agent-package.zip"
    zp.write_bytes(zip_bytes)
    if "gc_agent" not in sys.modules:
        sys.path.insert(0, str(zp))
    try:
        from gc_agent import config, mirror, updater  # noqa: F401
    except Exception as exc:
        raise InstallError("the agent package cannot be loaded: %s" % exc)
    return config, mirror, updater


def _results_seq(root):
    db = Path(root) / "ledger.db"
    if not db.is_file():
        return 0
    try:
        con = sqlite3.connect(str(db))
        try:
            row = con.execute("SELECT value FROM kv WHERE key='results_seq'").fetchone()
        finally:
            con.close()
        return int(row[0]) if row else 0
    except (sqlite3.Error, ValueError):
        return 0


def _unpack(updater, zip_bytes, version, sha, root):
    vroot = Path(root) / "versions"
    vroot.mkdir(parents=True, exist_ok=True)
    name = version
    for cand in (version, "%s-%s" % (version, sha[:12])):
        name = cand
        d = vroot / cand
        if not d.exists() or updater.package_sha_of(d) == sha:
            break
    final = vroot / name
    if final.exists() and updater.package_sha_of(final) == sha:
        return name
    if final.exists():
        shutil.rmtree(str(final))
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=str(vroot)))
    try:
        try:
            updater.safe_extract(zip_bytes, tmp)
        except updater.UpdateError as exc:
            raise InstallError("agent package refused: %s" % exc)
        if not (tmp / "agent_main.py").is_file() or not (tmp / "gc_agent" / "__init__.py").is_file():
            raise InstallError("agent package lacks agent_main.py or gc_agent/")
        (tmp / "PACKAGE_SHA256").write_text(sha + "\n", encoding="utf-8")
        os.replace(str(tmp), str(final))
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
    return name


def register_autostart(cmd):  # pragma: no cover - Windows only
    if not _IS_WIN:
        return False
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cmd)
    return True


def start_launcher(python, root):  # pragma: no cover - exercised by hand
    kw = {"cwd": str(root), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True}
    if _IS_WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen([python, str(Path(root) / "launcher.pyw"), "--root", str(root)], **kw)


def update_running(launcher, root, hub_url, token, ui):
    """The agent is running: change only hub_url and token in agent.json.
    The agent notices the file changed and reloads it (no restart needed)."""
    cfg_path = Path(root) / "agent.json"

    def read():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            return raw
        except (OSError, ValueError) as exc:
            raise InstallError("The GC agent is running but its agent.json cannot be read (%s). "
                               "Quit it from its tray icon, then run the installer again." % exc)

    # The running agent may write agent.json itself (Pause/Resume) between our
    # read and write, from a copy it read before ours. So: re-read right before
    # writing, merge only our two keys, then check they stuck; retry if not.
    for _attempt in range(5):
        raw = read()
        raw["hub_url"], raw["token"] = hub_url, token
        launcher.atomic_write_text(cfg_path, json.dumps(raw, indent=2, sort_keys=True) + "\n")
        time.sleep(0.2)
        now = read()
        if now.get("hub_url") == hub_url and now.get("token") == token:
            break
    else:
        raise InstallError("agent.json kept changing under the installer; quit the GC agent "
                           "from its tray icon and run the installer again.")
    ui.info("The GC agent is running, so only its hub URL and token were updated in "
            "agent.json; it picks them up within a few seconds. Nothing else was changed.")


def offer_delete_install_json(src, args, ui):
    """install.json holds this PC's token; it is not needed after installing."""
    p = Path(src) / "install.json"
    if not p.exists():
        return
    if args.yes:
        if not args.delete_install_json:
            return
    elif not ui.confirm("Delete install.json from the download folder? It holds this PC's "
                        "agent token and is no longer needed."):
        return
    try:
        p.unlink()
        ui.info("Deleted %s." % p)
    except OSError as exc:
        ui.info("Could not delete %s: %s" % (p, exc))


# ── install ──────────────────────────────────────────────────────────────
def install(args, ui):
    src = Path(args.source)
    hub_url, token = read_install_json(src)
    launcher_src = src / "launcher.pyw"
    if not launcher_src.is_file():
        raise InstallError("launcher.pyw is missing beside the installer")
    launcher = _load_module(launcher_src, "gc_install_launcher")
    root = Path(args.root) if args.root else default_root()
    root.mkdir(parents=True, exist_ok=True)
    held = launcher.acquire_single_instance(root)
    if held is None:
        update_running(launcher, root, hub_url, token, ui)
        offer_delete_install_json(src, args, ui)
        return 0
    work = tempfile.mkdtemp(prefix="gc-agent-install-")
    try:
        data, version, sha = obtain_package(src, hub_url, token)
        config, mirror, updater = agent_modules(data, work)
        name = _unpack(updater, data, version, sha, root)

        tmp = root / ".launcher.pyw.tmp"
        shutil.copyfile(str(launcher_src), str(tmp))
        os.replace(str(tmp), str(root / "launcher.pyw"))

        cfg_path = root / "agent.json"
        existing = {}
        if cfg_path.is_file():
            try:
                existing = config.read_raw(cfg_path)
            except config.ConfigError:
                existing = {}
        python = record_python()
        cfg = dict(existing)
        cfg.update({"hub_url": hub_url, "token": token, "python": python})
        lw, _ = legacy_defaults()
        watch = args.watch_dir if args.watch_dir is not None else (existing.get("watch_dir") or lw)
        cfg["watch_dir"] = ui.ask_dir("Choose the folder where ChemStation writes the .CDF files",
                                      watch)
        # Off unless asked for explicitly (D9b); a reinstall keeps an earlier choice.
        mpath = args.mirror_path if args.mirror_path is not None \
            else (existing.get("results_mirror_path") or "")
        cfg["results_mirror_path"] = mpath or ""
        try:
            cfg = config.validate(cfg)
        except config.ConfigError as exc:
            raise InstallError(str(exc))

        if mpath and args.mirror_path is not None:
            if not Path(mpath).parent.is_dir():
                raise InstallError("the folder for the results CSV does not exist: %s"
                                   % Path(mpath).parent)
            info = mirror.inspect(mpath)
            if info["exists"]:
                if not info["header_ok"]:
                    raise InstallError("%s does not start with the %d-column results header, so it "
                                       "cannot be adopted. Choose another file, or none."
                                       % (mpath, len(mirror.CSV_HEADER)))
                if not info["adopted"]:
                    if not ui.confirm("Adopt the existing results file?\n%s\nsize: %d bytes\n"
                                      "last row: %s" % (mpath, info["size"], info["last_row"] or "(none)")):
                        raise InstallError("the results file was not adopted; nothing more was changed")
                    mirror.adopt(mpath, _results_seq(root))
                    ui.info("Adopted %s." % mpath)

        config.save(cfg_path, cfg)
        updater.switch(root, name)

        cmd = autostart_command(python, root)
        if args.dry_run:
            ui.info("Dry run: not registering autostart (HKCU\\%s\\%s = %s) and not starting "
                    "the launcher." % (RUN_KEY, RUN_VALUE, cmd))
        else:
            if not register_autostart(cmd):
                ui.info("Autostart not registered (not Windows).")
        held.release()
        held = None
        if not args.dry_run:
            start_launcher(python, root)
        ui.info("Installed the GC agent %s in %s." % (name, root))
        offer_delete_install_json(src, args, ui)
        return 0
    finally:
        if held is not None:
            held.release()
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None, find_spec=importlib.util.find_spec):
    ap = argparse.ArgumentParser(prog="install.pyw")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="non-interactive: take the defaults")
    ap.add_argument("--root", default=None)
    ap.add_argument("--source", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--watch-dir", default=None)
    ap.add_argument("--delete-install-json", action="store_true",
                    help="with --yes: delete install.json (it holds the token) after success")
    ap.add_argument("--mirror-path", default=None,
                    help="optional results mirror CSV (off by default); an existing file is adopted")
    args = ap.parse_args(argv)
    ui = ConsoleUI() if args.yes else TkUI()
    msg = check_python(sys.version_info)
    if msg:
        ui.error(msg)
        return 2
    missing = check_deps(find_spec)
    if missing:
        ui.error("This Python cannot import %s, which the GC agent's tray icon needs. Use the "
                 "Python that runs run.pyw (it has them), then run install.pyw again."
                 % ", ".join(missing))
        return 2
    try:
        return install(args, ui)
    except InstallError as exc:
        ui.error(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
