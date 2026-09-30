"""tasks.py: the registry of running work behind the "running now" indicator
(v4.0 lane E; spec ``docs/superpowers/specs/2026-09-30-ui-redesign-live-setup-design.md``,
"Global status, a way home, and Hub admin").

Stdlib only, in memory, no import-time side effects beyond the process's one
``REGISTRY``. The runners feed it:

* ``hub_admin.AdminJobs`` (load-folder, history import and its dry run, purge,
  and the diagnostics runner ``DIAG_JOBS``);
* ``download_jobs.DownloadJobs`` (the report ZIPs);
* the QBench upload thread and ``POST /api/reprocess`` in ``app.py`` (a
  reprocess batch is watched: ``watch`` + ``jobs_probe`` read its jobs' states
  on this module's own thread, never on a request).

``snapshot(viewer, names=)`` is what ``GET /api/live`` carries as ``tasks``:
running tasks, plus tasks that ended within ``RETAIN_SECONDS``. Each is
``{id, kind, title, instrument, state, progress: {done, total, text} | None,
by, started_at, ended_at, open_url, download_url, mine}``. **No paths,
tokens, parameters, summaries or error text**: a feed can only set the
whitelisted fields, text is capped and must be a string, and a download link
goes to its owner only, until it expires or is fetched. Every signed-in user
sees titles, progress and ``by``; opening a result stays behind its own auth.

Every feed call is thread-safe and **never raises into its caller** (a status
line is never worth failing an import over).
"""
from __future__ import annotations

import itertools
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("tasks")

RETAIN_SECONDS = 30 * 60        # a finished task stays visible this long
MAX_KEPT = 200                  # tasks remembered at most (oldest ended dropped first)
TEXT_MAX = 120
STATES = ("running", "done", "failed", "stopped", "interrupted")
WATCH_SECONDS = 2.0
WATCH_MAX_ERRORS = 5

#: Progress phases of the admin jobs and the diagnostics build, in words.
#: An unknown phase is never echoed (it could be anything).
PHASES = {
    "scan": "Scanning the folder",
    "identify": "Reading the CDFs",
    "submit": "Submitting CDFs",
    "match": "Reading CDFs and matching them to the CSV",
    "check": "Checking CDFs",
    "import": "Classifying samples",
    "commit": "Committing",
    "purge": "Purging",
    "backup": "Backing up the database",
    "database": "Copying the database",
    "logs": "Collecting logs",
    "tables": "Exporting the store's tables",
    "settings": "Adding settings",
    "exports": "Checking the results files",
    "reports": "Adding reports",
    "problem_cdfs": "Adding held and failed CDFs",
    "calibration": "Adding calibration files",
    "updater": "Adding the updater's log",
    "all_cdfs": "Adding every raw CDF",
}


def phase_text(progress: Any) -> Optional[str]:
    """A job's ``progress`` event (``{phase, done, total, file}``) in words."""
    if not isinstance(progress, dict):
        return None
    phase = progress.get("phase")
    return PHASES.get(phase) if isinstance(phase, str) else None


def _plural(n: Any, one: str, many: str) -> str:
    return f"{n} {one if n in (1, '1') else many}"


def _title(kind: str, instrument: Optional[str], total: Any, names: dict) -> str:
    name = names.get(instrument) or instrument or "the GC"
    if kind == "load-folder":
        return f"Loading CDFs into {name}"
    if kind == "import-history":
        return f"Importing {name} history"
    if kind == "import-history-dry-run":
        return f"{name} history dry run"
    if kind == "purge":
        return f"Purging {name} data"
    if kind == "diagnostics-bundle":
        return "Diagnostics bundle"
    if kind == "reports-zip":
        return "Report ZIP" + (f" · {_plural(total, 'report', 'reports')}"
                               if isinstance(total, int) else "")
    if kind == "qbench-upload":
        return "QBench upload" + (f" · {_plural(total, 'report', 'reports')}"
                                  if isinstance(total, int) else "")
    if kind == "reprocess":
        return "Re-process" + (f" · {_plural(total, 'sample', 'samples')}"
                               if isinstance(total, int) else "")
    words = str(kind or "task").replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def _fmt(n: int) -> str:
    return f"{n:,}"


def _size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{round(n / (1024 * 1024))} MB"
    return f"{max(1, round(n / 1024))} KB"


def _outcome(kind: str, state: str, instrument: Optional[str], counts: dict,
             names: dict, why: str) -> str:
    """One line for an ended task: what happened, with its counts, never a
    repeat of its title and progress (v4.0 lane E review)."""
    name = names.get(instrument) or instrument or "the GC"
    c = {k: v for k, v in (counts or {}).items() if isinstance(v, int)}
    n = lambda k: c.get(k, 0)  # noqa: E731
    if state == "interrupted":
        base = {"load-folder": f"Loading CDFs into {name}",
                "import-history": f"{name} history import",
                "import-history-dry-run": f"{name} dry run",
                "purge": f"{name} purge", "diagnostics-bundle": "Diagnostics bundle",
                "reports-zip": "Report ZIP", "qbench-upload": "QBench upload",
                "reprocess": "Re-process"}.get(kind) or _title(kind, None, None, names)
        return base + (" stopped without finishing" if why == "crash"
                       else " interrupted by a restart")
    if kind == "import-history-dry-run":
        if state == "done":
            return f"{name} dry run finished" + (
                f" · {_fmt(n('classified'))} classified" if "classified" in c else "")
        return f"{name} dry run {'stopped' if state == 'stopped' else 'failed'}"
    if kind == "import-history":
        if state == "done":
            return f"{name} history imported · {_plural(_fmt(n('imported')), 'sample', 'samples')}"
        if state == "stopped":
            return f"{name} history import stopped" + (
                f" after {_fmt(n('done'))}" if "done" in c else "")
        return f"{name} history import failed"
    if kind == "load-folder":
        if state == "done":
            out = f"Loaded {_fmt(n('created'))} {'CDF' if n('created') == 1 else 'CDFs'} into {name}"
            return out + (f" · {_fmt(n('duplicate'))} already there" if n("duplicate") else "")
        return f"Loading CDFs into {name} {'stopped' if state == 'stopped' else 'failed'}"
    if kind == "purge":
        if state == "done":
            return f"Purged {name}" + (f" · {_plural(_fmt(n('samples')), 'sample', 'samples')}"
                                       if "samples" in c else "")
        return f"{name} purge {'stopped' if state == 'stopped' else 'failed'}"
    if kind == "diagnostics-bundle":
        if state == "done":
            return "Diagnostics ready" + (f" · {_size(n('bytes'))}" if "bytes" in c else "")
        return f"Diagnostics bundle {'stopped' if state == 'stopped' else 'failed'}"
    if kind == "reports-zip":
        if state == "done":
            return "Report ZIP ready" + (f" · {_plural(_fmt(n('reports')), 'report', 'reports')}"
                                         if "reports" in c else "")
        return "Report ZIP failed"
    if kind == "qbench-upload":
        if state == "stopped":
            return f"QBench upload stopped after {_plural(_fmt(n('ok')), 'report', 'reports')}"
        if state in ("done", "failed") and n("ok"):
            return (f"Uploaded {_plural(_fmt(n('ok')), 'report', 'reports')} to QBench"
                    + (f" · {_fmt(n('failed'))} failed" if n("failed") else ""))
        return "QBench upload failed" + (f" · {_plural(_fmt(n('failed')), 'report', 'reports')}"
                                         if n("failed") else "")
    if kind == "reprocess":
        if n("ok") or state == "done" and not n("failed"):
            return (f"Re-processed {_plural(_fmt(n('ok')), 'sample', 'samples')}"
                    + (f" · {_fmt(n('failed'))} failed" if n("failed") else ""))
        return "Re-process failed" + (f" · {_plural(_fmt(n('failed')), 'sample', 'samples')}"
                                      if n("failed") else "")
    verb = {"done": "finished", "stopped": "stopped"}.get(state, "failed")
    return f"{_title(kind, instrument, None, names)} {verb}"


def _counts(v: Any) -> dict:
    """Only whole, non-negative numbers: an outcome never carries text."""
    if not isinstance(v, dict):
        return {}
    return {str(k)[:32]: int(x) for k, x in v.items()
            if isinstance(x, int) and not isinstance(x, bool) and x >= 0}


def _local_url(u: Any) -> Optional[str]:
    """A path on this hub ("/..."), never "//host" or "/\\host" (another site)."""
    if isinstance(u, str) and u.startswith("/") and not u.startswith(("//", "/\\")):
        return u
    return None


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _num(v: Any) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def _text(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    v = " ".join(v.split())
    return v[:TEXT_MAX] or None


def _name(v: Any) -> Optional[str]:
    return v.strip()[:64] or None if isinstance(v, str) else None


class Registry:
    """The process's running work. See the module docstring."""

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 retain: float = RETAIN_SECONDS) -> None:
        self._clock = clock
        self.retain = retain
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._tasks: dict = {}
        self._watches: dict = {}          # task id -> [probe, errors]
        self._watcher: Optional[threading.Thread] = None

    # ── the feeds ──
    def begin(self, kind: str, *, by: Optional[str] = None, owner: Optional[str] = None,
              instrument: Optional[str] = None, total: Any = None, text: Any = None,
              open_url: Optional[str] = None,
              alive: Optional[Callable[[], bool]] = None) -> str:
        """A new running task; returns its id (``"<kind>:<n>"``, never a job's
        own id, which may be a secret). ``owner`` (default ``by``) is who may
        download its result."""
        tid = f"{kind}:{next(self._ids)}"
        try:
            now = self._clock()
            task = {"id": tid, "kind": str(kind), "instrument": _name(instrument),
                    "state": "running", "done": None, "total": _num(total),
                    "text": _text(text), "by": _name(by),
                    "owner": _name(owner) or _name(by), "started": now, "ended": None,
                    "open_url": _local_url(open_url),
                    "download": None, "download_until": None, "alive": alive,
                    "counts": {}, "why": "restart"}
            with self._lock:
                self._tasks[tid] = task
                self._prune(now)
        except Exception:  # noqa: BLE001 - never into the caller
            log.exception("tasks: begin(%r) failed", kind)
        return tid

    def update(self, tid: str, *, done: Any = None, total: Any = None,
               text: Any = None) -> None:
        """Progress of a running task (``None`` leaves a field alone)."""
        try:
            with self._lock:
                t = self._tasks.get(tid)
                if t is None or t["state"] != "running":
                    return
                self._set_progress(t, done, total, text)
        except Exception:  # noqa: BLE001
            log.exception("tasks: update failed")

    @staticmethod
    def _set_progress(t: dict, done: Any, total: Any, text: Any) -> None:
        if done is not None:
            t["done"] = _num(done)
        if total is not None:
            t["total"] = _num(total)
        if text is not None:
            t["text"] = _text(text)

    def finish(self, tid: str, state: str, *, done: Any = None, total: Any = None,
               text: Any = None, download: Optional[str] = None,
               download_until: Optional[float] = None, counts: Any = None) -> None:
        """The task ended (``done | failed | stopped | interrupted``; anything
        else counts as failed). ``download``: the owner's one-time link, valid
        until ``download_until``."""
        try:
            now = self._clock()
            with self._lock:
                t = self._tasks.get(tid)
                if t is None or t["state"] != "running":
                    return
                self._set_progress(t, done, total, text)
                t["state"] = state if state in STATES and state != "running" else "failed"
                t["ended"] = now
                t["counts"] = _counts(counts)
                if _local_url(download):
                    t["download"] = download
                    t["download_until"] = download_until
                self._watches.pop(tid, None)
                self._prune(now)
        except Exception:  # noqa: BLE001
            log.exception("tasks: finish failed")

    def set_download(self, tid: str, url: Optional[str],
                     until: Optional[float] = None) -> None:
        """Set or clear (fetched) a task's download link."""
        try:
            with self._lock:
                t = self._tasks.get(tid)
                if t is not None:
                    t["download"] = _local_url(url)
                    t["download_until"] = until
        except Exception:  # noqa: BLE001
            log.exception("tasks: set_download failed")

    def note_interrupted(self, kind: str, *, instrument: Optional[str] = None,
                         by: Optional[str] = None, open_url: Optional[str] = None) -> str:
        """Start-up recovery found work a restart cut short (an import, a
        purge): show it as an ended, interrupted task for ``RETAIN_SECONDS``."""
        tid = self.begin(kind, by=by, instrument=instrument, open_url=open_url)
        self.finish(tid, "interrupted")
        return tid

    # ── watched tasks (a reprocess batch: its jobs finish on the Worker) ──
    def watch(self, tid: str, probe: Callable[[], dict], *, start: bool = True) -> None:
        """Call ``probe()`` every ``WATCH_SECONDS`` on this registry's thread
        until it returns a ``state`` (``{done, total, text, state?}``). A probe
        that keeps failing (``WATCH_MAX_ERRORS`` in a row) fails the task."""
        with self._lock:
            self._watches[tid] = [probe, 0]
        if start:
            self._start_watcher()

    def run_watches(self) -> None:
        """One round of every watch (the thread's body; tests call it)."""
        with self._lock:
            items = list(self._watches.items())
        for tid, entry in items:
            probe = entry[0]
            try:
                out = probe() or {}
            except Exception:  # noqa: BLE001 - a locked store, ...
                entry[1] += 1
                if entry[1] >= WATCH_MAX_ERRORS:
                    log.warning("tasks: giving up watching %s", tid, exc_info=True)
                    self.finish(tid, "failed")
                continue
            entry[1] = 0
            state = out.get("state")
            if state:
                self.finish(tid, state, done=out.get("done"), total=out.get("total"),
                            text=out.get("text"), counts=out.get("counts"))
            else:
                self.update(tid, done=out.get("done"), total=out.get("total"),
                            text=out.get("text"))

    def _start_watcher(self) -> None:
        with self._lock:
            if self._watcher is not None and self._watcher.is_alive():
                return

            def loop() -> None:
                while True:
                    time.sleep(WATCH_SECONDS)
                    try:
                        self.run_watches()
                    except Exception:  # noqa: BLE001 - the loop must survive
                        log.exception("tasks: watch round failed")
                    with self._lock:
                        if not self._watches:
                            self._watcher = None
                            return

            self._watcher = threading.Thread(target=loop, name="gc-tasks-watch", daemon=True)
            self._watcher.start()

    # ── the feed ──
    def _prune(self, now: float) -> None:
        """Forget tasks that ended more than ``retain`` ago; cap the rest (lock held)."""
        for tid, t in list(self._tasks.items()):
            if t["ended"] is not None and now - t["ended"] > self.retain:
                del self._tasks[tid]
        if len(self._tasks) > MAX_KEPT:
            ended = sorted((t for t in self._tasks.values() if t["ended"] is not None),
                           key=lambda t: t["ended"])
            for t in ended[:len(self._tasks) - MAX_KEPT]:
                del self._tasks[t["id"]]

    def snapshot(self, viewer: Optional[str] = None, *,
                 names: Optional[dict] = None) -> list:
        """The feed for ``viewer`` (the signed-in name): running tasks first
        (newest first), then those that ended within ``RETAIN_SECONDS``
        (most recent first). Memory only; never raises."""
        try:
            return self._snapshot(viewer, names or {})
        except Exception:  # noqa: BLE001
            log.exception("tasks: snapshot failed")
            return []

    def _snapshot(self, viewer: Optional[str], names: dict) -> list:
        now = self._clock()
        with self._lock:
            self._prune(now)
            rows = list(self._tasks.values())
            for t in rows:
                if t["state"] == "running" and t["alive"] is not None:
                    try:
                        dead = not t["alive"]()
                    except Exception:  # noqa: BLE001
                        dead = False
                    if dead:
                        t["state"], t["ended"], t["why"] = "interrupted", now, "crash"
                        self._watches.pop(t["id"], None)
            rows = [dict(t) for t in rows]
        running = sorted((t for t in rows if t["state"] == "running"),
                         key=lambda t: t["started"], reverse=True)
        ended = sorted((t for t in rows if t["state"] != "running"),
                       key=lambda t: t["ended"], reverse=True)
        who = _name(viewer)
        out = []
        for t in running + ended:
            mine = who is not None and who == t["owner"]
            download = t["download"] if (mine and t["download"] and (
                t["download_until"] is None or now < t["download_until"])) else None
            progress = None
            if t["done"] is not None or t["total"] is not None or t["text"] is not None:
                progress = {"done": t["done"], "total": t["total"], "text": t["text"]}
            out.append({
                "id": t["id"], "kind": t["kind"],
                "title": _title(t["kind"], t["instrument"], t["total"], names),
                "instrument": t["instrument"], "state": t["state"], "progress": progress,
                "by": t["by"], "started_at": _iso(t["started"]), "ended_at": _iso(t["ended"]),
                "open_url": t["open_url"], "download_url": download, "mine": mine,
                "outcome": None if t["state"] == "running" else _outcome(
                    t["kind"], t["state"], t["instrument"], t["counts"], names, t["why"]),
            })
        return out

    def reset(self) -> None:
        """Forget everything (tests)."""
        with self._lock:
            self._tasks.clear()
            self._watches.clear()


REGISTRY = Registry()


# ── probes ──────────────────────────────────────────────────────────────────

FINISHED_JOB_STATES = ("done", "failed", "superseded")


def jobs_probe(job_ids: Iterable[Any], db: Path, *, timeout: float = 0.25) -> Callable[[], dict]:
    """A ``watch`` probe for a reprocess batch: how many of the store's
    ``jobs`` rows ``job_ids`` have finished (read-only, short busy timeout).
    Jobs no longer in the store are not counted."""
    ids = sorted({int(i) for i in job_ids if isinstance(i, int) and not isinstance(i, bool)})

    def probe() -> dict:
        if not ids:
            return {"done": 0, "total": 0, "state": "done"}
        conn = sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True,
                               timeout=timeout)
        try:
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT state FROM jobs WHERE id IN ({marks})", ids).fetchall()
        finally:
            conn.close()
        states = [r[0] for r in rows]
        total = len(states)
        finished = sum(1 for s in states if s in FINISHED_JOB_STATES)
        failed = sum(1 for s in states if s == "failed")
        text = f"{finished} of {total} re-processed" + (f" · {failed} failed" if failed else "")
        out = {"done": finished, "total": total, "text": text,
               "counts": {"ok": finished - failed, "failed": failed}}
        if finished == total:
            out["state"] = "failed" if total and failed == total else "done"
        return out

    return probe


def note_unfinished_imports(db: Path, *, registry: Optional[Registry] = None) -> int:
    """At start-up, before the hub runs anything (v4.0 lane E review): every
    real history import the store says started but never finished was cut
    short by the restart (admin jobs die with the process). Each shows as an
    interrupted task for ``RETAIN_SECONDS``. Read-only; a missing or locked
    store notes nothing. Returns how many were noted."""
    reg = registry or REGISTRY
    p = Path(db)
    if not p.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = conn.execute('SELECT instrument_id, "by" FROM import_runs '
                                "WHERE finished_at IS NULL").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        log.warning("tasks: could not read unfinished import runs", exc_info=True)
        return 0
    for inst, by in rows:
        name = by.rsplit(" (", 1)[0] if isinstance(by, str) and by.endswith(")") else by
        reg.note_interrupted("import-history", instrument=inst, by=name,
                             open_url="/admin/hub#import-history")
    return len(rows)


__all__ = ["REGISTRY", "Registry", "jobs_probe", "phase_text", "RETAIN_SECONDS"]
