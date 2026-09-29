"""diagnostics.py: the admin "Download diagnostics" bundle.

One zip an admin downloads from /admin/hub and hands to Claude to debug the
production hub. ``build_bundle(options, data_dir=, db=, out_path=)`` writes it
and returns its manifest; ``estimate(data_dir=, db=)`` sizes each option for
the admin page. The routes are in ``hub_admin.py``.

Options (``OPTION_KEYS``; defaults in ``OPTIONS``):

* ``summary``: ``summary.txt`` (the manifest's facts, readable). The manifest
  itself is always written: version, Python, platform, disk free, uptime, the
  hub runtime (worker/exporter/maintenance alive, last errors), the updater
  files in the data folder (read, never modified), ``hub_control``'s status
  snapshot when that module exists, who asked, the options, and every member
  with its size and sha256.
* ``logs``: ``app.log`` and its rotations (and other ``*.log`` at the data
  folder root), newest first, at most ``LOG_CAP_BYTES`` in all.
* ``tables``: JSON extracts of the store (instruments without ``token_hash``,
  agents, jobs not done plus the last ``JOBS_DONE_LAST`` finished, import
  runs, the last ``REPORT_LOG_LAST`` report_log rows, conflicts, sample counts
  per instrument/status, settings_kv without secret keys) and notifications.
* ``settings``: ``settings.json`` with every secret-looking key's value
  replaced by ``[REDACTED]``; paths are kept.
* ``database``: a copy of ``gc.db``, scrubbed (below).
* ``exports``: per instrument: export path, size, sidecar, lock file, pending
  ledger rows and the last ``EXPORT_TAIL_LINES`` lines of the export CSV.
* ``reports``: the newest ``REPORTS_LAST`` files under ``data/reports``.
* ``problem_cdfs``: the stored CDFs of samples received in the last
  ``PROBLEM_DAYS`` days whose status is a problem (``PROBLEM_STATUSES``) or
  that carry a review note, and of conflicts' held files, at most
  ``PROBLEM_CDF_CAP_BYTES``; the rest is listed under ``skipped``.
* ``all_cdfs`` (off by default): every file under ``data/cdf``.

Secrets never go in. Excluded by name wherever they are: the QBench credential
store (``qbench_secrets``' store path, any ``qbench*.json`` and its corrupt
copies), ``qbenchlogin.txt``, ``admin-setup-code.txt``. Nothing outside the
data folder is read into the zip (symlinks are resolved first) except each
instrument's export CSV tail and sidecar, which are the hub's own output. The
QBench store is only opened to learn the values to redact. Every secret value
the hub knows (``known_secrets``: the admin password hash, token hashes, the
setup code, the QBench pair, the Selenium login, secret-looking settings
values) is replaced by ``[REDACTED]`` in every text member; a binary member
(a CDF) that contains one is left out. The database copy is taken with
``VACUUM INTO`` from a read-only connection (a WAL reader: it never takes the
write lock, so the hub keeps processing and serving), then in the copy the
secret ``settings_kv`` rows are deleted, ``instruments.token_hash`` is nulled,
every known secret is replaced in every text column, and the file is vacuumed
with ``secure_delete`` so no freed page keeps the old bytes. Before the zip
is finished every member is checked once more; a secret found there fails the
build (``SecretLeak``) rather than ship.

One build at a time per process: ``exclusive()`` raises ``Busy`` (the route's
409). Stdlib only.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import store

PROCESS_STARTED = time.time()      # imported at app start-up (through hub_admin)

OPTIONS: dict = {
    "summary": True, "logs": True, "tables": True, "settings": True, "database": True,
    "exports": True, "reports": True, "problem_cdfs": True, "all_cdfs": False,
}
OPTION_KEYS = tuple(OPTIONS)
LABELS = {
    "summary": "Summary (version, runtime state, updater files)",
    "logs": "Logs (app.log and rotations)",
    "tables": "Store tables (jobs, agents, instruments, conflicts, ...)",
    "settings": "settings.json (secrets redacted)",
    "database": "Database copy (secrets scrubbed)",
    "exports": "Export files health",
    "reports": "Parity reports",
    "problem_cdfs": "CDFs of problem samples (last 30 days)",
    "all_cdfs": "All raw CDFs",
}

LOG_CAP_BYTES = 50 * 1024 * 1024
PROBLEM_CDF_CAP_BYTES = 200 * 1024 * 1024
PROBLEM_DAYS = 30
PROBLEM_STATUSES = ("error", "awaiting_calibration", "pending_corrections", "review_method",
                    "other_method")
REPORTS_LAST = 20
REPORT_LOG_LAST = 1000
JOBS_DONE_LAST = 500
EXPORT_TAIL_LINES = 20
EXPORT_TAIL_BYTES = 256 * 1024
UPDATER_FILE_MAX = 64 * 1024
TEXT_IN_MEMORY_MAX = 64 * 1024 * 1024
MIN_SECRET_LEN = 6
REDACTED = "[REDACTED]"
TMP_DIRNAME = "diagnostics-tmp"

UPDATER_FILES = ("staged.json", "held-tags.json")
SECRET_KEY_RE = re.compile(
    r"pass(word|wd)?|secret|token|credential|api[_-]?key|private|cookie|session|setup[_-]?code",
    re.IGNORECASE)
EXCLUDED_NAMES = ("admin-setup-code.txt", "qbenchlogin.txt")
EXCLUDED_NAME_RE = re.compile(r"^(\.qbench-.*|qbench.*\.json(\.corrupt-.*)?)$", re.IGNORECASE)

Progress = Optional[Callable[[dict], Any]]


class Busy(RuntimeError):
    """Another diagnostics build is running."""


class SecretLeak(RuntimeError):
    """The final check found a secret in the zip; nothing is shipped."""


_BUILD_LOCK = threading.Lock()


@contextlib.contextmanager
def exclusive():
    """Hold the one-build-at-a-time lock, or raise ``Busy`` at once."""
    if not _BUILD_LOCK.acquire(blocking=False):
        raise Busy("a diagnostics bundle is already being built")
    try:
        yield
    finally:
        _BUILD_LOCK.release()


def normalize_options(raw: Any) -> dict:
    """``OPTIONS`` with ``raw``'s booleans applied. ``ValueError`` for a
    non-object, an unknown key or a non-boolean value."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("options must be an object")
    unknown = set(raw) - set(OPTION_KEYS)
    if unknown:
        raise ValueError(f"unknown options: {sorted(unknown)}")
    out = dict(OPTIONS)
    for k, v in raw.items():
        if not isinstance(v, bool):
            raise ValueError(f"option {k} must be true or false")
        out[k] = v
    return out


# ── secrets ─────────────────────────────────────────────────────────────────

def is_secret_key(key: Any) -> bool:
    return isinstance(key, str) and bool(SECRET_KEY_RE.search(key))


def _qbench_store_paths() -> list:
    out = []
    try:
        import qbench_secrets
        out.append(Path(qbench_secrets._store_path()))
        out.append(Path(qbench_secrets.default_store_path()))
    except Exception:  # noqa: BLE001 - never stop a bundle over this
        pass
    return out


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _secret_values_in(value: Any, under_secret: bool = False) -> Iterable[str]:
    """String values held under a secret-looking key, at any depth."""
    if isinstance(value, dict):
        for k, v in value.items():
            if under_secret or is_secret_key(k):
                yield from _strings(v)
            else:
                yield from _secret_values_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _secret_values_in(v, under_secret)
    elif under_secret and isinstance(value, str):
        yield value


def _read_text(path: Path, limit: int = 1024 * 1024) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


def _pieces(value: str) -> Iterable[str]:
    """The value, its lines and its ``$``-separated parts (a password hash)."""
    yield value
    for line in value.splitlines():
        yield line.strip()
        for part in line.split("$"):
            yield part.strip()


def known_secrets(data_dir: Path, db: Optional[Path]) -> list:
    """Every secret value the hub keeps, longest first (for redaction)."""
    found: set = set()
    data_dir = Path(data_dir)
    for name in EXCLUDED_NAMES:
        text = _read_text(data_dir / name)
        if text:
            found.add(text)
    stores = set(_qbench_store_paths())
    for p in data_dir.iterdir() if data_dir.is_dir() else ():
        if EXCLUDED_NAME_RE.match(p.name):
            stores.add(p)
    for p in stores:
        text = _read_text(p)
        if text is None:
            continue
        found.add(text)
        try:
            found.update(_strings(json.loads(text)))
        except ValueError:
            pass
    for env in ("QBENCH_CLIENT_ID", "QBENCH_CLIENT_SECRET"):
        if os.environ.get(env):
            found.add(os.environ[env])
    settings_text = _read_text(data_dir / "settings.json", limit=16 * 1024 * 1024)
    if settings_text:
        try:
            found.update(_secret_values_in(json.loads(settings_text)))
        except ValueError:
            pass
    if db is not None and Path(db).is_file():
        conn = store.open_db(db, readonly=True)
        try:
            for key, value in conn.execute("SELECT key, value FROM settings_kv"):
                if is_secret_key(key) and value:
                    found.add(str(value))
            for (h,) in conn.execute("SELECT token_hash FROM instruments "
                                     "WHERE token_hash IS NOT NULL AND token_hash != ''"):
                found.add(str(h))
        finally:
            conn.close()
    out = set()
    for value in found:
        for piece in _pieces(str(value)):
            if len(piece) >= MIN_SECRET_LEN and piece != REDACTED:
                out.add(piece)
    return sorted(out, key=len, reverse=True)


def _encodings(secrets: list) -> list:
    """Byte forms to look for: UTF-8 and UTF-16-LE (a Windows log or registry
    export may be UTF-16)."""
    out = []
    for s in secrets:
        out.append(s.encode("utf-8"))
        out.append(s.encode("utf-16-le"))
    return out


def redact_text(text: str, secrets: list) -> str:
    for s in secrets:
        if s in text:
            text = text.replace(s, REDACTED)
    return text


def redact_settings(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if is_secret_key(k) else redact_settings(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_settings(v) for v in value]
    return value


def _file_has(path: Path, needles: list) -> bool:
    """Whether any of ``needles`` (bytes) occurs in the file, read in chunks."""
    if not needles:
        return False
    overlap = max(len(n) for n in needles)
    tail = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                return False
            buf = tail + chunk
            if any(n in buf for n in needles):
                return True
            tail = buf[-overlap:]


# ── the bundle ──────────────────────────────────────────────────────────────

def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
        timespec="seconds")


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


class _Bundle:
    def __init__(self, zf: zipfile.ZipFile, data_dir: Path, secrets: list, progress: Progress):
        self.zf = zf
        self.data_dir = Path(data_dir).resolve()
        self.secrets = secrets
        self.needles = _encodings(secrets)
        self.progress = progress
        self.files: list = []
        self.skipped: list = []
        self.names: set = set()
        self.forbidden = {p.resolve() for p in _qbench_store_paths() if p.exists()}

    def event(self, phase: str, **kw) -> None:
        if self.progress is not None:
            try:
                self.progress(dict(kw, phase=phase))
            except Exception:  # noqa: BLE001 - progress is advisory
                pass

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append({"name": name, "reason": reason})

    def excluded(self, path: Path) -> Optional[str]:
        """Why ``path`` may never go in, or None."""
        if path.name in EXCLUDED_NAMES or EXCLUDED_NAME_RE.match(path.name):
            return "excluded (secret)"
        try:
            real = path.resolve()
        except OSError:
            return "unreadable"
        if real in self.forbidden:
            return "excluded (secret)"
        if real.name in EXCLUDED_NAMES or EXCLUDED_NAME_RE.match(real.name):
            return "excluded (secret)"
        return None

    def _record(self, name: str, data: Optional[bytes] = None, path: Optional[Path] = None):
        h = hashlib.sha256()
        size = 0
        if data is not None:
            h.update(data)
            size = len(data)
        else:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
                    size += len(chunk)
        self.files.append({"name": name, "size": size, "sha256": h.hexdigest()})
        self.names.add(name)

    def add_bytes(self, name: str, data: bytes) -> None:
        if name in self.names:
            return
        self.zf.writestr(name, data)
        self._record(name, data=data)

    def add_text(self, name: str, text: str) -> None:
        self.add_bytes(name, redact_text(text, self.secrets).encode("utf-8"))

    def add_json(self, name: str, value: Any) -> None:
        self.add_text(name, json.dumps(value, indent=1, default=str, ensure_ascii=False))

    def add_file(self, name: str, path: Path, *, text: bool, outside_ok: bool = False) -> int:
        """Add a file (redacting text; leaving out a binary that holds a
        secret). Returns the bytes added (0 when skipped)."""
        if name in self.names:
            return 0
        why = self.excluded(path)
        if why is None and not outside_ok and not _within(path, self.data_dir):
            why = "outside the data folder"
        if why is None and not path.is_file():
            why = "missing"
        if why is not None:
            self.skip(name, why)
            return 0
        try:
            size = path.stat().st_size
            if text and size <= TEXT_IN_MEMORY_MAX:
                data = path.read_bytes()
                if any(n in data for n in self.needles):
                    data = redact_text(data.decode("utf-8", errors="replace"),
                                       self.secrets).encode("utf-8")
                self.add_bytes(name, data)
                return len(data)
            if _file_has(path, self.needles):
                self.skip(name, "contains a secret")
                return 0
            self.zf.write(path, name)
            self._record(name, path=path)
            return size
        except OSError as exc:
            self.skip(name, f"unreadable: {exc}")
            return 0


def _git_info(app_dir: Path) -> dict:
    head = app_dir / ".git" / "HEAD"
    if not head.is_file():
        return {}
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            name = ref[5:]
            p = app_dir / ".git" / name
            sha = p.read_text(encoding="utf-8").strip() if p.is_file() else None
            return {"ref": name, "commit": sha}
        return {"commit": ref}
    except OSError:
        return {}


def _status_snapshot() -> dict:
    """``hub_control.status_snapshot()`` when that module exists."""
    try:
        import hub_control
    except ImportError:
        return {"available": False, "note": "status snapshot unavailable"}
    try:
        snap = hub_control.status_snapshot()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "note": f"status snapshot unavailable ({exc})"}
    if isinstance(snap, dict):
        return dict(snap, available=True)
    return {"available": True, "value": snap}


def runtime_state(runtime: Any) -> dict:
    """What ``hub.running()`` knows: thread liveness and last errors."""
    if runtime is None:
        return {"running": False}

    def alive(obj):
        try:
            return bool(obj.is_alive()) if obj is not None else None
        except Exception:  # noqa: BLE001
            return None

    exp = getattr(runtime, "exporter", None)
    maint = getattr(runtime, "maintenance", None)
    refused = getattr(exp, "_refused", None) or {}
    failed = getattr(maint, "_failed_at", None)
    return {
        "running": True,
        "worker_alive": alive(getattr(runtime, "worker", None)),
        "exporter_alive": alive(exp),
        "maintenance_alive": alive(maint),
        "exporter_last_errors": {k: v for k, v in (getattr(exp, "_last_error", None) or {}).items()
                                 if v},
        "exporter_refused": {k: str(v) for k, v in dict(refused).items()},
        "maintenance_backup_failed_at": failed.isoformat() if failed else None,
    }


def _updater_files(data_dir: Path, secrets: list) -> dict:
    out = {}
    names = list(UPDATER_FILES) + sorted(p.name for p in data_dir.glob("switch-*"))
    for name in names:
        p = data_dir / name
        if not p.is_file():
            continue
        try:
            st = p.stat()
            with open(p, "rb") as fh:
                raw = fh.read(UPDATER_FILE_MAX)
        except OSError as exc:
            out[name] = {"error": str(exc)}
            continue
        text = redact_text(raw.decode("utf-8", errors="replace"), secrets)
        try:
            content = json.loads(text)
        except ValueError:
            content = text
        out[name] = {"size": st.st_size,
                     "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(
                         timespec="seconds"),
                     "content": content}
    return out


def _disk(data_dir: Path) -> dict:
    try:
        u = shutil.disk_usage(data_dir)
        return {"total": u.total, "used": u.used, "free": u.free}
    except OSError as exc:
        return {"error": str(exc)}


def _log_files(data_dir: Path) -> list:
    """``app.log``, ``app.log.1``, ... then other ``*.log*`` at the root, newest first."""
    def rot(p: Path) -> int:
        suffix = p.name[len("app.log"):].lstrip(".")
        return int(suffix) if suffix.isdigit() else (0 if not suffix else 10 ** 6)

    app_logs = sorted((p for p in data_dir.glob("app.log*") if p.is_file()), key=rot)
    others = sorted((p for p in data_dir.glob("*.log*")
                     if p.is_file() and not p.name.startswith("app.log")),
                    key=lambda p: -p.stat().st_mtime)
    return app_logs + others


# ── database snapshot ───────────────────────────────────────────────────────

def snapshot_db(db: Path, dest: Path, secrets: list) -> None:
    """A consistent copy of ``db`` at ``dest`` (``VACUUM INTO`` from a
    read-only connection: no write lock), scrubbed of secrets."""
    if dest.exists():
        dest.unlink()
    src = store.open_db(db, readonly=True)
    try:
        src.execute("VACUUM INTO ?", (str(dest),))
    finally:
        src.close()
    conn = sqlite3.connect(str(dest), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("BEGIN")
        keys = [r[0] for r in conn.execute("SELECT key FROM settings_kv")]
        for k in keys:
            if is_secret_key(k):
                conn.execute("DELETE FROM settings_kv WHERE key=?", (k,))
        conn.execute("UPDATE instruments SET token_hash=NULL")
        needles = list(secrets)

        def scrub(value):
            if isinstance(value, str):
                return redact_text(value, needles)
            return value

        def needs_scrub(*values):
            return any(isinstance(v, str) and any(s in v for s in needles) for v in values)

        conn.create_function("gc_scrub", 1, scrub, deterministic=True)
        conn.create_function("gc_needs_scrub", -1, needs_scrub, deterministic=True)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables if needles else ():
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
            if not cols:
                continue
            sets = ", ".join(f'"{c}" = gc_scrub("{c}")' for c in cols)
            args = ", ".join(f'"{c}"' for c in cols)
            conn.execute(f'UPDATE "{t}" SET {sets} WHERE gc_needs_scrub({args})')
        conn.execute("COMMIT")
        conn.execute("VACUUM")
    finally:
        conn.close()


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list:
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _has_token(db: Path) -> dict:
    """``{instrument: bool}`` from the live store (the copy's hashes are nulled)."""
    conn = store.open_db(db, readonly=True)
    try:
        return {r[0]: bool(r[1]) for r in conn.execute("SELECT id, token_hash FROM instruments")}
    finally:
        conn.close()


def _tables(conn: sqlite3.Connection, has_token: dict) -> dict:
    out = {}
    insts = _rows(conn, "SELECT * FROM instruments ORDER BY id")
    for r in insts:
        r.pop("token_hash", None)
        r["has_token"] = bool(has_token.get(r["id"]))
    out["instruments"] = insts
    out["agents"] = _rows(conn, "SELECT * FROM agents ORDER BY instrument_id")
    finished = ("done", "failed", "superseded")
    out["jobs"] = (_rows(conn, f"SELECT * FROM jobs WHERE state NOT IN {finished} ORDER BY id")
                   + _rows(conn, f"SELECT * FROM jobs WHERE state IN {finished} "
                                 "ORDER BY id DESC LIMIT ?", (JOBS_DONE_LAST,)))
    out["import_runs"] = _rows(conn, "SELECT * FROM import_runs ORDER BY id DESC")
    out["report_log"] = _rows(conn, "SELECT * FROM report_log ORDER BY id DESC LIMIT ?",
                              (REPORT_LOG_LAST,))
    out["conflicts"] = _rows(conn, "SELECT * FROM conflicts ORDER BY id DESC")
    out["sample_counts"] = _rows(conn, "SELECT instrument_id, status, backfill, count(*) AS n "
                                       "FROM samples GROUP BY instrument_id, status, backfill "
                                       "ORDER BY instrument_id, status, backfill")
    out["settings_kv"] = [r for r in _rows(conn, "SELECT key, value FROM settings_kv ORDER BY key")
                          if not is_secret_key(r["key"])]
    out["instrument_corrections"] = _rows(conn, "SELECT * FROM instrument_corrections "
                                                "ORDER BY instrument_id, cut")
    out["schema"] = [{"user_version": conn.execute("PRAGMA user_version").fetchone()[0]}]
    return out


def _problem_cdf_rows(conn: sqlite3.Connection, now: datetime) -> list:
    """``(rel_or_abs_path, why)`` of problem samples' and conflicts' CDFs, newest first."""
    since = (now - timedelta(days=PROBLEM_DAYS)).astimezone(timezone.utc).isoformat()
    marks = ",".join("?" * len(PROBLEM_STATUSES))
    rows = conn.execute(
        f"SELECT cdf_path, status, review_note, received_at FROM samples "
        f"WHERE cdf_path IS NOT NULL AND received_at >= ? "
        f"AND (status IN ({marks}) OR (review_note IS NOT NULL AND review_note != '')) "
        f"ORDER BY received_at DESC, id DESC", (since, *PROBLEM_STATUSES)).fetchall()
    out = [(r[0], f"sample {r[1]}" + (" (review note)" if r[2] else ""), r[3]) for r in rows]
    for r in conn.execute("SELECT cdf_path, received_at FROM conflicts WHERE cdf_path IS NOT NULL "
                          "AND received_at >= ? ORDER BY received_at DESC, id DESC", (since,)):
        out.append((r[0], "conflict", r[1]))
    out.sort(key=lambda t: t[2] or "", reverse=True)
    return [(p, why) for p, why, _ in out]


def _resolve_cdf(data_dir: Path, raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else data_dir / raw


def _arcname(data_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(data_dir.resolve()).as_posix()
    except ValueError:
        return "outside/" + path.name


def _export_health(conn: sqlite3.Connection, data_dir: Path, secrets: list) -> list:
    import exports
    out = []
    for inst in _rows(conn, "SELECT id, export_path FROM instruments ORDER BY id"):
        configured = (inst.get("export_path") or "").strip()
        path = Path(configured) if configured else data_dir / "results" / f"{inst['id']}_results.csv"
        pending = conn.execute("SELECT count(*) FROM export_rows WHERE instrument_id=? "
                               "AND hub_appended_at IS NULL", (inst["id"],)).fetchone()[0]
        row = {"instrument": inst["id"], "path": str(path), "configured": bool(configured),
               "exists": path.is_file(), "size": None, "mtime": None, "sidecar": None,
               "lock_present": exports.lock_path(path).exists(), "pending": pending, "tail": []}
        try:
            if row["exists"]:
                st = path.stat()
                row["size"] = st.st_size
                row["mtime"] = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(
                    timespec="seconds")
                with open(path, "rb") as fh:
                    fh.seek(max(0, st.st_size - EXPORT_TAIL_BYTES))
                    tail = fh.read().decode("utf-8", errors="replace")
                lines = tail.splitlines()
                if st.st_size > EXPORT_TAIL_BYTES:
                    lines = lines[1:]            # a partial first line
                row["tail"] = [redact_text(line, secrets) for line in lines[-EXPORT_TAIL_LINES:]]
            side = exports.sidecar_path(path)
            if side.is_file():
                text = redact_text(side.read_text(encoding="utf-8", errors="replace"), secrets)
                try:
                    row["sidecar"] = json.loads(text)
                except ValueError:
                    row["sidecar"] = {"unparsable": text[:4096]}
        except OSError as exc:
            row["error"] = str(exc)
        out.append(row)
    return out


def _summary_text(m: dict) -> str:
    lines = ["GC hub diagnostics", "==================", ""]
    for key in ("created_at", "who", "hostname", "app_version", "git", "python", "platform",
                "data_dir", "uptime_seconds", "disk", "runtime", "status_snapshot", "options"):
        lines.append(f"{key}: {json.dumps(m.get(key), default=str)}")
    lines.append("")
    lines.append("updater files:")
    for name, info in (m.get("updater") or {}).items():
        lines.append(f"  {name}: {json.dumps(info.get('content', info), default=str)}")
    if m.get("counts"):
        lines.append("")
        lines.append("samples per instrument/status:")
        for r in m["counts"]:
            lines.append(f"  {r['instrument_id']} {r['status']} backfill={r['backfill']}: {r['n']}")
    return "\n".join(lines) + "\n"


def build_bundle(options: Any, *, data_dir, db, out_path, progress: Progress = None,
                 who: Optional[str] = None, runtime: Any = None,
                 now: Optional[datetime] = None) -> dict:
    """Write the diagnostics zip to ``out_path`` and return its manifest."""
    opts = normalize_options(options)
    data_dir = Path(data_dir).resolve()
    db = Path(db)
    out_path = Path(out_path)
    now = now or datetime.now(timezone.utc)
    work = out_path.parent / (out_path.name + ".work")
    work.mkdir(parents=True, exist_ok=True)
    secrets = known_secrets(data_dir, db)
    import version
    manifest: dict = {
        "kind": "gc-hub-diagnostics", "format": 1,
        "created_at": _now_iso(now), "who": who, "hostname": socket.gethostname(),
        "app_version": version.APP_VERSION, "version_file": version.read_version(),
        "git": _git_info(version.APP_DIR),
        "python": sys.version, "platform": platform.platform(), "pid": os.getpid(),
        "data_dir": str(data_dir), "disk": _disk(data_dir),
        "uptime_seconds": round(time.time() - PROCESS_STARTED, 1),
        "runtime": runtime_state(runtime), "status_snapshot": _status_snapshot(),
        "updater": _updater_files(data_dir, secrets),
        "options": opts,
        "limits": {"log_cap_bytes": LOG_CAP_BYTES, "problem_cdf_cap_bytes": PROBLEM_CDF_CAP_BYTES,
                   "problem_days": PROBLEM_DAYS, "reports_last": REPORTS_LAST,
                   "report_log_last": REPORT_LOG_LAST, "jobs_done_last": JOBS_DONE_LAST,
                   "export_tail_lines": EXPORT_TAIL_LINES},
        "redacted_values": len(secrets),
    }
    try:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            b = _Bundle(zf, data_dir, secrets, progress)
            needs_db = any(opts[k] for k in ("tables", "database", "exports", "problem_cdfs"))
            snap = work / "gc.db"
            conn = None
            if needs_db and db.is_file():
                b.event("database")
                snapshot_db(db, snap, secrets)
                conn = sqlite3.connect(f"{snap.resolve().as_uri()}?mode=ro", uri=True)
            elif needs_db:
                b.skip("database/gc.db", "missing")
            try:
                if conn is not None:
                    manifest["counts"] = _rows(conn, "SELECT instrument_id, status, backfill, "
                                                     "count(*) AS n FROM samples GROUP BY "
                                                     "instrument_id, status, backfill ORDER BY "
                                                     "instrument_id, status, backfill")
                if opts["logs"]:
                    b.event("logs")
                    budget = LOG_CAP_BYTES
                    for p in _log_files(data_dir):
                        name = f"logs/{p.name}"
                        if budget <= 0:
                            b.skip(name, "log cap reached")
                            continue
                        why = b.excluded(p)
                        if why:
                            b.skip(name, why)
                            continue
                        try:
                            size = p.stat().st_size
                            margin = max((len(n) for n in b.needles), default=0)
                            with open(p, "rb") as fh:
                                fh.seek(max(0, size - budget - margin))
                                data = fh.read()
                        except OSError as exc:
                            b.skip(name, f"unreadable: {exc}")
                            continue
                        text = redact_text(data.decode("utf-8", errors="replace"), secrets)
                        raw = text.encode("utf-8")
                        if len(raw) > budget:          # redaction/decoding grew it
                            raw = raw[-budget:]
                        if size > budget:
                            b.skip(name, f"log cap: only the last {len(raw)} of {size} bytes")
                        b.add_bytes(name, raw)
                        budget -= len(raw)
                if opts["tables"] and conn is not None:
                    b.event("tables")
                    for tname, rows in _tables(conn, _has_token(db)).items():
                        b.add_json(f"tables/{tname}.json", rows)
                if opts["tables"]:
                    nf = data_dir / "notifications.json"
                    if nf.is_file():
                        text = _read_text(nf, limit=TEXT_IN_MEMORY_MAX) or "[]"
                        try:
                            b.add_json("tables/notifications.json", json.loads(text))
                        except ValueError:
                            b.add_text("tables/notifications.json", text)
                    else:
                        b.add_json("tables/notifications.json", [])
                if opts["settings"]:
                    b.event("settings")
                    sf = data_dir / "settings.json"
                    text = _read_text(sf, limit=TEXT_IN_MEMORY_MAX) if sf.is_file() else None
                    if text is None:
                        b.skip("settings/settings.json", "missing")
                    else:
                        try:
                            b.add_json("settings/settings.json", redact_settings(json.loads(text)))
                        except ValueError:
                            b.skip("settings/settings.json", "not valid JSON; left out")
                if opts["exports"] and conn is not None:
                    b.event("exports")
                    b.add_json("exports/health.json", _export_health(conn, data_dir, secrets))
                if opts["reports"]:
                    b.event("reports")
                    rdir = data_dir / "reports"
                    files = [p for p in rdir.rglob("*") if p.is_file()] if rdir.is_dir() else []
                    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    for p in files[:REPORTS_LAST]:
                        b.add_file(f"reports/{p.relative_to(rdir).as_posix()}", p, text=True)
                if opts["problem_cdfs"] and conn is not None:
                    b.event("problem_cdfs")
                    budget = PROBLEM_CDF_CAP_BYTES
                    seen = set()
                    for raw_path, why in _problem_cdf_rows(conn, now):
                        p = _resolve_cdf(data_dir, raw_path)
                        name = _arcname(data_dir, p)
                        if name in seen:
                            continue
                        seen.add(name)
                        try:
                            size = p.stat().st_size
                        except OSError:
                            b.skip(name, f"missing ({why})")
                            continue
                        if size > budget:
                            b.skip(name, f"problem CDF cap reached ({why}, {size} bytes)")
                            continue
                        budget -= b.add_file(name, p, text=False)
                if opts["all_cdfs"]:
                    b.event("all_cdfs")
                    cdir = data_dir / "cdf"
                    for p in sorted(cdir.rglob("*")) if cdir.is_dir() else ():
                        if p.is_file() or p.is_symlink():
                            b.add_file(_arcname(data_dir, p) if _within(p, data_dir)
                                       else f"cdf/{p.relative_to(cdir).as_posix()}", p,
                                       text=False)
                if opts["database"] and conn is not None:
                    b.event("database-add")
                    conn.close()
                    conn = None
                    if _file_has(snap, b.needles):
                        b.skip("database/gc.db", "a secret survived the scrub; left out")
                    else:
                        zf.write(snap, "database/gc.db")
                        b._record("database/gc.db", path=snap)
            finally:
                if conn is not None:
                    conn.close()
            if opts["summary"]:
                manifest["files"] = list(b.files)
                manifest["skipped"] = list(b.skipped)
                b.add_text("summary.txt", _summary_text(manifest))
            manifest["files"] = list(b.files)
            manifest["skipped"] = list(b.skipped)
            text = redact_text(json.dumps(manifest, indent=1, default=str, ensure_ascii=False),
                               secrets)
            zf.writestr("manifest.json", text.encode("utf-8"))
            manifest = json.loads(text)
        b.event("verify")
        _verify(out_path, b.needles)
    except BaseException:
        with contextlib.suppress(OSError):
            out_path.unlink()
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    b.event("done")
    return manifest


def _verify(path: Path, needles: list) -> None:
    """Read every member back; a secret anywhere fails the build."""
    if not needles:
        return
    overlap = max(len(n) for n in needles)
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if any(n.decode("utf-8", "ignore") in info.filename for n in needles[::2]):
                raise SecretLeak(f"a secret is in a member name ({info.filename!r})")
            tail = b""
            with zf.open(info) as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    buf = tail + chunk
                    if any(n in buf for n in needles):
                        raise SecretLeak(f"a secret is in {info.filename}")
                    tail = buf[-overlap:]


# ── size estimate ───────────────────────────────────────────────────────────

def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def estimate(*, data_dir, db, now: Optional[datetime] = None) -> dict:
    """``{option: {label, default, bytes, files, note}}``, uncompressed sizes."""
    data_dir = Path(data_dir)
    db = Path(db)
    now = now or datetime.now(timezone.utc)
    out = {k: {"label": LABELS[k], "default": OPTIONS[k], "bytes": 0, "files": 0, "note": ""}
           for k in OPTION_KEYS}
    out["summary"].update(bytes=16 * 1024, files=2)
    logs = [p for p in _log_files(data_dir)]
    total = sum(_size(p) for p in logs)
    out["logs"].update(bytes=min(total, LOG_CAP_BYTES), files=len(logs),
                       note=f"capped at {LOG_CAP_BYTES // (1024 * 1024)} MB" if total > LOG_CAP_BYTES
                       else "")
    out["settings"].update(bytes=_size(data_dir / "settings.json"),
                           files=int((data_dir / "settings.json").is_file()))
    rdir = data_dir / "reports"
    reports = sorted((p for p in rdir.rglob("*") if p.is_file()), key=lambda p: -p.stat().st_mtime
                     )[:REPORTS_LAST] if rdir.is_dir() else []
    out["reports"].update(bytes=sum(_size(p) for p in reports), files=len(reports))
    cdir = data_dir / "cdf"
    cdfs = [p for p in cdir.rglob("*") if p.is_file()] if cdir.is_dir() else []
    out["all_cdfs"].update(bytes=sum(_size(p) for p in cdfs), files=len(cdfs),
                           note="can be very large; off by default")
    if db.is_file():
        db_bytes = _size(db) + _size(db.parent / (db.name + "-wal"))
        out["database"].update(bytes=db_bytes, files=1)
        conn = store.open_db(db, readonly=True)
        try:
            rows = sum(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                       for t in ("instruments", "agents", "import_runs", "conflicts"))
            rows += min(conn.execute("SELECT count(*) FROM report_log").fetchone()[0],
                        REPORT_LOG_LAST)
            rows += min(conn.execute("SELECT count(*) FROM jobs").fetchone()[0],
                        JOBS_DONE_LAST + 1000)
            out["tables"].update(bytes=rows * 400 + _size(data_dir / "notifications.json"),
                                 files=11, note="approximate")
            n_inst = conn.execute("SELECT count(*) FROM instruments").fetchone()[0]
            out["exports"].update(bytes=n_inst * 4096, files=1, note="approximate")
            seen, pbytes, pfiles, over = set(), 0, 0, 0
            for raw_path, _why in _problem_cdf_rows(conn, now):
                p = _resolve_cdf(data_dir, raw_path)
                if p in seen:
                    continue
                seen.add(p)
                s = _size(p)
                if pbytes + s > PROBLEM_CDF_CAP_BYTES:
                    over += 1
                    continue
                pbytes += s
                pfiles += 1
            out["problem_cdfs"].update(bytes=pbytes, files=pfiles,
                                       note=f"{over} more over the "
                                            f"{PROBLEM_CDF_CAP_BYTES // (1024 * 1024)} MB cap"
                                       if over else "")
        finally:
            conn.close()
    return out


TMP_MAX_AGE_SECONDS = 3600


def cleanup_tmp(data_dir, max_age: float = TMP_MAX_AGE_SECONDS) -> Path:
    """The temp folder under the data folder, cleared of leftovers older than
    ``max_age`` (a build killed half-way; a bundle still being streamed to
    another admin is younger). Returns it."""
    tmp = Path(data_dir) / TMP_DIRNAME
    cutoff = time.time() - max_age
    if tmp.is_dir():
        for p in tmp.iterdir():
            try:
                if p.lstat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    p.unlink()
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp
