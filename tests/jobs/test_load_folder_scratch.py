"""The parity runbook on ASAPSV1 (release-candidate fixes): ``tools/load_folder.py
--process`` on a SCRATCH data folder works while the live hub answers on its
own port, a folder a hub has served is refused without an explicit
``--hub-port``, and ``--copy-instrument-from`` copies production's instrument
(calibration, method map, corrections, settings, standards) into the scratch
store, reading production only."""
from __future__ import annotations

from loader_testlib import SIMDIS, hub  # noqa: F401

import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import cdf_fixtures as fx
import make_golden
import store
from jobs import load_folder as load_folder_mod

REPO = Path(__file__).resolve().parent.parent.parent
CLI = REPO / "tools" / "load_folder.py"
INJECTED = datetime(2026, 9, 25, 14, 23)


def _cli(*args, env=None):
    e = dict(os.environ)
    e.pop("GC_DATA_DIR", None)
    e.update(env or {})
    return subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True,
                          text=True, env=e, timeout=180)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _golden_values(hub) -> dict:
    import corrections
    return corrections.seed_from_file(str(hub.corrections))


def _set_corrections(db, instrument_id, values):
    with store.connection(db) as conn:
        with store.write_txn(conn):
            store.corrections.set_all(conn, instrument_id, values, by="test", reason="prod values")


def _production(hub, *, gc2=True, shift=1.0):
    """``hub.data`` as the production folder: settings.json, a comparison
    standard, gc1 bootstrapped, and (optionally) gc2 with an absolute
    calibration path and hub corrections shifted by ``shift`` from the file."""
    (hub.data / "settings.json").write_text(json.dumps(hub.conf), encoding="utf-8")
    std = hub.data / "gc_comparison_standards"
    std.mkdir()
    fx.sample_cdf(std / "Diesel.CDF", name="Diesel", method_name=SIMDIS)
    hub.gc1()
    values = {c: v + shift for c, v in _golden_values(hub).items()}
    if gc2:
        hub.gc2(calibration_cdf=str(hub.cal),
                calibration_assignments=json.dumps(make_golden.calibration_entries()),
                calibration_sensitivity=50, export_path=str(hub.root / "share" / "gc2.csv"),
                live_since=datetime(2026, 9, 1))
        _set_corrections(hub.db, "gc2", values)
    (hub.data / "app.log").write_text("the hub ran here\n", encoding="utf-8")
    return values


def _robocopy(tmp_path, name="40401"):
    src = tmp_path / "robocopy"
    src.mkdir()
    fx.sample_cdf(src / f"{name}.CDF", name=name, injected=INJECTED, method_name=SIMDIS)
    return src


def _rev(db, sample_id):
    with store.connection(db) as conn:
        return dict(conn.execute(
            "SELECT r.* FROM samples s JOIN sample_results r ON r.sample_id=s.id "
            "AND r.revision=s.current_revision WHERE s.id=?", (sample_id,)).fetchone())


# ── A: --process on a scratch folder ignores the live hub's port ────────────

def test_process_on_a_scratch_folder_runs_while_the_live_hub_answers_on_its_port(hub, tmp_path):
    values = _production(hub)
    prod_db_sha = _sha(hub.db)
    src = _robocopy(tmp_path)
    scratch = tmp_path / "parity-gc2"                     # doesn't exist yet
    with socket.socket() as live:                         # the live hub, on the hub's own port
        live.bind(("127.0.0.1", 0))
        live.listen(1)
        port = live.getsockname()[1]
        res = _cli("--data-dir", scratch, "--copy-instrument-from", hub.data, "--process",
                   "--json", "gc2", src, env={"PORT": str(port), "GC_PORT": str(port)})
    assert res.returncode == 0, res.stderr + res.stdout
    summary = json.loads(res.stdout)
    assert summary["created"] == 1 and summary["processed_jobs"] >= 1
    copied = summary["copied_instrument"]
    assert copied["created"] is True and copied["corrections"] == 11
    assert copied["settings_copied"] is True and copied["standards_copied"] == 1

    sdb = scratch / "gc.db"
    (sid,) = summary["sample_ids"]
    s = store.samples.get(sid, db=sdb)
    assert s["status"] == "final", s
    assert _rev(sdb, sid) and json.loads(_rev(sdb, sid)["corrections_used"])["values"] == values
    assert not (scratch / load_folder_mod.PROCESS_LOCK).exists()
    # production was only read
    assert _sha(hub.db) == prod_db_sha
    assert store.samples.count(db=hub.db) == 0


def test_the_copy_takes_calibration_method_map_and_corrections_never_the_export_path(hub, tmp_path):
    values = _production(hub)
    prod = store.instruments.get("gc2", db=hub.db)
    scratch = tmp_path / "scratch"
    res = _cli("--data-dir", scratch, "--copy-instrument-from", hub.data, "gc2", _robocopy(tmp_path))
    assert res.returncode == 0, res.stderr + res.stdout
    assert "copied gc2 from" in res.stdout

    row = store.instruments.get("gc2", db=scratch / "gc.db")
    assert row["name"] == prod["name"] and row["method_map"] == prod["method_map"]
    assert row["live_since"] == prod["live_since"] and row["enabled"] == 1
    assert row["export_path"] is None and row["token_hash"] is None
    # an absolute calibration outside the production folder: copied under calibration/<id>/
    rel = Path(row["calibration_cdf"])
    assert not rel.is_absolute() and rel.parts[:2] == ("calibration", "gc2")
    assert _sha(scratch / rel) == _sha(hub.cal)
    assert json.loads(row["calibration_assignments"]) == make_golden.calibration_entries()
    assert store.corrections.read("gc2", db=scratch / "gc.db")["values"] == values
    audit = store.corrections.audit("gc2", db=scratch / "gc.db")
    assert audit and "parity run" in audit[0]["reason"]
    assert json.loads((scratch / "settings.json").read_text()) == hub.conf
    assert (scratch / "gc_comparison_standards" / "Diesel.CDF").is_file()


def test_a_data_relative_calibration_keeps_its_relative_path(hub, tmp_path):
    _production(hub, gc2=False)
    rel = Path("cdf") / "gc1" / "2026" / "09" / "CAL_1.CDF"
    (hub.data / rel).parent.mkdir(parents=True)
    (hub.data / rel).write_bytes(hub.cal.read_bytes())
    store.instruments.upsert({"id": "gc1", "calibration_cdf": str(rel)}, db=hub.db)
    values = {c: v + 2.0 for c, v in _golden_values(hub).items()}
    _set_corrections(hub.db, "gc1", values)
    scratch = tmp_path / "scratch"
    res = _cli("--data-dir", scratch, "--copy-instrument-from", hub.data, "--process", "--json",
               "gc1", _robocopy(tmp_path))
    assert res.returncode == 0, res.stderr + res.stdout
    row = store.instruments.get("gc1", db=scratch / "gc.db")
    assert Path(row["calibration_cdf"]) == rel and (scratch / rel).is_file()
    (sid,) = json.loads(res.stdout)["sample_ids"]
    assert store.samples.get(sid, db=scratch / "gc.db")["status"] == "final"
    # production's hub values, not a seed from the phase-1 file
    assert json.loads(_rev(scratch / "gc.db", sid)["corrections_used"])["values"] == values


# ── refusals: nothing loaded, production untouched ──────────────────────────

def test_process_is_refused_on_a_folder_a_hub_has_served_without_hub_port(hub, tmp_path):
    _production(hub)
    src = _robocopy(tmp_path)
    res = _cli("--data-dir", hub.data, "--process", "gc1", src)
    assert res.returncode == 2
    assert "app.log" in res.stderr and "--hub-port" in res.stderr
    assert store.samples.count(db=hub.db) == 0
    # with the (stopped) hub's port named, it runs
    res = _cli("--data-dir", hub.data, "--process", "--hub-port", _free_port(), "gc1", src)
    assert res.returncode == 0, res.stderr + res.stdout


def test_the_copy_is_refused_into_a_folder_a_hub_has_served(hub, tmp_path):
    _production(hub)
    other = tmp_path / "other"
    other.mkdir()
    (other / "app.log").write_text("x")
    res = _cli("--data-dir", other, "--copy-instrument-from", hub.data, "gc2", _robocopy(tmp_path))
    assert res.returncode == 2 and "app.log" in res.stderr
    assert not (other / "gc.db").exists()


def test_the_copy_is_refused_onto_itself(hub, tmp_path):
    _production(hub)
    res = _cli("--data-dir", hub.data, "--copy-instrument-from", hub.data, "gc2", _robocopy(tmp_path))
    assert res.returncode == 2 and "scratch" in res.stderr


def test_the_copy_is_refused_when_production_lacks_the_instrument(hub, tmp_path):
    _production(hub, gc2=False)
    scratch = tmp_path / "scratch"
    res = _cli("--data-dir", scratch, "--copy-instrument-from", hub.data, "gc3", _robocopy(tmp_path))
    assert res.returncode == 2 and "gc3" in res.stderr
    assert store.samples.count(db=scratch / "gc.db") == 0


def test_the_copy_is_refused_when_the_calibration_cdf_is_missing(hub, tmp_path):
    _production(hub)
    store.instruments.upsert({"id": "gc2", "calibration_cdf": str(tmp_path / "gone.CDF")}, db=hub.db)
    scratch = tmp_path / "scratch"
    res = _cli("--data-dir", scratch, "--copy-instrument-from", hub.data, "gc2", _robocopy(tmp_path))
    assert res.returncode == 2 and "calibration CDF is not found" in res.stderr
    assert store.instruments.get("gc2", db=scratch / "gc.db") is None


def test_copy_instrument_refuses_a_different_calibration_file_already_in_scratch(hub, tmp_path):
    _production(hub)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    store.migrate(scratch / "gc.db")
    load_folder_mod.copy_instrument("gc2", hub.data, data_dir=scratch)
    row = store.instruments.get("gc2", db=scratch / "gc.db")
    (scratch / row["calibration_cdf"]).write_bytes(b"other")
    import pytest
    with pytest.raises(load_folder_mod.CopyInstrumentError, match="different content"):
        load_folder_mod.copy_instrument("gc2", hub.data, data_dir=scratch)
    # a re-run with the same files is fine and keeps the corrections unchanged
    (scratch / row["calibration_cdf"]).write_bytes(hub.cal.read_bytes())
    again = load_folder_mod.copy_instrument("gc2", hub.data, data_dir=scratch)
    assert again["created"] is False
    assert len(store.corrections.audit("gc2", db=scratch / "gc.db")) == 11
