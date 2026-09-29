"""How this process may restart itself, and the app's side of the updater's
restart→switch handshake (``restart_update``, copied verbatim from
coa-reviewer).

The hub always runs under the updater (v2: GC_DATA_DIR is required), which
supervises the port and relaunches within ~20 s, so the app must only EXIT —
spawning its own replacement would race the updater for the port. The one
exception is a *paused* updater, which supervises nothing: then the app
spawns its replacement, but never while a switch is under way.

The watcher (``await_switch``) is ported from coa-reviewer's app.py
(``_await_switch`` and friends). Whatever the updater does, the person who
clicked Restart gets a restart, and once the updater has (or may have)
taken the request this process never spawns a replacement.

Stdlib + restart_update only, no import-time side effects, so it is
unit-testable without importing app.

``restart_update`` logs as ``"coa.restart"`` here too. That is by design: the
file must stay byte-identical to coa-reviewer's (the updater imports whichever
app's copy it finds first), so it cannot be renamed for gc.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Tuple

import restart_update

logger = logging.getLogger("gc.restart")

BLOCKING = ("switching", restart_update.ACCEPTED_FILE)

POLL_SECONDS = 1.0
# How long to wait to be killed once the updater has accepted: its switch()
# stops the app, flips the junction and starts the new release well within
# this; if not, we exit and its supervise() takes over.
ACCEPTED_WAIT_SECONDS = 45.0
# Marker gone with no outcome file for this many polls in a row: the updater
# took it but its claim fell back to deleting the marker.
UNKNOWN_CLAIM_POLLS = 3
WITHDRAW_ATTEMPTS = 3
MARKER_READ_LIMIT_BYTES = 4096

# Daily auto-restart (ported from coa-reviewer's _should_auto_restart).
AUTO_RESTART_HOUR = 3                   # 3 AM local time
# The storm guard: the once-a-day flag lives in memory and a fresh process
# starts idle, so without this a just-restarted process restarts again as soon
# as it has been idle long enough — several times through the 3 AM hour, each
# exit spending one of the updater's supervision starts.
AUTO_RESTART_MIN_UPTIME_SECONDS = 3600
# Under the updater, a manual restart this soon after starting is refused:
# the updater allows only a few starts per 15 minutes before giving up.
MANUAL_RESTART_MIN_UPTIME_SECONDS = 300


def restart_mode(env: Optional[Mapping[str, str]] = None) -> str:
    """Always ``"exit"``: the hub runs under the updater (v2 has no
    self-respawning legacy mode). ``env`` is accepted for compatibility."""
    return "exit"


def switch_request_fresh(data_dir: Path, now: Optional[float] = None) -> bool:
    """Whether a ``switch-requested`` marker exists that the updater could
    still act on. It refuses any request older than its
    MAX_REQUEST_AGE_SECONDS (45 s), so one older than PICKUP_SECONDS — or one
    whose ``at`` cannot be read — can never trigger a switch."""
    path = Path(data_dir) / restart_update.MARKER_FILE
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MARKER_READ_LIMIT_BYTES)
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("could not read %s (treating it as stale): %s", path, exc)
        return False
    try:
        at = float(json.loads(raw.decode("utf-8")).get("at"))
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
        return False
    age = (time.time() if now is None else now) - at
    return -5.0 <= age <= restart_update.PICKUP_SECONDS   # updater's own skew allowance


def may_respawn(data_dir: Path) -> bool:
    """False while the updater is switching, has accepted a switch, or could
    still take a pending request: it will start the new release itself, and
    a child of ours would fight it for the port."""
    for name in BLOCKING:
        if (Path(data_dir) / name).exists():
            logger.warning("Not respawning: %s exists", Path(data_dir) / name)
            return False
    if switch_request_fresh(data_dir):
        logger.warning("Not respawning: a switch request the updater could still take exists")
        return False
    return True


# An explicit Stop (hub_control's POST /api/admin/hub/stop, the hub tray's
# "Stop hub") writes the updater's ``paused`` marker so nothing relaunches the
# hub, then exits. ``paused`` normally means "respawn yourself" (below), so the
# stop is recorded here first and wins over it: a stop never self-respawns.
_stop_requested = threading.Event()


def request_stop() -> None:
    """Record that this process is stopping for good (no respawn, ever)."""
    _stop_requested.set()


def stop_requested() -> bool:
    return _stop_requested.is_set()


def _reset_stop_for_tests() -> None:
    _stop_requested.clear()


def should_respawn(data_dir: Path, env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether a restart should spawn its own replacement before exiting:
    no — the updater relaunches us — except while it is ``paused`` (and not
    mid-switch, ``may_respawn``), when it supervises nothing and exiting
    would leave the app down (coa-reviewer's _exit_for_updater). Never after
    an explicit stop (``request_stop``), which is what wrote ``paused``."""
    if stop_requested():
        logger.warning("Not respawning: the hub was stopped on purpose")
        return False
    d = Path(data_dir)
    if not (d / "paused").exists() or (d / "switching").exists():
        return False
    logger.warning("The updater is paused and will not restart the app — "
                   "restarting it ourselves")
    return may_respawn(d)


# Windows process-creation flags (numeric so this imports anywhere).
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def respawn_command(executable: str, argv, *, cwd: str,
                    windows: bool) -> Tuple[list, str, int]:
    """``(args, cwd, creationflags)`` for a self-spawned replacement (only
    ever while the updater is paused): run ``app.py`` relative to the current
    directory — the updater's ``current`` junction — so the replacement runs
    whatever the junction points at, with no console window, as coa-reviewer
    does. ``argv[0]`` is dropped (the replacement always runs ``app.py``)."""
    args = [executable, "app.py", *list(argv[1:])]
    flags = (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if windows else 0
    return args, cwd, flags


def should_auto_restart(*, hour: int, today: str, done_today: Optional[str],
                        uptime_seconds: float, idle: bool) -> bool:
    """Whether the daily auto-restart fires now."""
    if hour != AUTO_RESTART_HOUR or done_today == today:
        return False
    if uptime_seconds < AUTO_RESTART_MIN_UPTIME_SECONDS:
        return False
    return bool(idle)


def manual_restart_wait(mode: str, uptime_seconds: float) -> Optional[int]:
    """Seconds until a manual restart is allowed, or None if it is now."""
    if mode != "exit" or uptime_seconds >= MANUAL_RESTART_MIN_UPTIME_SECONDS:
        return None
    return max(1, int(-(-(MANUAL_RESTART_MIN_UPTIME_SECONDS - uptime_seconds) // 1)))


def decide(data_dir: Optional[Path], current_version: str) -> Tuple[str, Optional[str]]:
    """``("switch", tag)`` when a newer healthy release is staged, else
    ``("restart", None)``. No data dir (never in v2) never switches."""
    if data_dir is None:
        return "restart", None
    tag = restart_update.staged_update(data_dir, current_version)
    return ("switch", tag) if tag else ("restart", None)


# ── The watcher ────────────────────────────────────────────────────────────
# Returns what the caller must do next:
#   "restart" — nobody has the request (refused, or withdrawn unclaimed):
#               restart normally;
#   "exit"    — the updater has (or may have) it: exit WITHOUT respawning.
# Switch files are cleared before either answer. Every wait is a fixed number
# of polls and every sleep goes through ``sleep``, so tests drive the
# updater's side from inside it.

def _polls(seconds: float, poll: float) -> int:
    return max(1, int(round(seconds / max(poll, 1e-3))))


def _matching_outcome(data_dir: Path, at: float) -> Optional[dict]:
    """The updater's answer to *this* request, or None. An outcome whose
    ``at`` is not ours is a leftover from an earlier request."""
    outcome = restart_update.read_switch_outcome(data_dir)
    if outcome is None:
        return None
    try:
        theirs = float(outcome.get("at"))
    except (TypeError, ValueError):
        theirs = None
    if theirs is None or abs(theirs - at) > 1e-3:
        logger.debug("Ignoring a switch outcome for another request (at=%r)", outcome.get("at"))
        return None
    return outcome


def _poll_switch(data_dir: Path, at: float, polls: int, poll: float,
                 sleep: Callable[[float], None]) -> Tuple[str, Optional[dict]]:
    """``("refused"|"accepted", outcome)``, ``("unknown", None)`` when the
    marker vanished without an outcome, or ``("unclaimed", None)``."""
    missing = 0
    for _ in range(max(1, polls)):
        sleep(poll)
        outcome = _matching_outcome(data_dir, at)
        if outcome is not None:
            return str(outcome.get("state")), outcome
        missing = missing + 1 if not restart_update.marker_present(data_dir) else 0
        if missing >= UNKNOWN_CLAIM_POLLS:
            return "unknown", None
    return "unclaimed", None


def _withdraw_after_pickup(data_dir: Path, tag: str, at: float, pickup: float,
                           answer_polls: int, poll: float,
                           sleep: Callable[[float], None]):
    """Nobody took the request in time: take it back. Returns ``"restart"``
    once withdrawn, the updater's answer ``(verdict, outcome)`` if it claimed
    the marker as we withdrew it, or ``"exit"`` if the marker is stuck and
    still fresh enough for the updater to act on."""
    for _ in range(WITHDRAW_ATTEMPTS):
        if restart_update.withdraw_switch_request(data_dir):
            logger.warning("Updater did not take the switch to %s within %.0fs — restarting "
                           "normally (is updater.py up to date on this host?)", tag, pickup)
            restart_update.clear_switch_files(data_dir)
            return "restart"
        if not restart_update.marker_present(data_dir):
            logger.info("Updater claimed the switch to %s as we withdrew it", tag)
            return _poll_switch(data_dir, at, answer_polls, poll, sleep)
        sleep(poll)
    # withdraw() also returns False on an OSError. The marker is still here,
    # so nobody claimed it: this is not "the updater has it".
    fresh = switch_request_fresh(data_dir)
    restart_update.clear_switch_files(data_dir)
    if fresh:
        logger.error("Could not remove the unclaimed switch request for %s and the updater "
                     "could still take it — exiting without a respawn", tag)
        return "exit"
    logger.error("Could not withdraw the unclaimed switch request for %s — restarting "
                 "normally", tag)
    return "restart"


def _after_watcher_error(data_dir: Path) -> str:
    """The watcher broke. If the request is still ours (withdrawn now, or
    stuck in place) nobody has it: restart normally. Only a marker that is
    gone means the updater may have it."""
    try:
        if restart_update.withdraw_switch_request(data_dir) or \
                restart_update.marker_present(data_dir):
            restart_update.clear_switch_files(data_dir)
            return "restart"
    except Exception:
        logger.exception("could not check the switch request after a watcher error")
    try:
        restart_update.clear_switch_files(data_dir)
    except Exception:
        logger.exception("could not clear switch files")
    return "exit"


def await_switch(data_dir: Path, tag: str, at: float, *,
                 pickup: float = restart_update.PICKUP_SECONDS,
                 accepted_wait: float = ACCEPTED_WAIT_SECONDS,
                 poll: float = POLL_SECONDS,
                 sleep: Callable[[float], None] = time.sleep,
                 on_taken: Optional[Callable[[], None]] = None) -> str:
    """Watch for the updater's answer to the switch request written at
    ``at``. Returns ``"restart"`` or ``"exit"`` (see above). Never raises.

    ``on_taken`` runs once the updater has (or may have) taken the request,
    before waiting to be killed — the app takes the results-CSV lock there,
    so the updater's taskkill /F cannot cut an append in half."""
    data_dir = Path(data_dir)
    try:
        verdict, outcome = _poll_switch(data_dir, at, _polls(pickup, poll), poll, sleep)
        if verdict == "unclaimed":
            answer = _withdraw_after_pickup(data_dir, tag, at, pickup,
                                            _polls(accepted_wait, poll), poll, sleep)
            if isinstance(answer, str):
                return answer
            verdict, outcome = answer
        if verdict == "refused":
            logger.warning("Updater refused the switch to %s (%s) — restarting normally",
                           tag, (outcome or {}).get("why", "no reason given"))
            restart_update.clear_switch_files(data_dir)
            return "restart"
        if verdict == "accepted":
            logger.info("Updater accepted the switch to %s; waiting to be stopped", tag)
        else:
            logger.warning("The switch request for %s was taken but left no outcome; "
                           "waiting to be stopped", tag)
        if on_taken is not None:
            try:
                on_taken()
            except Exception:
                logger.exception("on_taken failed; waiting to be stopped anyway")
        for _ in range(_polls(accepted_wait, poll)):
            sleep(poll)
        logger.warning("Still running %.0fs after the updater took the switch to %s",
                       accepted_wait, tag)
        restart_update.clear_switch_files(data_dir)
        return "exit"
    except Exception:
        logger.exception("Restart watcher for %s failed", tag)
        return _after_watcher_error(data_dir)
