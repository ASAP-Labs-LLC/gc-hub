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
    """What _upsert_csv_row wrote before: open("w") + DictWriter."""
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
        distill._upsert_csv_row(self.csv, _row("L1", "2024-01-01 10:00", 1, 'µ, "quoted"\nline'))
        distill._upsert_csv_row(self.csv, _row("L2", "2024-01-02 10:00", 2))
        return self.csv.read_bytes()

    def test_upsert_replace_bytes_identical_to_old_rewrite(self):
        self._seed()
        new = _row("L1", "2024-01-01 10:00", 99, "ümlaut")
        with self.csv.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        rows[0] = dict(zip(distill.CSV_HEADER, new))
        ref = self.dir / "ref.csv"
        expected = _old_rewrite(ref, rows)
        ref.unlink()

        distill._upsert_csv_row(self.csv, new)
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
                distill._upsert_csv_row(self.csv, _row("L1", "2024-01-01 10:00", 99))
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
        with mock.patch.object(distill.os, "replace", side_effect=err):
            with self.assertRaises(PermissionError) as cm:
                distill._upsert_csv_row(self.csv, _row("L1", "2024-01-01 10:00", 99))
        self.assertEqual(cm.exception.filename, str(self.csv))
        self.assertEqual(self.csv.read_bytes(), before)
        self.assertEqual(self._leftovers(), [])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_file_mode_is_kept(self):
        self._seed()
        os.chmod(self.csv, 0o664)
        distill._upsert_csv_row(self.csv, _row("L1", "2024-01-01 10:00", 99))
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


class NoTruncatingRewritesTests(unittest.TestCase):
    def test_every_results_csv_rewrite_is_atomic(self):
        hits = []
        for name in ("distill.py", "app.py", "looker.py"):
            hits += _truncating_csv_rewrites(ROOT / name)
        self.assertEqual(hits, [], "rewrite the results CSV via distill._atomic_write_csv")


if __name__ == "__main__":
    unittest.main()
