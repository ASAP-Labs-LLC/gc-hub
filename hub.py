"""hub.py: the hub's one start-up (phase 2, 2A1 T5; integration carry-over I4).

``start(app_conf)`` owns everything that runs in the background, in this
order, and ``HubRuntime.stop()`` undoes it:

1. ``store.migrate`` → ``instruments.bootstrap_gc1`` → ``seed_gc1_corrections``
   (first start: gc1's eleven factors from the phase-1 file into the hub's
   ``instrument_corrections``) → ``instruments.startup``, which starts the one
   ``pipeline.Worker`` (sweeps ``cdf/.incoming``, requeues, starts its
   thread). The Worker's ``notifier`` is the notification store's ``add``,
   its corrections are the hub's own (``corrections_provider(db)``) and its
   ``on_final`` wakes the exporter (and calls the caller's ``on_final``,
   which the app uses to refresh ``sample_cache`` for that sample).
2. ``exports.HubExporter(notifier).start()``: appends every instrument's
   pending ledger rows to its export file, every ``export_interval``
   seconds and at once after a result becomes final (``wake_exports``).
3. ``Maintenance``: a thread that, every ``maintenance_interval`` seconds,
   writes the nightly ``store.backup_nightly`` (once per local day, from
   ``BACKUP_HOUR``; the day's file is the record, so a restart doesn't
   back up twice) and deletes ``done``/``superseded`` jobs older than
   ``PRUNE_DAYS`` (``store.jobs.prune_done``, once per day). A failed
   backup is notified once and retried after ``BACKUP_RETRY``.

One runtime per process (``running()``); the app calls ``start`` from
``_init_app`` and ``stop`` at exit. ``GC_DATA_DIR`` is required: without it
``paths.DataDirMissing`` is raised (app.py refuses to start before that).

Corrections are hub-owned (D4b): the Worker reads only
``instrument_corrections`` (``StoreProvider``, source ``hub``). gc1 is seeded
once from the phase-1 file; a missing or invalid file is notified and gc1
stays ``pending_corrections`` (never silent zeros); every other instrument
waits until its eleven values are entered on the Instruments page.

The running exporter is handed to ``instruments_api.set_exporter`` (2A2) and
``hub_admin.set_exporter`` when those modules exist, for the admin export
actions (their refusal state lives in the exporter).
"""
from __future__ import annotations

import functools
import logging
import threading
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


SEED_REASON = "seeded from correction_factors.json"
SEED_BY = "startup"


def seed_gc1_corrections(app_conf: dict, db, notifier: Optional[Notifier] = None) -> bool:
    """Give ``gc1`` its corrections from the phase-1 file (``settings.json``'s
    ``correction_factors_json``) if the hub has none for it yet. Returns True
    when it seeded. An existing hub row is never touched. A missing, unreadable
    or invalid file (``corrections.seed_from_file`` raises, never zeros) is
    logged and notified, and gc1 stays ``pending_corrections`` until its
    values are entered (or the file is fixed and the hub restarted)."""
    if store.corrections.read(instruments.GC1, db=db) is not None:
        return False
    path = str((app_conf or {}).get("correction_factors_json") or "").strip()
    try:
        if not path:
            raise corrections.CorrectionsUnavailable("correction_factors_json is not set")
        values = corrections.seed_from_file(path)
        with store.connection(db) as conn, store.write_txn(conn):
            if store.corrections.read(instruments.GC1, db=conn) is not None:
                return False
            store.corrections.set_all(conn, instruments.GC1, values, by=SEED_BY, reason=SEED_REASON)
    except corrections.CorrectionsUnavailable as exc:
        msg = (f"GC-1 has no correction factors: they could not be seeded from the phase-1 "
               f"file ({exc}). GC-1 samples wait as pending_corrections until the factors "
               f"are entered on the Instruments page (or the file is fixed and the hub "
               f"restarted).")
        log.error("%s", msg)
        _notify(notifier, "error", msg)
        return False
    msg = (f"GC-1 correction factors were seeded from {path}. Check them on the Instruments "
           f"page; never also enter GC factors in LEM.")
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
    """The nightly backup and the job-table prune (``run_once`` is one pass)."""

    def __init__(self, db, data_dir, *, notifier: Optional[Notifier] = None,
                 keep: int = BACKUP_KEEP) -> None:
        self.db = Path(db)
        self.data_dir = Path(data_dir)
        self.notifier = notifier
        self.keep = keep
        self._failed_at: Optional[datetime] = None
        self._notified_day: Optional[str] = None
        self._pruned_day: Optional[str] = None
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
        try:
            n = store.jobs.prune_done(cutoff, db=self.db)
        except Exception:  # noqa: BLE001
            log.exception("hub: pruning finished jobs failed")
            return None
        self._pruned_day = day
        if n:
            log.info("hub: pruned %d finished job(s) older than %d days", n, PRUNE_DAYS)
        return n

    def run_once(self, now: Optional[datetime] = None) -> dict:
        """``now`` is local and naive (default the current time)."""
        now = now or datetime.now()
        return {"backup": self._backup(now), "pruned": self._prune(now)}

    def start(self, interval: float = MAINTENANCE_INTERVAL_SECONDS) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
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
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None


class HubRuntime:
    """What ``start`` started: ``worker``, ``exporter``, ``maintenance``."""

    def __init__(self, data_dir: Path, db: Path, worker, exporter: exports.HubExporter,
                 maintenance: Optional[Maintenance], notifier: Optional[Notifier]) -> None:
        self.data_dir = data_dir
        self.db = db
        self.worker = worker
        self.exporter = exporter
        self.maintenance = maintenance
        self.notifier = notifier

    def wake_exports(self) -> None:
        """Flush now: call after anything writes a ledger row outside the
        Worker (Export to LIMS, a backfill release)."""
        self.exporter.wake()

    def exporter_alive(self) -> bool:
        return self.exporter.is_alive()

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
          **worker_kw) -> HubRuntime:
    """Start the hub (see the module docstring) and return its runtime.

    ``app_conf`` is ``settings.json`` (default ``settings.load_settings()``),
    used to bootstrap ``gc1``; ``conf_fn`` supplies it per job (default
    ``settings.load_settings``). ``notifier`` defaults to the notification
    store's ``add`` (``None`` = log only). ``RuntimeError`` if a runtime is
    already running in this process.
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
        instruments.bootstrap_gc1(app_conf, db=db)
        seed_gc1_corrections(app_conf, db, notifier)
        exporter = exports.HubExporter(db, data_dir=data, notifier=notifier)

        def final_hook(sample_id: int) -> None:
            exporter.wake()
            if on_final is not None:
                on_final(sample_id)

        # 2A2's instruments.corrections_provider (hub corrections; gc1's file
        # only until seeded, which the line above just did), else the hub's own.
        provider_for = getattr(instruments, "corrections_provider", None) or corrections_provider
        worker_kw.setdefault("corrections_provider", provider_for(db))
        worker = instruments.startup(app_conf, notifier, db=db, data_dir=data, conf_fn=conf_fn,
                                     on_final=final_hook, **worker_kw)
        try:
            exporter.start(export_interval)
            maint = None
            if maintenance:
                maint = Maintenance(db, data, notifier=notifier)
                maint.start(maintenance_interval)
        except BaseException:
            worker.stop()
            exporter.stop()
            raise
        _running = HubRuntime(data, db, worker, exporter, maint, notifier)
    _share_exporter(exporter)
    log.info("hub: started (data %s)", data)
    return _running
