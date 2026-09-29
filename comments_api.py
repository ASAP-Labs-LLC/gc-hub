"""comments_api.py: the comment and preset routes (phase 4). A Blueprint
registered by ``app.py``; the rules are ``comments.py``.

Operator routes (a signed-in session, no password, like the other operator
actions; the app's cross-site guard covers every POST; JSON only, 64 KiB cap).
The author is the session's account name (``web_auth.current_name``); the
initials are derived from it, and any ``initials`` in the body is ignored::

    GET  /api/samples/<id>/comments
         → {comments: [{id, sample_id, revision, text, preset_id, source, t0, t1,
                        initials, name, created_at}]}   non-deleted, oldest first; 404
    POST /api/samples/<id>/comments   {text | preset_id, t0?, t1?}
         → 201 {comment}; 400 invalid (text ≤ 500), 404 unknown sample/preset,
           409 already 100 comments, 413 over 64 KiB, 415 not JSON
    POST /api/samples/<id>/comments/<cid>/delete   {}
         → {comment}; soft delete (deleted_at/by name + initials/by IP); 404 not
           this sample's, 409 already deleted
    GET  /api/comment-presets → {presets: [{id, text, sort}]}   active, in order

Admin (``ingest_api._admin_body``: JSON, admin password, 64 KiB)::

    POST /api/admin/comment-presets {password, action, ...}
         action list                      → {presets}      (active and inactive)
         action create     {text}         → 201 {preset, presets}; 409 at 50 active
         action update     {id, text}     → {preset, presets}
         action reorder    {ids}          → {presets}; ids = every preset once
         action deactivate|activate {id}  → {preset, presets}
         400 unknown action / invalid, 404 unknown preset

The request's client address (``netctx.client_ip``: the real client through
the tunnel) is stored as ``author_ip``/``deleted_by_ip``; no route returns it.
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, jsonify, request

import admin_auth
import comments
import netctx
import paths
import store
import web_auth

log = logging.getLogger("comments_api")

bp = Blueprint("comments_api", __name__)


def _db() -> Path:
    return paths.require_data_dir() / store.DB_FILENAME


def _err(message: str, status: int):
    return jsonify({"error": message}), status


def _refused(exc: comments.CommentError):
    return _err(str(exc), exc.status)


def _ip():
    return netctx.client_ip()


def _name():
    return web_auth.current_name()


@bp.route("/api/samples/<int:sample_id>/comments", methods=["GET", "POST"])
def api_sample_comments(sample_id):
    if request.method == "GET":
        if store.samples.get(sample_id, db=_db()) is None:
            return _err(f"Unknown sample {sample_id}.", 404)
        return jsonify({"comments": comments.list_comments(sample_id, db=_db())})
    body, err = admin_auth._json_body()
    if err:
        return err
    try:
        c = comments.add_comment(sample_id, author_name=_name(), text=body.get("text"),
                                 preset_id=body.get("preset_id"), t0=body.get("t0"),
                                 t1=body.get("t1"), author_ip=_ip(), db=_db())
    except comments.CommentError as exc:
        return _refused(exc)
    return jsonify({"comment": c}), 201


@bp.route("/api/samples/<int:sample_id>/comments/<int:comment_id>/delete", methods=["POST"])
def api_delete_sample_comment(sample_id, comment_id):
    body, err = admin_auth._json_body()
    if err:
        return err
    try:
        c = comments.delete_comment(sample_id, comment_id, author_name=_name(),
                                    author_ip=_ip(), db=_db())
    except comments.CommentError as exc:
        return _refused(exc)
    log.info("comment %s of sample %s deleted by %s", comment_id, sample_id,
             web_auth.actor())
    return jsonify({"comment": c})


@bp.route("/api/comment-presets", methods=["GET"])
def api_comment_presets():
    return jsonify({"presets": [{"id": p["id"], "text": p["text"], "sort": p["sort"]}
                                for p in comments.list_presets(db=_db())]})


def _preset_view(p: dict) -> dict:
    return {"id": p["id"], "text": p["text"], "sort": p["sort"], "active": p["active"]}


@bp.route("/api/admin/comment-presets", methods=["POST"])
def api_admin_comment_presets():
    import ingest_api
    body, err = ingest_api._admin_body()
    if err:
        return err
    action = body.get("action")
    db = _db()
    try:
        if action == "list":
            preset, status = None, 200
        elif action == "create":
            preset = comments.create_preset(body.get("text"), by=web_auth.actor(), db=db)
            status = 201
        elif action == "update":
            preset, status = comments.update_preset(body.get("id"), text=body.get("text"),
                                                    db=db), 200
        elif action in ("deactivate", "activate"):
            preset, status = comments.set_preset_active(body.get("id"), action == "activate",
                                                        db=db), 200
        elif action == "reorder":
            comments.reorder_presets(body.get("ids"), db=db)
            preset, status = None, 200
        else:
            return _err("action must be one of list, create, update, reorder, deactivate, "
                        "activate", 400)
    except comments.CommentError as exc:
        return _refused(exc)
    if action != "list":
        log.info("admin: comment preset %s %s by %s", action,
                 preset["id"] if preset else "", web_auth.actor())
    out = {"presets": [_preset_view(p) for p in comments.list_presets(True, db=db)]}
    if preset is not None:
        out["preset"] = _preset_view(preset)
    return jsonify(out), status
