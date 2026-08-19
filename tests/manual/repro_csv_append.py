"""Reproduction harness for the "results don't append to the CSV" bug.

Drives the REAL distillation tunnel against the one real CDF on this box
(Feb24 n-alkanes.CDF) using a throwaway temp distill_output + processed dir, then
exercises the /api/reprocess and /api/export-lims routes the same way the UI does.

Run via tests/manual/run_repro.sh (uses the test venv). Prints, for each path,
whether a row actually landed in the CSV.
"""
from __future__ import annotations

import csv
import sys
import tempfile
import traceback
from pathlib import Path

# Ensure the webapp dir (two levels up) is importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

REAL_CDF = Path("/Volumes/Labsharedrive/Ryan C/GC Data/Feb24 n-alkanes.CDF")


def _banner(msg: str) -> None:
    print("\n" + "=" * 70 + f"\n{msg}\n" + "=" * 70)


def _csv_rows(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    if not REAL_CDF.is_file():
        print(f"SKIP: real CDF not found at {REAL_CDF}")
        return 0

    import settings as settings_mod
    import distill

    tmp = Path(tempfile.mkdtemp(prefix="gc_repro_"))
    out_csv = tmp / "distill_results.csv"
    proc = tmp / "processed"
    proc.mkdir()

    base = dict(settings_mod.load_settings())
    orig_loader = settings_mod.load_settings

    def fake_load():
        c = dict(base)
        c["distill_output"] = str(out_csv)
        c["processed_cdf_dir"] = str(proc)
        c["calibration_cdf"] = str(REAL_CDF)   # use the n-alkane std as cal
        c["calibration_assignments"] = ""       # force auto-detect
        c["correction_factors_json"] = ""       # avoid the prod network path
        return c

    settings_mod.load_settings = fake_load
    distill._SETTINGS_CACHE = None  # invalidate the cached real settings

    failures = 0

    # ── Test A: the raw distillation tunnel (what reprocess calls) ──────
    _banner("A) distill.process_cdf(reprocess=True) — the append tunnel")
    try:
        final = distill.process_cdf(REAL_CDF, reprocess=True)
        print("process_cdf returned:", final)
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print("process_cdf RAISED:", repr(exc))
        traceback.print_exc()
    rows = _csv_rows(out_csv)
    print(f"==> CSV row count after process_cdf: {len(rows)}")
    if rows:
        print("    sample:", rows[0].get("Lab ID"), "| 2887 IBP:", rows[0].get("2887 IBP"))
    else:
        failures += 1
        print("    !! NO ROW WRITTEN — append tunnel is broken in this env")

    sample_in_csv = rows[0]["Lab ID"] if rows else None
    inj_in_csv = rows[0]["InjectionDateTime"] if rows else ""

    # ── Test B: /api/reprocess route end-to-end ─────────────────────────
    _banner("B) POST /api/reprocess (route → looker → process_cdf)")
    try:
        import app as app_module
        app_module._watcher_stop.set()
        # app imports settings as settings_mod; align its loader too
        app_module.settings_mod.load_settings = fake_load
        distill._SETTINGS_CACHE = None
        client = app_module.app.test_client()
        before = len(_csv_rows(out_csv))
        resp = client.post("/api/reprocess", json={"paths": [str(REAL_CDF)]})
        print("reprocess response:", resp.status_code, resp.get_json())
        # the route queues a background task; drain it
        app_module._task_queue.join()
        after = len(_csv_rows(out_csv))
        print(f"==> CSV rows before={before} after={after}")
        # upsert: count stays 1 but values refresh — that's correct, not a bug
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print("reprocess route RAISED:", repr(exc))
        traceback.print_exc()

    # ── Test C: /api/export-lims sends the selection down the tunnel ────
    _banner("C) POST /api/export-lims {paths:[CDF]} — must compute + append")
    try:
        import app as app_module
        client = app_module.app.test_client()
        before = len(_csv_rows(out_csv))
        resp = client.post("/api/export-lims", json={"paths": [str(REAL_CDF)]})
        print("export-lims response:", resp.status_code, resp.get_json())
        app_module._task_queue.join()  # drain the queued export task
        after = len(_csv_rows(out_csv))
        print(f"==> CSV rows before={before} after={after}")
        if after > before:
            print(f"    OK: export appended a row (now {after} total)")
        else:
            failures += 1
            print("    !! export did NOT append a row — still broken")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print("export-lims route RAISED:", repr(exc))
        traceback.print_exc()

    settings_mod.load_settings = orig_loader
    _banner(f"DONE — hard failures: {failures}  (temp dir: {tmp})")
    return failures


if __name__ == "__main__":
    sys.exit(main())
