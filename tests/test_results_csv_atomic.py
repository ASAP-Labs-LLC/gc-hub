"""Whole-file rewrites of the results CSV are atomic.

A restart under the updater ends in os._exit (and the updater's taskkill /F
ends it harder still); an exit in the middle of ``open("w")`` + rewrite used
to leave the results CSV truncated. Every rewrite now goes through
``distill._atomic_write_csv``: temp file in the same folder, fsync,
``os.replace``. The bytes written must not change.
"""
from __future__ import annotations

import ast
import csv
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import distill  # noqa: E402


def _row(lab, dt, v, note="plain"):
    r = [""] * len(distill.CSV_HEADER)
    r[0], r[1] = lab, dt
    r[2] = str(v)
    r[-1] = note
    return r


def _old_rewrite(path: Path, rows: list[dict]) -> bytes:
    """What the rewrites wrote before: open("w") + DictWriter."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=distill.CSV_HEADER)
        w.writeheader()
        w.writerows(rows)
    return path.read_bytes()


class AtomicCsvTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.dir = Path(self._t.name)
        self.csv = self.dir / "distill_results.csv"

    def tearDown(self):
        self._t.cleanup()

    def _leftovers(self):
        return sorted(p.name for p in self.dir.iterdir() if p.name != self.csv.name)

    def _seed(self):
        distill._append_csv_row(self.csv, _row("L1", "2024-01-01 10:00", 1, 'µ, "quoted"\nline'))
        distill._append_csv_row(self.csv, _row("L2", "2024-01-02 10:00", 2))
        return self.csv.read_bytes()

    def _rows(self):
        with self.csv.open("r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def _rewrite(self, v=99):
        rows = self._rows()
        rows[0] = dict(zip(distill.CSV_HEADER, _row("L1", "2024-01-01 10:00", v)))
        distill._atomic_write_csv(self.csv, distill.CSV_HEADER, rows)

    def test_rewrite_bytes_identical_to_old_rewrite(self):
        self._seed()
        new = _row("L1", "2024-01-01 10:00", 99, "ümlaut")
        rows = self._rows()
        rows[0] = dict(zip(distill.CSV_HEADER, new))
        ref = self.dir / "ref.csv"
        expected = _old_rewrite(ref, rows)
        ref.unlink()

        distill._atomic_write_csv(self.csv, distill.CSV_HEADER, rows)
        self.assertEqual(self.csv.read_bytes(), expected)
        self.assertIn(b"\r\n", expected)          # csv's line terminator, unchanged
        self.assertEqual(self._leftovers(), [])

    def test_writer_failure_leaves_the_original_intact(self):
        before = self._seed()

        class Boom(csv.DictWriter):
            def writerows(self, rows):
                self.writerow(rows[0])
                raise OSError("disk went away mid-write")

        with mock.patch.object(distill.csv, "DictWriter", Boom):
            with self.assertRaises(OSError):
                self._rewrite()
        self.assertEqual(self.csv.read_bytes(), before)
        self.assertEqual(self._leftovers(), [])

    def test_helper_bad_row_leaves_original_and_no_temp(self):
        before = self._seed()
        good = dict(zip(distill.CSV_HEADER, _row("L9", "x", 9)))
        with self.assertRaises(ValueError):
            distill._atomic_write_csv(self.csv, distill.CSV_HEADER, [good, {"bogus": 1}])
        self.assertEqual(self.csv.read_bytes(), before)
        self.assertEqual(self._leftovers(), [])

    def test_locked_file_raises_like_open_w_did(self):
        # Windows: os.replace onto a CSV Excel holds open -> PermissionError.
        # Surface it as open("w") would have (filename = the CSV), clean up.
        before = self._seed()
        err = PermissionError(13, "Access is denied")
        with mock.patch.object(distill.os, "replace", side_effect=err) as rep, \
                mock.patch.object(distill.time, "sleep") as slept:
            with self.assertRaises(PermissionError) as cm:
                self._rewrite()
        self.assertEqual(rep.call_count, distill.CSV_REPLACE_ATTEMPTS)
        self.assertEqual(distill.CSV_REPLACE_ATTEMPTS, 10)
        self.assertEqual(slept.call_count, distill.CSV_REPLACE_ATTEMPTS - 1)
        for c in slept.call_args_list:
            self.assertTrue(0.1 <= c.args[0] <= 0.2, c)
        self.assertEqual(cm.exception.filename, str(self.csv))
        self.assertEqual(self.csv.read_bytes(), before)
        self.assertEqual(self._leftovers(), [])

    def test_transient_sharing_lock_is_retried(self):
        # Antivirus / indexer / backup / an SMB client briefly holding the
        # CSV without delete-sharing: the replace succeeds on a later try.
        self._seed()
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) <= 3:
                raise PermissionError(13, "The process cannot access the file")
            return real(src, dst)
        with mock.patch.object(distill.os, "replace", side_effect=flaky), \
                mock.patch.object(distill.time, "sleep"):
            self._rewrite(v=42)
        self.assertEqual(len(calls), 4)
        self.assertEqual(float(self._rows()[0]["2887 IBP"]), 42.0)
        self.assertEqual(self._leftovers(), [])

    def test_sweep_stale_temps(self):
        self._seed()
        (self.dir / ".distill_results.csv.abc123.tmp").write_text("x")
        (self.dir / "other.tmp").write_text("x")
        self.assertEqual(distill.sweep_stale_csv_temps(self.csv), 1)
        self.assertEqual(self._leftovers(), ["other.tmp"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_file_mode_is_kept(self):
        self._seed()
        os.chmod(self.csv, 0o664)
        self._rewrite()
        self.assertEqual(stat.S_IMODE(self.csv.stat().st_mode), 0o664)


def _truncating_csv_rewrites(path: Path) -> list[str]:
    """``with <x>.open("w"…)`` / ``open(…, "w")`` blocks that build a csv
    writer — a non-atomic whole-file rewrite — outside _atomic_write_csv."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def mode_w(call: ast.Call) -> bool:
        args = list(call.args) + [k.value for k in call.keywords if k.arg == "mode"]
        return any(isinstance(a, ast.Constant) and a.value == "w" for a in args)

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name == "_atomic_write_csv":
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.With):
                continue
            opens_w = any(isinstance(i.context_expr, ast.Call) and mode_w(i.context_expr)
                          for i in node.items)
            writes_csv = any(isinstance(n, ast.Attribute) and n.attr in ("writer", "DictWriter")
                             for b in node.body for n in ast.walk(b))
            if opens_w and writes_csv:
                found.append(f"{path.name}:{node.lineno} in {fn.name}")
    return found


def _unlocked_csv_reads(path: Path, funcs) -> list[str]:
    """``<x>.open("r"…)`` of the results CSV in ``funcs`` not inside a
    ``with distill._CSV_LOCK`` block."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name not in funcs:
            continue
        locked = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.With) and any(
                    ast.unparse(i.context_expr) == "distill._CSV_LOCK" for i in node.items):
                locked |= {id(n) for n in ast.walk(node)}
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "open" and ast.unparse(node.func.value) == "csv_path"
                    and id(node) not in locked):
                bad.append(f"{fn.name}:{node.lineno}")
    return bad


class ReadersTakeTheLockTests(unittest.TestCase):
    def test_table_and_curve_read_under_the_csv_lock(self):
        # On Windows os.replace fails while a reader holds the CSV open; the
        # app's own readers must not be one of those.
        self.assertEqual(_unlocked_csv_reads(ROOT / "app.py",
                                             {"api_table", "api_distillation_curve"}), [])

    def test_upsert_is_gone(self):
        # Dead code: process_cdf dedupes inline and appends.
        self.assertFalse(hasattr(distill, "_upsert_csv_row"))


class NoTruncatingRewritesTests(unittest.TestCase):
    def test_every_results_csv_rewrite_is_atomic(self):
        hits = []
        for name in ("distill.py", "app.py"):
            hits += _truncating_csv_rewrites(ROOT / name)
        self.assertEqual(hits, [], "rewrite the results CSV via distill._atomic_write_csv")


if __name__ == "__main__":
    unittest.main()
