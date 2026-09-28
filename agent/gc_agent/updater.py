"""Self-update from the hub (checked at start, hourly, and when a heartbeat
reports a different package sha256).

download → verify sha256 → safe unzip into ``versions/<v>/`` → smoke test
(``python.exe`` beside the recorded ``pythonw.exe``, ``-c "import agent_main"``,
cwd ``versions/<v>``, 30 s) → ``previous.txt`` = old, ``current.txt`` = new
(both atomic) → keep three versions → the agent exits with code 3 and the
launcher starts the new one.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

from . import util
from .client import NetworkError

log = logging.getLogger("gc_agent.updater")

SMOKE_TIMEOUT = 30
KEEP = 3
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class UpdateError(Exception):
    pass


# ── files in the root ────────────────────────────────────────────────────
def read_pointer(root, name):
    try:
        v = (Path(root) / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return v if _VERSION_RE.match(v) else ""


def current_version(root):
    return read_pointer(root, "current.txt")


def previous_version(root):
    return read_pointer(root, "previous.txt")


def bad_packages(root):
    try:
        v = json.loads((Path(root) / "bad_packages.json").read_text(encoding="utf-8"))
        return [s for s in v if isinstance(s, str)] if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def package_sha_of(version_dir):
    try:
        return (Path(version_dir) / "PACKAGE_SHA256").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ── unzip ────────────────────────────────────────────────────────────────
def _unsafe(name):
    if not name or name.startswith(("/", "\\")) or _DRIVE_RE.match(name) or ":" in name:
        return True
    parts = re.split(r"[\\/]+", name)
    return any(p == ".." for p in parts)


def safe_extract(zip_bytes, dest):
    """Extract *zip_bytes* into *dest*, refusing the whole archive when any
    entry is absolute, has a drive letter, contains ``..`` or is a symlink.
    Nothing is written when refused."""
    try:
        z = zipfile.ZipFile(BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise UpdateError("not a zip: %s" % exc)
    with z:
        infos = z.infolist()
        for info in infos:
            if _unsafe(info.filename):
                raise UpdateError("unsafe path in package: %r" % info.filename)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateError("symlink in package: %r" % info.filename)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        base = dest.resolve()
        for info in infos:
            target = (dest / info.filename.replace("\\", "/")).resolve()
            if target != base and base not in target.parents:
                raise UpdateError("unsafe path in package: %r" % info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(str(target), "wb") as out:
                shutil.copyfileobj(src, out)


# ── smoke test ───────────────────────────────────────────────────────────
def smoke_python(recorded):
    """``python.exe`` beside the recorded ``pythonw.exe`` (a console
    interpreter, so the smoke test's output and exit code are real)."""
    if not recorded:
        return sys.executable
    p = Path(recorded)
    if p.name.lower() == "pythonw.exe":
        cand = p.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(p)


def smoke_test(python, version_dir, timeout=SMOKE_TIMEOUT):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run([python, "-c", "import agent_main"], cwd=str(version_dir), env=env,
                           capture_output=True, text=True, timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        return False, "timed out after %d s" % timeout
    except OSError as exc:
        return False, "cannot run %s: %s" % (python, exc)
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0, out[-500:]


# ── switch / prune ───────────────────────────────────────────────────────
def switch(root, new_version):
    cur = current_version(root)
    if cur and cur != new_version:
        util.atomic_write_text(Path(root) / "previous.txt", cur + "\n")
    util.atomic_write_text(Path(root) / "current.txt", new_version + "\n")


def prune(root, keep=KEEP):
    vdir = Path(root) / "versions"
    if not vdir.is_dir():
        return
    protect = {current_version(root), previous_version(root)} - {""}
    dirs = []
    for p in vdir.iterdir():
        if not p.is_dir():
            continue
        if p.name.startswith("."):
            shutil.rmtree(str(p), ignore_errors=True)   # stale temp extraction
            continue
        dirs.append(p)
    others = sorted((p for p in dirs if p.name not in protect),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    room = max(0, keep - len([p for p in dirs if p.name in protect]))
    for p in others[room:]:
        log.info("removing old agent version %s", p.name)
        shutil.rmtree(str(p), ignore_errors=True)


# ── the check ────────────────────────────────────────────────────────────
class Updater:
    def __init__(self, root, client, own_sha, python):
        self.root = Path(root)
        self.client = client
        self.own_sha = own_sha or ""
        self.python = python

    def check(self):
        """Returns 'current', 'skipped', 'switched' or 'refused: <why>'.
        Network trouble returns 'refused: ...' too; nothing is switched."""
        try:
            return self._check()
        except UpdateError as exc:
            log.error("update refused: %s", exc)
            return "refused: %s" % exc
        except NetworkError as exc:
            log.warning("update check failed: %s", exc)
            return "refused: network error: %s" % exc

    def _check(self):
        info = self.client.package_info()
        if info.status != 200 or not isinstance(info.json, dict):
            raise UpdateError("package info: HTTP %d %s" % (info.status, info.error_text()))
        version, sha = info.json.get("version"), info.json.get("sha256")
        if not isinstance(sha, str) or not re.match(r"^[0-9a-fA-F]{64}$", sha):
            raise UpdateError("package info has no valid sha256")
        sha = sha.lower()
        if sha == self.own_sha.lower():
            return "current"
        if sha in bad_packages(self.root):
            log.info("package %s was reverted before; not installing it again", sha[:12])
            return "skipped"
        if not isinstance(version, str) or not _VERSION_RE.match(version):
            raise UpdateError("bad package version %r" % (version,))
        z = self.client.package_zip()
        if z.status != 200:
            raise UpdateError("package download: HTTP %d" % z.status)
        got = util.sha256_bytes(z.body)
        if got != sha:
            raise UpdateError("downloaded package sha256 %s does not match %s" % (got, sha))

        vroot = self.root / "versions"
        vroot.mkdir(parents=True, exist_ok=True)
        name = version
        for cand in (version, "%s-%s" % (version, sha[:12])):
            d = vroot / cand
            if not d.exists():
                name = cand
                break
            if package_sha_of(d) == sha:
                name = cand
                break
        else:
            raise UpdateError("versions/%s exists with other content" % name)
        final = vroot / name
        if not (final.exists() and package_sha_of(final) == sha):
            tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=str(vroot)))
            try:
                safe_extract(z.body, tmp)
                if not (tmp / "agent_main.py").is_file() or not (tmp / "gc_agent" / "__init__.py").is_file():
                    raise UpdateError("package lacks agent_main.py or gc_agent/")
                util.atomic_write_text(tmp / "PACKAGE_SHA256", sha + "\n")
                os.replace(str(tmp), str(final))
            except BaseException:
                shutil.rmtree(str(tmp), ignore_errors=True)
                raise
        ok, out = smoke_test(smoke_python(self.python), final)
        if not ok:
            shutil.rmtree(str(final), ignore_errors=True)
            raise UpdateError("smoke test of %s failed: %s" % (name, out))
        switch(self.root, name)
        prune(self.root)
        log.info("switched to agent %s (%s); restarting", name, sha[:12])
        return "switched"
