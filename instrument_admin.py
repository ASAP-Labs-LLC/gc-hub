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
import re
from pathlib import Path
from typing import Any, Optional

import instruments
import methods
import store

log = logging.getLogger("instrument_admin")

ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
NAME_MAX = 64
UID_MAX = 128
SKEW_WARN_SECONDS = 120
EDITABLE = frozenset({"name", "enabled", "method", "live_since", "lem_machine_uid"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

LIVE_SINCE_NOTE = ("live_since applies to samples received from now on: samples already received "
                   "keep their backfill flag (release them on the Backfill screen).")


class AdminError(ValueError):
    """A refused admin request: ``status`` (400/404/409) and ``extra`` JSON keys."""

    def __init__(self, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


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
    if not isinstance(value, str):
        raise AdminError("live_since must be a local date and time, e.g. 2026-10-01 08:00.")
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

def create(fields: Any, *, db: store.Db = None) -> dict:
    if not isinstance(fields, dict):
        raise AdminError("Expected an object of instrument fields.")
    fields = dict(fields)
    iid = fields.pop("id", None)
    if not isinstance(iid, str) or not ID_RE.match(iid):
        raise AdminError("The id must be 1-32 characters: a lower-case letter, then lower-case "
                         "letters, digits, '-' or '_' (e.g. gc2). It can't be changed later.")
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
    log.warning("instrument %s created (%s)", iid, row["name"])
    return public_row(row)


def update(instrument_id: Any, fields: Any, *, db: store.Db = None) -> tuple:
    """Change editable fields; ``(public row, warnings)``."""
    clean = _clean(fields, EDITABLE)
    if not clean:
        raise AdminError("Nothing to change.")
    with store.connection(db) as conn:
        with store.write_txn(conn):
            get(instrument_id, db=conn)
            row = store.instruments.upsert(dict(clean, id=instrument_id), db=conn)
    log.warning("instrument %s updated: %s", instrument_id, sorted(clean))
    warnings = live_since_warnings(instrument_id, db=db) if "live_since" in clean and \
        clean["live_since"] is not None else []
    return public_row(row), warnings


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


def set_export_path(instrument_id: str, path: Any, exporter) -> dict:
    """Point the instrument's export at ``path`` (absolute). A file already
    there must then be adopted before the hub appends to it."""
    import exports
    get(instrument_id, db=exporter.db)
    if not isinstance(path, str) or not path.strip() or not Path(path.strip()).is_absolute():
        raise AdminError("The export path must be an absolute path to a CSV file, e.g. "
                         r"\\asapserver\Labsharedrive\...\distill_results.csv.")
    try:
        exporter.new_path(instrument_id, path.strip())
    except exports.ExportRefused as err:
        raise _refused(err) from None
    log.warning("instrument %s export path set to %s", instrument_id, path.strip())
    return export_status(instrument_id, exporter)


def adopt_export(instrument_id: str, exporter, *, by: Optional[str]) -> dict:
    """Adopt the export file as it is now (a v1 file LEM already tails)."""
    import exports
    get(instrument_id, db=exporter.db)
    try:
        side = exporter.adopt(instrument_id, by=by)
    except exports.ExportRefused as err:
        raise _refused(err) from None
    return {"adopted": side, "status": export_status(instrument_id, exporter)}
