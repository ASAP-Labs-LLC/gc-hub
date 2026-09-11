"""Tests for instance.py — per-instance identity (port, paths, recents).

Stdlib-only so it runs on a bare interpreter, matching the convention in
test_qbench_import.py. Never touches the developer's real home directory:
every test that writes redirects instance.RECENT_PORTS_PATH / SETTINGS_DIR
into a temp dir.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import instance  # noqa: E402


class ValidatePortTests(unittest.TestCase):
    def test_accepts_int(self) -> None:
        self.assertEqual(instance.validate_port(5561), 5561)

    def test_accepts_numeric_string_with_whitespace(self) -> None:
        self.assertEqual(instance.validate_port("  5561 "), 5561)

    def test_rejects_non_numeric(self) -> None:
        with self.assertRaises(ValueError):
            instance.validate_port("abc")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            instance.validate_port("")

    def test_rejects_none(self) -> None:
        with self.assertRaises(ValueError):
            instance.validate_port(None)

    def test_rejects_privileged_port(self) -> None:
        with self.assertRaises(ValueError):
            instance.validate_port(80)

    def test_rejects_out_of_range_high(self) -> None:
        with self.assertRaises(ValueError):
            instance.validate_port(70000)

    def test_error_message_is_operator_readable(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            instance.validate_port(80)
        self.assertIn("1024", str(ctx.exception))
        self.assertIn("65535", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
