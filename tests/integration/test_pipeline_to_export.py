"""The seam between the pipeline and the exports, end to end.

A synthetic gc1 CDF goes through ``pipeline.submit`` → the production Worker
(from ``instruments.startup``) → ``export_rows`` → ``HubExporter.flush``. The
export file must be exactly what v1's ``distill._append_csv_row`` would have
written for the same values (header + one row, byte for byte) with
``Source File`` = the hub-relative ``cdf_path``, and the ledger line the agent
pulls (``export_rows.rows_after``) must be that same row.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent
if str(TESTS_DIR / "pipeline") not in sys.path:
    sys.path.insert(0, str(TESTS_DIR / "pipeline"))

from pipeline_helpers import hub  # noqa: E402,F401  (hub: the fixture)

import distill  # noqa: E402
import exports  # noqa: E402
import instruments  # noqa: E402
import store  # noqa: E402


def test_a_submitted_cdf_reaches_the_export_file_as_v1_would_write_it(hub, tmp_path):
    hub.gc1()                                       # live since 2020: not backfill
    export_file = tmp_path / "share" / "gc1_results.csv"
    export_file.parent.mkdir()
    store.instruments.upsert({"id": "gc1", "export_path": str(export_file)}, db=hub.db)

    # The production worker, its thread stopped so the job runs here.
    w = instruments.startup(hub.conf, None, db=hub.db, data_dir=hub.data,
                            conf_fn=lambda: hub.conf, poll_seconds=60)
    w.stop()
    assert not w.is_alive()

    sub = hub.submit(hub.cdf(name="40304"))
    assert sub.outcome == "created"
    assert w.run_until_idle() >= 1
    sample = hub.sample(sub.sample_id)
    assert sample["status"] == "final", sample
    assert sample["backfill"] == 0
    cdf_path = sample["cdf_path"]
    assert cdf_path and not Path(cdf_path).is_absolute()
    assert (hub.data / cdf_path).is_file()          # hub-relative: under the data folder

    result = exports.HubExporter(db=hub.db, data_dir=hub.data).flush("gc1")
    assert (result.appended, result.pending, result.error) == (1, 0, None)

    # v1's append of the same values into an empty file.
    results = json.loads(store.get_revision(sub.sample_id, db=hub.db)["results"])
    row_data = [results.get(c, "") for c in distill.CSV_HEADER[:-1]] + [cdf_path]
    v1_file = tmp_path / "v1.csv"
    distill._append_csv_row(v1_file, row_data)
    v1 = v1_file.read_bytes()
    header = exports.header_line().encode("utf-8")
    assert v1.startswith(header)
    v1_row = v1[len(header):]

    data = export_file.read_bytes()
    assert data == v1                               # header + exactly one row, byte for byte
    assert data.count(b"\r\n") == 2
    assert v1_row.endswith(b"," + cdf_path.encode("utf-8") + b"\r\n")

    # The agent results contract: the ledger serves that same line.
    rows = store.export_rows.rows_after("gc1", 0, db=hub.db)
    assert len(rows) == 1
    assert rows[0]["line"].encode("utf-8") == v1_row
    assert rows[0]["sample_id"] == sub.sample_id
    assert rows[0]["hub_appended_at"] is not None
