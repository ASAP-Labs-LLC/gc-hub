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

from typing import Any, Optional

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

    out: dict = {}

    def put(key, done, detail, blocker=None, action=None, **extra):
        out[key] = dict({"key": key, "n": _n(key), "title": TITLES[key], "done": bool(done),
                         "detail": detail, "blocker": None if done else blocker,
                         "action": action, "done_by": None, "done_at": None}, **extra)

    # 1 create
    uid = r.get("lem_machine_uid")
    put("create", True,
        f"{name} is created. LEM machine: {uid}." if uid else
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
    path = export.get("path") or f"results/{r.get('id')}_results.csv"
    if live:
        put("go_live", True, f"Live since {live} (the GC's clock). Results are written to "
                             f"{path}.", action="go_live")
    else:
        so_far = (f"The {_plural(backfill, 'run')} received so far "
                  f"{'is' if backfill == 1 else 'are'} backfill. " if backfill else "")
        put("go_live", False,
            f"Not live yet: until you go live, every run counts as backfill and is never written "
            f"to the results file LEM reads ({path}) unless you release it. {so_far}".strip(),
            action="go_live")

    # 8 first result
    if f.get("first_result"):
        put("first_result", True, f"{name} has a final result, written to the results CSV.")
    else:
        prereqs = ["corrections"] + ([] if received else ["checkin"]) + \
            ["calibration", "method", "go_live"]
        missing = next((k for k in prereqs if not out[k]["done"]), None)
        if missing == "go_live":
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
            ev = (f.get("events") or {}).get(EVENT_FOR.get(key, ""))
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
    }


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
