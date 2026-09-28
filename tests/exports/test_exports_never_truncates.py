"""Property-style: whatever sequence of operations runs, the exporter never
truncates or rewrites a CSV. Every file it has touched is, after each of its
operations, a prefix-extension of its bytes before that operation.

Outside interference (someone appending, editing or truncating the file, a
lock, a partial write) is interleaved at random; those steps are the
*world's* doing and are not held to the property, but the exporter's next
step still is. When the run ends cleanly (no interference left unadopted),
every ledger row appears in the files exactly once.
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

import store
import exports
from exports_testlib import add_final, header_bytes, ledger_lines, make_db, pending


class _Lock:
    def __init__(self, path):
        raise PermissionError(13, "locked by Excel", str(path))


class _Partial:
    def __init__(self, path, n):
        self.fh = open(path, "ab")
        self.n = n

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.fh.close()

    def write(self, data):
        self.fh.write(data[: self.n])
        self.fh.flush()
        raise OSError(64, "network name no longer available")

    def fileno(self):
        return self.fh.fileno()

    def flush(self):
        self.fh.flush()


def _snapshot(paths) -> dict:
    return {p: (p.read_bytes() if p.exists() else b"") for p in paths}


@pytest.mark.parametrize("seed", range(40))
def test_exporter_only_ever_extends_files(tmp_path, seed, monkeypatch):
    rng = random.Random(seed)
    db = make_db(tmp_path / "db")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    real_open = exports._open_append
    exp = exports.HubExporter(db=db, data_dir=data_dir, retry_sleep=lambda s: None,
                              tail_bytes=rng.choice([16, 64, 4096]))
    touched: set[Path] = set()
    fresh_n = 0

    def exporter_step(name, fn):
        paths = touched | {exp.export_path("gc1")}
        before = _snapshot(paths)
        try:
            fn()
        except (exports.ExportRefused, OSError):
            pass
        after_paths = touched | {exp.export_path("gc1")}
        touched.update(p for p in after_paths if p.exists())
        for p, old in before.items():
            now = p.read_bytes() if p.exists() else b""
            assert now[: len(old)] == old, f"seed {seed}: {name} rewrote {p.name}"

    for _ in range(30):
        op = rng.choice(["add", "add", "flush", "flush", "lock", "partial", "restart",
                         "outside-append", "outside-edit", "outside-truncate", "adopt",
                         "new-path", "write-fresh", "tick"])
        path = exp.export_path("gc1")
        if op == "add":
            add_final(db)
        elif op in ("flush", "tick"):
            exporter_step(op, (lambda: exp.flush("gc1")) if op == "flush" else exp.tick)
        elif op == "lock":
            monkeypatch.setattr(exports, "_open_append", _Lock)
            exporter_step(op, lambda: exp.flush("gc1"))
            monkeypatch.setattr(exports, "_open_append", real_open)
        elif op == "partial":
            n = rng.randrange(1, 200)
            monkeypatch.setattr(exports, "_open_append", lambda p, n=n: _Partial(p, n))
            exporter_step(op, lambda: exp.flush("gc1"))
            monkeypatch.setattr(exports, "_open_append", real_open)
        elif op == "restart":
            exp = exports.HubExporter(db=db, data_dir=data_dir, retry_sleep=lambda s: None,
                                      tail_bytes=rng.choice([16, 64, 4096]))
        elif op == "outside-append" and path.exists():
            with path.open("ab") as fh:
                fh.write(b"outsider,row\r\n")
        elif op == "outside-edit" and path.exists() and path.stat().st_size > 10:
            data = bytearray(path.read_bytes())
            i = rng.randrange(len(data))
            data[i] = (data[i] + 1) % 256
            path.write_bytes(bytes(data))
        elif op == "outside-truncate" and path.exists() and path.stat().st_size > 10:
            path.write_bytes(path.read_bytes()[: rng.randrange(path.stat().st_size)])
        elif op == "adopt":
            exporter_step(op, lambda: exp.adopt("gc1"))
        elif op == "new-path":
            fresh_n += 1
            exporter_step(op, lambda: exp.new_path("gc1", data_dir / f"new{fresh_n}.csv"))
        elif op == "write-fresh":
            fresh_n += 1
            exporter_step(op, lambda: exp.write_fresh("gc1", data_dir / f"fresh{fresh_n}.csv"))


def test_clean_run_writes_every_row_exactly_once(tmp_path, monkeypatch):
    """Locks, partial writes and restarts only (no outsiders): the file ends
    as header + every ledger line once, in seq order."""
    for seed in range(25):
        rng = random.Random(1000 + seed)
        root = tmp_path / f"s{seed}"
        db = make_db(root)
        real_open = exports._open_append
        exp = exports.HubExporter(db=db, data_dir=root / "data", retry_sleep=lambda s: None)
        for _ in range(25):
            op = rng.choice(["add", "add", "flush", "lock", "partial", "restart"])
            if op == "add":
                add_final(db)
            elif op == "flush":
                exp.flush("gc1")
            elif op == "lock":
                monkeypatch.setattr(exports, "_open_append", _Lock)
                exp.flush("gc1")
                monkeypatch.setattr(exports, "_open_append", real_open)
            elif op == "partial":
                n = rng.randrange(1, 300)
                monkeypatch.setattr(exports, "_open_append", lambda p, n=n: _Partial(p, n))
                exp.flush("gc1")
                monkeypatch.setattr(exports, "_open_append", real_open)
            else:
                exp = exports.HubExporter(db=db, data_dir=root / "data",
                                          retry_sleep=lambda s: None)
        add_final(db)
        exp.flush("gc1")
        assert pending(db) == [], f"seed {seed}"
        expected = header_bytes() + b"".join(r["line"].encode() for r in ledger_lines(db))
        assert exp.export_path("gc1").read_bytes() == expected, f"seed {seed}"
