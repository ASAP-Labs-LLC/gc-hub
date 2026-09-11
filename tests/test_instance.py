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


class ResolvePortTests(unittest.TestCase):
    def test_defaults_when_nothing_given(self) -> None:
        self.assertEqual(
            instance.resolve_port(argv=[], env={}), instance.DEFAULT_PORT
        )

    def test_env_is_used_when_no_flag(self) -> None:
        self.assertEqual(
            instance.resolve_port(argv=[], env={"GC_PORT": "5561"}), 5561
        )

    def test_blank_env_falls_back_to_default(self) -> None:
        self.assertEqual(
            instance.resolve_port(argv=[], env={"GC_PORT": "  "}),
            instance.DEFAULT_PORT,
        )

    def test_flag_space_form(self) -> None:
        self.assertEqual(
            instance.resolve_port(argv=["--port", "5562"], env={}), 5562
        )

    def test_flag_equals_form(self) -> None:
        self.assertEqual(
            instance.resolve_port(argv=["--port=5562"], env={}), 5562
        )

    def test_flag_beats_env(self) -> None:
        self.assertEqual(
            instance.resolve_port(argv=["--port", "5562"], env={"GC_PORT": "5561"}),
            5562,
        )

    def test_flag_survives_other_args(self) -> None:
        self.assertEqual(
            instance.resolve_port(argv=["--debug", "--port", "5562", "-x"], env={}),
            5562,
        )

    def test_flag_without_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            instance.resolve_port(argv=["--port"], env={})

    def test_bad_flag_value_raises_rather_than_defaulting(self) -> None:
        # A typo must be loud. Silently falling back to 5560 would start a
        # second server on the first instance's port.
        with self.assertRaises(ValueError):
            instance.resolve_port(argv=["--port", "banana"], env={})

    def test_bad_env_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            instance.resolve_port(argv=[], env={"GC_PORT": "banana"})


class ActivePortTests(unittest.TestCase):
    def test_reads_env(self) -> None:
        self.assertEqual(instance.active_port(env={"GC_PORT": "5561"}), 5561)

    def test_defaults_when_unset(self) -> None:
        self.assertEqual(instance.active_port(env={}), instance.DEFAULT_PORT)

    def test_never_raises_on_garbage(self) -> None:
        # settings.py calls this at import time; it must not explode.
        self.assertEqual(
            instance.active_port(env={"GC_PORT": "banana"}), instance.DEFAULT_PORT
        )


if __name__ == "__main__":
    unittest.main()
