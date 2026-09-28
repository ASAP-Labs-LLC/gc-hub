"""Tests for instance.py — the hub's port (resolution, validation, probe).

Stdlib-only so it runs on a bare interpreter, matching the convention in
test_qbench_import.py.
"""

from __future__ import annotations

import socket
import sys
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

    def test_port_env_wins(self) -> None:
        # The updater's PORT is its handshake and wins outright.
        self.assertEqual(
            instance.resolve_port(
                argv=["--port", "5561"],
                env={"PORT": "5580", "GC_PORT": "5562", "GC_DATA_DIR": "/srv/gcdata"},
            ),
            5580,
        )
        self.assertEqual(instance.resolve_port(argv=[], env={"PORT": "5580"}), 5580)

    def test_without_port_env_precedence_is_flag_then_gc_port(self) -> None:
        self.assertEqual(
            instance.resolve_port(argv=["--port", "5561"], env={"GC_PORT": "5562"}), 5561)
        self.assertEqual(instance.resolve_port(argv=[], env={"GC_PORT": "5562"}), 5562)


class LegacyIdentityIsGoneTests(unittest.TestCase):
    """v1's per-port settings files, pidfiles and remembered ports went with
    run.pyw (spec D14)."""

    def test_removed(self) -> None:
        for name in ("settings_path", "pidfile_name", "load_recent_ports", "remember_port",
                     "active_port", "SETTINGS_DIR", "RECENT_PORTS_PATH"):
            self.assertFalse(hasattr(instance, name), name)


class PortInUseTests(unittest.TestCase):
    def test_free_port_reports_false(self) -> None:
        # Bind to port 0 to have the OS pick a free port, then release it.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertFalse(instance.port_in_use(port))

    def test_bound_port_reports_true(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("", 0))
            held.listen(1)
            port = held.getsockname()[1]
            self.assertTrue(instance.port_in_use(port))

    def test_invalid_port_raises(self) -> None:
        with self.assertRaises(ValueError):
            instance.port_in_use("banana")


if __name__ == "__main__":
    unittest.main()
