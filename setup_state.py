"""The GC setup guide's steps (v3.1; spec "GC setup guide").

Derived, never stored: ``steps(instrument_row, facts)`` computes the eight
steps from existing rows and is **pure** (no I/O, inputs untouched), so every
state is unit-tested (``tests/test_setup_state.py``). ``gather`` reads those
facts from the store; ``summary`` gives "Step N of 8" / "Ready".

Each step is ``{key, n, title, status, detail, blocker, action, done_by,
done_at}`` (plus ``unmapped`` on the method step):

* ``status`` — ``done`` (its condition holds), ``blocked`` (it can't be done
  until something else happens: ``blocker`` says what), ``current`` (the first
  step that is neither), ``waiting`` (later steps that could be done, but step
  order is advice: samples that arrive early wait and are re-queued).
* ``action`` — what the page offers: ``edit``, ``corrections``,
  ``installer``, ``calibration``, ``methods``, ``go_live`` or ``None``.
* ``done_by``/``done_at`` — from the newest ``instrument_events`` row of the
  step's kind (``by`` is ``web_auth.actor()``).

``facts`` (what ``gather`` returns)::

    corrections_count   rows in instrument_corrections (11 = complete)
    agent               {last_seen, version, host} | None
    calibration         instrument_admin.calibration_status(...)
    samples_received    all samples of the instrument
    backfill_unreleased backfill samples not released
    methods             {mapped_seen: [name], unmapped_seen: [{name, count}], held}
    export              {path, configured, refused}
    first_result        a final sample with export_rows.hub_appended_at
    events              store.instrument_events.latest_by_kind(...)
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

TOTAL = 8
CUTS = 11

TITLES = {
    "create": "Create the instrument",
    "corrections": "Enter the 11 correction factors",
    "installer": "Install the agent on the GC computer",
    "checkin": "Wait for the agent to check in",
    "calibration": "Set the calibration",
    "method": "Confirm the GC method",
    "go_live": "Choose the results file and go live",
    "first_result": "The first result",
}
ORDER = ("create", "corrections", "installer", "checkin", "calibration", "method", "go_live",
         "first_result")
EVENT_FOR = {"create": "created", "corrections": "corrections_saved", "installer": "installer",
             "calibration": "calibration_saved", "method": "method_mapped",
             "go_live": "live_since"}


def _n(key: str) -> int:
    return ORDER.index(key) + 1


def _waits_for(key: str) -> str:
    return f"Waits for step {_n(key)} ({TITLES[key].lower()})."


def _plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def steps(instrument_row: dict, facts: dict) -> list:
    """The eight setup steps for one instrument (see the module docstring)."""
    r = instrument_row
    f = facts
    name = r.get("name") or r.get("id") or "this GC"
    agent = f.get("agent") or {}
    seen = bool(agent.get("last_seen"))
    cal = f.get("calibration") or {}
    received = int(f.get("samples_received") or 0)
    backfill = int(f.get("backfill_unreleased") or 0)
    meth = f.get("methods") or {}
    mapped = list(meth.get("mapped_seen") or [])
    unmapped = list(meth.get("unmapped_seen") or [])
    held = int(meth.get("held") or 0)
    export = f.get("export") or {}
    ncorr = int(f.get("corrections_count") or 0)
    live = r.get("live_since")
    events = f.get("events") or {}
    disabled = not r.get("enabled", 1)
    refused_note = "This GC is disabled: its runs are refused. Enable it in Edit details."

    out: dict = {}

    def put(key, done, detail, blocker=None, action=None, **extra):
        out[key] = dict({"key": key, "n": _n(key), "title": TITLES[key], "done": bool(done),
                         "detail": detail, "blocker": None if done else blocker,
                         "action": action, "done_by": None, "done_at": None}, **extra)

    # 1 create
    uid = r.get("lem_machine_uid")
    lem = f.get("lem_title") or uid
    put("create", True,
        f"{name} is created. LEM machine: {lem}." if uid else
        f"{name} is created. No LEM machine is chosen yet (optional, but LabStation "
        f"routes results by it).", action="edit")

    # 2 correction factors
    if ncorr >= CUTS:
        put("corrections", True, "All 11 D86 correction factors are saved.", action="corrections")
    else:
        have = f"{ncorr} of 11 are entered. " if ncorr else ""
        put("corrections", False,
            f"{have}Until all 11 are saved, this GC's samples wait (pending corrections) and are "
            f"processed automatically once they are.", action="corrections")

    # 3 installer
    if r.get("token_issued_at"):
        put("installer", True, "The installer was downloaded; it holds this GC's key.",
            action="installer")
    elif seen:
        put("installer", False, "The agent's key was revoked: download a new installer and run "
                                "it on the GC computer.", action="installer")
    else:
        put("installer", False, "Download the installer, copy it to the GC computer and run "
                                "Install.", action="installer")

    # 4 agent checks in
    if seen:
        bits = [f"from {agent['host']}" if agent.get("host") else None,
                f"agent v{agent['version']}" if agent.get("version") else None]
        put("checkin", True, "The agent has checked in" +
            (" " + ", ".join(b for b in bits if b) if any(bits) else "") + ".",
            done_at=agent.get("last_seen"))
    elif disabled:
        put("checkin", False, f"As soon as the agent says hello, {name} shows as Live.",
            blocker=refused_note)
    elif not r.get("token_issued_at"):
        put("checkin", False, f"As soon as the agent says hello, {name} shows as Live.",
            blocker="Install the agent first (step 3).")
    else:
        put("checkin", False, f"Waiting for the agent's first check-in from the {name} "
                              f"computer. No button to press: this updates by itself.")

    # 5 calibration
    if cal.get("usable"):
        put("calibration", True,
            f"The calibration is usable: {_plural(int(cal.get('assigned') or 0), 'peak')} "
            f"assigned to carbon numbers.", action="calibration")
    else:
        problem = cal.get("problem") or "The calibration is not usable yet."
        if not cal.get("calibration_cdf") and received == 0:
            put("calibration", False, "Run the n-alkane standard on the GC, pick that run here "
                                      "and assign its peaks.", action="calibration",
                blocker="Run the n-alkane standard on the GC first: the calibration is picked "
                        "from this GC's received runs.")
        else:
            put("calibration", False,
                f"{problem} Samples wait (awaiting calibration) and are processed automatically "
                f"once it is usable.", action="calibration")

    # 6 method
    names = [m.get("name") for m in unmapped if m.get("name")]
    if mapped:
        put("method", True, f"Runs with {', '.join(mapped)} are processed.", action="methods",
            unmapped=names)
    elif received == 0:
        put("method", False, "The GC method is confirmed from the first run it sends.",
            action="methods", unmapped=names,
            blocker="Offered as soon as the first run arrives from the GC.")
    else:
        parts = []
        for m in unmapped:
            label = m.get("name") or "(no method name)"
            parts.append(f"{label} ({_plural(int(m.get('count') or 0), 'run')})")
        waiting = _plural(held, "run") + (" is" if held == 1 else " are")
        put("method", False,
            f"{waiting} waiting as another method until the method is mapped: "
            f"{', '.join(parts) if parts else 'none seen'}. Runs with no method name can't be "
            f"mapped; fix the method in ChemStation."
            if any(not m.get("name") for m in unmapped) else
            f"{waiting} waiting as another method until the method is mapped: "
            f"{', '.join(parts) if parts else 'none seen'}.",
            action="methods", unmapped=names)

    # 7 results file and go live
    # Only a configured export path is a file LEM reads. The hub's default
    # results/<id>_results.csv is its own: choosing it must be explicit (an
    # ``export_hub_only`` event), or "Ready" would hide that LEM gets nothing.
    path = export.get("path") or f"results/{r.get('id')}_results.csv"
    configured = bool(export.get("configured"))
    hub_only = not configured and "export_hub_only" in events
    decided = configured or hub_only
    if configured:
        file_text = f"Results are written to {path}, the file LEM reads."
    elif hub_only:
        file_text = (f"Results are written to the hub's own file {path}, as chosen: LEM won't "
                     f"see them.")
    else:
        file_text = (f"No results file for LEM is chosen yet: results would go to the hub's own "
                     f"file {path}, which LEM does not read. Choose the file LEM reads (or keep "
                     f"the hub-only file on purpose) before going live; rows written to the "
                     f"hub-only file never reach LEM, even after you change the path later.")
    now_local = f.get("now_local")
    future = bool(live and now_local and str(live) > str(now_local))
    if live and decided and not future:
        put("go_live", True, f"Live since {str(live)[:16]} (the GC's clock). {file_text}",
            action="go_live", needs_results_file=False)
    elif live and future:
        put("go_live", False, f"Goes live at {str(live)[:16]} (the GC's clock). {file_text}",
            action="go_live", needs_results_file=not decided,
            blocker=f"Waiting until {str(live)[:16]} (the GC's clock): runs before then are "
                    f"backfill.")
    elif live:
        put("go_live", False, f"Live since {str(live)[:16]} (the GC's clock), but {file_text[0].lower()}"
                              f"{file_text[1:]}", action="go_live", needs_results_file=True)
    else:
        so_far = (f"The {_plural(backfill, 'run')} received so far "
                  f"{'is' if backfill == 1 else 'are'} backfill. " if backfill else "")
        put("go_live", False,
            f"Not live yet: until you go live, every run counts as backfill and is never written "
            f"to a results file unless you release it. {so_far}{file_text}",
            action="go_live", needs_results_file=not decided)

    # 8 first result
    if f.get("first_result"):
        put("first_result", True, f"{name} has a final result, written to the results CSV"
                                  f"{'' if configured else ' (the hub-only file)'}.")
    else:
        prereqs = ["corrections"] + ([] if received else ["checkin"]) + \
            ["calibration", "method", "go_live"]
        missing = next((k for k in prereqs if not out[k]["done"]), None)
        if disabled:
            blocker = refused_note
        elif missing == "go_live":
            blocker = (_waits_for("go_live") + " Until then runs are backfill and never written "
                                               "to the results file.")
        elif missing:
            blocker = _waits_for(missing)
        elif export.get("refused"):
            blocker = (f"The results file was refused ({export['refused']}): fix it in Results "
                       f"file / export.")
        else:
            blocker = None
        put("first_result", False,
            f"Run any sample on {name}. It shows up within a minute, is processed and written to "
            f"the results CSV.", blocker=blocker)

    # statuses
    current_taken = False
    result = []
    for key in ORDER:
        s = out[key]
        if s["done"]:
            s["status"] = "done"
            ev = events.get(EVENT_FOR.get(key, ""))
            if ev:
                s["done_by"] = ev.get("by")
                s["done_at"] = ev.get("at")
        elif s["blocker"]:
            s["status"] = "blocked"
        elif not current_taken:
            s["status"] = "current"
            current_taken = True
        else:
            s["status"] = "waiting"
        del s["done"]
        result.append(s)
    return result


def summary(step_list: list) -> dict:
    """``{total, done, step, ready}``: ``step`` is the current step's number
    ("Step N of 8"), else the first unfinished one; ``None`` once ready."""
    done = sum(1 for s in step_list if s["status"] == "done")
    cur = next((s["n"] for s in step_list if s["status"] == "current"), None)
    if cur is None:
        cur = next((s["n"] for s in step_list if s["status"] != "done"), None)
    return {"total": len(step_list), "done": done, "step": cur, "ready": done == len(step_list)}


# ── the facts, from the store ───────────────────────────────────────────────

def gather(instrument_id: str, conf: dict, *, db: Any = None, data_dir: Any = None,
           export: Optional[dict] = None) -> dict:
    """Read ``steps``'s facts for one instrument. ``export`` is the exporter's
    status when the caller has one (``instrument_admin.export_status``);
    otherwise the path is the row's, or the default results file."""
    import instrument_admin as ia
    import instruments
    import store

    row = ia.get(instrument_id, db=db)
    try:
        cal = ia.calibration_status(instrument_id, conf, db=db, data_dir=data_dir)
    except Exception as exc:  # noqa: BLE001 - a broken calibration is "not usable", not a 500
        cal = {"usable": False, "problem": f"The calibration can't be checked: {exc}",
               "calibration_cdf": "", "assigned": 0}
    mapping = instruments.method_map(row)
    with store.connection(db) as conn:
        ncorr = conn.execute("SELECT COUNT(*) FROM instrument_corrections WHERE instrument_id=?",
                             (instrument_id,)).fetchone()[0]
        a = conn.execute("SELECT last_seen, version, host FROM agents WHERE instrument_id=?",
                         (instrument_id,)).fetchone()
        received = conn.execute("SELECT COUNT(*) FROM samples WHERE instrument_id=?",
                                (instrument_id,)).fetchone()[0]
        backfill = conn.execute("SELECT COUNT(*) FROM samples WHERE instrument_id=? AND backfill=1 "
                                "AND released_at IS NULL", (instrument_id,)).fetchone()[0]
        held = conn.execute("SELECT COUNT(*) FROM samples WHERE instrument_id=? AND status IN "
                            "('other_method', 'review_method')", (instrument_id,)).fetchone()[0]
        first = conn.execute(
            "SELECT 1 FROM samples s JOIN export_rows e ON e.sample_id=s.id WHERE "
            "s.instrument_id=? AND s.status='final' AND e.hub_appended_at IS NOT NULL LIMIT 1",
            (instrument_id,)).fetchone()
        seen = [dict(m) for m in store.samples.methods_seen(instrument_id, db=conn)]
    mapped, unmapped = [], []
    for m in seen:
        nm = m.get("method_name") or ""
        if nm and mapping.get(nm):
            mapped.append(nm)
        else:
            unmapped.append({"name": nm, "count": int(m.get("count") or 0)})
    if export is None:
        configured = bool((row.get("export_path") or "").strip())
        path = row["export_path"].strip() if configured else str(
            (data_dir if data_dir is not None else _data_dir()) / "results" /
            f"{instrument_id}_results.csv")
        export = {"path": path, "configured": configured, "refused": None}
    return {
        "corrections_count": int(ncorr),
        "agent": dict(a) if a is not None else None,
        "calibration": cal,
        "samples_received": int(received),
        "backfill_unreleased": int(backfill),
        "methods": {"mapped_seen": sorted(mapped), "unmapped_seen": unmapped, "held": int(held)},
        "export": {"path": export.get("path"), "configured": bool(export.get("configured")),
                   "refused": export.get("refused")},
        "first_result": first is not None,
        "events": store.instrument_events.latest_by_kind(instrument_id, db=db),
        # the hub's local clock, to tell a live_since still in the future
        "now_local": store.local_dt(datetime.now().replace(microsecond=0)),
        "lem_title": _lem_title(conf, row.get("lem_machine_uid")),
    }


def _lem_title(conf: dict, uid: Optional[str]) -> Optional[str]:
    """The LEM machine's title from the cached list (never a fetch), or None."""
    if not uid:
        return None
    try:
        import lem_machines
        return lem_machines.CACHE.title(lem_machines.resolve_url(conf), uid)
    except Exception:  # noqa: BLE001 - a title is a nicety; the uid is shown instead
        return None


def _data_dir():
    from pathlib import Path
    import paths
    return Path(paths.data_dir() or ".")


def for_instrument(instrument_id: str, conf: dict, *, db: Any = None, data_dir: Any = None,
                   export: Optional[dict] = None) -> dict:
    """``{instrument_id, steps, summary}``: what ``GET /api/instruments/<id>/setup`` serves."""
    import instrument_admin as ia
    row = ia.get(instrument_id, db=db)
    st = steps(row, gather(instrument_id, conf, db=db, data_dir=data_dir, export=export))
    return {"instrument_id": instrument_id, "steps": st, "summary": summary(st)}


class SummaryCache:
    """``summary(steps(row, gather(...)))`` per instrument, reused for ``ttl``
    seconds (the Instruments list and the 5 s fallback poll ask for every
    instrument each time). An answer is recomputed at once when the row's
    ``updated_at`` or the instrument's newest event changes, so an admin
    change shows immediately; a heartbeat or a new sample shows within ``ttl``."""

    def __init__(self, ttl: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict = {}

    def summary(self, row: dict, conf: dict, *, db: Any = None, data_dir: Any = None) -> dict:
        import store
        iid = row["id"]
        with store.connection(db) as conn:
            last_event = conn.execute("SELECT MAX(id) FROM instrument_events WHERE instrument_id=?",
                                      (iid,)).fetchone()[0]
        key = (str(db), iid)
        mark = (row.get("updated_at"), last_event)
        now = self._clock()
        with self._lock:
            hit = self._items.get(key)
            if hit and hit[0] == mark and now - hit[1] <= self.ttl:
                return dict(hit[2])
        out = summary(steps(row, gather(iid, conf, db=db, data_dir=data_dir)))
        with self._lock:
            self._items[key] = (mark, now, out)
        return dict(out)


SUMMARIES = SummaryCache()
