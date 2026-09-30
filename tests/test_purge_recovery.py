"""A purge killed at every stage, then recovered (v3.1 critic, B1).

Each case copies the two-GC hub, runs ``tests/purge_crash_child.py`` in a
subprocess that SIGKILLs itself at one stage, and then runs
``purge.recover`` (what ``hub.start`` does before the exporter starts):

* killed before the COMMIT (during/after the backup, inside the transaction):
  nothing changed, the journal is ``abandoned``, the unused backup (and a
  half-written one) is deleted, a notification says nothing was removed;
* killed after the COMMIT (before the sidecar, mid-move): the purge is
  finished: every row of GC-1 gone and nothing else touched, every planned
  file moved, the results CSV unlinked (the next GC-1 row appends, no
  ledger-mismatch), a notification "finished after a restart";
* the recovery itself killed (after the sidecar, mid-move, before the
  notification) and run again ends in the same state;
* a second recovery does nothing.

The last test boots the real app on a data folder killed after its COMMIT:
recovery runs at start, GC-1's ingest is accepted and its export is not refused.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("netCDF4")
pytest.importorskip("scipy")
if os.name == "nt":                                     # SIGKILL of self
    pytest.skip("POSIX kill semantics", allow_module_level=True)

import purge_helpers as ph  # noqa: E402
from purge_helpers import dump, tree, without  # noqa: E402

import exports  # noqa: E402
import purge  # noqa: E402
import store  # noqa: E402

CHILD = Path(__file__).resolve().parent / "purge_crash_child.py"


@pytest.fixture(scope="module")
def template():
    root = Path(tempfile.mkdtemp(prefix="gc-purge-rec-tpl-"))
    try:
        yield ph.build_two_gc_hub(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def h(template, tmp_path):
    return ph.HubCopy(template, tmp_path)


def _child(h, mode, stage, killed=True):
    r = subprocess.run([sys.executable, str(CHILD), str(h.data), mode, stage],
                       capture_output=True, text=True, timeout=120)
    if killed:
        assert r.returncode == -9 and "killed:" in r.stdout, (r.returncode, r.stdout, r.stderr)
    else:
        assert r.returncode == 0, (r.stdout, r.stderr)
    return r.stdout


def _recover(h):
    return purge.recover(db=h.db, data_dir=h.data, notifier=h.notifier)


def _journals(h):
    root = h.data / "purged"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(root.glob("*/manifest.json"))]


def _state(h):
    return dump(h.db), tree(h.data / "cdf")


def _assert_unchanged(h, before):
    db, files = before
    assert dump(h.db) == db
    assert tree(h.data / "cdf") == files
    [j] = _journals(h)
    assert j["state"] == "abandoned"
    assert not Path(j["backup"]).exists()
    assert not list((h.data / "backups").glob("*pre-purge-*"))      # nor a temp file
    assert any("interrupted before it changed anything; nothing was removed" in m
               for _l, m in h.notes)


def _assert_finished(h, before):
    db, files = before
    gc1 = {r["id"] for r in db["samples"] if r["instrument_id"] == "gc1"}
    assert dump(h.db) == without(db, gc1)
    [j] = _journals(h)
    assert j["state"] == "done" and j["recovered"] is True and j["export_relinked"]
    moves = j["moves"]
    assert moves and all((h.data / m["to"]).is_file() and not (h.data / m["from"]).exists()
                         for m in moves)
    cal = store.instruments.get("gc1", db=h.db)["calibration_cdf"]
    assert {f"cdf/gc1/{p}" for p in tree(h.data / "cdf" / "gc1")} == {cal}   # no orphans
    assert set(tree(h.data / "cdf")) | {m["from"][4:] for m in moves} >= set(files)
    assert Path(j["backup"]).is_file()
    assert sum("finished after a restart" in m for _l, m in h.notes) == 1
    # the results CSV was unlinked: a new GC-1 result appends, nothing refused
    csv = h.exporter.export_path("gc1")
    old = csv.read_bytes()
    import pipeline
    sid = pipeline.submit("gc1", h.cdf("88001", datetime(2026, 9, 28, 9, 0)), conf=h.conf,
                          data_dir=h.data, db=h.db).sample_id
    h.worker().run_until_idle()
    assert store.samples.get(sid, db=h.db)["status"] == "final"
    r = h.exporter.flush("gc1")
    assert r.error is None and r.appended == 1
    assert csv.read_bytes().startswith(old)
    assert _recover(h) == []                                     # a second recovery: nothing


@pytest.mark.parametrize("stage", ["vacuum", "backup", "txn"])
def test_killed_before_the_commit_changes_nothing(h, stage):
    before = _state(h)
    _child(h, "run", stage)
    [j] = _journals(h)
    assert j["state"] == "pending"
    assert dump(h.db) == before[0]                   # the transaction never committed
    out = _recover(h)
    assert [s["state"] for s in out] == ["abandoned"]
    _assert_unchanged(h, before)
    assert _recover(h) == []


@pytest.mark.parametrize("stage", ["after_commit", "move3"])
def test_killed_after_the_commit_is_finished_at_the_next_start(h, stage):
    before = _state(h)
    _child(h, "run", stage)
    [j] = _journals(h)
    assert j["state"] in ("pending", "committed")
    side = json.loads(Path(str(h.exporter.export_path("gc1")) + ".gchub.json").read_text())
    # killed before the sidecar: still linked to a purged row (the next append
    # would refuse ledger-mismatch); killed mid-move: already unlinked
    assert bool(side["last_line_sha256"]) is (stage == "after_commit")
    out = _recover(h)
    assert [s["state"] for s in out] == ["done"]
    _assert_finished(h, before)


@pytest.mark.parametrize("stage", ["relink", "move2", "notify"])
def test_a_recovery_killed_part_way_is_recovered_again(h, stage):
    before = _state(h)
    _child(h, "run", "after_commit")
    _child(h, "recover", stage)
    out = _recover(h)
    assert [s["state"] for s in out] == ["done"]
    _assert_finished(h, before)


def test_a_committed_purge_whose_journal_still_says_pending_is_finished(h, monkeypatch):
    """The crash fell between the COMMIT and the journal update."""
    before = _state(h)
    real = purge._save_quietly
    monkeypatch.setattr(purge, "_save_quietly", lambda *a: None)       # journal never updated

    def stop(*_a, **_k):
        raise KeyboardInterrupt("killed")
    monkeypatch.setattr(purge.exports.HubExporter, "after_purge", stop)
    with pytest.raises(KeyboardInterrupt):
        h.run()
    monkeypatch.setattr(purge, "_save_quietly", real)
    monkeypatch.undo()
    [j] = _journals(h)
    assert j["state"] == "pending"
    assert _recover(h)[0]["state"] == "done"
    _assert_finished(h, before)


def test_the_booted_hub_recovers_before_its_exporter(template, tmp_path):
    from bootapp import booted, post, setup_admin, wait_for
    from ingest_helpers import ingest   # noqa: F401 (path set up below)
    import ingest_api
    h = ph.HubCopy(template, tmp_path)
    _child(h, "run", "after_commit")
    token = ingest_api.mint_token("gc1", db=h.db)
    with booted(tmp_path) as (port, _proc, data, _home):
        assert wait_for(lambda: _journals(h)[0]["state"] == "done", timeout=60)
        pw = setup_admin(port, data)
        body = h.cdf("88002", datetime(2026, 9, 28, 10, 0)).read_bytes()
        code, res = ingest(port, token, body)
        assert code == 201, res
        assert wait_for(lambda: store.samples.get(res["sample_id"], db=h.db)["status"] == "final",
                        timeout=60)

        def exported():
            _c, st = post(port, "/api/admin/exports", {"password": pw})
            gc1 = {s["instrument"]: s for s in st["instruments"]}["gc1"]
            assert gc1["refused"] is None, gc1
            return gc1["pending"] == 0
        assert wait_for(exported, timeout=60)
        notes = json.loads((data / "notifications.json").read_text(encoding="utf-8"))
        text = json.dumps(notes)
        assert "finished after a restart" in text


sys.path.insert(0, str(Path(__file__).resolve().parent / "ingest"))
