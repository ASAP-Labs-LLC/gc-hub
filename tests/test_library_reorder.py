"""Library reorder (/api/library/reindex-times) must not hold the results-CSV
lock while it reads every source CDF.

Re-deriving injection times opens one NetCDF file per row, which on a share
can take minutes; holding ``distill._CSV_LOCK`` for all of that froze every
CSV-reading request (the table, the distillation curve, the Looker's
appends). The worker now snapshots the rows under the lock, derives the times
with the lock released (``distill.derive_injection_times``), then re-reads
under the lock and applies them only to rows still present
(``distill.apply_injection_times``) before the atomic write.
"""
from __future__ import annotations

import ast
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import distill  # noqa: E402


def _row(lab, dt, src):
    return {"Lab ID": lab, "InjectionDateTime": dt, "Source File": src, "IBP": "1"}


class DeriveInjectionTimesTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._t = tempfile.TemporaryDirectory()
        self.dir = Path(self._t.name)
        self.a = self.dir / "a.cdf"
        self.b = self.dir / "b.cdf"
        self.bad = self.dir / "bad.cdf"
        for p in (self.a, self.b, self.bad):
            p.write_bytes(b"x")

    def tearDown(self):
        self._t.cleanup()

    def _meta(self, path):
        if path == self.bad:
            raise OSError("unreadable")
        return "s", {self.a: datetime(2026, 9, 1, 8, 0),
                     self.b: datetime(2026, 9, 1, 9, 30)}[path]

    def test_derives_by_row_identity_and_counts_unreadable(self):
        rows = [
            _row("L1", "2026-09-01 08:00:00", str(self.a)),       # unchanged
            _row("L2", "2026-01-01 00:00:00", f" {self.b} "),     # changes
            _row("L3", "x", str(self.bad)),                       # unreadable
            _row("L4", "y", str(self.dir / "missing.cdf")),       # missing
            _row("L5", "z", ""),                                  # no source
        ]
        derived, unreadable = distill.derive_injection_times(rows, read_metadata=self._meta)
        self.assertEqual(unreadable, 2)
        self.assertEqual(derived, {
            ("L1", "2026-09-01 08:00:00", str(self.a)): "2026-09-01 08:00:00",
            ("L2", "2026-01-01 00:00:00", str(self.b)): "2026-09-01 09:30:00",
        })


class ApplyInjectionTimesTests(unittest.TestCase):
    def test_applies_only_to_rows_still_present(self):
        derived = {
            ("L1", "old1", "/a.cdf"): "new1",
            ("L2", "old2", "/b.cdf"): "new2",    # row deleted meanwhile
            ("L3", "same", "/c.cdf"): "same",
        }
        current = [
            _row("L1", "old1", "/a.cdf"),
            _row("L9", "t9", "/z.cdf"),           # appended meanwhile: untouched
            _row("L3", "same", "/c.cdf"),
            _row("L1", "old1", "/other.cdf"),     # same Lab ID + time, other file
        ]
        updated = distill.apply_injection_times(current, derived)
        self.assertEqual(updated, 1)
        self.assertEqual([r["InjectionDateTime"] for r in current],
                         ["new1", "t9", "same", "old1"])
        self.assertEqual(current[0]["IBP"], "1")

    def test_row_edited_meanwhile_is_left_alone(self):
        derived = {("L1", "old1", "/a.cdf"): "new1"}
        current = [_row("L1", "changed-by-someone-else", "/a.cdf")]
        self.assertEqual(distill.apply_injection_times(current, derived), 0)
        self.assertEqual(current[0]["InjectionDateTime"], "changed-by-someone-else")


def _worker_fn():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_do_reindex_injection_times":
            return node
    raise AssertionError("_do_reindex_injection_times not found in app.py")


def _is_csv_lock_with(node):
    if not isinstance(node, ast.With):
        return False
    for item in node.items:
        expr = item.context_expr
        if isinstance(expr, ast.Attribute) and expr.attr == "_CSV_LOCK":
            return True
    return False


def _calls(node):
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


class WorkerLockScopeTests(unittest.TestCase):
    def test_cdfs_are_not_read_under_the_csv_lock(self):
        fn = _worker_fn()
        locked = [n for n in ast.walk(fn) if _is_csv_lock_with(n)]
        self.assertGreaterEqual(len(locked), 2, "expected snapshot + merge lock blocks")
        for block in locked:
            called = _calls(block)
            self.assertNotIn("cdf_metadata", called)
            self.assertNotIn("derive_injection_times", called)
        self.assertIn("derive_injection_times", _calls(fn))

    def test_merge_and_atomic_write_happen_under_the_lock(self):
        fn = _worker_fn()
        locked_calls = set()
        for block in (n for n in ast.walk(fn) if _is_csv_lock_with(n)):
            locked_calls |= _calls(block)
        self.assertIn("apply_injection_times", locked_calls)
        self.assertIn("_atomic_write_csv", locked_calls)


if __name__ == "__main__":
    unittest.main()
