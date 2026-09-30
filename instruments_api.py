"""The Instruments page and its JSON routes (phase 2, 2A2): a Flask Blueprint.

The operations live in ``instrument_admin`` (and ``standards``); this module
is the HTTP layer. Reads are open ``GET``s, like ``/api/agents``. **Every
state change** is a ``POST`` under ``/api/admin/`` that needs the admin
password in a JSON body (``Content-Type: application/json``, at most 64 KiB;
``ingest_api._admin_body``), on top of app.py's cross-site guard. Refusals
are ``{error, ...}`` with 400/404/409 (``instrument_admin.AdminError``).
``by`` in audit trails is ``admin@<client address>``.

Routes::

    GET  /instruments                                        the page (v4.0 design)
    GET  /instruments/classic                                the 2A2 page (this release only)
    GET  /instruments/<id>                                   one instrument (v4.0 design)
    GET  /setup[?instrument=|new=1]                          the setup guide (v4.0)
    GET  /api/instruments/activity[?limit=]                  the Activity feed (v4.0)
    GET  /api/instruments/<id>/setup                         the eight setup steps (v4.0)
    GET  /api/instruments                                    every instrument + summary
    GET  /api/instruments/<id>                               one, with corrections/methods/export
    POST /api/admin/instruments                              {id, name, ...} create (201)
    POST /api/admin/instruments/<id>                         {name|enabled|method|live_since|lem_machine_uid}
    POST /api/admin/instruments/<id>/export-path             {path}
    POST /api/admin/instruments/<id>/export-adopt            {}
    POST /api/admin/instruments/<id>/export-hub-only         {} keep the hub-only file (v4.0)
    GET  /api/instruments/<id>/calibration[?sensitivity=]    the Calibration page payload
    GET  /api/instruments/<id>/calibration-candidates[?q=]   the instrument's own samples
    POST /api/admin/instruments/<id>/calibration-cdf         {sample_id | path}
    POST /api/admin/instruments/<id>/calibration             {assignments, sensitivity}
    GET  /api/instruments/<id>/corrections                   values, source, audit
    POST /api/admin/instruments/<id>/corrections             {values, reason}
    POST /api/admin/instruments/<id>/corrections/seed        {} (gc1 only, once)
    GET  /api/instruments/<id>/methods                       methods seen
    POST /api/admin/instruments/<id>/methods                 {method_name, hub_method | null}
    POST /api/admin/instruments/<id>/review-method           {sample_ids?} -> other_method
    GET  /api/instruments/<id>/backfill[?q&status&released&limit&offset]
    POST /api/admin/instruments/<id>/backfill/release        {sample_ids}
    GET  /api/conflicts[?instrument=&include_resolved=1]
    POST /api/admin/conflicts/<cid>/keep                     {}
    POST /api/admin/conflicts/<cid>/replace                  {}
    GET  /api/standards[?instrument=|for_instrument=]        D12
    POST /api/admin/standards/<sid>/instrument               {instrument_id | null}
    GET  /api/lem/machines                                   D10: LEM's machines (dropdown)

**The open GETs** (by design, like ``/api/agents``: the hub has no login and
listens on the lab LAN) show instrument settings, correction values and their
history, method names, backfill and conflict listings (hashes and stored file
names, never server paths) and the standards list; they never show a token or
its hash. Only changes are gated. The calibration GET runs peak detection, so
it takes one slot at a time (``PEAK_WAIT_SECONDS``, else 429).

The agent panel uses 2B1's routes (``/api/agents``, installer, revoke-token,
agent-command, hub-url; ``ingest_api``).

**Exporter.** Export status, new path and adopt go through the hub's running
``exports.HubExporter`` when start-up registers it with
``set_exporter(exporter)`` (so a refusal it recorded clears on adopt);
otherwise through one of this module's own on the same store.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, jsonify, render_template, request

import admin_auth
import ingest_api
import instrument_activity
import instrument_admin as ia
import lem_machines
import paths
import setup_state
import standards
import store
import version
import web_auth

log = logging.getLogger("instruments_api")

bp = Blueprint("instruments_api", __name__)

_exporter_lock = threading.Lock()
_exporter = None


def set_exporter(exporter) -> None:
    """Start-up (T5) registers the hub's running ``HubExporter`` here."""
    global _exporter
    with _exporter_lock:
        _exporter = exporter


def _get_exporter():
    global _exporter
    with _exporter_lock:
        if _exporter is None:
            import exports
            _exporter = exports.HubExporter(_db(), data_dir=paths.data_dir())
        return _exporter


def _db():
    return admin_auth.hub_db()


def _data():
    return paths.data_dir()


def _conf() -> dict:
    import settings
    return settings.load_settings()


def _comp_dir(conf: Optional[dict] = None) -> Path:
    conf = conf if conf is not None else _conf()
    return Path(conf.get("comparison_defaults_dir") or str(paths.standards_dir()))


def _by() -> str:
    return web_auth.actor()


def _err(message: str, status: int = 400, **extra):
    return jsonify(dict(extra, error=message)), status


@bp.errorhandler(ia.AdminError)
def _admin_error(exc: ia.AdminError):
    return _err(exc.message, exc.status, **exc.extra)


@bp.errorhandler(admin_auth.NoStore)
def _no_store(exc):
    return _err(str(exc), 503)


def _admin():
    """``(body, None)`` or ``(None, error response)``: JSON, 64 KiB, password."""
    return ingest_api._admin_body()


# ── summaries ───────────────────────────────────────────────────────────────

def _counts(db) -> dict:
    out: dict = {}
    with store.connection(db) as conn:
        for r in conn.execute("SELECT instrument_id, status, COUNT(*) AS n FROM samples "
                              "GROUP BY instrument_id, status"):
            out.setdefault(r["instrument_id"], {})[r["status"]] = r["n"]
        backfill = {r["instrument_id"]: r["n"] for r in conn.execute(
            "SELECT instrument_id, COUNT(*) AS n FROM samples WHERE backfill=1 "
            "AND released_at IS NULL GROUP BY instrument_id")}
        conflicts = {r["instrument_id"]: r["n"] for r in conn.execute(
            "SELECT instrument_id, COUNT(*) AS n FROM conflicts WHERE resolved IS NULL "
            "GROUP BY instrument_id")}
        corr = {r["instrument_id"]: r["n"] for r in conn.execute(
            "SELECT instrument_id, COUNT(*) AS n FROM instrument_corrections GROUP BY instrument_id")}
        today = {r["instrument_id"]: r["n"] for r in conn.execute(
            "SELECT instrument_id, COUNT(*) AS n FROM samples WHERE received_at >= ? "
            "AND backfill=0 GROUP BY instrument_id", (_local_midnight_utc(),))}
        pending = {r["instrument_id"]: r["n"] for r in conn.execute(
            "SELECT instrument_id, COUNT(*) AS n FROM export_rows WHERE hub_appended_at IS NULL "
            "GROUP BY instrument_id")}
    return {"status": out, "backfill": backfill, "conflicts": conflicts, "corrections": corr,
            "today": today, "export_pending": pending}


# "Held" on the Instruments cards: samples waiting for something an admin fixes.
HELD_STATUSES = ("awaiting_calibration", "pending_corrections", "other_method", "review_method")


def _hub_local_now() -> str:
    """The hub's local clock in live_since's form, for "Go live now"."""
    from datetime import datetime
    return store.local_dt(datetime.now().replace(microsecond=0))


def _local_midnight_utc() -> str:
    """Today's local midnight, as the store's UTC timestamp form (``received_at``)."""
    from datetime import datetime, timezone
    now = datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc).isoformat(timespec="microseconds")


def _summary(row: dict, conf: dict, db, counts: dict, agents: dict) -> dict:
    out = ia.public_row(row)
    iid = row["id"]
    try:
        out["calibration"] = ia.calibration_status(iid, conf, db=db, data_dir=_data())
    except Exception as exc:  # noqa: BLE001 - one bad row must not blank the page
        log.exception("calibration status for %s", iid)
        out["calibration"] = {"usable": False, "problem": f"could not check: {exc}"}
    out["counts"] = counts["status"].get(iid, {})
    out["backfill_unreleased"] = counts["backfill"].get(iid, 0)
    out["open_conflicts"] = counts["conflicts"].get(iid, 0)
    out["corrections_set"] = counts["corrections"].get(iid, 0) > 0
    out["agent"] = agents.get(iid)
    # v4.0: the cards' numbers and "Step N of 8" / "Ready"
    out["today"] = counts["today"].get(iid, 0)
    out["held"] = sum(out["counts"].get(s, 0) for s in HELD_STATUSES)
    out["export_pending"] = counts["export_pending"].get(iid, 0)
    try:
        out["setup"] = setup_state.SUMMARIES.summary(row, conf, db=db, data_dir=_data())
    except Exception:  # noqa: BLE001 - one bad row must not blank the page
        log.exception("setup state for %s", iid)
        out["setup"] = None
    return out


# ── pages (v4.0: the new design; the 2A2 page stays at /instruments/classic) ──

def _page(template: str, **extra):
    return render_template(template, app_version=version.APP_VERSION, **extra)


@bp.route("/instruments", methods=["GET"])
def instruments_page():
    return _page("instruments_home.html", nav="instruments")


@bp.route("/instruments/classic", methods=["GET"])
def instruments_classic_page():
    return render_template("instruments.html", app_version=version.APP_VERSION)


@bp.route("/instruments/<iid>", methods=["GET"])
def instrument_detail_page(iid):
    if store.instruments.get(iid, db=_db()) is None:
        return _page("instrument_missing.html", nav="instruments", instrument_id=iid), 404
    if iid in ia.UNREACHABLE_IDS:
        # /api/instruments/<iid> is shadowed by a hub route for this id (made
        # before the id was reserved): the classic page can still manage it
        from flask import redirect
        return redirect(f"/instruments/classic?instrument={iid}")
    return _page("instrument_detail.html", nav="instruments", instrument_id=iid)


@bp.route("/setup", methods=["GET"])
def setup_page():
    return _page("setup_guide.html", nav="setup")


@bp.route("/api/instruments", methods=["GET"])
def api_instruments():
    db = _db()
    conf = _conf()
    counts = _counts(db)
    agents = {a["instrument_id"]: a for a in ingest_api.agents_status(db=db)}
    import methods
    return jsonify({
        "instruments": [_summary(r, conf, db, counts, agents) for r in store.instruments.list(db=db)],
        "hub_methods": methods.names(),
        "hub_url": ingest_api.configured_hub_url(db=db),
        "hub_url_effective": ingest_api.effective_hub_url(db=db),
        "agent_commands": list(ingest_api.AGENT_COMMANDS),
        "skew_warn_seconds": ia.SKEW_WARN_SECONDS,
        "hub_local_now": _hub_local_now(),
    })


@bp.route("/api/instruments/activity", methods=["GET"])
def api_activity():
    """The Activity feed: ``{entries: [...]}``, newest first (``instrument_activity``)."""
    try:
        limit = instrument_activity.clamp_limit(request.args.get("limit"))
    except ValueError:
        return _err("limit must be an integer")
    return jsonify({"entries": instrument_activity.feed(limit, db=_db())})


def _export_status(iid: str) -> dict:
    try:
        return ia.export_status(iid, _get_exporter())
    except ia.AdminError:
        raise
    except Exception as exc:  # noqa: BLE001 - e.g. an unreachable share
        log.exception("export status for %s", iid)
        return {"error": str(exc)}


@bp.route("/api/instruments/<iid>", methods=["GET"])
def api_instrument(iid):
    db = _db()
    conf = _conf()
    row = ia.get(iid, db=db)
    counts = _counts(db)
    agents = {a["instrument_id"]: a for a in ingest_api.agents_status(db=db)}
    return jsonify({
        "instrument": _summary(row, conf, db, counts, agents),
        "corrections": ia.corrections_view(iid, conf, db=db),
        "methods": ia.methods_view(iid, db=db),
        "export": _export_status(iid),
        "live_since_warnings": ia.live_since_warnings(iid, db=db)[1:],
        "hub_local_now": _hub_local_now(),
    })


@bp.route("/api/instruments/<iid>/setup", methods=["GET"])
def api_setup(iid):
    """The setup guide's eight steps for one instrument (``setup_state``)."""
    ia.get(iid, db=_db())
    ex = _export_status(iid)
    export = ex if "path" in ex else None
    return jsonify(setup_state.for_instrument(iid, _conf(), db=_db(), data_dir=_data(),
                                              export=export))


# ── create / update / export ────────────────────────────────────────────────

def _fields(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "password"}


@bp.route("/api/admin/instruments", methods=["POST"])
def api_create_instrument():
    body, err = _admin()
    if err:
        return err
    row = ia.create(_fields(body), db=_db(), by=_by())
    return jsonify({"instrument": row}), 201


@bp.route("/api/admin/instruments/<iid>", methods=["POST"])
def api_update_instrument(iid):
    body, err = _admin()
    if err:
        return err
    row, warnings = ia.update(iid, _fields(body), db=_db(), by=_by())
    return jsonify({"instrument": row, "warnings": warnings})


@bp.route("/api/admin/instruments/<iid>/export-path", methods=["POST"])
def api_export_path(iid):
    body, err = _admin()
    if err:
        return err
    return jsonify({"export": ia.set_export_path(iid, body.get("path"), _get_exporter(), by=_by())})


@bp.route("/api/admin/instruments/<iid>/export-hub-only", methods=["POST"])
def api_export_hub_only(iid):
    """v4.0: keep the hub's own results file on purpose (LEM won't see it)."""
    _body, err = _admin()
    if err:
        return err
    return jsonify({"export": ia.keep_hub_only_export(iid, _get_exporter(), by=_by())})


@bp.route("/api/admin/instruments/<iid>/export-adopt", methods=["POST"])
def api_export_adopt(iid):
    _body, err = _admin()
    if err:
        return err
    out = ia.adopt_export(iid, _get_exporter(), by=_by())
    return jsonify({"export": out["status"], "adopted_size": out["adopted"].get("size")})


# ── calibration ─────────────────────────────────────────────────────────────

# This open GET runs peak detection on a CDF: one at a time, and a caller that
# can't get the slot soon is told to retry (429) instead of queueing CPU work.
PEAK_WAIT_SECONDS = 5.0
_PEAK_SLOTS = threading.BoundedSemaphore(1)


@bp.route("/api/instruments/<iid>/calibration", methods=["GET"])
def api_calibration(iid):
    if not _PEAK_SLOTS.acquire(timeout=PEAK_WAIT_SECONDS):
        return _err("Peak detection is busy; try again in a moment.", 429)
    try:
        return jsonify(ia.calibration_view(iid, _conf(), request.args.get("sensitivity"),
                                           db=_db(), data_dir=_data()))
    finally:
        _PEAK_SLOTS.release()


@bp.route("/api/instruments/<iid>/calibration-candidates", methods=["GET"])
def api_calibration_candidates(iid):
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return _err("limit must be an integer")
    return jsonify({"candidates": ia.calibration_candidates(iid, request.args.get("q"), limit,
                                                            db=_db())})


@bp.route("/api/admin/instruments/<iid>/calibration-cdf", methods=["POST"])
def api_calibration_cdf(iid):
    body, err = _admin()
    if err:
        return err
    st = ia.set_calibration_cdf(iid, _conf(), sample_id=body.get("sample_id"),
                                path=body.get("path"), db=_db(), data_dir=_data(), by=_by())
    return jsonify({"calibration": st})


@bp.route("/api/admin/instruments/<iid>/calibration", methods=["POST"])
def api_calibration_save(iid):
    body, err = _admin()
    if err:
        return err
    return jsonify(ia.save_calibration(iid, body.get("assignments"), body.get("sensitivity"),
                                       _conf(), db=_db(), data_dir=_data(), by=_by()))


# ── corrections ─────────────────────────────────────────────────────────────

@bp.route("/api/instruments/<iid>/corrections", methods=["GET"])
def api_corrections(iid):
    return jsonify(ia.corrections_view(iid, _conf(), db=_db()))


@bp.route("/api/admin/instruments/<iid>/corrections", methods=["POST"])
def api_corrections_save(iid):
    body, err = _admin()
    if err:
        return err
    return jsonify(ia.save_corrections(iid, body.get("values"), body.get("reason"), by=_by(),
                                       db=_db()))


@bp.route("/api/admin/instruments/<iid>/corrections/seed", methods=["POST"])
def api_corrections_seed(iid):
    _body, err = _admin()
    if err:
        return err
    if iid != "gc1":
        return _err("Only GC-1 is seeded from the phase-1 corrections file; enter the other "
                    "instruments' values in the editor.")
    return jsonify(ia.seed_gc1(_conf(), by=_by(), db=_db()))


# ── methods ─────────────────────────────────────────────────────────────────

@bp.route("/api/instruments/<iid>/methods", methods=["GET"])
def api_methods(iid):
    return jsonify(ia.methods_view(iid, db=_db()))


@bp.route("/api/admin/instruments/<iid>/methods", methods=["POST"])
def api_methods_set(iid):
    body, err = _admin()
    if err:
        return err
    return jsonify(ia.set_method_mapping(iid, body.get("method_name"), body.get("hub_method"),
                                         db=_db(), by=_by()))


@bp.route("/api/admin/instruments/<iid>/review-method", methods=["POST"])
def api_review_method(iid):
    body, err = _admin()
    if err:
        return err
    return jsonify({"marked": ia.mark_review_other(iid, body.get("sample_ids"), db=_db())})


# ── backfill ────────────────────────────────────────────────────────────────

def _flag(name: str) -> Any:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    low = raw.strip().lower()
    if low in ("1", "true", "yes"):
        return True
    if low in ("0", "false", "no"):
        return False
    raise ia.AdminError(f"{name} must be true or false.")


@bp.route("/api/instruments/<iid>/backfill", methods=["GET"])
def api_backfill(iid):
    a = request.args
    return jsonify(ia.backfill_list(iid, a.get("q"), a.get("status") or None, _flag("released"),
                                    a.get("limit", 100), a.get("offset", 0), db=_db()))


@bp.route("/api/admin/instruments/<iid>/backfill/release", methods=["POST"])
def api_backfill_release(iid):
    body, err = _admin()
    if err:
        return err
    results = ia.release(iid, body.get("sample_ids"), by=_by(), db=_db(), data_dir=_data())
    if any(r.get("ok") for r in results):
        _wake_exports()         # export rows were written outside the Worker: flush now
    return jsonify({"results": results})


def _wake_exports() -> None:
    """Ask the running hub's exporter for a pass now (not after its interval)."""
    try:
        import hub
        rt = hub.running()
        if rt is not None:
            rt.wake_exports()
    except Exception:  # noqa: BLE001 - the rows are written; the next tick flushes them
        log.exception("could not wake the exporter")


# ── conflicts ───────────────────────────────────────────────────────────────

@bp.route("/api/conflicts", methods=["GET"])
def api_conflicts():
    inst = request.args.get("instrument") or None
    return jsonify({"conflicts": ia.conflicts_list(inst, bool(_flag("include_resolved")),
                                                   db=_db(), data_dir=_data())})


@bp.route("/api/admin/conflicts/<cid>/keep", methods=["POST"])
def api_conflict_keep(cid):
    _body, err = _admin()
    if err:
        return err
    ia.keep_existing(cid, by=_by(), db=_db())
    return jsonify({"ok": True})


@bp.route("/api/admin/conflicts/<cid>/replace", methods=["POST"])
def api_conflict_replace(cid):
    _body, err = _admin()
    if err:
        return err
    job = ia.replace(cid, by=_by(), db=_db(), data_dir=_data())
    return jsonify({"ok": True, "job_id": job})


# ── standards (D12) ─────────────────────────────────────────────────────────

@bp.route("/api/standards", methods=["GET"])
def api_standards():
    db = _db()
    comp = _comp_dir()
    target = request.args.get("for_instrument")
    if target:
        return jsonify({"standards": standards.for_sample(comp, target, db=db)})
    return jsonify({"standards": standards.list_standards(comp, request.args.get("instrument") or None,
                                                          db=db)})


@bp.route("/api/admin/standards/<sid>/instrument", methods=["POST"])
def api_standard_instrument(sid):
    body, err = _admin()
    if err:
        return err
    try:
        sid_int = int(sid)
    except ValueError:
        return _err("standard id must be an integer")
    inst = body.get("instrument_id")
    if inst is not None and not isinstance(inst, str):
        return _err("instrument_id must be an instrument id or null")
    try:
        row = standards.set_instrument(sid_int, inst or None, db=_db())
    except LookupError as exc:
        return _err(str(exc), 404)
    return jsonify({"standard": row})


# ── LEM machines (D10) ──────────────────────────────────────────────────────

@bp.route("/api/lem/machines", methods=["GET"])
def api_lem_machines():
    """LEM's machine list for the LEM machine dropdown, fetched server-side
    (``lem_machines``: 60 s cache, stale on failure, never writes to LEM).
    ``{machines: [{uid, title, status, closed}], source: live|cached|unavailable,
    age_seconds, error?}``; ``error`` is generic (no URL, no internals)."""
    resp = jsonify(lem_machines.machines(lem_machines.resolve_url(_conf())))
    resp.headers["Cache-Control"] = "no-store"
    return resp
