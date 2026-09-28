"""The hub side of the agent HTTP contract (phase 2, 2B1; contracts §1).

Tokens
======

``mint_token(instrument_id)`` returns ``secrets.token_urlsafe(32)`` and
stores only its sha256 (``instruments.token_hash``) and
``token_issued_at``. One token per instrument: minting another revokes the
previous one. ``verify_token(token)`` looks the hash up and compares it
with ``hmac.compare_digest``; it returns the instrument row or ``None``.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.machinery
import importlib.util
import json
import logging
import re
import secrets
import tempfile
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, request

import admin_auth
import distill
import paths
import pipeline
import store

log = logging.getLogger("ingest_api")


def _db(db):
    return db if db is not None else admin_auth.hub_db()


# ── tokens ──────────────────────────────────────────────────────────────────

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenExists(RuntimeError):
    """``mint_token(require_no_token=True)`` on an instrument that has a token."""


def new_token() -> str:
    return secrets.token_urlsafe(32)


def mint_token(instrument_id: str, *, db=None, token: Optional[str] = None,
               require_no_token: bool = False) -> str:
    """Store ``token`` (default: a new one) as ``instrument_id``'s token,
    revoking any previous one. With ``require_no_token``, raise
    ``TokenExists`` instead if the instrument already has one (checked in the
    same transaction as the write)."""
    db = _db(db)
    token = token or new_token()
    with store.connection(db) as conn:
        with store.write_txn(conn):
            row = store.instruments.get(instrument_id, db=conn)
            if row is None:
                raise LookupError(f"unknown instrument {instrument_id!r}")
            if require_no_token and row.get("token_hash"):
                raise TokenExists(row.get("token_issued_at") or "")
            store.instruments.upsert({"id": instrument_id, "token_hash": token_hash(token),
                                      "token_issued_at": store.now_iso()}, db=conn)
    log.warning("agent token minted for %s (any previous token is revoked)", instrument_id)
    return token


def revoke_token(instrument_id: str, *, db=None) -> None:
    """Remove the instrument's token: its agent is refused (401) until a new
    installer is downloaded."""
    with store.connection(_db(db)) as conn:
        with store.write_txn(conn):
            if store.instruments.get(instrument_id, db=conn) is None:
                raise LookupError(f"unknown instrument {instrument_id!r}")
            store.instruments.upsert({"id": instrument_id, "token_hash": None,
                                      "token_issued_at": None}, db=conn)
    log.warning("agent token revoked for %s", instrument_id)


def verify_token(token: Any, *, db=None) -> Optional[dict]:
    """The instrument this token belongs to, or ``None``."""
    if not isinstance(token, str) or not token or len(token) > 512:
        return None
    digest = token_hash(token)
    with store.connection(_db(db)) as conn:
        row = conn.execute("SELECT * FROM instruments WHERE token_hash=?", (digest,)).fetchone()
    if row is None or not hmac.compare_digest(str(row["token_hash"]), digest):
        return None
    return dict(row)


def bearer_token(header: Optional[str]) -> Optional[str]:
    """The token from an ``Authorization: Bearer <token>`` header, or ``None``."""
    if not isinstance(header, str):
        return None
    parts = header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    tok = parts[1].strip()
    return tok or None


# ── the agent package ───────────────────────────────────────────────────────

AGENT_DIR = Path(__file__).resolve().parent / "agent"
_pkg_lock = threading.Lock()
_pkg_cache: dict = {}          # version -> (zip bytes, sha256)


def hub_version() -> str:
    import version
    return version.APP_VERSION


def _builder():
    src = AGENT_DIR / "build_package.py"
    loader = importlib.machinery.SourceFileLoader("gc_hub_agent_build_package", str(src))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def agent_package(version: Optional[str] = None) -> tuple:
    """``(zip bytes, sha256)`` of the agent package for ``version`` (default the
    hub's), built from ``agent/`` with ``agent/build_package.py`` once per
    version and process (the build is deterministic)."""
    version = version or hub_version()
    with _pkg_lock:
        hit = _pkg_cache.get(version)
        if hit is None:
            with tempfile.TemporaryDirectory(prefix="gc-agent-pkg-") as tmp:
                out = Path(tmp) / "agent-package.zip"
                sha = _builder().build(AGENT_DIR, version, out)
                data = out.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha:   # pragma: no cover - build is atomic
                raise RuntimeError("agent package sha mismatch after build")
            hit = _pkg_cache[version] = (data, sha)
            log.info("agent package %s built (%d bytes, sha256 %s)", version, len(data), sha)
        return hit


# ── the agents table ────────────────────────────────────────────────────────

AGENT_COMMANDS = ("restart", "pause", "resume", "retry-rejected", "adopt-mirror")
_AGENT_TIME = "%Y-%m-%dT%H:%M:%S"
SKEW_WARN_SECONDS = 120
_skew_warned: set = set()

# field -> accepted type; every field is optional and None is always allowed
_HEARTBEAT_FIELDS = {
    "version": str, "package_sha256": str, "state": str, "queue_size": int,
    "rejected_count": int, "last_file": str, "last_error": str, "host": str,
    "agent_time": str, "results_seq": int,
}
_MAX_TEXT = 2000
MAX_INT = 2 ** 63          # SQLite INTEGER range; counts and seqs are >= 0


def _heartbeat_values(body: Any) -> dict:
    """The heartbeat's fields, type-checked (``ValueError`` on bad input)."""
    if not isinstance(body, dict):
        raise ValueError("the heartbeat body must be a JSON object")
    out = {}
    for name, typ in _HEARTBEAT_FIELDS.items():
        v = body.get(name)
        if v is None:
            out[name] = None
            continue
        if typ is int and (isinstance(v, bool) or not isinstance(v, int)):
            raise ValueError(f"{name} must be an integer")
        if typ is int and not 0 <= v < MAX_INT:
            raise ValueError(f"{name} must be between 0 and 2**63 - 1")
        if typ is str and not isinstance(v, str):
            raise ValueError(f"{name} must be a string")
        out[name] = v[:_MAX_TEXT] if isinstance(v, str) else v
    if out["agent_time"] is not None:
        try:
            datetime.strptime(out["agent_time"], _AGENT_TIME)
        except ValueError:
            raise ValueError("agent_time must be YYYY-MM-DDTHH:MM:SS") from None
    return out


def record_heartbeat(instrument_id: str, values: dict, *, db=None,
                     take_command: bool = True) -> Optional[str]:
    """Upsert the instrument's ``agents`` row from a heartbeat and take its
    pending command (delivered once: cleared in the same transaction). With
    ``take_command=False`` (a disabled instrument) the command stays queued."""
    cols = ["version", "package_sha256", "state", "queue_size", "rejected_count", "last_file",
            "last_error", "host", "agent_time", "results_seq"]
    with store.connection(_db(db)) as conn:
        with store.write_txn(conn):
            conn.execute(
                f"INSERT INTO agents(instrument_id, {', '.join(cols)}, last_seen) "
                f"VALUES (?, {', '.join('?' for _ in cols)}, ?) "
                f"ON CONFLICT(instrument_id) DO UPDATE SET "
                + ", ".join(f"{c}=excluded.{c}" for c in cols + ["last_seen"]),
                [instrument_id, *(values.get(c) for c in cols), store.now_iso()])
            row = conn.execute("SELECT pending_command FROM agents WHERE instrument_id=?",
                               (instrument_id,)).fetchone()
            command = row["pending_command"] if row and take_command else None
            if command is not None:
                conn.execute("UPDATE agents SET pending_command=NULL WHERE instrument_id=?",
                             (instrument_id,))
    if command is not None and command not in AGENT_COMMANDS:
        log.warning("dropping unknown pending command %r for %s", command, instrument_id)
        return None
    return command


def set_agent_command(instrument_id: str, command: str, *, db=None) -> None:
    """Queue ``command`` for the instrument's agent (the next heartbeat takes it)."""
    if command not in AGENT_COMMANDS:
        raise ValueError(f"unknown agent command {command!r}")
    with store.connection(_db(db)) as conn:
        with store.write_txn(conn):
            if store.instruments.get(instrument_id, db=conn) is None:
                raise LookupError(f"unknown instrument {instrument_id!r}")
            conn.execute("INSERT INTO agents(instrument_id, pending_command) VALUES (?, ?) "
                         "ON CONFLICT(instrument_id) DO UPDATE SET "
                         "pending_command=excluded.pending_command", (instrument_id, command))


def clock_skew_seconds(agent_time: Optional[str], last_seen: Optional[str]) -> Optional[float]:
    """The agent's clock minus the hub's LOCAL clock at that heartbeat (M4):
    ``agent_time`` is naive local, ``last_seen`` UTC, so ``last_seen`` is
    converted to hub local time first."""
    if not agent_time or not last_seen:
        return None
    try:
        agent = datetime.strptime(agent_time, _AGENT_TIME)
        seen = datetime.fromisoformat(last_seen)
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    hub_local = seen.astimezone().replace(tzinfo=None, microsecond=0)
    return (agent - hub_local).total_seconds()


def agents_status(*, db=None) -> list:
    """Every instrument with its agent row (``None`` fields if it never
    reported), ``clock_skew_seconds`` and the token's issue date."""
    with store.connection(_db(db)) as conn:
        rows = conn.execute(
            "SELECT i.id AS instrument_id, i.name AS instrument_name, i.enabled, "
            "i.token_issued_at, a.version, a.package_sha256, a.state, a.queue_size, "
            "a.rejected_count, a.last_file, a.last_error, a.host, a.agent_time, a.last_seen, "
            "a.results_seq, a.pending_command "
            "FROM instruments i LEFT JOIN agents a ON a.instrument_id=i.id ORDER BY i.id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["clock_skew_seconds"] = clock_skew_seconds(d["agent_time"], d["last_seen"])
        out.append(d)
    return out


# ── the agent routes ────────────────────────────────────────────────────────

MAX_BODY = 25 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CDF_MAGIC = (b"CDF\x01", b"CDF\x02", b"CDF\x05", b"\x89HDF\r\n\x1a\n")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_READ_CHUNK = 1024 * 1024

bp = Blueprint("ingest_api", __name__)


def _err(message: str, status: int):
    return jsonify({"error": message}), status


def _authenticate(*, enabled_only: bool = False):
    """``(instrument row, None)`` or ``(None, error response)``. With
    ``enabled_only``, a disabled instrument's token gets 403 (the agent holds)."""
    token = bearer_token(request.headers.get("Authorization"))
    if token is None:
        return None, _err("missing agent token (Authorization: Bearer)", 401)
    try:
        inst = verify_token(token)
    except admin_auth.NoStore:
        return None, _err("the hub has no data folder configured", 503)
    if inst is None:
        log.warning("refused agent request %s %s from %s: bad token", request.method,
                    request.path, request.remote_addr)
        return None, _err("invalid agent token", 401)
    if enabled_only and not inst.get("enabled", 1):
        return None, _err(f"instrument {inst['id']} is disabled", 403)
    return inst, None


def _filename(raw: Optional[str]) -> Optional[str]:
    """``X-GC-Filename`` percent-decoded, reduced to a base name without
    control characters (the lab-ID fallback and ``source_name``)."""
    if not raw:
        return None
    name = urllib.parse.unquote(raw, errors="replace")
    name = re.split(r"[\\/]", name)[-1]
    name = _CONTROL.sub("", name).strip()[:255]
    return name or None


def _mtime(raw: Optional[str]):
    if raw is None or not raw.strip():
        return None
    return datetime.strptime(raw.strip(), _AGENT_TIME)     # ValueError -> 400


_PATH_RE = re.compile(r"""(?:[A-Za-z]:)?[\\/](?:[^\\/'"\s]+[\\/])*([^\\/'"\s]*)""")


def public_message(message: str) -> str:
    """``message`` with any server path reduced to its base name (M4)."""
    return _PATH_RE.sub(lambda m: m.group(1) or "", message)


def _read_body(limit: int) -> Optional[bytes]:
    """The request body, or ``None`` when it is larger than ``limit``."""
    chunks, size = [], 0
    stream = request.stream
    while True:
        chunk = stream.read(min(_READ_CHUNK, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            return None
    return b"".join(chunks)


def _notifier():
    try:
        import notifications
        return notifications.get_store().add
    except Exception:  # noqa: BLE001 - a notification must never break ingest
        log.exception("ingest: notifications unavailable")
        return None


_notified_conflicts: set = set()


def _instrument_name(instrument_id: Optional[str]) -> str:
    if not instrument_id:
        return "another instrument"
    try:
        row = store.instruments.get(instrument_id, db=_db(None))
    except Exception:  # noqa: BLE001
        row = None
    return (row or {}).get("name") or instrument_id


@bp.route("/api/ingest", methods=["POST"])
def api_ingest():
    inst, err = _authenticate()
    if err:
        return err
    try:
        request.max_content_length = MAX_BODY       # Flask >= 3.1; the checks below also hold
    except Exception:  # noqa: BLE001
        pass
    length = request.content_length
    if length is not None and length > MAX_BODY:
        return _err(f"the file is larger than {MAX_BODY // (1024 * 1024)} MB", 413)
    sha = (request.headers.get("X-GC-SHA256") or "").strip().lower()
    if not _SHA_RE.match(sha):
        return _err("X-GC-SHA256 must be the body's sha256 as 64 hex digits", 400)
    try:
        mtime = _mtime(request.headers.get("X-GC-Mtime"))
    except ValueError:
        return _err("X-GC-Mtime must be local time as YYYY-MM-DDTHH:MM:SS", 400)
    filename = _filename(request.headers.get("X-GC-Filename"))
    body = _read_body(MAX_BODY)
    if body is None:
        return _err(f"the file is larger than {MAX_BODY // (1024 * 1024)} MB", 413)
    actual = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(actual, sha):
        return _err(f"sha256 mismatch: the body is {actual}, the header says {sha}", 400)
    if not body.startswith(_CDF_MAGIC):
        return _err("not a CDF (NetCDF) file", 415)
    try:
        res = pipeline.submit(inst["id"], body, mtime=mtime, source_name=filename,
                              data_dir=paths.data_dir(), db=_db(None), notifier=_notifier())
    except pipeline.UnknownInstrument as exc:
        return _err(str(exc), 401)
    except pipeline.InstrumentDisabled as exc:     # before SubmitRejected (its base class)
        return _err(str(exc), 403)
    except pipeline.SubmitRejected as exc:
        log.warning("ingest: %s rejected %s: %s", inst["id"], filename, exc)
        return _err(public_message(str(exc)), 400)
    if res.outcome == "created":
        return jsonify({"sample_id": res.sample_id, "sha256": res.sha256,
                        "status": res.status}), 201
    if res.outcome == "duplicate":
        return jsonify({"sample_id": res.sample_id, "sha256": res.sha256, "duplicate": True}), 200
    if res.outcome == "conflict":
        _notify_conflict(inst, res, filename)
        return jsonify({"sha256": actual, "conflict_id": res.conflict_id}), 202
    if res.outcome == "cross_instrument":
        return _err(f"already received from {_instrument_name(res.instrument_id)}", 409)
    log.error("ingest: unexpected submit outcome %r", res.outcome)   # pragma: no cover
    return _err("unexpected ingest outcome", 500)


def _notify_conflict(inst: dict, res, filename: Optional[str]) -> None:
    """One notification per conflict id per process (a re-sent file doesn't repeat it)."""
    if res.conflict_id in _notified_conflicts:
        return
    _notified_conflicts.add(res.conflict_id)
    notify = _notifier()
    if notify is None:
        return
    try:
        notify("warning", f"{inst.get('name') or inst['id']}: {filename or 'a file'} has the same "
                          f"lab ID and injection time as sample {res.sample_id} but different "
                          f"content. It is held for review (conflict {res.conflict_id}).")
    except Exception:  # noqa: BLE001
        log.exception("ingest: conflict notification failed")


@bp.route("/api/agent/heartbeat", methods=["POST"])
def api_agent_heartbeat():
    inst, err = _authenticate()
    if err:
        return err
    admin_auth.limit_json_body()      # 64 KiB, chunked bodies included (a JSON 413)
    raw = request.get_data(cache=False)
    if len(raw) >= admin_auth.MAX_JSON_BODY:    # a chunked body stops at the cap
        return _err("the heartbeat is too large", 413)
    try:
        values = _heartbeat_values(json.loads(raw or b"null"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        return _err(f"bad heartbeat: {str(exc)[:200]}", 400)
    command = record_heartbeat(inst["id"], values, take_command=bool(inst.get("enabled", 1)))
    now = datetime.now()
    if values["agent_time"]:
        skew = (datetime.strptime(values["agent_time"], _AGENT_TIME)
                - now.replace(microsecond=0)).total_seconds()
        if abs(skew) > SKEW_WARN_SECONDS:
            if inst["id"] not in _skew_warned:
                _skew_warned.add(inst["id"])
                log.warning("agent %s clock is %+.0f s off the hub's local time", inst["id"], skew)
        else:
            _skew_warned.discard(inst["id"])
    try:
        pkg_sha = agent_package()[1]
    except Exception:  # noqa: BLE001 - a broken package must not stop heartbeats
        log.exception("agent package build failed")
        pkg_sha = ""
    if command:
        log.info("delivered command %r to agent %s", command, inst["id"])
    return jsonify({"command": command, "agent_package_sha256": pkg_sha,
                    "server_time": now.strftime(_AGENT_TIME)})


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    if len(raw) > 20:
        raise ValueError(name)
    value = int(raw)          # ValueError -> 400
    if not -MAX_INT <= value < MAX_INT:
        raise ValueError(name)
    return value


@bp.route("/api/agent/results", methods=["GET"])
def api_agent_results():
    inst, err = _authenticate(enabled_only=True)
    if err:
        return err
    try:
        after = _int_arg("after", 0)
        limit = _int_arg("limit", 500)
    except ValueError:
        return _err("after and limit must be integers", 400)
    if after < 0:
        return _err("after must be >= 0", 400)
    if limit < 1:
        return _err("limit must be >= 1", 400)
    limit = min(limit, 500)
    rows = store.export_rows.rows_after(inst["id"], after, limit + 1, db=_db(None))
    return jsonify({"header": list(distill.CSV_HEADER),
                    "rows": [{"seq": r["seq"], "line": r["line"]} for r in rows[:limit]],
                    "more": len(rows) > limit})


def _package_or_error():
    try:
        return agent_package(), None
    except Exception:  # noqa: BLE001
        log.exception("agent package build failed")
        return None, _err("the hub could not build the agent package", 503)


@bp.route("/api/agent/package", methods=["GET"])
def api_agent_package():
    _inst, err = _authenticate(enabled_only=True)
    if err:
        return err
    pkg, err = _package_or_error()
    if err:
        return err
    return jsonify({"version": hub_version(), "sha256": pkg[1]})


@bp.route("/api/agent/package.zip", methods=["GET"])
def api_agent_package_zip():
    _inst, err = _authenticate(enabled_only=True)
    if err:
        return err
    pkg, err = _package_or_error()
    if err:
        return err
    return Response(pkg[0], mimetype="application/zip", headers={
        "Content-Disposition": f'attachment; filename="gc-agent-{hub_version()}.zip"',
        "X-GC-SHA256": pkg[1]})


@bp.route("/api/agents", methods=["GET"])
def api_agents():
    """Every instrument's agent status (read-only; for the Instruments page)."""
    try:
        return jsonify({"agents": agents_status()})
    except admin_auth.NoStore:
        return jsonify({"agents": []})


# ── admin: installer download, hub URL, agent commands ──────────────────────

HUB_URL_KEY = admin_auth.HUB_URL_KEY


def _admin_body():
    """``(body, None)`` when the request is JSON and carries the admin
    password; else ``(None, error response)``. (The cross-site guard in
    app.py already covers these /api/ routes.) The body is capped at 64 KiB."""
    admin_auth.limit_json_body()
    if not request.is_json:
        return None, _err("Expected Content-Type: application/json", 415)
    body = admin_auth.get_json_object()
    if not isinstance(body, dict):
        return None, _err("Expected a JSON object", 400)
    try:
        ok = admin_auth.check_admin_body(body)
    except Exception:  # noqa: BLE001 - the gate never opens on an error
        log.exception("admin check failed")
        ok = False
    if not ok:
        return None, _err("Incorrect password", 403)
    return body, None


def configured_hub_url(*, db=None) -> Optional[str]:
    return store.settings_kv.get(HUB_URL_KEY, db=_db(db)) or None


def _valid_hub_url(url: str) -> bool:
    """``http(s)://host[:port]`` only: no user info, path, query or fragment."""
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port      # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and bool(parts.hostname)
            and "@" not in parts.netloc and parts.path in ("", "/")
            and not parts.query and not parts.fragment)


def derived_hub_url() -> Optional[str]:
    """The request's own address as the hub URL, or ``None`` when it can't be
    trusted for an agent on another PC: a loopback address (the admin is on
    the server itself) or a name that isn't this machine's (a rebinding or a
    proxy). Then the admin must set the hub URL first (I5)."""
    name = admin_auth._hostname(request.host)
    if name is None:
        return None
    if admin_auth.is_ip_literal(name):
        import ipaddress
        if ipaddress.ip_address(name).is_loopback:
            return None
    elif name == "localhost" or not admin_auth.is_machine_name(name):
        return None
    return request.host_url.rstrip("/")


def installer_zip(hub_url: str, token: str, package: tuple, version: str) -> bytes:
    """The "Download installer" zip (contract §1): ``install.pyw``,
    ``launcher.pyw``, ``install.json`` ``{hub_url, token}``,
    ``agent-package.zip`` and ``agent-package.json`` ``{version, sha256}``,
    at the zip root. The only place a token is ever written."""
    import io
    import zipfile
    data, sha = package
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("install.pyw", "launcher.pyw"):
            z.writestr(name, (AGENT_DIR / name).read_bytes())
        z.writestr("install.json", json.dumps({"hub_url": hub_url, "token": token}, indent=2))
        z.writestr("agent-package.zip", data)
        z.writestr("agent-package.json", json.dumps({"version": version, "sha256": sha}, indent=2))
    return buf.getvalue()


@bp.route("/api/admin/instruments/<instrument_id>/installer", methods=["POST"])
def api_admin_installer(instrument_id):
    """Mint a new token for the instrument and return the installer zip. If it
    already has a token, the request must say ``confirm_revoke: true`` (the old
    token stops working); otherwise 409 and nothing changes."""
    body, err = _admin_body()
    if err:
        return err
    inst = store.instruments.get(instrument_id, db=_db(None))
    if inst is None:
        return _err(f"unknown instrument {instrument_id!r}", 404)
    hub_url = configured_hub_url() or derived_hub_url()
    if hub_url is None:
        return jsonify({"error": "Set the hub URL first (the address the GC PCs use to reach "
                                 "this hub, e.g. http://asapsv1:5560): this page was opened as "
                                 f"{request.host}, which a GC PC can't use.",
                        "needs_hub_url": True}), 409
    confirmed = body.get("confirm_revoke") is True
    needs_confirm = jsonify({
        "error": f"{inst.get('name') or instrument_id} already has an agent token (issued "
                 f"{inst.get('token_issued_at')}). A new installer revokes it: the agent using "
                 f"it stops until it gets the new one.",
        "needs_confirm": True, "hub_url": hub_url}), 409
    if inst.get("token_hash") and not confirmed:
        return needs_confirm
    pkg, err = _package_or_error()          # before minting: a failed build revokes nothing
    if err:
        return err
    token = new_token()                     # the zip is built before the hash is stored
    data = installer_zip(hub_url, token, pkg, hub_version())
    try:
        mint_token(instrument_id, token=token, require_no_token=not confirmed)
    except TokenExists:                     # minted by someone else meanwhile
        return needs_confirm
    except LookupError:
        return _err(f"unknown instrument {instrument_id!r}", 404)
    log.warning("agent installer downloaded for %s by %s (hub_url %s)", instrument_id,
                request.remote_addr, hub_url)
    return Response(data, mimetype="application/zip", headers={
        "Content-Disposition": f'attachment; filename="gc-agent-installer-{instrument_id}.zip"',
        "Cache-Control": "no-store", "X-GC-Hub-URL": hub_url})


@bp.route("/api/admin/instruments/<instrument_id>/revoke-token", methods=["POST"])
def api_admin_revoke_token(instrument_id):
    """Revoke the instrument's agent token (its agent gets 401 and holds)."""
    _body, err = _admin_body()
    if err:
        return err
    try:
        revoke_token(instrument_id)
    except LookupError as exc:
        return _err(str(exc), 404)
    return jsonify({"ok": True})


@bp.route("/api/admin/hub-url", methods=["POST"])
def api_admin_hub_url():
    """Set (or, with an empty value, clear) the hub URL written into installers."""
    body, err = _admin_body()
    if err:
        return err
    url = body.get("hub_url")
    if url is None or (isinstance(url, str) and not url.strip()):
        store.settings_kv.delete(HUB_URL_KEY, db=_db(None))
        return jsonify({"hub_url": None})
    if not isinstance(url, str) or not _valid_hub_url(url.strip()):
        return _err("hub_url must be an http:// or https:// URL, e.g. http://asapsv1:5560", 400)
    url = url.strip().rstrip("/")
    store.settings_kv.set(HUB_URL_KEY, url, db=_db(None))
    return jsonify({"hub_url": url})


@bp.route("/api/admin/instruments/<instrument_id>/agent-command", methods=["POST"])
def api_admin_agent_command(instrument_id):
    body, err = _admin_body()
    if err:
        return err
    command = body.get("command")
    if command not in AGENT_COMMANDS:
        return _err(f"command must be one of {', '.join(AGENT_COMMANDS)}", 400)
    try:
        set_agent_command(instrument_id, command)
    except LookupError as exc:
        return _err(str(exc), 404)
    log.info("agent command %r queued for %s", command, instrument_id)
    return jsonify({"ok": True, "command": command})
