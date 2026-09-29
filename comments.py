"""comments.py: sample comments, comment presets and the report log (phase 4).

The rules behind ``comments_api.py`` (the routes) and the two functions the
report builder calls. Stdlib + ``store`` only, no Flask, no import-time side
effects, so it is tested in-process.

Comments
========

A comment belongs to a sample and records the sample's ``current_revision``
when it was added. ``source`` is ``'preset'`` (the preset's text is **copied**:
a later preset edit never rewrites history), ``'free'`` or ``'annotation'``
(a time span ``t0 < t1`` in minutes; blank text defaults to
``Marked region Cx–Cy`` from the revision's calibration anchors, else
``Marked region a–b min``). Deleting is a soft delete (``deleted_at``,
``deleted_by_name``/``deleted_by_initials``, ``deleted_by_ip``) because
comments feed reports.

Authors are the **signed-in account name** (sign-in, spec D6 rev 2): the
route passes the session's name as ``author_name``; ``author_initials`` is
derived from it (``initials_from_name``: the first letter of each word,
accents folded, A–Z only, at most 4, ``X`` if nothing is left). Initials a
client sends are ignored. The request's address is stored beside them
(``author_ip``) but never returned by ``public``/``for_report``: reports print
initials; ``public`` and ``for_report`` also carry the name.

Limits: text ≤ ``MAX_COMMENT_TEXT`` (500) characters, at most
``MAX_ACTIVE_COMMENTS`` (100) non-deleted comments per sample; preset text ≤
``MAX_PRESET_TEXT`` (200), at most ``MAX_PRESETS`` (50) active presets.
Texts lose C0 control characters (tab and newline kept); annotation times
are 0–1000 min; ids are positive SQLite integers (else 400, never 404).

Every refusal is a ``CommentError`` whose ``status`` is the HTTP status the
route answers with (400 invalid, 404 unknown sample/comment/preset, 409 a
limit or an already-deleted comment).

The report seam (frozen with lane P3)
=====================================

::

    for_report(sample_id, db) -> [{id, text, initials, author_name, created_at, t0, t1}]
        # non-deleted, oldest first; t0/t1 None unless an annotation
    log_report(sample_id, *, kind, revision=None, standard_name=None, params=None,
               ranges=None, windows=None, bullets=None, bullets_text=None,
               conclusion=None, conclusion_edited=None, comment_ids=None,
               pdf_sha256=None, author_initials=None, author_ip=None,
               app_version=None, created_at=None, db) -> int
        # one report_log row per PDF; kind 'download'|'zip'|'qbench';
        # params/ranges/windows/bullets/comment_ids JSON-encoded;
        # app_version defaults to version.APP_VERSION
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from typing import Any, Optional, Sequence

import store

MAX_COMMENT_TEXT = 500
MAX_ACTIVE_COMMENTS = 100
MAX_PRESET_TEXT = 200
MAX_PRESETS = 50

_INITIALS_RE = re.compile(r"^[A-Z]{1,4}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f]")     # C0 controls except \t and \n
MAX_MINUTES = 1000.0                                   # annotation span bounds, minutes
MAX_ID = 2 ** 63 - 1                                   # SQLite INTEGER


class CommentError(ValueError):
    """A refused comment/preset operation; ``status`` is the HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── validation ──────────────────────────────────────────────────────────────

def normalize_initials(raw: Any) -> str:
    """Upper-cased initials, 1–4 letters A–Z, or ``CommentError`` (400)."""
    if not isinstance(raw, str):
        raise CommentError("Your initials are required (1–4 letters).")
    value = raw.strip().upper()
    if not _INITIALS_RE.match(value):
        raise CommentError("Initials must be 1–4 letters A–Z.")
    return value


def initials_from_name(name: Any) -> str:
    """Initials from an account name: split on non-letters (accents folded
    first), the first letter of each word, upper-cased, A–Z only, at most 4;
    ``X`` when nothing is left."""
    if not isinstance(name, str):
        return "X"
    folded = "".join(c for c in unicodedata.normalize("NFKD", name)
                     if not unicodedata.combining(c))
    words = [w for w in re.split(r"[^A-Za-z]+", folded) if w]
    return "".join(w[0] for w in words).upper()[:4] or "X"


def _author(name: Any) -> tuple:
    """``(name, initials)`` for the signed-in author, or 400."""
    if not isinstance(name, str) or not name.strip():
        raise CommentError("Sign in to add or delete comments.")
    name = name.strip()[:128]
    return name, initials_from_name(name)


def _text(raw: Any, limit: int, what: str, *, allow_empty: bool = False) -> str:
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise CommentError(f"{what} must be text.")
    value = _CONTROL_RE.sub("", raw).strip()
    if not value and not allow_empty:
        raise CommentError(f"{what} is empty.")
    if len(value) > limit:
        raise CommentError(f"{what} is longer than {limit} characters.")
    return value


def _minutes(raw: Any, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) \
            or not 0 <= raw <= MAX_MINUTES:
        raise CommentError(f"{name} must be a number of minutes from 0 to {MAX_MINUTES:g}.")
    return float(raw)


def _int_id(raw: Any, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= MAX_ID:
        raise CommentError(f"{name} must be a positive integer id.")
    return raw


# ── carbon labels for annotation spans ──────────────────────────────────────

def _revision_anchors(sample_id: int, *, db) -> list:
    """``[(time, carbon)]`` of the sample's current revision's calibration, sorted
    by time; ``[]`` when there is no revision or fewer than 2 anchors."""
    rev = store.get_revision(sample_id, db=db)
    if not rev or not rev.get("calibration_used"):
        return []
    try:
        cal = json.loads(rev["calibration_used"])
        pairs = sorted((float(a[0]), float(a[1])) for a in (cal.get("anchors") or []))
    except (TypeError, ValueError, AttributeError, IndexError):
        return []
    return pairs if len(pairs) >= 2 else []


def _carbon_at(t: float, pairs: Sequence[tuple]) -> float:
    """Linear interpolation, extrapolated from the end pairs (as the PDF does)."""
    if t <= pairs[0][0]:
        (x0, y0), (x1, y1) = pairs[0], pairs[1]
    elif t >= pairs[-1][0]:
        (x0, y0), (x1, y1) = pairs[-2], pairs[-1]
    else:
        i = next(k for k in range(len(pairs) - 1) if pairs[k][0] <= t <= pairs[k + 1][0])
        (x0, y0), (x1, y1) = pairs[i], pairs[i + 1]
    if x1 == x0:
        return y0
    return y0 + (t - x0) * (y1 - y0) / (x1 - x0)


def annotation_default_text(t0: float, t1: float, pairs: Sequence[tuple]) -> str:
    if len(pairs) >= 2:
        c0, c1 = round(_carbon_at(t0, pairs)), round(_carbon_at(t1, pairs))
        span = f"C{c0}" if c0 == c1 else f"C{c0}–C{c1}"
        return f"Marked region {span}"
    return f"Marked region {t0:.2f}–{t1:.2f} min"


# ── comments ────────────────────────────────────────────────────────────────

def public(row: dict) -> dict:
    """A comment as the API returns it (no IP addresses)."""
    return {"id": row["id"], "sample_id": row["sample_id"], "revision": row["revision"],
            "text": row["text"], "preset_id": row["preset_id"], "source": row["source"],
            "t0": row["t0"], "t1": row["t1"], "initials": row["author_initials"],
            "name": row.get("author_name"), "created_at": row["created_at"]}


def list_comments(sample_id: int, *, db=None) -> list[dict]:
    """The sample's non-deleted comments, oldest first."""
    return [public(r) for r in store.sample_comments.list(sample_id, db=db)]


def add_comment(sample_id: int, *, author_name: Any, text: Any = None, preset_id: Any = None,
                t0: Any = None, t1: Any = None, author_ip: Optional[str] = None,
                db=None) -> dict:
    """Validate and add one comment by the signed-in ``author_name``; returns
    it (``public`` form)."""
    author_name, initials = _author(author_name)
    span = (t0, t1) != (None, None)
    if span:
        if t0 is None or t1 is None:
            raise CommentError("An annotation needs both t0 and t1.")
        a, b = sorted((_minutes(t0, "t0"), _minutes(t1, "t1")))
        if a == b:
            raise CommentError("An annotation span must have a width.")
    if preset_id is not None:
        preset_id = _int_id(preset_id, "preset_id")
        if text is not None:
            raise CommentError("Send either text or preset_id, not both.")
        if span:
            raise CommentError("A preset comment has no time span.")
    else:
        text = _text(text, MAX_COMMENT_TEXT, "Comment text", allow_empty=span)

    with store.connection(db) as conn:
        sample = store.samples.get(sample_id, db=conn)
        if sample is None:
            raise CommentError(f"Unknown sample {sample_id}.", 404)
        if preset_id is not None:
            preset = store.comment_presets.get(preset_id, db=conn)
            if preset is None:
                raise CommentError(f"Unknown preset {preset_id}.", 404)
            if not preset["active"]:
                raise CommentError("That preset is no longer offered.")
            text, source = preset["text"], "preset"
        elif span:
            source = "annotation"
            if not text:
                text = annotation_default_text(a, b, _revision_anchors(sample_id, db=conn))
        else:
            source = "free"
        with store.write_txn(conn):
            if store.sample_comments.count_active(sample_id, db=conn) >= MAX_ACTIVE_COMMENTS:
                raise CommentError(f"This sample already has {MAX_ACTIVE_COMMENTS} comments; "
                                   "delete one first.", 409)
            cid = store.sample_comments.add(
                sample_id, text=text, source=source, author_initials=initials,
                author_name=author_name,
                author_ip=author_ip, revision=sample.get("current_revision"),
                preset_id=preset_id, t0=a if span else None, t1=b if span else None, db=conn)
        return public(store.sample_comments.get(cid, db=conn))


def delete_comment(sample_id: int, comment_id: int, *, author_name: Any,
                   author_ip: Optional[str] = None, db=None) -> dict:
    """Soft-delete one of the sample's comments (by the signed-in
    ``author_name``); returns it as it was."""
    author_name, initials = _author(author_name)
    with store.connection(db) as conn:
        row = store.sample_comments.get(comment_id, db=conn)
        if row is None or row["sample_id"] != sample_id:
            raise CommentError(f"Sample {sample_id} has no comment {comment_id}.", 404)
        if not store.sample_comments.soft_delete(comment_id, deleted_by_initials=initials,
                                                 deleted_by_name=author_name,
                                                 deleted_by_ip=author_ip, db=conn):
            raise CommentError("That comment was already deleted.", 409)
        return public(row)


# ── the report seam ─────────────────────────────────────────────────────────

def for_report(sample_id: int, db=None) -> list[dict]:
    """The comments a report prints: non-deleted, oldest first, as
    ``{id, text, initials, author_name, created_at, t0, t1}`` (the report
    prints the initials; never an IP)."""
    return [{"id": r["id"], "text": r["text"], "initials": r["author_initials"],
             "author_name": r.get("author_name"),
             "created_at": r["created_at"], "t0": r["t0"], "t1": r["t1"]}
            for r in store.sample_comments.list(sample_id, db=db)]


def _finite(value: Any) -> Any:
    """NaN/±inf → None, recursively (report parameters may carry them)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def _json(value: Any) -> Optional[str]:
    """Strict JSON (never NaN/Infinity): non-finite floats become null."""
    return None if value is None else json.dumps(_finite(value), allow_nan=False)


def log_report(sample_id: int, *, kind: str, revision: Optional[int] = None,
               standard_name: Optional[str] = None, params: Any = None, ranges: Any = None,
               windows: Any = None, bullets: Any = None, bullets_text: Optional[str] = None,
               conclusion: Optional[str] = None, conclusion_edited: Optional[bool] = None,
               comment_ids: Optional[Sequence[int]] = None, pdf_sha256: Optional[str] = None,
               author_initials: Optional[str] = None, author_ip: Optional[str] = None,
               app_version: Optional[str] = None, created_at: Optional[str] = None,
               user_name: Optional[str] = None, db=None) -> int:
    """Record one produced report PDF in ``report_log``; returns its id.
    ``ValueError`` on an unknown ``kind``. ``user_name`` is the signed-in
    account; without explicit ``author_initials`` they are derived from it.
    Initials, when given, are stored upper-cased."""
    if not author_initials and user_name:
        author_initials = initials_from_name(user_name)
    if app_version is None:
        try:
            import version
            app_version = version.APP_VERSION
        except Exception:  # noqa: BLE001 - the log must not fail over the version
            app_version = "unknown"
    fields = {
        "revision": revision, "standard_name": standard_name, "params_json": _json(params),
        "ranges_json": _json(ranges), "windows_json": _json(windows),
        "bullets_json": _json(bullets), "bullets_text": bullets_text, "conclusion": conclusion,
        "conclusion_edited": None if conclusion_edited is None else int(bool(conclusion_edited)),
        "comment_ids_json": _json(list(comment_ids) if comment_ids is not None else None),
        "app_version": app_version, "pdf_sha256": pdf_sha256,
        "author_initials": author_initials.strip().upper() if author_initials else None,
        "author_ip": author_ip, "user_name": user_name,
    }
    if created_at is not None:
        fields["created_at"] = created_at
    return store.report_log.add(sample_id, kind=kind, db=db, **fields)


# ── presets ─────────────────────────────────────────────────────────────────

def list_presets(include_inactive: bool = False, *, db=None) -> list[dict]:
    return [{"id": p["id"], "text": p["text"], "sort": p["sort"], "active": p["active"]}
            for p in store.comment_presets.list(include_inactive, db=db)]


def _preset_or_404(preset_id: Any, *, db) -> dict:
    pid = _int_id(preset_id, "id")
    p = store.comment_presets.get(pid, db=db)
    if p is None:
        raise CommentError(f"Unknown preset {pid}.", 404)
    return p


def create_preset(text: Any, *, by: Optional[str], db=None) -> dict:
    text = _text(text, MAX_PRESET_TEXT, "Preset text")
    with store.connection(db) as conn:
        with store.write_txn(conn):
            if store.comment_presets.count_active(db=conn) >= MAX_PRESETS:
                raise CommentError(f"There are already {MAX_PRESETS} presets; "
                                   "deactivate one first.", 409)
            pid = store.comment_presets.add(text, created_by=by, db=conn)
        return store.comment_presets.get(pid, db=conn)


def update_preset(preset_id: Any, *, text: Any, db=None) -> dict:
    text = _text(text, MAX_PRESET_TEXT, "Preset text")
    with store.connection(db) as conn:
        p = _preset_or_404(preset_id, db=conn)
        store.comment_presets.update(p["id"], text=text, db=conn)
        return store.comment_presets.get(p["id"], db=conn)


def set_preset_active(preset_id: Any, active: bool, *, db=None) -> dict:
    with store.connection(db) as conn:
        with store.write_txn(conn):
            p = _preset_or_404(preset_id, db=conn)
            if active and not p["active"] and \
                    store.comment_presets.count_active(db=conn) >= MAX_PRESETS:
                raise CommentError(f"There are already {MAX_PRESETS} active presets.", 409)
            store.comment_presets.update(p["id"], active=1 if active else 0, db=conn)
        return store.comment_presets.get(p["id"], db=conn)


def reorder_presets(ids: Any, *, db=None) -> None:
    """``ids`` must name every preset (active or not) exactly once."""
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool)
                                            for i in ids):
        raise CommentError("ids must be a list of preset ids.")
    with store.connection(db) as conn:
        with store.write_txn(conn):
            have = [p["id"] for p in store.comment_presets.list(True, db=conn)]
            if len(ids) != len(set(ids)) or set(ids) != set(have):
                raise CommentError("ids must list every preset exactly once.")
            store.comment_presets.reorder(ids, db=conn)
