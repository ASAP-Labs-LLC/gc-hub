"""diagnostics.py: the admin "Download diagnostics" bundle.

One zip an admin downloads from /admin/hub and hands to Claude to debug the
production hub. ``build_bundle(options, data_dir=, db=, out_path=)`` writes it
and returns its manifest; ``estimate(data_dir=, db=)`` sizes each option for
the admin page (cached for ``ESTIMATE_TTL_SECONDS``) and ``check_disk`` uses the
same estimate to refuse a build the disk can't hold. The routes are in
``hub_admin.py``: the POST builds and returns a one-time download token
(``register_download``), a GET streams the file once and deletes it.

Options (``OPTION_KEYS``; defaults in ``OPTIONS``):

* ``summary``: ``summary.txt`` (the manifest's facts, readable: what was
  skipped or truncated and why, the secrets that could not be redacted, by
  count only). The manifest is always written: version, Python, platform, disk
  free, uptime, the hub runtime (worker/exporter/maintenance alive, last
  errors), the updater files in the data folder (read, never modified),
  ``hub_control``'s status snapshot when that module exists, who asked, the
  options, and every member with its size and sha256.
* ``logs``: ``app.log`` and its rotations (and other ``*.log`` at the data
  folder root), newest first, at most ``LOG_CAP_BYTES`` in all.
* ``tables``: JSON extracts of the store (instruments without ``token_hash``,
  agents, jobs not done plus the last ``JOBS_DONE_LAST`` finished, import
  runs, the last ``REPORT_LOG_LAST`` report_log rows, conflicts, sample counts
  per instrument/status, the last ``RECENT_ERRORS_LAST`` samples in error,
  settings_kv without secret keys) and notifications.
* ``settings``: ``settings.json`` with every secret-looking key's value
  replaced by ``[REDACTED]``; paths are kept.
* ``database``: a copy of ``gc.db``, scrubbed (below). It holds everything in
  the store, soft-deleted comments and the operators' (lab LAN) IP addresses
  included.
* ``exports``: per instrument: export path, size, mtime, pending ledger rows,
  lock file; the sidecar and the last ``EXPORT_TAIL_LINES`` lines only for a
  file that is provably a hub export (below).
* ``reports``: the newest ``REPORTS_LAST`` files under ``data/reports``.
* ``problem_cdfs``: the stored CDFs of samples received in the last
  ``PROBLEM_DAYS`` days whose status is a problem (``PROBLEM_STATUSES``) or
  that carry a review note, and of conflicts' held files, at most
  ``PROBLEM_CDF_CAP_BYTES``; the rest is listed under ``skipped``.
* ``calibration``: each instrument's calibration CDF and assignments, the
  phase-1 ``correction_factors_json`` file, and the comparison standards
  (names and sizes; the files too when under ``STANDARDS_MAX_BYTES``).
* ``updater``: the last ``UPDATER_LOG_LINES`` lines of the updater's log, its
  config files (secret keys and token shapes redacted) and the app root's
  small marker/state files. The app root is ``GC_APP_ROOT`` or the data
  folder's parent (``C:\\ASAPApps\\gc``), the updater folder ``GC_UPDATER_DIR``
  or the app root's sibling ``updater``; absent ones are skipped silently.
* ``environment``: installed packages (``name==version``), Python, platform,
  and whether each relevant environment variable is set (never its value).
* ``all_cdfs`` (off by default): every file under ``data/cdf``, at most
  ``ALL_CDF_CAP_BYTES`` (the summary says where it stopped).

Files outside the data folder are read only where named above (the
calibration CDF, which must be a ``.cdf``; the corrections file, a ``.json``;
an export CSV; a problem CDF stored with an absolute ``.cdf`` path; the
updater's files), each through ``bounded`` (a share that hangs is given up on
after ``SHARE_TIMEOUT_SECONDS``). Walks never follow symlinks or junctions, and
a file whose real path escapes its root is skipped.

**The export file (C1).** An instrument's ``export_path`` is only checked to
be absolute and a ``.csv``, so its content is read only when the path and its
real path both end in ``.csv``, neither is excluded, and either its
``.gchub.json`` sidecar parses and names this instrument or its first line is
``exports.header_line()``. Otherwise only its path, size and mtime go in.

**Secrets never go in.** Excluded by name wherever they are: the QBench
credential store (``qbench_secrets``' store path, any ``qbench*.json`` and its
corrupt copies), ``qbenchlogin.txt``, ``admin-setup-code.txt``. The values the
hub knows (``known_secrets``: the admin password's hash salt and digest, the
plaintext password the route was given, token hashes, the setup code, the
QBench pair from the store and the environment, the Selenium login from
``qbench_pdf_uploader.CREDENTIALS_FILE`` on the share (and the data folder),
secret-looking settings values) are replaced by ``[REDACTED]`` in every text
member, case-insensitively and in JSON-escaped form too; so are the shapes of
secrets it doesn't know (``PATTERNS``: GitHub tokens, bearer tokens, JSON
token/password fields, the setup-code log line). A value too short or too
common to redact safely (purely digits, an algorithm name) is left alone and
only counted in the summary. A member that still holds a known value (a
UTF-16 log, a CDF, a file name) is left out and noted. The database copy is
taken with ``VACUUM INTO`` from a read-only connection (a WAL reader: it never
takes the write lock, so the hub keeps processing and serving); in the copy the
secret ``settings_kv`` rows are deleted, ``instruments.token_hash`` is nulled,
every text column is redacted as above, and the file is vacuumed with
``secure_delete``. Before the zip is handed over every member is read back once
more; a known value found there fails the build (``SecretLeak``).

One build at a time per process (``exclusive()`` raises ``Busy``, the route's
409); ``busy()`` is also true while a bundle is waiting to be downloaded or
streaming, so the auto-restart waits. Stdlib only.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import secrets as _secrets
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
    "exports": True, "reports": True, "problem_cdfs": True, "calibration": True,
    "updater": True, "environment": True, "all_cdfs": False,
}
OPTION_KEYS = tuple(OPTIONS)
LABELS = {
    "summary": "Summary (version, runtime state, updater files)",
    "logs": "Logs (app.log and rotations)",
    "tables": "Store tables (jobs, agents, instruments, conflicts, recent errors, ...)",
    "settings": "settings.json (secrets redacted)",
    "database": "Database copy (secrets scrubbed)",
    "exports": "Export files health",
    "reports": "Parity reports",
    "problem_cdfs": "CDFs of problem samples (last 30 days)",
    "calibration": "Calibration CDFs, correction factors, comparison standards",
    "updater": "Updater log, config and state files",
    "environment": "Python packages and environment (names only)",
    "all_cdfs": "All raw CDFs",
}

LOG_CAP_BYTES = 50 * 1024 * 1024
PROBLEM_CDF_CAP_BYTES = 200 * 1024 * 1024
ALL_CDF_CAP_BYTES = 2 * 1024 * 1024 * 1024
STANDARDS_MAX_BYTES = 50 * 1024 * 1024
DISK_MARGIN_BYTES = 2 * 1024 * 1024 * 1024
PROBLEM_DAYS = 30
PROBLEM_STATUSES = ("error", "awaiting_calibration", "pending_corrections", "review_method",
                    "other_method")
REPORTS_LAST = 20
REPORT_LOG_LAST = 1000
JOBS_DONE_LAST = 500
RECENT_ERRORS_LAST = 200
EXPORT_TAIL_LINES = 20
EXPORT_TAIL_BYTES = 256 * 1024
UPDATER_FILE_MAX = 64 * 1024
UPDATER_LOG_LINES = 500
UPDATER_LOG_BYTES = 4 * 1024 * 1024
UPDATER_CONFIG_SUFFIXES = (".json", ".toml", ".ini", ".cfg", ".conf", ".yaml", ".yml")
CORRECTIONS_MAX_BYTES = 1024 * 1024
TEXT_IN_MEMORY_MAX = 64 * 1024 * 1024
MIN_SECRET_LEN = 6
SHARE_TIMEOUT_SECONDS = 3.0
ESTIMATE_TTL_SECONDS = 60.0
DOWNLOAD_TTL_SECONDS = 600.0
REDACTED = "[REDACTED]"
TMP_DIRNAME = "diagnostics-tmp"
ENV_NAMES = ("GC_DATA_DIR", "PORT", "GC_PORT", "GC_APP_ROOT", "GC_UPDATER_DIR",
             "QBENCH_STORE_PATH", "QBENCH_CLIENT_ID", "QBENCH_CLIENT_SECRET", "COA_DATA_DIR",
             "APPDATA", "TEMP", "HOME", "USERPROFILE")

UPDATER_FILES = ("staged.json", "held-tags.json")
SECRET_KEY_RE = re.compile(
    r"pass(word|wd)?|secret|token|credential|api[_-]?key|private|cookie|session|setup[_-]?code"
    r"|\bpat\b|_pat$|^pat_",
    re.IGNORECASE)
EXCLUDED_NAMES = ("admin-setup-code.txt", "qbenchlogin.txt")
EXCLUDED_NAME_RE = re.compile(r"^(\.qbench-.*|qbench.*\.json(\.corrupt-.*)?)$", re.IGNORECASE)
# Values that look like fragments of a secret but are not secret, and are common
# enough in data that redacting them corrupts it (I2).
NOT_SECRET = {"pbkdf2_sha256", "pbkdf2", "sha256", "sha512", "sha1", "bcrypt", "scrypt",
              "argon2", "argon2id", "true", "false", "null", "none"}
PATTERNS = (
    (re.compile(r"(Admin setup code:?\s*)[^\s(]+", re.IGNORECASE), r"\1" + REDACTED),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"(Bearer\s+)(?!\[REDACTED\])\S+", re.IGNORECASE), r"\1" + REDACTED),
    (re.compile(r'("(?:token|access_token|refresh_token|client_secret|password|secret|api_key)"'
                r'\s*:\s*")(?:[^"\\]|\\.)*(")', re.IGNORECASE), r"\1" + REDACTED + r"\2"),
)

# Lower-case substrings every PATTERNS match contains (the database scrub's
# SQL pre-filter).
PATTERN_HINTS = ("admin setup code", "ghp_", "github_pat_", "bearer", '"token"',
                 '"access_token"', '"refresh_token"', '"client_secret"', '"password"',
                 '"secret"', '"api_key"')

Progress = Optional[Callable[[dict], Any]]


class Busy(RuntimeError):
    """Another diagnostics build is running."""


class NoSpace(RuntimeError):
    """Not enough free disk for the bundle (the route's 507)."""


class SecretLeak(RuntimeError):
    """The final check found a secret in the zip; nothing is shipped."""


# ── one at a time; downloads; busy ──────────────────────────────────────────

_BUILD_LOCK = threading.Lock()
_state_lock = threading.Lock()
_streams = 0
_downloads: dict = {}


@contextlib.contextmanager
def exclusive():
    """Hold the one-build-at-a-time lock, or raise ``Busy`` at once."""
    if not _BUILD_LOCK.acquire(blocking=False):
        raise Busy("a diagnostics bundle is already being built")
    try:
        yield
    finally:
        _BUILD_LOCK.release()


@contextlib.contextmanager
def streaming():
    """Mark a bundle download in progress (``busy()``)."""
    global _streams
    with _state_lock:
        _streams += 1
    try:
        yield
    finally:
        with _state_lock:
            _streams -= 1


def _purge_downloads(now: float) -> None:
    for token, d in list(_downloads.items()):
        if d["expires"] <= now:
            _downloads.pop(token, None)
            with contextlib.suppress(OSError):
                Path(d["path"]).unlink()


def register_download(path: Path, name: str) -> str:
    """A random single-use token for ``path``, valid ``DOWNLOAD_TTL_SECONDS``."""
    token = _secrets.token_urlsafe(32)
    now = time.time()
    with _state_lock:
        _purge_downloads(now)
        _downloads[token] = {"path": str(path), "name": name,
                             "expires": now + DOWNLOAD_TTL_SECONDS}
    return token


def claim_download(token: str) -> Optional[dict]:
    """The download for ``token`` (removed: single use), or None when unknown
    or expired (an expired file is deleted)."""
    now = time.time()
    with _state_lock:
        _purge_downloads(now)
        return _downloads.pop(str(token), None)


def busy() -> bool:
    """True while a bundle is being built, waits to be downloaded, or streams."""
    with _state_lock:
        _purge_downloads(time.time())
        return _BUILD_LOCK.locked() or _streams > 0 or bool(_downloads)


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


# ── bounded file access (a share may hang) ──────────────────────────────────

def bounded(fn: Callable[[], Any], timeout: Optional[float] = None):
    """``(True, fn())``, or ``(False, None)`` when it raised or did not finish
    within ``timeout`` (default ``SHARE_TIMEOUT_SECONDS``; the thread is a
    daemon and is left behind)."""
    box: dict = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["e"] = exc

    t = threading.Thread(target=run, daemon=True, name="diagnostics-io")
    t.start()
    t.join(SHARE_TIMEOUT_SECONDS if timeout is None else timeout)
    if t.is_alive() or "e" in box:
        return False, None
    return True, box.get("v")


def _read_text(path: Path, limit: int = 1024 * 1024) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


def _is_file(path: Path) -> bool:
    ok, v = bounded(lambda: Path(path).is_file())
    return bool(ok and v)


def _stat(path: Path):
    ok, v = bounded(lambda: Path(path).stat())
    return v if ok else None


def _walk_files(root: Path) -> list:
    """Every regular file under ``root``, never following a symlink or
    junction, skipping anything whose real path escapes ``root``."""
    root = Path(root)
    if not root.is_dir():
        return []
    try:
        real_root = root.resolve()
    except OSError:
        return []
    isjunction = getattr(os.path, "isjunction", lambda p: False)
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        keep = []
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if os.path.islink(p) or isjunction(p):
                continue
            keep.append(d)
        dirnames[:] = sorted(keep)
        for f in sorted(filenames):
            p = Path(dirpath) / f
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                p.resolve().relative_to(real_root)
            except (OSError, ValueError):
                continue
            out.append(p)
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


def _selenium_login_path() -> Optional[Path]:
    try:
        import qbench_pdf_uploader
        return Path(qbench_pdf_uploader.CREDENTIALS_FILE)
    except Exception:  # noqa: BLE001
        return None


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


def _hash_parts(value: str) -> list:
    """A ``algo$iterations$salt$digest`` hash: only its salt and digest (I2)."""
    parts = value.split("$")
    if len(parts) == 4 and parts[1].isdigit():
        return [parts[2], parts[3]]
    return [value]


class Secrets:
    """The values to redact, and how to find them in text and bytes."""

    def __init__(self, raw: Iterable[str]):
        values: set = set()
        short = 0
        for v in raw:
            v = str(v or "").strip()
            if not v or v == REDACTED:
                continue
            if len(v) < MIN_SECRET_LEN or v.isdigit() or v.lower() in NOT_SECRET:
                short += 1
                continue
            values.add(v)
        forms = set(values)
        for v in values:
            for esc in (json.dumps(v)[1:-1], json.dumps(v, ensure_ascii=False)[1:-1]):
                forms.add(esc)
        self.values = sorted(values, key=len, reverse=True)
        self.forms = sorted(forms, key=len, reverse=True)
        self.too_short = short
        self.selenium = "not found"
        alts = "|".join(re.escape(f) for f in self.forms)
        self._text = re.compile(alts, re.IGNORECASE) if alts else None
        # Searching is substring tests on lower-cased data (C speed; a big
        # IGNORECASE alternation over megabytes is far slower); the regex
        # only runs to replace, where there is a hit.
        self._lower = sorted({f.lower() for f in self.forms}, key=len, reverse=True)
        self._lower_bytes = sorted({f.lower().encode(enc) for f in self.forms
                                    for enc in ("utf-8", "utf-16-le")}, key=len, reverse=True)
        self.overlap = max((len(b) for b in self._lower_bytes), default=0)

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self):
        return iter(self.values)

    def hit(self, text: str) -> bool:
        """Whether ``redact`` would change ``text`` (searches only)."""
        if self.in_text(text):
            return True
        return any(rx.search(text) for rx, _ in PATTERNS)

    def redact(self, text: str) -> str:
        if self._text is not None and self.in_text(text):
            text = self._text.sub(REDACTED, text)
        for rx, repl in PATTERNS:
            text = rx.sub(repl, text)
        return text

    def in_text(self, text: str) -> bool:
        if not self._lower:
            return False
        low = text.lower()
        return any(f in low for f in self._lower)

    def in_bytes(self, data: bytes) -> bool:
        if not self._lower_bytes:
            return False
        low = data.lower()
        return any(n in low for n in self._lower_bytes)

    def in_file(self, path: Path) -> bool:
        if not self._lower_bytes:
            return False
        tail = b""
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                buf = tail + chunk
                if self.in_bytes(buf):
                    return True
                tail = buf[-self.overlap:]
        return False


def _lines_and_value(text: str) -> list:
    return [text] + [line.strip() for line in text.splitlines()]


def known_secrets(data_dir: Path, db: Optional[Path], extra: Iterable[str] = ()) -> Secrets:
    """Every secret value the hub keeps (see the module docstring)."""
    found: list = []
    data_dir = Path(data_dir)
    for name in EXCLUDED_NAMES:
        text = _read_text(data_dir / name)
        if text:
            found.extend(_lines_and_value(text))
    selenium = "not found"
    login = _selenium_login_path()
    if login is not None:
        ok, text = bounded(lambda: _read_text(login))
        if not ok:
            selenium = "creds file unreadable"
        elif text:
            selenium = "read"
            found.extend(_lines_and_value(text))
    stores = set(_qbench_store_paths())
    for p in data_dir.iterdir() if data_dir.is_dir() else ():
        if EXCLUDED_NAME_RE.match(p.name):
            stores.add(p)
    for p in stores:
        ok, text = bounded(lambda p=p: _read_text(p))
        if not ok or text is None:
            continue
        found.append(text)
        try:
            found.extend(_strings(json.loads(text)))
        except ValueError:
            found.extend(_lines_and_value(text))
    for env in ("QBENCH_CLIENT_ID", "QBENCH_CLIENT_SECRET"):
        if os.environ.get(env):
            found.append(os.environ[env])
    settings_text = _read_text(data_dir / "settings.json", limit=16 * 1024 * 1024)
    if settings_text:
        try:
            found.extend(_secret_values_in(json.loads(settings_text)))
        except ValueError:
            pass
    if db is not None and Path(db).is_file():
        conn = store.open_db(db, readonly=True)
        try:
            for key, value in conn.execute("SELECT key, value FROM settings_kv"):
                if is_secret_key(key) and value:
                    found.extend(_hash_parts(str(value)))
            for (h,) in conn.execute("SELECT token_hash FROM instruments "
                                     "WHERE token_hash IS NOT NULL AND token_hash != ''"):
                found.append(str(h))
        finally:
            conn.close()
    found.extend(str(e) for e in extra if e)
    out = Secrets(found)
    out.selenium = selenium
    return out


def redact_text(text: str, secrets: Secrets) -> str:
    return secrets.redact(text)


def redact_settings(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if is_secret_key(k) else redact_settings(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_settings(v) for v in value]
    return value


def _redact_config_text(text: str) -> str:
    """``key = value`` / ``key: value`` lines with a secret-looking key."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\s*[\"']?([\w.-]+)[\"']?\s*[=:]\s*)(.*)$", line)
        if m and is_secret_key(m.group(2)):
            line = m.group(1) + REDACTED
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


# ── the bundle ──────────────────────────────────────────────────────────────

def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
        timespec="seconds")


def _mtime_iso(st) -> Optional[str]:
    return (datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")
            if st is not None else None)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)) or "_"


class _Bundle:
    def __init__(self, zf: zipfile.ZipFile, data_dir: Path, secrets: Secrets, progress: Progress):
        self.zf = zf
        self.data_dir = Path(data_dir).resolve()
        self.secrets = secrets
        self.progress = progress
        self.files: list = []
        self.skipped: list = []
        self.names: set = set()
        self.forbidden = set()
        for p in _qbench_store_paths() + [_selenium_login_path()]:
            if p is None:
                continue
            ok, real = bounded(lambda p=p: p.resolve())
            if ok:
                self.forbidden.add(real)

    def event(self, phase: str, **kw) -> None:
        if self.progress is not None:
            try:
                self.progress(dict(kw, phase=phase))
            except Exception:  # noqa: BLE001 - progress is advisory
                pass

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append({"name": redact_text(name, self.secrets), "reason": reason})

    def excluded(self, path: Path) -> Optional[str]:
        """Why ``path`` may never go in, or None."""
        if path.name in EXCLUDED_NAMES or EXCLUDED_NAME_RE.match(path.name):
            return "excluded (secret)"
        ok, real = bounded(lambda: path.resolve())
        if not ok:
            return "unreadable"
        if real in self.forbidden:
            return "excluded (secret)"
        if real.name in EXCLUDED_NAMES or EXCLUDED_NAME_RE.match(real.name):
            return "excluded (secret)"
        return None

    def leaks(self, name: str, data: bytes) -> bool:
        """A known secret still in this member's name or bytes (M2)."""
        return self.secrets.in_text(name) or self.secrets.in_bytes(data)

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

    def add_bytes(self, name: str, data: bytes) -> int:
        if name in self.names:
            return 0
        if self.leaks(name, data):
            self.skip(name, "contains a secret after redaction; left out")
            return 0
        self.zf.writestr(name, data)
        self._record(name, data=data)
        return len(data)

    def add_text(self, name: str, text: str) -> int:
        return self.add_bytes(name, redact_text(text, self.secrets).encode("utf-8"))

    def add_json(self, name: str, value: Any) -> int:
        return self.add_text(name, json.dumps(value, indent=1, default=str, ensure_ascii=False))

    def add_file(self, name: str, path: Path, *, text: bool, outside_ok: bool = False) -> int:
        """Add a file (redacting text; leaving out one that still holds a
        secret). Returns the bytes added (0 when skipped)."""
        if name in self.names:
            return 0
        why = self.excluded(path)
        if why is None and not outside_ok and not _within(path, self.data_dir):
            why = "outside the data folder"
        if why is None and not _is_file(path):
            why = "missing"
        if why is not None:
            self.skip(name, why)
            return 0
        try:
            size = path.stat().st_size
            if text and size <= TEXT_IN_MEMORY_MAX:
                data = path.read_bytes()
                return self.add_text(name, data.decode("utf-8", errors="replace"))
            if self.secrets.in_text(name):
                self.skip(name, "contains a secret after redaction; left out")
                return 0
            if self.secrets.in_file(path):
                self.skip(name, "contains a secret; left out")
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


def _small_file_info(p: Path, secrets: Secrets) -> dict:
    try:
        st = p.stat()
        with open(p, "rb") as fh:
            raw = fh.read(UPDATER_FILE_MAX)
    except OSError as exc:
        return {"error": str(exc)}
    text = redact_text(raw.decode("utf-8", errors="replace"), secrets)
    try:
        content = redact_settings(json.loads(text))
    except ValueError:
        content = text
    return {"size": st.st_size, "mtime": _mtime_iso(st), "content": content}


def _updater_files(data_dir: Path, secrets: Secrets) -> dict:
    out = {}
    names = list(UPDATER_FILES) + sorted(p.name for p in data_dir.glob("switch-*"))
    for name in names:
        p = data_dir / name
        if p.is_file() and not p.is_symlink():
            out[name] = _small_file_info(p, secrets)
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

    def ok(p: Path) -> bool:
        return p.is_file() and not p.is_symlink()

    app_logs = sorted((p for p in data_dir.glob("app.log*") if ok(p)), key=rot)
    others = sorted((p for p in data_dir.glob("*.log*")
                     if ok(p) and not p.name.startswith("app.log")),
                    key=lambda p: -p.stat().st_mtime)
    return app_logs + others


def app_root(data_dir: Path) -> Path:
    raw = (os.environ.get("GC_APP_ROOT") or "").strip()
    return Path(raw) if raw else Path(data_dir).resolve().parent


def updater_dir(data_dir: Path) -> Path:
    raw = (os.environ.get("GC_UPDATER_DIR") or "").strip()
    return Path(raw) if raw else app_root(data_dir).parent / "updater"


# ── database snapshot ───────────────────────────────────────────────────────

def snapshot_db(db: Path, dest: Path, secrets: Secrets) -> None:
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

        def scrub(value):
            return redact_text(value, secrets) if isinstance(value, str) else value

        conn.create_function("gc_scrub", 1, scrub, deterministic=True)
        # Find candidate cells in SQL (C speed, no Python per row, the GIL
        # released), and only run the Python redaction on those.
        needles = sorted({f.lower() for f in secrets.forms} | set(PATTERN_HINTS))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
            for c in cols:
                cond = " OR ".join([f'instr(lower("{c}"), ?) > 0'] * len(needles)
                                   + [f'instr("{c}", ?) > 0'] * len(secrets.forms))
                if not cond:
                    continue
                conn.execute(f'UPDATE "{t}" SET "{c}" = gc_scrub("{c}") '
                             f'WHERE typeof("{c}") = \'text\' AND ({cond})',
                             (*needles, *secrets.forms))
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
    out["recent_errors"] = _rows(conn, "SELECT id, instrument_id, lab_id, injection_dt, "
                                       "received_at, status, method_name, error FROM samples "
                                       "WHERE status='error' ORDER BY received_at DESC, id DESC "
                                       "LIMIT ?", (RECENT_ERRORS_LAST,))
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
    except (ValueError, OSError):
        return "outside/" + _safe(path.name)


def _is_cdf(path: Path) -> bool:
    return path.suffix.lower() == ".cdf"


# ── exports health (C1) ─────────────────────────────────────────────────────

def _export_probe(b: _Bundle, inst_id: str, path: Path) -> dict:
    """Stat the export file; read its sidecar and tail only when it is
    provably this instrument's hub export."""
    import exports
    row = {"exists": False, "size": None, "mtime": None, "sidecar": None, "tail": [],
           "tail_skipped": None, "lock_present": exports.lock_path(path).exists()}
    st = path.stat() if path.is_file() else None
    if st is None:
        return row
    row.update(exists=True, size=st.st_size, mtime=_mtime_iso(st))
    real = path.resolve()
    if path.suffix.lower() != ".csv" or real.suffix.lower() != ".csv":
        row["tail_skipped"] = "not a .csv"
        return row
    why = b.excluded(path) or b.excluded(real)
    if why:
        row["tail_skipped"] = why
        return row
    sidecar = None
    side = exports.sidecar_path(path)
    if side.is_file() and not side.is_symlink():
        try:
            doc = json.loads(side.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and doc.get("instrument") == inst_id:
                sidecar = doc
        except (OSError, ValueError):
            pass
    header = exports.header_line().rstrip("\r\n").encode("utf-8")
    with open(real, "rb") as fh:
        first = fh.read(len(header) + 2)
    is_hub = sidecar is not None or first.rstrip(b"\r\n") == header
    if not is_hub:
        row["tail_skipped"] = ("not a hub export (no sidecar naming this instrument, and not "
                               "the hub's header)")
        return row
    row["sidecar"] = redact_settings(sidecar) if sidecar is not None else None
    with open(real, "rb") as fh:
        fh.seek(max(0, st.st_size - EXPORT_TAIL_BYTES))
        tail = fh.read().decode("utf-8", errors="replace")
    lines = tail.splitlines()
    if st.st_size > EXPORT_TAIL_BYTES:
        lines = lines[1:]            # a partial first line
    row["tail"] = [redact_text(line, b.secrets) for line in lines[-EXPORT_TAIL_LINES:]]
    return row


def _export_health(b: _Bundle, conn: sqlite3.Connection, data_dir: Path) -> list:
    out = []
    for inst in _rows(conn, "SELECT id, export_path FROM instruments ORDER BY id"):
        configured = (inst.get("export_path") or "").strip()
        path = Path(configured) if configured else data_dir / "results" / f"{inst['id']}_results.csv"
        pending = conn.execute("SELECT count(*) FROM export_rows WHERE instrument_id=? "
                               "AND hub_appended_at IS NULL", (inst["id"],)).fetchone()[0]
        row = {"instrument": inst["id"], "path": str(path), "configured": bool(configured),
               "pending": pending}
        ok, probe = bounded(lambda: _export_probe(b, inst["id"], path))
        if ok:
            row.update(probe)
        else:
            row.update(exists=None, size=None, mtime=None, sidecar=None, tail=[],
                       lock_present=None, tail_skipped="unreadable or timed out")
        out.append(row)
    return out


# ── calibration, updater, environment (I5) ──────────────────────────────────

def _add_calibration(b: _Bundle, conn: sqlite3.Connection, data_dir: Path,
                     settings: Optional[dict]) -> None:
    for inst in _rows(conn, "SELECT id, calibration_cdf, calibration_assignments "
                            "FROM instruments ORDER BY id"):
        base = f"calibration/{_safe(inst['id'])}"
        raw = (inst.get("calibration_cdf") or "").strip()
        if raw:
            p = Path(raw)
            if not p.is_absolute():
                p = data_dir / p
            if not _is_cdf(p):
                b.skip(f"{base}/{_safe(p.name)}", "calibration file is not a .cdf")
            else:
                b.add_file(f"{base}/{_safe(p.name)}", p, text=False, outside_ok=True)
        assignments = inst.get("calibration_assignments")
        if assignments:
            try:
                b.add_json(f"{base}/assignments.json", json.loads(assignments))
            except ValueError:
                b.add_text(f"{base}/assignments.txt", str(assignments))
    raw = str((settings or {}).get("correction_factors_json") or "").strip()
    if raw:
        p = Path(raw)
        name = "calibration/correction_factors.json"
        st = _stat(p)
        why = None
        if p.suffix.lower() != ".json":
            why = "correction factors file is not a .json"
        elif st is None:
            why = "missing or unreadable"
        elif st.st_size > CORRECTIONS_MAX_BYTES:
            why = f"too large ({st.st_size} bytes)"
        else:
            why = b.excluded(p)
        if why:
            b.skip(name, why)
        else:
            ok, text = bounded(lambda: _read_text(p, CORRECTIONS_MAX_BYTES))
            if not ok or text is None:
                b.skip(name, "unreadable or timed out")
            else:
                try:
                    b.add_json(name, redact_settings(json.loads(text)))
                except ValueError:
                    b.add_text(name, text)
    std = data_dir / "gc_comparison_standards"
    files = _walk_files(std)
    listing = [{"name": p.relative_to(std).as_posix(), "size": p.stat().st_size} for p in files]
    if std.is_dir():
        b.add_json("calibration/standards.json", listing)
    total = sum(f["size"] for f in listing)
    if total > STANDARDS_MAX_BYTES:
        b.skip("calibration/standards/", f"standards total {total} bytes; listed only")
    else:
        for p in files:
            b.add_file(f"calibration/standards/{p.relative_to(std).as_posix()}", p, text=False)


def _add_updater(b: _Bundle, data_dir: Path) -> None:
    root = app_root(data_dir)
    ok, is_dir = bounded(lambda: root.is_dir())
    if ok and is_dir:
        info: dict = {"path": str(root), "files": {}, "dirs": [], "links": {}}
        for p in sorted(root.iterdir()):
            try:
                if p.is_symlink() or getattr(os.path, "isjunction", lambda x: False)(p):
                    ok, target = bounded(lambda p=p: os.readlink(p))
                    info["links"][p.name] = redact_text(str(target), b.secrets) if ok else None
                elif p.is_dir():
                    info["dirs"].append(p.name)
                    if p.name == "releases":
                        info["releases"] = sorted(c.name for c in p.iterdir() if c.is_dir())
                elif p.is_file():
                    if b.excluded(p) or is_secret_key(p.name):
                        info["files"][p.name] = {"size": p.stat().st_size,
                                                 "content": "(not read: a secret)"}
                    elif p.stat().st_size <= UPDATER_FILE_MAX:
                        info["files"][p.name] = _small_file_info(p, b.secrets)
                    else:
                        info["files"][p.name] = {"size": p.stat().st_size,
                                                 "content": "(not read: large)"}
            except OSError as exc:
                info["files"][p.name] = {"error": str(exc)}
        b.add_json("updater/app_root.json", info)
    udir = updater_dir(data_dir)
    ok, is_dir = bounded(lambda: udir.is_dir())
    if not (ok and is_dir):
        return
    log = udir / "updater.log"
    if log.is_file() and not log.is_symlink() and not b.excluded(log):
        def tail():
            size = log.stat().st_size
            with open(log, "rb") as fh:
                fh.seek(max(0, size - UPDATER_LOG_BYTES))
                lines = fh.read().decode("utf-8", errors="replace").splitlines()
            if size > UPDATER_LOG_BYTES:
                lines = lines[1:]
            return lines[-UPDATER_LOG_LINES:]
        ok, lines = bounded(tail)
        if ok:
            b.add_text("updater/updater.log", "\n".join(lines) + "\n")
        else:
            b.skip("updater/updater.log", "unreadable or timed out")
    for p in sorted(udir.iterdir()):
        if (not p.is_file() or p.is_symlink() or p.suffix.lower() not in UPDATER_CONFIG_SUFFIXES
                or b.excluded(p)):
            continue
        name = f"updater/{_safe(p.name)}"
        if p.stat().st_size > UPDATER_FILE_MAX:
            b.skip(name, "config file too large")
            continue
        text = _read_text(p, UPDATER_FILE_MAX) or ""
        try:
            b.add_json(name, redact_settings(json.loads(text)))
        except ValueError:
            b.add_text(name, _redact_config_text(text))


def environment_info() -> dict:
    try:
        from importlib import metadata
        pkgs = sorted({f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions()
                       if d.metadata and d.metadata["Name"]}, key=str.lower)
    except Exception as exc:  # noqa: BLE001
        pkgs = [f"(unavailable: {exc})"]
    return {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
            "packages": pkgs,
            "env": {n: ("set" if os.environ.get(n) else "unset") for n in ENV_NAMES}}


def _summary_text(m: dict) -> str:
    lines = ["GC hub diagnostics", "==================", ""]
    for key in ("created_at", "who", "hostname", "app_version", "git", "python", "platform",
                "data_dir", "uptime_seconds", "disk", "runtime", "status_snapshot", "options"):
        lines.append(f"{key}: {json.dumps(m.get(key), default=str)}")
    s = m.get("secrets") or {}
    lines += ["", "secrets:",
              f"  values redacted: {s.get('redacted_values', 0)}",
              f"  {s.get('too_short_to_redact', 0)} too short to redact (left as they are; "
              f"never shown)",
              f"  Selenium login file (qbenchlogin.txt): {s.get('selenium_login')}"]
    lines += ["", "updater files:"]
    for name, info in (m.get("updater") or {}).items():
        lines.append(f"  {name}: {json.dumps(info.get('content', info), default=str)}")
    if m.get("counts"):
        lines += ["", "samples per instrument/status:"]
        for r in m["counts"]:
            lines.append(f"  {r['instrument_id']} {r['status']} backfill={r['backfill']}: {r['n']}")
    trunc = m.get("truncated") or {}
    if trunc.get("all_cdfs"):
        t = trunc["all_cdfs"]
        lines += ["", f"all raw CDFs stopped at {t['cap_bytes']} bytes "
                      f"({t['files_left_out']} file(s) left out)"]
    if m.get("skipped"):
        lines += ["", "skipped:"]
        lines += [f"  {s['name']}: {s['reason']}" for s in m["skipped"]]
    return "\n".join(lines) + "\n"


def build_bundle(options: Any, *, data_dir, db, out_path, progress: Progress = None,
                 who: Optional[str] = None, runtime: Any = None,
                 now: Optional[datetime] = None, extra_secrets: Iterable[str] = ()) -> dict:
    """Write the diagnostics zip to ``out_path`` and return its manifest.
    ``extra_secrets`` are more values to redact (the route passes the admin
    password it was given)."""
    opts = normalize_options(options)
    data_dir = Path(data_dir).resolve()
    db = Path(db)
    out_path = Path(out_path)
    now = now or datetime.now(timezone.utc)
    work = out_path.parent / (out_path.name + ".work")
    work.mkdir(parents=True, exist_ok=True)
    secrets = known_secrets(data_dir, db, extra=extra_secrets)
    import version
    manifest: dict = {
        "kind": "gc-hub-diagnostics", "format": 2,
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
                   "all_cdf_cap_bytes": ALL_CDF_CAP_BYTES, "problem_days": PROBLEM_DAYS,
                   "reports_last": REPORTS_LAST, "report_log_last": REPORT_LOG_LAST,
                   "jobs_done_last": JOBS_DONE_LAST, "export_tail_lines": EXPORT_TAIL_LINES},
        "secrets": {"redacted_values": len(secrets), "too_short_to_redact": secrets.too_short,
                    "selenium_login": secrets.selenium},
        "truncated": {},
    }
    b = None
    try:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            b = _Bundle(zf, data_dir, secrets, progress)
            needs_db = any(opts[k] for k in ("tables", "database", "exports", "problem_cdfs",
                                             "calibration"))
            snap = work / "gc.db"
            conn = None
            if needs_db and db.is_file():
                b.event("database")
                snapshot_db(db, snap, secrets)
                conn = sqlite3.connect(f"{snap.resolve().as_uri()}?mode=ro", uri=True)
            elif needs_db:
                b.skip("database/gc.db", "missing")
            settings_doc = None
            sf = data_dir / "settings.json"
            if sf.is_file():
                try:
                    settings_doc = json.loads(_read_text(sf, TEXT_IN_MEMORY_MAX) or "")
                except ValueError:
                    settings_doc = None
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
                            margin = secrets.overlap
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
                        budget -= b.add_bytes(name, raw)
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
                    if settings_doc is None:
                        b.skip("settings/settings.json", "missing or not valid JSON")
                    else:
                        b.add_json("settings/settings.json", redact_settings(settings_doc))
                if opts["exports"] and conn is not None:
                    b.event("exports")
                    b.add_json("exports/health.json", _export_health(b, conn, data_dir))
                if opts["reports"]:
                    b.event("reports")
                    rdir = data_dir / "reports"
                    files = _walk_files(rdir)
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
                        outside = not _within(p, data_dir)
                        if outside and not _is_cdf(p):
                            b.skip(name, f"outside the data folder and not a .cdf ({why})")
                            continue
                        st = _stat(p)
                        if st is None:
                            b.skip(name, f"missing ({why})")
                            continue
                        if st.st_size > budget:
                            b.skip(name, f"problem CDF cap reached ({why}, {st.st_size} bytes)")
                            continue
                        budget -= b.add_file(name, p, text=False, outside_ok=outside)
                if opts["calibration"] and conn is not None:
                    b.event("calibration")
                    _add_calibration(b, conn, data_dir, settings_doc)
                if opts["updater"]:
                    b.event("updater")
                    _add_updater(b, data_dir)
                if opts["environment"]:
                    b.add_json("environment.json", environment_info())
                if opts["all_cdfs"]:
                    b.event("all_cdfs")
                    cdir = data_dir / "cdf"
                    budget = ALL_CDF_CAP_BYTES
                    files = _walk_files(cdir)
                    for i, p in enumerate(files):
                        name = _arcname(data_dir, p)
                        if name in b.names:
                            continue
                        size = p.stat().st_size
                        if size > budget:
                            manifest["truncated"]["all_cdfs"] = {
                                "cap_bytes": ALL_CDF_CAP_BYTES,
                                "files_left_out": len(files) - i}
                            break
                        budget -= b.add_file(name, p, text=False)
                if opts["database"] and conn is not None:
                    b.event("database-add")
                    conn.close()
                    conn = None
                    if b.leaks("database/gc.db", b"") or secrets.in_file(snap):
                        b.skip("database/gc.db", "a secret survived the scrub; left out")
                    else:
                        zf.write(snap, "database/gc.db")
                        b._record("database/gc.db", path=snap)
            finally:
                if conn is not None:
                    conn.close()
            manifest["files"] = list(b.files)
            manifest["skipped"] = list(b.skipped)
            if opts["summary"]:
                b.add_text("summary.txt", _summary_text(manifest))
                manifest["files"] = list(b.files)
                manifest["skipped"] = list(b.skipped)
            text = redact_text(json.dumps(manifest, indent=1, default=str, ensure_ascii=False),
                               secrets)
            zf.writestr("manifest.json", text.encode("utf-8"))
            manifest = json.loads(text)
        b.event("verify")
        _verify(out_path, secrets)
    except BaseException:
        with contextlib.suppress(OSError):
            out_path.unlink()
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    b.event("done")
    return manifest


def _verify(path: Path, secrets: Secrets) -> None:
    """Read every member back; a known secret anywhere fails the build."""
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if secrets.in_text(info.filename):
                raise SecretLeak("a secret is in a member name")
            tail = b""
            with zf.open(info) as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    buf = tail + chunk
                    if secrets.in_bytes(buf):
                        raise SecretLeak(f"a secret is in {info.filename}")
                    tail = buf[-secrets.overlap:] if secrets.overlap else b""


# ── size estimate and disk check ────────────────────────────────────────────

def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


_estimate_cache: dict = {}
_estimate_lock = threading.Lock()


def clear_estimate_cache() -> None:
    with _estimate_lock:
        _estimate_cache.clear()


def estimate(*, data_dir, db, now: Optional[datetime] = None) -> dict:
    """``{option: {label, default, bytes, files, note}}``, uncompressed sizes,
    cached for ``ESTIMATE_TTL_SECONDS`` (M5: the walk is not free)."""
    key = (str(Path(data_dir)), str(Path(db)))
    with _estimate_lock:
        hit = _estimate_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < ESTIMATE_TTL_SECONDS:
            return json.loads(json.dumps(hit[1]))
    value = _estimate(data_dir=Path(data_dir), db=Path(db),
                      now=now or datetime.now(timezone.utc))
    with _estimate_lock:
        _estimate_cache[key] = (time.monotonic(), value)
    return json.loads(json.dumps(value))


def _estimate(*, data_dir: Path, db: Path, now: datetime) -> dict:
    out = {k: {"label": LABELS[k], "default": OPTIONS[k], "bytes": 0, "files": 0, "note": ""}
           for k in OPTION_KEYS}
    out["summary"].update(bytes=16 * 1024, files=2)
    logs = _log_files(data_dir)
    total = sum(_size(p) for p in logs)
    out["logs"].update(bytes=min(total, LOG_CAP_BYTES), files=len(logs),
                       note=f"capped at {LOG_CAP_BYTES // (1024 * 1024)} MB" if total > LOG_CAP_BYTES
                       else "")
    out["settings"].update(bytes=_size(data_dir / "settings.json"),
                           files=int((data_dir / "settings.json").is_file()))
    reports = sorted(_walk_files(data_dir / "reports"), key=lambda p: -p.stat().st_mtime
                     )[:REPORTS_LAST]
    out["reports"].update(bytes=sum(_size(p) for p in reports), files=len(reports))
    cdfs = _walk_files(data_dir / "cdf")
    cdf_bytes = sum(_size(p) for p in cdfs)
    out["all_cdfs"].update(bytes=min(cdf_bytes, ALL_CDF_CAP_BYTES), files=len(cdfs),
                           note=(f"{cdf_bytes} bytes in all; stops at "
                                 f"{ALL_CDF_CAP_BYTES // (1024 ** 3)} GB"
                                 if cdf_bytes > ALL_CDF_CAP_BYTES
                                 else "can be very large; off by default"))
    std = _walk_files(data_dir / "gc_comparison_standards")
    std_bytes = sum(_size(p) for p in std)
    cal_bytes = std_bytes if std_bytes <= STANDARDS_MAX_BYTES else 0
    cal_files = len(std) if std_bytes <= STANDARDS_MAX_BYTES else 0
    ud = updater_dir(data_dir)
    ok, has = bounded(lambda: ud.is_dir())
    out["updater"].update(bytes=(min(_size(ud / "updater.log"), 200 * 1024) + 64 * 1024)
                          if ok and has else 16 * 1024, files=3, note="approximate")
    out["environment"].update(bytes=32 * 1024, files=1, note="approximate")
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
            rows += RECENT_ERRORS_LAST
            out["tables"].update(bytes=rows * 400 + _size(data_dir / "notifications.json"),
                                 files=12, note="approximate")
            insts = _rows(conn, "SELECT id, calibration_cdf FROM instruments")
            out["exports"].update(bytes=len(insts) * 4096, files=1, note="approximate")
            for inst in insts:
                raw = (inst.get("calibration_cdf") or "").strip()
                if raw:
                    p = Path(raw) if Path(raw).is_absolute() else data_dir / raw
                    st = _stat(p) if _is_cdf(p) else None
                    if st is not None:
                        cal_bytes += st.st_size
                        cal_files += 1
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
    out["calibration"].update(bytes=cal_bytes + 16 * 1024, files=cal_files + 2,
                              note="" if std_bytes <= STANDARDS_MAX_BYTES
                              else "standards listed only (over 50 MB)")
    return out


def check_disk(tmp: Path, options: dict, *, data_dir, db) -> dict:
    """Refuse (``NoSpace``) unless the temp folder's disk has the estimate for
    ``options`` + the database size (the working copy) + ``DISK_MARGIN_BYTES``
    free. Returns ``{free, need}``."""
    est = estimate(data_dir=data_dir, db=db)
    need = (sum(est[k]["bytes"] for k, on in options.items() if on)
            + _size(Path(db)) + DISK_MARGIN_BYTES)
    free = shutil.disk_usage(tmp).free
    if free < need:
        gib = 1024 ** 3
        raise NoSpace(f"Not enough free disk space for the diagnostics bundle: "
                      f"{free / gib:.1f} GB free, about {need / gib:.1f} GB needed (the "
                      f"bundle, a working copy of the database and a 2 GB margin). Untick "
                      f"the large options or free some space.")
    return {"free": free, "need": need}


TMP_MAX_AGE_SECONDS = 3600


def cleanup_tmp(data_dir, max_age: float = TMP_MAX_AGE_SECONDS) -> Path:
    """The temp folder under the data folder, cleared of leftovers older than
    ``max_age`` (``0`` at start-up: nothing can be in use then). Bundles
    waiting to be downloaded are kept. Returns the folder."""
    tmp = Path(data_dir) / TMP_DIRNAME
    cutoff = time.time() - max_age
    with _state_lock:
        waiting = {Path(d["path"]).name for d in _downloads.values()}
    if tmp.is_dir():
        for p in tmp.iterdir():
            if p.name in waiting:
                continue
            try:
                if max_age > 0 and p.lstat().st_mtime >= cutoff:
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


__all__ = ["Busy", "NoSpace", "SecretLeak", "OPTIONS", "OPTION_KEYS", "build_bundle", "busy",
           "check_disk", "claim_download", "cleanup_tmp", "estimate", "exclusive",
           "known_secrets", "normalize_options", "register_download", "streaming"]
