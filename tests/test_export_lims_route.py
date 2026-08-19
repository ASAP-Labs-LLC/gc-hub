"""Integration guard for /api/export-lims.

After the 2026-06-16 fix, Export-to-LIMS runs the selected sample(s) through the
SAME distillation tunnel as reprocess (looker.reprocess_paths/reprocess_samples →
process_cdf → append a new CSV row). This test stubs the looker so no real CDF /
calibration is needed, and verifies: the route dispatches the selection through
the tunnel, a row is appended, and errors surface to the notification tray.

Imports app (flask-gated); the watcher is stopped in setUp.
"""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

try:
    import flask  # noqa: F401
    _HAS_FLASK = True
except Exception:  # pragma: no cover
    _HAS_FLASK = False


@unittest.skipUnless(_HAS_FLASK, "flask not installed")
class ExportLimsRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module
        import distill as distill_module
        cls.app = app_module
        cls.distill = distill_module

    def setUp(self):
        self.app._watcher_stop.set()
        self.tmp = tempfile.TemporaryDirectory()
        self.results_csv = Path(self.tmp.name) / "results.csv"

        self._orig_load = self.app.settings_mod.load_settings

        def fake_load():
            conf = self._orig_load()
            conf["distill_output"] = str(self.results_csv)
            return conf

        self.app.settings_mod.load_settings = fake_load
        self.distill._SETTINGS_CACHE = None

        # Stub looker: its reprocess_* methods append a row through the real
        # distill append helper (the tunnel), returning ok/error per the API.
        outer = self
        results_csv = self.results_csv

        class _StubLooker:
            def __init__(self):
                self.calls = []

            def _append(self, key):
                row = [key, "2026-02-01 09:00:00"] + \
                      [0] * (len(outer.distill.CSV_HEADER) - 2)
                outer.distill._append_csv_row(results_csv, row)

            def reprocess_paths(self, paths, stop_event=None):
                self.calls.append(("paths", list(paths)))
                res = {}
                for p in paths:
                    if "BAD" in p:
                        res[p] = {"status": "error", "error": "calibration missing"}
                    else:
                        self._append(p)
                        res[p] = {"status": "ok", "path": p}
                return res

            def reprocess_samples(self, samples, stop_event=None):
                self.calls.append(("samples", list(samples)))
                res = {}
                for s in samples:
                    self._append(s)
                    res[s] = {"status": "ok"}
                return res

        self.stub = _StubLooker()
        self._orig_get_looker = self.app._get_looker
        self.app._get_looker = lambda: self.stub

    def tearDown(self):
        self.app.settings_mod.load_settings = self._orig_load
        self.app._get_looker = self._orig_get_looker
        self.distill._SETTINGS_CACHE = None
        self.tmp.cleanup()
        self.app._watcher_stop.clear()

    def _rows(self):
        if not self.results_csv.exists():
            return []
        with self.results_csv.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_export_dispatches_through_tunnel_and_appends(self):
        client = self.app.app.test_client()
        resp = client.post("/api/export-lims",
                           json={"paths": ["/x/SAMPLE_A.CDF"]})
        self.assertEqual(resp.status_code, 200)
        self.app._task_queue.join()  # drain the queued task
        self.assertIn(("paths", ["/x/SAMPLE_A.CDF"]), self.stub.calls)
        self.assertEqual(len(self._rows()), 1)

    def test_export_appends_again_for_same_sample(self):
        client = self.app.app.test_client()
        client.post("/api/export-lims", json={"paths": ["/x/SAMPLE_A.CDF"]})
        self.app._task_queue.join()
        client.post("/api/export-lims", json={"paths": ["/x/SAMPLE_A.CDF"]})
        self.app._task_queue.join()
        self.assertEqual(len(self._rows()), 2, "each export appends a new row")

    def test_export_error_surfaces_notification(self):
        store = self.app.notifications_mod.get_store()
        before = len(store.list_all())
        client = self.app.app.test_client()
        client.post("/api/export-lims", json={"paths": ["/x/BAD.CDF"]})
        self.app._task_queue.join()
        after = store.list_all()
        self.assertGreater(len(after), before,
                           "a failed export must add a notification")

    def test_export_requires_selection(self):
        client = self.app.app.test_client()
        resp = client.post("/api/export-lims", json={})
        self.assertNotEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
