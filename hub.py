"""hub.py: the hub's one start-up (phase 2, 2A1 T5; integration carry-over I4).

``start(app_conf)`` owns everything that runs in the background, in this
order, and ``HubRuntime.stop()`` undoes it:

1. ``store.migrate`` → ``instruments.bootstrap_gc1`` → ``instruments.startup``,
   which starts the one ``pipeline.Worker`` (sweeps ``cdf/.incoming``,
   requeues, starts its thread). The Worker's ``notifier`` is the
   notification store's ``add``, its corrections come **only** from the
   hub's store (``corrections_provider(db)``, never the phase-1 file per
   job) and its ``on_final`` wakes the exporter (and calls the caller's
   ``on_final``, which the app uses to refresh ``sample_cache``).
2. ``exports.HubExporter(notifier).start()``: appends every instrument's
   pending ledger rows to its export file, every ``export_interval``
   seconds and at once after a result becomes final (``wake_exports``).
3. Outside the module lock (it reads a file that may be on the share):
   ``seed_gc1_corrections``, gc1's eleven factors from the phase-1 file into
   ``instrument_corrections`` once, through ``instrument_admin.seed_gc1``.
4. ``Maintenance``: a thread that, every ``maintenance_interval`` seconds,
   retries that seed every ``SEED_RETRY`` while gc1 has none, writes the
   nightly ``store.backup_nightly`` (once per local day, from
   ``BACKUP_HOUR``; the day's file is the record, so a restart doesn't
   back up twice) and deletes ``done``/``superseded`` jobs older than
   ``PRUNE_DAYS`` (``store.jobs.prune_done``, once per day). A failed
   backup is notified once and retried after ``BACKUP_RETRY``.

``HubRuntime.pause()``/``resume()`` stop and restart the Worker, exporter and
maintenance threads while the runtime stays (the hub tray's "Pause
processing", ``hub_control``); ``set_processing_paused`` persists the choice
in ``settings_kv`` and ``start`` honours it (``paused=``).

``start_with_retry(start_fn)`` is how the app calls it: retried with backoff
(``START_BACKOFF``), notified after ``START_NOTIFY_AFTER`` failures.
``background_busy(db)`` tells the auto-restart whether the Worker has work.

One runtime per process (``running()``); the app calls ``start`` from
``_init_app`` and ``stop`` at exit. ``GC_DATA_DIR`` is required: without it
``paths.DataDirMissing`` is raised (app.py refuses to start before that).

Corrections are hub-owned (D4b): the Worker reads only
``instrument_corrections`` (``StoreProvider``, source ``hub``). gc1 is seeded
once from the phase-1 file; a missing or invalid file is notified once and gc1
stays ``pending_corrections`` (never silent zeros) until a retry succeeds or
its values are entered on the Instruments page; every other instrument waits
until its eleven values are entered.

The running exporter is handed to ``instruments_api.set_exporter`` (2A2) and
``hub_admin.set_exporter`` when those modules exist, for the admin export
actions (their refusal state lives in the exporter).
"""
from __future__ import annotations

import functools
import json
import os
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import corrections
import exports
import instruments
import paths
import store

log = logging.getLogger("hub")

BACKUP_HOUR = 1                      # local; before the 3 AM auto-restart
BACKUP_KEEP = 14
BACKUP_RETRY = timedelta(hours=1)
PRUNE_DAYS = 30
MAINTENANCE_INTERVAL_SECONDS = 300

Notifier = Callable[[str, str], Any]
_DEFAULT = object()

_lock = threading.Lock()
_running: Optional["HubRuntime"] = None


def corrections_provider(db) -> corrections.StoreProvider:
    """The Worker's ``corrections_provider``: the instrument's saved hub
    corrections (D4b), nothing else. No row → ``pending_corrections``."""
    return corrections.StoreProvider(functools.partial(store.corrections.read, db=db))


SEED_BY = "startup"
SEED_RETRY = timedelta(minutes=10)
SEED_FAILED_TEXT = ("GC-1 has no correction factors: they could not be seeded from the "
                    "phase-1 file ({why}). GC-1 samples wait as pending_corrections. The hub "
                    "retries the seed every 10 minutes (fix correction_factors_json or the "
                    "share), or enter the 11 factors on the Instruments page.")


def seed_gc1_corrections(app_conf: dict, db, notifier: Optional[Notifier] = None, *,
                         notify_failure: bool = True) -> Optional[bool]:
    """Give ``gc1`` its corrections from the phase-1 file (``settings.json``'s
    ``correction_factors_json``) once, through ``instrument_admin.seed_gc1``
    (the one seed implementation: audited, and gc1's ``pending_corrections``
    samples are queued in the same transaction). ``None`` when gc1 already has
    hub corrections (never touched); True when it seeded (info notification);
    False when the file is missing, unreadable or invalid (the seed never
    writes zeros): logged, and notified when ``notify_failure``."""
    import instrument_admin
    if store.corrections.read(instruments.GC1, db=db) is not None:
        return None
    conf = app_conf or {}
    path = str(conf.get("correction_factors_json") or "").strip()
    try:
        if not path:
            raise instrument_admin.AdminError("correction_factors_json is not set", 409)
        result = instrument_admin.seed_gc1(conf, by=SEED_BY, db=db)
    except instrument_admin.AdminError as exc:
        if store.corrections.read(instruments.GC1, db=db) is not None:
            return None                      # seeded meanwhile (the Instruments page)
        msg = SEED_FAILED_TEXT.format(why=exc.message)
        log.error("%s", msg)
        if notify_failure:
            _notify(notifier, "error", msg)
        return False
    msg = (f"GC-1 correction factors were seeded from {path} ({result['queued']} waiting "
           f"sample(s) queued). Check them on the Instruments page; never also enter GC "
           f"factors in LEM.")
    log.info("%s", msg)
    _notify(notifier, "info", msg)
    return True


def _notify(notifier: Optional[Notifier], level: str, message: str) -> None:
    if notifier is None:
        return
    try:
        notifier(level, message)
    except Exception:  # noqa: BLE001 - a notification never stops the hub
        log.exception("hub: notifier failed")


def default_notifier() -> Optional[Notifier]:
    try:
        import notifications
        return notifications.get_store().add
    except Exception:  # noqa: BLE001 - notifications must never stop the hub
        log.exception("hub: notifications unavailable; logging only")
        return None


class Maintenance:
    """The nightly backup, the job-table and sign-in-session prunes and, while gc1 has no hub
    corrections, the seed retry every ``SEED_RETRY`` (``run_once`` is one
    pass). ``conf_fn`` supplies ``settings.json`` for the seed (default
    ``settings.load_settings``); ``seed_attempted_at`` is when the start-up
    last tried (so the first retry waits the full interval)."""

    def __init__(self, db, data_dir, *, notifier: Optional[Notifier] = None,
                 keep: int = BACKUP_KEEP, conf_fn: Optional[Callable[[], dict]] = None,
                 seed_attempted_at: Optional[datetime] = None) -> None:
        self.db = Path(db)
        self.data_dir = Path(data_dir)
        self.notifier = notifier
        self.keep = keep
        self.conf_fn = conf_fn
        self._seed_at = seed_attempted_at
        self._failed_at: Optional[datetime] = None
        self._notified_day: Optional[str] = None
        self._pruned_day: Optional[str] = None
        self._sessions_pruned: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _notify(self, level: str, message: str) -> None:
        log.error("%s", message)
        if self.notifier is not None:
            try:
                self.notifier(level, message)
            except Exception:  # noqa: BLE001
                log.exception("hub: notifier failed")

    def _backup(self, now: datetime) -> Optional[Path]:
        day = now.strftime("%Y-%m-%d")
        if now.hour < BACKUP_HOUR:
            return None
        if (self.db.parent / "backups" / f"gc-{day}.db").is_file():
            return None
        if self._failed_at is not None and now - self._failed_at < BACKUP_RETRY:
            return None
        try:
            dest = store.backup_nightly(self.db, keep=self.keep,
                                        settings_path=self.data_dir / "settings.json", now=now)
        except Exception as exc:  # noqa: BLE001 - retried later, never fatal
            self._failed_at = now
            log.exception("hub: nightly backup failed")
            if self._notified_day != day:
                self._notified_day = day
                self._notify("error", f"The nightly backup of the GC hub database failed: {exc}. "
                                      f"It is retried every hour; check free space in the data folder.")
            return None
        self._failed_at = None
        return dest

    def _prune(self, now: datetime) -> Optional[int]:
        day = now.strftime("%Y-%m-%d")
        if self._pruned_day == day:
            return None
        cutoff = (now - timedelta(days=PRUNE_DAYS)).astimezone(timezone.utc)
        try:   # sign-in sessions that ended (revoked, expired, idle) over 30 days ago
            self._sessions_pruned = store.web_sessions.prune(
                older_than_days=store.WEB_SESSION_PRUNE_DAYS,
                idle_seconds=store.WEB_SESSION_IDLE_SECONDS, db=self.db)
            if self._sessions_pruned:
                log.info("hub: pruned %d ended sign-in session(s)", self._sessions_pruned)
        except Exception:  # noqa: BLE001
            log.exception("hub: pruning old sign-in sessions failed")
        try:
            n = store.jobs.prune_done(cutoff, db=self.db)
        except Exception:  # noqa: BLE001
            log.exception("hub: pruning finished jobs failed")
            return None
        self._pruned_day = day
        if n:
            log.info("hub: pruned %d finished job(s) older than %d days", n, PRUNE_DAYS)
        return n

    def _seed(self, now: datetime) -> Optional[bool]:
        if store.corrections.read(instruments.GC1, db=self.db) is not None:
            return None
        if self._seed_at is not None and now - self._seed_at < SEED_RETRY:
            return None
        self._seed_at = now
        if self.conf_fn is not None:
            conf = self.conf_fn()
        else:
            import settings
            conf = settings.load_settings()
        # quiet on failure: the start-up already raised the notification
        return seed_gc1_corrections(conf, self.db, self.notifier, notify_failure=False)

    def run_once(self, now: Optional[datetime] = None) -> dict:
        """``now`` is local and naive (default the current time)."""
        now = now or datetime.now()
        try:
            seeded = self._seed(now)
        except Exception:  # noqa: BLE001 - never stops the backup
            log.exception("hub: gc1 corrections seed retry failed")
            seeded = False
        self._sessions_pruned = None
        return {"seeded": seeded, "backup": self._backup(now), "pruned": self._prune(now),
                "sessions_pruned": self._sessions_pruned}

    def start(self, interval: float = MAINTENANCE_INTERVAL_SECONDS,
              join_timeout: float = 60.0) -> None:
        """Start the loop; a previous thread whose ``stop`` timed out is
        waited for first (``RuntimeError`` if it is still running after
        ``join_timeout``), so two loops never run at once."""
        if self._thread is not None and self._thread.is_alive():
            if not self._stop.is_set():
                return
            self._thread.join(join_timeout)
            if self._thread.is_alive():
                raise RuntimeError("the previous maintenance thread is still running")
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001 - the loop must survive
                    log.exception("hub: maintenance pass failed")
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, name="gc-hub-maintenance", daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float = 10.0) -> None:
        """A thread still busy after ``timeout`` is kept (``is_alive``) and
        finishes on its own; ``start`` waits for it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if not self._thread.is_alive():
                self._thread = None


class HubRuntime:
    """What ``start`` started: ``worker``, ``exporter``, ``maintenance``.

    ``pause()`` stops those three threads while the runtime (and the web app
    around it) stays up; ``resume()`` starts them again. Ingest keeps
    accepting meanwhile: ``pipeline.submit`` only needs the store, and the
    queued jobs run on resume. ``paused`` says which it is. The persisted
    choice (``set_processing_paused``) is the caller's business."""

    def __init__(self, data_dir: Path, db: Path, worker, exporter: exports.HubExporter,
                 maintenance: Optional[Maintenance], notifier: Optional[Notifier], *,
                 export_interval: float = exports.FLUSH_INTERVAL_SECONDS,
                 maintenance_enabled: bool = True,
                 maintenance_interval: float = MAINTENANCE_INTERVAL_SECONDS,
                 conf_fn: Optional[Callable[[], dict]] = None,
                 paused: bool = False) -> None:
        self.data_dir = data_dir
        self.db = db
        self.worker = worker
        self.exporter = exporter
        self.maintenance = maintenance
        self.notifier = notifier
        self.export_interval = export_interval
        self.maintenance_enabled = maintenance_enabled
        self.maintenance_interval = maintenance_interval
        self.conf_fn = conf_fn
        self.paused = paused
        self._pause_lock = threading.Lock()

    def wake_exports(self) -> None:
        """Flush now: call after anything writes a ledger row outside the
        Worker (Export to LIMS, a backfill release)."""
        self.exporter.wake()

    def exporter_alive(self) -> bool:
        return self.exporter.is_alive()

    def pause(self, timeout: float = 10.0) -> None:
        """Stop the Worker, exporter and maintenance threads (each finishes
        what it is doing first); idempotent."""
        with self._pause_lock:
            if self.paused:
                return
            self.paused = True
            if self.maintenance is not None:
                self.maintenance.stop(timeout)
            self.worker.stop(timeout)
            self.exporter.stop(timeout)
        log.warning("hub: processing paused (the Worker, exports and maintenance are stopped)")

    def resume(self) -> None:
        """Start the threads ``pause`` stopped (or a paused start never
        started); idempotent."""
        with self._pause_lock:
            if not self.paused:
                return
            self.worker.start()
            self.exporter.start(self.export_interval)
            if self.maintenance_enabled:
                if self.maintenance is None:
                    self.maintenance = Maintenance(self.db, self.data_dir, notifier=self.notifier,
                                                   conf_fn=self.conf_fn,
                                                   seed_attempted_at=datetime.now())
                self.maintenance.start(self.maintenance_interval)
            self.paused = False
        log.warning("hub: processing resumed")

    def stop(self, timeout: float = 10.0) -> None:
        global _running
        if self.maintenance is not None:
            self.maintenance.stop(timeout)
        self.worker.stop(timeout)
        self.exporter.stop(timeout)
        with _lock:
            if _running is self:
                _running = None
        _share_exporter(None)
        log.info("hub: stopped")


def _share_exporter(exporter: Optional[exports.HubExporter]) -> None:
    """Hand the running exporter to the modules whose admin routes need its
    in-memory state (refusals, pending since): ``instruments_api`` (2A2)
    when present, and ``hub_admin``."""
    for name in ("instruments_api", "hub_admin"):
        try:
            mod = __import__(name)
        except ImportError:
            continue
        setter = getattr(mod, "set_exporter", None)
        if setter is not None:
            try:
                setter(exporter)
            except Exception:  # noqa: BLE001
                log.exception("hub: %s.set_exporter failed", name)


def running() -> Optional[HubRuntime]:
    return _running


def start(app_conf: Optional[dict] = None, *, data_dir=None, notifier: Any = _DEFAULT,
          conf_fn: Optional[Callable[[], dict]] = None,
          on_final: Optional[Callable[[int], Any]] = None,
          export_interval: float = exports.FLUSH_INTERVAL_SECONDS,
          maintenance: bool = True,
          maintenance_interval: float = MAINTENANCE_INTERVAL_SECONDS,
          paused: Optional[bool] = None,
          **worker_kw) -> HubRuntime:
    """Start the hub (see the module docstring) and return its runtime.

    ``app_conf`` is ``settings.json`` (default ``settings.load_settings()``),
    used to bootstrap ``gc1``; ``conf_fn`` supplies it per job (default
    ``settings.load_settings``). ``notifier`` defaults to the notification
    store's ``add`` (``None`` = log only). ``RuntimeError`` if a runtime is
    already running in this process.

    ``paused`` (default: the persisted ``processing_paused`` flag) builds
    the Worker, exporter and maintenance without starting their threads;
    ``HubRuntime.resume()`` starts them.
    """
    global _running
    data = Path(data_dir) if data_dir is not None else paths.require_data_dir()
    db = data / store.DB_FILENAME
    if app_conf is None:
        import settings
        app_conf = settings.load_settings()
    if notifier is _DEFAULT:
        notifier = default_notifier()
    with _lock:
        if _running is not None:
            raise RuntimeError("the hub is already running in this process")
        store.migrate(db)
        if paused is None:
            paused = processing_paused(db) is not None
        # A purge the process died in is finished (or abandoned) before the
        # exporter can flush (its sidecar must be unlinked first), and an
        # interrupted import is marked: once per process and data folder, and
        # admin jobs are refused until it has run (hub_admin.job_refusal).
        run_startup_recovery(db, data, notifier)
        instruments.bootstrap_gc1(app_conf, db=db)
        exporter =exports.HubExporter(db, data_dir=data, notifier=notifier)

        def final_hook(sample_id: int) -> None:
            exporter.wake()
            if on_final is not None:
                on_final(sample_id)

        # Hub corrections only, never the phase-1 file per job (I1): an
        # unseeded gc1 waits for the seed (below, then Maintenance).
        worker_kw.setdefault("corrections_provider", corrections_provider(db))
        worker = instruments.startup(app_conf, notifier, db=db, data_dir=data, conf_fn=conf_fn,
                                     on_final=final_hook, start=not paused, **worker_kw)
        if paused:
            # Worker.start (which requeues) will not run until resume, so a
            # job left `running` by a dead process is put back now: nothing
            # is running while paused. Idempotent with the Worker's own
            # requeue on resume (jobs are deduplicated per sample).
            import pipeline
            counts = pipeline.requeue_on_start(db=db)
            log.info("hub: starting paused; requeued %s", counts)
        if not paused:
            try:
                exporter.start(export_interval)
            except BaseException:
                worker.stop()
                raise
        rt = _running = HubRuntime(data, db, worker, exporter, None, notifier,
                                   export_interval=export_interval,
                                   maintenance_enabled=maintenance,
                                   maintenance_interval=maintenance_interval,
                                   conf_fn=conf_fn, paused=bool(paused))
    _share_exporter(exporter)
    # The seed reads the corrections file (often on the share, which can
    # stall), so it runs outside the lock; the Worker is already up and gc1's
    # samples simply wait until it succeeds (the seed queues them).
    seeded_at = datetime.now()
    try:
        seed_gc1_corrections(app_conf, db, notifier)
    except Exception:  # noqa: BLE001 - Maintenance retries it
        log.exception("hub: gc1 corrections seed failed")
    if maintenance:
        with rt._pause_lock:
            rt.maintenance = Maintenance(db, data, notifier=notifier, conf_fn=conf_fn,
                                         seed_attempted_at=seeded_at)
            if not rt.paused:
                rt.maintenance.start(maintenance_interval)
    log.info("hub: started (data %s)%s", data,
             " with processing PAUSED (resume it from the hub tray or the admin API)"
             if rt.paused else "")
    return rt


_recovery_lock = threading.Lock()
_recovered_dirs: set = set()


def _dir_key(data_dir) -> str:
    return os.path.normcase(str(Path(data_dir).resolve()))


def startup_recovered(data_dir) -> bool:
    """Whether this process has run the start-up recovery for ``data_dir``
    (``hub_admin`` refuses admin jobs until then)."""
    with _recovery_lock:
        return _dir_key(data_dir) in _recovered_dirs


def forget_startup_recovery(data_dir) -> None:
    """Tests only: as if this process had not recovered ``data_dir`` yet."""
    with _recovery_lock:
        _recovered_dirs.discard(_dir_key(data_dir))


def run_startup_recovery(db, data_dir, notifier: Optional[Notifier]) -> Optional[dict]:
    """``recover_purges`` + ``mark_interrupted_imports``, once per process and
    data folder: ``hub.start`` runs again on a retry or after a failed
    respawn, while jobs may be running, and must never recover them. Returns
    ``None`` when it had already run."""
    with _recovery_lock:
        key = _dir_key(data_dir)
        if key in _recovered_dirs:
            return None
        out = {"purges": recover_purges(db, data_dir, notifier),
               "imports": mark_interrupted_imports(db, data_dir, notifier)}
        _recovered_dirs.add(key)
        return out


def _live_job_instruments(kinds: tuple) -> set:
    """Instruments an admin job of ``kinds`` running in this process owns."""
    try:
        import hub_admin
        job = hub_admin.JOBS.current()
    except Exception:  # noqa: BLE001
        return set()
    if job is None or job.get("state") != "running" or job.get("kind") not in kinds:
        return set()
    inst = (job.get("params") or {}).get("instrument")
    return {inst} if inst else {"*"}


def recover_purges(db, data_dir, notifier: Optional[Notifier]) -> list:
    """``purge.recover`` (interrupted purges; v3.1), skipping any purge live in
    this process (its instrument is paused, or an admin purge job owns it). A
    failure is logged and notified, never a failed start: the hub still
    serves, and the next start tries again (every recovery step is idempotent)."""
    try:
        import purge
        return purge.recover(db=db, data_dir=data_dir, notifier=notifier,
                             skip_instruments=_live_job_instruments(("purge",)))
    except Exception as exc:  # noqa: BLE001
        log.exception("hub: recovering an interrupted purge failed")
        _notify(notifier, "error", f"An interrupted purge could not be finished at start-up "
                                   f"({exc}); it is retried at the next start. See app.log and "
                                   f"the purged folder's manifest.json.")
        return []


def mark_interrupted_imports(db, data_dir, notifier: Optional[Notifier]) -> list:
    """``jobs.import_history.mark_interrupted``: a history import the process
    died in is marked interrupted and announced (never a failed start)."""
    try:
        from jobs import import_history
        return import_history.mark_interrupted(
            db=db, data_dir=data_dir, notifier=notifier,
            skip_instruments=_live_job_instruments(("import-history",)))
    except Exception:  # noqa: BLE001
        log.exception("hub: marking interrupted history imports failed")
        return []


# ── Processing paused (persisted) ─────────────────────────────────────────
# The hub tray's "Pause processing" (hub_control) keeps the web app serving
# and ingest accepting while the Worker, exporter and maintenance are
# stopped. The choice outlives a restart: ``start`` reads it.

PROCESSING_PAUSED_KEY = "hub_processing_paused"


def processing_paused(db) -> Optional[dict]:
    """``{"since", "by"}`` while processing is paused, else None."""
    return parse_processing_paused(store.settings_kv.get(PROCESSING_PAUSED_KEY, db=db))


def parse_processing_paused(raw) -> Optional[dict]:
    """``processing_paused`` from the stored value (hub_control reads the
    row itself, with a short timeout)."""
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except ValueError:
        val = None
    if not isinstance(val, dict):
        val = {}
    return {"since": val.get("since"), "by": val.get("by")}


def set_processing_paused(db, paused: bool, *, by: Optional[str] = None) -> None:
    """Persist (``paused``) or clear the flag ``start`` honours."""
    if paused:
        store.settings_kv.set(PROCESSING_PAUSED_KEY, json.dumps({
            "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "by": by}), db=db)
    else:
        store.settings_kv.delete(PROCESSING_PAUSED_KEY, db=db)


START_BACKOFF = (5, 10, 20, 40, 80, 160, 300)     # seconds; then every 300 s
START_NOTIFY_AFTER = 3                             # consecutive failures


def start_with_retry(start_fn: Callable[[], Any], *, sleep: Callable[[float], Any] = time.sleep,
                     notifier: Optional[Notifier] = None,
                     stop: Optional[threading.Event] = None):
    """Call ``start_fn()`` until it succeeds, waiting ``START_BACKOFF`` between
    tries (5 s doubling to 5 min, then every 5 min). One error notification
    after ``START_NOTIFY_AFTER`` consecutive failures and one info when it then
    starts; nothing for a blip. Returns ``start_fn``'s result, or None when
    ``stop`` is set. Meanwhile ingest keeps accepting: ``submit`` only needs
    the store, and the queued jobs run once the Worker starts."""
    failures = 0
    notified = False
    while True:
        if stop is not None and stop.is_set():
            return None
        try:
            result = start_fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log.exception("hub: start failed (attempt %d)", failures)
            if failures >= START_NOTIFY_AFTER and not notified:
                notified = True
                _notify(notifier, "error", f"The GC hub has not started after {failures} "
                                           f"attempts: {exc}. Nothing is processed or exported "
                                           f"until it starts; it keeps retrying every few "
                                           f"minutes. See app.log.")
            sleep(START_BACKOFF[min(failures, len(START_BACKOFF)) - 1])
            continue
        if notified:
            _notify(notifier, "info", f"The GC hub started after {failures + 1} attempts.")
        return result


def background_busy(db, *, now: Optional[datetime] = None) -> bool:
    """True while the Worker has work now: a job ``running``, or ``queued``
    and due (``not_before`` unset or passed). A retry scheduled later (e.g.
    ``pending_corrections`` every 5 minutes) is not work now. False when the
    store can't be read (never blocks a restart on an error). While
    processing is paused, due jobs cannot run, so only a ``running`` one
    counts (a paused hub must not skip its 3 AM restart forever)."""
    stamp = store._ts((now or datetime.now()).astimezone())
    try:
        paused = processing_paused(db) is not None
        with store.connection(db) as conn:
            if paused:
                r = conn.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone()
            else:
                r = conn.execute(
                    "SELECT 1 FROM jobs WHERE state='running' OR (state='queued' AND "
                    "(not_before IS NULL OR not_before <= ?)) LIMIT 1", (stamp,)).fetchone()
    except Exception:  # noqa: BLE001
        log.exception("hub: could not read the job queue")
        return False
    return r is not None
