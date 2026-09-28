"""History import (plan 2D), task 5: dry run, the summary (JSON + text) and
the dry-run-only CLI."""
from __future__ import annotations

from import_history_testlib import (  # noqa: F401  (hub: the fixture)
    ALIASES, OTHER_SHARE, db_dump, hub, row, sample, src, tree, write_csv)

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import cdf_fixtures as fx
from jobs.import_history import COUNT_KEYS, format_summary, import_history

REPO = Path(__file__).resolve().parent.parent.parent
CLI = REPO / "tools" / "import_history.py"

T = [datetime(2026, 9, 17, 14, 50, 18), datetime(2026, 9, 18, 15, 33, 41),
     datetime(2026, 9, 19, 16, 43, 5), datetime(2026, 9, 20, 17, 53, 17)]


def _iso(d):
    return d.isoformat(sep=" ")


@pytest.fixture()
def processed(hub):
    hub.gc1()
    return hub.root / "robocopy" / "processed_cdfs2"


def _history(hub, processed):
    a = sample(processed, "A1", T[0])
    sample(processed, "A1", T[0], shift=0.2, file_name="A1 copy.CDF")      # key collision
    sample(processed, "C3", T[2], method="D7096GAS.M")                       # orphan, other method
    ws = sample(processed, "WS", T[1], sample_name="   ", file_name="WS_09182026_153341.CDF")
    good = sample(hub.src, "TR", T[3])
    fx.truncated_copy(good, processed / "TR_09202026_175317.CDF")
    mis = sample(processed, "M5", datetime(2026, 9, 25, 0, 24, 50))
    rows = [row("A1", _iso(T[0]), source=src(a.name)),
            row("   ", _iso(T[1]), source=src(ws.name)),          # v1 wrote the spaces
            row("M5", "2026-09-25 02:45:00", source=src(mis.name)),
            row("TR", _iso(T[3]), source=src("TR_09202026_175317.CDF")),
            row("R9", "2026-09-26 03:12:00", source=src("R9.CDF")),
            row("X7", _iso(T[3]), source=f"{OTHER_SHARE}\\X7.CDF")]
    return write_csv(hub.root / "distill_results.csv", rows)


def _run(hub, processed, csv_path, **kw):
    kw.setdefault("conf", hub.conf)
    return import_history("gc1", processed, csv_path, instrument_folder_aliases=ALIASES,
                          db=hub.db, data_dir=hub.data, **kw)


def test_dry_run_writes_nothing_and_predicts_the_real_run(hub, processed):
    csv_path = _history(hub, processed)
    before_db, before_files = db_dump(hub), tree(hub.data)
    src_before = tree(processed)

    dry = _run(hub, processed, csv_path, dry_run=True)

    assert db_dump(hub) == before_db and tree(hub.data) == before_files
    assert tree(processed) == src_before
    assert dry["dry_run"] is True and dry["store_checked"] is True
    real = _run(hub, processed, csv_path)
    assert real["dry_run"] is False
    assert dry["counts"] == real["counts"]
    assert tree(processed) == src_before


def test_dry_run_without_a_store(hub, processed):
    csv_path = _history(hub, processed)
    dry = import_history("gc1", processed, csv_path, instrument_folder_aliases=ALIASES,
                         db=None, data_dir=None, dry_run=True)
    assert dry["store_checked"] is False
    c = dry["counts"]
    assert c["imported"] == 5          # A1, M5, WS (matched by time); orphan C3; R9
    assert c["attached"] == 3 and c["orphans"] == 1 and c["result_only"] == 1
    assert c["conflicts"] == 1         # A1 copy against A1


def test_summary_counts_every_class(hub, processed):
    csv_path = _history(hub, processed)
    s = _run(hub, processed, csv_path)
    json.dumps(s)                                   # JSON-ready
    assert set(s["counts"]) == set(COUNT_KEYS)
    c = s["counts"]
    assert c["cdf_files"] == 6 and c["cdfs_read"] == 6
    assert c["whitespace_only_names"] == 1
    assert s["examples"]["whitespace_only_names"][0]["cdf"].endswith("WS_09182026_153341.CDF")
    assert c["name_from_filename"] == 1
    assert c["unmatched_without_key"] == 0       # v1's "   " row...
    assert c["whitespace_matched"] == 1          # ...matched to WS by its time
    assert c["truncated"] == 1
    assert c["mixed_rows"] == 1
    assert c["key_collisions"] == 1 and c["conflicts"] == 1
    assert c["time_corrected"] == 1
    assert c["other_method"] == 1
    assert s["v1_misparse"]["v1_misparsed_cdfs"] == 1
    assert s["v1_misparse"]["rows_matched_via_v1_form"] == 1
    assert s["method_names"] == {"SIMDISB.M": 4, "D7096GAS.M": 1}
    assert c["csv_rows"] == 6


def test_text_summary_names_every_class(hub, processed):
    csv_path = _history(hub, processed)
    s = _run(hub, processed, csv_path, dry_run=True)
    text = format_summary(s, examples=3)
    for label in ("DRY RUN", "result-only", "orphan", "attached", "conflicts", "truncated",
                  "cross-instrument", "whitespace-only", "ambiguous", "held", "mixed",
                  "no lab ID or time", "v1 misparse", "Method names", "SIMDISB.M",
                  "D7096GAS.M", "TR_09202026_175317.CDF"):
        assert label in text, label


def _cli(*args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True,
                          env=e, timeout=120)


def test_cli_dry_run_prints_the_summary_and_writes_json(hub, processed, tmp_path):
    csv_path = _history(hub, processed)
    before_db = db_dump(hub)
    out = tmp_path / "dry.json"
    p = _cli("--dry-run", "--instrument", "gc1", "--processed-dir", str(processed),
             "--results-csv", str(csv_path), "--alias", "processed_cdfs2", "--json", str(out),
             "--db", str(hub.db))
    assert p.returncode == 0, p.stderr
    assert "DRY RUN" in p.stdout and "whitespace-only" in p.stdout
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["dry_run"] is True and data["counts"]["conflicts"] == 1
    assert db_dump(hub) == before_db


def test_cli_refuses_a_real_import(hub, processed):
    csv_path = _history(hub, processed)
    p = _cli("--instrument", "gc1", "--processed-dir", str(processed), "--results-csv",
             str(csv_path), "--alias", "processed_cdfs2")
    assert p.returncode == 2
    assert "inside the hub" in (p.stderr + p.stdout)
    p = _cli("--dry-run", "--instrument", "gc1", "--processed-dir", str(processed / "nope"),
             "--alias", "processed_cdfs2")
    assert p.returncode == 2
