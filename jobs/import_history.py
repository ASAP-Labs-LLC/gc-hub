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
not a finite number), ``notes`` ``{"import": {"csv", "csv_sha256",
"line_no", "run_id"}}``, and the
sample's CDF (NULL for result-only). Since the values stay strings,
``exports.format_line(results, source_file)`` reproduces v1's line byte for
byte, ``Source File`` aside (the hub passes its relative ``cdf_path``).
Consumers of ``sample_results.results`` must therefore accept strings as
well as numbers. Lines the hub writes (release, Export to
LIMS, a fresh export file) put the sample's corrected ``injection_dt`` in
``InjectionDateTime``; the stored revision keeps v1's string.

Identity and times
==================

``lab_id`` is ``import_match``'s identity form (the pipeline's rule).
``injection_dt`` is the CDF's **correct** time; for a CDF with no stamp it is
the file's mtime **truncated to the whole second** (the hub's rule, so a
live copy of the same file collides with the imported one).
``legacy_injection_dt`` is what v1 wrote (``CdfMeta.legacy_injection_dt``:
the 3.11+ misparse, or the unrounded mtime) and ``time_corrected`` is set when
the two differ; when rows are attached it is the time the current row
actually holds. ``is_blank`` is decided as ``pipeline.submit`` does (a blank
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

A second run over the same inputs writes nothing. Per sample:

* a file this instrument already holds is not imported again; rows the CSV
  has gained since are added as further ``import`` revisions (``delta``)
  only when the stored import revisions are exactly the first rows (same
  values, same CSV path, same line numbers; else ``rows_changed``), no new
  row's line is held by another sample's import revision (``rows_moved``),
  and the hub has not revised the sample (``delta_refused``). An imported
  orphan that gains rows takes its status by the method rule. A sample the
  hub itself received (no import revision) never gets v1 rows
  (``present_not_imported``). **Re-runs must use the same CSV path.**
* a CDF whose imported **result-only** sample exists (same lab ID; its CSV
  time is the CDF's correct time or any v1 form) upgrades that sample in
  place through ``pipeline.attach_cdf_to_result_only`` (shared with
  ``pipeline.submit``), adding the rows appended since, when its stored
  rows are a prefix of the CDF's rows (``attached_to_result_only``; else
  ``attach_refused``);
* a result-only key already held (or a CDF sample whose correct or legacy
  time is that CSV time) is not duplicated (``key_taken``);
* conflicts are found by sha;
* stored import revisions no longer found at their line of the CSV are
  counted (``rows_vanished``) and listed; nothing is deleted.

Rows are also held back when their result-only time is not
``YYYY-MM-DD HH:MM:SS`` (``noncanonical_time``) or their Source File names
a CDF that could not be read (``rows_of_unreadable_cdfs``). A row whose Lab
ID is whitespace only is matched by time to the one orphan CDF whose sample
name is whitespace only (``whitespace_matched``).

Files are staged in ``cdf/.incoming`` outside any transaction (hashed while
copied, with a fresh mtime so ``pipeline.sweep_incoming`` leaves them; the
original mtime is set once the file is in place). Then one ``write_txn`` per
``batch_size`` samples (default 500), each sample in its own SAVEPOINT (a
failure is recorded and the batch goes on), the stored file moved into place
as its last step. **Progress events are buffered and sent after the batch
commits**, so the callback never runs under the write lock. If a batch fails
to commit, that batch rolls back and the files it placed are removed; if
``progress`` raises (the admin page's pause/cancel), the run ends after the
batch just committed. Either way the summary so far is attached to the
exception as ``exc.import_summary``, the run is recorded as stopped, and it
is re-raised; a re-run resumes.

Every real run is recorded in ``store.import_runs`` (who, when, sources
with the CSV's sha256, counts); each revision's ``notes.import`` holds
``csv``, ``csv_sha256``, ``line_no`` and ``run_id``.

API
===

::

    import_history(instrument_id, processed_dir, results_csv, *,
                   instrument_folder_aliases, db=None, data_dir=None,
                   progress=None, dry_run=False, conf=None, by="import",
                   batch_size=500, examples=20, compare_dirs=()) -> dict
    format_summary(summary, *, examples=5) -> str

``results_csv=None`` imports the CDFs alone (all orphans). ``conf`` is the
global settings (only ``blank_max_intensity_pa`` is read; default
``settings.load_settings()``). ``compare_dirs``: other instruments'
processed folders; files with the same bytes are reported
(``same_file_elsewhere``, D2). ``dry_run=True`` writes nothing: with a
``db`` it classifies against the store (read only); with ``db=None`` and no
``data_dir`` it assumes an empty store and ``instruments.DEFAULT_METHOD_MAP``.

``progress(event)`` receives dicts for the admin page's SSE stream:
``{"phase": "scan"}``; ``{"phase": "match", "done", "total"}`` (CDF
metadata, every 500 files); ``{"phase": "check", "done", "total"}`` (the
truncation check); ``{"phase": "import", "done", "total", "outcome",
"lab_id"}`` per sample and ``{"phase": "commit", "done", "total"}`` per batch
(both sent after the batch has committed);
``{"phase": "done", "summary"}``.

The summary is JSON-ready: ``instrument``, ``processed_dir``,
``results_csv``, ``csv_sha256``, ``run_id``, ``aliases``, ``dry_run``, ``store_checked``,
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
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
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
    "whitespace_only_names", "whitespace_matched", "name_from_filename",
    "attached_to_result_only", "attach_refused", "rows_moved", "rows_vanished",
    "noncanonical_time", "rows_of_unreadable_cdfs", "same_file_elsewhere",
)


def _emit(progress: Optional[Progress], event: dict) -> None:
    if progress is not None:
        progress(event)


def _whole_second(dt: str) -> str:
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

_CANON_CSV_DT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _canonical_csv_time(dt: str) -> bool:
    """``YYYY-MM-DD HH:MM:SS`` and a real date/time (what v1 wrote from a CDF stamp)."""
    if not _CANON_CSV_DT.match(dt or ""):
        return False
    try:
        datetime.fromisoformat(dt)
    except ValueError:
        return False
    return True


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


@dataclass
class _Ctx:
    """What every sample of one run shares."""
    inst_id: str
    csv_name: str = ""
    csv_sha256: Optional[str] = None
    run_id: Optional[int] = None
    by: str = "import"
    data_dir: Optional[Path] = None
    conf: dict = field(default_factory=dict)
    # (csv, line_no) -> sample id, for every import revision already stored
    claimed: dict = field(default_factory=dict)


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
    # v1's string: what the (current) row actually holds, else the 3.11+ form
    legacy = rows[-1].injection_dt_raw if rows else (c.legacy_injection_dt or c.injection_dt)
    return _Item(ms=ms, rows=rows, lab_id=ms.lab_id, injection_dt=inj, dt_source=c.dt_source,
                 legacy=legacy, time_corrected=int(legacy != inj), method_name=c.method_name,
                 status=_method_status(c.method_name, mm, bool(rows)))


def _cdf_forms(item: _Item) -> list:
    c = item.cdf
    return pipeline.v1_time_forms(c.raw_stamp, item.injection_dt, item.legacy, c.injection_dt,
                                  c.legacy_injection_dt, *c.v1_injection_dts)


# ── classification against the store ───────────────────────────────────────

def _note(rev: dict) -> dict:
    try:
        return (json.loads(rev["notes"] or "{}") or {}).get("import") or {}
    except (TypeError, ValueError):
        return {}


def _prefix(conn, ctx: _Ctx, existing: dict, rows: list) -> tuple:
    """(outcome, rows to add) for rows of a sample the store already holds.

    The stored ``import`` revisions must be exactly the first rows: same
    values, same CSV and same line numbers (``rows_changed`` otherwise). A new
    row whose line another sample's import revision already claims is
    ``rows_moved``. A sample the hub itself received (no import revision, not
    ``raw_only``) gets no v1 rows (``present_not_imported``); one the hub has
    revised since gets no more (``delta_refused``)."""
    revs = store.list_revisions(existing["id"], db=conn)
    imported = [r for r in revs if r["reason"] == REASON]
    other = [r for r in revs if r["reason"] != REASON]
    k = len(imported)
    if k == 0 and existing["status"] != "raw_only" and existing["cdf_sha256"] is not None:
        return ("present_not_imported" if rows else "already_imported"), []
    if k > len(rows):
        return "rows_changed", []
    for rev, r in zip(imported, rows):
        note = _note(rev)
        if (json.loads(rev["results"]) != _values(r) or note.get("line_no") != r.line_no
                or note.get("csv") != ctx.csv_name):
            return "rows_changed", []
    if k == len(rows):
        return "already_imported", []
    add = rows[k:]
    if any(ctx.claimed.get((ctx.csv_name, r.line_no), existing["id"]) != existing["id"]
           for r in add):
        return "rows_moved", []
    if other:
        return "delta_refused", []
    return "delta", add


def _moved(ctx: _Ctx, rows: list) -> list:
    """Rows of a new sample that an earlier run stored on another sample."""
    return [r.line_no for r in rows if (ctx.csv_name, r.line_no) in ctx.claimed]


def _classify(conn, ctx: _Ctx, item: _Item) -> tuple:
    """(outcome, info). Outcomes: new, attach, attach_refused, delta,
    already_imported, present_not_imported, delta_refused, rows_changed,
    rows_moved, cross_instrument, already_conflict, conflict,
    collisions_unresolved, key_taken, truncated. ``conn`` None = an empty
    store."""
    if item.problem is not None:
        return "truncated", {"problem": item.problem}
    if conn is None:
        if item.collision_with is not None:
            return "conflict", {"kept": item.collision_with}
        return "new", {}
    inst_id = ctx.inst_id
    c = item.cdf
    if c is not None:
        known = pipeline.existing_result(c.sha256, inst_id, conn)
        if known is not None:
            if known.outcome == "duplicate":
                existing = store.samples.get(known.sample_id, db=conn)
                outcome, add = _prefix(conn, ctx, existing, item.rows)
                return outcome, {"sample_id": existing["id"], "add": add}
            if known.outcome == "cross_instrument":
                return "cross_instrument", {"instrument_id": known.instrument_id,
                                            "sample_id": known.sample_id}
            return "already_conflict", {"conflict_id": known.conflict_id,
                                        "sample_id": known.sample_id}
        existing = store.samples.find_by_key(inst_id, item.lab_id, item.injection_dt, db=conn)
        if existing is not None and existing["cdf_sha256"] is None and existing["legacy_unverified"]:
            ro = existing
        elif existing is not None:
            return "conflict", {"sample_id": existing["id"]}
        else:
            ro = pipeline.find_result_only(conn, inst_id, item.lab_id, _cdf_forms(item))
        if ro is not None:
            if item.collision_with is not None:
                return "collisions_unresolved", {"kept": item.collision_with,
                                                 "result_only_sample_id": ro["id"]}
            outcome, add = _prefix(conn, ctx, ro, item.rows)
            if outcome in ("already_imported", "delta"):
                return "attach", {"sample_id": ro["id"], "add": add}
            return "attach_refused", {"sample_id": ro["id"], "reason": outcome}
        if item.collision_with is not None:     # the kept CDF was not imported
            return "collisions_unresolved", {"kept": item.collision_with}
        moved = _moved(ctx, item.rows)
        if moved:
            return "rows_moved", {"lines": moved}
        return "new", {}
    existing = store.samples.find_by_key(inst_id, item.lab_id, item.injection_dt, db=conn)
    if existing is not None and existing["cdf_sha256"] is None:
        outcome, add = _prefix(conn, ctx, existing, item.rows)
        return outcome, {"sample_id": existing["id"], "add": add}
    if existing is not None:
        return "key_taken", {"sample_id": existing["id"]}
    legacy = store.samples.find_by_legacy(inst_id, item.lab_id, item.injection_dt, db=conn)
    if legacy:
        return "key_taken", {"sample_id": legacy[0]["id"]}
    moved = _moved(ctx, item.rows)
    if moved:
        return "rows_moved", {"lines": moved}
    return "new", {}


# ── writing ─────────────────────────────────────────────────────────────────

def _stage(src: Path, sha: str, data_dir: Path) -> Path:
    """Copy ``src`` into ``cdf/.incoming`` (hashing as it goes); ``ValueError``
    if the bytes are not the ones the matcher read. The copy keeps a fresh
    mtime (``pipeline.sweep_incoming`` only removes old files); the original
    mtime is set once the file is in its final place."""
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
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _place(item: _Item, final: Path, placed: list) -> None:
    """Move the staged copy to ``final`` and give it the original's mtime."""
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(item.staged, final)
    item.staged = None
    placed.append(final)
    ns = item.extra.get("mtime_ns")
    if ns is not None:
        os.utime(final, ns=(ns, ns))


def _add_revisions(conn, ctx: _Ctx, sample_id: int, rows: list) -> int:
    for r in rows:
        vals = _values(r)
        store.add_revision(
            conn, sample_id, json.dumps(vals), reason=REASON, by=ctx.by,
            corrections_used=LEGACY_CORRECTIONS, best_fit=(vals["Best Fit"] or None),
            fit_score=_fit_score(vals["Fit Score"]),
            notes={"import": {"csv": ctx.csv_name, "csv_sha256": ctx.csv_sha256,
                              "line_no": r.line_no, "run_id": ctx.run_id}})
    return len(rows)


def _month_file(ctx: _Ctx, item: _Item, sid: int) -> Path:
    return (ctx.data_dir / "cdf" / ctx.inst_id / item.injection_dt[:4] / item.injection_dt[5:7]
            / f"{pipeline.safe_stem(item.lab_id)}_{sid}.CDF")


def _insert(conn, ctx: _Ctx, item: _Item, placed: list) -> int:
    """Insert one new sample and its revisions (inside a savepoint); the
    stored file is moved into place last. Returns the sample id."""
    c = item.cdf
    if c is None:
        sid = store.samples.insert_received(
            ctx.inst_id, item.lab_id, item.injection_dt, "csv", cdf_sha256=None, cdf_path=None,
            method_name=None, backfill=1, status=item.status, legacy_unverified=1,
            time_unverifiable=int(item.ms.time_unverifiable), db=conn)
        _add_revisions(conn, ctx, sid, item.rows)
        return sid
    sid = store.samples.insert_received(
        ctx.inst_id, item.lab_id, item.injection_dt, item.dt_source, cdf_sha256=c.sha256,
        cdf_path=None, method_name=item.method_name, source_name=Path(c.path).name,
        is_blank=item.is_blank, backfill=1, status=item.status, legacy_injection_dt=item.legacy,
        time_corrected=item.time_corrected, db=conn)
    final = _month_file(ctx, item, sid)
    store.samples.update(sid, cdf_path=pipeline.rel_path(final, ctx.data_dir), db=conn)
    _add_revisions(conn, ctx, sid, item.rows)
    _place(item, final, placed)
    return sid


def _attach(conn, ctx: _Ctx, item: _Item, sid: int, add: list, placed: list) -> None:
    """The CDF of an imported result-only sample: ``pipeline.attach_cdf_to_result_only``
    (the routine ``submit`` uses), plus the rows appended since; no recompute."""
    c = item.cdf
    final = _month_file(ctx, item, sid)
    pipeline.attach_cdf_to_result_only(
        conn, sid, cdf_sha256=c.sha256, cdf_path=pipeline.rel_path(final, ctx.data_dir),
        method_name=item.method_name, injection_dt=item.injection_dt,
        injection_dt_source=item.dt_source, source_name=Path(c.path).name,
        is_blank=item.is_blank, legacy_injection_dt=item.legacy, status=item.status)
    _add_revisions(conn, ctx, sid, add)
    _place(item, final, placed)


def _hold_conflict(conn, ctx: _Ctx, item: _Item, existing_id: int, placed: list) -> int:
    """Store ``item``'s CDF as a conflict against sample ``existing_id`` (as
    ``submit`` does); the file is moved into place last. Returns the id."""
    c = item.cdf
    final = (ctx.data_dir / "cdf" / ctx.inst_id / "conflicts" / item.injection_dt[:4]
             / item.injection_dt[5:7] / f"{pipeline.safe_stem(item.lab_id)}_{c.sha256[:12]}.CDF")
    cid = store.conflicts.add(ctx.inst_id, item.lab_id, item.injection_dt, existing_id, c.sha256,
                              pipeline.rel_path(final, ctx.data_dir), db=conn)
    _place(item, final, placed)
    return cid


# ── the job ─────────────────────────────────────────────────────────────────

def _new_summary(instrument_id, processed_dir, results_csv, aliases, dry_run) -> dict:
    return {
        "instrument": instrument_id, "processed_dir": str(processed_dir),
        "results_csv": str(results_csv) if results_csv else None, "csv_sha256": None,
        "run_id": None, "aliases": list(aliases), "dry_run": bool(dry_run),
        "store_checked": False, "started_at": store.now_iso(),
        "counts": {k: 0 for k in COUNT_KEYS}, "method_names": {}, "method_names_per_folder": {},
        "v1_misparse": {}, "matcher": {}, "examples": {},
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


def _file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def import_history(instrument_id: str, processed_dir, results_csv, *, instrument_folder_aliases,
                   db: store.Db = None, data_dir=None, progress: Optional[Progress] = None,
                   dry_run: bool = False, conf: Optional[dict] = None, by: str = "import",
                   batch_size: int = BATCH_SIZE, examples: int = 20,
                   compare_dirs=()) -> dict:
    """Import one instrument's v1 history (see the module docstring)."""
    started = time.monotonic()
    aliases = ([instrument_folder_aliases] if isinstance(instrument_folder_aliases, str)
               else list(instrument_folder_aliases or []))
    root = Path(processed_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"not a folder: {root}")
    if results_csv is not None and not Path(results_csv).is_file():
        raise FileNotFoundError(f"no results CSV at {results_csv}")

    if data_dir is None and not dry_run:
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
        conf = pipeline.load_conf()

    summary = _new_summary(instrument_id, root, results_csv, aliases, dry_run)
    summary["store_checked"] = db is not None
    csv_sha = _file_sha256(results_csv) if results_csv else None
    summary["csv_sha256"] = csv_sha
    ctx = _Ctx(inst_id=instrument_id, csv_name=str(results_csv) if results_csv else "",
               csv_sha256=csv_sha, by=by, data_dir=data_dir, conf=conf or {})
    if not dry_run:
        ctx.run_id = store.import_runs.start(instrument_id, by=by, sources={
            "processed_dir": str(root), "results_csv": ctx.csv_name or None,
            "csv_sha256": csv_sha, "aliases": aliases,
            "compare_dirs": [str(d) for d in compare_dirs]}, db=db)
        summary["run_id"] = ctx.run_id
    rec = _Recorder(summary, examples)
    try:
        _run(summary, rec, ctx, root=root, results_csv=results_csv, aliases=aliases, db=db,
             progress=progress, dry_run=dry_run, batch_size=max(1, int(batch_size)), mm=mm,
             compare_dirs=compare_dirs)
    except BaseException as exc:
        summary["stopped"] = f"{type(exc).__name__}: {exc}"
        summary["seconds"] = round(time.monotonic() - started, 3)
        log.warning("import_history: stopped partway: %s", summary["stopped"])
        if ctx.run_id is not None:
            try:
                store.import_runs.finish(ctx.run_id, summary["counts"], stopped=summary["stopped"],
                                         db=db)
            except Exception:  # noqa: BLE001 - the original error matters more
                log.exception("import_history: could not record the stopped run")
        exc.import_summary = summary
        raise
    summary["seconds"] = round(time.monotonic() - started, 3)
    if ctx.run_id is not None:
        store.import_runs.finish(ctx.run_id, summary["counts"], db=db)
    log.info("import_history: %s", _one_line(summary))
    _emit(progress, {"phase": "done", "summary": summary})
    return summary


def _match_whitespace_names(report, rec: _Recorder) -> list:
    """Rows whose Lab ID is whitespace only (v1 wrote a blank-but-spaced sample
    name verbatim) matched by time to the one orphan CDF with such a name;
    returns the rows that stay keyless."""
    keyless = [r for r in report.unmatched_rows
               if not (import_match.normalise_lab_id(r.lab_id) and r.injection_dt_raw)]
    blank_named = set(report.stats.get("name_from_filename", []))
    cands = []
    for ms in report.samples:
        if ms.cdf is None or ms.rows or ms.cdf.path not in blank_named:
            continue
        try:
            name = distill.read_cdf_names(Path(ms.cdf.path))[0]
        except Exception:  # noqa: BLE001
            continue
        if name and not name.strip():
            c = ms.cdf
            cands.append((ms, set(pipeline.v1_time_forms(c.raw_stamp, c.injection_dt,
                                                         c.legacy_injection_dt,
                                                         *c.v1_injection_dts))))
    left = []
    for r in keyless:
        raw_lab = r.values.get("Lab ID", "")
        hits = [ms for ms, forms in cands if r.injection_dt_raw in forms]
        if raw_lab and not raw_lab.strip() and r.injection_dt_raw and len(hits) == 1:
            hits[0].rows.append(r)
            hits[0].rows.sort(key=lambda x: x.line_no)
            rec.add("whitespace_matched")
            rec.example("whitespace_matched", _row_brief(r, cdf=hits[0].cdf.path))
        else:
            left.append(r)
    return left


def _norm_base(p: str) -> str:
    return (p or "").strip().replace("/", "\\").rsplit("\\", 1)[-1].casefold()


def _load_claims(conn, ctx: _Ctx) -> list:
    """Every stored import revision of this instrument: fills ``ctx.claimed``
    and returns ``[(sample_id, revision, note, results)]``."""
    out = []
    for r in conn.execute(
            "SELECT r.sample_id, r.revision, r.notes, r.results FROM sample_results r "
            "JOIN samples s ON s.id=r.sample_id WHERE s.instrument_id=? AND r.reason=?",
            (ctx.inst_id, REASON)):
        note = _note(r)
        if note.get("line_no") is not None:
            ctx.claimed[(note.get("csv") or "", note["line_no"])] = r["sample_id"]
        out.append((r["sample_id"], r["revision"], note, r["results"]))
    return out


def _run(summary, rec, ctx: _Ctx, *, root, results_csv, aliases, db, progress, dry_run,
         batch_size, mm, compare_dirs) -> None:
    _emit(progress, {"phase": "scan"})
    report = import_match.dry_run(
        root, results_csv, instrument_folder=aliases,
        on_progress=lambda done, total: _emit(progress, {"phase": "match", "done": done,
                                                         "total": total}))
    keyless = _match_whitespace_names(report, rec)
    _report_matcher(summary, rec, report, keyless)

    # every CSV row by line, to spot stored rows that have vanished (I4)
    csv_rows = {}
    for ms in report.samples:
        for r in ms.rows:
            csv_rows[r.line_no] = r
    for r in (*report.unmatched_rows, *report.mixed_rows, *report.held_rows):
        csv_rows.setdefault(r.line_no, r)
    if db is not None:
        with store.connection(db) as conn:
            stored = _load_claims(conn, ctx)
        for sid, revno, note, res in stored:
            if (note.get("csv") or "") != ctx.csv_name or not ctx.csv_name:
                continue
            now = csv_rows.get(note.get("line_no"))
            if now is None or _values(now) != json.loads(res):
                rec.add("rows_vanished")
                old = json.loads(res)
                rec.example("rows_vanished", {"sample_id": sid, "revision": revno,
                                              "line_no": note.get("line_no"),
                                              "lab_id": old.get("Lab ID"),
                                              "injection_dt": old.get("InjectionDateTime")})

    unreadable = {_norm_base(p): p for p, _err in report.stats.get("cdf_errors", [])}
    ambiguous = {r.line_no for r, _chosen, _others in report.ambiguous_rows}
    items = []
    for ms in report.samples:
        rows = [r for r in ms.rows if r.line_no not in ambiguous]
        if ms.cdf is None:
            keep = []
            for r in rows:
                if r.source_file and _norm_base(r.source_file) in unreadable:
                    rec.add("rows_of_unreadable_cdfs")
                    rec.example("rows_of_unreadable_cdfs",
                                _row_brief(r, cdf=unreadable[_norm_base(r.source_file)]))
                else:
                    keep.append(r)
            rows = keep
            if rows and not _canonical_csv_time(ms.injection_dt):
                for r in rows:
                    rec.add("noncanonical_time")
                    rec.example("noncanonical_time", _row_brief(r))
                continue
            if not rows:
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
    if compare_dirs:
        _compare(rec, cdf_items, compare_dirs)

    total = len(items)
    done = 0
    predicted_new: set = set()          # dry run: CDF paths it would import
    for start in range(0, total, batch_size):
        batch = items[start:start + batch_size]
        if dry_run or db is None:
            done = _dry_batch(batch, rec, ctx, db=db, progress=progress, done=done, total=total,
                              predicted_new=predicted_new)
            continue
        done = _write_batch(batch, rec, ctx, db=db, progress=progress, done=done, total=total)


def _compare(rec: _Recorder, cdf_items: list, compare_dirs) -> None:
    """D2: files with the same bytes in another instrument's folder."""
    ours = {it.cdf.sha256: it.cdf.path for it in cdf_items}
    for d in compare_dirs:
        for p in import_match.iter_cdf_paths(d):
            try:
                sha = _file_sha256(p)
            except OSError:
                continue
            if sha in ours:
                rec.add("same_file_elsewhere")
                rec.example("same_file_elsewhere", {"cdf": ours[sha], "other": str(p)})


def _dry_batch(batch, rec, ctx: _Ctx, *, db, progress, done, total, predicted_new) -> int:
    """Classify without writing (``db`` None: an empty store). A collision's
    kept CDF that this run would import makes the other a conflict."""
    with (store.connection(db) if db is not None else _nullcontext()) as conn:
        for item in batch:
            outcome, info = _classify(conn, ctx, item)
            if item.collision_with is not None and outcome in ("conflict", "collisions_unresolved"):
                if conn is None or outcome == "collisions_unresolved":
                    if item.collision_with in predicted_new:
                        outcome, info = "conflict", {"kept": item.collision_with}
                    else:
                        outcome, info = "collisions_unresolved", {"kept": item.collision_with}
            if outcome in ("new", "attach") and item.cdf is not None:
                predicted_new.add(item.cdf.path)
                try:
                    item.is_blank = pipeline.genuine_blank(Path(item.cdf.path), item.lab_id,
                                                           ctx.conf)
                except Exception:  # noqa: BLE001 - the real run records it
                    item.is_blank = 0
            _record(rec, item, outcome, info)
            done += 1
            _emit(progress, {"phase": "import", "done": done, "total": total,
                             "outcome": outcome, "lab_id": item.lab_id})
    return done


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


_STAGED_OUTCOMES = ("new", "attach", "conflict", "collisions_unresolved")


def _prepare(batch, ctx: _Ctx, db) -> None:
    """Outside any transaction: stage the files of the samples that will need one."""
    with store.connection(db) as conn:
        for item in batch:
            outcome, _info = _classify(conn, ctx, item)
            # a collision's kept CDF may be inserted earlier in this same batch
            if outcome in _STAGED_OUTCOMES and item.cdf is not None:
                try:
                    if outcome in ("new", "attach"):
                        item.is_blank = pipeline.genuine_blank(Path(item.cdf.path), item.lab_id,
                                                               ctx.conf)
                    item.extra["mtime_ns"] = os.stat(item.cdf.path).st_mtime_ns
                    item.staged = _stage(Path(item.cdf.path), item.cdf.sha256, ctx.data_dir)
                except Exception as exc:  # noqa: BLE001 - recorded per sample
                    item.extra["stage_error"] = f"{type(exc).__name__}: {exc}"


def _write_batch(batch, rec, ctx: _Ctx, *, db, progress, done, total) -> int:
    """One transaction for the batch. Progress events are buffered and sent
    only after the commit, so the callback never runs under the write lock."""
    _prepare(batch, ctx, db)
    placed: list = []
    pending: list = []
    try:
        with store.connection(db) as conn:
            with store.write_txn(conn):
                for item in batch:
                    pending.append((item, *_apply_one(conn, ctx, item, placed)))
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
    events = []
    for item, outcome, info in pending:
        _record(rec, item, outcome, info)
        done += 1
        events.append({"phase": "import", "done": done, "total": total, "outcome": outcome,
                       "lab_id": item.lab_id})
    for e in events:
        _emit(progress, e)
    _emit(progress, {"phase": "commit", "done": done, "total": total})
    return done


def _apply_one(conn, ctx: _Ctx, item: _Item, placed: list) -> tuple:
    if "stage_error" in item.extra:
        return "failed", {"error": item.extra["stage_error"]}
    try:
        with store.write_txn(conn):             # a SAVEPOINT
            outcome, info = _classify(conn, ctx, item)
            if outcome in _STAGED_OUTCOMES[:3] and item.cdf is not None and item.staged is None:
                raise RuntimeError("the file was not staged")
            if outcome == "new":
                info["sample_id"] = _insert(conn, ctx, item, placed)
            elif outcome == "attach":
                _attach(conn, ctx, item, info["sample_id"], info["add"], placed)
            elif outcome == "delta":
                existing = store.samples.get(info["sample_id"], db=conn)
                _add_revisions(conn, ctx, existing["id"], info["add"])
                if existing["status"] == "raw_only":        # an imported orphan gained rows
                    store.samples.set_status(existing["id"], item.status, db=conn)
            elif outcome == "conflict":
                info["conflict_id"] = _hold_conflict(conn, ctx, item, info["sample_id"], placed)
            return outcome, info
    except Exception as exc:  # noqa: BLE001 - recorded per sample, the batch goes on
        log.warning("import_history: %s @ %s failed: %s", item.lab_id, item.injection_dt, exc)
        return "failed", {"error": f"{type(exc).__name__}: {exc}"}


_OUTCOME_KEYS = {"conflict": "conflicts", "already_conflict": "conflicts_existing",
                 "delta": "already_imported", "attach": "attached_to_result_only"}


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
    if outcome in ("delta", "attach") and info.get("add"):
        rec.add("delta_revisions", len(info["add"]))
        rec.add("revisions", len(info["add"]))
        rec.example("delta", dict(brief, sample_id=info["sample_id"],
                                  added=[r.line_no for r in info["add"]]))
    key = _OUTCOME_KEYS.get(outcome, outcome)
    rec.add(key)
    if outcome == "attach" and item.is_blank:
        rec.add("blanks")
    if item.collision_with is not None:
        brief["kept"] = item.collision_with
    rec.example(key, dict(brief, **{k: v for k, v in info.items() if k != "add"}))
    if outcome not in ("delta", "already_imported", "attach"):
        rec.add("rows_not_imported", len(item.rows))


def _report_matcher(summary: dict, rec: _Recorder, report, keyless: list) -> None:
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
    c["unmatched_without_key"] = len(keyless)
    c["name_from_filename"] = len(st.get("name_from_filename", []))
    for p in st.get("name_from_filename", []):
        try:
            name = distill.read_cdf_names(Path(p))[0]
        except Exception:  # noqa: BLE001 - it was readable a moment ago; not worth failing
            continue
        if name and not name.strip():
            # the hub uses the file stem; v1 wrote the spaces to the CSV's Lab ID
            c["whitespace_only_names"] += 1
            rec.example("whitespace_only_names", {"cdf": p, "sample_name": name})
    for p in st.get("name_from_filename", []):
        rec.example("name_from_filename", {"cdf": p})
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
    summary["matcher"] = {
        "source_folders": st.get("source_folders", {}),
        "mixed_warning": st.get("mixed_warning", False),
        "near_misses": (st.get("near_misses") or {}).get("count", 0),
        "rows_noncanonical_dt": (st.get("rows_noncanonical_dt") or {}).get("count", 0),
        "unmatched_same_lab_other_time": st.get("unmatched_same_lab_other_time", 0),
        "csv_issues": _issue_counts(st.get("csv_issues", [])),
        "csv_encoding": st.get("csv_encoding"),
        "dt_source": st.get("dt_source", {}),
        "timezone": st.get("timezone", {}),
    }
    summary["method_names"] = st.get("method_names", {})
    summary["method_names_per_folder"] = st.get("method_names_per_folder", {})
    summary["v1_misparse"] = {
        "v1_misparsed_cdfs": st.get("v1_misparsed_cdfs", 0),
        "rows_matched_via_v1_form": st.get("rows_matched_via_v1_form", 0),
        "v1_family_matches": st.get("v1_family_matches", {}),
        "result_only_time_unverifiable": st.get("result_only_time_unverifiable", 0),
        "examples": st.get("v1_misparsed_examples", []),
    }


def _issue_counts(issues: list) -> dict:
    out: dict = {}
    for i in issues:
        out[i["kind"]] = out.get(i["kind"], 0) + 1
    return dict(sorted(out.items()))


def _one_line(summary: dict) -> str:
    c = summary["counts"]
    return (f"{summary['instrument']}{' (dry run)' if summary.get('dry_run') else ''}: "
            f"{c['imported']} imported ({c['attached']} attached, "
            f"{c['orphans']} orphan, {c['result_only']} result-only), {c['revisions']} revisions, "
            f"{c['already_imported']} already imported, {c['conflicts']} conflicts, "
            f"{c['cross_instrument']} cross-instrument, {c['truncated']} truncated, "
            f"{c['failed']} failed")


# (key, label) per section of the text summary.
_SECTIONS = (
    ("Imported", (
        ("imported", "samples"),
        ("attached", "attached (CDF + rows)"),
        ("orphans", "orphan CDFs (no row; raw_only unless another method)"),
        ("result_only", "result-only (rows, no CDF; legacy_unverified)"),
        ("revisions", "import revisions"),
        ("delta_revisions", "of which rows appended since the last run"),
        ("final", "status final"),
        ("raw_only", "status raw_only"),
        ("other_method", "status other_method (method name not mapped)"),
        ("review_method", "status review_method (no method name)"),
        ("time_corrected", "time corrected (v1 misparse or unrounded mtime)"),
        ("time_unverifiable", "result-only time unverifiable (possible v1 misparse)"),
        ("no_injection_time", "no injection stamp (mtime, whole second)"),
        ("blanks", "genuine blanks"),
    )),
    ("Already in the hub (not imported again)", (
        ("already_imported", "already imported"),
        ("present_not_imported", "received by the hub itself (v1 rows not attached)"),
        ("delta_refused", "new rows, but the hub has revised the sample"),
        ("rows_changed", "stored rows differ from the CSV now"),
        ("key_taken", "result-only key already held"),
    )),
    ("Not imported", (
        ("conflicts", "conflicts stored (same lab ID and time, different file)"),
        ("conflicts_existing", "conflicts already held"),
        ("key_collisions", "key collisions in the folder (the other file is a conflict)"),
        ("collisions_unresolved", "key collisions whose kept file was not imported"),
        ("cross_instrument", "cross-instrument (file held by another instrument)"),
        ("truncated", "truncated or empty CDFs (recopy, then re-run)"),
        ("failed", "failed"),
        ("cdf_errors", "unreadable CDFs"),
        ("dup_sha", "identical bytes (duplicate files)"),
    )),
    ("CSV rows not imported", (
        ("ambiguous_rows", "ambiguous (one CSV key fits several CDFs)"),
        ("held_rows", "held (bad layout)"),
        ("mixed_rows", "mixed (Source File outside this instrument's folder)"),
        ("unmatched_without_key", "no lab ID or time"),
        ("rows_not_imported", "rows of samples not imported"),
    )),
    ("Source", (
        ("cdf_files", "CDF files"),
        ("cdfs_read", "CDFs read"),
        ("csv_rows", "CSV rows"),
        ("whitespace_only_names", "whitespace-only sample names (hub: file stem; v1: the spaces)"),
        ("name_from_filename", "no sample name (file stem used)"),
    )),
)


def _example_text(entry: dict) -> str:
    parts = []
    for k, v in entry.items():
        if v in (None, "", [], {}):
            continue
        parts.append(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}")
    return ", ".join(parts)


def format_summary(summary: dict, *, examples: int = 5) -> str:
    """The summary as text: header, counts per section, v1 misparse, method
    names, then up to ``examples`` examples per class."""
    c = summary["counts"]
    n = max(0, int(examples))
    out: list = []
    add = out.append
    dry = bool(summary.get("dry_run"))
    add(f"History import, {summary['instrument']}" + (" -- DRY RUN (nothing written)" if dry else ""))
    add(f"  Processed folder: {summary['processed_dir']}")
    add(f"  Results CSV: {summary['results_csv'] or '(none: CDFs only)'}")
    add(f"  Folder aliases: {' | '.join(summary['aliases']) or '(none: mixed-row check off)'}")
    if not summary.get("store_checked"):
        add("  Store: not checked (an empty store and the default method map were assumed)")
    add(f"  Started {summary['started_at']}"
        + (f", took {summary['seconds']:.1f} s" if "seconds" in summary else ""))
    if summary.get("stopped"):
        add(f"  STOPPED PARTWAY: {summary['stopped']} (committed batches stay; re-run to resume)")
    for heading, keys in _SECTIONS:
        add("")
        add("Would be imported" if dry and heading == "Imported" else heading)
        for key, label in keys:
            add(f"  {c.get(key, 0):7d}  {label}")
    m = summary.get("v1_misparse") or {}
    add("")
    add("v1 misparse (Python 3.11+ fromisoformat on compact stamps)")
    add(f"  {m.get('v1_misparsed_cdfs', 0):7d}  CDFs v1 gave a wrong time")
    add(f"  {m.get('rows_matched_via_v1_form', 0):7d}  rows matched only through v1's form")
    fams = m.get("v1_family_matches") or {}
    if fams:
        add("           rows per v1 family: " + ", ".join(f"{k} {v}" for k, v in fams.items()))
    add("")
    add("Method names (detection_method_name) of the CDFs kept")
    for name, count in (summary.get("method_names") or {}).items():
        add(f"  {count:7d}  {name or '(absent)'}")
    if (summary.get("matcher") or {}).get("mixed_warning"):
        add("")
        add("WARNING: most CSV rows name a Source File outside the folder aliases; check them.")
    if n:
        labels = {k: lbl for _h, keys in _SECTIONS for k, lbl in keys}
        labels["delta"] = "rows appended since the last run"
        for cls, entries in (summary.get("examples") or {}).items():
            if not entries:
                continue
            add("")
            add(f"Examples: {labels.get(cls, cls)} (first {min(n, len(entries))} of "
                f"{c.get(cls, len(entries))})")
            for e in entries[:n]:
                add("  " + _example_text(e))
    return "\n".join(out) + "\n"
