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
* an open ``sqlite3.Connection``: used as is and never closed. Inside
  ``write_txn(conn)`` the write joins that transaction; otherwise it gets its
  own ``BEGIN IMMEDIATE``.

**Inside ``write_txn(conn)`` always pass ``db=conn``.** Opening any new
connection on a thread that holds a write transaction raises
``RuntimeError("... pass db=conn inside write_txn")`` at once: a second
connection would either wait ``busy_timeout`` for the lock its own thread
holds, or read the pre-transaction state. **Never call network, corrections
or other slow code inside ``write_txn``**; compute first, then write.

Connections and transactions::

    open_db(path=None, *, create=False, readonly=False) -> sqlite3.Connection
        # WAL, busy_timeout=10000, synchronous=NORMAL, foreign_keys=ON, row_factory=Row,
        # autocommit (isolation_level=None). The file must exist unless create=True
        # (only migrate creates it).
    connection(db=None)                   # context manager: open_db + close
    write_txn(conn)                       # BEGIN IMMEDIATE ... COMMIT; ROLLBACK on exception
                                          # or COMMIT failure; nested = SAVEPOINT
    migrate(path=None) -> int             # creates/upgrades; additive; PRAGMA user_version
    backup_nightly(path=None, keep=14, *, settings_path=None, now=None) -> Path
    default_db_path() -> Path
    now_iso() -> str                      # UTC 'YYYY-MM-DDTHH:MM:SS.ffffff+00:00'
    local_dt(value) -> str                # naive local 'YYYY-MM-DD HH:MM:SS' (injection_dt form)
    is_backfill(live_since, injection_dt) -> bool
    GATE_SQL                              # "status='final' AND (backfill=0 OR released_at IS NOT NULL)"

Samples and revisions::

    samples.insert_received(instrument_id, lab_id, injection_dt, injection_dt_source, *,
        cdf_sha256, cdf_path, method_name=None, source_name=None, is_blank=0, backfill=0,
        status='received', legacy_injection_dt=None, time_corrected=0,
        legacy_unverified=0, received_at=None, time_unverifiable=0, db=None) -> int (sample id)
    samples.get(sample_id, *, db) -> dict | None
    samples.find_by_sha(sha256, *, db) -> dict | None          # across ALL instruments
    samples.find_by_key(instrument_id, lab_id, injection_dt, *, db) -> dict | None
    samples.find_by_legacy(instrument_id, lab_id, dt, *, db) -> list[dict]
        # injection_dt = dt OR legacy_injection_dt = dt; legacy matches first
    samples.latest_blank(instrument_id, at, method_names, *, exclude_sample_id=None, db) -> dict | None
        # same-time ties: highest cdf_sha256 (content, not arrival)
    samples.is_gated(sample_id, *, db) -> bool                 # the export/QBench gate
    samples.methods_seen(instrument_id, *, db) -> list[{method_name, count, first_seen, last_seen}]
    samples.set_status(sample_id, status, *, error=None, db)   # error cleared unless given
    samples.update(sample_id, *, db, **fields)                 # whitelisted columns only
    samples.search(q=None, instrument=None, date_from=None, date_to=None, status=None,
                   limit=100, offset=0, *, method_name=None, backfill=None,
                   time_unverifiable=None, db) -> list[dict]
    samples.count(<same filters>, *, db) -> int
    add_revision(conn, sample_id, results, *, reason, by=None, d86_uncorrected=None,
                 calibration_used=None, blank_used=None, corrections_used=None,
                 best_fit=None, fit_score=None, flags=None, processed_at=None, notes=None,
                 cdf_sha256=<sample's>, cdf_path=<sample's>) -> int   # the CDF that produced it
                 # MUST run inside write_txn(conn); bumps samples.current_revision
    get_revision(sample_id, revision=None, *, db) -> dict | None   # None = current
    list_revisions(sample_id, *, db) -> list[dict]                 # ascending

Instruments and corrections (D4b)::

    instruments.get(instrument_id, *, db) -> dict | None
    instruments.list(enabled_only=False, *, db) -> list[dict]      # ordered by id
    instruments.upsert(fields: dict, *, db) -> dict                 # 'id' required; partial update
    corrections.read(instrument_id, *, db) -> {values, updated_at, updated_by} | None
    corrections.set_all(conn, instrument_id, values, *, by, reason) -> int (cuts changed)
    corrections.audit(instrument_id, limit=100, *, db) -> list[dict]   # newest first

Exports (the ledger; file writing is exports.py)::

    export_rows.append_pending(conn, instrument_id, sample_id, revision, line) -> int (seq)
    export_rows.pending_hub_appends(instrument_id, *, db) -> list[dict]   # seq ascending
    export_rows.mark_hub_appended(seq | [seq, ...], *, at=None, db)
    export_rows.rows_after(instrument_id, seq, limit=500, *, db) -> list[dict]
    # row dicts: {seq, instrument_id, sample_id, revision, line, hub_appended_at}

Jobs (durable queue)::

    jobs.enqueue(kind, payload, not_before=None, *, sample_id=None, db) -> int
    jobs.enqueue_for_status(instrument_id, status, *, method_name=None, kind='process', db) -> int
    jobs.claim_next(now=None, *, kind=None, db) -> dict | None   # atomic; payload decoded
    jobs.complete(job_id, *, db)
    jobs.fail(job_id, error, retry_at=None, *, db)   # retry_at -> queued again, else 'failed'
    jobs.requeue_stale_running(*, db) -> int
    jobs.prune_done(older_than, *, db) -> int
    jobs.get(job_id, *, db) / jobs.list(state=None, kind=None, *, db)

Small tables::

    sample_cache.get(sample_id, *, db) / sample_cache.put(sample_id, *, db, **fields)
    settings_kv.get(key, default=None, *, db) / .set(key, value, *, db) / .delete(key, *, db)
    conflicts.add(instrument_id, lab_id, injection_dt, existing_sample_id, cdf_sha256,
                  cdf_path, *, received_at=None, db) -> int
    conflicts.list(instrument_id=None, unresolved_only=True, *, db) -> list[dict]
    conflicts.find_by_sha(sha256, unresolved_only=True, *, db) -> dict | None
    conflicts.get(conflict_id, *, db) -> dict | None
    conflicts.set_error(conflict_id, error, *, db)          # why the last Replace failed (None clears)
    conflicts.resolve(conflict_id, resolution, *, by, db)   # 'kept-existing' | 'replaced'; clears error

Conventions and decisions (where the spec left a choice)
========================================================

* **Rows come back as plain ``dict``s**, detached from the connection.
  JSON columns (``method_map``, ``results``, ``flags``...) are returned as the
  stored TEXT; helpers accept a ``dict``/``list`` and encode it with
  ``json.dumps`` (a ``str`` is stored verbatim). The one exception is
  ``jobs`` rows, whose ``payload`` is decoded (left as TEXT if undecodable).
* **Two time domains.**
  - *Store timestamps* (``received_at``, ``created_at``/``updated_at``,
    ``processed_at``, ``resolved_at``, ``hub_appended_at``, jobs'
    ``not_before``/``finished_at``, corrections' ``updated_at``/``changed_at``)
    are tz-aware UTC, ``now_iso()`` form, so they sort as strings. Jobs
    **reject** naive datetimes and offset-less strings (``ValueError``).
  - *Instrument-clock times* (``injection_dt``, ``legacy_injection_dt``,
    ``instruments.live_since`` and the ``search`` date bounds) are naive
    local ``datetime.isoformat(sep=" ")``, e.g. ``2026-10-01 00:00:00``,
    because that is how the CDF/CSV hold injection times. ``live_since`` and
    the date bounds are normalised to that form (``local_dt``), so string
    comparison with ``injection_dt`` is correct: the ISO ``T`` form would
    sort after the same moment written with a space. An aware value (or an
    offset/``Z`` string) is refused with ``ValueError``.
* **Statuses are validated in code** (``STATUSES``), with no CHECK
  constraint, so a later release can add one additively; this code reads
  unknown statuses without complaint. ``INJECTION_DT_SOURCES`` is
  ``('cdf', 'mtime', 'csv')``; ``'csv'`` is result-only imports.
* **``method_name``** is stored normalised (trimmed, basename, upper-case;
  the caller normalises). ``''`` means the CDF had none; NULL only for
  result-only imports. ``insert_received`` turns ``None`` into ``''`` for a
  CDF-backed sample.
* **``is_blank``** means a *genuine* blank. It is decided **once, in
  ``pipeline.submit`` at receive time, from the CDF bytes** (the name rule
  plus ``is_plausible_blank``), and passed to ``insert_received`` — not at
  process time — so blank selection never depends on processing order.
  ``latest_blank`` also requires a stored CDF, and normalises ``at`` with
  ``local_dt``.
* **Integrity errors are not wrapped.** ``insert_received`` raises
  ``sqlite3.IntegrityError`` on a duplicate ``cdf_sha256`` (any instrument)
  or a duplicate (instrument, lab ID, injection time). Check and insert
  inside one ``write_txn``.
* ``samples.time_unverifiable`` (0/1, beyond the spec text; Lane D) marks a
  result-only import whose CSV time could be a v1 misparse, so its correct
  injection time can't be established.
* ``samples.review_note`` (TEXT, beyond the spec; 2A1 T2) is a
  human-readable note that a final result may need a look (e.g. an
  earlier-injected blank arrived after it was processed). No revision is
  touched. ``sample_results.notes`` (JSON, beyond the spec) records how a
  revision was computed, e.g. ``{"blank_rejected": {"sample_id", "reason"}}``.
* ``sample_results.cdf_sha256``/``cdf_path`` (beyond the spec; 2D) record the
  CDF each revision was computed from (NULL for a result-only import), so a
  conflict Replace leaves a trail. ``conflicts.error`` (beyond the spec) is
  the last failed Replace attempt's message, cleared when it is resolved.
* ``samples.time_corrected`` is an INTEGER flag (0/1). The spec's comment on
  that line (``'cdf'|'mtime'``) belongs to ``injection_dt_source``.
* ``samples.id``, ``export_rows.seq`` and ``corrections_audit.id`` are
  ``AUTOINCREMENT``: never reused, even after a delete.
* Foreign keys beyond the spec's explicit ones: ``sample_results.blank_used``,
  ``conflicts.existing_sample_id`` and ``jobs.sample_id`` → ``samples``;
  ``export_rows`` → ``instruments``, ``samples`` and ``(sample_id, revision)``
  → ``sample_results``; ``agents``, ``instrument_corrections``,
  ``standards``, ``conflicts.instrument_id`` → ``instruments``;
  ``sample_cache`` → ``samples``. ``corrections_audit`` has none (append-only
  history must survive anything).
* **Jobs.** States ``queued``, ``running``, ``done``, ``failed``,
  ``superseded`` (no CHECK). Two columns beyond the spec: ``sample_id``
  (nullable) and ``finished_at``. At most one *queued* job per
  ``(kind, sample_id)`` (partial unique index): ``enqueue`` of a duplicate
  returns the existing job, replaces its payload (last intent wins) and
  keeps the earlier ``not_before`` (NULL = now). ``enqueue_for_status``
  only brings a queued job forward to now and keeps its payload, since its
  own payload is the bare ``{"sample_id"}``. A running job re-queued while a twin is
  queued becomes ``superseded``. ``claim_next`` marks a job with an
  undecodable payload ``failed`` and moves on. ``requeue_stale_running``
  treats every ``running`` job as stale: call it only at start-up.
* **Corrections (D4b).** ``set_all`` writes only the cuts whose value
  changed, with one timestamp, and one audit row per changed cut
  (``old_value`` NULL on first insert). The reason is required. Checking for all 11 cuts and
  the ±50 °C limit is ``corrections.py``'s job; the store only refuses
  non-finite numbers.
* Column names that are SQL keywords (``export_rows."row"``,
  ``sample_results."by"``) are always quoted. ``export_rows.row`` is exposed
  as ``line`` by the helpers.
* **Backups.** Pre-migrate copies go to ``<db dir>/backups/pre-migrate-<from
  version>-<UTC stamp>.db`` via the SQLite backup API (includes WAL frames),
  only when the file already existed and a migration is pending. Nightly:
  ``<db dir>/backups/gc-<YYYY-MM-DD>.db`` by ``VACUUM INTO`` from a read-only
  connection (a second run the same day replaces that day's file), plus
  ``settings-<YYYY-MM-DD>.json`` copied from ``paths.settings_file()``.
  Pruning keeps the newest ``keep`` (≥ 1) ``gc-*.db`` files and the settings
  copies of the days kept; pre-migrate copies are never pruned.
* **Rollback safety.** ``migrate`` on a database whose ``user_version`` is
  higher than ``len(MIGRATIONS)`` logs a warning and carries on. It raises
  ``SchemaError`` only if a table or column this code needs is missing.
  Each migration step is one transaction with its ``user_version`` bump.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
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
INJECTION_DT_SOURCES: tuple[str, ...] = ("cdf", "mtime", "csv")
CONFLICT_RESOLUTIONS: tuple[str, ...] = ("kept-existing", "replaced")
REVISION_REASONS: tuple[str, ...] = (
    "processed", "reprocess", "import", "export-lims", "corrections-released", "replace",
)
GATE_SQL = "status='final' AND (backfill=0 OR released_at IS NOT NULL)"

Db = Union[None, str, os.PathLike, sqlite3.Connection]
PathLike = Union[None, str, os.PathLike]


class SchemaError(RuntimeError):
    """The database lacks a table or column this code needs."""


# ── schema ──────────────────────────────────────────────────────────────────

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
            token_hash TEXT,
            token_issued_at TEXT,
            export_path TEXT,
            method_map TEXT,
            created_at TEXT,
            updated_at TEXT)""",
        """CREATE TABLE samples(
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
            status TEXT NOT NULL,
            backfill INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            current_revision INTEGER,
            released_at TEXT,
            released_by TEXT,
            qbench_revision INTEGER,
            qbench_uploaded_at TEXT,
            received_at TEXT NOT NULL,
            time_unverifiable INTEGER NOT NULL DEFAULT 0,
            review_note TEXT,
            UNIQUE(instrument_id, lab_id, injection_dt))""",
        "CREATE INDEX samples_status ON samples(status)",
        "CREATE INDEX samples_inst_dt ON samples(instrument_id, injection_dt)",
        "CREATE INDEX samples_dt ON samples(injection_dt)",
        "CREATE INDEX samples_lab ON samples(lab_id)",
        "CREATE INDEX samples_legacy ON samples(instrument_id, lab_id, legacy_injection_dt)",
        "CREATE INDEX samples_blanks ON samples(instrument_id, injection_dt) WHERE is_blank=1",
        "CREATE INDEX samples_methods ON samples(instrument_id, method_name)",
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
            notes TEXT,
            cdf_sha256 TEXT,
            cdf_path TEXT,
            PRIMARY KEY(sample_id, revision))""",
        "CREATE INDEX sample_results_sha ON sample_results(cdf_sha256)",
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
            resolved_at TEXT,
            error TEXT)""",
        "CREATE INDEX conflicts_sha ON conflicts(cdf_sha256)",
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
            created_at TEXT,
            sample_id INTEGER REFERENCES samples(id),
            finished_at TEXT)""",
        "CREATE INDEX jobs_state ON jobs(state, not_before, id)",
        "CREATE UNIQUE INDEX jobs_queued_sample ON jobs(kind, sample_id) WHERE state='queued'",
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
        """CREATE TABLE instrument_corrections(
            instrument_id TEXT NOT NULL REFERENCES instruments(id),
            cut TEXT NOT NULL,
            value REAL NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT,
            PRIMARY KEY(instrument_id, cut))""",
        """CREATE TABLE corrections_audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id TEXT NOT NULL,
            cut TEXT NOT NULL,
            old_value REAL,
            new_value REAL,
            changed_at TEXT NOT NULL,
            changed_by TEXT,
            reason TEXT NOT NULL)""",
        "CREATE INDEX corrections_audit_inst ON corrections_audit(instrument_id, id)",
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

# Tables and columns this code reads or writes (equal to a fresh v1 database).
# On a newer database these must exist; anything extra is ignored.
REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "instruments": frozenset({
        "id", "name", "method", "enabled", "live_since", "calibration_cdf",
        "calibration_assignments", "calibration_sensitivity", "lem_machine_uid",
        "token_hash", "token_issued_at", "export_path", "method_map", "created_at",
        "updated_at"}),
    "samples": frozenset({
        "id", "instrument_id", "lab_id", "injection_dt", "injection_dt_source",
        "method_name", "legacy_injection_dt", "time_corrected", "cdf_sha256", "cdf_path",
        "legacy_unverified", "source_name", "is_blank", "status", "backfill", "error",
        "current_revision", "released_at", "released_by", "qbench_revision",
        "qbench_uploaded_at", "received_at", "time_unverifiable", "review_note"}),
    "sample_results": frozenset({
        "sample_id", "revision", "results", "d86_uncorrected", "calibration_used",
        "blank_used", "corrections_used", "best_fit", "fit_score", "flags", "reason",
        "by", "processed_at", "notes", "cdf_sha256", "cdf_path"}),
    "conflicts": frozenset({
        "id", "instrument_id", "lab_id", "injection_dt", "existing_sample_id",
        "cdf_sha256", "cdf_path", "received_at", "resolved", "resolved_by", "resolved_at",
        "error"}),
    "export_rows": frozenset({
        "seq", "instrument_id", "sample_id", "revision", "row", "hub_appended_at"}),
    "jobs": frozenset({
        "id", "kind", "payload", "state", "attempts", "not_before", "last_error",
        "created_at", "sample_id", "finished_at"}),
    "agents": frozenset({
        "instrument_id", "version", "state", "queue_size", "rejected_count", "last_file",
        "last_error", "host", "agent_time", "last_seen", "results_seq", "pending_command"}),
    "instrument_corrections": frozenset({
        "instrument_id", "cut", "value", "updated_at", "updated_by"}),
    "corrections_audit": frozenset({
        "id", "instrument_id", "cut", "old_value", "new_value", "changed_at", "changed_by",
        "reason"}),
    "standards": frozenset({"id", "name", "instrument_id", "cdf_path", "added_at"}),
    "sample_cache": frozenset({
        "sample_id", "rules_fingerprint", "flags", "bestfit_fingerprint", "best_fit",
        "fit_score"}),
    "settings_kv": frozenset({"key", "value"}),
}


# ── time and encoding utilities ─────────────────────────────────────────────

def now_iso() -> str:
    """Current UTC time as the store's canonical timestamp string."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _ts(value: Union[None, str, datetime]) -> Optional[str]:
    """A tz-aware datetime or ISO string with an offset → canonical UTC string.

    Naive datetimes and offset-less strings raise ``ValueError``: a job time
    must never be guessed into UTC or local time.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
    if not isinstance(value, datetime):
        raise ValueError(f"expected a tz-aware datetime, got {value!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"naive time {value!r}: store timestamps must be tz-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def local_dt(value: Union[str, datetime, date]) -> str:
    """Normalise to the instrument-clock form ``YYYY-MM-DD HH:MM:SS`` (naive local).

    Accepts a naive ``datetime``, a ``date`` (midnight), or an ISO string with
    ``T`` or a space, or a bare date. Aware datetimes and strings with an
    offset or ``Z`` raise ``ValueError``: callers send naive local times.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            raise ValueError(f"{value!r} has a UTC marker; send a naive local time")
        value = datetime.fromisoformat(text)
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if not isinstance(value, datetime):
        raise ValueError(f"not a date/time: {value!r}")
    if value.tzinfo is not None:
        raise ValueError(f"{value!r} is tz-aware; send a naive local time")
    return value.isoformat(sep=" ")


def is_backfill(live_since: Union[None, str, datetime], injection_dt: str) -> bool:
    """D11: an injection before ``live_since`` (or with ``live_since`` unset) is backfill."""
    if live_since is None or (isinstance(live_since, str) and not live_since.strip()):
        return True
    return local_dt(injection_dt) < local_dt(live_since)


def _enc(value: Any) -> Any:
    """JSON-encode dicts/lists (and other non-scalars); pass str/numbers/None through."""
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    return json.dumps(value)


def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
    return None if r is None else dict(r)


def _rows(rs: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rs]


def _in(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def default_db_path() -> Path:
    """``paths.data_dir()/gc.db``. Raises ``RuntimeError`` without ``GC_DATA_DIR``."""
    d = paths.data_dir()
    if d is None:
        raise RuntimeError(f"{paths.DATA_ENV} is not set; the hub store needs a data folder")
    return d / DB_FILENAME


def _resolve(path: PathLike) -> Path:
    return default_db_path() if path is None else Path(path)


# ── connections and transactions ────────────────────────────────────────────

_local = threading.local()


def _txn_depth() -> int:
    return getattr(_local, "txn_depth", 0)


def open_db(path: PathLike = None, *, create: bool = False, readonly: bool = False) -> sqlite3.Connection:
    """Open a short-lived connection with the hub's pragmas.

    The file must already exist (``mode=rw``) unless ``create=True``
    (``migrate`` only); ``readonly=True`` opens ``mode=ro`` (backups).
    Autocommit mode: nothing is in a transaction unless ``write_txn`` (or a
    helper) begins one. The caller must close it; prefer ``with connection()``.
    Raises ``RuntimeError`` if this thread holds a write transaction.
    """
    if _txn_depth():
        raise RuntimeError("this thread holds a write transaction; "
                           "pass db=conn inside write_txn instead of opening a new connection")
    p = _resolve(path).resolve()
    if create:
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "rwc"
    else:
        mode = "ro" if readonly else "rw"
    conn = sqlite3.connect(f"{p.as_uri()}?mode={mode}", uri=True,
                           timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if not readonly:
            mode_now = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode_now).lower() != "wal":
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


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``.

    Any exception in the block, or a failed ``COMMIT`` (e.g. a deferred
    foreign key), rolls back and re-raises. Nested use (the connection is
    already in a transaction) opens a SAVEPOINT, rolled back on its own if
    the inner block raises. While the outer transaction is open, this thread
    may not open another connection (see ``open_db``).
    """
    if conn.in_transaction:
        _local.sp = getattr(_local, "sp", 0) + 1
        name = f"sp_{_local.sp}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute(f"ROLLBACK TO {name}")
                conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    _local.txn_depth = _txn_depth() + 1
    try:
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        try:
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        _local.txn_depth = _txn_depth() - 1


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
        missing += [f"{table}.{c}" for c in sorted(cols - have)]
    if missing:
        raise SchemaError("database is missing: " + ", ".join(missing))


def migrate(path: PathLike = None) -> int:
    """Create or upgrade the database to ``len(MIGRATIONS)``; return ``user_version``.

    Additive only. If the file existed and a step is pending, it is first
    copied to ``backups/pre-migrate-<v>-<ts>.db``. Each step runs in one
    ``BEGIN IMMEDIATE`` transaction with its ``user_version`` bump,
    re-reading the version under the lock so two processes can't apply a
    step twice. A newer ``user_version`` is logged and tolerated.
    """
    db_path = _resolve(path)
    target = len(MIGRATIONS)
    existed = db_path.exists() and db_path.stat().st_size > 0
    conn = open_db(db_path, create=True)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > target:
            log.warning("store: database user_version %s is newer than this code's %s; "
                        "carrying on (additive migrations)", version, target)
        elif version < target:
            if existed:
                _pre_migrate_backup(conn, db_path, version)
            while True:
                with write_txn(conn):
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if version >= target:
                        break
                    for stmt in MIGRATIONS[version]:
                        conn.execute(stmt)
                    conn.execute(f"PRAGMA user_version = {version + 1}")
                log.info("store: migrated to schema v%s", version + 1)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        _check_columns(conn)
        return version
    finally:
        conn.close()


# ── backups ─────────────────────────────────────────────────────────────────

_NIGHTLY_RE = re.compile(r"^gc-(\d{4}-\d{2}-\d{2})\.db$")


def backup_nightly(path: PathLike = None, keep: int = 14, *, settings_path: PathLike = None,
                   now: Optional[datetime] = None) -> Path:
    """``VACUUM INTO backups/gc-<date>.db`` plus a copy of ``settings.json``; prune to ``keep``.

    ``settings_path`` defaults to ``paths.settings_file()``. ``now`` (local
    date) is injectable for tests. Returns the backup path.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1")
    db_path = _resolve(path)
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    backups = db_path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / f"gc-{day}.db"
    tmp = backups / f".gc-{day}.db.tmp"
    if tmp.exists():
        tmp.unlink()
    conn = open_db(db_path, readonly=True)
    try:
        conn.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        conn.close()
    os.replace(tmp, dest)

    settings = Path(settings_path) if settings_path is not None else paths.settings_file()
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

_FROM_SAMPLE = object()     # add_revision: record the sample's current CDF

def add_revision(conn: sqlite3.Connection, sample_id: int, results: Any, *, reason: str,
                 by: Optional[str] = None, d86_uncorrected: Any = None,
                 calibration_used: Any = None, blank_used: Optional[int] = None,
                 corrections_used: Any = None, best_fit: Optional[str] = None,
                 fit_score: Optional[float] = None, flags: Any = None,
                 processed_at: Optional[str] = None, notes: Any = None,
                 cdf_sha256: Any = _FROM_SAMPLE, cdf_path: Any = _FROM_SAMPLE) -> int:
    """Write the next ``sample_results`` revision and make it current.

    Must run inside ``write_txn(conn)``, so the revision, the export row and
    the status change commit together. Returns the new revision number
    (1 for the first). The spec's reasons are in ``REVISION_REASONS``.
    ``notes`` is structured JSON about how the revision was computed (e.g.
    ``{"blank_rejected": {"sample_id", "reason"}}``), ``None`` when there is
    nothing to say. ``cdf_sha256``/``cdf_path`` record the CDF that produced
    the revision; by default the sample's current file (both NULL for a
    result-only import). A caller that computed from another file (a conflict
    Replace) passes it.
    """
    _require_txn(conn, "add_revision")
    if cdf_sha256 is _FROM_SAMPLE or cdf_path is _FROM_SAMPLE:
        cur = conn.execute("SELECT cdf_sha256, cdf_path FROM samples WHERE id=?",
                           (sample_id,)).fetchone()
        if cdf_sha256 is _FROM_SAMPLE:
            cdf_sha256 = cur[0] if cur is not None else None
        if cdf_path is _FROM_SAMPLE:
            cdf_path = cur[1] if cur is not None else None
    rev = conn.execute("SELECT COALESCE(MAX(revision), 0) + 1 FROM sample_results WHERE sample_id=?",
                       (sample_id,)).fetchone()[0]
    conn.execute(
        'INSERT INTO sample_results(sample_id, revision, results, d86_uncorrected, '
        'calibration_used, blank_used, corrections_used, best_fit, fit_score, flags, '
        'reason, "by", processed_at, notes, cdf_sha256, cdf_path) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (sample_id, rev, _enc(results), _enc(d86_uncorrected), _enc(calibration_used),
         blank_used, _enc(corrections_used), best_fit, fit_score, _enc(flags), reason, by,
         processed_at or now_iso(), _enc(notes), cdf_sha256, cdf_path))
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
        are maintained here; ``live_since`` is normalised with ``local_dt``.
        Unknown keys raise ``ValueError``.
        """
        fields = dict(fields)
        iid = fields.pop("id", None)
        if not iid:
            raise ValueError("instrument 'id' is required")
        unknown = set(fields) - (instruments.COLUMNS - {"id"})
        if unknown:
            raise ValueError(f"unknown instrument columns: {sorted(unknown)}")
        if fields.get("live_since") is not None:
            fields["live_since"] = local_dt(fields["live_since"])
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
                conn.execute(f"INSERT INTO instruments({', '.join(cols)}) VALUES ({_in(cols)})",
                             [iid] + [_enc(fields[c]) for c in cols[1:]])
            return dict(conn.execute("SELECT * FROM instruments WHERE id=?", (iid,)).fetchone())


# ── corrections (D4b) ───────────────────────────────────────────────────────

class corrections:  # noqa: N801
    """Per-instrument D86 correction factors and their append-only audit."""

    @staticmethod
    def read(instrument_id: str, *, db: Db = None) -> Optional[dict]:
        """``{values: {cut: float}, updated_at, updated_by}`` or ``None`` if none are set.

        ``updated_at`` is the latest change; ``updated_by`` is who made it.
        This is ``corrections.StoreProvider``'s ``read_fn``.
        """
        with connection(db) as conn:
            rows = _rows(conn.execute(
                "SELECT cut, value, updated_at, updated_by FROM instrument_corrections "
                "WHERE instrument_id=?", (instrument_id,)))
        if not rows:
            return None
        latest = max(rows, key=lambda r: r["updated_at"])
        return {"values": {r["cut"]: r["value"] for r in rows},
                "updated_at": latest["updated_at"], "updated_by": latest["updated_by"]}

    @staticmethod
    def set_all(conn: sqlite3.Connection, instrument_id: str, values: dict, *, by: Optional[str],
                reason: str) -> int:
        """Write each **changed** cut in ``values`` (one timestamp) and audit it.

        Unchanged cuts are not touched (no ``updated_at``/``updated_by``
        bump, no audit row). Must run inside ``write_txn(conn)``. ``reason``
        is required. ``None``, non-numeric and non-finite values raise
        ``ValueError``. Returns how many cuts changed (a first-time cut
        counts, with ``old_value`` NULL in the audit).
        """
        _require_txn(conn, "corrections.set_all")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a reason is required to change correction factors")
        if not values:
            raise ValueError("no correction values given")
        clean = {}
        for cut, v in values.items():
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                raise ValueError(f"correction for {cut!r} is not a number: {v!r}")
            try:
                f = float(v)
            except ValueError:
                raise ValueError(f"correction for {cut!r} is not a number: {v!r}") from None
            if not math.isfinite(f):
                raise ValueError(f"correction for {cut!r} is not a finite number: {v!r}")
            clean[str(cut)] = f
        stamp = now_iso()
        old = {r["cut"]: r["value"] for r in conn.execute(
            "SELECT cut, value FROM instrument_corrections WHERE instrument_id=?", (instrument_id,))}
        changed = 0
        for cut, new in clean.items():
            prev = old.get(cut)
            if prev is not None and prev == new:
                continue  # unchanged: no updated_at/updated_by bump, no audit row
            conn.execute(
                "INSERT INTO instrument_corrections(instrument_id, cut, value, updated_at, updated_by) "
                "VALUES (?,?,?,?,?) ON CONFLICT(instrument_id, cut) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (instrument_id, cut, new, stamp, by))
            conn.execute(
                "INSERT INTO corrections_audit(instrument_id, cut, old_value, new_value, "
                "changed_at, changed_by, reason) VALUES (?,?,?,?,?,?,?)",
                (instrument_id, cut, prev, new, stamp, by, reason.strip()))
            changed += 1
        return changed

    @staticmethod
    def audit(instrument_id: str, limit: int = 100, *, db: Db = None) -> list[dict]:
        """The audit trail, newest first."""
        with connection(db) as conn:
            return _rows(conn.execute(
                "SELECT * FROM corrections_audit WHERE instrument_id=? ORDER BY id DESC LIMIT ?",
                (instrument_id, int(limit))))


# ── samples ─────────────────────────────────────────────────────────────────

def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _listify(v: Union[str, Sequence[Any]]) -> list:
    return [v] if isinstance(v, str) else list(v)


def _search_where(q, instrument, date_from, date_to, status, method_name, backfill,
                  time_unverifiable=None) -> tuple[str, list]:
    where, args = [], []
    if q:
        pat = f"%{_like_escape(str(q).strip())}%"
        where.append("(lab_id LIKE ? ESCAPE '\\' OR source_name LIKE ? ESCAPE '\\')")
        args += [pat, pat]
    if instrument:
        inst = _listify(instrument)
        where.append(f"instrument_id IN ({_in(inst)})")
        args += inst
    if date_from:
        where.append("injection_dt >= ?")
        args.append(local_dt(date_from))
    if date_to:
        if isinstance(date_to, str) and re.fullmatch(r"\s*\d{4}-\d{2}-\d{2}\s*", date_to):
            # a bare date is inclusive of that whole day
            where.append("injection_dt < ?")
            args.append(local_dt(date.fromisoformat(date_to.strip()) + timedelta(days=1)))
        elif isinstance(date_to, date) and not isinstance(date_to, datetime):
            where.append("injection_dt < ?")
            args.append(local_dt(date_to + timedelta(days=1)))
        else:
            where.append("injection_dt <= ?")
            args.append(local_dt(date_to))
    if status:
        st = _listify(status)
        where.append(f"status IN ({_in(st)})")
        args += st
    if method_name is not None:
        mn = _listify(method_name)
        where.append(f"method_name IN ({_in(mn)})")
        args += mn
    if backfill is not None:
        where.append("backfill = ?")
        args.append(1 if backfill else 0)
    if time_unverifiable is not None:
        where.append("time_unverifiable = ?")
        args.append(1 if time_unverifiable else 0)
    return (" WHERE " + " AND ".join(where)) if where else "", args


def _check_status(status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown sample status {status!r}")


def _check_dt_source(source: str) -> None:
    if source not in INJECTION_DT_SOURCES:
        raise ValueError(f"unknown injection_dt_source {source!r}")


class samples:  # noqa: N801
    """The ``samples`` table."""

    UPDATABLE = frozenset(REQUIRED_COLUMNS["samples"]
                          - {"id", "instrument_id", "received_at", "current_revision"})

    @staticmethod
    def insert_received(instrument_id: str, lab_id: str, injection_dt: str,
                        injection_dt_source: str, *, cdf_sha256: Optional[str],
                        cdf_path: Optional[str], method_name: Optional[str] = None,
                        source_name: Optional[str] = None, is_blank: int = 0,
                        backfill: int = 0, status: str = "received",
                        legacy_injection_dt: Optional[str] = None, time_corrected: int = 0,
                        legacy_unverified: int = 0, received_at: Optional[str] = None,
                        time_unverifiable: int = 0, db: Db = None) -> int:
        """Insert a new sample (status ``received`` by default); return its id.

        ``ValueError`` for a status outside ``STATUSES`` or a source outside
        ``INJECTION_DT_SOURCES``. ``sqlite3.IntegrityError`` on a duplicate
        sha256 (any instrument), a duplicate (instrument, lab ID, injection
        time) or an unknown instrument. A CDF-backed sample with
        ``method_name=None`` is stored with ``''``.
        """
        _check_status(status)
        _check_dt_source(injection_dt_source)
        if method_name is None and (cdf_sha256 is not None or cdf_path is not None):
            method_name = ""
        with _writing(db) as conn:
            cur = conn.execute(
                "INSERT INTO samples(instrument_id, lab_id, injection_dt, injection_dt_source, "
                "method_name, legacy_injection_dt, time_corrected, cdf_sha256, cdf_path, "
                "legacy_unverified, source_name, is_blank, status, backfill, received_at, "
                "time_unverifiable) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (instrument_id, lab_id, injection_dt, injection_dt_source, method_name,
                 legacy_injection_dt, int(time_corrected), cdf_sha256, cdf_path,
                 int(legacy_unverified), source_name, int(is_blank), status, int(backfill),
                 received_at or now_iso(), int(time_unverifiable)))
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
    def find_by_legacy(instrument_id: str, lab_id: str, dt: str, *, db: Db = None) -> list[dict]:
        """Samples whose correct **or** v1 (legacy) injection time equals ``dt``.

        For matching v1 CSV rows (parity report, importer). Can return more
        than one sample (one's correct time may equal another's legacy
        string); legacy matches come first, then by id.
        """
        with connection(db) as conn:
            return _rows(conn.execute(
                "SELECT * FROM samples WHERE instrument_id=? AND lab_id=? "
                "AND (legacy_injection_dt=? OR injection_dt=?) "
                "ORDER BY (legacy_injection_dt IS ?) DESC, id",
                (instrument_id, lab_id, dt, dt, dt)))

    @staticmethod
    def latest_blank(instrument_id: str, at: str, method_names: Sequence[str], *,
                     exclude_sample_id: Optional[int] = None, db: Db = None) -> Optional[dict]:
        """The latest genuine blank (``is_blank=1``, with a stored CDF) on this
        instrument injected at or before ``at``, whose ``method_name`` is one of
        ``method_names`` (the names mapped to the sample's hub method). Blanks
        injected at the same time are ordered by ``cdf_sha256`` (the file's
        content), so the choice never depends on arrival order."""
        names = list(method_names)
        if not names:
            return None
        sql = (f"SELECT * FROM samples WHERE instrument_id=? AND is_blank=1 AND injection_dt<=? "
               f"AND cdf_path IS NOT NULL AND method_name IN ({_in(names)})")
        args: list = [instrument_id, local_dt(at)] + names
        if exclude_sample_id is not None:
            sql += " AND id<>?"
            args.append(exclude_sample_id)
        with connection(db) as conn:
            return _row(conn.execute(sql + " ORDER BY injection_dt DESC, cdf_sha256 DESC LIMIT 1", args).fetchone())

    @staticmethod
    def is_gated(sample_id: int, *, db: Db = None) -> bool:
        """True if the sample passes the export/QBench gate (``GATE_SQL``)."""
        with connection(db) as conn:
            return conn.execute(f"SELECT 1 FROM samples WHERE id=? AND {GATE_SQL}",
                                (sample_id,)).fetchone() is not None

    @staticmethod
    def methods_seen(instrument_id: str, *, db: Db = None) -> list[dict]:
        """``[{method_name, count, first_seen, last_seen}]`` by injection time, for
        the Instruments page. Result-only samples (NULL method) are left out."""
        with connection(db) as conn:
            return _rows(conn.execute(
                "SELECT method_name, COUNT(*) AS count, MIN(injection_dt) AS first_seen, "
                "MAX(injection_dt) AS last_seen FROM samples "
                "WHERE instrument_id=? AND method_name IS NOT NULL "
                "GROUP BY method_name ORDER BY method_name", (instrument_id,)))

    @staticmethod
    def set_status(sample_id: int, status: str, *, error: Optional[str] = None, db: Db = None) -> None:
        """Set ``status`` and ``error`` (cleared unless given). ``ValueError`` on an unknown status."""
        _check_status(status)
        with _writing(db) as conn:
            conn.execute("UPDATE samples SET status=?, error=? WHERE id=?", (status, error, sample_id))

    @staticmethod
    def update(sample_id: int, *, db: Db = None, **fields: Any) -> None:
        """Update whitelisted columns (e.g. ``cdf_path``, ``released_at``/``released_by``,
        ``qbench_revision``/``qbench_uploaded_at``, ``backfill``, ``is_blank``).
        ``id``, ``instrument_id``, ``received_at`` and ``current_revision``
        (only ``add_revision`` sets it) raise ``ValueError``."""
        if not fields:
            return
        bad = set(fields) - samples.UPDATABLE
        if bad:
            raise ValueError(f"cannot update sample columns: {sorted(bad)}")
        if "status" in fields:
            _check_status(fields["status"])
        if "injection_dt_source" in fields:
            _check_dt_source(fields["injection_dt_source"])
        cols = sorted(fields)
        with _writing(db) as conn:
            conn.execute(f"UPDATE samples SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",
                         [_enc(fields[c]) for c in cols] + [sample_id])

    @staticmethod
    def search(q: Optional[str] = None, instrument: Union[None, str, Sequence[str]] = None,
               date_from: Union[None, str, datetime, date] = None,
               date_to: Union[None, str, datetime, date] = None,
               status: Union[None, str, Sequence[str]] = None, limit: int = 100,
               offset: int = 0, *, method_name: Union[None, str, Sequence[str]] = None,
               backfill: Optional[bool] = None, time_unverifiable: Optional[bool] = None,
               db: Db = None) -> list[dict]:
        """Filter samples, newest injection first (ties: newest id first).

        ``q``: case-insensitive substring of ``lab_id`` or ``source_name``
        (LIKE wildcards are literal). ``instrument``/``status``/``method_name``:
        one value or a list. ``date_from``/``date_to`` are normalised with
        ``local_dt`` and compared with ``injection_dt``; a bare date
        ``date_to`` includes that whole day. ``backfill``: True/False/None.
        """
        where, args = _search_where(q, instrument, date_from, date_to, status, method_name, backfill,
                                    time_unverifiable)
        with connection(db) as conn:
            return _rows(conn.execute(
                f"SELECT * FROM samples{where} ORDER BY injection_dt DESC, id DESC LIMIT ? OFFSET ?",
                args + [int(limit), int(offset)]))

    @staticmethod
    def count(q: Optional[str] = None, instrument: Union[None, str, Sequence[str]] = None,
              date_from: Union[None, str, datetime, date] = None,
              date_to: Union[None, str, datetime, date] = None,
              status: Union[None, str, Sequence[str]] = None, *,
              method_name: Union[None, str, Sequence[str]] = None,
              backfill: Optional[bool] = None, time_unverifiable: Optional[bool] = None,
              db: Db = None) -> int:
        """How many samples ``search`` would match without paging."""
        where, args = _search_where(q, instrument, date_from, date_to, status, method_name, backfill,
                                    time_unverifiable)
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
        ``ValueError`` if the sample is missing or belongs to another instrument.
        """
        _require_txn(conn, "export_rows.append_pending")
        r = conn.execute("SELECT instrument_id FROM samples WHERE id=?", (sample_id,)).fetchone()
        if r is None:
            raise ValueError(f"sample {sample_id} does not exist")
        if r[0] != instrument_id:
            raise ValueError(f"sample {sample_id} belongs to {r[0]!r}, not {instrument_id!r}")
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
        """Stamp one ``seq`` or several as appended; an existing stamp is kept."""
        seqs = [seq] if isinstance(seq, int) else list(seq)
        if not seqs:
            return
        stamp = at or now_iso()
        with _writing(db) as conn:
            conn.executemany("UPDATE export_rows SET hub_appended_at=? "
                             "WHERE seq=? AND hub_appended_at IS NULL",
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
        try:
            d["payload"] = json.loads(d["payload"])
        except ValueError:
            pass  # a poison payload stays TEXT; claim_next fails such jobs
    return d


_EARLIER_NOT_BEFORE = ("CASE WHEN excluded.not_before IS NULL OR jobs.not_before IS NULL THEN NULL "
                       "WHEN excluded.not_before < jobs.not_before THEN excluded.not_before "
                       "ELSE jobs.not_before END")


class jobs:  # noqa: N801
    """Durable job queue. States: ``queued`` → ``running`` → ``done`` | ``failed``
    (| ``superseded``, see the module docstring)."""

    @staticmethod
    def enqueue(kind: str, payload: Any, not_before: Union[None, str, datetime] = None, *,
                sample_id: Optional[int] = None, db: Db = None) -> int:
        """Queue a job; ``payload`` is JSON-encoded. Returns the job id.

        With ``sample_id``, at most one job per ``(kind, sample_id)`` is
        queued: a duplicate returns the queued job's id, **replaces its
        payload** (the last request's intent wins, e.g. a reprocess with
        ``by``/``use_current_blank``) and keeps the **earlier** ``not_before``
        of the two (NULL = now).
        """
        nb = _ts(not_before)
        body = json.dumps(payload)
        with _writing(db) as conn:
            if sample_id is None:
                cur = conn.execute(
                    "INSERT INTO jobs(kind, payload, state, attempts, not_before, created_at) "
                    "VALUES (?, ?, 'queued', 0, ?, ?)", (kind, body, nb, now_iso()))
                return int(cur.lastrowid)
            conn.execute(
                "INSERT INTO jobs(kind, payload, state, attempts, not_before, created_at, sample_id) "
                "VALUES (?, ?, 'queued', 0, ?, ?, ?) "
                "ON CONFLICT(kind, sample_id) WHERE state='queued' DO UPDATE SET "
                f"payload=excluded.payload, not_before={_EARLIER_NOT_BEFORE}",
                (kind, body, nb, now_iso(), sample_id))
            return int(conn.execute("SELECT id FROM jobs WHERE kind=? AND sample_id=? AND state='queued'",
                                    (kind, sample_id)).fetchone()[0])

    @staticmethod
    def enqueue_for_status(instrument_id: str, status: str, *, method_name: Optional[str] = None,
                           kind: str = "process", db: Db = None) -> int:
        """Queue ``kind`` (payload ``{"sample_id": id}``, due now) for every sample of
        this instrument in ``status`` (and ``method_name``, if given), in one
        statement. Samples that already have a queued job keep it (and its
        payload), brought forward to now. Returns the number of samples now
        queued."""
        _check_status(status)
        sql = ("INSERT INTO jobs(kind, payload, state, attempts, not_before, created_at, sample_id) "
               "SELECT ?, json_object('sample_id', id), 'queued', 0, NULL, ?, id FROM samples "
               "WHERE instrument_id=? AND status=?")
        args: list = [kind, now_iso(), instrument_id, status]
        if method_name is not None:
            sql += " AND method_name=?"
            args.append(method_name)
        sql += (" ORDER BY id ON CONFLICT(kind, sample_id) WHERE state='queued' "
                "DO UPDATE SET not_before=NULL")
        with _writing(db) as conn:
            return conn.execute(sql, args).rowcount

    @staticmethod
    def claim_next(now: Union[None, str, datetime] = None, *, kind: Optional[str] = None,
                   db: Db = None) -> Optional[dict]:
        """Atomically take the oldest due ``queued`` job (``not_before`` NULL or ≤ ``now``).

        Marks it ``running`` and increments ``attempts``; returns it with
        ``payload`` decoded, or ``None``. A job whose payload isn't JSON is
        marked ``failed`` and skipped. ``BEGIN IMMEDIATE`` makes the
        select-and-update exclusive, so two workers never claim one job.
        """
        stamp = _ts(now) or now_iso()
        sql = ("SELECT * FROM jobs WHERE state='queued' AND (not_before IS NULL OR not_before <= ?)"
               + (" AND kind=?" if kind else "") + " ORDER BY id LIMIT 1")
        args = [stamp] + ([kind] if kind else [])
        with _writing(db) as conn:
            while True:
                r = conn.execute(sql, args).fetchone()
                if r is None:
                    return None
                try:
                    payload = json.loads(r["payload"]) if r["payload"] is not None else None
                except (ValueError, TypeError) as exc:
                    conn.execute("UPDATE jobs SET state='failed', last_error=?, finished_at=? WHERE id=?",
                                 (f"undecodable payload: {exc}", now_iso(), r["id"]))
                    log.error("store: job %s has an undecodable payload; marked failed", r["id"])
                    continue
                conn.execute("UPDATE jobs SET state='running', attempts=COALESCE(attempts, 0) + 1 "
                             "WHERE id=?", (r["id"],))
                job = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (r["id"],)).fetchone())
                job["payload"] = payload
                return job

    @staticmethod
    def complete(job_id: int, *, db: Db = None) -> None:
        with _writing(db) as conn:
            conn.execute("UPDATE jobs SET state='done', finished_at=? WHERE id=?", (now_iso(), job_id))

    @staticmethod
    def fail(job_id: int, error: Optional[str], retry_at: Union[None, str, datetime] = None, *,
             db: Db = None) -> None:
        """Record ``error``. With ``retry_at`` the job is queued again for then (or, if
        a twin is already queued for the same sample, it is ``superseded`` and the
        twin brought forward); without, it is ``failed``."""
        retry = _ts(retry_at)
        with _writing(db) as conn:
            if retry is None:
                conn.execute("UPDATE jobs SET state='failed', last_error=?, finished_at=? WHERE id=?",
                             (error, now_iso(), job_id))
                return
            me = conn.execute("SELECT kind, sample_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            twin = None
            if me is not None and me["sample_id"] is not None:
                twin = conn.execute("SELECT id, not_before FROM jobs WHERE kind=? AND sample_id=? "
                                    "AND state='queued' AND id<>?",
                                    (me["kind"], me["sample_id"], job_id)).fetchone()
            if twin is None:
                conn.execute("UPDATE jobs SET state='queued', not_before=?, last_error=? WHERE id=?",
                             (retry, error, job_id))
                return
            if twin["not_before"] is not None and retry < twin["not_before"]:
                conn.execute("UPDATE jobs SET not_before=? WHERE id=?", (retry, twin["id"]))
            conn.execute("UPDATE jobs SET state='superseded', last_error=?, finished_at=? WHERE id=?",
                         (error, now_iso(), job_id))

    @staticmethod
    def requeue_stale_running(*, db: Db = None) -> int:
        """Put every ``running`` job back to ``queued`` (start-up only); return how many
        were running. A running job whose ``(kind, sample_id)`` already has a queued
        twin (or an older running twin) is ``superseded`` instead, and the queued
        twin becomes due now."""
        with _writing(db) as conn:
            n = conn.execute("SELECT COUNT(*) FROM jobs WHERE state='running'").fetchone()[0]
            conn.execute(
                "UPDATE jobs SET not_before=NULL WHERE state='queued' AND sample_id IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM jobs r WHERE r.state='running' AND r.kind=jobs.kind "
                "AND r.sample_id=jobs.sample_id)")
            conn.execute(
                "UPDATE jobs SET state='superseded', finished_at=? WHERE state='running' "
                "AND sample_id IS NOT NULL AND EXISTS (SELECT 1 FROM jobs o WHERE o.kind=jobs.kind "
                "AND o.sample_id=jobs.sample_id AND (o.state='queued' OR "
                "(o.state='running' AND o.id<jobs.id)))", (now_iso(),))
            conn.execute("UPDATE jobs SET state='queued' WHERE state='running'")
            return int(n)

    @staticmethod
    def prune_done(older_than: Union[str, datetime], *, db: Db = None) -> int:
        """Delete ``done``/``superseded`` jobs finished before ``older_than`` (tz-aware)."""
        cutoff = _ts(older_than)
        if cutoff is None:
            raise ValueError("older_than is required")
        with _writing(db) as conn:
            return conn.execute("DELETE FROM jobs WHERE state IN ('done', 'superseded') "
                                "AND finished_at IS NOT NULL AND finished_at < ?", (cutoff,)).rowcount

    @staticmethod
    def get(job_id: int, *, db: Db = None) -> Optional[dict]:
        with connection(db) as conn:
            return _job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    @staticmethod
    def list(state: Optional[str] = None, kind: Optional[str] = None, *, db: Db = None) -> list[dict]:
        """Jobs, oldest first, optionally filtered by state and kind."""
        where, args = [], []
        if state:
            where.append("state=?")
            args.append(state)
        if kind:
            where.append("kind=?")
            args.append(kind)
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        with connection(db) as conn:
            return [_job(r) for r in conn.execute(sql, args)]


# ── sample_cache, settings_kv, conflicts ────────────────────────────────────

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
    """Hub-wide key/value settings (admin hash, etc.). Values are TEXT."""

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
    def get(conflict_id: int, *, db: Db = None) -> Optional[dict]:
        with connection(db) as conn:
            return _row(conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone())

    @staticmethod
    def set_error(conflict_id: int, error: Optional[str], *, db: Db = None) -> None:
        """Record (or clear, with ``None``) why the last Replace attempt failed.
        ``ValueError`` if the conflict is missing."""
        with _writing(db) as conn:
            if conn.execute("UPDATE conflicts SET error=? WHERE id=?",
                            (error, conflict_id)).rowcount != 1:
                raise ValueError(f"conflict {conflict_id} is missing")

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
    def find_by_sha(cdf_sha256: str, unresolved_only: bool = True, *, db: Db = None) -> Optional[dict]:
        """The newest conflict holding this CDF (a re-sent conflicting file dedupes on it)."""
        sql = "SELECT * FROM conflicts WHERE cdf_sha256=?"
        if unresolved_only:
            sql += " AND resolved IS NULL"
        with connection(db) as conn:
            return _row(conn.execute(sql + " ORDER BY id DESC LIMIT 1", (cdf_sha256,)).fetchone())

    @staticmethod
    def resolve(conflict_id: int, resolution: str, *, by: str, db: Db = None) -> None:
        """Record the admin's choice. ``ValueError`` if the resolution is unknown or
        the conflict is missing or already resolved. (Replacing the sample's CDF
        is the caller's job, in the same ``write_txn`` if it passes ``db=conn``.)"""
        if resolution not in CONFLICT_RESOLUTIONS:
            raise ValueError(f"unknown conflict resolution {resolution!r}")
        with _writing(db) as conn:
            n = conn.execute("UPDATE conflicts SET resolved=?, resolved_by=?, resolved_at=?, error=NULL "
                             "WHERE id=? AND resolved IS NULL",
                             (resolution, by, now_iso(), conflict_id)).rowcount
            if n != 1:
                raise ValueError(f"conflict {conflict_id} is missing or already resolved")
