"""Results mirror: append the hub's finished rows for this instrument to a
local CSV (the file LEM's station module tails), never rewriting a byte.

Sidecar ``<mirror>.gcagent.json`` = ``{"size", "sha256", "seq", "adopted_at"}``
describes the file as the agent last left it. It is deliberately not the
hub's ``<file>.gchub.json`` (another schema, plus a ``.gchub.lock`` the agent
does not take): the agent **never** mirrors into a file the hub owns, i.e.
one with a ``.gchub.json`` beside it (hub schema or unreadable) or whose
sidecar carries the hub's ``instrument``/``db_id`` keys. A ``.gchub.json`` in
the agent's own schema was written by an older agent and is renamed to
``.gcagent.json``. Sidecar temp files are ``.gcagent-<sidecar>.<random>.part``,
which v1's start-up sweep of ``.<csv>.*.tmp`` can never match.

An append is refused when:

* the file exists but has no sidecar (not adopted);
* the file is missing but has a sidecar (deleted by someone else);
* its size or sha256 differs from the sidecar (changed or shrunk).

During an append the sidecar also carries ``"pending"`` (the size, sha256
and seq the file will have afterwards), so a crash between the append and
the sidecar commit rolls forward instead of looking like tampering.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
from pathlib import Path

from . import util
from .client import NetworkError

log = logging.getLogger("gc_agent.mirror")

# Copied from distill.CSV_HEADER (the agent never imports hub modules).
# tests/agent/test_agent_mirror.py checks the two stay equal.
CSV_HEADER = [
    "Lab ID",
    "InjectionDateTime",
    "2887 IBP", "2887 T5", "2887 T10", "2887 T20", "2887 T30", "2887 T40",
    "2887 T50", "2887 T60", "2887 T70", "2887 T80", "2887 T90", "2887 T95", "2887 FBP",
    "D86 IBP", "D86 T5", "D86 T10", "D86 T20", "D86 T30", "D86 T40",
    "D86 T50", "D86 T60", "D86 T70", "D86 T80", "D86 T90", "D86 T95", "D86 FBP",
    "Best Fit", "Fit Score",
    "Source File",
]

MAX_PAGES = 20


class MirrorError(Exception):
    """The mirror file cannot be appended to safely; mirroring stops."""


class MirrorFetchError(Exception):
    """The hub did not return results (transient)."""


SIDECAR_SUFFIX = ".gcagent.json"
HUB_SIDECAR_SUFFIX = ".gchub.json"
_AGENT_KEYS = {"size", "sha256", "seq", "adopted_at", "pending"}
_HUB_KEYS = ("instrument", "db_id")
_HUB_OWNS = ("%s has the gc-hub export sidecar %s beside it: the hub owns this file, so the "
             "agent will not mirror into it (choose another results_mirror_path, or leave the "
             "mirror off; the hub already appends to the share CSVs LEM reads)")


def sidecar_path(path):
    p = Path(path)
    return p.with_name(p.name + SIDECAR_SUFFIX)


def hub_sidecar_path(path):
    p = Path(path)
    return p.with_name(p.name + HUB_SIDECAR_SUFFIX)


def _read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _agent_schema(d):
    return (isinstance(d, dict) and set(d) <= _AGENT_KEYS
            and {"size", "sha256", "seq"} <= set(d))


def hub_owner_reason(path):
    """Why the hub owns *path* (a message), or None. Writes nothing."""
    p = Path(path)
    hp = hub_sidecar_path(p)
    if hp.exists() and not _agent_schema(_read_json(hp)):
        return _HUB_OWNS % (p, hp.name)
    d = _read_json(sidecar_path(p)) if sidecar_path(p).exists() else None
    if isinstance(d, dict) and any(k in d for k in _HUB_KEYS):
        return _HUB_OWNS % (p, sidecar_path(p).name)
    return None


def claim(path):
    """Raise MirrorError when the hub owns *path*; rename a sidecar an older
    agent wrote as ``.gchub.json`` to ``.gcagent.json``."""
    reason = hub_owner_reason(path)
    if reason:
        raise MirrorError(reason)
    hp, ap = hub_sidecar_path(path), sidecar_path(path)
    if hp.exists():
        if ap.exists():
            raise MirrorError("%s has both %s and an older agent sidecar %s; remove the older "
                              "one, then adopt-mirror" % (path, ap.name, hp.name))
        os.replace(str(hp), str(ap))
        log.info("renamed the older agent sidecar %s to %s", hp.name, ap.name)


def header_line():
    b = io.StringIO()
    csv.writer(b).writerow(CSV_HEADER)
    return b.getvalue()


def _load_sidecar(path):
    sp = sidecar_path(path)
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise MirrorError("cannot read %s: %s" % (sp, exc))


def _save_sidecar(path, sc):
    sp = sidecar_path(path)
    util.atomic_write_bytes(sp, (json.dumps(sc, sort_keys=True) + "\n").encode("utf-8"),
                            tmp_prefix=".gcagent-" + sp.name + ".", tmp_suffix=".part")


def _first_and_last_rows(data):
    text = data.decode("utf-8-sig", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    first = next(csv.reader([lines[0]])) if lines else []
    last = lines[-1] if len(lines) > 1 else ""
    return first, last


def inspect(path):
    """What the installer shows before adoption. Writes nothing."""
    p = Path(path)
    owner = hub_owner_reason(p)
    if not p.is_file():
        return {"exists": False, "hub_owned": owner}
    data = p.read_bytes()
    first, last = _first_and_last_rows(data)
    adopted = sidecar_path(p).exists() or (hub_sidecar_path(p).exists() and owner is None)
    return {"exists": True, "size": len(data), "header_ok": first == CSV_HEADER,
            "last_row": last, "adopted": adopted, "hub_owned": owner}


def adopt(path, seq):
    """Accept the mirror file as it is now: check the header, then write the
    sidecar at the agent's current results seq. A missing file clears any
    stale sidecar, so the agent starts a new file. A file the hub owns is
    refused."""
    p = Path(path)
    claim(p)
    if not p.is_file():
        try:
            os.unlink(str(sidecar_path(p)))
        except FileNotFoundError:
            pass
        return {"exists": False}
    data = p.read_bytes()
    first, last = _first_and_last_rows(data)
    if first != CSV_HEADER:
        raise MirrorError("%s does not start with the %d-column results header" % (p, len(CSV_HEADER)))
    _save_sidecar(p, {"size": len(data), "sha256": util.sha256_bytes(data), "seq": int(seq),
                      "adopted_at": util.local_iso()})
    log.info("adopted mirror %s (%d bytes, seq %d)", p, len(data), seq)
    return {"exists": True, "size": len(data), "last_row": last}


class Mirror:
    def __init__(self, path, ledger, page_size=500):
        self.path = str(path)
        self.path_obj = Path(path)
        self.ledger = ledger
        self.page_size = page_size

    def _verified_state(self):
        """Return (existing bytes, sidecar) after the safety checks, rolling a
        pending append forward when the file matches it."""
        p = self.path_obj
        claim(p)
        sc = _load_sidecar(p)
        exists = p.is_file()
        if not exists:
            if sc is None or sc.get("size") == 0:
                return None, sc
            raise MirrorError("mirror file %s is missing but was written before; "
                              "use adopt-mirror to start a new one" % p)
        if sc is None:
            raise MirrorError("mirror file %s exists but was not adopted; "
                              "adopt it (installer or adopt-mirror)" % p)
        data = p.read_bytes()
        sha = util.sha256_bytes(data)
        if len(data) == sc.get("size") and sha == sc.get("sha256"):
            if "pending" in sc:
                sc = {k: v for k, v in sc.items() if k != "pending"}
                _save_sidecar(p, sc)
            return data, sc
        pend = sc.get("pending") or {}
        if len(data) == pend.get("size") and sha == pend.get("sha256"):
            sc = {"size": pend["size"], "sha256": pend["sha256"], "seq": pend["seq"],
                  "adopted_at": sc.get("adopted_at", util.local_iso())}
            _save_sidecar(p, sc)
            log.info("rolled mirror sidecar forward to seq %d", sc["seq"])
            return data, sc
        if len(data) < int(sc.get("size", 0)):
            raise MirrorError("mirror file %s shrunk since the agent last wrote it; "
                              "mirroring stopped (adopt-mirror to accept it)" % p)
        raise MirrorError("mirror file %s was changed by someone else; "
                          "mirroring stopped (adopt-mirror to accept it)" % p)

    def append(self, header, rows):
        """Append *rows* ({"seq", "line"}) newer than the current seq. Returns
        how many were appended. Raises MirrorError and writes nothing when
        it is not safe."""
        if list(header) != CSV_HEADER:
            raise MirrorError("the hub's results header differs from the agent's %d-column "
                              "header; mirroring stopped" % len(CSV_HEADER))
        for r in rows:
            if not isinstance(r, dict) or not isinstance(r.get("seq"), int) \
                    or not isinstance(r.get("line"), str):
                raise MirrorError("malformed results row from the hub: %r" % (r,))
            if not r["line"].endswith("\n"):
                raise MirrorError("results row %d has no line terminator" % r["seq"])
        data, sc = self._verified_state()
        seq = max(self.ledger.results_seq(), int(sc["seq"]) if sc else 0)
        new = sorted((r for r in rows if r["seq"] > seq), key=lambda r: r["seq"])
        if not new:
            if sc and int(sc["seq"]) > self.ledger.results_seq():
                self.ledger.set_results_seq(int(sc["seq"]))
            return 0
        blob = b"".join(r["line"].encode("utf-8") for r in new)
        if data is None:
            data = b""
            blob = header_line().encode("utf-8") + blob
            sc = {"size": 0, "sha256": util.sha256_bytes(b""), "seq": seq,
                  "adopted_at": util.local_iso()}
        h = hashlib.sha256(data)
        h.update(blob)
        new_seq = new[-1]["seq"]
        after = {"size": len(data) + len(blob), "sha256": h.hexdigest(), "seq": new_seq}
        _save_sidecar(self.path_obj, dict(sc, pending=after))
        self.path_obj.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "ab") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        _save_sidecar(self.path_obj, {"size": after["size"], "sha256": after["sha256"],
                                      "seq": new_seq, "adopted_at": sc.get("adopted_at")})
        self.ledger.set_results_seq(new_seq)
        log.info("mirrored %d row(s) up to seq %d", len(new), new_seq)
        return len(new)

    def sync(self, client):
        """Pull ``/api/agent/results`` pages after the current seq and append
        them. Returns rows appended; raises MirrorError / MirrorFetchError."""
        total = 0
        for _ in range(MAX_PAGES):
            sc = _load_sidecar(self.path_obj) if self.path_obj.exists() else None
            after = max(self.ledger.results_seq(), int(sc["seq"]) if sc else 0)
            try:
                r = client.results(after, self.page_size)
            except NetworkError as exc:
                raise MirrorFetchError("results pull failed: %s" % exc)
            if r.status != 200 or not isinstance(r.json, dict):
                raise MirrorFetchError("results pull: HTTP %d %s" % (r.status, r.error_text()))
            rows = r.json.get("rows") or []
            if not rows:
                break
            total += self.append(r.json.get("header") or [], rows)
            if not r.json.get("more"):
                break
        return total
