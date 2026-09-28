"""Instruments: the per-instrument processing context (phase 2, 2A1).

``context(instrument_row, global_conf)`` is ``InstrumentContext``: the global
settings (``settings.json``) with the instrument's calibration CDF,
assignments and sensitivity overlaid, plus ``instrument_id``. Every
``distill`` call that needs calibration gets this merged conf, never the
global one. ``GC_CAL_CDF`` plays no part: the context never reads it and the
hub computes with ``honour_env=False``.

The instrument row stores ``calibration_assignments`` as a plain JSON list
of the Calibration page's entries (``[{rt, carbon}|{rt, ignore}, ...]``) for
its own calibration CDF; the context re-keys it into distill's settings shape
(``{resolved cdf path: entries}``, ``distill.upsert_assignments``).

"Calibration usable" (spec, Status machine) means the calibration CDF exists
**and** it has at least two usable saved assignment pairs (the same pairs
``distill.anchors_for`` uses). The auto-detect fallback is off in the hub.

In 2A1 there is one instrument, ``gc1``, created once on first start by
``bootstrap_gc1`` from ``settings.json`` (calibration CDF, its assignments
and sensitivity copied; the default ``method_map``; ``live_since`` = now, so
only injections from then on export automatically, D11). After that the row
is the source of truth and ``settings.json``'s calibration keys are not read
again for processing.

``startup(app_conf, notifier, ...)`` is the hub's one start-up call (T4/T5
wire it): ``store.migrate`` → ``bootstrap_gc1`` → ``pipeline.Worker.start()``
(which sweeps ``cdf/.incoming`` leftovers and runs
``pipeline.requeue_on_start`` before its thread starts). It returns the
running Worker; call ``.stop()`` on shutdown.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import distill
import methods
import paths
import store

log = logging.getLogger("instruments")

GC1 = "gc1"
GC1_NAME = "GC-1"
DEFAULT_METHOD = "D2887"
DEFAULT_METHOD_MAP: Dict[str, str] = {"SIMDISB.M": "D2887", "SIMDISTB.M": "D2887"}


def method_map(instrument_row: dict) -> Dict[str, str]:
    """The row's ``method_map`` decoded, keys normalised (``methods.normalise_method_name``)
    and values upper-cased. ``{}`` when unset or unreadable."""
    raw = (instrument_row or {}).get("method_map")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            log.warning("instrument %s: unreadable method_map %r", instrument_row.get("id"), raw)
            return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name, hub_method in raw.items():
        key = methods.normalise_method_name(name)
        if key and hub_method:
            out[key] = str(hub_method).strip().upper()
    return out


def _entries(raw: Any, cal_cdf: Optional[Path]) -> list:
    """The row's assignments as a list of entries (a plain list; a distill-style
    map is accepted too and read for ``cal_cdf``)."""
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and cal_cdf is not None:
        found = raw.get(distill._cal_key(cal_cdf))
        if found is None:
            found = raw.get(str(cal_cdf))
        return found if isinstance(found, list) else []
    return []


def _calibration_path(raw: Optional[str], data_dir: Optional[Path]) -> Optional[Path]:
    text = (raw or "").strip()
    if not text:
        return None
    p = Path(text)
    if not p.is_absolute():
        base = Path(data_dir) if data_dir is not None else paths.data_dir()
        if base is not None:
            p = Path(base) / p
    return p


def context(instrument_row: dict, global_conf: Dict[str, str], *,
            data_dir: Optional[Path] = None) -> Dict[str, str]:
    """The merged conf for computing this instrument's samples (a new dict).

    ``calibration_cdf`` is the row's (a relative path is resolved against
    ``data_dir``, default ``paths.data_dir()``), ``''`` if unset;
    ``calibration_assignments`` the row's entries re-keyed for that CDF
    (``''`` if none); ``calibration_sensitivity`` the row's, as text;
    ``instrument_id`` the row's id. Everything else comes from
    ``global_conf``.
    """
    row = instrument_row or {}
    conf = dict(global_conf or {})
    cal = _calibration_path(row.get("calibration_cdf"), data_dir)
    entries = _entries(row.get("calibration_assignments"), cal)
    conf["calibration_cdf"] = str(cal) if cal is not None else ""
    conf["calibration_assignments"] = (distill.upsert_assignments("", cal, entries)
                                       if cal is not None and entries else "")
    sens = row.get("calibration_sensitivity")
    conf["calibration_sensitivity"] = str(float(sens) if sens is not None else 50.0)
    conf["instrument_id"] = str(row.get("id") or "")
    return conf


def calibration_problem(ctx: Dict[str, str]) -> Optional[str]:
    """Why the context's calibration is unusable, or ``None`` if it is usable."""
    raw = (ctx.get("calibration_cdf") or "").strip()
    if not raw:
        return "No calibration CDF is set for this instrument."
    cal = Path(raw)
    if not cal.is_file():
        return f"Calibration CDF not found: {cal}"
    amap = distill.parse_assignment_map(ctx.get("calibration_assignments", ""))
    pairs = distill._assignment_pairs(amap, cal)
    if len(pairs) < 2:
        return (f"Calibration {cal.name} has {len(pairs)} usable peak assignment(s); "
                f"at least 2 are needed (assign the peaks on the Calibration page).")
    return None


def calibration_usable(ctx: Dict[str, str]) -> bool:
    """The calibration CDF exists and has at least two usable assignment pairs."""
    return calibration_problem(ctx) is None


def bootstrap_gc1(global_conf: Dict[str, str], *, db: store.Db = None,
                  now: Optional[datetime] = None) -> dict:
    """Create the ``gc1`` row from ``settings.json`` if it doesn't exist; return the row.

    Copies ``calibration_cdf``, its saved assignments (as a plain list) and
    ``calibration_sensitivity``; sets the default ``method_map`` and
    ``live_since`` = ``now`` (local, naive; default the current time). An
    existing row is returned untouched.
    """
    conf = global_conf or {}
    with store.connection(db) as conn:
        with store.write_txn(conn):
            existing = store.instruments.get(GC1, db=conn)
            if existing is not None:
                return existing
            cal = _calibration_path(conf.get("calibration_cdf"), None)
            entries = _entries(conf.get("calibration_assignments", ""), cal) if cal else []
            if not isinstance(entries, list):
                entries = []
            try:
                sens = float(conf.get("calibration_sensitivity") or 50)
            except (TypeError, ValueError):
                sens = 50.0
            when = (now or datetime.now()).replace(microsecond=0)
            row = store.instruments.upsert({
                "id": GC1, "name": GC1_NAME, "method": DEFAULT_METHOD, "enabled": 1,
                "calibration_cdf": str(cal) if cal is not None else None,
                "calibration_assignments": json.dumps(entries) if entries else None,
                "calibration_sensitivity": sens,
                "method_map": json.dumps(DEFAULT_METHOD_MAP),
                "live_since": when,
            }, db=conn)
    log.info("instruments: created %s from settings.json (live since %s)", GC1, row["live_since"])
    return row


def startup(app_conf: Dict[str, str], notifier=None, *, db: store.Db = None,
            data_dir: Optional[Path] = None, conf_fn=None, **worker_kw):
    """Start the hub's processing: migrate the store, create ``gc1`` from
    ``app_conf`` (``settings.json``) if needed, then start the one
    ``pipeline.Worker`` (sweep ``.incoming``, requeue, thread) and return it.

    ``notifier(level, message)`` receives the pipeline's notifications (e.g.
    ``notifications.get_store().add``). ``db`` defaults to
    ``<data_dir>/gc.db`` and ``data_dir`` to ``paths.data_dir()``;
    ``conf_fn`` (default ``settings.load_settings``) supplies the global
    settings per job. ``format_line`` defaults to ``exports.format_line``
    (the frozen v1 export line). Other keywords go to ``pipeline.Worker``.
    """
    import exports
    import pipeline  # deferred: pipeline imports this module
    worker_kw.setdefault("format_line", exports.format_line)
    data = Path(data_dir) if data_dir is not None else paths.data_dir()
    if data is None:
        raise RuntimeError(f"{paths.DATA_ENV} is not set; the hub needs a data folder")
    db = db if db is not None else data / store.DB_FILENAME
    if not hasattr(db, "execute"):
        store.migrate(db)
    bootstrap_gc1(app_conf, db=db)
    worker = pipeline.Worker(db=db, data_dir=data, conf_fn=conf_fn, notifier=notifier, **worker_kw)
    worker.start()
    return worker
