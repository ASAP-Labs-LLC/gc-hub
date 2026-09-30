"""sample_links.py: sendable sample links (v3.1). A Blueprint registered by
``app.py``; spec ``docs/superpowers/specs/2026-09-30-ui-redesign-live-setup-design.md``
("Sendable sample links").

Pages (session; they render the classic main page, and ``static/js/deeplink.js``
selects the sample and the tab from the URL)::

    GET /lab/<lab_id>                         the lab ID's newest run
    GET /samples/<id>                         one run, Dashboard
    GET /samples/<id>/compare[?standard=<n>]  one run, Analysis (standard picked)
    GET /samples/<id>/data                    one run, Distillation Data

An unknown lab ID or sample id gets a friendly 404 page ("No GC result for
lab ID … yet", with a link to search). COA Reviewer and other apps link to
``/lab/<lab_id>``: they know lab IDs, not hub sample ids.

API (session)::

    GET /api/lab/<lab_id> → {lab_id, sample_id, runs: [{sample_id, lab_id,
        instrument, instrument_name, injection_dt, status}]}   newest first; 404

**Resolution.** The lab ID is matched exactly, else ASCII case-insensitively
(SQLite NOCASE: non-ASCII letters must match exactly; never a substring), both
through an index (``store.samples.with_lab_id``). The run opened is the one with the
latest ``injection_dt`` among the final runs, else among all runs (ties: the
newest id), across instruments. The URL is decoded once, by the server; ``/``
cannot be in a lab ID (the route does not match it).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from flask import Blueprint, current_app, jsonify, render_template

import paths
import store

bp = Blueprint("sample_links", __name__)

MAX_LAB_ID = 200
MAX_SAMPLE_ID = 2 ** 63 - 1           # SQLite's INTEGER range; larger ids cannot exist


def _db() -> Path:
    return paths.require_data_dir() / store.DB_FILENAME


def find_runs(lab_id: str, *, db) -> list[dict]:
    """The samples whose lab ID is ``lab_id`` exactly, else ASCII
    case-insensitively (SQLite NOCASE: é and É stay different); newest
    injection first (ties: newest id). Both lookups are indexed, never a
    substring, with no row limit."""
    if not lab_id or len(lab_id) > MAX_LAB_ID:
        return []
    return (store.samples.with_lab_id(lab_id, db=db)
            or store.samples.with_lab_id(lab_id, nocase=True, db=db))


def pick(runs: list[dict]) -> Optional[dict]:
    """The run a lab link opens: the newest final run, else the newest run
    (``runs`` newest first, as ``find_runs`` returns them)."""
    finals = [r for r in runs if r["status"] == "final"]
    return (finals or runs or [None])[0]


def resolve(lab_id: str, *, db) -> Optional[dict]:
    """``{lab_id, sample_id, runs}`` for a lab ID, or None."""
    runs = find_runs(lab_id, db=db)
    chosen = pick(runs)
    if chosen is None:
        return None
    names: dict = {}
    for r in runs:
        iid = r["instrument_id"]
        if iid not in names:
            inst = store.instruments.get(iid, db=db) or {}
            names[iid] = inst.get("name") or iid
    return {
        "lab_id": chosen["lab_id"],
        "sample_id": chosen["id"],
        "runs": [{"sample_id": r["id"], "lab_id": r["lab_id"], "instrument": r["instrument_id"],
                  "instrument_name": names[r["instrument_id"]],
                  "injection_dt": r["injection_dt"], "status": r["status"]} for r in runs],
    }


def _classic_page():
    """The classic main page (app.py's ``index`` view), at this URL."""
    return current_app.view_functions["index"]()


def _not_found(*, lab_id: Optional[str] = None, sample_id: Optional[int] = None):
    try:
        from version import APP_VERSION
    except Exception:  # noqa: BLE001
        APP_VERSION = "dev"
    return render_template("sample_link_missing.html", lab_id=lab_id, sample_id=sample_id,
                           app_version=APP_VERSION), 404


def _not_found_json(lab_id: str):
    return jsonify({"error": f"No GC result for lab ID {lab_id} yet", "lab_id": lab_id}), 404


@bp.route("/api/lab/<lab_id>", methods=["GET"])
def api_lab(lab_id: str):
    found = resolve(lab_id, db=_db())
    return jsonify(found) if found else _not_found_json(lab_id)


@bp.route("/lab/<lab_id>", methods=["GET"])
def lab_page(lab_id: str):
    if resolve(lab_id, db=_db()) is None:
        return _not_found(lab_id=lab_id)
    return _classic_page()


def _sample_page(sample_id: int):
    if not 0 < sample_id <= MAX_SAMPLE_ID or store.samples.get(sample_id, db=_db()) is None:
        return _not_found(sample_id=sample_id)
    return _classic_page()


@bp.route("/samples/<int:sample_id>", methods=["GET"])
def sample_page(sample_id: int):
    return _sample_page(sample_id)


@bp.route("/samples/<int:sample_id>/compare", methods=["GET"])
def sample_compare_page(sample_id: int):
    return _sample_page(sample_id)


@bp.route("/samples/<int:sample_id>/data", methods=["GET"])
def sample_data_page(sample_id: int):
    return _sample_page(sample_id)
