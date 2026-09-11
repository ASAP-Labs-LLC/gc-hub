# Multi-Instance Custom Ports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let more than one copy of the D2887 diesel parser run on one machine at the same time — each on its own port, with its own settings file and pidfile — with the launcher prompting the operator for the port at startup.

**Architecture:** A new stdlib-only `instance.py` derives everything that must differ between copies (settings path, pidfile name, remembered ports) from one value: the port. `app.py` resolves the port before it imports `settings`, and publishes it as `GC_PORT` in the environment so every module and subprocess agrees. `run.pyw` asks for the port in a small tkinter dialog before spawning Flask.

**Tech Stack:** Python 3.11+, Flask, tkinter (stdlib, launcher only), pystray + watchdog (launcher only), `unittest` for tests.

**Spec:** `docs/superpowers/specs/2026-09-11-multi-instance-custom-ports-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `instance.py` (create) | Per-instance identity: port resolution/validation, settings path, pidfile name, remembered-port list, port-in-use check. Stdlib only, no import-time side effects. |
| `settings.py` (modify) | `CONFIG_PATH` derived from the active port; seed a fresh non-default-port config from the primary instance minus the per-instance paths. |
| `app.py` (modify) | Resolve the port before importing `settings`; bind to it. |
| `run.pyw` (modify) | Port dialog, per-port pidfile, dynamic tray label and browser URL. |
| `tests/test_instance.py` (create) | All `instance.py` logic. |
| `tests/test_settings.py` (modify) | Port-aware `CONFIG_PATH` and the seeding rules. |
| `tests/test_app_routes.py` (modify) | AST guard that `app.run` no longer hardcodes 5560. |
| `CLAUDE.md`, `tests/README.md` (modify) | Docs. |

Run all commands from the webapp directory:

```bash
cd "/Users/rynatical/Projects/gc-data/GC2025/GC 2026.5 2887 Advanced analysis/webapp"
```

**Note on `python`:** this repo's tests are stdlib-only by design. Use `python3` on the dev Mac (`/opt/homebrew/bin/python3`). `pytest` is installed; `flask`, `numpy`, `netCDF4` and `tkinter` are **not**, which is why every test below imports only stdlib modules plus the module under test.

---

## Task 1: `instance.py` — port validation

**Files:**
- Create: `instance.py`
- Create: `tests/test_instance.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_instance.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_instance.py -v`
Expected: FAIL — collection error, `ModuleNotFoundError: No module named 'instance'`

- [ ] **Step 3: Write minimal implementation**

Create `instance.py`:

```python
"""instance.py — per-instance identity for the GC Viewer webapp.

One machine can run several copies of this app at once (one per GC
workstation folder). Everything that must differ between those copies — the
TCP port, the settings file, the launcher's pidfile — is derived here from a
single value: the port.

Stdlib only and free of import-time side effects, so the launcher, the Flask
app and the test suite can all import it. (``app.py`` cannot be imported by
tests: ``_init_app()`` starts background threads at import time.)
"""
from __future__ import annotations

DEFAULT_PORT = 5560
PORT_MIN = 1024
PORT_MAX = 65535


def validate_port(value) -> int:
    """Coerce *value* to a usable TCP port, or raise ``ValueError``.

    The message is shown verbatim to the operator in the launcher dialog, so
    keep it plain-language.
    """
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"Port must be a whole number — got {value!r}")
    if not (PORT_MIN <= port <= PORT_MAX):
        raise ValueError(
            f"Port must be between {PORT_MIN} and {PORT_MAX} — got {port}"
        )
    return port
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_instance.py -v`
Expected: PASS — 8 passed

- [ ] **Step 5: Commit**

```bash
git add instance.py tests/test_instance.py
git commit -m "Add instance.py with port validation"
```

---

## Task 2: `instance.py` — port resolution precedence

**Files:**
- Modify: `instance.py`
- Modify: `tests/test_instance.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_instance.py`, before the `if __name__` block:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_instance.py -v -k "ResolvePort or ActivePort"`
Expected: FAIL — `AttributeError: module 'instance' has no attribute 'resolve_port'`

- [ ] **Step 3: Write minimal implementation**

Add to `instance.py` — the `import` lines go at the top with the existing `from __future__` line, the functions at the end of the file:

```python
import os
import sys
from typing import Optional, Sequence
```

```python
def resolve_port(argv: Optional[Sequence[str]] = None, env=None) -> int:
    """Decide this process's port: ``--port`` → ``GC_PORT`` → the default.

    Raises ``ValueError`` on a malformed value rather than falling back — a
    typo that silently became 5560 would put a second server on the first
    instance's port.
    """
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    env = os.environ if env is None else env

    for i, arg in enumerate(argv):
        if arg == "--port":
            if i + 1 >= len(argv):
                raise ValueError("--port requires a port number")
            return validate_port(argv[i + 1])
        if arg.startswith("--port="):
            return validate_port(arg.split("=", 1)[1])

    raw = (env.get("GC_PORT") or "").strip()
    if raw:
        return validate_port(raw)
    return DEFAULT_PORT


def active_port(env=None) -> int:
    """The port of the running process, read from ``GC_PORT``.

    Lenient by design: ``settings.py`` calls this at import time and must not
    raise, so a malformed value degrades to the default.
    """
    env = os.environ if env is None else env
    raw = (env.get("GC_PORT") or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return validate_port(raw)
    except ValueError:
        return DEFAULT_PORT
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_instance.py -v`
Expected: PASS — 21 passed

- [ ] **Step 5: Commit**

```bash
git add instance.py tests/test_instance.py
git commit -m "Add port resolution precedence to instance.py"
```

---

## Task 3: `instance.py` — settings and pidfile paths

**Files:**
- Modify: `instance.py`
- Modify: `tests/test_instance.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_instance.py`, before the `if __name__` block:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_instance.py -v -k "SettingsPath or PidfileName"`
Expected: FAIL — `AttributeError: module 'instance' has no attribute 'SETTINGS_DIR'`

- [ ] **Step 3: Write minimal implementation**

Add to `instance.py` — `from pathlib import Path` goes with the imports at the top, the constants below the existing `PORT_MAX`, the functions at the end:

```python
from pathlib import Path
```

```python
SETTINGS_DIR = Path.home()
SETTINGS_STEM = ".gc_viewer_settings"
PIDFILE_STEM = ".gc_server"
```

```python
def settings_path(port=None, env=None) -> Path:
    """Config file for *port*. The default port keeps the historic filename."""
    port = active_port(env) if port is None else validate_port(port)
    if port == DEFAULT_PORT:
        return SETTINGS_DIR / f"{SETTINGS_STEM}.json"
    return SETTINGS_DIR / f"{SETTINGS_STEM}-{port}.json"


def pidfile_name(port=None, env=None) -> str:
    """Launcher pidfile name for *port*, relative to the webapp folder.

    Per-port so a second launcher's stale-process cleanup cannot kill the
    first instance's Flask subprocess.
    """
    port = active_port(env) if port is None else validate_port(port)
    if port == DEFAULT_PORT:
        return f"{PIDFILE_STEM}.pid"
    return f"{PIDFILE_STEM}-{port}.pid"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_instance.py -v`
Expected: PASS — 29 passed

- [ ] **Step 5: Commit**

```bash
git add instance.py tests/test_instance.py
git commit -m "Derive per-port settings and pidfile paths in instance.py"
```

---

## Task 4: `instance.py` — remembered ports

**Files:**
- Modify: `instance.py`
- Modify: `tests/test_instance.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_instance.py`, before the `if __name__` block:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_instance.py -v -k RecentPorts`
Expected: FAIL — `AttributeError: module 'instance' has no attribute 'RECENT_PORTS_PATH'`

- [ ] **Step 3: Write minimal implementation**

Add to `instance.py` — `import json` with the imports, `Dict`/`List` onto the existing `typing` import, the constants below `PIDFILE_STEM`, the functions at the end:

```python
import json
from typing import Dict, List, Optional, Sequence
```

```python
RECENT_PORTS_PATH = Path.home() / ".gc_launcher_ports.json"
RECENT_LIMIT = 8
```

```python
def load_recent_ports() -> Dict[str, object]:
    """Ports the operator has used before, most recent first.

    Launcher-level and shared by every instance — it is a list of choices,
    not per-instance config. Any unreadable or malformed file degrades to
    empty rather than blocking launch.
    """
    fallback: Dict[str, object] = {"recent": [], "last": DEFAULT_PORT}
    try:
        data = json.loads(RECENT_PORTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    if not isinstance(data, dict):
        return fallback

    recent: List[int] = []
    raw_recent = data.get("recent")
    if isinstance(raw_recent, list):
        for item in raw_recent:
            try:
                port = validate_port(item)
            except ValueError:
                continue
            if port not in recent:
                recent.append(port)
    recent = recent[:RECENT_LIMIT]

    try:
        last = validate_port(data.get("last"))
    except ValueError:
        last = recent[0] if recent else DEFAULT_PORT

    return {"recent": recent, "last": last}


def remember_port(port) -> None:
    """Record *port* as the most recently used. Never raises."""
    port = validate_port(port)
    state = load_recent_ports()
    recent = [p for p in state["recent"] if p != port]
    recent.insert(0, port)
    payload = {"recent": recent[:RECENT_LIMIT], "last": port}
    try:
        RECENT_PORTS_PATH.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_instance.py -v`
Expected: PASS — 38 passed

- [ ] **Step 5: Commit**

```bash
git add instance.py tests/test_instance.py
git commit -m "Remember recently used ports for the launcher dropdown"
```

---

## Task 5: `instance.py` — port-in-use check

**Files:**
- Modify: `instance.py`
- Modify: `tests/test_instance.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_instance.py`, before the `if __name__` block. Add `import socket` to the imports at the top of the file:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_instance.py -v -k PortInUse`
Expected: FAIL — `AttributeError: module 'instance' has no attribute 'port_in_use'`

- [ ] **Step 3: Write minimal implementation**

Add `import socket` to the imports in `instance.py`, and this function at the end:

```python
def port_in_use(port, host: str = "") -> bool:
    """True if *port* cannot be bound — i.e. something is already serving it.

    The launcher spawns Flask with CREATE_NEW_CONSOLE, so without this check a
    port clash kills the server inside a console window the operator never
    sees while the tray icon still looks healthy.

    ``host=""`` means INADDR_ANY, matching ``app.run(host="0.0.0.0")``.
    SO_REUSEADDR is deliberately NOT set: on Windows it permits binding an
    address already in use, which would report a busy port as free.
    """
    port = validate_port(port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_instance.py -v`
Expected: PASS — 41 passed

- [ ] **Step 5: Commit**

```bash
git add instance.py tests/test_instance.py
git commit -m "Add port-in-use probe for the launcher dialog"
```

---

## Task 6: `settings.py` — port-aware config path

**Files:**
- Modify: `settings.py:18`
- Modify: `tests/test_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings.py`, before the `if __name__` block. Add `import importlib` and `import os` to the imports at the top of the file:

```python
class ConfigPathPerPortTests(unittest.TestCase):
    """CONFIG_PATH is computed at import time from GC_PORT."""

    def setUp(self) -> None:
        self._saved_env = os.environ.get("GC_PORT")

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop("GC_PORT", None)
        else:
            os.environ["GC_PORT"] = self._saved_env
        importlib.reload(settings_mod)

    def test_default_port_keeps_legacy_filename(self) -> None:
        os.environ.pop("GC_PORT", None)
        importlib.reload(settings_mod)
        self.assertEqual(settings_mod.CONFIG_PATH.name, ".gc_viewer_settings.json")

    def test_explicit_default_port_keeps_legacy_filename(self) -> None:
        os.environ["GC_PORT"] = "5560"
        importlib.reload(settings_mod)
        self.assertEqual(settings_mod.CONFIG_PATH.name, ".gc_viewer_settings.json")

    def test_other_port_gets_its_own_file(self) -> None:
        os.environ["GC_PORT"] = "5561"
        importlib.reload(settings_mod)
        self.assertEqual(
            settings_mod.CONFIG_PATH.name, ".gc_viewer_settings-5561.json"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_settings.py -v -k ConfigPathPerPort`
Expected: FAIL — `test_other_port_gets_its_own_file` fails, `'.gc_viewer_settings.json' != '.gc_viewer_settings-5561.json'`

- [ ] **Step 3: Write minimal implementation**

In `settings.py`, add the import below the existing `from typing import Dict` line:

```python
import instance
```

Replace line 18:

```python
CONFIG_PATH: Path = Path.home() / ".gc_viewer_settings.json"
```

with:

```python
# Derived from GC_PORT at import time so several instances can run side by
# side. Stays a module-level Path (not a call) so tests can redirect it.
CONFIG_PATH: Path = instance.settings_path()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_settings.py -v`
Expected: PASS — all previous settings tests plus 3 new ones

- [ ] **Step 5: Commit**

```bash
git add settings.py tests/test_settings.py
git commit -m "Derive settings CONFIG_PATH from the active port"
```

---

## Task 7: `settings.py` — seed a new instance

**Files:**
- Modify: `settings.py` (`load_settings`)
- Modify: `tests/test_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings.py`, before the `if __name__` block. Add `import json` to the imports at the top of the file:

```python
class SeedNewInstanceTests(unittest.TestCase):
    """A fresh non-default-port instance inherits tuning but not paths.

    Inheriting distill_output would give two instances one results CSV and
    two processes appending to it.
    """

    PER_INSTANCE = ("watch_dir", "processed_cdf_dir", "distill_output")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)

        self._saved_env = os.environ.get("GC_PORT")
        self._saved_settings_dir = settings_mod.instance.SETTINGS_DIR
        self._saved_cfg = settings_mod.CONFIG_PATH
        self._saved_comp = settings_mod.DEFAULTS["comparison_defaults_dir"]

        settings_mod.instance.SETTINGS_DIR = tmp
        settings_mod.DEFAULTS["comparison_defaults_dir"] = str(tmp / "stds")

        # The primary instance's config, as it would exist on the server.
        self._primary = settings_mod.instance.settings_path(
            settings_mod.instance.DEFAULT_PORT
        )
        self._primary.write_text(
            json.dumps(
                {
                    "watch_dir": r"C:\GC\watch-A",
                    "processed_cdf_dir": r"C:\GC\processed-A",
                    "distill_output": r"C:\GC\results-A.csv",
                    "analysis_window": "555",
                    "series_colors": "red,blue",
                }
            ),
            encoding="utf-8",
        )

        # Act as the second instance.
        os.environ["GC_PORT"] = "5561"
        settings_mod.CONFIG_PATH = settings_mod.instance.settings_path(5561)

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop("GC_PORT", None)
        else:
            os.environ["GC_PORT"] = self._saved_env
        settings_mod.instance.SETTINGS_DIR = self._saved_settings_dir
        settings_mod.CONFIG_PATH = self._saved_cfg
        settings_mod.DEFAULTS["comparison_defaults_dir"] = self._saved_comp
        self._tmp.cleanup()

    def test_inherits_non_path_settings(self) -> None:
        conf = settings_mod.load_settings()
        self.assertEqual(conf["analysis_window"], "555")
        self.assertEqual(conf["series_colors"], "red,blue")

    def test_does_not_inherit_per_instance_paths(self) -> None:
        conf = settings_mod.load_settings()
        for key in self.PER_INSTANCE:
            self.assertEqual(
                conf[key], settings_mod.DEFAULTS[key],
                f"{key} must not be inherited from the primary instance",
            )

    def test_seed_file_is_written_once(self) -> None:
        settings_mod.load_settings()
        self.assertTrue(settings_mod.CONFIG_PATH.exists())
        on_disk = json.loads(settings_mod.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["analysis_window"], "555")
        for key in self.PER_INSTANCE:
            self.assertNotIn(key, on_disk)

    def test_operator_edits_survive_reload(self) -> None:
        conf = settings_mod.load_settings()
        conf["watch_dir"] = str(Path(self._tmp.name) / "watch-B")
        settings_mod.save_settings(conf)
        reloaded = settings_mod.load_settings()
        self.assertEqual(reloaded["watch_dir"], conf["watch_dir"])

    def test_no_seed_when_primary_absent(self) -> None:
        self._primary.unlink()
        conf = settings_mod.load_settings()
        self.assertEqual(conf["analysis_window"], settings_mod.DEFAULTS["analysis_window"])
        self.assertFalse(settings_mod.CONFIG_PATH.exists())

    def test_default_port_never_seeds(self) -> None:
        # Guards the existing contract that load_settings creates no file.
        os.environ.pop("GC_PORT", None)
        settings_mod.CONFIG_PATH = Path(self._tmp.name) / "fresh.json"
        settings_mod.load_settings()
        self.assertFalse(settings_mod.CONFIG_PATH.exists())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_settings.py -v -k SeedNewInstance`
Expected: FAIL — `test_inherits_non_path_settings` fails, `'301' != '555'`

- [ ] **Step 3: Write minimal implementation**

In `settings.py`, add below the `DEFAULTS` dict:

```python
# Settings that MUST differ between concurrently running instances. A new
# instance inherits everything else from the primary (port 5560) config, but
# these fall back to DEFAULTS so the operator is forced to choose them —
# two instances sharing distill_output would both append to one results CSV.
PER_INSTANCE_KEYS = ("watch_dir", "processed_cdf_dir", "distill_output")
```

Add this function above `load_settings`:

```python
def _seed_new_instance() -> None:
    """First run on a non-default port: copy the primary instance's config,
    minus the per-instance paths. No-op in every other case.
    """
    if instance.active_port() == instance.DEFAULT_PORT:
        return
    if CONFIG_PATH.exists():
        return

    primary = instance.settings_path(instance.DEFAULT_PORT)
    if primary == CONFIG_PATH or not primary.exists():
        return

    try:
        data = json.loads(primary.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Could not seed instance settings from %s: %s", primary, exc)
        return
    if not isinstance(data, dict):
        return

    for key in PER_INSTANCE_KEYS:
        data.pop(key, None)

    try:
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        LOGGER.info("Seeded new instance settings at %s from %s", CONFIG_PATH, primary)
    except Exception as exc:
        LOGGER.warning("Could not write seeded settings to %s: %s", CONFIG_PATH, exc)
```

Then add one line as the first statement of `load_settings`, above `conf = DEFAULTS.copy()`:

```python
def load_settings() -> Dict[str, str]:
    _seed_new_instance()   # no-op unless this is a fresh non-default port
    conf = DEFAULTS.copy()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_settings.py -v`
Expected: PASS — all settings tests, including the pre-existing `test_load_returns_defaults_when_no_file`

- [ ] **Step 5: Commit**

```bash
git add settings.py tests/test_settings.py
git commit -m "Seed a new instance's settings from the primary, minus paths"
```

---

## Task 8: `app.py` — bind the resolved port

**Files:**
- Modify: `app.py` (before line 52; and the `__main__` block at line ~3716)
- Modify: `tests/test_app_routes.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app_routes.py`, before the `if __name__` block:

```python
def _app_run_call() -> ast.Call:
    """The ``app.run(...)`` call in app.py's __main__ block."""
    tree = ast.parse((WEBAPP_DIR / "app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "app"
        ):
            return node
    raise AssertionError("No app.run(...) call found in app.py")


class PortBindingTests(unittest.TestCase):
    """The port must be resolvable per instance, never hardcoded.

    AST-only: importing app.py starts the Looker and auto-restart threads.
    """

    def test_app_run_has_no_hardcoded_port(self) -> None:
        call = _app_run_call()
        port_kw = next((kw for kw in call.keywords if kw.arg == "port"), None)
        self.assertIsNotNone(port_kw, "app.run must pass an explicit port=")
        self.assertNotIsInstance(
            port_kw.value, ast.Constant,
            "app.run port= must not be a literal — it has to vary per instance",
        )

    def test_app_py_imports_instance(self) -> None:
        tree = ast.parse((WEBAPP_DIR / "app.py").read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertIn("instance", imported)

    def test_instance_is_imported_before_settings(self) -> None:
        # settings.CONFIG_PATH is computed from GC_PORT at import time, so the
        # port must be published to the environment first.
        source = (WEBAPP_DIR / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        instance_line = settings_line = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "instance" and instance_line is None:
                        instance_line = node.lineno
                    if alias.name == "settings" and settings_line is None:
                        settings_line = node.lineno
        self.assertIsNotNone(instance_line)
        self.assertIsNotNone(settings_line)
        self.assertLess(instance_line, settings_line)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_app_routes.py -v -k PortBinding`
Expected: FAIL — `test_app_run_has_no_hardcoded_port` fails (port is the literal `5560`), and both other tests fail (`instance` not imported)

- [ ] **Step 3: Write minimal implementation**

In `app.py`, insert immediately **above** line 52 (`import distill`):

```python
# ── Per-instance identity ─────────────────────────────────────────────────
# Resolve this instance's port BEFORE importing settings: settings.CONFIG_PATH
# is derived from GC_PORT at import time, and distill/looker import settings.
# Publishing it back to the environment means every module — and every
# subprocess we spawn, including the daily auto-restart — agrees on the port.
import instance

GC_PORT = instance.resolve_port()
os.environ["GC_PORT"] = str(GC_PORT)
```

Then replace the `__main__` block at the end of the file:

```python
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5560, debug=True, threaded=True,
            use_reloader=False)  # reloader kills background threads on file changes
```

with:

```python
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=GC_PORT, debug=True, threaded=True,
            use_reloader=False)  # reloader kills background threads on file changes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_app_routes.py -v`
Expected: PASS — all route tests plus 3 new ones

- [ ] **Step 5: Verify no hardcoded port remains in the Python**

Run: `grep -rn "5560" *.py`
Expected: only `instance.py`'s `DEFAULT_PORT = 5560`

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_app_routes.py
git commit -m "Bind Flask to the resolved per-instance port"
```

---

## Task 9: `run.pyw` — per-port pidfile and dynamic port

**Files:**
- Modify: `run.pyw:34-36` (constants), `run.pyw` `main()` at line ~296

No automated test: `run.pyw` imports `ctypes.windll` and `pystray` at module level, so it cannot be imported on the dev Mac. All testable logic already lives in `instance.py`.

- [ ] **Step 1: Replace the port and pidfile constants**

In `run.pyw`, replace lines 34-36:

```python
PORT  = 5560
BASE  = Path(__file__).resolve().parent   # …/webapp/
_PIDFILE = BASE / ".gc_server.pid"        # tracks the Flask subprocess PID
```

with:

```python
BASE  = Path(__file__).resolve().parent   # …/webapp/

if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import instance  # noqa: E402  (needs BASE on sys.path first)

# Both are finalised in main() once the operator has picked a port. They stay
# module-level because the tray callbacks and pidfile helpers read them by
# name at call time.
PORT: int = instance.DEFAULT_PORT
_PIDFILE: Path = BASE / instance.pidfile_name(PORT)
```

- [ ] **Step 2: Wire the port into `main()`**

Replace the first two lines of `main()`:

```python
def main() -> None:
    # Kill any Flask process left over from a previous launcher session
    _cleanup_stale_server()
```

with:

```python
def main() -> None:
    global PORT, _PIDFILE

    try:
        port = _choose_port()
    except ValueError as exc:
        _user32.MessageBoxW(None, str(exc), "GC Viewer", 0x10)  # MB_ICONERROR
        return
    if port is None:
        return                                  # operator cancelled

    PORT = port
    os.environ["GC_PORT"] = str(PORT)           # inherited by the Flask subprocess
    _PIDFILE = BASE / instance.pidfile_name(PORT)
    instance.remember_port(PORT)

    # Kill any Flask process left over from a previous session *of this port*
    _cleanup_stale_server()
```

- [ ] **Step 3: Verify the port flows to the tray and browser**

Run: `grep -n "PORT" run.pyw`
Expected: the `PORT: int` declaration, the `global`/assignment in `main()`, `webbrowser.open(f"http://localhost:{PORT}")` at the Open-in-Browser handler, and the `f"GC Viewer  :{PORT}"` tray tooltip. No other occurrences, and no literal `5560`.

- [ ] **Step 4: Confirm the launcher still parses**

Run: `python3 -c "import ast; ast.parse(open('run.pyw').read()); print('run.pyw parses')"`
Expected: `run.pyw parses`

(A real run needs Windows — `ctypes.windll` and `pystray`.)

- [ ] **Step 5: Commit**

```bash
git add run.pyw
git commit -m "Give each launcher instance its own port and pidfile"
```

---

## Task 10: `run.pyw` — the port dialog

**Files:**
- Modify: `run.pyw` (new functions above `main()`)

- [ ] **Step 1: Add the dialog and the skip check**

In `run.pyw`, insert above `# ── Entry point ─────` :

```python
# ── Port selection ─────────────────────────────────────────────────────────

def _port_already_decided() -> int | None:
    """The port when it was chosen for us — an auto-start or scheduled launch.

    Returns None when the operator should be asked. Raises ValueError on a
    malformed explicit value, which main() surfaces as a message box.
    """
    argv = sys.argv[1:]
    if any(a == "--port" or a.startswith("--port=") for a in argv):
        return instance.resolve_port(argv=argv)
    raw = (os.environ.get("GC_PORT") or "").strip()
    if raw:
        return instance.validate_port(raw)
    return None


def _prompt_for_port() -> int | None:
    """Modal port picker. Returns the chosen port, or None if cancelled.

    tkinter is imported here, not at module scope, so instance.py and the test
    suite stay importable on machines without it.
    """
    import tkinter as tk
    from tkinter import ttk

    state = instance.load_recent_ports()
    choices = [str(p) for p in state["recent"]] or [str(instance.DEFAULT_PORT)]
    chosen: list[int] = []

    root = tk.Tk()
    root.title("GC Viewer — Start Instance")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=16)
    frame.grid(sticky="nsew")
    frame.columnconfigure(0, weight=1)
    frame.columnconfigure(1, weight=1)

    ttk.Label(frame, text="Port for this instance:").grid(
        row=0, column=0, columnspan=2, sticky="w"
    )

    var = tk.StringVar(value=str(state["last"]))
    combo = ttk.Combobox(frame, textvariable=var, values=choices, width=14)
    combo.grid(row=1, column=0, columnspan=2, sticky="we", pady=(6, 2))
    combo.focus_set()
    combo.select_range(0, tk.END)

    error = ttk.Label(frame, text="", foreground="#b00020", wraplength=260)
    error.grid(row=2, column=0, columnspan=2, sticky="w")

    def start(*_args) -> None:
        try:
            port = instance.validate_port(var.get())
        except ValueError as exc:
            error.config(text=str(exc))
            return
        if instance.port_in_use(port):
            error.config(text=f"Port {port} is already in use — pick another.")
            return
        chosen.append(port)
        root.destroy()

    def cancel(*_args) -> None:
        root.destroy()

    ttk.Button(frame, text="Start", command=start).grid(
        row=3, column=0, sticky="we", pady=(12, 0)
    )
    ttk.Button(frame, text="Cancel", command=cancel).grid(
        row=3, column=1, sticky="we", pady=(12, 0), padx=(6, 0)
    )

    root.bind("<Return>", start)
    root.bind("<Escape>", cancel)
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()

    return chosen[0] if chosen else None


def _choose_port() -> int | None:
    """Decide the port: an explicit one if given, otherwise ask the operator."""
    decided = _port_already_decided()
    if decided is not None:
        return decided
    return _prompt_for_port()
```

- [ ] **Step 2: Confirm the launcher still parses**

Run: `python3 -c "import ast; ast.parse(open('run.pyw').read()); print('run.pyw parses')"`
Expected: `run.pyw parses`

- [ ] **Step 3: Run the whole suite**

Run: `python3 -m pytest tests/ -v`
Expected: PASS for `test_instance.py`, `test_settings.py`, `test_app_routes.py`, `test_qbench_import.py`. Tests needing `numpy`/`netCDF4`/`flask` skip on this machine — that is their documented behavior, not a regression. Confirm zero failures and zero errors.

- [ ] **Step 4: Commit**

```bash
git add run.pyw
git commit -m "Ask the operator which port to start on"
```

---

## Task 11: Documentation

**Files:**
- Modify: `CLAUDE.md:21-23` (run section), `CLAUDE.md:13` (canonical-folder line)
- Modify: `tests/README.md`

- [ ] **Step 1: Fix the stale canonical-folder line**

In `CLAUDE.md`, replace:

```markdown
- The canonical folder is **`GC 2026.5 WEB/`**. A sibling `GC 2026.5 WEB - Copy/` is a backup of an older divergent build — do not edit it as if it were live.
```

with:

```markdown
- **The canonical D2887 folder is this one** — `GC 2026.5 2887 Advanced analysis/`. (An earlier copy of this file claimed `GC 2026.5 WEB/`; that tree stopped at 2026-06-16, has never been launched, and lacks `analysis_core.py`, `fuel_fit.py` and `sample_flags.py`.) `GC 2027/webapp` is the same 2026-06-16 build with D7096 *gasoline* modules bolted on — it is the gasoline fork, not newer diesel work.
```

- [ ] **Step 2: Update the run section**

In `CLAUDE.md`, replace:

```markdown
python app.py                          # dev server on http://0.0.0.0:5560
```

with:

```markdown
python app.py                          # dev server on http://0.0.0.0:5560
python app.py --port 5561              # or GC_PORT=5561 python app.py
```

- [ ] **Step 3: Document multi-instance operation**

In `CLAUDE.md`, add a section directly after the "Run / develop" block:

```markdown
## Running more than one instance

Several copies can run on one machine — one per GC workstation folder. Double-
clicking `run.pyw` opens a port dialog (default 5560, dropdown of previously
used ports) before the server starts; it refuses a port that is already bound.
Pass `--port N` or set `GC_PORT` to skip the dialog for an auto-started
instance.

`instance.py` derives everything that must differ from the port alone:

| | port 5560 | port 5561 |
|---|---|---|
| Settings | `~/.gc_viewer_settings.json` | `~/.gc_viewer_settings-5561.json` |
| Pidfile | `.gc_server.pid` | `.gc_server-5561.pid` |

Port 5560 keeps the historic filenames, so the existing install needed no
migration. Remembered ports live in `~/.gc_launcher_ports.json` (shared by all
instances — it is a list of choices, not config).

A new instance's settings are seeded from the 5560 file, **except**
`settings.PER_INSTANCE_KEYS` (`watch_dir`, `processed_cdf_dir`,
`distill_output`), which reset to defaults so the operator must choose them.
Two instances sharing `distill_output` would both append to one results CSV.

**Give each instance its own watch folder.** The Looker dedupes on
`(Lab ID, InjectionDateTime)` within one CSV, not across instances, so two
instances watching one folder both process every file.
```

- [ ] **Step 4: Add the test-suite row**

In `tests/README.md`, add to the coverage table:

```markdown
| `test_instance.py` | `instance.py` — port resolution precedence (`--port` > `GC_PORT` > 5560), validation, per-port settings/pidfile paths, the remembered-ports list, and the port-in-use probe. Stdlib-only. |
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md tests/README.md
git commit -m "Document multi-instance operation and correct the canonical folder"
```

---

## Task 12: Full verification and delivery

**Files:** none modified

- [ ] **Step 1: Run the full suite**

Run: `python3 -m pytest tests/ -v`
Expected: zero failures, zero errors. Record the exact pass/skip counts — they go in the handoff.

- [ ] **Step 2: Run the JS suite (unchanged, proves nothing regressed)**

Run: `node tests/js/run.js`
Expected: passes, or a clear "node not installed" — report whichever happened, do not claim a pass you did not see.

- [ ] **Step 3: Confirm no stray hardcoded ports**

Run: `grep -rn "5560" *.py run.pyw`
Expected: exactly one hit — `instance.py: DEFAULT_PORT = 5560`

- [ ] **Step 4: Push the branch**

```bash
git push -u origin feat/multi-instance-ports
```

- [ ] **Step 5: Copy the touched files to the share**

Only the files this change touched. Do **not** sync the whole folder — the share has no `qbench_secrets.py` and that asymmetry is pre-existing.

```bash
SRC="/Users/rynatical/Projects/gc-data/GC2025/GC 2026.5 2887 Advanced analysis/webapp"
DST="/Volumes/Labsharedrive/Ryan C/GC Data/GC2025/GC 2026.5 2887 Advanced analysis/webapp"

for f in instance.py settings.py app.py run.pyw CLAUDE.md \
         tests/test_instance.py tests/test_settings.py \
         tests/test_app_routes.py tests/README.md; do
  cp "$SRC/$f" "$DST/$f"
done
```

- [ ] **Step 6: Verify the copy**

```bash
for f in instance.py settings.py app.py run.pyw CLAUDE.md \
         tests/test_instance.py tests/test_settings.py \
         tests/test_app_routes.py tests/README.md; do
  diff -q "$SRC/$f" "$DST/$f" || echo "MISMATCH: $f"
done
```
Expected: no output.

- [ ] **Step 7: Hand off with the verification gap stated**

Report to the user: the test counts actually observed, and that **the tkinter port dialog has not been run** — it needs Windows (`ctypes.windll`, `pystray`), and this machine has no tkinter either. Ask them to launch `run.pyw` twice on `\\ASAPServer` (5560, then 5561) and confirm: the dialog appears with 5560 prefilled, the second launch's dropdown offers 5560, an in-use port is refused, both tray icons show their own port, and each instance keeps its own settings.

---

## Not in scope

- The gasoline apps (`GC 2026 GAS`, `GC 2027/webapp 2`) and the stale D2887 trees (`GC 2026.5 WEB`, `GC 2027/webapp`).
- `app.py`'s daily `_do_restart()` respawns `[sys.executable] + sys.argv`. Under `run.pyw` that is `['-c']`, which is already wrong before this change — the port survives anyway via the inherited `GC_PORT`. Pre-existing; not touched here.
- Enforcing that two instances use different watch folders. Documented in `CLAUDE.md` instead.
