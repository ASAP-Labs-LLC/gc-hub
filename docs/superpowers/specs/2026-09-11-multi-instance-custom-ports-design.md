# Multi-Instance Support & Custom Ports — Design

Date: 2026-09-11
Scope: `GC 2026.5 2887 Advanced analysis/webapp`

Let more than one copy of the diesel (D2887) parser run on one machine at the
same time, each on its own port, watching its own folder and writing its own
results CSV. The operator picks the port from a dialog at launch rather than
editing source.

## Why this tree

`GC 2026.5 2887 Advanced analysis/webapp` is the current D2887 app. The other
two D2887 trees are stale: `GC 2026.5 WEB/webapp` last changed 2026-06-16 and
has never been launched (no pidfile), and `GC 2027/webapp` carries the same
2026-06-16 `app.py`/`distill.py` with D7096 *gasoline* modules added on
2026-06-18 — it is the gasoline fork, not newer diesel work. This tree is a
strict feature superset of `GC 2026.5 WEB` (`analysis_core.py`, `fuel_fit.py`,
`sample_flags.py`) and is the only one launched since June (pidfile dated
2026-09-03).

`webapp/CLAUDE.md` currently claims "The canonical folder is `GC 2026.5 WEB/`".
That line was copied in with the folder and is wrong; this change corrects it.

## What blocks multiple instances today

Three hardcoded singletons, all of which must move together — fixing only the
port produces two servers that fight over one config and one results file:

1. **Port.** `app.py:3717` `app.run(..., port=5560, ...)` and `run.pyw:34`
   `PORT = 5560` (used at `:266` for the browser-open URL and `:286` for the
   tray tooltip).
2. **Settings.** `settings.py:18` `CONFIG_PATH = Path.home() /
   ".gc_viewer_settings.json"` — one file, so both instances share
   `watch_dir`, `processed_cdf_dir` and `distill_output`.
3. **Pidfile.** `run.pyw:36` `_PIDFILE = BASE / ".gc_server.pid"`, and
   `_cleanup_stale_server()` taskkills whatever PID it finds at startup — so
   launching a second instance from the same folder kills the first.

The sibling GAS app already reads `int(os.environ.get("GC_PORT", "5560"))` in
`app.py`, but its launcher still hardcodes 5560 and nothing ever sets the
variable. This design completes that pattern rather than copying it half-done.

## 1. New module: `instance.py`

One small stdlib-only module owning per-instance identity. It exists as a
separate module because importing `app.py` has side effects (`_init_app()`
starts the Looker and auto-restart threads at import), so anything that needs
unit tests cannot live there — the same reasoning that produced
`analysis_core.py`.

```python
DEFAULT_PORT = 5560
PORT_MIN, PORT_MAX = 1024, 65535
RECENT_PORTS_PATH = Path.home() / ".gc_launcher_ports.json"
RECENT_LIMIT = 8

def resolve_port(argv=None, env=None) -> int
def validate_port(value) -> int          # raises ValueError with an operator-readable message
def port_in_use(port, host="127.0.0.1") -> bool
def settings_path(port=None) -> Path
def pidfile_name(port=None) -> str
def load_recent_ports() -> dict          # {"recent": [...], "last": int}
def remember_port(port) -> None
```

**Resolution order:** `--port N` (or `--port=N`) → `GC_PORT` env → 5560.
Non-integer, out-of-range, and empty values raise `ValueError`; `resolve_port`
lets that propagate rather than silently falling back, so a typo is visible
instead of quietly starting a second server on the first one's port.

**Path mapping** — port 5560 keeps today's exact filenames, so the existing
production install needs no migration and no settings move:

| | port 5560 | other ports (e.g. 5561) |
|---|---|---|
| Settings | `~/.gc_viewer_settings.json` | `~/.gc_viewer_settings-5561.json` |
| Pidfile | `.gc_server.pid` | `.gc_server-5561.pid` |

**Recent ports** live in `~/.gc_launcher_ports.json` — launcher-level, shared
across instances, since it is a list of choices rather than instance config.
`remember_port` moves the port to the front, de-duplicates, and caps the list
at `RECENT_LIMIT`. A corrupt or unreadable file degrades to
`{"recent": [], "last": DEFAULT_PORT}` rather than blocking launch.

## 2. Port wiring in `app.py`

`app.py` resolves the port once, early, and writes it back into the
environment before `settings` is first imported:

```python
if __name__ == "__main__":
    _port = instance.resolve_port()
    os.environ["GC_PORT"] = str(_port)
    app.run(host="0.0.0.0", port=_port, debug=True, threaded=True,
            use_reloader=False)
```

`GC_PORT` becomes the single source of truth for the running process, and any
subprocess inherits it. The literal `5560` leaves `app.py` entirely.

## 3. Settings isolation

`settings.py` computes `CONFIG_PATH` at import from `instance.settings_path()`,
which reads `GC_PORT`. It stays a module-level `Path`, not a function call, so
`tests/test_settings.py` can keep redirecting it exactly as it does today.

**Seeding a new instance.** When the settings file for a non-default port does
not exist, `load_settings()` seeds it from the port-5560 file if that exists,
then blanks three keys so they fall back to `DEFAULTS`:

- `watch_dir`
- `processed_cdf_dir`
- `distill_output`

Everything else — analysis thresholds, series colors, flag rules, comparison
standards dir, correction factors path — is inherited, so a second instance is
not a from-scratch reconfiguration. The three blanked keys are exactly the ones
that must differ: leaving `distill_output` inherited would give two instances
one results CSV and two writers appending to it. If the 5560 file is absent,
the new instance starts from `DEFAULTS` as today.

Seeding writes the new file once, at first load, so the operator's subsequent
edits persist normally.

## 4. Launcher popup (`run.pyw`)

Before spawning Flask, `run.pyw` shows a small tkinter dialog (stdlib; present
on the Windows production interpreter):

- An editable `ttk.Combobox` pre-filled with `load_recent_ports()["last"]`
  (5560 on first run), its dropdown listing the remembered ports.
- **Start** / **Cancel**; Enter starts, Escape cancels. Cancel exits without
  launching.
- On Start, validates via `instance.validate_port` and `instance.port_in_use`.
  A bad or already-bound port shows an inline message in the dialog and keeps
  it open. The in-use check matters because the launcher spawns Flask with
  `CREATE_NEW_CONSOLE` — without it, a port clash kills the server inside a
  console window the operator never sees, and the tray icon sits there looking
  healthy.
- On success, `remember_port(port)` is called and the dialog closes.

**Skip conditions.** If `GC_PORT` is set in the environment, or `--port` was
passed to `run.pyw`, the dialog is skipped and that port is used. This keeps an
auto-started or scheduled instance hands-free.

`PORT` becomes a runtime value rather than the module constant at `:34`. It
already flows to the browser-open URL (`:266`) and the tray tooltip (`:286`),
so two tray icons will label themselves `GC Viewer  :5560` and
`GC Viewer  :5561` with no further change — which is how the operator tells
them apart.

`_PIDFILE` becomes `BASE / instance.pidfile_name(PORT)`, resolved after the
port is known, so `_cleanup_stale_server()` only ever kills its own instance.

The tkinter import stays inside the dialog function, so `instance.py` and the
test suite remain importable on machines without tkinter.

## 5. Documentation

- `webapp/CLAUDE.md`: correct the stale "canonical folder is `GC 2026.5 WEB/`"
  line; update the run line (`python app.py` → note `--port`/`GC_PORT`); add a
  short "Running more than one instance" section covering the port dialog and
  the per-port settings/pidfile naming.
- `tests/README.md`: add the `test_instance.py` row.

## Testing

Written before the implementation. All new tests are stdlib-only so they run on
a bare interpreter, matching the existing `test_qbench_import.py` convention.

**`tests/test_instance.py`** (new)
- `resolve_port`: flag beats env beats default; `--port=N` and `--port N` forms;
  missing flag value; non-integer, 0, 80, 70000 each raise `ValueError`.
- `settings_path`: 5560 → `~/.gc_viewer_settings.json`; 5561 → the `-5561`
  variant; reads `GC_PORT` when no argument is given.
- `pidfile_name`: 5560 → `.gc_server.pid`; 5561 → `.gc_server-5561.pid`.
- Recent ports: round-trip; most-recent-first ordering; de-duplication; cap at
  `RECENT_LIMIT`; corrupt file degrades to the default shape.

**`tests/test_settings.py`** (extended)
- `CONFIG_PATH` honors `GC_PORT`; 5560 keeps the legacy filename.
- Seeding: a new non-default-port file inherits analysis keys from the 5560
  file and has `watch_dir` / `processed_cdf_dir` / `distill_output` at their
  `DEFAULTS` values, not the inherited ones.
- Seeding with no 5560 file present yields plain `DEFAULTS`.

**`tests/test_app_routes.py`** (extended, AST-only — no import)
- The `app.run(...)` call carries no literal `5560`.

**Not covered by automated tests:** the tkinter dialog itself. It is Windows
GUI code that cannot run headless, and `run.pyw` imports `ctypes.windll` at
module level, so it is not importable on the development Mac at all. All
dialog *logic* (validation, in-use check, recent-list update) lives in
`instance.py` and is tested there; the widget stays a thin shell. Operator
verification on the Windows box is required before this is considered done.

## Delivery

Work happens in the git clone at `/Users/rynatical/Projects/gc-data`, is
committed and pushed to `ASAP-Labs-LLC/gc-data`, then the touched files are
copied to `/Volumes/Labsharedrive/Ryan C/GC Data/GC2025/GC 2026.5 2887
Advanced analysis/webapp/`. No git is run on the share.

The share does not carry `qbench_secrets.py` (a clone-only credentials commit).
That asymmetry is pre-existing and is left alone — only files this change
touches are copied out.

## Out of scope

- The gasoline apps (`GC 2026 GAS`, `GC 2027/webapp 2`) and the stale D2887
  trees. They keep their current behavior; nothing here propagates to them.
- Consolidating the five sibling webapp copies.
- Running two instances against the *same* watch folder — the Looker dedupes on
  `(Lab ID, InjectionDateTime)` within one CSV, not across instances, so two
  instances pointed at one folder would both process every file. The blanked
  path keys steer the operator away from this; enforcing it is not attempted.
