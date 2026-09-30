"""The Instruments page's Activity feed (v3.1; spec "instrument_events").

``feed(limit, *, db)`` merges, newest first:

* ``instrument_events`` rows (schema v4: installer, token revoked, calibration,
  method mapped, export path, ``live_since``, created), except
  ``corrections_saved``, which comes from the audit below instead;
* ``samples.received_at`` -> ``sample_received``;
* ``corrections_audit``, one entry per save (rows sharing ``changed_at``,
  ``changed_by`` and ``reason``) -> ``corrections_saved``;
* ``report_log`` -> ``report`` (``detail.report_kind``: download, zip, qbench);
* ``export_rows.hub_appended_at`` -> ``export_written`` (written to the
  results CSV);
* ``agents.last_seen`` -> ``agent_seen`` (one per agent: its last check-in).

Every entry is ``{key, kind, at, instrument_id, instrument_name, by,
sample_id, lab_id, injection_dt, detail}``. ``key`` is stable for the same fact, so the page
prepends only keys it has not shown (an agent's key stays ``agent:<id>``: its
entry moves up on each check-in). Each source is read newest-first with the
limit, then merged, so the work is bounded by ``limit`` per source. Strings
here come from agents and users: the page renders them as text only.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import store

DEFAULT_LIMIT = 30
MAX_LIMIT = 200


def clamp_limit(raw: Any) -> int:
    """The ``?limit=`` value: default 30, clamped to 1..200; ``ValueError`` if not a number."""
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    n = int(raw)
    return max(1, min(n, MAX_LIMIT))


def _sort_key(at: Optional[str]) -> float:
    if not at:
        return 0.0
    try:
        dt = datetime.fromisoformat(at)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _entry(key, kind, at, iid, names, by=None, sample_id=None, lab_id=None, detail=None,
           injection_dt=None) -> dict:
    return {"key": key, "kind": kind, "at": at, "instrument_id": iid,
            "instrument_name": names.get(iid, iid), "by": by, "sample_id": sample_id,
            "lab_id": lab_id, "injection_dt": injection_dt, "detail": detail}


def feed(limit: int = DEFAULT_LIMIT, *, db: store.Db = None) -> list:
    limit = clamp_limit(limit)
    out: list = []
    with store.connection(db) as conn:
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM instruments")}
        for r in conn.execute(
                "SELECT * FROM instrument_events WHERE kind != 'corrections_saved' "
                "ORDER BY at DESC, id DESC LIMIT ?", (limit,)):
            try:
                detail = json.loads(r["detail"]) if r["detail"] else None
            except ValueError:
                detail = None
            out.append(_entry(f"event:{r['id']}", r["kind"], r["at"], r["instrument_id"], names,
                              by=r["by"], detail=detail))
        for r in conn.execute(
                "SELECT id, instrument_id, lab_id, injection_dt, received_at, status, source_name FROM samples "
                "ORDER BY received_at DESC, id DESC LIMIT ?", (limit,)):
            out.append(_entry(f"received:{r['id']}", "sample_received", r["received_at"],
                              r["instrument_id"], names, sample_id=r["id"], lab_id=r["lab_id"],
                              injection_dt=r["injection_dt"],
                              detail={"status": r["status"], "source_name": r["source_name"]}))
        for r in conn.execute(
                "SELECT instrument_id, changed_at, changed_by, reason, COUNT(*) AS n, "
                "SUM(CASE WHEN old_value IS NOT new_value THEN 1 ELSE 0 END) AS changed "
                "FROM corrections_audit GROUP BY instrument_id, changed_at, changed_by, reason "
                "ORDER BY changed_at DESC LIMIT ?", (limit,)):
            out.append(_entry(f"corrections:{r['instrument_id']}:{r['changed_at']}",
                              "corrections_saved", r["changed_at"], r["instrument_id"], names,
                              by=r["changed_by"],
                              detail={"changed": int(r["changed"] or 0), "reason": r["reason"]}))
        for r in conn.execute(
                "SELECT l.id, l.kind, l.created_at, l.user_name, s.id AS sample_id, s.lab_id, s.injection_dt, "
                "s.instrument_id FROM report_log l JOIN samples s ON s.id=l.sample_id "
                "ORDER BY l.created_at DESC, l.id DESC LIMIT ?", (limit,)):
            out.append(_entry(f"report:{r['id']}", "report", r["created_at"], r["instrument_id"],
                              names, by=r["user_name"], sample_id=r["sample_id"],
                              lab_id=r["lab_id"], injection_dt=r["injection_dt"],
                              detail={"report_kind": r["kind"]}))
        for r in conn.execute(
                "SELECT e.seq, e.instrument_id, e.hub_appended_at, s.id AS sample_id, s.lab_id, s.injection_dt "
                "FROM export_rows e JOIN samples s ON s.id=e.sample_id "
                "WHERE e.hub_appended_at IS NOT NULL "
                "ORDER BY e.hub_appended_at DESC, e.seq DESC LIMIT ?", (limit,)):
            out.append(_entry(f"export:{r['seq']}", "export_written", r["hub_appended_at"],
                              r["instrument_id"], names, sample_id=r["sample_id"],
                              lab_id=r["lab_id"], injection_dt=r["injection_dt"]))
        for r in conn.execute(
                "SELECT instrument_id, last_seen, host, version FROM agents "
                "WHERE last_seen IS NOT NULL ORDER BY last_seen DESC LIMIT ?", (limit,)):
            out.append(_entry(f"agent:{r['instrument_id']}", "agent_seen", r["last_seen"],
                              r["instrument_id"], names,
                              detail={"host": r["host"], "version": r["version"]}))
    out.sort(key=lambda e: _sort_key(e["at"]), reverse=True)
    return out[:limit]
