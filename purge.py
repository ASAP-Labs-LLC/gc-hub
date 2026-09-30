"""purge.py: purge one instrument's samples (v3.1; spec "Purge instrument data").

"Purge only GC-1's samples, and keep GC-2 and the settings." A sample spans
``samples`` and every table that references it; hand-editing ``gc.db`` is
unsafe, so this is the one way to do it. It runs as an admin job
(``hub_admin``: ``POST /api/admin/purge/preview|start``) and in tests
in-process.

API::

    preview(instrument_id, scope, *, db=None, data_dir=None, exporter=None) -> dict
        # read-only: {ok, problems, unhandled, fk_problems, instrument, instrument_name,
        #  scope, confirm_text, samples, tables: {table: rows}, appended, appended_rows,
        #  kept_samples: [{id, lab_id, reason}], kept_files: [{path, reason}],
        #  kept_files_total, files: {move, bytes, missing, outside}, results_path, warning}
    run(instrument_id, scope, *, confirm_text, by, db=None, data_dir=None, exporter=None,
        notifier=None, new_results_path=None, progress=None, wait_seconds=600,
        poll_seconds=0.5) -> summary dict
    recover(*, db=None, data_dir=None, notifier=None, exporter=None) -> [summary]
        # hub.start, before the exporter: finish or abandon interrupted purges
    latest_journal(data_dir) -> summary | None      # the newest purge's journal
    confirmation_text(instrument_row, *, db=None) -> "PURGE <name>" ("PURGE <name> (<id>)"
                                                     # when another instrument has that name)
    sample_references(conn) -> {(table, column, parent)}   # from the live schema
    unhandled_references(conn) -> [(table, column, parent)]  # not in KNOWN_REFERENCES
    check_new_results_path(raw, instrument_id, *, db, data_dir, exporter=None) -> Path
    PurgeError (status 500), PurgeRefused(message, status=400|404|409)

``scope`` is ``all`` (every sample of the instrument, and its conflicts that
have no existing sample) or ``backfill`` (only ``backfill=1`` samples, i.e.
imported history, to redo an import).

**What ``run`` does, in order** (refusals first, so a refused purge changes
nothing):

1. The confirmation must be exactly ``confirmation_text``; a
   ``new_results_path`` must be an absolute ``.csv`` path in an existing folder
   and not another instrument's results file (as ``exports new-path``). The
   schema must have no table referencing samples that this module does not
   handle, and ``PRAGMA foreign_key_check`` must already be clean.
2. The instrument is paused (``pipeline.pause_instrument``): the Worker claims
   none of its jobs and ingest answers 503 "purge in progress; retry" (the
   agent backs off and resends). Running jobs of the instrument are waited for
   (``wait_seconds``, then ``PurgeError``), never while holding a lock. Other
   instruments carry on throughout. The pause is always lifted at the end.
   Operator actions on that instrument's samples (reprocess, export, reports)
   are not paused: they may act on samples that are about to be removed.
3. Nothing to purge: return (``nothing_to_do``), no backup, no files.
4. **The journal**: ``<data>/purged/<inst>-<stamp>/manifest.json``, written
   atomically, ``state: pending`` with the backup path and the planned sample
   ids. Then ``VACUUM INTO backups/pre-purge-<inst>-<stamp>.db``, checked with
   ``PRAGMA integrity_check``. The nightly pruning only ever deletes
   ``gc-<date>.db``, so this is never pruned.
5. ONE write transaction, holding the exporter's lock for the instrument (no
   flush sees it half done): the purge set is computed under the lock (a
   sample that a kept row still points at, e.g. the blank a kept result
   subtracted, or another instrument's conflict, is kept, with its file, and
   listed in ``kept_samples``); every row owned by a purged sample is deleted
   (``OWNER_COLUMNS``, children first; conflicts only the instrument's own),
   then the samples; each delete must remove exactly what was counted, no row
   may still point at a purged id and ``PRAGMA foreign_key_check`` must be
   clean, else it all rolls back. The journal is rewritten (still ``pending``)
   with the final sample ids and file moves before the ``COMMIT``, and set to
   ``committed`` right after it. Then the results CSV's sidecar is unlinked
   from the purged ledger rows (``HubExporter.after_purge``, idempotent: the
   CSV is append-only and never edited; without this the next append would
   refuse ``ledger-mismatch``).
6. The purged samples' CDFs (the sample's file, the files its revisions
   recorded, its conflicts' held files) are MOVED to
   ``<data>/purged/<inst>-<stamp>/`` with the same layout relative to the data
   folder, never deleted. A file anything kept still references is never
   moved: any instrument's calibration CDF, ``settings.json``'s, the
   calibration a kept revision was computed with (``calibration_used.cdf``), a
   comparison standard, or a kept sample, revision or conflict. Only files
   under ``<data>/cdf/`` are ever moved. Each move is idempotent (the file
   system says what is done), so an interrupted move is simply finished.
7. ``new_results_path`` is applied through ``HubExporter.new_path``; an
   ``instrument_events`` row is written when that table exists (schema v4,
   another v3.1 branch); a notification is raised; ``live`` events are
   published (the instrument and the purged sample ids); the journal becomes
   ``done``.
   A failure in any step after the commit never fails the purge: it is
   ``completed_with_warnings`` and the warnings are in the summary and the
   notification.

**A failure before the commit** changes nothing: the journal becomes
``abandoned``, its backup is deleted (so failed purges don't pile up backups)
and a notification says nothing was removed. **A crash** (the process killed)
is repaired by ``recover`` at the next start, before the exporter runs: a
``pending`` journal whose samples all still exist is abandoned the same way; a
``committed`` one (or a ``pending`` one whose samples are all gone: the crash
fell between the ``COMMIT`` and the journal update) is finished: sidecar,
remaining moves, new results path, event and a notification "finished after a
restart". Every step is idempotent, so a crash during recovery is recovered
at the next start too.

Kept always: the instrument row and its settings (calibration, corrections,
``method_map``, ``lem_machine_uid``, ``live_since``, ``export_path``, agent
and token), other instruments' data, global settings and presets,
``import_runs`` and the corrections audit.

Restore (DEPLOY.md "Restore after a purge"): stop the hub, copy the backup
over ``gc.db``, move the purged folder's contents back into the data folder.
It rolls back every instrument to the backup's time.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import exports
import live
import paths
import pipeline
import store

log = logging.getLogger("purge")

SCOPES = ("all", "backfill")
PURGED_DIR = "purged"
BACKUP_PREFIX = "pre-purge-"
JOURNAL = "manifest.json"
WAIT_SECONDS = 600.0
LIST_MAX = 200          # kept_files/kept_samples/failed shown at most (totals are exact)
JOURNAL_EVERY = 1000    # moves between journal progress writes (the file system is the truth)

# table -> the column naming the sample a row belongs to. A row is deleted
# when that sample is purged (and, for conflicts, the conflict is the
# purged instrument's own: see _DOOMED).
OWNER_COLUMNS: dict = {
    "sample_results": "sample_id",
    "export_rows": "sample_id",
    "jobs": "sample_id",
    "sample_cache": "sample_id",
    "conflicts": "existing_sample_id",
    "sample_comments": "sample_id",
    "report_log": "sample_id",
}

# Every reference to samples (or to a table that references samples) in the
# schema, as (table, column, parent). tests/test_purge.py fails when the live
# schema has one that is not listed here, and run() refuses such a database.
KNOWN_REFERENCES = frozenset({
    ("sample_results", "sample_id", "samples"),
    ("sample_results", "blank_used", "samples"),
    ("conflicts", "existing_sample_id", "samples"),
    ("export_rows", "sample_id", "samples"),
    ("export_rows", "sample_id", "sample_results"),
    ("export_rows", "revision", "sample_results"),
    ("jobs", "sample_id", "samples"),
    ("sample_cache", "sample_id", "samples"),
    ("sample_comments", "sample_id", "samples"),
    ("report_log", "sample_id", "samples"),
})

# A column that looks like a sample reference even without a declared FK.
_SAMPLE_COLUMN = re.compile(r"^(?:.*_)?sample_id$")

_P = "(SELECT id FROM temp.purge_ids)"
_CTX_INST = "(SELECT instrument_id FROM temp.purge_ctx)"
_CTX_SCOPE = "(SELECT scope FROM temp.purge_ctx)"

# Which rows of each table the purge deletes (SQL over the temp tables).
_DOOMED: dict = {t: f'"{c}" IN {_P}' for t, c in OWNER_COLUMNS.items()}
_DOOMED["conflicts"] = (
    f"(instrument_id = {_CTX_INST} AND (existing_sample_id IN {_P} OR "
    f"(existing_sample_id IS NULL AND {_CTX_SCOPE} = 'all')))")


def _doomed(t: str) -> str:
    """Never NULL: a NULL column must neither be deleted nor count as kept wrongly."""
    return f"IFNULL(({_DOOMED[t]}), 0)"


class PurgeError(RuntimeError):
    """The purge could not be done; nothing was changed unless the message says so."""
    status = 500


class PurgeRefused(PurgeError):
    """Refused before anything was changed (bad input, unknown instrument, an
    unsafe database). ``status`` is the HTTP status the route answers."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _Retry(Exception):
    """A job of the instrument started running: roll back and wait again."""


# ── schema ──────────────────────────────────────────────────────────────────

def sample_references(conn: sqlite3.Connection) -> set:
    """Every ``(table, column, parent)`` that points at ``samples``, or at a
    table that does (transitively), from ``PRAGMA foreign_key_list``, plus any
    ``sample_id``/``*_sample_id`` column without a declared foreign key."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    fks = {t: [(r[2], r[3]) for r in conn.execute(f'PRAGMA foreign_key_list("{t}")')]
           for t in tables}
    found: set = set()
    parents = {"samples"}
    changed = True
    while changed:
        changed = False
        for t in tables:
            if t == "samples":
                continue
            for parent, col in fks[t]:
                if parent in parents and (t, col, parent) not in found:
                    found.add((t, col, parent))
                    changed = True
                    parents.add(t)
    for t in tables:
        if t == "samples":
            continue
        for r in conn.execute(f'PRAGMA table_info("{t}")'):
            col = r[1]
            if _SAMPLE_COLUMN.match(col) and not any(f[0] == t and f[1] == col for f in found):
                found.add((t, col, "samples"))
    return found


def unhandled_references(conn: sqlite3.Connection) -> list:
    return sorted(sample_references(conn) - KNOWN_REFERENCES)


def _delete_order() -> list:
    """OWNER_COLUMNS' tables, each after every table that references it."""
    after: dict = {t: set() for t in OWNER_COLUMNS}      # t -> tables that must go first
    for t, _c, p in KNOWN_REFERENCES:
        if p in after and t != p:
            after[p].add(t)
    order: list = []
    pending = sorted(OWNER_COLUMNS)
    while pending:
        ready = [t for t in pending if after[t] <= set(order)]
        if not ready:
            raise PurgeError("cyclic references between the purged tables")
        order += ready
        pending = [t for t in pending if t not in ready]
    return order


def _problems(conn: sqlite3.Connection) -> tuple:
    unhandled = unhandled_references(conn)
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    problems = []
    if unhandled:
        problems.append("the database has tables referencing samples that the purge does not "
                        "handle (update the hub first): "
                        + ", ".join(f"{t}.{c} -> {p}" for t, c, p in unhandled))
    if fk:
        problems.append(f"the database already has {len(fk)} foreign-key problem(s) "
                        "(PRAGMA foreign_key_check); nothing was purged")
    return problems, unhandled, len(fk)


# ── inputs ──────────────────────────────────────────────────────────────────

def confirmation_text(instrument_row: dict, *, db=None) -> str:
    """``PURGE <name>``; ``PURGE <name> (<id>)`` when another instrument has the
    same name (case-insensitive), so the text names exactly one instrument."""
    name = instrument_row.get("name") or instrument_row["id"]
    if db is not None:
        with store.connection(db) as conn:
            dup = conn.execute("SELECT 1 FROM instruments WHERE id<>? AND "
                               "lower(COALESCE(name, id))=lower(?)",
                               (instrument_row["id"], name)).fetchone()
        if dup is not None:
            return f"PURGE {name} ({instrument_row['id']})"
    return f"PURGE {name}"


def _instrument(instrument_id: Any, db) -> dict:
    inst_id = str(instrument_id or "").strip()
    if not inst_id:
        raise PurgeRefused("instrument is required")
    row = store.instruments.get(inst_id, db=db)
    if row is None:
        raise PurgeRefused(f"Unknown instrument {inst_id!r}", 404)
    return row


def _check_scope(scope: Any) -> str:
    if scope not in SCOPES:
        raise PurgeRefused("scope must be 'all' or 'backfill'")
    return scope


def _exporter(exporter, db, data_dir: Path) -> exports.HubExporter:
    return exporter if exporter is not None else exports.HubExporter(db, data_dir=data_dir)


def _same_file(a: Path, b: Path) -> bool:
    if exports._same_file_path(a, b):
        return True
    try:
        return os.path.samefile(a, b)       # a case-insensitive disk, a link
    except OSError:
        return False


def check_new_results_path(raw: Any, instrument_id: str, *, db, data_dir,
                           exporter=None) -> Path:
    """Validated like ``exports new-path``: an absolute ``.csv`` path in an
    existing folder, not another instrument's results file, not this one's."""
    text = str(raw or "").strip()
    p = Path(text) if text else None
    if p is None or not p.is_absolute() or p.suffix.lower() != ".csv" or not p.parent.is_dir():
        raise PurgeRefused("new_results_path must be an absolute .csv path in an existing folder")
    exp = _exporter(exporter, db, Path(data_dir))
    for other in store.instruments.list(db=db):
        if other["id"] == instrument_id:
            continue
        if _same_file(exp.export_path(other["id"]), p):
            raise PurgeRefused(f"{p} is {other['id']}'s results file; two instruments must "
                               "never share one", 409)
    if _same_file(exp.export_path(instrument_id), p):
        raise PurgeRefused("new_results_path is already this instrument's results file")
    return p


# ── the plan (inside a transaction on conn) ─────────────────────────────────

def _keep_reason(t: str, c: str, row) -> str:
    if (t, c) == ("sample_results", "blank_used"):
        return f"kept: it is the blank a kept result (sample {row['owner']}) subtracted"
    if t == "conflicts":
        return f"kept: conflict {row['rid']} of another instrument points at it"
    return f"kept: {t} row {row['rid']} (not purged) points at it"


def _plan(conn: sqlite3.Connection, instrument_id: str, scope: str) -> dict:
    """Fill ``temp.purge_ids`` / ``temp.purge_ctx`` and count what goes.
    Writes only temp tables."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS purge_ids(id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS purge_ctx(instrument_id TEXT, scope TEXT)")
    conn.execute("DELETE FROM temp.purge_ids")
    conn.execute("DELETE FROM temp.purge_ctx")
    conn.execute("INSERT INTO temp.purge_ctx VALUES (?, ?)", (instrument_id, scope))
    where = "instrument_id=?" + (" AND backfill=1" if scope == "backfill" else "")
    conn.execute(f"INSERT INTO temp.purge_ids(id) SELECT id FROM samples WHERE {where}",
                 (instrument_id,))
    kept: dict = {}
    while True:          # a kept row pointing at a purged sample keeps that sample
        found = []
        for t, c, p in sorted(KNOWN_REFERENCES):
            if p != "samples":
                continue
            owner = OWNER_COLUMNS[t]
            for r in conn.execute(
                    f'SELECT "{c}" AS sid, "{owner}" AS owner, rowid AS rid FROM "{t}" '
                    f'WHERE "{c}" IN {_P} AND NOT {_doomed(t)}'):
                found.append((r[0], _keep_reason(t, c, {"owner": r[1], "rid": r[2]})))
        if not found:
            break
        for sid, reason in found:
            kept.setdefault(sid, reason)
            conn.execute("DELETE FROM temp.purge_ids WHERE id=?", (sid,))
    tables = {"samples": conn.execute("SELECT COUNT(*) FROM temp.purge_ids").fetchone()[0]}
    for t in OWNER_COLUMNS:
        tables[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE {_doomed(t)}').fetchone()[0]
    appended = conn.execute(
        f"SELECT COUNT(DISTINCT sample_id), COUNT(*) FROM export_rows WHERE sample_id IN {_P} "
        "AND hub_appended_at IS NOT NULL").fetchone()
    kept_samples = []
    for sid, reason in sorted(kept.items()):
        r = conn.execute("SELECT lab_id FROM samples WHERE id=?", (sid,)).fetchone()
        kept_samples.append({"id": sid, "lab_id": r[0] if r else None, "reason": reason})
    ids = [r[0] for r in conn.execute("SELECT id FROM temp.purge_ids ORDER BY id")]
    return {"samples": tables["samples"], "tables": tables, "appended": appended[0],
            "appended_rows": appended[1], "kept_samples": kept_samples, "sample_ids": ids}


def _key(p: Path) -> str:
    try:
        p = p.resolve()
    except OSError:
        p = Path(os.path.abspath(p))
    return os.path.normcase(str(p))


def _path(raw: Any, data_dir: Path) -> Optional[Path]:
    text = str(raw or "").strip()
    if not text:
        return None
    p = Path(text)
    return p if p.is_absolute() else data_dir / p


def _settings_calibration(data_dir: Path) -> Optional[str]:
    try:
        return json.loads((data_dir / "settings.json").read_text(encoding="utf-8")).get(
            "calibration_cdf")
    except (OSError, ValueError, AttributeError):
        return None


def _calibration_used_cdf(raw: Any) -> Optional[str]:
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return val.get("cdf") if isinstance(val, dict) and isinstance(val.get("cdf"), str) else None


def _protected(conn: sqlite3.Connection, data_dir: Path) -> dict:
    """``{path key: reason}`` for every CDF something kept references."""
    out: dict = {}

    def add(raw, reason):
        p = _path(raw, data_dir)
        if p is not None:
            out.setdefault(_key(p), reason)

    for r in conn.execute("SELECT id, name, calibration_cdf FROM instruments"):
        add(r[2], f"the calibration CDF of {r[1] or r[0]}")
    add(_settings_calibration(data_dir), "the calibration CDF in settings.json")
    for r in conn.execute("SELECT name, cdf_path FROM standards"):
        add(r[1], f"comparison standard {r[0]}")
    for r in conn.execute(f"SELECT id, cdf_path FROM samples WHERE id NOT IN {_P}"):
        add(r[1], f"the CDF of sample {r[0]}, which is not purged")
    for r in conn.execute(f"SELECT sample_id, cdf_path, blank_cdf_path, calibration_used "
                          f"FROM sample_results WHERE NOT {_doomed('sample_results')}"):
        add(r[1], f"used by a result of sample {r[0]}, which is not purged")
        add(r[2], f"the blank a result of sample {r[0]} subtracted (not purged)")
        add(_calibration_used_cdf(r[3]),
            f"the calibration a result of sample {r[0]} was computed with (not purged)")
    for r in conn.execute(f"SELECT id, cdf_path FROM conflicts WHERE NOT {_doomed('conflicts')}"):
        add(r[1], f"held as conflict {r[0]}, which is not purged")
    return out


def _candidates(conn: sqlite3.Connection, data_dir: Path) -> dict:
    """``{path key: Path}``: every CDF a purged row records."""
    out: dict = {}
    queries = (f"SELECT cdf_path FROM samples WHERE id IN {_P}",
               f"SELECT cdf_path FROM sample_results WHERE {_doomed('sample_results')}",
               f"SELECT blank_cdf_path FROM sample_results WHERE {_doomed('sample_results')}",
               f"SELECT cdf_path FROM conflicts WHERE {_doomed('conflicts')}")
    for q in queries:
        for (raw,) in conn.execute(q):
            p = _path(raw, data_dir)
            if p is not None:
                out.setdefault(_key(p), p)
    return out


def _shown(p: Path, data_dir: Path) -> str:
    try:
        return p.resolve().relative_to(data_dir.resolve()).as_posix()
    except (ValueError, OSError):
        return str(p)


def _files(conn: sqlite3.Connection, data_dir: Path) -> dict:
    """What happens to each purged CDF: ``move`` (Paths), ``kept``
    ([{path, reason}]), ``missing``, ``outside``, ``bytes``."""
    protected = _protected(conn, data_dir)
    cdf_root = _key(data_dir / "cdf")
    incoming = _key(data_dir / "cdf" / pipeline.INCOMING_DIR)
    move, kept, missing, outside, size = [], [], 0, 0, 0
    for key, p in sorted(_candidates(conn, data_dir).items()):
        if key in protected:
            kept.append({"path": _shown(p, data_dir), "reason": protected[key]})
        elif not key.startswith(cdf_root + os.sep) or key.startswith(incoming + os.sep):
            outside += 1
            kept.append({"path": _shown(p, data_dir),
                         "reason": "outside the hub's CDF folder; left where it is"})
        elif not p.is_file():
            missing += 1
        else:
            move.append(p)
            size += p.stat().st_size
    return {"move": move, "kept": kept, "missing": missing, "outside": outside, "bytes": size}


def _running_jobs(conn: sqlite3.Connection, instrument_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM jobs j JOIN samples s ON s.id=j.sample_id "
                        "WHERE j.state='running' AND s.instrument_id=?",
                        (instrument_id,)).fetchone()[0]


def _read_plan(db, instrument_id: str, scope: str) -> dict:
    with store.connection(db) as conn:
        conn.execute("BEGIN")
        try:
            return _plan(conn, instrument_id, scope)
        finally:
            conn.execute("ROLLBACK")


# ── preview ─────────────────────────────────────────────────────────────────

def preview(instrument_id: Any, scope: Any, *, db=None, data_dir=None, exporter=None) -> dict:
    """What ``run`` would do now. Writes nothing (a read transaction, rolled back)."""
    data_dir = Path(data_dir) if data_dir is not None else paths.require_data_dir()
    db = db if db is not None else data_dir / store.DB_FILENAME
    inst = _instrument(instrument_id, db)
    scope = _check_scope(scope)
    with store.connection(db) as conn:
        problems, unhandled, fk = _problems(conn)
        conn.execute("BEGIN")
        try:
            plan = _plan(conn, inst["id"], scope) if not unhandled else {
                "samples": 0, "tables": {}, "appended": 0, "appended_rows": 0,
                "kept_samples": []}
            files = _files(conn, data_dir) if not unhandled else {
                "move": [], "kept": [], "missing": 0, "outside": 0, "bytes": 0}
        finally:
            conn.execute("ROLLBACK")
    try:
        results_path = str(_exporter(exporter, db, data_dir).export_path(inst["id"]))
    except Exception:  # noqa: BLE001 - informational only
        results_path = None
    warning = None
    if plan["appended"]:
        warning = (f"{plan['appended']} of these samples were already appended to the results "
                   f"file ({results_path}). The file is append-only and is not edited: if the "
                   "same runs are sent again they are appended again. You can switch to a new "
                   "results file after the purge.")
    return {"ok": not problems, "problems": problems,
            "unhandled": [list(u) for u in unhandled], "fk_problems": fk,
            "instrument": inst["id"], "instrument_name": inst.get("name") or inst["id"],
            "scope": scope, "confirm_text": confirmation_text(inst, db=db),
            "samples": plan["samples"], "tables": plan["tables"],
            "appended": plan["appended"], "appended_rows": plan["appended_rows"],
            "kept_samples": plan["kept_samples"][:LIST_MAX],
            "kept_files": files["kept"][:LIST_MAX], "kept_files_total": len(files["kept"]),
            "files": {"move": len(files["move"]), "bytes": files["bytes"],
                      "missing": files["missing"], "outside": files["outside"]},
            "results_path": results_path, "warning": warning}


# ── the journal ─────────────────────────────────────────────────────────────

def _save(path: Path, journal: dict) -> None:
    """Replace the journal atomically (temp file in the same folder, fsync, replace)."""
    fd, tmp = tempfile.mkstemp(prefix=".manifest.", suffix=".part", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(journal, fh, indent=1, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save_quietly(path: Path, journal: dict) -> None:
    try:
        _save(path, journal)
    except Exception as exc:  # noqa: BLE001 - after the commit: finish, report it
        log.exception("purge: could not write the journal %s", path)
        journal.setdefault("warnings", []).append(f"The purge journal could not be written ({exc}).")


_BIG = ("sample_ids", "moves")


def _summary(journal: dict) -> dict:
    out = {k: v for k, v in journal.items() if k not in _BIG}
    out["kept_samples"] = list(journal.get("kept_samples") or [])[:LIST_MAX]
    out["kept_files"] = list(journal.get("kept_files") or [])[:LIST_MAX]
    return out


def latest_journal(data_dir) -> Optional[dict]:
    """The newest purge's journal (summary form), for the admin page."""
    root = Path(data_dir) / PURGED_DIR
    best = None
    for m in root.glob(f"*/{JOURNAL}") if root.is_dir() else ():
        try:
            j = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = str(j.get("started_at") or "")
        if best is None or key > best[0]:
            best = (key, j)
    return _summary(best[1]) if best else None


# ── run ─────────────────────────────────────────────────────────────────────

def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe(instrument_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", instrument_id) or "instrument"


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        cand = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not cand.exists():
            return cand
        i += 1


def _backup_tmp(dest: Path) -> Path:
    return dest.parent / f".{dest.name}.tmp"


def _backup(db: Path, dest: Path) -> Path:
    """``VACUUM INTO`` ``dest`` (via a temp file), integrity-checked."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _backup_tmp(dest)
    if tmp.exists():
        tmp.unlink()
    conn = store.open_db(db, readonly=True)
    try:
        conn.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        conn.close()
    check = sqlite3.connect(str(tmp))
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if ok != "ok":
        tmp.unlink()
        raise PurgeError(f"the backup failed its integrity check ({ok}); nothing was purged")
    os.replace(tmp, dest)
    log.warning("purge: database backed up to %s", dest)
    return dest


def _wait_for_running(db, instrument_id: str, deadline: float, poll: float, step) -> None:
    """Wait (holding no lock) until none of the instrument's jobs is running."""
    while True:
        with store.connection(db) as conn:
            n = _running_jobs(conn, instrument_id)
        if not n:
            return
        if time.monotonic() >= deadline:
            raise PurgeError(f"{n} processing job(s) for {instrument_id} are still running; "
                             "nothing was purged. Try again when they have finished.")
        step({"phase": "waiting for processing", "done": 0, "total": n})
        time.sleep(poll)


def _delete(conn: sqlite3.Connection, plan: dict) -> None:
    for t in _delete_order():
        n = conn.execute(f'DELETE FROM "{t}" WHERE {_doomed(t)}').rowcount
        if n != plan["tables"][t]:
            raise PurgeError(f"{t}: {n} rows deleted, {plan['tables'][t]} expected; rolled back")
    n = conn.execute(f"DELETE FROM samples WHERE id IN {_P}").rowcount
    if n != plan["samples"]:
        raise PurgeError(f"samples: {n} deleted, {plan['samples']} expected; rolled back")


def _verify(conn: sqlite3.Connection) -> None:
    for t, c, p in sorted(sample_references(conn)):
        if p != "samples":
            continue
        n = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE "{c}" IN {_P}').fetchone()[0]
        if n:
            raise PurgeError(f"{t}.{c} still points at {n} purged sample(s); rolled back")
    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise PurgeError(f"PRAGMA foreign_key_check found {len(bad)} problem(s) after the "
                         "delete; rolled back")


def _record_event(db, instrument_id: str, by: str, detail: dict) -> None:
    """An ``instrument_events`` row (schema v4, another v3.1 branch) when that
    table exists."""
    with store.connection(db) as conn:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("instrument_events")')}
        if not {"instrument_id", "kind", "by", "at", "detail"} <= cols:
            return
        with store.write_txn(conn):
            conn.execute('INSERT INTO instrument_events(instrument_id, kind, "by", at, detail) '
                         "VALUES (?, 'purge', ?, ?, ?)",
                         (instrument_id, by, store.now_iso(), json.dumps(detail)))


def _publish(journal: dict) -> None:
    """Live events (``live.py``): the instrument, and every purged sample, so
    open pages drop the rows (more than the ring holds makes them reload)."""
    live.publish("instrument", {"instrument_id": journal["instrument"]})
    live.publish_samples(journal.get("sample_ids") or [])


def _who(by: str) -> str:
    return re.sub(r"\s*\([^()]*\)\s*$", "", by or "").strip() or "An admin"


def _notify(notifier, level: str, message: str) -> None:
    log.warning("purge: %s", message)
    if notifier is None:
        return
    try:
        notifier(level, message)
    except Exception:  # noqa: BLE001
        log.exception("purge: notification failed")


def _abandon(journal: dict, jpath: Path, reason: str, notifier, *, interrupted: bool) -> None:
    """Nothing was committed: delete the backup, mark the journal abandoned, say so."""
    b = journal.get("backup")
    if b:
        for p in (Path(b), _backup_tmp(Path(b))):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                log.exception("purge: could not delete the unused backup %s", p)
        journal["backup_deleted"] = True
    journal.update(state="abandoned", reason=reason, finished_at=store.now_iso())
    try:
        _save(jpath, journal)
    except Exception:  # noqa: BLE001
        log.exception("purge: could not write the journal %s", jpath)
    name = journal.get("instrument_name") or journal.get("instrument")
    if interrupted:
        text = (f"A purge of {name} was interrupted before it changed anything; nothing was "
                f"removed. Its unused backup was deleted.")
    else:
        text = (f"The purge of {name} stopped before it changed anything ({reason}); nothing "
                f"was removed. Its unused backup was deleted.")
    _notify(notifier, "warning", text)


def _move_all(journal: dict, jpath: Path, data_dir: Path, step) -> None:
    """Every planned move, idempotent: a file already at its destination (and
    gone from its source) counts as moved."""
    moved = missing = 0
    failed: list = []
    moves = journal.get("moves") or []
    for i, m in enumerate(moves, 1):
        src, dest = data_dir / m["from"], data_dir / m["to"]
        try:
            if src.exists() and dest.exists():
                raise FileExistsError(f"{m['to']} already exists")
            if src.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dest)
                moved += 1
            elif dest.exists():
                moved += 1
            else:
                missing += 1
        except OSError as exc:
            failed.append({"path": m["from"], "error": str(exc)})
            log.error("purge: could not move %s: %s", src, exc)
        if i % JOURNAL_EVERY == 0:
            journal["files"]["moved_so_far"] = i
            _save_quietly(jpath, journal)
        step({"phase": "moving files", "done": i, "total": len(moves), "file": m["from"]})
    journal["files"].pop("moved_so_far", None)
    journal["files"].update(moved=moved, failed=failed[:LIST_MAX], failed_total=len(failed),
                            missing=journal["files"].get("missing", 0) + missing)
    if failed:
        journal["warnings"].append(f"{len(failed)} CDF(s) could not be moved and are still in "
                                   "the cdf folder (see files.failed)")


def _finish(journal: dict, jpath: Path, *, db, data_dir: Path, exporter, notifier, step,
            recovered: bool) -> dict:
    """Everything after the commit. Each step is idempotent and recorded in the
    journal; a failure becomes a warning, never an exception."""
    inst_id = journal["instrument"]
    name = journal.get("instrument_name") or inst_id
    journal.setdefault("warnings", [])
    exp = _exporter(exporter, db, data_dir)
    if recovered:
        journal["recovered"] = True
    if not journal.get("export_relinked"):
        try:
            journal["export"] = exp.after_purge(inst_id)
            journal["export_relinked"] = True
        except Exception as exc:  # noqa: BLE001 - the admin can Adopt
            journal["export"] = {"path": None, "relinked": False, "error": str(exc)}
            journal["warnings"].append(
                f"The results file's record could not be updated ({exc}); if Admin > Exports "
                "shows it refused (ledger-mismatch), Adopt it.")
        _save_quietly(jpath, journal)
    try:
        _move_all(journal, jpath, data_dir, step)
    except Exception as exc:  # noqa: BLE001
        log.exception("purge: moving the files failed")
        journal["warnings"].append(f"Moving the CDFs failed ({exc}); they are listed in "
                                   f"{jpath}.")
    _save_quietly(jpath, journal)
    new_path = journal.get("new_results_path")
    if new_path and not journal.get("new_path_done"):
        try:
            exp.new_path(inst_id, Path(new_path))
            exp.wake()
            journal["export"] = dict(journal.get("export") or {}, new_path=str(new_path))
            journal["new_path_done"] = True
        except Exception as exc:  # noqa: BLE001
            journal["warnings"].append(f"The new results file was refused: {exc}. Set it on "
                                       "Admin > Exports.")
        _save_quietly(jpath, journal)
    if not journal.get("event_recorded"):
        try:
            _record_event(db, inst_id, journal.get("by") or "", {k: journal.get(k) for k in (
                "scope", "samples", "tables", "backup", "purged_folder", "appended")})
        except Exception:  # noqa: BLE001 - the purge is done; the record is extra
            log.exception("purge: could not record the instrument event")
        journal["event_recorded"] = True
        _save_quietly(jpath, journal)
    journal.update(state="done", finished_at=store.now_iso(),
                   completed_with_warnings=bool(journal["warnings"]))
    if not journal.get("notified"):
        message = (f"{_who(journal.get('by'))} purged {journal.get('samples', 0):,} {name} "
                   f"samples ({journal.get('scope')})"
                   + (" (finished after a restart)" if recovered else "")
                   + f" · backup {Path(journal['backup']).name} · files in "
                   f"{PURGED_DIR}{os.sep}{Path(journal['purged_folder']).name}")
        if journal["warnings"]:
            message += " · completed with warnings: " + " ".join(journal["warnings"])
        _notify(notifier, "warning", message)
        journal["notified"] = True
    _save_quietly(jpath, journal)
    _publish(journal)
    return _summary(journal)


def run(instrument_id: Any, scope: Any, *, confirm_text: Any, by: str, db=None, data_dir=None,
        exporter=None, notifier: Optional[Callable[[str, str], Any]] = None,
        new_results_path: Any = None, progress: Optional[Callable[[dict], Any]] = None,
        wait_seconds: float = WAIT_SECONDS, poll_seconds: float = 0.5,
        _before_commit: Optional[Callable[[sqlite3.Connection], Any]] = None) -> dict:
    """Purge (see the module docstring). ``progress(event)`` may raise (an
    admin's Stop) until the transaction commits; after that the purge always
    finishes. ``_before_commit(conn)`` is a test hook, run inside the
    transaction after every delete."""
    data_dir = Path(data_dir) if data_dir is not None else paths.require_data_dir()
    db = db if db is not None else data_dir / store.DB_FILENAME
    inst = _instrument(instrument_id, db)
    inst_id = inst["id"]
    name = inst.get("name") or inst_id
    scope = _check_scope(scope)
    expected = confirmation_text(inst, db=db)
    if confirm_text != expected:
        raise PurgeRefused(f'Type "{expected}" exactly to confirm the purge')
    new_path = (check_new_results_path(new_results_path, inst_id, db=db, data_dir=data_dir,
                                       exporter=exporter)
                if new_results_path not in (None, "") else None)
    with store.connection(db) as conn:
        problems, _unhandled, _fk = _problems(conn)
    if problems:
        raise PurgeRefused("; ".join(problems), 409)

    committed = False

    def step(event: dict) -> None:
        if progress is None:
            return
        if not committed:
            progress(event)             # may raise: stop before anything is changed
            return
        try:
            progress(event)
        except Exception:  # noqa: BLE001 - past the point of no return: finish
            pass

    pipeline.pause_instrument(inst_id)
    log.warning("purge: %s (%s) started by %s; processing and ingest for it paused",
                inst_id, scope, by)
    try:
        step({"phase": "pausing", "done": 0, "total": 0})
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        _wait_for_running(db, inst_id, deadline, poll_seconds, step)
        pre = _read_plan(db, inst_id, scope)
        if not pre["samples"]:
            log.warning("purge: %s (%s): nothing to purge", inst_id, scope)
            return {"instrument": inst_id, "instrument_name": name, "scope": scope, "by": by,
                    "state": "done", "nothing_to_do": True, "samples": 0, "tables": {},
                    "appended": 0, "kept_samples": [], "kept_files": [], "backup": None,
                    "purged_folder": None, "files": {"moved": 0, "bytes": 0, "missing": 0,
                                                     "outside": 0, "failed": []},
                    "export": None, "warnings": [], "started_at": store.now_iso(),
                    "finished_at": store.now_iso()}

        stamp = _stamp()
        dest_root = _unique(data_dir / PURGED_DIR / f"{_safe(inst_id)}-{stamp}")
        dest_root.mkdir(parents=True)
        jpath = dest_root / JOURNAL
        backup_path = _unique(db.parent / "backups" / f"{BACKUP_PREFIX}{_safe(inst_id)}-{stamp}.db")
        journal = {"version": 1, "state": "pending", "instrument": inst_id,
                   "instrument_name": name, "scope": scope, "by": by,
                   "started_at": store.now_iso(), "finished_at": None,
                   "backup": str(backup_path), "purged_folder": str(dest_root),
                   "new_results_path": str(new_path) if new_path else None,
                   "sample_ids": pre["sample_ids"], "samples": pre["samples"],
                   "tables": pre["tables"], "appended": pre["appended"],
                   "kept_samples": pre["kept_samples"], "kept_files": [], "moves": [],
                   "files": {"moved": 0, "bytes": 0, "missing": 0, "outside": 0, "failed": []},
                   "export": None, "warnings": [], "nothing_to_do": False}
        _save(jpath, journal)
        try:
            step({"phase": "backup", "done": 0, "total": 1})
            _backup(db, backup_path)
            step({"phase": "deleting", "done": 0, "total": pre["samples"]})
            exp = _exporter(exporter, db, data_dir)
            base = data_dir.resolve()
            while True:
                _wait_for_running(db, inst_id, deadline, poll_seconds, step)   # no lock held
                with exports._instrument_lock(inst_id):
                    try:
                        with store.connection(db) as conn:
                            with store.write_txn(conn):
                                if _running_jobs(conn, inst_id):
                                    raise _Retry()
                                plan = _plan(conn, inst_id, scope)
                                files = _files(conn, data_dir)
                                _delete(conn, plan)
                                _verify(conn)
                                journal.update(
                                    sample_ids=plan["sample_ids"], samples=plan["samples"],
                                    tables=plan["tables"], appended=plan["appended"],
                                    kept_samples=plan["kept_samples"], kept_files=files["kept"],
                                    moves=[{"from": p.resolve().relative_to(base).as_posix(),
                                            "to": (dest_root / p.resolve().relative_to(base))
                                            .relative_to(data_dir).as_posix()}
                                           for p in files["move"]],
                                    files={"moved": 0, "bytes": files["bytes"],
                                           "missing": files["missing"],
                                           "outside": files["outside"],
                                           "kept": len(files["kept"]), "failed": []})
                                _save(jpath, journal)          # still pending: before COMMIT
                                if _before_commit is not None:
                                    _before_commit(conn)
                    except _Retry:
                        continue                               # releases the lock, waits again
                    committed = True
                    log.warning("purge: %s (%s) deleted %s", inst_id, scope, plan["tables"])
                    journal["state"] = "committed"
                    journal["committed_at"] = store.now_iso()
                    _save_quietly(jpath, journal)
                    try:
                        journal["export"] = exp.after_purge(inst_id)
                        journal["export_relinked"] = True
                    except Exception:  # noqa: BLE001 - _finish retries and reports it
                        log.exception("purge: after_purge failed")
                    _save_quietly(jpath, journal)
                break
        except BaseException as exc:
            if committed:
                raise
            _abandon(journal, jpath, f"{type(exc).__name__}: {exc}", notifier, interrupted=False)
            raise
        try:
            return _finish(journal, jpath, db=db, data_dir=data_dir, exporter=exp,
                           notifier=notifier, step=step, recovered=False)
        except Exception as exc:  # noqa: BLE001 - never report a committed purge as failed
            log.exception("purge: finishing failed")
            journal.setdefault("warnings", []).append(f"Finishing the purge failed: {exc}")
            journal["completed_with_warnings"] = True
            return _summary(journal)
    finally:
        pipeline.resume_instrument(inst_id)
        _wake_worker()


# ── recovery (hub.start) ────────────────────────────────────────────────────

def recover(*, db=None, data_dir=None, notifier=None, exporter=None) -> list:
    """Finish or abandon every purge a crash interrupted (see the module
    docstring). Called by ``hub.start`` before the exporter starts. Safe to run
    again at any point: every step is idempotent."""
    data_dir = Path(data_dir) if data_dir is not None else paths.require_data_dir()
    db = db if db is not None else data_dir / store.DB_FILENAME
    root = data_dir / PURGED_DIR
    out = []
    if not root.is_dir():
        return out
    for jpath in sorted(root.glob(f"*/{JOURNAL}")):
        try:
            journal = json.loads(jpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("purge: unreadable journal %s", jpath)
            continue
        state = journal.get("state")
        if state not in ("pending", "committed"):
            continue
        if state == "pending":
            ids = [int(i) for i in journal.get("sample_ids") or []]
            with store.connection(db) as conn:
                present = sum(conn.execute(
                    f"SELECT COUNT(*) FROM samples WHERE id IN ({','.join('?' * len(chunk))})",
                    chunk).fetchone()[0] for chunk in _chunks(ids, 500))
            if ids and present == 0:
                journal["state"] = "committed"          # the crash fell after the COMMIT
                _save(jpath, journal)
            elif present == len(ids):
                log.warning("purge: %s was interrupted before its commit; abandoning", jpath)
                _abandon(journal, jpath, "interrupted (the hub stopped) before it changed "
                                         "anything", notifier, interrupted=True)
                out.append(_summary(journal))
                continue
            else:
                journal.update(state="needs-attention",
                               reason=f"{present} of {len(ids)} planned samples still exist")
                _save(jpath, journal)
                _notify(notifier, "error",
                        f"An interrupted purge of {journal.get('instrument_name')} is in an "
                        f"unexpected state ({journal['reason']}); nothing was changed by the "
                        f"hub. See {jpath}.")
                out.append(_summary(journal))
                continue
        log.warning("purge: finishing %s after a restart", jpath)
        out.append(_finish(journal, jpath, db=db, data_dir=data_dir, exporter=exporter,
                           notifier=notifier, step=lambda e: None, recovered=True))
    return out


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _wake_worker() -> None:
    try:
        import hub
        rt = hub.running()
        if rt is not None and rt.worker is not None:
            rt.worker.wake()
    except Exception:  # noqa: BLE001 - it polls anyway
        pass
