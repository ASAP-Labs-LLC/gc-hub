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


class SettingsPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_dir = instance.SETTINGS_DIR
        instance.SETTINGS_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        instance.SETTINGS_DIR = self._saved_dir
        self._tmp.cleanup()

    def test_default_port_keeps_legacy_filename(self) -> None:
        # The production install must not need a settings migration.
        path = instance.settings_path(instance.DEFAULT_PORT)
        self.assertEqual(path.name, ".gc_viewer_settings.json")

    def test_other_port_gets_suffixed_filename(self) -> None:
        path = instance.settings_path(5561)
        self.assertEqual(path.name, ".gc_viewer_settings-5561.json")

    def test_reads_env_when_no_argument(self) -> None:
        path = instance.settings_path(env={"GC_PORT": "5561"})
        self.assertEqual(path.name, ".gc_viewer_settings-5561.json")

    def test_lives_in_settings_dir(self) -> None:
        path = instance.settings_path(5561)
        self.assertEqual(path.parent, Path(self._tmp.name))


class PidfileNameTests(unittest.TestCase):
    def test_default_port_keeps_legacy_name(self) -> None:
        self.assertEqual(
            instance.pidfile_name(instance.DEFAULT_PORT), ".gc_server.pid"
        )

    def test_other_port_gets_suffixed_name(self) -> None:
        self.assertEqual(instance.pidfile_name(5561), ".gc_server-5561.pid")

    def test_two_ports_never_collide(self) -> None:
        self.assertNotEqual(
            instance.pidfile_name(5560), instance.pidfile_name(5561)
        )

    def test_reads_env_when_no_argument(self) -> None:
        self.assertEqual(
            instance.pidfile_name(env={"GC_PORT": "5561"}), ".gc_server-5561.pid"
        )


class RecentPortsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = instance.RECENT_PORTS_PATH
        instance.RECENT_PORTS_PATH = Path(self._tmp.name) / "ports.json"

    def tearDown(self) -> None:
        instance.RECENT_PORTS_PATH = self._saved
        self._tmp.cleanup()

    def test_missing_file_gives_empty_defaults(self) -> None:
        state = instance.load_recent_ports()
        self.assertEqual(state["recent"], [])
        self.assertEqual(state["last"], instance.DEFAULT_PORT)

    def test_remember_then_load_roundtrip(self) -> None:
        instance.remember_port(5561)
        state = instance.load_recent_ports()
        self.assertEqual(state["recent"], [5561])
        self.assertEqual(state["last"], 5561)

    def test_most_recent_first(self) -> None:
        instance.remember_port(5560)
        instance.remember_port(5561)
        instance.remember_port(5562)
        self.assertEqual(instance.load_recent_ports()["recent"], [5562, 5561, 5560])

    def test_repeat_moves_to_front_without_duplicating(self) -> None:
        instance.remember_port(5560)
        instance.remember_port(5561)
        instance.remember_port(5560)
        self.assertEqual(instance.load_recent_ports()["recent"], [5560, 5561])

    def test_list_is_capped(self) -> None:
        for port in range(5560, 5560 + instance.RECENT_LIMIT + 3):
            instance.remember_port(port)
        self.assertEqual(
            len(instance.load_recent_ports()["recent"]), instance.RECENT_LIMIT
        )

    def test_corrupt_file_degrades_to_defaults(self) -> None:
        instance.RECENT_PORTS_PATH.write_text("{not json", encoding="utf-8")
        state = instance.load_recent_ports()
        self.assertEqual(state["recent"], [])
        self.assertEqual(state["last"], instance.DEFAULT_PORT)

    def test_garbage_entries_are_dropped(self) -> None:
        instance.RECENT_PORTS_PATH.write_text(
            '{"recent": [5561, "banana", 80, 5562], "last": 5561}', encoding="utf-8"
        )
        self.assertEqual(instance.load_recent_ports()["recent"], [5561, 5562])

    def test_last_falls_back_when_invalid(self) -> None:
        instance.RECENT_PORTS_PATH.write_text(
            '{"recent": [5561], "last": "banana"}', encoding="utf-8"
        )
        self.assertEqual(instance.load_recent_ports()["last"], 5561)

    def test_unwritable_path_does_not_raise(self) -> None:
        # A read-only home must not stop the app launching.
        instance.RECENT_PORTS_PATH = (
            Path(self._tmp.name) / "no-such-dir" / "ports.json"
        )
        instance.remember_port(5561)  # must not raise


if __name__ == "__main__":
    unittest.main()
