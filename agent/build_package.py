#!/usr/bin/env python3
"""Build the GC agent's update package (contract §1):

    python agent/build_package.py --version v2.3.0 --out dist/gc-agent-v2.3.0.zip

The zip's root holds ``agent_main.py``, the ``gc_agent/`` package (``*.py``
only), ``VERSION`` and ``requirements-agent.txt``. It is built from an
allowlist, so ``agent.json`` / ``install.json`` (the only files that ever hold
a token) and ``launcher.pyw`` / ``install.pyw`` can never get in. The bytes
are deterministic (sorted entries, fixed timestamps and modes), so the same
source and version always give the same sha256, which is what the agents
compare. Prints ``<sha256>  <zip name>``.

Stdlib only. It lives in agent/ (not scripts/) because the hub release ships
agent/ and the hub (2B1) builds the zip at runtime: import ``build()`` or run
this file. It is not itself part of the zip (allowlist below).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import zipfile
from pathlib import Path

DEFAULT_AGENT_DIR = Path(__file__).resolve().parent
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)
_ROOT_FILES = ("agent_main.py", "requirements-agent.txt")


def _entries(agent_dir):
    agent_dir = Path(agent_dir)
    for name in _ROOT_FILES:
        p = agent_dir / name
        if not p.is_file():
            raise FileNotFoundError("agent package source is missing %s" % p)
        yield name, p.read_bytes()
    pkg = agent_dir / "gc_agent"
    if not (pkg / "__init__.py").is_file():
        raise FileNotFoundError("agent package source is missing %s" % (pkg / "__init__.py"))
    for p in sorted(pkg.rglob("*.py")):
        rel = p.relative_to(agent_dir)
        if "__pycache__" in rel.parts:
            continue
        yield rel.as_posix(), p.read_bytes()


def build(agent_dir, version, out_path):
    """Write the package zip to *out_path*; return its sha256 (hex)."""
    version = str(version).strip()
    if not version or any(c in version for c in "\r\n"):
        raise ValueError("bad version %r" % version)
    entries = dict(_entries(agent_dir))
    entries["VERSION"] = (version + "\n").encode("utf-8")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    with zipfile.ZipFile(str(tmp), "w") as z:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 0
            z.writestr(info, entries[name])
    os.replace(str(tmp), str(out_path))
    return hashlib.sha256(out_path.read_bytes()).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--agent-dir", default=str(DEFAULT_AGENT_DIR))
    a = ap.parse_args(argv)
    sha = build(a.agent_dir, a.version, a.out)
    print("%s  %s" % (sha, Path(a.out).name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
