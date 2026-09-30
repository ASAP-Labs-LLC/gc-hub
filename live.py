"""live.py: the in-memory event bus behind live updates (v3.1).

Spec: ``docs/superpowers/specs/2026-09-30-ui-redesign-live-setup-design.md``,
"Live updates (v3.1)". Stdlib only, no import-time side effects beyond the
process's one ``BUS``; importable by the pipeline, the exporter and tools.

* A ring of ``RING_SIZE`` events, a ``boot_id`` (random per process) and a
  monotonically increasing ``seq``. The cursor a client holds is
  ``"<boot_id>:<seq>"``.
* ``publish(kind, payload)`` is thread-safe and **never raises into its
  caller** (a live update is never worth failing a sample, a heartbeat or an
  export over). Kinds: ``sample`` (``{sample_id}`` or ``{sample_ids}``),
  ``agent`` (``{instrument_id}``), ``instrument`` (``{instrument_id}``),
  ``notification``, ``hub``.
* ``Bus.since(cursor)``: the sample and instrument ids changed after the
  cursor, or ``reset`` when the cursor is missing or garbled, from another
  boot, from the future, or older than the ring (events were dropped).
* ``poll(cursor, agents=, unread=, hub=)`` builds ``GET /api/live``'s answer
  from memory only: the agent snapshot and hub state come from
  ``hub_control``'s refresher cache and the unread count from the in-memory
  notification store, so the request path never opens SQLite.
"""
from __future__ import annotations

import collections
import logging
import secrets
import threading
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("live")

RING_SIZE = 1000
KINDS = ("sample", "agent", "instrument", "notification", "hub")
CURSOR_MAX = 128
LOG_EVERY = 100          # log at most one publish failure in this many


def _ints(values: Iterable[Any]) -> list:
    out = []
    for v in values:
        if isinstance(v, bool):
            continue
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _normalise(kind: Any, payload: Any) -> Optional[tuple]:
    """``(kind, sample ids, instrument ids)`` or None for an unknown kind."""
    if kind not in KINDS:
        return None
    p = payload if isinstance(payload, dict) else {}
    samples: list = []
    insts: list = []
    if kind == "sample":
        if "sample_id" in p:
            samples += _ints([p.get("sample_id")])
        if isinstance(p.get("sample_ids"), (list, tuple, set)):
            samples += _ints(p.get("sample_ids"))
    if kind in ("instrument", "agent"):
        iid = p.get("instrument_id")
        if isinstance(iid, str) and iid:
            insts.append(iid[:64])
    return kind, tuple(samples), tuple(insts)


class Bus:
    """One process's ring of events. See the module docstring."""

    def __init__(self, size: int = RING_SIZE, boot_id: Optional[str] = None) -> None:
        self.boot_id = boot_id or secrets.token_hex(8)
        self.size = size
        self._ring: collections.deque = collections.deque(maxlen=size)   # (seq, kind, s, i)
        self._seq = 0
        self._lock = threading.Lock()
        self._failures = 0

    def publish(self, kind: str, payload: Any = None) -> None:
        """Record one event. Never raises."""
        try:
            ev = _normalise(kind, payload)
            if ev is None:
                return
            with self._lock:
                self._seq += 1
                self._ring.append((self._seq,) + ev)
        except Exception:  # noqa: BLE001 - never into the caller
            try:
                self._failures += 1
                if self._failures % LOG_EVERY == 1:
                    log.exception("live: publish(%r) failed", kind)
            except Exception:  # noqa: BLE001
                pass

    def cursor(self) -> str:
        with self._lock:
            return f"{self.boot_id}:{self._seq}"

    def _parse(self, cursor: Any) -> Optional[int]:
        """The cursor's seq when it belongs to this boot, else None."""
        if not isinstance(cursor, str) or not cursor or len(cursor) > CURSOR_MAX:
            return None
        boot, sep, raw = cursor.partition(":")
        if not sep or boot != self.boot_id or not raw.isdigit():
            return None
        return int(raw)

    def since(self, cursor: Any) -> dict:
        """``{cursor, reset, samples, instruments, kinds}`` after ``cursor``."""
        seq = self._parse(cursor)
        with self._lock:
            now = self._seq
            events = list(self._ring)
        out = {"cursor": f"{self.boot_id}:{now}", "reset": False, "samples": [],
               "instruments": [], "kinds": []}
        if seq is None or seq > now:
            out["reset"] = True
            return out
        if seq == now:
            return out
        oldest = events[0][0] if events else now + 1
        if seq < oldest - 1:          # the events right after the cursor were dropped
            out["reset"] = True
            return out
        samples, insts, kinds = set(), set(), set()
        for ev_seq, kind, s, i in events:
            if ev_seq <= seq:
                continue
            kinds.add(kind)
            samples.update(s)
            insts.update(i if kind == "instrument" else ())
        out["samples"] = sorted(samples)
        out["instruments"] = sorted(insts)
        out["kinds"] = sorted(kinds)
        return out


BUS = Bus()


def publish(kind: str, payload: Any = None) -> None:
    """Publish on the process's bus. Never raises."""
    try:
        BUS.publish(kind, payload)
    except Exception:  # noqa: BLE001
        pass


def publish_samples(sample_ids: Iterable[Any]) -> None:
    """One ``sample`` event naming several samples. Never raises."""
    try:
        ids = _ints(sample_ids or ())
        if ids:
            BUS.publish("sample", {"sample_ids": ids})
    except Exception:  # noqa: BLE001
        pass


def notifications_unread() -> int:
    """The notification tray's count, from the in-memory store (no SQLite)."""
    import notifications
    return int(notifications.get_store().count())


def _safe(fn: Optional[Callable[[], Any]], default: Any) -> Any:
    if fn is None:
        return default
    try:
        v = fn()
        return default if v is None else v
    except Exception:  # noqa: BLE001 - a broken snapshot never fails the poll
        return default


def poll(cursor: Any, *, bus: Optional[Bus] = None,
         agents: Optional[Callable[[], list]] = None,
         unread: Optional[Callable[[], int]] = None,
         hub: Optional[Callable[[], dict]] = None) -> dict:
    """``GET /api/live``'s answer: ``{cursor, reset, samples, instruments,
    kinds, agents, notifications_unread, hub: {state, staged_update},
    version}`` (``version``: this process's, so an open tab can offer a
    reload after an update). From memory only; never raises."""
    b = bus or BUS
    s = b.since(cursor)
    h = _safe(hub, None)
    return {
        "cursor": s["cursor"],
        "reset": s["reset"],
        "samples": s["samples"],
        "instruments": s["instruments"],
        "kinds": s["kinds"],
        "agents": list(_safe(agents, [])),
        "notifications_unread": int(_safe(unread, 0)),
        "hub": {"state": (h or {}).get("state"), "staged_update": (h or {}).get("staged_update")},
        "version": _version(),
    }


def _version() -> str:
    try:
        import version
        return str(version.APP_VERSION)
    except Exception:  # noqa: BLE001
        return "dev"
