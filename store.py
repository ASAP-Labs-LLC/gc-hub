"""store.py: the hub's SQLite store (phase 2, schema v1).

SQLite is the source of truth (spec D6). This module owns the schema, its
additive migrations, short-lived connections, and small typed helpers. It is
stdlib-only apart from ``paths`` and has no import-time side effects.

API summary
===========

Every helper takes a keyword-only ``db`` argument, which is one of:

* ``None``: the default database, ``paths.data_dir()/gc.db`` (``RuntimeError``
  if ``GC_DATA_DIR`` is unset);
* a path (``str``/``Path``): a short-lived connection is opened for this one
  call and closed again; writes run in their own ``BEGIN IMMEDIATE``;
* an open ``sqlite3.Connection`` (from ``open_db``/``connection``): used as is
  and never closed. If the connection is inside ``write_txn`` the write joins
  that transaction; otherwise it gets its own ``BEGIN IMMEDIATE``.

Connections and transactions::

    open_db(path=None) -> sqlite3.Connection    # WAL, busy_timeout=10000, synchronous=NORMAL,
                                                # foreign_keys=ON, row_factory=sqlite3.Row,
                                                # autocommit (isolation_level=None)
    connection(db=None)                         # context manager: open_db + close
    write_txn(conn)                             # context manager: BEGIN IMMEDIATE ... COMMIT,
                                                # ROLLBACK on exception; nested = SAVEPOINT
    migrate(path=None) -> int                   # additive, PRAGMA user_version; returns version
    backup_nightly(path=None, keep=14, *, settings_path=None, now=None) -> Path
    default_db_path() -> Path
    now_iso() -> str                            # UTC 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00'

Samples and revisions::

    samples.insert_received(instrument_id, lab_id, injection_dt, injection_dt_source, *,
        cdf_sha256, cdf_path, method_name=None, source_name=None, is_blank=0, backfill=0,
        status='received', legacy_injection_dt=None, time_corrected=0,
        legacy_unverified=0, received_at=None, db=None) -> int (sample id)
    samples.get(sample_id, *, db) -> dict | None
    samples.find_by_sha(sha256, *, db) -> dict | None          # across ALL instruments
    samples.find_by_key(instrument_id, lab_id, injection_dt, *, db) -> dict | None
    samples.set_status(sample_id, status, *, error=None, db)   # error cleared unless given
    samples.update(sample_id, *, db, **fields)                 # whitelisted columns only
    samples.search(q=None, instrument=None, date_from=None, date_to=None, status=None,
                   limit=100, offset=0, *, db) -> list[dict]   # newest injection first
    samples.count(q=None, instrument=None, date_from=None, date_to=None, status=None, *, db) -> int
    add_revision(conn, sample_id, results, *, reason, by=None, d86_uncorrected=None,
                 calibration_used=None, blank_used=None, corrections_used=None,
                 best_fit=None, fit_score=None, flags=None, processed_at=None) -> int
                 # MUST run inside write_txn(conn); bumps samples.current_revision
    get_revision(sample_id, revision=None, *, db) -> dict | None   # None = current
    list_revisions(sample_id, *, db) -> list[dict]                 # ascending

Instruments::

    instruments.get(instrument_id, *, db) -> dict | None
    instruments.list(enabled_only=False, *, db) -> list[dict]      # ordered by id
    instruments.upsert(fields: dict, *, db) -> dict                 # 'id' required; partial update

Exports (the ledger; file writing is exports.py)::

    export_rows.append_pending(conn, instrument_id, sample_id, revision, line) -> int (seq)
    export_rows.pending_hub_appends(instrument_id, *, db) -> list[dict]   # seq ascending
    export_rows.mark_hub_appended(seq | [seq, ...], *, at=None, db)
    export_rows.rows_after(instrument_id, seq, limit=500, *, db) -> list[dict]
    # row dicts: {seq, instrument_id, sample_id, revision, line, hub_appended_at}

Jobs (durable queue)::

    jobs.enqueue(kind, payload, not_before=None, *, db) -> int
    jobs.claim_next(now=None, *, kind=None, db) -> dict | None   # atomic; payload decoded
    jobs.complete(job_id, *, db)
    jobs.fail(job_id, error, retry_at=None, *, db)   # retry_at -> 'queued' again, else 'failed'
    jobs.requeue_stale_running(*, db) -> int
    jobs.get(job_id, *, db) -> dict | None

Small tables::

    sample_cache.get(sample_id, *, db) -> dict | None
    sample_cache.put(sample_id, *, db, **fields)        # merges only the fields given
    settings_kv.get(key, default=None, *, db) -> str | None
    settings_kv.set(key, value, *, db) / settings_kv.delete(key, *, db)
    conflicts.add(instrument_id, lab_id, injection_dt, existing_sample_id, cdf_sha256,
                  cdf_path, *, received_at=None, db) -> int
    conflicts.list(instrument_id=None, unresolved_only=True, *, db) -> list[dict]
    conflicts.resolve(conflict_id, resolution, *, by, db)   # 'kept-existing' | 'replaced'
    CorrectionsCacheStore(db=None).load(instrument_id) / .save(instrument_id, values, methods, fetched_at)
                                                        # the contracts §2 cache_store

Conventions and decisions (where the spec left a choice)
========================================================

* **Rows come back as plain ``dict``s**, detached from the connection.
  JSON columns (``method_map``, ``results``, ``flags``...) are returned as the
  stored TEXT; helpers accept a ``dict``/``list`` for them and encode it with
  ``json.dumps`` (a ``str`` is stored verbatim). The one exception is
  ``jobs`` rows, whose ``payload`` is decoded.
* **Timestamps** written by the store (``received_at``, ``created_at``,
  ``processed_at``, ``resolved_at``, ``hub_appended_at``, jobs'
  ``not_before``) are UTC ISO-8601 with microseconds and ``+00:00``
  (``now_iso()``), so they sort as strings. ``jobs`` normalise any datetime or
  ISO string they are given (naive = UTC). ``injection_dt`` is **not** a
  store timestamp: it is the naive local ``isoformat(sep=" ")`` from the CDF,
  stored exactly as given.
* **Integrity errors are not wrapped.** ``insert_received`` raises
  ``sqlite3.IntegrityError`` on a duplicate ``cdf_sha256`` (any instrument)
  or a duplicate (instrument, lab ID, injection time). Callers that need
  "check then insert" do both inside one ``write_txn``.
* ``samples.time_corrected`` is an INTEGER flag (0/1). The spec's comment on
  that line (``'cdf'|'mtime'``) belongs to ``injection_dt_source``.
* ``samples.status`` has a CHECK constraint with exactly the eight statuses
  of the spec's status machine (``STATUSES``). Adding a status later is not
  an additive change (SQLite cannot alter a CHECK): it needs a table rebuild
  in a migration.
* ``samples.id`` and ``export_rows.seq`` are ``AUTOINCREMENT`` so they are
  never reused, even if a row is deleted: agents track ``results_seq`` and
  stored CDF filenames embed the sample id.
* Foreign keys beyond the spec's explicit ones: ``sample_results.blank_used``
  and ``conflicts.existing_sample_id`` → ``samples``; ``export_rows`` →
  ``instruments``, ``samples`` and ``(sample_id, revision)`` →
  ``sample_results``; ``agents``, ``corrections_cache``, ``standards`` →
  ``instruments``; ``sample_cache`` → ``samples``. ``current_revision`` and
  ``qbench_revision`` carry no FK (circular / optional).
* ``jobs.state`` is one of ``queued``, ``running``, ``done``, ``failed``
  (no CHECK, so new states stay additive). There is one worker, so
  ``requeue_stale_running`` treats every ``running`` job as stale; call it
  only at start-up, before the worker runs.
* Column names that are SQL keywords (``corrections_cache."values"``,
  ``export_rows."row"``, ``sample_results."by"``) are always quoted.
  ``export_rows.row`` is exposed as ``line`` by the helpers.
* **Backups.** Pre-migrate copies go to ``<db dir>/backups/pre-migrate-<from
  version>-<UTC stamp>.db`` via the SQLite backup API (WAL-safe), only when
  the file already existed and a migration is pending. Nightly backups are
  ``<db dir>/backups/gc-<YYYY-MM-DD>.db`` (``VACUUM INTO``; a second run on
  the same day replaces that day's file) plus ``settings-<YYYY-MM-DD>.json``.
  Pruning keeps the newest ``keep`` ``gc-*.db`` files and removes the settings
  copies of pruned days; pre-migrate copies are never pruned.
* **Rollback safety.** ``migrate`` on a database whose ``user_version`` is
  higher than ``SCHEMA_VERSION`` logs a warning and carries on. It raises
  ``SchemaError`` only if a table or column this code needs is missing.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence, Union

import paths

log = logging.getLogger("store")

DB_FILENAME = "gc.db"
BUSY_TIMEOUT_MS = 10000

STATUSES: tuple[str, ...] = (
    "received", "awaiting_calibration", "pending_corrections", "final",
    "raw_only", "error", "other_method", "review_method",
)
CONFLICT_RESOLUTIONS: tuple[str, ...] = ("kept-existing", "replaced")
REVISION_REASONS: tuple[str, ...] = (
    "processed", "reprocess", "import", "export-lims", "corrections-released", "replace",
)

Db = Union[None, str, os.PathLike, sqlite3.Connection]


class SchemaError(RuntimeError):
    """The database lacks a table or column this code needs."""


# ── schema ──────────────────────────────────────────────────────────────────

_STATUS_LIST = ", ".join(f"'{s}'" for s in STATUSES)

# MIGRATIONS[i] takes user_version i to i + 1. Additive only: CREATE TABLE,
# CREATE INDEX, ALTER TABLE ... ADD COLUMN. Never drop, rename or rewrite.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (  # v1
        """CREATE TABLE instruments(
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'D2887',
            enabled INTEGER NOT NULL DEFAULT 1,
            live_since TEXT,
            calibration_cdf TEXT,
            calibration_assignments TEXT,
            calibration_sensitivity REAL NOT NULL DEFAULT 50,
            lem_machine_uid TEXT,
            correction_map TEXT,
            token_hash TEXT,
            token_issued_at TEXT,
            export_path TEXT,
            method_map TEXT,
            created_at TEXT,
            updated_at TEXT)""",
        f"""CREATE TABLE samples(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id TEXT NOT NULL REFERENCES instruments(id),
            lab_id TEXT NOT NULL,
            injection_dt TEXT NOT NULL,
            injection_dt_source TEXT NOT NULL,
            method_name TEXT,
            legacy_injection_dt TEXT,
            time_corrected INTEGER NOT NULL DEFAULT 0,
            cdf_sha256 TEXT UNIQUE,
            cdf_path TEXT,
            legacy_unverified INTEGER NOT NULL DEFAULT 0,
            source_name TEXT,
            is_blank INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK (status IN ({_STATUS_LIST})),
            backfill INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            current_revision INTEGER,
            released_at TEXT,
            released_by TEXT,
            qbench_revision INTEGER,
            qbench_uploaded_at TEXT,
            received_at TEXT NOT NULL,
            UNIQUE(instrument_id, lab_id, injection_dt))""",
        "CREATE INDEX samples_status ON samples(status)",
        "CREATE INDEX samples_inst_dt ON samples(instrument_id, injection_dt)",
        "CREATE INDEX samples_dt ON samples(injection_dt)",
        "CREATE INDEX samples_lab ON samples(lab_id)",
        """CREATE TABLE sample_results(
            sample_id INTEGER NOT NULL REFERENCES samples(id),
            revision INTEGER NOT NULL,
            results TEXT NOT NULL,
            d86_uncorrected TEXT,
            calibration_used TEXT,
            blank_used INTEGER REFERENCES samples(id),
            corrections_used TEXT,
            best_fit TEXT,
            fit_score REAL,
            flags TEXT,
            reason TEXT NOT NULL,
            "by" TEXT,
            processed_at TEXT NOT NULL,
            PRIMARY KEY(sample_id, revision))""",
        """CREATE TABLE conflicts(
            id INTEGER PRIMARY KEY,
            instrument_id TEXT REFERENCES instruments(id),
            lab_id TEXT,
            injection_dt TEXT,
            existing_sample_id INTEGER REFERENCES samples(id),
            cdf_sha256 TEXT,
            cdf_path TEXT,
            received_at TEXT,
            resolved TEXT,
            resolved_by TEXT,
            resolved_at TEXT)""",
        """CREATE TABLE export_rows(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id TEXT NOT NULL REFERENCES instruments(id),
            sample_id INTEGER NOT NULL REFERENCES samples(id),
            revision INTEGER NOT NULL,
            "row" TEXT NOT NULL,
            hub_appended_at TEXT,
            FOREIGN KEY(sample_id, revision) REFERENCES sample_results(sample_id, revision))""",
        "CREATE INDEX export_rows_inst_seq ON export_rows(instrument_id, seq)",
        "CREATE INDEX export_rows_pending ON export_rows(instrument_id, seq) WHERE hub_appended_at IS NULL",
        """CREATE TABLE jobs(
            id INTEGER PRIMARY KEY,
            kind TEXT,
            payload TEXT,
            state TEXT,
            attempts INTEGER,
            not_before TEXT,
            last_error TEXT,
            created_at TEXT)""",
        "CREATE INDEX jobs_state ON jobs(state, not_before, id)",
        """CREATE TABLE agents(
            instrument_id TEXT PRIMARY KEY REFERENCES instruments(id),
            version TEXT,
            state TEXT,
            queue_size INTEGER,
            rejected_count INTEGER,
            last_file TEXT,
            last_error TEXT,
            host TEXT,
            agent_time TEXT,
            last_seen TEXT,
            results_seq INTEGER,
            pending_command TEXT)""",
        """CREATE TABLE corrections_cache(
            instrument_id TEXT PRIMARY KEY REFERENCES instruments(id),
            "values" TEXT,
            methods TEXT,
            fetched_at TEXT)""",
        """CREATE TABLE standards(
            id INTEGER PRIMARY KEY,
            name TEXT,
            instrument_id TEXT REFERENCES instruments(id),
            cdf_path TEXT,
            added_at TEXT)""",
        """CREATE TABLE sample_cache(
            sample_id INTEGER PRIMARY KEY REFERENCES samples(id),
            rules_fingerprint TEXT,
            flags TEXT,
            bestfit_fingerprint TEXT,
            best_fit TEXT,
            fit_score REAL)""",
        """CREATE TABLE settings_kv(
            key TEXT PRIMARY KEY,
            value TEXT)""",
    ),
)
SCHEMA_VERSION = len(MIGRATIONS)

# Tables and columns this code reads or writes. On a newer database these
# must exist; anything extra is ignored.
REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "instruments": frozenset({
        "id", "name", "method", "enabled", "live_since", "calibration_cdf",
        "calibration_assignments", "calibration_sensitivity", "lem_machine_uid",
        "correction_map", "token_hash", "token_issued_at", "export_path", "method_map",
        "created_at", "updated_at"}),
    "samples": frozenset({
        "id", "instrument_id", "lab_id", "injection_dt", "injection_dt_source",
        "method_name", "legacy_injection_dt", "time_corrected", "cdf_sha256", "cdf_path",
        "legacy_unverified", "source_name", "is_blank", "status", "backfill", "error",
        "current_revision", "released_at", "released_by", "qbench_revision",
        "qbench_uploaded_at", "received_at"}),
    "sample_results": frozenset({
        "sample_id", "revision", "results", "d86_uncorrected", "calibration_used",
        "blank_used", "corrections_used", "best_fit", "fit_score", "flags", "reason",
        "by", "processed_at"}),
    "conflicts": frozenset({
        "id", "instrument_id", "lab_id", "injection_dt", "existing_sample_id",
        "cdf_sha256", "cdf_path", "received_at", "resolved", "resolved_by", "resolved_at"}),
    "export_rows": frozenset({
        "seq", "instrument_id", "sample_id", "revision", "row", "hub_appended_at"}),
    "jobs": frozenset({
        "id", "kind", "payload", "state", "attempts", "not_before", "last_error",
        "created_at"}),
    "agents": frozenset({
        "instrument_id", "version", "state", "queue_size", "rejected_count", "last_file",
        "last_error", "host", "agent_time", "last_seen", "results_seq", "pending_command"}),
    "corrections_cache": frozenset({"instrument_id", "values", "methods", "fetched_at"}),
    "standards": frozenset({"id", "name", "instrument_id", "cdf_path", "added_at"}),
    "sample_cache": frozenset({
        "sample_id", "rules_fingerprint", "flags", "bestfit_fingerprint", "best_fit",
        "fit_score"}),
    "settings_kv": frozenset({"key", "value"}),
}


# ── small utilities ─────────────────────────────────────────────────────────

def now_iso() -> str:
    """Current UTC time as the store's canonical timestamp string."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _ts(value: Union[None, str, datetime, date]) -> Optional[str]:
    """Normalise a datetime or ISO string to the canonical UTC form (naive = UTC)."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    elif isinstance(value, date) and not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _enc(value: Any) -> Any:
    """JSON-encode dicts/lists (and other non-scalars); pass str/numbers/None through."""
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    return json.dumps(value)


def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
    return None if r is None else dict(r)


def _rows(rs: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rs]


def default_db_path() -> Path:
    """``paths.data_dir()/gc.db``. Raises ``RuntimeError`` without ``GC_DATA_DIR``."""
    d = paths.data_dir()
    if d is None:
        raise RuntimeError(f"{paths.DATA_ENV} is not set; the hub store needs a data folder")
    return d / DB_FILENAME


def _resolve(path: Union[None, str, os.PathLike]) -> Path:
    return default_db_path() if path is None else Path(path)


# ── connections and transactions ────────────────────────────────────────────

def open_db(path: Union[None, str, os.PathLike] = None) -> sqlite3.Connection:
    """Open a short-lived connection with the hub's pragmas.

    Autocommit mode (``isolation_level=None``): nothing is in a transaction
    unless ``write_txn`` (or a helper) begins one. The caller must close it;
    prefer ``with connection(...)``.
    """
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
    except BaseException:
        conn.close()
        raise
    return conn


@contextlib.contextmanager
def connection(db: Db = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection: ``db`` itself if it is one, else a new one closed on exit."""
    if isinstance(db, sqlite3.Connection):
        yield db
        return
    conn = open_db(db)
    try:
        yield conn
    finally:
        conn.close()


_savepoint_ids = threading.local()


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``; ``ROLLBACK`` and re-raise on any exception.

    Nested use (the connection is already in a transaction) opens a
    SAVEPOINT instead, which is rolled back on its own if the inner block
    raises, leaving the outer transaction to decide.
    """
    if conn.in_transaction:
        n = getattr(_savepoint_ids, "n", 0) + 1
        _savepoint_ids.n = n
        name = f"sp_{n}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextlib.contextmanager
def _writing(db: Db) -> Iterator[sqlite3.Connection]:
    """A connection inside a write transaction (joining the caller's if any)."""
    with connection(db) as conn:
        with write_txn(conn):
            yield conn


def _require_txn(conn: sqlite3.Connection, what: str) -> None:
    if not isinstance(conn, sqlite3.Connection) or not conn.in_transaction:
        raise RuntimeError(f"{what} must be called inside write_txn(conn)")


# ── migrations ──────────────────────────────────────────────────────────────

def _backup_to(conn: sqlite3.Connection, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(str(dest))
    try:
        conn.backup(out)
    finally:
        out.close()


def _pre_migrate_backup(conn: sqlite3.Connection, db_path: Path, from_version: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups = db_path.parent / "backups"
    dest = backups / f"pre-migrate-{from_version}-{stamp}.db"
    i = 1
    while dest.exists():
        dest = backups / f"pre-migrate-{from_version}-{stamp}-{i}.db"
        i += 1
    _backup_to(conn, dest)
    log.info("store: pre-migrate backup written to %s", dest)
    return dest


def _check_columns(conn: sqlite3.Connection) -> None:
    missing = []
    for table, cols in REQUIRED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if not have:
            missing.append(table)
            continue
        for c in sorted(cols - have):
            missing.append(f"{table}.{c}")
    if missing:
        raise SchemaError("database is missing: " + ", ".join(missing))


def migrate(path: Union[None, str, os.PathLike] = None) -> int:
    """Bring the database to ``SCHEMA_VERSION``; return the resulting ``user_version``.

    Additive only. If the file existed and a migration is pending, it is
    first copied to ``backups/pre-migrate-<v>-<ts>.db``. Each step runs in
    one ``BEGIN IMMEDIATE`` transaction together with its ``user_version``
    bump, re-reading the version under the lock so two processes can't
    apply a step twice. A newer ``user_version`` is logged and tolerated.
    """
    db_path = _resolve(path)
    existed = db_path.exists() and db_path.stat().st_size > 0
    with connection(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            log.warning("store: database user_version %s is newer than this code's %s; "
                        "carrying on (additive migrations)", version, SCHEMA_VERSION)
        elif version < SCHEMA_VERSION:
            if existed:
                _pre_migrate_backup(conn, db_path, version)
            while True:
                with write_txn(conn):
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if version >= SCHEMA_VERSION:
                        break
                    for stmt in MIGRATIONS[version]:
                        conn.execute(stmt)
                    conn.execute(f"PRAGMA user_version = {version + 1}")
                log.info("store: migrated to schema v%s", version + 1)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        _check_columns(conn)
        return version


# ── backups ─────────────────────────────────────────────────────────────────

_NIGHTLY_RE = re.compile(r"^gc-(\d{4}-\d{2}-\d{2})\.db$")


def backup_nightly(path: Union[None, str, os.PathLike] = None, keep: int = 14, *,
                   settings_path: Union[None, str, os.PathLike] = None,
                   now: Optional[datetime] = None) -> Path:
    """``VACUUM INTO backups/gc-<date>.db`` plus a copy of ``settings.json``; prune to ``keep``.

    ``settings_path`` defaults to ``settings.json`` next to the database (the
    data folder). ``now`` (local date) is injectable for tests. Returns the
    backup path.
    """
    db_path = _resolve(path)
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    backups = db_path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / f"gc-{day}.db"
    tmp = backups / f".gc-{day}.db.tmp"
    if tmp.exists():
        tmp.unlink()
    with connection(db_path) as conn:
        conn.execute("VACUUM INTO ?", (str(tmp),))
    os.replace(tmp, dest)

    settings = Path(settings_path) if settings_path is not None else db_path.parent / "settings.json"
    if settings.is_file():
        s_tmp = backups / f".settings-{day}.json.tmp"
        s_tmp.write_bytes(settings.read_bytes())
        os.replace(s_tmp, backups / f"settings-{day}.json")

    dated = sorted((m.group(1), p) for p in backups.iterdir()
                   if (m := _NIGHTLY_RE.match(p.name)))
    for old_day, old in dated[:max(0, len(dated) - keep)]:
        old.unlink()
        s = backups / f"settings-{old_day}.json"
        if s.exists():
            s.unlink()
    log.info("store: nightly backup written to %s", dest)
    return dest


# ── revisions ───────────────────────────────────────────────────────────────

def add_revision(conn: sqlite3.Connection, sample_id: int, results: Any, *, reason: str,
                 by: Optional[str] = None, d86_uncorrected: Any = None,
                 calibration_used: Any = None, blank_used: Optional[int] = None,
                 corrections_used: Any = None, best_fit: Optional[str] = None,
                 fit_score: Optional[float] = None, flags: Any = None,
                 processed_at: Optional[str] = None) -> int:
    """Write the next ``sample_results`` revision and make it current.

    Must run inside ``write_txn(conn)``, so the revision, the export row and
    the status change commit together. Returns the new revision number
    (1 for the first). ``reason`` is free text; the spec's values are in
    ``REVISION_REASONS``.
    """
    _require_txn(conn, "add_revision")
    rev = conn.execute("SELECT COALESCE(MAX(revision), 0) + 1 FROM sample_results WHERE sample_id=?",
                       (sample_id,)).fetchone()[0]
    conn.execute(
        'INSERT INTO sample_results(sample_id, revision, results, d86_uncorrected, '
        'calibration_used, blank_used, corrections_used, best_fit, fit_score, flags, '
        'reason, "by", processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (sample_id, rev, _enc(results), _enc(d86_uncorrected), _enc(calibration_used),
         blank_used, _enc(corrections_used), best_fit, fit_score, _enc(flags), reason, by,
         processed_at or now_iso()))
    conn.execute("UPDATE samples SET current_revision=? WHERE id=?", (rev, sample_id))
    return rev


def get_revision(sample_id: int, revision: Optional[int] = None, *, db: Db = None) -> Optional[dict]:
    """One revision (``None`` = the sample's ``current_revision``), or ``None``."""
    with connection(db) as conn:
        if revision is None:
            r = conn.execute("SELECT current_revision FROM samples WHERE id=?", (sample_id,)).fetchone()
            if r is None or r[0] is None:
                return None
            revision = r[0]
        return _row(conn.execute("SELECT * FROM sample_results WHERE sample_id=? AND revision=?",
                                 (sample_id, revision)).fetchone())


def list_revisions(sample_id: int, *, db: Db = None) -> list[dict]:
    """Every revision of a sample, oldest first."""
    with connection(db) as conn:
        return _rows(conn.execute("SELECT * FROM sample_results WHERE sample_id=? ORDER BY revision",
                                  (sample_id,)))


# ── instruments ─────────────────────────────────────────────────────────────

class instruments:  # noqa: N801  (a namespace: store.instruments.get(...))
    """The ``instruments`` table."""

    COLUMNS = REQUIRED_COLUMNS["instruments"]

    @staticmethod
    def get(instrument_id: str, *, db: Db = None) -> Optional[dict]:
        with connection(db) as conn:
            return _row(conn.execute("SELECT * FROM instruments WHERE id=?", (instrument_id,)).fetchone())

    @staticmethod
    def list(enabled_only: bool = False, *, db: Db = None) -> list[dict]:
        sql = "SELECT * FROM instruments" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
        with connection(db) as conn:
            return _rows(conn.execute(sql))

    @staticmethod
    def upsert(fields: dict, *, db: Db = None) -> dict:
        """Insert or partially update one instrument; returns the stored row.

        ``fields['id']`` is required, and ``name`` is required on insert.
        Only the given columns change on update. ``created_at``/``updated_at``
        are maintained here. Unknown keys raise ``ValueError``.
        """
        fields = dict(fields)
        iid = fields.pop("id", None)
        if not iid:
            raise ValueError("instrument 'id' is required")
        unknown = set(fields) - (instruments.COLUMNS - {"id"})
        if unknown:
            raise ValueError(f"unknown instrument columns: {sorted(unknown)}")
        stamp = now_iso()
        fields.pop("created_at", None)
        fields["updated_at"] = stamp
        with _writing(db) as conn:
            exists = conn.execute("SELECT 1 FROM instruments WHERE id=?", (iid,)).fetchone()
            if exists:
                cols = sorted(fields)
                conn.execute(f"UPDATE instruments SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",
                             [_enc(fields[c]) for c in cols] + [iid])
            else:
                fields["created_at"] = stamp
                cols = ["id"] + sorted(fields)
                conn.execute(f"INSERT INTO instruments({', '.join(cols)}) "
                             f"VALUES ({', '.join('?' for _ in cols)})",
                             [iid] + [_enc(fields[c]) for c in cols[1:]])
            return dict(conn.execute("SELECT * FROM instruments WHERE id=?", (iid,)).fetchone())


# ── samples ─────────────────────────────────────────────────────────────────

def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_where(q, instrument, date_from, date_to, status) -> tuple[str, list]:
    where, args = [], []
    if q:
        pat = f"%{_like_escape(str(q).strip())}%"
        where.append("(lab_id LIKE ? ESCAPE '\\' OR source_name LIKE ? ESCAPE '\\')")
        args += [pat, pat]
    if instrument:
        if isinstance(instrument, str):
            instrument = [instrument]
        where.append(f"instrument_id IN ({', '.join('?' for _ in instrument)})")
        args += list(instrument)
    if date_from:
        where.append("injection_dt >= ?")
        args.append(str(date_from))
    if date_to:
        s = str(date_to)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            # a bare date is inclusive of that whole day
            where.append("injection_dt < ?")
            args.append((date.fromisoformat(s) + timedelta(days=1)).isoformat())
        else:
            where.append("injection_dt <= ?")
            args.append(s)
    if status:
        if isinstance(status, str):
            status = [status]
        where.append(f"status IN ({', '.join('?' for _ in status)})")
        args += list(status)
    return (" WHERE " + " AND ".join(where)) if where else "", args


class samples:  # noqa: N801
    """The ``samples`` table."""

    UPDATABLE = frozenset(REQUIRED_COLUMNS["samples"] - {"id", "instrument_id", "received_at"})

    @staticmethod
    def insert_received(instrument_id: str, lab_id: str, injection_dt: str,
                        injection_dt_source: str, *, cdf_sha256: Optional[str],
                        cdf_path: Optional[str], method_name: Optional[str] = None,
                        source_name: Optional[str] = None, is_blank: int = 0,
                        backfill: int = 0, status: str = "received",
                        legacy_injection_dt: Optional[str] = None, time_corrected: int = 0,
                        legacy_unverified: int = 0, received_at: Optional[str] = None,
                        db: Db = None) -> int:
        """Insert a new sample (status ``received`` by default); return its id.

        Raises ``sqlite3.IntegrityError`` on a duplicate sha256 (any
        instrument), a duplicate (instrument, lab ID, injection time), an
        unknown instrument, or a status outside ``STATUSES``.
        """
        with _writing(db) as conn:
            cur = conn.execute(
                "INSERT INTO samples(instrument_id, lab_id, injection_dt, injection_dt_source, "
                "method_name, legacy_injection_dt, time_corrected, cdf_sha256, cdf_path, "
                "legacy_unverified, source_name, is_blank, status, backfill, received_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (instrument_id, lab_id, injection_dt, injection_dt_source, method_name,
                 legacy_injection_dt, int(time_corrected), cdf_sha256, cdf_path,
                 int(legacy_unverified), source_name, int(is_blank), status, int(backfill),
                 received_at or now_iso()))
            return int(cur.lastrowid)

    @staticmethod
    def get(sample_id: int, *, db: Db = None) -> Optional[dict]:
        with connection(db) as conn:
            return _row(conn.execute("SELECT * FROM samples WHERE id=?", (sample_id,)).fetchone())

    @staticmethod
    def find_by_sha(cdf_sha256: str, *, db: Db = None) -> Optional[dict]:
        """The sample holding this CDF, on any instrument."""
        with connection(db) as conn:
            return _row(conn.execute("SELECT * FROM samples WHERE cdf_sha256=?", (cdf_sha256,)).fetchone())

    @staticmethod
    def find_by_key(instrument_id: str, lab_id: str, injection_dt: str, *, db: Db = None) -> Optional[dict]:
        with connection(db) as conn:
            return _row(conn.execute(
                "SELECT * FROM samples WHERE instrument_id=? AND lab_id=? AND injection_dt=?",
                (instrument_id, lab_id, injection_dt)).fetchone())

    @staticmethod
    def set_status(sample_id: int, status: str, *, error: Optional[str] = None, db: Db = None) -> None:
        """Set ``status`` and ``error`` (cleared unless given). ``ValueError`` on an unknown status."""
        if status not in STATUSES:
            raise ValueError(f"unknown sample status {status!r}")
        with _writing(db) as conn:
            conn.execute("UPDATE samples SET status=?, error=? WHERE id=?", (status, error, sample_id))

    @staticmethod
    def update(sample_id: int, *, db: Db = None, **fields: Any) -> None:
        """Update whitelisted columns (e.g. ``cdf_path``, ``released_at``/``released_by``,
        ``qbench_revision``/``qbench_uploaded_at``, ``backfill``, ``is_blank``).
        ``id``, ``instrument_id`` and ``received_at`` are immutable (``ValueError``)."""
        if not fields:
            return
        bad = set(fields) - samples.UPDATABLE
        if bad:
            raise ValueError(f"cannot update sample columns: {sorted(bad)}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"unknown sample status {fields['status']!r}")
        cols = sorted(fields)
        with _writing(db) as conn:
            conn.execute(f"UPDATE samples SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",
                         [_enc(fields[c]) for c in cols] + [sample_id])

    @staticmethod
    def search(q: Optional[str] = None, instrument: Union[None, str, Sequence[str]] = None,
               date_from: Optional[str] = None, date_to: Optional[str] = None,
               status: Union[None, str, Sequence[str]] = None, limit: int = 100,
               offset: int = 0, *, db: Db = None) -> list[dict]:
        """Filter samples, newest injection first (ties: newest id first).

        ``q``: case-insensitive substring of ``lab_id`` or ``source_name``
        (LIKE wildcards are literal). ``instrument``/``status``: one value or a
        list. ``date_from``/``date_to`` compare against ``injection_dt``; a
        bare ``YYYY-MM-DD`` ``date_to`` includes that whole day.
        """
        where, args = _search_where(q, instrument, date_from, date_to, status)
        with connection(db) as conn:
            return _rows(conn.execute(
                f"SELECT * FROM samples{where} ORDER BY injection_dt DESC, id DESC LIMIT ? OFFSET ?",
                args + [int(limit), int(offset)]))

    @staticmethod
    def count(q: Optional[str] = None, instrument: Union[None, str, Sequence[str]] = None,
              date_from: Optional[str] = None, date_to: Optional[str] = None,
              status: Union[None, str, Sequence[str]] = None, *, db: Db = None) -> int:
        """How many samples ``search`` would match without paging."""
        where, args = _search_where(q, instrument, date_from, date_to, status)
        with connection(db) as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM samples{where}", args).fetchone()[0])


# ── export rows ─────────────────────────────────────────────────────────────

_EXPORT_COLS = 'seq, instrument_id, sample_id, revision, "row" AS line, hub_appended_at'


class export_rows:  # noqa: N801
    """The append-only export ledger. ``line`` is the frozen CSV line (column ``row``)."""

    @staticmethod
    def append_pending(conn: sqlite3.Connection, instrument_id: str, sample_id: int,
                       revision: int, line: str) -> int:
        """Record a newly final result's CSV line; return its ``seq``.

        Must run inside ``write_txn(conn)`` with the matching ``add_revision``.
        """
        _require_txn(conn, "export_rows.append_pending")
        cur = conn.execute('INSERT INTO export_rows(instrument_id, sample_id, revision, "row") '
                           "VALUES (?,?,?,?)", (instrument_id, sample_id, revision, line))
        return int(cur.lastrowid)

    @staticmethod
    def pending_hub_appends(instrument_id: str, *, db: Db = None) -> list[dict]:
        """Rows not yet appended to the hub-side CSV, ``seq`` ascending."""
        with connection(db) as conn:
            return _rows(conn.execute(
                f"SELECT {_EXPORT_COLS} FROM export_rows "
                "WHERE instrument_id=? AND hub_appended_at IS NULL ORDER BY seq", (instrument_id,)))

    @staticmethod
    def mark_hub_appended(seq: Union[int, Iterable[int]], *, at: Optional[str] = None,
                          db: Db = None) -> None:
        """Stamp one ``seq`` or several as appended to the hub CSV."""
        seqs = [seq] if isinstance(seq, int) else list(seq)
        if not seqs:
            return
        stamp = at or now_iso()
        with _writing(db) as conn:
            conn.executemany("UPDATE export_rows SET hub_appended_at=? WHERE seq=?",
                             [(stamp, s) for s in seqs])

    @staticmethod
    def rows_after(instrument_id: str, seq: int, limit: int = 500, *, db: Db = None) -> list[dict]:
        """This instrument's rows with ``seq`` greater than ``seq``, ascending (the agent pull)."""
        with connection(db) as conn:
            return _rows(conn.execute(
                f"SELECT {_EXPORT_COLS} FROM export_rows WHERE instrument_id=? AND seq>? "
                "ORDER BY seq LIMIT ?", (instrument_id, int(seq), int(limit))))


# ── jobs ────────────────────────────────────────────────────────────────────

def _job(r: Optional[sqlite3.Row]) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    if d.get("payload") is not None:
        d["payload"] = json.loads(d["payload"])
    return d


class jobs:  # noqa: N801
    """Durable job queue. States: ``queued`` → ``running`` → ``done`` | ``failed``."""

    @staticmethod
    def enqueue(kind: str, payload: Any, not_before: Union[None, str, datetime] = None, *,
                db: Db = None) -> int:
        """Queue a job; ``payload`` is JSON-encoded. Returns the job id."""
        with _writing(db) as conn:
            cur = conn.execute(
                "INSERT INTO jobs(kind, payload, state, attempts, not_before, created_at) "
                "VALUES (?, ?, 'queued', 0, ?, ?)",
                (kind, json.dumps(payload), _ts(not_before), now_iso()))
            return int(cur.lastrowid)

    @staticmethod
    def claim_next(now: Union[None, str, datetime] = None, *, kind: Optional[str] = None,
                   db: Db = None) -> Optional[dict]:
        """Atomically take the oldest due ``queued`` job (``not_before`` NULL or ≤ ``now``).

        Marks it ``running`` and increments ``attempts``; returns the job with
        ``payload`` decoded, or ``None``. ``BEGIN IMMEDIATE`` makes the
        select-and-update exclusive, so two workers never claim the same job.
        """
        stamp = _ts(now) or now_iso()
        sql = ("SELECT id FROM jobs WHERE state='queued' AND (not_before IS NULL OR not_before <= ?)"
               + (" AND kind=?" if kind else "") + " ORDER BY id LIMIT 1")
        args = [stamp] + ([kind] if kind else [])
        with _writing(db) as conn:
            r = conn.execute(sql, args).fetchone()
            if r is None:
                return None
            conn.execute("UPDATE jobs SET state='running', attempts=COALESCE(attempts, 0) + 1 WHERE id=?",
                         (r["id"],))
            return _job(conn.execute("SELECT * FROM jobs WHERE id=?", (r["id"],)).fetchone())

    @staticmethod
    def complete(job_id: int, *, db: Db = None) -> None:
        with _writing(db) as conn:
            conn.execute("UPDATE jobs SET state='done' WHERE id=?", (job_id,))

    @staticmethod
    def fail(job_id: int, error: Optional[str], retry_at: Union[None, str, datetime] = None, *,
             db: Db = None) -> None:
        """Record ``error``. With ``retry_at`` the job is queued again for then; else ``failed``."""
        with _writing(db) as conn:
            if retry_at is not None:
                conn.execute("UPDATE jobs SET state='queued', not_before=?, last_error=? WHERE id=?",
                             (_ts(retry_at), error, job_id))
            else:
                conn.execute("UPDATE jobs SET state='failed', last_error=? WHERE id=?", (error, job_id))

    @staticmethod
    def requeue_stale_running(*, db: Db = None) -> int:
        """Put every ``running`` job back to ``queued`` (start-up only); return how many."""
        with _writing(db) as conn:
            return conn.execute("UPDATE jobs SET state='queued' WHERE state='running'").rowcount

    @staticmethod
    def get(job_id: int, *, db: Db = None) -> Optional[dict]:
        with connection(db) as conn:
            return _job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


# ── sample_cache, settings_kv, conflicts, corrections_cache ─────────────────

class sample_cache:  # noqa: N801
    """Flags and best-fit cache, one row per sample (replaces the JSON caches)."""

    FIELDS = frozenset(REQUIRED_COLUMNS["sample_cache"] - {"sample_id"})

    @staticmethod
    def get(sample_id: int, *, db: Db = None) -> Optional[dict]:
        with connection(db) as conn:
            return _row(conn.execute("SELECT * FROM sample_cache WHERE sample_id=?", (sample_id,)).fetchone())

    @staticmethod
    def put(sample_id: int, *, db: Db = None, **fields: Any) -> None:
        """Upsert; only the fields given change (``rules_fingerprint``, ``flags``,
        ``bestfit_fingerprint``, ``best_fit``, ``fit_score``)."""
        bad = set(fields) - sample_cache.FIELDS
        if bad:
            raise ValueError(f"unknown sample_cache fields: {sorted(bad)}")
        cols = sorted(fields)
        with _writing(db) as conn:
            conn.execute("INSERT OR IGNORE INTO sample_cache(sample_id) VALUES (?)", (sample_id,))
            if cols:
                conn.execute(f"UPDATE sample_cache SET {', '.join(f'{c}=?' for c in cols)} WHERE sample_id=?",
                             [_enc(fields[c]) for c in cols] + [sample_id])


class settings_kv:  # noqa: N801
    """Hub-wide key/value settings (admin hash, LEM_URL...). Values are TEXT."""

    @staticmethod
    def get(key: str, default: Optional[str] = None, *, db: Db = None) -> Optional[str]:
        with connection(db) as conn:
            r = conn.execute("SELECT value FROM settings_kv WHERE key=?", (key,)).fetchone()
            return default if r is None else r[0]

    @staticmethod
    def set(key: str, value: Any, *, db: Db = None) -> None:
        with _writing(db) as conn:
            conn.execute("INSERT INTO settings_kv(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _enc(value)))

    @staticmethod
    def delete(key: str, *, db: Db = None) -> None:
        with _writing(db) as conn:
            conn.execute("DELETE FROM settings_kv WHERE key=?", (key,))


class conflicts:  # noqa: N801
    """Same (instrument, lab ID, injection time) received with a different CDF."""

    @staticmethod
    def add(instrument_id: str, lab_id: str, injection_dt: str, existing_sample_id: Optional[int],
            cdf_sha256: str, cdf_path: str, *, received_at: Optional[str] = None,
            db: Db = None) -> int:
        with _writing(db) as conn:
            cur = conn.execute(
                "INSERT INTO conflicts(instrument_id, lab_id, injection_dt, existing_sample_id, "
                "cdf_sha256, cdf_path, received_at) VALUES (?,?,?,?,?,?,?)",
                (instrument_id, lab_id, injection_dt, existing_sample_id, cdf_sha256, cdf_path,
                 received_at or now_iso()))
            return int(cur.lastrowid)

    @staticmethod
    def list(instrument_id: Optional[str] = None, unresolved_only: bool = True, *,
             db: Db = None) -> list[dict]:
        """Conflicts, oldest first."""
        where, args = [], []
        if instrument_id:
            where.append("instrument_id=?")
            args.append(instrument_id)
        if unresolved_only:
            where.append("resolved IS NULL")
        sql = "SELECT * FROM conflicts" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        with connection(db) as conn:
            return _rows(conn.execute(sql, args))

    @staticmethod
    def resolve(conflict_id: int, resolution: str, *, by: str, db: Db = None) -> None:
        """Record the admin's choice. ``ValueError`` if the resolution is unknown or
        the conflict is missing or already resolved. (Replacing the sample's CDF
        is the caller's job, in the same ``write_txn`` if it passes ``db=conn``.)"""
        if resolution not in CONFLICT_RESOLUTIONS:
            raise ValueError(f"unknown conflict resolution {resolution!r}")
        with _writing(db) as conn:
            n = conn.execute("UPDATE conflicts SET resolved=?, resolved_by=?, resolved_at=? "
                             "WHERE id=? AND resolved IS NULL",
                             (resolution, by, now_iso(), conflict_id)).rowcount
            if n != 1:
                raise ValueError(f"conflict {conflict_id} is missing or already resolved")


class CorrectionsCacheStore:
    """SQLite ``cache_store`` for ``corrections.LemProvider`` (contracts §2)."""

    def __init__(self, db: Union[None, str, os.PathLike] = None):
        self.db = db

    def load(self, instrument_id: str) -> Optional[dict]:
        """``{values, methods, fetched_at}`` or ``None``."""
        with connection(self.db) as conn:
            r = conn.execute('SELECT "values", methods, fetched_at FROM corrections_cache '
                             "WHERE instrument_id=?", (instrument_id,)).fetchone()
        if r is None:
            return None
        return {"values": json.loads(r["values"]) if r["values"] else {},
                "methods": json.loads(r["methods"]) if r["methods"] else [],
                "fetched_at": r["fetched_at"]}

    def save(self, instrument_id: str, values: dict, methods: list, fetched_at: str) -> None:
        with _writing(self.db) as conn:
            conn.execute('INSERT INTO corrections_cache(instrument_id, "values", methods, fetched_at) '
                         "VALUES (?,?,?,?) ON CONFLICT(instrument_id) DO UPDATE SET "
                         '"values"=excluded."values", methods=excluded.methods, '
                         "fetched_at=excluded.fetched_at",
                         (instrument_id, json.dumps(values), json.dumps(list(methods)), fetched_at))
