# looker.py

"""looker.py
~~~~~~~~~~~~
Daemon thread: watches *watch_dir* for fresh *.CDF files, copies them
into *processed_dir*, then calls distill.process_cdf() on each copy.

A new `scan_now()` helper lets the GUI trigger a single, synchronous
scan without starting the background thread.
"""
from __future__ import annotations

import csv
import re
import logging
import shutil
import time
import json
from concurrent.futures import (
    ThreadPoolExecutor,
    Future,
    CancelledError,
    FIRST_COMPLETED,
    wait,
)
from datetime import datetime
from pathlib import Path
from threading import Event, Thread, Lock
from typing import Sequence

import distill  # needs process_cdf() and cdf_metadata()
import paths  # stdlib-only; where every on-disk state location lives

LOGGER = logging.getLogger("looker")


class Looker(Thread):
    def __init__(
        self,
        watch_dir: Path,
        processed_dir: Path,
        poll: float = 2.0,
        blank_cache: Path | None = None,
        max_workers: int = 4,
    ) -> None:
        super().__init__(daemon=True)
        self.watch_dir = watch_dir
        self.processed_dir = processed_dir
        self.poll = poll
        self.blank_cache = blank_cache or processed_dir / ".blank_cache.json"
        self.blank_cache.parent.mkdir(parents=True, exist_ok=True)
        self._latest_blank_path: Path | None = None
        self.max_workers = max(1, int(max_workers) if max_workers else 1)

        # signal for both run-loop & in-flight scans
        self._stop_event = Event()
        self._seen: set[Path] = set()
        self._state_lock = Lock()
        self._pending_lock = Lock()
        self._executor: ThreadPoolExecutor | None = ThreadPoolExecutor(max_workers=self.max_workers)
        self._pending: set[Future] = set()

        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self._index: set[tuple[str, str]] = set()  # (sample, str(date))
        self._index_cache = self.processed_dir / ".processed_index.json"
        # Try fast path: load cached index; fallback to slow scan once
        self._load_index_cache()
        if not self._index:
            self._build_index()
            self._save_index_cache()
        self._load_blank_cache()

    def _ensure_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self.max_workers)
        return self._executor

    def _on_future_done(self, fut: Future) -> None:
        try:
            exc = fut.exception()
        except CancelledError:
            exc = None
        if exc:
            LOGGER.error("CDF processing task failed", exc_info=(type(exc), exc, exc.__traceback__))
        with self._pending_lock:
            self._pending.discard(fut)

    def _shutdown_executor(self, *, wait_for_tasks: bool) -> None:
        executor = self._executor
        if executor is None:
            return
        with self._pending_lock:
            pending = list(self._pending)
            self._pending.clear()
        for fut in pending:
            fut.cancel()
        executor.shutdown(wait=wait_for_tasks)
        self._executor = None

    def _submit_candidate(self, src: Path) -> Future | None:
        if self._stop_event.is_set():
            return None
        executor = self._ensure_executor()
        fut = executor.submit(self._handle_new, src)
        with self._pending_lock:
            self._pending.add(fut)
        fut.add_done_callback(self._on_future_done)
        return fut

    def _wait_for_futures(self, futures: list[Future]) -> None:
        pending = {f for f in futures if f is not None}
        while pending:
            done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            if not pending:
                break
            if self._stop_event.is_set():
                for fut in list(pending):
                    fut.cancel()
                break
        if pending:
            wait(pending)

    # ------------------------------------------------------------------ #
    # Public control
    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        """Signal any in-progress scan (or the background run-loop) to exit ASAP."""
        self._stop_event.set()
        self._shutdown_executor(wait_for_tasks=False)

    def scan_now(self) -> None:
        """
        Run **one** scan in the caller’s thread.
        Clears any prior stop-signal first.
        """
        self._stop_event.clear()
        futures = self._scan_once()
        self._wait_for_futures(futures)

    def update_paths(self, watch_dir: Path, processed_dir: Path) -> None:
        """Change watched directories while running."""
        self._shutdown_executor(wait_for_tasks=False)
        self.watch_dir = watch_dir
        self.processed_dir = processed_dir
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self._index_cache = self.processed_dir / ".processed_index.json"
        self._index.clear()
        self._build_index()
        self._save_index_cache()
        self._seen.clear()
        self.blank_cache = processed_dir / ".blank_cache.json"
        self.blank_cache.parent.mkdir(parents=True, exist_ok=True)
        self._load_blank_cache()
        LOGGER.info("Updated paths → watch=%s processed=%s", watch_dir, processed_dir)

    # ------------------------------------------------------------------ #
    # Thread run-loop
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        LOGGER.info("Watching %s …", self.watch_dir)
        while not self._stop_event.is_set():
            self._scan_once()
            LOGGER.debug("Sleeping %.2fs", self.poll)
            time.sleep(self.poll)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _scan_once(self) -> list[Future]:
        LOGGER.debug("Scanning %s recursively", self.watch_dir)
        futures: list[Future] = []
        try:
            candidates = [
                fp
                for fp in self.watch_dir.rglob("*.cdf")
                if fp.is_file() and fp not in self._seen
            ]
        except Exception as exc:
            LOGGER.error("Unable to list %s: %s", self.watch_dir, exc)
            return futures

        candidates.sort(key=lambda p: p.stat().st_mtime)

        for fp in candidates:
            if self._stop_event.is_set():
                LOGGER.info("Scan aborted by user")
                break

            fut = self._submit_candidate(fp)
            if fut is not None:
                futures.append(fut)
            self._seen.add(fp)

        return futures

    def _handle_new(self, src: Path) -> None:
        if self._stop_event.is_set():
            LOGGER.debug("Stop requested before starting %s", src.name)
            return

        LOGGER.info("New CDF: %s", src.name)
        try:
            sample, date = distill.cdf_metadata(src)
        except Exception:
            LOGGER.exception("Metadata read failed for %s", src)
            return
        LOGGER.debug("Metadata sample=%s date=%s", sample, date)

        key = (sample, str(date))
        with self._state_lock:
            if key in self._index:
                if self._distillation_row_exists(sample, date):
                    LOGGER.info("Already processed %s %s", sample, date)
                    return
                LOGGER.warning(
                    "Index entry found for %s %s but distillation row missing; forcing reprocess",
                    sample,
                    date,
                )
                self._index.discard(key)
                self._save_index_cache()
            blank_reference = self._latest_blank_path

        safe_sample = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in sample
        ).strip("_") or src.stem
        safe_date = str(date).replace(':', '-').replace(' ', '_')
        dest = self.processed_dir / f"{safe_sample}_{safe_date}.CDF"
        LOGGER.debug("Destination path %s", dest)

        try:
            shutil.copy2(src, dest)
            LOGGER.debug("Copied → %s", dest)
        except Exception:
            LOGGER.exception("Copy failed")
            return

        if self._stop_event.is_set():
            LOGGER.info("Processing aborted by user before distillation %s", dest.name)
            return

        is_blank = self._is_blank_sample(sample)
        blank_path = None if is_blank else blank_reference

        try:
            processed = distill.process_cdf(dest, blank_path=blank_path)
        except Exception:
            LOGGER.exception("distill.process_cdf() failed")
            return

        LOGGER.debug("Processed %s", processed.name)
        with self._state_lock:
            if is_blank:
                self._register_blank(processed)
            self._index.add(key)
            self._save_index_cache()

    def _build_index(self) -> None:
        LOGGER.debug("Building processed file index from %s", self.processed_dir)
        if self._index:
            LOGGER.debug("Index already loaded from cache (%d entries)", len(self._index))
            return
        for fp in self.processed_dir.glob("*.CDF"):
            try:
                sample, date = distill.cdf_metadata(fp)
            except Exception as exc:
                LOGGER.debug("Metadata read failed for %s: %s", fp, exc)
                continue
            self._index.add((sample, str(date)))

    # ------------------------------------------------------------------ #
    # Index cache helpers
    # ------------------------------------------------------------------ #
    def _load_index_cache(self) -> None:
        if not self._index_cache.is_file():
            return
        try:
            import json
            with self._index_cache.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            entries = data.get("entries", []) if isinstance(data, dict) else data
            for pair in entries:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    self._index.add((str(pair[0]), str(pair[1])))
            LOGGER.info("Loaded processed index cache (%d entries)", len(self._index))
        except Exception as exc:
            LOGGER.warning("Failed to read index cache %s: %s", self._index_cache, exc)
            self._index.clear()

    def _save_index_cache(self) -> None:
        try:
            import json
            self._index_cache.parent.mkdir(parents=True, exist_ok=True)
            with self._index_cache.open("w", encoding="utf-8") as fh:
                json.dump({"entries": sorted(list(self._index))}, fh)
        except Exception as exc:
            LOGGER.debug("Failed to write index cache %s: %s", self._index_cache, exc)

    # ------------------------------------------------------------------ #
    # Blank cache helpers
    # ------------------------------------------------------------------ #
    def _load_blank_cache(self) -> None:
        if not self.blank_cache.is_file():
            # ensure directory exists to avoid later write errors
            self.blank_cache.parent.mkdir(parents=True, exist_ok=True)
            return
        try:
            with self.blank_cache.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            p = Path(data.get("path", ""))
            mtime = float(data.get("mtime", 0))
            if p.is_file() and abs(p.stat().st_mtime - mtime) < 1:
                if distill.is_plausible_blank(p):
                    self._latest_blank_path = p
                    LOGGER.info("Loaded blank cache %s", p)
                else:
                    LOGGER.warning("Ignoring cached blank %s: it carries sample signal", p.name)
        except Exception as exc:
            LOGGER.warning("Failed to read blank cache %s: %s", self.blank_cache, exc)

    def _save_blank_cache(self, path: Path) -> None:
        try:
            self.blank_cache.parent.mkdir(parents=True, exist_ok=True)
            with self.blank_cache.open("w", encoding="utf-8") as fh:
                json.dump({"path": str(path), "mtime": path.stat().st_mtime}, fh)
        except Exception as exc:
            LOGGER.warning("Failed to write blank cache %s: %s", self.blank_cache, exc)

    # ------------------------------------------------------------------ #
    # Reprocess helpers
    # ------------------------------------------------------------------ #
    _BLANK_NAME = re.compile(r"^blank[\s_\-]*\d*$", re.IGNORECASE)

    @classmethod
    def _is_blank_sample(cls, sample: str) -> bool:
        # Exact names only ("Blank", "Blank2", "blank_3"), not any substring.
        return bool(cls._BLANK_NAME.match((sample or "").strip()))

    def _register_blank(self, path: Path) -> bool:
        """Make *path* the reference blank if it is genuine and the newest.

        Returns True when the cache was updated. A file merely *named* blank
        (e.g. a sequence line whose sample name was never changed) is refused
        by :func:`distill.is_plausible_blank`; an older genuine blank never
        replaces a newer one, whatever order files are (re)processed in.
        """
        path = Path(path)
        if not distill.is_plausible_blank(path):
            LOGGER.warning("Not registering %s as blank: it carries sample signal", path.name)
            return False
        try:
            _, cand_dt = distill.cdf_metadata(path)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Not registering %s as blank: %s", path.name, exc)
            return False
        cur = self._latest_blank_path
        if cur is not None and cur != path and Path(cur).is_file():
            try:
                _, cur_dt = distill.cdf_metadata(Path(cur))
                if cur_dt > cand_dt and distill.is_plausible_blank(Path(cur)):
                    LOGGER.info("Keeping newer blank %s over %s", Path(cur).name, path.name)
                    return False
            except Exception:  # noqa: BLE001
                pass
        self._latest_blank_path = path
        self._save_blank_cache(path)
        LOGGER.info("Reference blank is now %s (%s)", path.name, cand_dt)
        return True

    def _distillation_row_exists(self, sample: str, date: datetime) -> bool:
        """Return True if distillation CSV already has this sample/date row."""
        try:
            conf = distill.settings.load_settings()  # type: ignore[attr-defined]
        except Exception:
            return True  # fall back to skip check if settings not available
        csv_path = Path(conf.get("distill_output", str(paths.default_results_csv())))
        if not csv_path.exists():
            return False
        target_date = date.isoformat(sep=" ")
        try:
            with distill._CSV_LOCK:
                with csv_path.open("r", encoding="utf-8", newline="") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        if (row.get("Lab ID") or "").strip() != sample.strip():
                            continue
                        if (row.get("InjectionDateTime") or "").strip() == target_date:
                            return True
        except Exception as exc:
            LOGGER.debug("Failed to scan distillation CSV %s: %s", csv_path, exc)
            return True
        return False

    def _find_latest_sample_cdf(self, sample: str) -> tuple[Path, datetime] | None:
        """Locate the newest processed CDF for *sample*.

        Uses os.scandir (fast) and filename matching before falling back to
        the expensive cdf_metadata() NetCDF read — only opens CDFs whose
        filename contains the sample name.
        """
        import os as _os
        sample_key = sample.strip().lower()
        # Build a simplified match key from the sample name (alphanumeric only)
        match_key = "".join(c for c in sample_key if c.isalnum())
        best: tuple[Path, datetime] | None = None
        root = self.processed_dir
        if not root.exists():
            return None
        try:
            with _os.scandir(str(root)) as it:
                for entry in it:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if not entry.name.upper().endswith(".CDF"):
                        continue
                    # Quick filename pre-filter: skip files that clearly don't
                    # match the sample name (avoids expensive NetCDF reads)
                    fname_key = "".join(c for c in entry.name.lower() if c.isalnum())
                    if match_key and match_key not in fname_key:
                        continue
                    path = Path(entry.path)
                    try:
                        sample_name, inj_dt = distill.cdf_metadata(path)
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.debug("cdf_metadata failed for %s: %s", path, exc)
                        continue
                    if sample_name.strip().lower() != sample_key:
                        continue
                    if best is None or inj_dt > best[1]:
                        best = (path, inj_dt)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Failed to search %s for %s: %s", root, sample, exc)
        return best

    def rebuild_database(self, *, backup: bool = True) -> None:
        """Clear the distillation CSV and processed index so everything is re-scanned.

        If *backup* is True, the existing CSV is renamed with a timestamp suffix
        before deletion so data is not permanently lost.
        """
        try:
            conf = distill.settings.load_settings()  # type: ignore[attr-defined]
        except Exception:
            conf = {}
        csv_path = Path(conf.get("distill_output", str(paths.default_results_csv())))

        if csv_path.exists():
            if backup:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = csv_path.with_name(f"{csv_path.stem}_backup_{ts}{csv_path.suffix}")
                try:
                    shutil.copy2(csv_path, backup_path)
                    LOGGER.info("CSV backed up to %s", backup_path)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("Could not back up CSV: %s", exc)
            try:
                csv_path.unlink()
                LOGGER.info("Deleted distillation CSV %s", csv_path)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Could not delete CSV: %s", exc)

        # Clear the processed index so every CDF is treated as new
        with self._state_lock:
            self._index.clear()
        self._seen.clear()

        # Remove the on-disk index cache
        try:
            if self._index_cache.exists():
                self._index_cache.unlink()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Could not remove index cache: %s", exc)

        self._save_index_cache()
        LOGGER.info("Database rebuild prepared — ready for full re-scan")

    def reprocess_samples(
        self, sample_ids: Sequence[str], stop_event: Event | None = None
    ) -> dict[str, dict[str, str]]:
        """Force distillation re-run for selected samples (latest CDF per sample).

        Each sample is re-parsed and its row is appended to the distillation CSV.
        """
        cleaned = [s.strip() for s in sample_ids if s and s.strip()]
        if not cleaned:
            LOGGER.info("No samples provided for reprocess request")
            return {}

        total = len(cleaned)
        LOGGER.info("── Starting reprocess of %d sample(s) ──", total)
        results: dict[str, dict[str, str]] = {}

        for i, sample in enumerate(cleaned, start=1):
            if stop_event and stop_event.is_set():
                LOGGER.info("Reprocess cancelled by user (stopped before %s)", sample)
                results[sample] = {"status": "cancelled"}
                break

            LOGGER.info("[%d/%d] %s — searching for CDF in %s …", i, total, sample, self.processed_dir)

            match = self._find_latest_sample_cdf(sample)
            if match is None:
                LOGGER.warning("[%d/%d] %s — no CDF found, skipping", i, total, sample)
                results[sample] = {"status": "missing"}
                continue

            path, inj_dt = match
            blank_path = None if self._is_blank_sample(sample) else self._latest_blank_path
            if blank_path:
                LOGGER.info("[%d/%d] %s — CDF: %s  blank: %s", i, total, sample, path.name, blank_path.name)
            else:
                LOGGER.info("[%d/%d] %s — CDF: %s  (no blank)", i, total, sample, path.name)

            LOGGER.info("[%d/%d] %s — running distillation + CSV export …", i, total, sample)
            try:
                processed_path = distill.process_cdf(path, blank_path=blank_path, reprocess=True)
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("[%d/%d] %s — FAILED: %s", i, total, sample, exc)
                results[sample] = {
                    "status": "error",
                    "error": str(exc),
                    "path": str(path),
                }
                continue

            LOGGER.info("[%d/%d] %s — done  ✓  (output: %s)", i, total, sample, processed_path.name)
            with self._state_lock:
                self._index.add((sample, str(inj_dt)))
                self._save_index_cache()
                if self._is_blank_sample(sample):
                    self._register_blank(processed_path)
            results[sample] = {"status": "ok", "path": str(processed_path)}

        ok = sum(1 for r in results.values() if r.get("status") == "ok")
        skipped = sum(1 for r in results.values() if r.get("status") == "missing")
        errors = sum(1 for r in results.values() if r.get("status") == "error")
        LOGGER.info("── Reprocess complete: %d OK  |  %d skipped  |  %d errors ──", ok, skipped, errors)
        return results

    def reprocess_paths(
        self, cdf_paths: Sequence[str], stop_event: Event | None = None
    ) -> dict[str, dict[str, str]]:
        """Force a distillation re-run for *specific* CDF files.

        Unlike :meth:`reprocess_samples` (which resolves a name to the newest
        matching CDF), this reprocesses the exact file given. Required for
        samples that share a Lab ID — e.g. daily QC — so the run the user
        selected is the one updated, not merely the most recent of that name.
        Blank selection mirrors ``reprocess_samples`` so the numbers match.
        """
        cleaned = [p.strip() for p in cdf_paths if p and p.strip()]
        if not cleaned:
            LOGGER.info("No paths provided for reprocess request")
            return {}

        total = len(cleaned)
        LOGGER.info("── Starting reprocess of %d file(s) by exact path ──", total)
        results: dict[str, dict[str, str]] = {}

        for i, raw in enumerate(cleaned, start=1):
            if stop_event and stop_event.is_set():
                LOGGER.info("Reprocess cancelled by user (stopped before %s)", raw)
                results[raw] = {"status": "cancelled"}
                break

            path = Path(raw)
            if not path.is_file():
                LOGGER.warning("[%d/%d] %s — file not found, skipping", i, total, raw)
                results[raw] = {"status": "missing"}
                continue

            try:
                sample_name, inj_dt = distill.cdf_metadata(path)
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("[%d/%d] %s — metadata read failed: %s", i, total, raw, exc)
                results[raw] = {"status": "error", "error": str(exc), "path": raw}
                continue

            blank_path = None if self._is_blank_sample(sample_name) else self._latest_blank_path
            LOGGER.info("[%d/%d] %s — reprocessing exact file %s (blank: %s)",
                        i, total, sample_name, path.name,
                        blank_path.name if blank_path else "none")
            try:
                processed_path = distill.process_cdf(path, blank_path=blank_path, reprocess=True)
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("[%d/%d] %s — FAILED: %s", i, total, sample_name, exc)
                results[raw] = {"status": "error", "error": str(exc), "path": raw}
                continue

            with self._state_lock:
                self._index.add((sample_name, str(inj_dt)))
                self._save_index_cache()
                if self._is_blank_sample(sample_name):
                    self._register_blank(processed_path)
            results[raw] = {"status": "ok", "path": str(processed_path)}

        ok = sum(1 for r in results.values() if r.get("status") == "ok")
        LOGGER.info("── Reprocess-by-path complete: %d OK ──", ok)
        return results
