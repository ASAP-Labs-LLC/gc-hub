"""Run a purge (or a purge recovery) on a data folder and SIGKILL this process
at a chosen stage, as a power cut or a killed hub would (tests/test_purge_recovery.py).

usage: purge_crash_child.py <data_dir> run <stage>       stage: vacuum | backup | txn |
                                                           after_commit | move3 | none
       purge_crash_child.py <data_dir> recover <stage>   stage: relink | move2 | notify | none

Prints what it did; exits 0 only when it was not killed. Not a test module.
"""
from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import exports  # noqa: E402
import purge  # noqa: E402
import store  # noqa: E402


def die(why: str) -> None:
    print("killed:", why, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)


def main() -> None:
    data, mode, stage = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    db = data / store.DB_FILENAME
    notes = []

    def notifier(level, message):
        notes.append(message)
        print("notify:", message, flush=True)

    if mode == "run":
        hook = None
        if stage == "vacuum":            # killed during VACUUM INTO: a half-written temp file
            def vacuum(_db, dest):
                dest.parent.mkdir(parents=True, exist_ok=True)
                purge._backup_tmp(dest).write_bytes(b"partial")
                die("during the backup")
            purge._backup = vacuum
        if stage == "backup":
            orig = purge._backup

            def backup(*a, **k):
                out = orig(*a, **k)
                die("after the backup")
                return out
            purge._backup = backup
        if stage == "txn":
            def hook(_conn):
                die("inside the transaction, every delete done")
        if stage == "after_commit":
            def relink(self, inst):
                die("after the commit, before the sidecar")
            exports.HubExporter.after_purge = relink

        def progress(ev):
            if stage == "move3" and ev.get("phase") == "moving files" and ev.get("done") == 3:
                die("after 3 moves")

        name = store.instruments.get("gc1", db=db)["name"]
        s = purge.run("gc1", "all", confirm_text=f"PURGE {name}", by="Ryan C (10.0.0.9)", db=db,
                      data_dir=data, progress=progress, notifier=notifier, _before_commit=hook)
        print("finished", s["state"], flush=True)
        return

    if stage == "relink":
        orig_ap = exports.HubExporter.after_purge

        def relink(self, inst):
            out = orig_ap(self, inst)
            die("recovery: after the sidecar")
            return out
        exports.HubExporter.after_purge = relink
    if stage == "move2":
        real = os.replace
        count = {"n": 0}

        def replace(src, dst):
            real(src, dst)
            if Path(dst).name != purge.JOURNAL:
                count["n"] += 1
                if count["n"] == 2:
                    die("recovery: after 2 moves")
        purge.os.replace = replace
    if stage == "notify":
        def notify(*_a, **_k):
            die("recovery: before the notification")
        purge._notify = notify
    out = purge.recover(db=db, data_dir=data, notifier=notifier)
    print("recovered", [j.get("state") for j in out], flush=True)


if __name__ == "__main__":
    main()
