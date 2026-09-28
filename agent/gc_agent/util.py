"""Small stdlib helpers shared by the agent modules."""
from __future__ import annotations

import datetime
import hashlib
import os
import tempfile
import time
from pathlib import Path


def local_iso(ts=None):
    """Local wall time as ``YYYY-MM-DDTHH:MM:SS`` (no timezone), per §1."""
    if ts is None:
        ts = time.time()
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def atomic_write_bytes(path, data, retries=10, tmp_prefix=None, tmp_suffix=".tmp"):
    """Write *data* to *path* via a temp file in the same folder, fsync and
    ``os.replace``. Retries briefly on Windows sharing violations. The temp
    file is ``<tmp_prefix><random><tmp_suffix>`` (default ``.<name>.*.tmp``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if tmp_prefix is None:
        tmp_prefix = "." + path.name + "."
    fd, tmp = tempfile.mkstemp(prefix=tmp_prefix, suffix=tmp_suffix, dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp, str(path))
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path, text):
    atomic_write_bytes(path, text.encode("utf-8"))
