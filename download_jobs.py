"""download_jobs.py: a file built in the background, then fetched once (v3.0.1).

https://gc.asaplabs.net is a Cloudflare tunnel, and Cloudflare ends any
request whose answer takes more than 100 s (HTTP 524). Anything that builds a
large download (the report ZIP: one PDF per sample, seconds each) therefore
starts a job here and answers at once; the page polls the job and, when it is
done, navigates to a one-time link that streams the file and deletes it
(like the diagnostics bundle's download).

- ``start(kind, fn, name=, owner=, total=)`` runs ``fn(progress, out_path)``
  on a daemon thread; ``fn`` writes the file to ``out_path`` and returns a
  small summary (``result``). ``progress(done=, total=)`` updates the counts.
  ``NothingToDownload`` fails the job with just its message.
- At most ``max_running`` builds at once (``Busy``).
- The job id is random (``secrets.token_urlsafe(32)``) and only the owner (the
  signed-in name that started it) sees or fetches it.
- ``claim`` hands the file out once; ``finished_streaming`` deletes it. A
  finished job and its file expire ``ttl`` seconds after it ended.
- ``busy()`` (a build running or a file waiting) holds off the 3 AM restart
  and the hub tray's Stop.
- With ``tasks=`` (a ``tasks.Registry``; v4.0 lane E) each build is also a
  task: its kind, owner and ``done``/``total``, and once done the owner's
  ``download_url(job_id)`` until the file is fetched or expires. The job id
  (the link's secret) is never the task's id.

Stdlib only; no import-time side effects.
"""
from __future__ import annotations

import contextlib
import logging
import secrets
import shutil
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("download_jobs")

MAX_KEPT = 50           # finished jobs remembered at most (oldest dropped first)


class Busy(RuntimeError):
    """Too many builds are running."""


class NothingToDownload(RuntimeError):
    """The build produced nothing worth downloading (the message says why)."""


_PUBLIC = ("id", "kind", "state", "name", "done", "total", "size", "result", "error",
           "started_at", "finished_at")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DownloadJobs:
    def __init__(self, folder: Callable[[], Path], *, max_running: int = 2,
                 ttl: float = 600.0, tasks=None,
                 download_url: Optional[Callable[[str], str]] = None) -> None:
        self._folder = folder
        self.max_running = max_running
        self.ttl = ttl
        self._lock = threading.Lock()
        self._jobs: dict = {}
        self._tasks = tasks                  # v4.0 lane E: the running-now feed
        self._download_url = download_url

    # ── internals ──
    def _purge(self, now: float) -> None:
        """Forget expired finished jobs (deleting their files); lock held."""
        for jid, job in list(self._jobs.items()):
            ended = job.get("_ended")
            if ended is not None and ended + self.ttl <= now and job["state"] != "streaming":
                self._jobs.pop(jid, None)
                self._unlink(job)
        finished = sorted((j for j in self._jobs.values() if j.get("_ended") is not None),
                          key=lambda j: j["_ended"])
        for job in finished[:max(0, len(self._jobs) - MAX_KEPT)]:
            if job["state"] != "streaming":
                self._jobs.pop(job["id"], None)
                self._unlink(job)

    @staticmethod
    def _unlink(job: dict) -> None:
        for p in (job.get("_path"), job.get("_part")):
            if p is not None:
                with contextlib.suppress(OSError):
                    Path(p).unlink()

    @staticmethod
    def _public(job: dict) -> dict:
        return {k: job.get(k) for k in _PUBLIC}

    def running(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j["state"] == "running")

    # ── the API ──
    def start(self, kind: str, fn: Callable, *, name: str, owner: str, total: int) -> dict:
        folder = Path(self._folder())
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._purge(time.time())
            if sum(1 for j in self._jobs.values() if j["state"] == "running") >= self.max_running:
                raise Busy(f"{self.max_running} downloads are already being built; "
                           f"try again when one has finished")
            jid = secrets.token_urlsafe(32)
            job = {"id": jid, "kind": kind, "state": "running", "name": name, "done": 0,
                   "total": total, "size": None, "result": None, "error": None,
                   "started_at": _now_iso(), "finished_at": None, "_owner": owner,
                   "_part": folder / f"{jid}.part", "_path": None, "_ended": None,
                   "_task": None}
            if self._tasks is not None:
                job["_task"] = self._tasks.begin(kind, by=owner, owner=owner, total=total)
            self._jobs[jid] = job
        threading.Thread(target=self._run, args=(job, fn), daemon=True,
                         name=f"download-{kind}").start()
        return self._public(job)

    def _progress(self, job: dict, done=None, total=None) -> None:
        with self._lock:
            if done is not None:
                job["done"] = done
            if total is not None:
                job["total"] = total
            if job.get("_task") is not None:
                self._tasks.update(job["_task"], done=done, total=total)

    def _run(self, job: dict, fn: Callable) -> None:
        part: Path = job["_part"]
        try:
            result = fn(lambda **kw: self._progress(job, **kw), part)
            final = part.with_suffix(".done")
            part.replace(final)
            size = final.stat().st_size
            state, error = "done", None
        except NothingToDownload as exc:
            result, final, size, state, error = None, None, None, "failed", str(exc)
        except BaseException as exc:  # noqa: BLE001 - report it, never kill the hub
            log.error("download job %s (%s) failed:\n%s", job["kind"], job["name"],
                      traceback.format_exc())
            result, final, size = None, None, None
            state, error = "failed", f"{type(exc).__name__}: {exc}"
        if final is None:
            with contextlib.suppress(OSError):
                part.unlink()
        with self._lock:
            job.update(state=state, error=error, result=result, size=size, _path=final,
                       _part=None, finished_at=_now_iso(), _ended=time.time())
            if job.get("_task") is not None:
                url = (self._download_url(job["id"])
                       if state == "done" and self._download_url is not None else None)
                written = (result or {}).get("written") if isinstance(result, dict) else None
                self._tasks.finish(job["_task"], state, download=url,
                                   download_until=job["_ended"] + self.ttl,
                                   counts={"reports": written if isinstance(written, int)
                                           else job.get("done")})

    def status(self, job_id: str, owner: str) -> Optional[dict]:
        """The job, or None (unknown, expired, or someone else's)."""
        with self._lock:
            self._purge(time.time())
            job = self._jobs.get(str(job_id))
            if job is None or job["_owner"] != owner:
                return None
            return self._public(job)

    def claim(self, job_id: str, owner: str) -> Optional[dict]:
        """``{path, name, size}`` of a finished file, once; None otherwise."""
        with self._lock:
            self._purge(time.time())
            job = self._jobs.get(str(job_id))
            if job is None or job["_owner"] != owner or job["state"] != "done":
                return None
            job["state"] = "streaming"
            if job.get("_task") is not None:
                self._tasks.set_download(job["_task"], None)     # fetched: no more link
            return {"id": job["id"], "path": str(job["_path"]), "name": job["name"],
                    "size": job["size"]}

    def finished_streaming(self, claimed: dict) -> None:
        """The claimed file was sent (or the client went away): delete it."""
        with self._lock:
            job = self._jobs.get(claimed["id"])
            if job is not None:
                job["state"] = "fetched"
                job["_path"] = None
        with contextlib.suppress(OSError):
            Path(claimed["path"]).unlink()

    def busy(self) -> bool:
        """A build running, a file waiting to be fetched, or one streaming."""
        with self._lock:
            self._purge(time.time())
            return any(j["state"] in ("running", "done", "streaming") for j in self._jobs.values())

    def cleanup(self) -> None:
        """At start-up: delete whatever a previous process left in the folder."""
        folder = Path(self._folder())
        if folder.is_dir():
            for p in folder.iterdir():
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        p.unlink()


__all__ = ["Busy", "DownloadJobs", "NothingToDownload"]
