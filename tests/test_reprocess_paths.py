"""Tests for path-targeted reprocessing.

Daily QC samples can share a Lab ID, so reprocessing by name reprocesses the
*newest* CDF — the wrong run. ``Looker.reprocess_paths`` must reprocess the
EXACT file it is given. process_cdf / cdf_metadata are mocked so no real
NetCDF read is needed.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import distill  # noqa: E402
import looker as looker_mod  # noqa: E402


class ReprocessPathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.watch = base / "watch"
        self.proc = base / "proc"
        self.watch.mkdir()
        self.proc.mkdir()
        self.lk = looker_mod.Looker(self.watch, self.proc)

    def tearDown(self) -> None:
        try:
            if self.lk._executor:
                self.lk._executor.shutdown(wait=False)
        except Exception:
            pass
        self._tmp.cleanup()

    def test_reprocesses_the_exact_path_not_latest_by_name(self) -> None:
        # Two QC runs sharing the name "QC" but on different days/files.
        old = self.proc / "QC_01012024.CDF"
        new = self.proc / "QC_02022024.CDF"
        old.write_bytes(b"x")
        new.write_bytes(b"x")
        called: dict = {}

        def fake_process(path, *, blank_path=None, reprocess=False):
            called["path"] = Path(path)
            called["reprocess"] = reprocess
            return Path(path)

        with mock.patch.object(distill, "cdf_metadata",
                               return_value=("QC", datetime(2024, 1, 1))), \
             mock.patch.object(distill, "process_cdf", side_effect=fake_process):
            res = self.lk.reprocess_paths([str(old)])

        # The exact file we asked for — NOT the newest ("new").
        self.assertEqual(called["path"], old)
        self.assertTrue(called["reprocess"], "must upsert, not skip")
        self.assertEqual(res[str(old)]["status"], "ok")

    def test_missing_path_is_reported_not_fatal(self) -> None:
        ghost = str(self.proc / "does_not_exist.CDF")
        res = self.lk.reprocess_paths([ghost])
        self.assertEqual(res[ghost]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
