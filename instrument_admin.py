"""The Instruments page's operations (phase 2, 2A2).

Everything an admin changes per instrument, as plain functions over the
store, so they are tested in-process; ``instruments_api`` is the thin Flask
layer (admin gate, JSON). Every function takes a keyword-only ``db`` (see
``store``). Errors are :class:`AdminError` with an HTTP-ish ``status``
(400 bad input, 404 unknown, 409 refused in the current state) and an
``extra`` dict the route adds to its JSON body.

Instruments
===========

::

    public_row(row) -> dict                  # no token_hash; has_token bool
    list_instruments(*, db) -> [public row]
    get(instrument_id, *, db) -> row         # AdminError 404
    create(fields, *, db) -> public row      # id, name (+ enabled, method, live_since, lem_machine_uid)
    update(instrument_id, fields, *, db) -> (public row, warnings)
    agent_clock(instrument_id, *, db) -> {skew_seconds, last_seen, host} | None
    live_since_warnings(instrument_id, *, db) -> [str]

* Ids match ``ID_RE`` (lower case, digits, ``-``/``_``; they never change).
  A new instrument gets method ``D2887``, the default ``method_map``,
  ``enabled=1`` and **no** ``live_since``: until an admin sets it, everything
  it sends is backfill (D11).
* ``live_since`` is the instrument's clock, naive local (``store.local_dt``):
  ``YYYY-MM-DD HH:MM[:SS]`` or with ``T``; an offset or ``Z`` is refused;
  ``""``/``None`` clears it. Setting it returns warnings: it applies to
  samples received from now on (the backfill flag is decided at submit), and
  the GC PC's clock is off by more than ``SKEW_WARN_SECONDS`` (from the
  agent's last heartbeat), or unknown because no agent has reported.
* ``lem_machine_uid`` is informational (LabStation routing; D4b: LEM holds no
  GC corrections).
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import distill
import instruments
import live
import methods
import paths
import store

log = logging.getLogger("instrument_admin")

ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# Ids a page URL uses (/instruments/classic, an old bookmark since v6.0.0;
# /api/instruments/activity): an instrument with one of these ids could never
# be opened.
RESERVED_IDS = frozenset({"activity", "classic", "new", "setup"})
# ...of which these can't be opened at /instruments/<id> (the page or its API is shadowed)
UNREACHABLE_IDS = frozenset({"activity", "classic"})
NAME_MAX = 64
UID_MAX = 128
SKEW_WARN_SECONDS = 120
EDITABLE = frozenset({"name", "enabled", "method", "live_since", "lem_machine_uid"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Strict: date, one space or T, HH:MM and optional :SS. No zone, no fraction.
_LIVE_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$")

LIVE_SINCE_NOTE = ("live_since applies to samples received from now on: samples already received "
                   "keep their backfill flag (release them on the Backfill screen).")


class AdminError(ValueError):
    """A refused admin request: ``status`` (400/404/409) and ``extra`` JSON keys."""

    def __init__(self, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


def _changed(instrument_id: Any) -> None:
    """Live update (v4.0): the instrument changed (after the commit)."""
    live.publish("instrument", {"instrument_id": instrument_id})


def _not_found(instrument_id: Any) -> AdminError:
    return AdminError(f"Unknown instrument {instrument_id!r}", 404)


# ── rows ────────────────────────────────────────────────────────────────────

def public_row(row: Optional[dict]) -> Optional[dict]:
    """The instrument row as the page may see it: never ``token_hash``."""
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k != "token_hash"}
    out["has_token"] = bool(row.get("token_hash"))
    return out


def list_instruments(*, db: store.Db = None) -> list:
    return [public_row(r) for r in store.instruments.list(db=db)]


def get(instrument_id: Any, *, db: store.Db = None) -> dict:
    if not isinstance(instrument_id, str) or not instrument_id:
        raise _not_found(instrument_id)
    row = store.instruments.get(instrument_id, db=db)
    if row is None:
        raise _not_found(instrument_id)
    return row


# ── field validation ────────────────────────────────────────────────────────

def reserved_id_warnings(*, db: store.Db = None) -> list:
    """One message per existing instrument whose id is now reserved (made
    before v4.0): its new page can't be opened at /instruments/<id>."""
    out = []
    for r in store.instruments.list(db=db):
        if r["id"] in RESERVED_IDS:
            out.append(f"Instrument {r['name']} has the id \"{r['id']}\", which is now reserved by the hub's "
                       f"pages, so /instruments/{r['id']} does not open it. Its runs still "
                       f"process; to manage it on the pages, add the GC again under another id.")
    return out


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdminError("The name must be a non-empty string.")
    text = value.strip()
    if len(text) > NAME_MAX or _CONTROL.search(text):
        raise AdminError(f"The name must be at most {NAME_MAX} printable characters.")
    return text


def _enabled(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    raise AdminError("enabled must be true or false.")


def _method(value: Any) -> str:
    key = str(value or "").strip().upper() if isinstance(value, str) else ""
    if key not in methods.names():
        raise AdminError(f"method must be one of {', '.join(methods.names())}.")
    return key


def _live_since(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or not _LIVE_SINCE_RE.match(value.strip()):
        raise AdminError("live_since must be the GC's local date and time as YYYY-MM-DD HH:MM[:SS], "
                         "e.g. 2026-10-01 08:00, with no time zone.")
    try:
        return store.local_dt(value)
    except ValueError:
        raise AdminError("live_since must be the GC's local date and time, e.g. 2026-10-01 08:00, "
                         "with no time zone.") from None


def _uid(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or len(value.strip()) > UID_MAX or _CONTROL.search(value):
        raise AdminError(f"lem_machine_uid must be text of at most {UID_MAX} characters.")
    return value.strip()


_CLEAN = {"name": _name, "enabled": _enabled, "method": _method, "live_since": _live_since,
          "lem_machine_uid": _uid}


def _clean(fields: Any, allowed: frozenset) -> dict:
    if not isinstance(fields, dict):
        raise AdminError("Expected an object of instrument fields.")
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise AdminError(f"These fields can't be set here: {', '.join(map(str, unknown))}.")
    return {k: _CLEAN[k](v) for k, v in fields.items()}


# ── create / update ─────────────────────────────────────────────────────────

def create(fields: Any, *, db: store.Db = None, by: Optional[str] = None) -> dict:
    if not isinstance(fields, dict):
        raise AdminError("Expected an object of instrument fields.")
    fields = dict(fields)
    iid = fields.pop("id", None)
    if not isinstance(iid, str) or not ID_RE.match(iid):
        raise AdminError("The id must be 1-32 characters: a lower-case letter, then lower-case "
                         "letters, digits, '-' or '_' (e.g. gc2). It can't be changed later.")
    if iid in RESERVED_IDS:
        raise AdminError(f"The id {iid!r} is reserved by the hub's pages; choose another (e.g. gc2).")
    if "name" not in fields:
        raise AdminError("A name is required.")
    clean = _clean(fields, EDITABLE)
    clean.setdefault("method", instruments.DEFAULT_METHOD)
    clean.setdefault("enabled", 1)
    with store.connection(db) as conn:
        with store.write_txn(conn):
            if store.instruments.get(iid, db=conn) is not None:
                raise AdminError(f"An instrument with id {iid!r} already exists.", 409)
            row = store.instruments.upsert(dict(
                clean, id=iid, method_map=json.dumps(instruments.DEFAULT_METHOD_MAP)), db=conn)
            store.instrument_events.add(conn, iid, "created", by=by, detail={"name": row["name"]})
            if clean.get("live_since"):
                store.instrument_events.add(conn, iid, "live_since", by=by, detail={
                    "live_since": clean["live_since"], "old": None})
    log.warning("instrument %s created (%s)", iid, row["name"])
    _changed(iid)
    return public_row(row)


def update(instrument_id: Any, fields: Any, *, db: store.Db = None,
           by: Optional[str] = None) -> tuple:
    """Change editable fields; ``(public row, warnings)``. A changed
    ``live_since`` is recorded as an ``instrument_events`` row."""
    clean = _clean(fields, EDITABLE)
    if not clean:
        raise AdminError("Nothing to change.")
    with store.connection(db) as conn:
        with store.write_txn(conn):
            old = get(instrument_id, db=conn)
            row = store.instruments.upsert(dict(clean, id=instrument_id), db=conn)
            if "live_since" in clean and clean["live_since"] != old.get("live_since"):
                store.instrument_events.add(conn, instrument_id, "live_since", by=by, detail={
                    "live_since": clean["live_since"], "old": old.get("live_since")})
    log.warning("instrument %s updated: %s", instrument_id, sorted(clean))
    _changed(instrument_id)
    warnings: list = []
    if "live_since" in clean:
        warnings = live_since_change_warnings(old.get("live_since"), clean["live_since"])
        if clean["live_since"] is not None:
            warnings += live_since_warnings(instrument_id, db=db)
    return public_row(row), warnings


def live_since_change_warnings(old: Optional[str], new: Optional[str],
                               now: Optional[datetime] = None) -> list:
    """Review I1: a change that stops or delays a live instrument's exports.
    Clearing ``live_since`` makes everything received from now on backfill;
    moving it later, or into the future, holds back injections before it."""
    out = []
    if new is None:
        if old:
            out.append("live_since was cleared: everything this instrument sends from now on is "
                       "backfill, which stops its automatic exports (to LEM) until live_since is "
                       "set again.")
        return out
    if old and store.local_dt(new) > store.local_dt(old):
        out.append(f"live_since moved later (from {old} to {new}): samples injected before "
                   f"{new} that arrive from now on are backfill and are not exported until released.")
    stamp = store.local_dt((now or datetime.now()).replace(microsecond=0))
    if store.local_dt(new) > stamp:
        out.append(f"live_since {new} is in the future: nothing this instrument sends is exported "
                   f"automatically until then.")
    return out


# ── the agent's clock ───────────────────────────────────────────────────────

def agent_clock(instrument_id: str, *, db: store.Db = None) -> Optional[dict]:
    """The agent's clock skew at its last heartbeat (``None`` if it never reported)."""
    import ingest_api   # deferred: it imports flask
    with store.connection(db) as conn:
        r = conn.execute("SELECT agent_time, last_seen, host FROM agents WHERE instrument_id=?",
                         (instrument_id,)).fetchone()
    if r is None or not r["last_seen"]:
        return None
    return {"skew_seconds": ingest_api.clock_skew_seconds(r["agent_time"], r["last_seen"]),
            "last_seen": r["last_seen"], "host": r["host"]}


def _minutes(seconds: float) -> str:
    m = abs(seconds) / 60.0
    return f"{m:.0f} min" if m >= 1 else f"{abs(seconds):.0f} s"


def skew_warning(skew: Optional[float]) -> Optional[str]:
    if skew is None or abs(skew) <= SKEW_WARN_SECONDS:
        return None
    side = "ahead of" if skew > 0 else "behind"
    return (f"The GC PC's clock is {_minutes(skew)} {side} the hub's. Injection times come from "
            f"the GC's clock, so live_since is compared with that clock: fix the PC's clock, or "
            f"allow for it.")


def live_since_warnings(instrument_id: str, *, db: store.Db = None) -> list:
    warnings = [LIVE_SINCE_NOTE]
    clock = agent_clock(instrument_id, db=db)
    if clock is None:
        warnings.append("No agent has reported for this instrument yet (it has not reported a "
                        "heartbeat), so its clock can't be checked: check the GC PC's clock "
                        "before relying on live_since.")
    elif clock["skew_seconds"] is None:
        warnings.append("The agent's last heartbeat had no readable clock time: check the GC "
                        "PC's clock before relying on live_since.")
    else:
        w = skew_warning(clock["skew_seconds"])
        if w:
            warnings.append(w)
    return warnings


# ── export path (D9b) ───────────────────────────────────────────────────────

def _refused(err) -> AdminError:
    """``exports.ExportRefused`` as a 409 carrying its reason code and detail."""
    return AdminError(f"Refused ({err.reason}): {err.detail}", 409, reason=err.reason,
                      detail=err.detail, path=str(err.path))


def export_status(instrument_id: str, exporter) -> dict:
    """The exporter's status for the instrument, plus whether a path is configured."""
    row = get(instrument_id, db=exporter.db)
    st = dict(exporter.status(instrument_id))
    st["configured"] = bool((row.get("export_path") or "").strip())
    return st


def set_export_path(instrument_id: str, path: Any, exporter, *, by: Optional[str] = None) -> dict:
    """Point the instrument's export at ``path`` (absolute, ``.csv``, as
    hub_admin's new-path route requires). A file already
    there must then be adopted before the hub appends to it."""
    import exports
    get(instrument_id, db=exporter.db)
    if (not isinstance(path, str) or not path.strip() or not Path(path.strip()).is_absolute()
            or Path(path.strip()).suffix.lower() != ".csv"):
        raise AdminError("The export path must be an absolute path to a .csv file, e.g. "
                         r"\\asapserver\Labsharedrive\...\distill_results.csv.")
    try:
        # the export_path event is written in the same transaction as the path
        exporter.new_path(instrument_id, path.strip(), event_by=by)
    except exports.ExportRefused as err:
        raise _refused(err) from None
    log.warning("instrument %s export path set to %s", instrument_id, path.strip())
    _changed(instrument_id)
    return export_status(instrument_id, exporter)


def adopt_export(instrument_id: str, exporter, *, by: Optional[str]) -> dict:
    """Adopt the export file as it is now (a v1 file LEM already tails)."""
    import exports
    get(instrument_id, db=exporter.db)
    try:
        side = exporter.adopt(instrument_id, by=by)
    except exports.ExportRefused as err:
        raise _refused(err) from None
    store.instrument_events.add(exporter.db, instrument_id, "export_adopted", by=by,
                                detail={"size": (side or {}).get("size")})
    _changed(instrument_id)
    return {"adopted": side, "status": export_status(instrument_id, exporter)}


def keep_hub_only_export(instrument_id: str, exporter, *, by: Optional[str]) -> dict:
    """Record the explicit choice to keep the hub's own results file
    (``results/<id>_results.csv``, which LEM does not read): the setup guide's
    step 7 needs a configured path or this choice. 409 when a path is set."""
    row = get(instrument_id, db=exporter.db)
    if (row.get("export_path") or "").strip():
        raise AdminError(f"{row['name']} already writes to {row['export_path']}; the hub-only file "
                         f"is not in use.", 409)
    st = export_status(instrument_id, exporter)
    store.instrument_events.add(exporter.db, instrument_id, "export_hub_only", by=by,
                                detail={"path": st.get("path")})
    log.warning("instrument %s: results kept in the hub-only file %s (by %s)", instrument_id,
                st.get("path"), by)
    return st


# ── calibration (Processing per instrument) ─────────────────────────────────

def _clear_cal_cache() -> None:
    with distill._CAL_LOCK:
        distill._CAL_CACHE.clear()


def _cal_path(ctx: dict) -> Optional[Path]:
    return distill.active_calibration_path(ctx, honour_env=False)


def calibration_status(instrument_id: str, conf: dict, *, db: store.Db = None,
                       data_dir=None) -> dict:
    """``{usable, problem, calibration_cdf, sensitivity, assigned}``: "Calibration
    usable" means the CDF exists and has at least two usable assignment pairs."""
    row = get(instrument_id, db=db)
    ctx = instruments.context(row, conf, data_dir=data_dir)
    problem = instruments.calibration_problem(ctx)
    cal = _cal_path(ctx)
    assigned = 0
    if cal is not None:
        amap = distill.parse_assignment_map(ctx.get("calibration_assignments", ""))
        assigned = len(distill._assignment_pairs(amap, cal))
    return {"usable": problem is None, "problem": problem,
            "calibration_cdf": str(cal) if cal is not None else "",
            "stored_as": row.get("calibration_cdf") or "",
            "sensitivity": float(row.get("calibration_sensitivity") or 50.0),
            "assigned": assigned}


def calibration_candidates(instrument_id: str, q: Optional[str] = None, limit: int = 50, *,
                           db: store.Db = None) -> list:
    """This instrument's received samples with a stored CDF, newest first, to pick
    a calibration run from (no server paths)."""
    get(instrument_id, db=db)
    limit = max(1, min(int(limit), 200))
    rows = store.samples.search(q=q or None, instrument=instrument_id, limit=limit * 2, db=db)
    out = []
    for r in rows:
        if not r.get("cdf_path"):
            continue
        out.append({"sample_id": r["id"], "lab_id": r["lab_id"], "injection_dt": r["injection_dt"],
                    "status": r["status"], "method_name": r["method_name"],
                    "source_name": r.get("source_name")})
        if len(out) >= limit:
            break
    return out


def _int_id(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AdminError(f"{what} must be an integer.")
    try:
        return int(value)
    except ValueError:
        raise AdminError(f"{what} must be an integer.") from None


def _check_cdf(path: Path) -> None:
    """Refuse (400) a file that doesn't read as a chromatogram CDF, before
    anything changes: truncated, not NetCDF, or no intensity data."""
    import pipeline
    try:
        problem = pipeline.cdf_problem(path)
        if problem is None:
            t, y = distill.gc_xy_from_cdf(path)
            if len(t) < 2 or len(t) != len(y):
                problem = "it holds no chromatogram"
    except Exception as exc:  # noqa: BLE001 - any read failure means "not usable"
        problem = f"it can't be read ({type(exc).__name__})"
    if problem:
        raise AdminError(f"{path.name} is not a usable calibration CDF: {problem}.")


def set_calibration_cdf(instrument_id: str, conf: dict, *, sample_id: Any = None,
                        path: Any = None, db: store.Db = None, data_dir=None,
                        by: Optional[str] = None) -> dict:
    """Set the calibration CDF from one of the instrument's own samples (stored
    data-relative) or an absolute path to an existing file. A changed CDF
    clears the saved assignments (they belong to one file). When the result is
    usable, the instrument's ``awaiting_calibration`` samples are queued."""
    import pipeline
    if (sample_id is None) == (path is None):
        raise AdminError("Give either sample_id (one of this instrument's samples) or path.")
    with store.connection(db) as conn:
        row = get(instrument_id, db=conn)
        if sample_id is not None:
            sid = _int_id(sample_id, "sample_id")
            s = store.samples.get(sid, db=conn)
            if s is None:
                raise AdminError(f"Sample {sid} not found.", 404)
            if s["instrument_id"] != instrument_id:
                other = store.instruments.get(s["instrument_id"], db=conn) or {}
                raise AdminError(f"Sample {sid} was received from {other.get('name') or s['instrument_id']}, "
                                 f"not {row['name']}: a calibration must come from the same instrument.")
            if not s.get("cdf_path"):
                raise AdminError(f"Sample {sid} has no stored CDF (result-only import).")
            new = s["cdf_path"]
        else:
            if not isinstance(path, str) or not path.strip() or not Path(path.strip()).is_absolute():
                raise AdminError("path must be an absolute path to a calibration CDF.")
            if not Path(path.strip()).is_file():
                raise AdminError(f"No file at {path.strip()}.")
            new = path.strip()
        _check_cdf(Path(new) if Path(new).is_absolute() else Path(data_dir or paths.data_dir()) / new)
        with store.write_txn(conn):
            fields = {"id": instrument_id, "calibration_cdf": new}
            if new != (row.get("calibration_cdf") or ""):
                fields["calibration_assignments"] = None
            store.instruments.upsert(fields, db=conn)
            _clear_cal_cache()
            st = calibration_status(instrument_id, conf, db=conn, data_dir=data_dir)
            st["queued"] = pipeline.on_calibration_saved(instrument_id, db=conn) if st["usable"] else 0
            store.instrument_events.add(conn, instrument_id, "calibration_cdf", by=by, detail={
                "sample_id": sid if sample_id is not None else None, "file": Path(new).name,
                "usable": st["usable"]})
    log.warning("instrument %s calibration CDF set to %s", instrument_id, new)
    _changed(instrument_id)
    return st


def clean_assignments(assignments: Any) -> list:
    """The Calibration page's entries, validated: ``[{rt, carbon}|{rt, ignore}]``;
    unassigned peaks are dropped. Carbons must increase with retention time."""
    if not isinstance(assignments, list):
        raise AdminError("assignments must be a list.")
    valid = set(distill.N_ALKANE_CARBON)
    clean: list = []
    for entry in assignments:
        if not isinstance(entry, dict) or "rt" not in entry:
            raise AdminError("Each assignment needs an 'rt'.")
        rt = entry["rt"]
        if isinstance(rt, bool) or not isinstance(rt, (int, float)) or not math.isfinite(rt):
            raise AdminError(f"rt must be a number, not {rt!r}.")
        if entry.get("ignore"):
            clean.append({"rt": float(rt), "ignore": True})
        elif entry.get("carbon") is not None:
            c = entry["carbon"]
            if isinstance(c, bool) or not isinstance(c, (int, float)) or int(c) != c or int(c) not in valid:
                raise AdminError(f"Unknown carbon number: {c!r}.")
            clean.append({"rt": float(rt), "carbon": int(c)})
    errors = distill.validate_assignments(clean)
    if errors:
        raise AdminError("Assigned carbons must increase with retention time: " + "; ".join(errors))
    return clean


def _sensitivity(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 <= value <= 100:
        raise AdminError("sensitivity must be a number from 0 to 100.")
    return float(value)


def save_calibration(instrument_id: str, assignments: Any, sensitivity: Any, conf: dict, *,
                     db: store.Db = None, data_dir=None, by: Optional[str] = None) -> dict:
    """Save the peak assignments (and sensitivity) for the instrument's
    calibration CDF and, in the same transaction, queue its
    ``awaiting_calibration`` samples (``pipeline.on_calibration_saved``; nothing
    else is reprocessed)."""
    import pipeline
    clean = clean_assignments(assignments)
    sens = None if sensitivity is None else _sensitivity(sensitivity)
    with store.connection(db) as conn:
        with store.write_txn(conn):
            row = get(instrument_id, db=conn)
            cal = _cal_path(instruments.context(row, conf, data_dir=data_dir))
            if cal is None:
                raise AdminError(f"{row['name']} has no calibration CDF: choose one first.", 409)
            fields = {"id": instrument_id, "calibration_assignments": json.dumps(clean) if clean else None}
            if sens is not None:
                fields["calibration_sensitivity"] = sens
            store.instruments.upsert(fields, db=conn)
            _clear_cal_cache()
            queued = pipeline.on_calibration_saved(instrument_id, db=conn)
            st = calibration_status(instrument_id, conf, db=conn, data_dir=data_dir)
            store.instrument_events.add(conn, instrument_id, "calibration_saved", by=by, detail={
                "file": cal.name, "assigned": st["assigned"], "usable": st["usable"],
                "queued": queued})
    anchors = distill.anchors_for(distill.parse_assignment_map(
        distill.upsert_assignments("", cal, clean)), cal) if clean else None
    log.warning("instrument %s calibration saved (%d entries, %d queued)", instrument_id,
                len(clean), queued)
    _changed(instrument_id)
    return dict(st, saved=len(clean), queued=queued,
                anchors=0 if anchors is None else int(anchors[0].size))


def calibration_view(instrument_id: str, conf: dict, sensitivity: Any = None, *,
                     db: store.Db = None, data_dir=None) -> dict:
    """The Calibration page's payload for this instrument: the trace, detected
    peaks (at ``sensitivity``, default the saved one), compounds, the saved
    assignments and the usable status."""
    import numpy as np
    row = get(instrument_id, db=db)
    ctx = instruments.context(row, conf, data_dir=data_dir)
    cal = _cal_path(ctx)
    if cal is None:
        raise AdminError(f"{row['name']} has no calibration CDF: choose one on the Instruments page.", 409)
    if not cal.is_file():
        raise AdminError(f"The calibration file is missing: {cal.name}", 409)
    if sensitivity is None:
        sens = float(row.get("calibration_sensitivity") or 50.0)
    else:
        try:
            sens = _sensitivity(float(sensitivity))
        except (TypeError, ValueError):
            raise AdminError("sensitivity must be a number from 0 to 100.") from None
    t, y = distill.gc_xy_from_cdf(cal)
    peak_times = distill.calibration_peak_times(cal, sens)
    inten = np.interp(peak_times, t, y).tolist() if peak_times else []
    amap = distill.parse_assignment_map(ctx.get("calibration_assignments", ""))
    step = max(1, len(t) // 3000)
    st = calibration_status(instrument_id, conf, db=db, data_dir=data_dir)
    return {
        "instrument": row["id"], "instrument_name": row["name"],
        "cdf_name": cal.name, "sensitivity": sens,
        "trace": {"x": t[::step].tolist(), "y": y[::step].tolist()},
        "peaks": [{"index": i, "rt": round(float(rt), 4), "intensity": round(float(v), 1)}
                  for i, (rt, v) in enumerate(zip(peak_times, inten))],
        "compounds": [{"carbon": c, "bp": bp}
                      for c, bp in zip(distill.N_ALKANE_CARBON, distill.N_ALKANE_BP)],
        "assignments": amap.get(distill._cal_key(cal), []),
        "usable": st["usable"], "problem": st["problem"], "assigned": st["assigned"],
    }


# ── corrections (D4b / 2C) ──────────────────────────────────────────────────

REASON_MAX = 500
SEED_REASON = "seeded from correction_factors.json"


def _reason(reason: Any) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise AdminError("A reason is required to change correction factors.")
    if len(reason.strip()) > REASON_MAX:
        raise AdminError(f"The reason must be at most {REASON_MAX} characters.")
    return reason.strip()


def _write_corrections(instrument_id: str, values: dict, reason: str, by: Optional[str],
                       db: store.Db) -> dict:
    """``set_all`` and queueing the instrument's ``pending_corrections`` samples,
    in one transaction."""
    with store.connection(db) as conn:
        with store.write_txn(conn):
            get(instrument_id, db=conn)
            changed = store.corrections.set_all(conn, instrument_id, values, by=by, reason=reason)
            queued = store.jobs.enqueue_for_status(instrument_id, "pending_corrections", db=conn)
            store.instrument_events.add(conn, instrument_id, "corrections_saved", by=by,
                                        detail={"changed": changed, "reason": reason})
    _changed(instrument_id)
    return {"changed": changed, "queued": queued}


def save_corrections(instrument_id: str, values: Any, reason: Any, *, by: Optional[str],
                     db: store.Db = None) -> dict:
    """Save all eleven D86 corrections for the instrument (``validate_values``:
    finite numbers within ±50 °C), with a required reason. The table, its audit
    and queueing **only** this instrument's ``pending_corrections`` samples are
    one transaction. ``{changed, queued}``."""
    import corrections
    get(instrument_id, db=db)
    errors = corrections.validate_values(values)
    if errors:
        raise AdminError(" ".join(errors), errors=errors)
    why = _reason(reason)
    out = _write_corrections(instrument_id, {c: float(values[c]) for c in corrections.D86_CUTS},
                             why, by, db)
    log.warning("instrument %s corrections saved by %s (%d changed, %d queued): %s",
                instrument_id, by, out["changed"], out["queued"], why)
    return out


def corrections_view(instrument_id: str, conf: dict, *, db: store.Db = None,
                     audit_limit: int = 200) -> dict:
    """What the editor shows: the cuts, the current values and their source
    (``hub``; for gc1 before seeding the interim ``file`` values, or the file's
    error), whether gc1 can still be seeded, and the audit (newest first)."""
    import corrections
    row = get(instrument_id, db=db)
    rec = store.corrections.read(instrument_id, db=db)
    out = {"cuts": list(corrections.D86_CUTS), "max_abs": corrections.MAX_ABS_CORRECTION_C,
           "values": None, "source": None, "updated_at": None, "updated_by": None,
           "complete": False, "can_seed": False, "file_error": None,
           "audit": store.corrections.audit(instrument_id, audit_limit, db=db)}
    if rec is not None:
        out.update(values=rec["values"], source="hub", updated_at=rec["updated_at"],
                   updated_by=rec["updated_by"],
                   complete=not corrections.validate_values(rec["values"]))
    elif row["id"] == instruments.GC1:
        out["can_seed"] = True
        try:
            out.update(values=corrections.seed_from_file(conf.get("correction_factors_json", "") or ""),
                       source="file", complete=True)
        except corrections.CorrectionsUnavailable as exc:
            out["file_error"] = exc.reason
    return out


def seed_gc1(conf: dict, *, by: Optional[str], db: store.Db = None) -> dict:
    """Seed gc1's hub corrections once from the phase-1 file
    (``corrections.seed_from_file``, strict), audited as ``SEED_REASON``. 409
    once gc1 has hub corrections, or if the file is unusable."""
    import corrections
    get(instruments.GC1, db=db)
    if store.corrections.read(instruments.GC1, db=db) is not None:
        raise AdminError("GC-1 already has hub correction factors; edit them instead.", 409)
    try:
        values = corrections.seed_from_file(conf.get("correction_factors_json", "") or "")
    except corrections.CorrectionsUnavailable as exc:
        raise AdminError(exc.reason, 409) from None
    with store.connection(db) as conn:
        with store.write_txn(conn):
            if store.corrections.read(instruments.GC1, db=conn) is not None:
                raise AdminError("GC-1 already has hub correction factors; edit them instead.", 409)
            changed = store.corrections.set_all(conn, instruments.GC1, values, by=by,
                                                reason=SEED_REASON)
            queued = store.jobs.enqueue_for_status(instruments.GC1, "pending_corrections", db=conn)
            store.instrument_events.add(conn, instruments.GC1, "corrections_saved", by=by,
                                        detail={"changed": changed, "reason": SEED_REASON})
    log.warning("gc1 corrections seeded from %s by %s", conf.get("correction_factors_json"), by)
    _changed(instruments.GC1)
    return {"changed": changed, "queued": queued, "values": values}


# ── methods seen (Method detection) ─────────────────────────────────────────

METHOD_NAME_MAX = 200


def methods_view(instrument_id: str, *, db: store.Db = None) -> dict:
    """Every ChemStation method name the instrument has sent (``'' `` = none),
    with count, first/last injection and what it maps to; mapped names never
    seen are listed with count 0. ``review_count`` = samples held
    ``review_method``."""
    row = get(instrument_id, db=db)
    mapping = instruments.method_map(row)
    seen = {r["method_name"]: dict(r, mapped_to=mapping.get(r["method_name"]) if r["method_name"] else None)
            for r in store.samples.methods_seen(instrument_id, db=db)}
    for name, hub_method in mapping.items():
        seen.setdefault(name, {"method_name": name, "count": 0, "first_seen": None,
                               "last_seen": None, "mapped_to": hub_method})
    review = store.samples.count(instrument=instrument_id, status="review_method", db=db)
    return {"instrument": instrument_id, "hub_methods": methods.names(), "method_map": mapping,
            "seen": sorted(seen.values(), key=lambda r: r["method_name"]), "review_count": review}


def set_method_mapping(instrument_id: str, method_name: Any, hub_method: Any, *,
                       db: store.Db = None, by: Optional[str] = None) -> dict:
    """Map a ChemStation method name (normalised) to a hub method, or unmap it
    (``hub_method`` None). Mapping queues that name's ``other_method`` samples
    (``pipeline.on_method_mapped``) in the same transaction; unmapping never
    touches results."""
    import pipeline
    if not isinstance(method_name, str) or len(method_name) > METHOD_NAME_MAX:
        raise AdminError("method_name must be a ChemStation method name, e.g. SIMDISB.M.")
    name = methods.normalise_method_name(method_name)
    if not name:
        raise AdminError("A sample with no method name can't be mapped: mark it as another "
                         "method, or fix the method in ChemStation.")
    target = None
    if hub_method is not None:
        target = _method(hub_method)
    with store.connection(db) as conn:
        with store.write_txn(conn):
            row = get(instrument_id, db=conn)
            mapping = instruments.method_map(row)
            if target is None:
                mapping.pop(name, None)
            else:
                mapping[name] = target
            store.instruments.upsert({"id": instrument_id, "method_map": json.dumps(mapping)}, db=conn)
            queued = pipeline.on_method_mapped(instrument_id, name, db=conn) if target else 0
            store.instrument_events.add(conn, instrument_id, "method_mapped", by=by, detail={
                "method": name, "hub_method": target, "queued": queued})
    log.warning("instrument %s: method %s %s", instrument_id, name,
                f"mapped to {target} ({queued} queued)" if target else "unmapped")
    _changed(instrument_id)
    return {"method_map": mapping, "queued": queued}


def mark_review_other(instrument_id: str, sample_ids: Any = None, *, db: store.Db = None) -> int:
    """Classify the instrument's ``review_method`` samples (all, or the given
    ids) as ``other_method``: stored, never processed. Returns how many."""
    ids = None
    if sample_ids is not None:
        if not isinstance(sample_ids, list) or len(sample_ids) > 5000:
            raise AdminError("sample_ids must be a list of sample ids.")
        ids = [_int_id(v, "sample_ids") for v in sample_ids]
    with store.connection(db) as conn:
        with store.write_txn(conn):
            get(instrument_id, db=conn)
            where = "instrument_id=? AND status='review_method'"
            where_args: list = [instrument_id]
            if ids is not None:
                if not ids:
                    return 0
                where += f" AND id IN ({','.join('?' for _ in ids)})"
                where_args += ids
            changed_ids = [r[0] for r in conn.execute(
                f"SELECT id FROM samples WHERE {where}", where_args)]
            n = conn.execute(
                f"UPDATE samples SET status='other_method', error=? WHERE {where}",
                ["classified as another method by an admin", *where_args]).rowcount
    log.warning("instrument %s: %d review_method sample(s) marked other_method", instrument_id, n)
    live.publish_samples(changed_ids)
    _changed(instrument_id)
    return n


# ── backfill release (D11) ──────────────────────────────────────────────────

RELEASE_MAX = 500
_SAMPLE_FIELDS = ("lab_id", "injection_dt", "status", "method_name", "source_name", "released_at",
                  "released_by", "current_revision", "error", "received_at", "is_blank")


def _sample_public(s: dict) -> dict:
    out = {"sample_id": s["id"]}
    out.update({k: s.get(k) for k in _SAMPLE_FIELDS})
    return out


def _bounded(value: Any, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        raise AdminError(f"{what} must be an integer from {lo} to {hi}.")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise AdminError(f"{what} must be an integer from {lo} to {hi}.") from None
    if not lo <= n <= hi:
        raise AdminError(f"{what} must be an integer from {lo} to {hi}.")
    return n


def backfill_list(instrument_id: str, q: Optional[str] = None, status: Optional[str] = None,
                  released: Any = None, limit: Any = 100, offset: Any = 0, *,
                  db: store.Db = None) -> dict:
    """The instrument's backfill samples, newest injection first:
    ``{samples, total, live_since, live_since_set_at}``. ``released``
    True/False filters on ``released_at``. ``live_since`` (the GC's clock) and
    ``live_since_set_at`` (the hub's UTC time of the newest ``live_since``
    event, None without one) let the page say why each row is backfill: the
    flag is decided when a run arrives (``store.is_backfill``)."""
    inst = get(instrument_id, db=db)
    limit = _bounded(limit, "limit", 1, 1000)
    offset = _bounded(offset, "offset", 0, 10 ** 9)
    if status is not None and status not in store.STATUSES:
        raise AdminError(f"status must be one of {', '.join(store.STATUSES)}.")
    if released not in (None, True, False):
        raise AdminError("released must be true, false or absent.")
    where = ["instrument_id=?", "backfill=1"]
    args: list = [instrument_id]
    if q:
        pat = "%" + str(q).strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(lab_id LIKE ? ESCAPE '\\' OR source_name LIKE ? ESCAPE '\\')")
        args += [pat, pat]
    if status:
        where.append("status=?")
        args.append(status)
    if released is not None:
        where.append("released_at IS NOT NULL" if released else "released_at IS NULL")
    sql_where = " WHERE " + " AND ".join(where)
    with store.connection(db) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM samples{sql_where}", args).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM samples{sql_where} ORDER BY injection_dt DESC, id DESC LIMIT ? OFFSET ?",
            args + [limit, offset])]
        set_at = conn.execute(
            "SELECT at FROM instrument_events WHERE instrument_id=? AND kind='live_since' "
            "ORDER BY at DESC, id DESC LIMIT 1", (instrument_id,)).fetchone()
    return {"samples": [_sample_public(r) for r in rows], "total": int(total),
            "live_since": inst.get("live_since"), "live_since_set_at": set_at[0] if set_at else None}


def _id_list(sample_ids: Any, what: str = "sample_ids", most: int = RELEASE_MAX) -> list:
    if not isinstance(sample_ids, list) or not sample_ids or len(sample_ids) > most:
        raise AdminError(f"{what} must be a list of 1 to {most} sample ids.")
    out: list = []
    for v in sample_ids:
        i = _int_id(v, what)
        if i not in out:
            out.append(i)
    return out


def release(instrument_id: str, sample_ids: Any, *, by: Optional[str], db: store.Db = None,
            data_dir=None, notifier=None) -> list:
    """Release the selected backfill samples of this instrument, one by one
    (``pipeline.release_backfill``: sets ``released_at`` and writes the export
    row). ``[{sample_id, ok, seq | error}]`` in request order. ``notifier(level,
    message)`` hears about a Lab ID LEM will misread (v5.1.0)."""
    import pipeline
    ids = _id_list(sample_ids)
    inst = get(instrument_id, db=db)
    out = []
    for sid in ids:
        s = store.samples.get(sid, db=db)
        if s is None:
            out.append({"sample_id": sid, "ok": False, "error": f"sample {sid} does not exist"})
            continue
        if s["instrument_id"] != instrument_id:
            out.append({"sample_id": sid, "ok": False,
                        "error": f"sample {sid} is not from {inst['name']}"})
            continue
        try:
            seq = pipeline.release_backfill(sid, by=by, db=db, data_dir=data_dir, notifier=notifier)
        except pipeline.NotExportable as exc:
            out.append({"sample_id": sid, "ok": False, "error": str(exc)})
            continue
        out.append({"sample_id": sid, "ok": True, "seq": seq})
    log.warning("instrument %s: backfill release by %s: %d of %d released", instrument_id, by,
                sum(1 for r in out if r["ok"]), len(out))
    _changed(instrument_id)
    return out


# ── conflicts ───────────────────────────────────────────────────────────────

def _held(c: dict, data_dir) -> dict:
    size = None
    name = Path(c["cdf_path"]).name if c.get("cdf_path") else None
    if c.get("cdf_path") and data_dir is not None:
        p = Path(data_dir) / c["cdf_path"]
        try:
            size = p.stat().st_size
        except OSError:
            size = None
    return {"lab_id": c["lab_id"], "injection_dt": c["injection_dt"], "cdf_sha256": c["cdf_sha256"],
            "file_name": name, "size": size, "received_at": c["received_at"]}


def conflicts_list(instrument_id: Optional[str] = None, include_resolved: bool = False, *,
                   db: store.Db = None, data_dir=None) -> list:
    """Conflicts (unresolved unless ``include_resolved``), oldest first, each with
    the held file's identity, the existing sample's, the last Replace error and
    whether a Replace is pending."""
    import pipeline
    names = {r["id"]: r["name"] for r in store.instruments.list(db=db)}
    out = []
    with store.connection(db) as conn:
        for c in store.conflicts.list(instrument_id, unresolved_only=not include_resolved, db=conn):
            s = store.samples.get(c["existing_sample_id"], db=conn) if c["existing_sample_id"] else None
            rj = pipeline._replace_job(conn, s["id"]) if s is not None else None
            existing = None
            if s is not None:
                existing = {"sample_id": s["id"], "lab_id": s["lab_id"],
                            "injection_dt": s["injection_dt"], "cdf_sha256": s["cdf_sha256"],
                            "file_name": Path(s["cdf_path"]).name if s.get("cdf_path") else None,
                            "source_name": s.get("source_name"), "status": s["status"],
                            "current_revision": s["current_revision"],
                            "received_at": s["received_at"]}
            out.append({"id": c["id"], "instrument_id": c["instrument_id"],
                        "instrument_name": names.get(c["instrument_id"], c["instrument_id"]),
                        "held": _held(c, data_dir), "existing": existing, "error": c.get("error"),
                        "resolved": c["resolved"], "resolved_by": c["resolved_by"],
                        "resolved_at": c["resolved_at"],
                        "replace_pending": bool(rj and rj["payload"].get("conflict_id") == c["id"])})
    return out


def _conflict(conflict_id: Any, db) -> dict:
    cid = _int_id(conflict_id, "conflict id")
    c = store.conflicts.get(cid, db=db)
    if c is None:
        raise AdminError(f"Conflict {cid} not found.", 404)
    if c["resolved"] is not None:
        raise AdminError(f"Conflict {cid} is already resolved ({c['resolved']}).", 409)
    return c


def keep_existing(conflict_id: Any, *, by: Optional[str], db: store.Db = None) -> None:
    """Keep the existing sample's file (a pending Replace then does nothing)."""
    c = _conflict(conflict_id, db)
    try:
        store.conflicts.resolve(c["id"], "kept-existing", by=by or "admin", db=db)
    except ValueError as exc:
        raise AdminError(str(exc), 409) from None
    log.warning("conflict %s: kept existing (by %s)", c["id"], by)
    _changed(c["instrument_id"])


def replace(conflict_id: Any, *, by: Optional[str], db: store.Db = None, data_dir=None) -> int:
    """Ask for the held file to replace the existing sample's
    (``pipeline.resolve_conflict_replace``: queues a job; the worker resolves
    the conflict). Returns the job id."""
    import pipeline
    c = _conflict(conflict_id, db)
    try:
        return pipeline.resolve_conflict_replace(c["id"], by=by, db=db, data_dir=data_dir)
    except ValueError as exc:
        raise AdminError(str(exc), 409) from None
