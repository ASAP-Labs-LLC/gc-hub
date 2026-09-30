"""A v1 history import killed part-way (a restart, an update, a power cut) and
started again (v3.1).

The import commits batch by batch; a kill mid-batch rolls that batch back
(SQLite) and may leave its staged copies in ``cdf/.incoming`` and the files it
had already moved into place. At the next start ``hub.start`` marks the run
``interrupted`` (``import_runs.stopped``), removes the importer's staged
copies and raises one notification ("... after N of M; press Start to resume,
nothing was lost"); pressing Start again resumes, and the result is the same
as an import that was never interrupted: the same samples and revisions, the
same files, nothing left over.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for _p in (TESTS.parent, TESTS, TESTS / "import_history", TESTS / "pipeline", TESTS / "golden"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("netCDF4")
if os.name == "nt":
    pytest.skip("POSIX kill semantics", allow_module_level=True)

from import_history_testlib import ALIASES, row, sample, src, tree, write_csv  # noqa: E402
from pipeline_helpers import Hub  # noqa: E402

import hub  # noqa: E402
import store  # noqa: E402
from jobs.import_history import import_history  # noqa: E402

CHILD = TESTS / "import_crash_child.py"
BATCH = 2
TIMES = [datetime(2026, 9, 10 + i, 9 + i, 5 * i, 7) for i in range(7)]


def _build(root: Path) -> dict:
    h = Hub(root)
    h.gc1()
    processed = root / "robocopy" / "processed_cdfs2"
    rows = []
    for i, t in enumerate(TIMES):
        p = sample(processed, f"H{i}", t, shift=0.01 * i)
        rows.append(row(f"H{i}", t.isoformat(sep=" "), source=src(p.name), ibp=f"25{i}.5"))
    csv = write_csv(root / "distill_results.csv", rows)
    (h.data / "settings.json").write_text(json.dumps(h.conf), encoding="utf-8")
    case = {"data": str(h.data), "processed": str(processed), "csv": str(csv), "conf": h.conf,
            "aliases": ALIASES, "batch_size": BATCH}
    (root / "case.json").write_text(json.dumps(case), encoding="utf-8")
    return case


def _run(case: dict):
    data = Path(case["data"])
    return import_history("gc1", case["processed"], case["csv"],
                          instrument_folder_aliases=case["aliases"],
                          db=data / store.DB_FILENAME, data_dir=data, conf=case["conf"],
                          batch_size=case["batch_size"], by="Test Operator (127.0.0.1)")


_VOLATILE = {"received_at", "processed_at", "notes", "created_at", "finished_at"}


def _state(data: Path) -> dict:
    """The imported state without what differs between two runs by nature
    (timestamps, the run id in each revision's notes)."""
    conn = sqlite3.connect(str(data / store.DB_FILENAME))
    conn.row_factory = sqlite3.Row
    try:
        out = {}
        for t in ("samples", "sample_results", "conflicts", "export_rows", "jobs"):
            out[t] = sorted(json.dumps({k: r[k] for k in r.keys() if k not in _VOLATILE},
                                       sort_keys=True) for r in conn.execute(f"SELECT * FROM {t}"))
    finally:
        conn.close()
    files = {k: v for k, v in tree(data / "cdf").items() if not k.startswith(".incoming")}
    return {"db": out, "files": files}


@pytest.fixture
def cases(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    case_a = _build(a)
    b = tmp_path / "b"
    shutil.copytree(a, b)
    case_b = json.loads(json.dumps(case_a).replace(str(a), str(b)))
    (b / "case.json").write_text(json.dumps(case_b), encoding="utf-8")
    return case_a, case_b


def test_an_import_killed_mid_batch_resumes_to_the_same_result(cases, tmp_path):
    killed, clean = cases
    data = Path(killed["data"])
    r = subprocess.run([sys.executable, str(CHILD), str(Path(killed["data"]).parent), "4"],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == -9, (r.stdout, r.stderr)
    runs = store.import_runs.list("gc1", db=data / store.DB_FILENAME)
    assert len(runs) == 1 and runs[0]["finished_at"] is None          # left "running"
    assert json.loads(runs[0]["counts"])["progress"] == {"done": 2, "total": len(TIMES)}
    assert any(p.name.startswith("import-") for p in (data / "cdf" / ".incoming").iterdir())

    notes = []
    rt = hub.start(killed["conf"], data_dir=data, notifier=lambda lvl, m: notes.append((lvl, m)),
                   conf_fn=lambda: killed["conf"], maintenance=False)
    try:
        run = store.import_runs.list("gc1", db=data / store.DB_FILENAME)[0]
        assert run["finished_at"] is not None and "interrupted" in run["stopped"]
        text = " ".join(m for _l, m in notes)
        assert "History import for GC-1 was interrupted by a restart after 2 of 7" in text
        assert "press Start to resume, nothing was lost" in text
        assert not [p for p in (data / "cdf" / ".incoming").iterdir()
                    if p.name.startswith("import-")]
    finally:
        rt.stop()
    # a second start says nothing more
    notes.clear()
    rt = hub.start(killed["conf"], data_dir=data, notifier=lambda lvl, m: notes.append((lvl, m)),
                   conf_fn=lambda: killed["conf"], maintenance=False)
    rt.stop()
    assert not any("History import" in m for _l, m in notes)

    resumed = _run(killed)                          # the admin presses Start again
    assert resumed["counts"]["imported"] == len(TIMES) - 2
    _run(clean)                                     # never interrupted
    assert _state(data) == _state(Path(clean["data"]))
