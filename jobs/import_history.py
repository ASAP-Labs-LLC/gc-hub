"""History import (phase 2, plan 2D): v1's processed CDFs and results CSV
into the hub, for one instrument (spec "History import (2D)", D1/D2/D10/D11).

``import_match`` pairs the CSV rows with the CDFs by each CDF's own metadata
(contract §3); this module turns its ``MatchReport`` into samples and
revisions. It runs **inside the hub process** as an admin job (the hub is the
single writer); ``tools/import_history.py`` only does a dry run.

What each class becomes
=======================

* **CDF + rows** (attached): the CDF is copied into the data folder
  (``cdf/<inst>/<YYYY>/<MM>/<lab>_<id>.CDF``, the pipeline's layout, with the
  original file's mtime; ``source_name`` is its file name) and each CSV row
  becomes a revision, in CSV order, the last current. Status by the method
  rule (spec "Method detection"): ``final`` if the CDF's method name maps in
  the instrument's ``method_map`` to a registered hub method,
  ``other_method`` if not, ``review_method`` if the CDF has none. The rows
  are kept as revisions in every case.
* **Orphan CDF** (no row): ``raw_only`` (processed only on request) under
  the same method rule (``other_method``/``review_method`` otherwise).
* **Result-only** (rows, no CDF): ``final``, ``cdf_sha256``/``cdf_path``/
  ``method_name`` NULL, ``legacy_unverified=1``, ``injection_dt_source
  'csv'``, ``injection_dt`` the CSV string, ``time_unverifiable`` from the
  matcher (a whole-minute time v1 could have misparsed).
* Every imported sample has ``backfill=1`` and is never exported
  automatically (D11): **no ``export_rows`` are written and no job is
  queued**, so nothing is recomputed.

Verbatim revisions
==================

Historical numbers are never recomputed. Each revision is written with
``store.add_revision`` (reason ``import``): ``results`` is the JSON of the
row's ``CSV_HEADER`` cells **as the exact strings v1 wrote** (v1's
``InjectionDateTime`` and ``Source File`` included), ``corrections_used``
``{"source": "legacy"}``, ``blank_used``/``calibration_used``/
``d86_uncorrected``/``flags`` NULL, ``best_fit`` the ``Best Fit`` cell (NULL
if empty), ``fit_score`` the ``Fit Score`` cell as a float (NULL if empty or
not a finite number), ``notes`` ``{"import": {"csv", "line_no"}}``, and the
sample's CDF (NULL for result-only). Since the values stay strings,
``exports.format_line(results, source_file)`` reproduces v1's line byte for
byte, ``Source File`` aside (the hub passes its relative ``cdf_path``).
Consumers of ``sample_results.results`` must therefore accept strings as
well as numbers.

Identity and times
==================

``lab_id`` is ``import_match``'s identity form (the pipeline's rule).
``injection_dt`` is the CDF's **correct** time; for a CDF with no stamp it is
the file's mtime **truncated to the whole second** (the hub's rule, so a
live copy of the same file collides with the imported one).
``legacy_injection_dt`` is what v1 wrote (``CdfMeta.legacy_injection_dt``:
the 3.11+ misparse, or the unrounded mtime) and ``time_corrected`` is set when
the two differ. ``is_blank`` is decided as ``pipeline.submit`` does (a blank
name and a plausible blank signal), so live samples after cutover find
history blanks; no late-blank review notes are written.

Other classes (listed in the summary)
=====================================

* ``key_collisions`` ``(kept, other)``: ``other`` is stored as a conflict
  against ``kept``'s sample (``cdf/<inst>/conflicts/...``,
  ``store.conflicts.add``), for the admin conflict screen. A CDF whose key
  (lab ID, injection time) is already held in the store by a different file
  is a conflict too (as ``submit``); its rows are not imported.
* A file already held by another instrument (sample or conflict):
  ``cross_instrument``, not imported.
* A truncated or empty CDF (``pipeline.cdf_problem``): not imported, nor its
  rows; recopy it and re-run.
* ``ambiguous_rows`` (one CSV key fitting several CDFs), ``held_rows`` (bad
  layout), ``mixed_rows`` (another instrument's folder) and rows without a
  lab ID or time: listed, never imported.

Idempotent and resumable
========================

A second run over the same inputs writes nothing. Per sample: a file this
instrument already holds is not imported again; if the CSV has gained rows
for it since, and its stored import revisions are exactly the first rows
(and it has no hub revision), the new rows are added as further ``import``
revisions (``delta``; an imported orphan that gains rows takes its status by
the method rule). A sample the hub itself received (no import revision, not
``raw_only``) never gets v1 rows (``present_not_imported``). A result-only
key already held (or a CDF sample whose correct or legacy time is that CSV
time) is not duplicated (``key_taken``). Conflicts are found by sha.

Files are staged in ``cdf/.incoming`` outside any transaction (hashed while
copied; a source that changed since it was read fails that sample). Then one
``write_txn`` per ``batch_size`` samples (default 500), each sample in its own
SAVEPOINT (a failure is recorded and the batch goes on), the stored file moved
into place as its last step. If a batch fails to commit, or ``progress``
raises (the admin page's pause/cancel), that batch rolls back, the files it
placed are removed, the summary so far is attached to the exception as
``exc.import_summary`` and it is re-raised; a re-run resumes.

API
===

::

    import_history(instrument_id, processed_dir, results_csv, *,
                   instrument_folder_aliases, db=None, data_dir=None,
                   progress=None, dry_run=False, conf=None, by="import",
                   batch_size=500, examples=20) -> dict
    format_summary(summary, *, examples=5) -> str

``results_csv=None`` imports the CDFs alone (all orphans). ``conf`` is the
global settings (only ``blank_max_intensity_pa`` is read; default
``settings.load_settings()``). ``dry_run=True`` writes nothing: with a
``db`` it classifies against the store (read only); with ``db=None`` and no
``data_dir`` it assumes an empty store and ``instruments.DEFAULT_METHOD_MAP``.

``progress(event)`` receives dicts for the admin page's SSE stream:
``{"phase": "scan"}``; ``{"phase": "match", "done", "total"}`` (CDF
metadata, every 500 files); ``{"phase": "check", "done", "total"}`` (the
truncation check); ``{"phase": "import", "done", "total", "outcome",
"lab_id"}`` per sample; ``{"phase": "commit", "done", "total"}`` per batch;
``{"phase": "done", "summary"}``.

The summary is JSON-ready: ``instrument``, ``processed_dir``,
``results_csv``, ``aliases``, ``dry_run``, ``store_checked``,
``started_at``, ``seconds``, ``counts`` (see ``COUNT_KEYS``),
``method_names`` (histogram of the CDFs kept), ``method_names_per_folder``,
``v1_misparse``, ``matcher`` (selected matcher stats), ``examples`` (up to
``examples`` entries per class) and, when stopped, ``stopped``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import distill
import import_match
import instruments
import methods
import paths
import pipeline
import store

log = logging.getLogger("jobs.import_history")

Progress = Callable[[dict], object]
REASON = "import"
LEGACY_CORRECTIONS = {"source": "legacy"}
BATCH_SIZE = 500
_CHUNK = 1 << 20

# The counters of the summary, in display order.
COUNT_KEYS = (
    "cdf_files", "cdfs_read", "cdf_errors", "dup_sha", "csv_rows",
    "imported", "attached", "orphans", "result_only", "revisions", "delta_revisions",
    "final", "raw_only", "other_method", "review_method",
    "time_corrected", "time_unverifiable", "no_injection_time", "blanks",
    "already_imported", "present_not_imported", "delta_refused", "rows_changed", "key_taken",
    "cross_instrument", "conflicts", "conflicts_existing", "key_collisions",
    "collisions_unresolved", "truncated", "failed",
    "ambiguous_rows", "held_rows", "mixed_rows", "unmatched_without_key", "rows_not_imported",
    "whitespace_only_names", "name_from_filename",
)


def _emit(progress: Optional[Progress], event: dict) -> None:
    if progress is not None:
        progress(event)


def _whole_second(dt: str) -> str:
    from datetime import datetime
    return datetime.fromisoformat(dt).replace(microsecond=0).isoformat(sep=" ")


def _values(r: import_match.CsvRow) -> dict:
    return {c: r.values.get(c, "") for c in distill.CSV_HEADER}


def _fit_score(cell: str) -> Optional[float]:
    try:
        v = float((cell or "").strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _row_brief(r: import_match.CsvRow, **extra) -> dict:
    return dict({"line_no": r.line_no, "lab_id": r.values.get("Lab ID", r.lab_id),
                 "injection_dt": r.injection_dt_raw, "source_file": r.source_file}, **extra)


# ── the plan for one matched sample ─────────────────────────────────────────

@dataclass
class _Item:
    ms: import_match.MatchedSample
    rows: list                        # the rows to import (ambiguous ones removed)
    lab_id: str
    injection_dt: str
    dt_source: str
    legacy: Optional[str] = None
    time_corrected: int = 0
    method_name: Optional[str] = None
    status: str = "final"
    problem: Optional[str] = None     # cdf_problem (not imported)
    is_blank: int = 0
    staged: Optional[Path] = None     # the copy in cdf/.incoming
    collision_with: Optional[str] = None   # a key collision's `other`: the kept CDF's path
    extra: dict = field(default_factory=dict)

    @property
    def cdf(self):
        return self.ms.cdf


def _method_status(method_name: str, mm: dict, has_rows: bool) -> str:
    name = methods.normalise_method_name(method_name)
    if not name:
        return "review_method"
    hub_method = mm.get(name)
    if hub_method is None:
        return "other_method"
    try:
        methods.get(hub_method)
    except methods.UnknownMethod:
        return "other_method"
    return "final" if has_rows else "raw_only"


def _plan(ms: import_match.MatchedSample, rows: list, mm: dict) -> _Item:
    c = ms.cdf
    if c is None:
        return _Item(ms=ms, rows=rows, lab_id=ms.lab_id, injection_dt=ms.injection_dt,
                     dt_source="csv", status="final")
    inj = _whole_second(c.injection_dt) if c.dt_source == "mtime" else c.injection_dt
    legacy = c.legacy_injection_dt or c.injection_dt
    return _Item(ms=ms, rows=rows, lab_id=ms.lab_id, injection_dt=inj, dt_source=c.dt_source,
                 legacy=legacy, time_corrected=int(legacy != inj), method_name=c.method_name,
                 status=_method_status(c.method_name, mm, bool(rows)))


# ── classification against the store ───────────────────────────────────────

def _import_revisions(conn, sample_id: int) -> tuple[list, list]:
    revs = store.list_revisions(sample_id, db=conn)
    return [r for r in revs if r["reason"] == REASON], [r for r in revs if r["reason"] != REASON]


def _delta(conn, existing: dict, rows: list) -> tuple[str, list]:
    """(outcome, rows to add) for a sample this instrument already holds."""
    imported, other = _import_revisions(conn, existing["id"])
    k = len(imported)
    if k == 0 and existing["status"] != "raw_only":
        return ("present_not_imported" if rows else "already_imported"), []
    stored = [json.loads(r["results"]) for r in imported]
    wanted = [_values(r) for r in rows]
    if k > len(wanted) or stored != wanted[:k]:
        return "rows_changed", []
    if k == len(wanted):
        return "already_imported", []
    if other:
        return "delta_refused", []
    return "delta", rows[k:]


def _classify(conn, inst_id: str, item: _Item) -> tuple[str, dict]:
    """(outcome, info). Outcomes: new, delta, already_imported,
    present_not_imported, delta_refused, rows_changed, cross_instrument,
    already_conflict, conflict, key_taken, truncated. ``conn`` None = an
    empty store."""
    if item.problem is not None:
        return "truncated", {"problem": item.problem}
    if conn is None:
        if item.collision_with is not None:
            return "conflict", {"kept": item.collision_with}
        return "new", {}
    c = item.cdf
    if c is not None:
        known = pipeline._existing_result(c.sha256, inst_id, conn)
        if known is not None:
            if known.outcome == "duplicate":
                existing = store.samples.get(known.sample_id, db=conn)
                outcome, add = _delta(conn, existing, item.rows)
                return outcome, {"sample_id": existing["id"], "add": add}
            if known.outcome == "cross_instrument":
                return "cross_instrument", {"instrument_id": known.instrument_id,
                                            "sample_id": known.sample_id}
            return "already_conflict", {"conflict_id": known.conflict_id,
                                        "sample_id": known.sample_id}
        existing = store.samples.find_by_key(inst_id, item.lab_id, item.injection_dt, db=conn)
        if existing is not None:
            return "conflict", {"sample_id": existing["id"]}
        if item.collision_with is not None:     # the kept CDF was not imported
            return "collisions_unresolved", {"kept": item.collision_with}
        return "new", {}
    existing = store.samples.find_by_key(inst_id, item.lab_id, item.injection_dt, db=conn)
    if existing is not None and existing["cdf_sha256"] is None:
        outcome, add = _delta(conn, existing, item.rows)
        return outcome, {"sample_id": existing["id"], "add": add}
    if existing is not None:
        return "key_taken", {"sample_id": existing["id"]}
    legacy = store.samples.find_by_legacy(inst_id, item.lab_id, item.injection_dt, db=conn)
    if legacy:
        return "key_taken", {"sample_id": legacy[0]["id"]}
    return "new", {}


# ── writing ─────────────────────────────────────────────────────────────────

def _stage(src: Path, sha: str, data_dir: Path) -> Path:
    """Copy ``src`` into ``cdf/.incoming`` (hashing as it goes); ``ValueError``
    if the bytes are not the ones the matcher read."""
    incoming = data_dir / "cdf" / pipeline.INCOMING_DIR
    incoming.mkdir(parents=True, exist_ok=True)
    tmp = incoming / f"import-{uuid.uuid4().hex}.CDF"
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            for chunk in iter(lambda: fin.read(_CHUNK), b""):
                h.update(chunk)
                fout.write(chunk)
        if h.hexdigest() != sha:
            raise ValueError(f"{src} changed since it was read (sha256 differs)")
        st = os.stat(src)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _add_revisions(conn, sample_id: int, rows: list, *, csv_name: str, by: str) -> int:
    for r in rows:
        vals = _values(r)
        store.add_revision(
            conn, sample_id, json.dumps(vals), reason=REASON, by=by,
            corrections_used=LEGACY_CORRECTIONS, best_fit=(vals["Best Fit"] or None),
            fit_score=_fit_score(vals["Fit Score"]),
            notes={"import": {"csv": csv_name, "line_no": r.line_no}})
    return len(rows)


def _insert(conn, inst_id: str, item: _Item, data_dir: Path, *, csv_name: str, by: str,
            placed: list) -> int:
    """Insert one new sample and its revisions (inside a savepoint); the
    stored file is moved into place last. Returns the sample id."""
    c = item.cdf
    if c is None:
        sid = store.samples.insert_received(
            inst_id, item.lab_id, item.injection_dt, "csv", cdf_sha256=None, cdf_path=None,
            method_name=None, backfill=1, status=item.status, legacy_unverified=1,
            time_unverifiable=int(item.ms.time_unverifiable), db=conn)
        _add_revisions(conn, sid, item.rows, csv_name=csv_name, by=by)
        return sid
    sid = store.samples.insert_received(
        inst_id, item.lab_id, item.injection_dt, item.dt_source, cdf_sha256=c.sha256,
        cdf_path=None, method_name=item.method_name, source_name=Path(c.path).name,
        is_blank=item.is_blank, backfill=1, status=item.status, legacy_injection_dt=item.legacy,
        time_corrected=item.time_corrected, db=conn)
    month_dir = data_dir / "cdf" / inst_id / item.injection_dt[:4] / item.injection_dt[5:7]
    final = month_dir / f"{pipeline._safe_stem(item.lab_id)}_{sid}.CDF"
    store.samples.update(sid, cdf_path=pipeline._rel(final, data_dir), db=conn)
    _add_revisions(conn, sid, item.rows, csv_name=csv_name, by=by)
    month_dir.mkdir(parents=True, exist_ok=True)
    os.replace(item.staged, final)
    item.staged = None
    placed.append(final)
    return sid


def _hold_conflict(conn, inst_id: str, item: _Item, existing_id: int, data_dir: Path,
                   placed: list) -> int:
    """Store ``item``'s CDF as a conflict against sample ``existing_id`` (as
    ``submit`` does); the file is moved into place last. Returns the id."""
    c = item.cdf
    cdir = (data_dir / "cdf" / inst_id / "conflicts" / item.injection_dt[:4]
            / item.injection_dt[5:7])
    final = cdir / f"{pipeline._safe_stem(item.lab_id)}_{c.sha256[:12]}.CDF"
    cid = store.conflicts.add(inst_id, item.lab_id, item.injection_dt, existing_id, c.sha256,
                              pipeline._rel(final, data_dir), db=conn)
    cdir.mkdir(parents=True, exist_ok=True)
    os.replace(item.staged, final)
    item.staged = None
    placed.append(final)
    return cid


# ── the job ─────────────────────────────────────────────────────────────────

def _new_summary(instrument_id, processed_dir, results_csv, aliases, dry_run) -> dict:
    return {
        "instrument": instrument_id, "processed_dir": str(processed_dir),
        "results_csv": str(results_csv) if results_csv else None,
        "aliases": list(aliases), "dry_run": bool(dry_run), "store_checked": False,
        "started_at": store.now_iso(), "counts": {k: 0 for k in COUNT_KEYS},
        "method_names": {}, "method_names_per_folder": {}, "v1_misparse": {}, "matcher": {},
        "examples": {},
    }


class _Recorder:
    """Counts and examples; a batch's entries are merged only once it commits."""

    def __init__(self, summary: dict, limit: int):
        self.summary = summary
        self.limit = max(0, int(limit))

    def add(self, key: str, n: int = 1) -> None:
        self.summary["counts"][key] = self.summary["counts"].get(key, 0) + n

    def example(self, cls: str, entry: dict) -> None:
        ex = self.summary["examples"].setdefault(cls, [])
        if len(ex) < self.limit:
            ex.append(entry)


def import_history(instrument_id: str, processed_dir, results_csv, *, instrument_folder_aliases,
                   db: store.Db = None, data_dir=None, progress: Optional[Progress] = None,
                   dry_run: bool = False, conf: Optional[dict] = None, by: str = "import",
                   batch_size: int = BATCH_SIZE, examples: int = 20) -> dict:
    """Import one instrument's v1 history (see the module docstring)."""
    started = time.monotonic()
    aliases = ([instrument_folder_aliases] if isinstance(instrument_folder_aliases, str)
               else list(instrument_folder_aliases or []))
    root = Path(processed_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"not a folder: {root}")
    if results_csv is not None and not Path(results_csv).is_file():
        raise FileNotFoundError(f"no results CSV at {results_csv}")

    if data_dir is None and not (dry_run and db is None):
        data_dir = paths.data_dir()
        if data_dir is None:
            raise RuntimeError(f"{paths.DATA_ENV} is not set; the import needs the hub's data folder")
    data_dir = Path(data_dir) if data_dir is not None else None
    if db is None and data_dir is not None:
        db = data_dir / store.DB_FILENAME

    inst = None
    if db is not None:
        inst = store.instruments.get(instrument_id, db=db)
        if inst is None:
            raise pipeline.UnknownInstrument(f"unknown instrument {instrument_id!r}")
        if not inst.get("enabled", 1) and not dry_run:
            raise pipeline.InstrumentDisabled(f"instrument {instrument_id} is disabled")
    mm = (instruments.method_map(inst) if inst is not None
          else {methods.normalise_method_name(k): v
                for k, v in instruments.DEFAULT_METHOD_MAP.items()})
    if conf is None and not dry_run:
        conf = pipeline._load_conf()

    summary = _new_summary(instrument_id, root, results_csv, aliases, dry_run)
    summary["store_checked"] = db is not None
    rec = _Recorder(summary, examples)
    try:
        _run(summary, rec, inst_id=instrument_id, root=root, results_csv=results_csv,
             aliases=aliases, db=db, data_dir=data_dir, progress=progress, dry_run=dry_run,
             conf=conf or {}, by=by, batch_size=max(1, int(batch_size)), mm=mm)
    except BaseException as exc:
        summary["stopped"] = f"{type(exc).__name__}: {exc}"
        summary["seconds"] = round(time.monotonic() - started, 3)
        log.warning("import_history: stopped partway: %s", summary["stopped"])
        exc.import_summary = summary
        raise
    summary["seconds"] = round(time.monotonic() - started, 3)
    log.info("import_history: %s", _one_line(summary))
    _emit(progress, {"phase": "done", "summary": summary})
    return summary


def _run(summary, rec, *, inst_id, root, results_csv, aliases, db, data_dir, progress, dry_run,
         conf, by, batch_size, mm) -> None:
    _emit(progress, {"phase": "scan"})
    report = import_match.dry_run(
        root, results_csv, instrument_folder=aliases,
        on_progress=lambda done, total: _emit(progress, {"phase": "match", "done": done,
                                                         "total": total}))
    _report_matcher(summary, rec, report)

    ambiguous = {r.line_no for r, _chosen, _others in report.ambiguous_rows}
    items = []
    for ms in report.samples:
        rows = [r for r in ms.rows if r.line_no not in ambiguous]
        if ms.cdf is None and not rows:
            continue
        items.append(_plan(ms, rows, mm))
    for kept, other in report.key_collisions:
        it = _plan(import_match.MatchedSample(cdf=other, rows=[]), [], mm)
        it.collision_with = kept.path
        items.append(it)

    cdf_items = [it for it in items if it.cdf is not None]
    for n, it in enumerate(cdf_items, 1):
        it.problem = pipeline.cdf_problem(it.cdf.path)
        if n % 500 == 0 or n == len(cdf_items):
            _emit(progress, {"phase": "check", "done": n, "total": len(cdf_items)})

    total = len(items)
    csv_name = str(results_csv) if results_csv else ""
    done = 0
    for start in range(0, total, batch_size):
        batch = items[start:start + batch_size]
        if dry_run or db is None:
            conn_ctx = store.connection(db) if db is not None else None
            try:
                conn = conn_ctx.__enter__() if conn_ctx is not None else None
                for item in batch:
                    outcome, info = _classify(conn, inst_id, item)
                    _record(rec, item, outcome, info)
                    done += 1
                    _emit(progress, {"phase": "import", "done": done, "total": total,
                                     "outcome": outcome, "lab_id": item.lab_id})
            finally:
                if conn_ctx is not None:
                    conn_ctx.__exit__(None, None, None)
            continue
        done = _write_batch(batch, rec, inst_id=inst_id, db=db, data_dir=data_dir, conf=conf,
                            by=by, csv_name=csv_name, progress=progress, done=done, total=total)
        _emit(progress, {"phase": "commit", "done": done, "total": total})


def _prepare(batch, inst_id, db, data_dir, conf) -> None:
    """Outside any transaction: stage the files of the samples that look new."""
    with store.connection(db) as conn:
        for item in batch:
            outcome, _info = _classify(conn, inst_id, item)
            # a collision's kept CDF may be inserted earlier in this same batch
            if outcome in ("new", "conflict", "collisions_unresolved") and item.cdf is not None:
                try:
                    if outcome == "new":
                        item.is_blank = pipeline._genuine_blank(Path(item.cdf.path), item.lab_id,
                                                                conf)
                    item.staged = _stage(Path(item.cdf.path), item.cdf.sha256, data_dir)
                except Exception as exc:  # noqa: BLE001 - recorded per sample
                    item.extra["stage_error"] = f"{type(exc).__name__}: {exc}"


def _write_batch(batch, rec, *, inst_id, db, data_dir, conf, by, csv_name, progress, done, total):
    _prepare(batch, inst_id, db, data_dir, conf)
    placed: list = []
    pending: list = []
    try:
        with store.connection(db) as conn:
            with store.write_txn(conn):
                for item in batch:
                    outcome, info = _apply_one(conn, inst_id, item, data_dir, csv_name=csv_name,
                                               by=by, placed=placed)
                    pending.append((item, outcome, info))
                    done += 1
                    _emit(progress, {"phase": "import", "done": done, "total": total,
                                     "outcome": outcome, "lab_id": item.lab_id})
    except BaseException:
        for p in placed:
            try:
                p.unlink()
            except OSError:
                log.warning("import_history: could not remove %s", p)
        raise
    finally:
        for item in batch:
            if item.staged is not None:
                item.staged.unlink(missing_ok=True)
                item.staged = None
    for item, outcome, info in pending:
        _record(rec, item, outcome, info)
    return done


def _apply_one(conn, inst_id, item: _Item, data_dir: Path, *, csv_name, by, placed) -> tuple:
    if "stage_error" in item.extra:
        return "failed", {"error": item.extra["stage_error"]}
    try:
        with store.write_txn(conn):             # a SAVEPOINT
            outcome, info = _classify(conn, inst_id, item)
            if outcome == "new":
                if item.cdf is not None and item.staged is None:
                    raise RuntimeError("the file was not staged")
                info["sample_id"] = _insert(conn, inst_id, item, data_dir, csv_name=csv_name,
                                            by=by, placed=placed)
            elif outcome == "delta":
                raise NotImplementedError("delta")
            elif outcome == "conflict":
                if item.staged is None:
                    raise RuntimeError("the file was not staged")
                info["conflict_id"] = _hold_conflict(conn, inst_id, item, info["sample_id"],
                                                     data_dir, placed)
            return outcome, info
    except Exception as exc:  # noqa: BLE001 - recorded per sample, the batch goes on
        log.warning("import_history: %s @ %s failed: %s", item.lab_id, item.injection_dt, exc)
        return "failed", {"error": f"{type(exc).__name__}: {exc}"}


_OUTCOME_KEYS = {"conflict": "conflicts", "already_conflict": "conflicts_existing",
                 "delta": "already_imported"}


def _record(rec: _Recorder, item: _Item, outcome: str, info: dict) -> None:
    c = item.cdf
    brief = {"lab_id": item.ms.lab_id_display, "injection_dt": item.injection_dt,
             "cdf": c.path if c is not None else None, "rows": [r.line_no for r in item.rows]}
    if outcome == "new":
        rec.add("imported")
        rec.add(item.status)
        rec.add("revisions", len(item.rows))
        if c is None:
            rec.add("result_only")
            rec.example("result_only", brief)
            if item.ms.time_unverifiable:
                rec.add("time_unverifiable")
                rec.example("time_unverifiable", brief)
        else:
            rec.add("attached" if item.rows else "orphans")
            rec.example("attached" if item.rows else "orphans", brief)
            if item.time_corrected:
                rec.add("time_corrected")
                rec.example("time_corrected", dict(brief, legacy_injection_dt=item.legacy))
            if item.dt_source == "mtime":
                rec.add("no_injection_time")
            if item.is_blank:
                rec.add("blanks")
        if item.status in ("other_method", "review_method"):
            rec.example(item.status, dict(brief, method_name=item.method_name))
        return
    key = _OUTCOME_KEYS.get(outcome, outcome)
    rec.add(key)
    if item.collision_with is not None:
        brief["kept"] = item.collision_with
    rec.example(key, dict(brief, **{k: v for k, v in info.items() if k != "add"}))
    rec.add("rows_not_imported", len(item.rows))


def _report_matcher(summary: dict, rec: _Recorder, report) -> None:
    st = report.stats
    c = summary["counts"]
    c["cdf_files"] = st.get("files_seen", 0)
    c["cdfs_read"] = st.get("cdfs", 0)
    c["cdf_errors"] = len(st.get("cdf_errors", []))
    c["dup_sha"] = len(report.dup_sha)
    c["csv_rows"] = st.get("rows", 0)
    c["key_collisions"] = len(report.key_collisions)
    c["ambiguous_rows"] = len(report.ambiguous_rows)
    c["held_rows"] = len(report.held_rows)
    c["mixed_rows"] = len(report.mixed_rows)
    keyless = [r for r in report.unmatched_rows
               if not (import_match.normalise_lab_id(r.lab_id) and r.injection_dt_raw)]
    c["unmatched_without_key"] = len(keyless)
    c["name_from_filename"] = len(st.get("name_from_filename", []))
    for e in st.get("cdf_errors", []):
        rec.example("cdf_errors", {"cdf": e[0], "error": e[1]})
    for kept, dup in report.dup_sha:
        rec.example("dup_sha", {"kept": kept, "duplicate": dup})
    for r, chosen, others in report.ambiguous_rows:
        rec.example("ambiguous_rows", _row_brief(r, chosen=chosen.path,
                                                 others=[o.path for o in others]))
    for r in report.held_rows:
        rec.example("held_rows", _row_brief(r))
    for r in report.mixed_rows:
        rec.example("mixed_rows", _row_brief(r))
    for r in keyless:
        rec.example("unmatched_without_key", _row_brief(r))
    summary["method_names"] = st.get("method_names", {})
    summary["method_names_per_folder"] = st.get("method_names_per_folder", {})
    summary["v1_misparse"] = {
        "v1_misparsed_cdfs": st.get("v1_misparsed_cdfs", 0),
        "rows_matched_via_v1_form": st.get("rows_matched_via_v1_form", 0),
        "v1_family_matches": st.get("v1_family_matches", {}),
        "result_only_time_unverifiable": st.get("result_only_time_unverifiable", 0),
        "examples": st.get("v1_misparsed_examples", []),
    }


def _one_line(summary: dict) -> str:
    c = summary["counts"]
    return (f"{summary['instrument']}: {c['imported']} imported ({c['attached']} attached, "
            f"{c['orphans']} orphan, {c['result_only']} result-only), {c['revisions']} revisions, "
            f"{c['already_imported']} already imported, {c['conflicts']} conflicts, "
            f"{c['cross_instrument']} cross-instrument, {c['truncated']} truncated, "
            f"{c['failed']} failed")


def format_summary(summary: dict, *, examples: int = 5) -> str:
    """The summary as text."""
    return _one_line(summary) + "\n"
