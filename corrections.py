"""D86 correction factors for the hub: where they come from, and when to refuse.

Contract: ``docs/superpowers/specs/2026-09-28-phase2-contracts.md`` §2.
Rules: the design's "LEM corrections (2C)".

A correction is added to a reported D86 temperature, so "no correction" is a
claim about the result, never a default for "could not find out". Every
provider here either returns a value for every cut in its map (0.0 only where
the source says zero) or raises :class:`CorrectionsUnavailable`, which the
pipeline turns into ``pending_corrections``. A cut the map does not name is
absent from ``values`` and the pipeline leaves it uncorrected (+0.0).

Times are timezone-aware UTC inside this module and are written as ISO text
with an offset, so a DST change can never make a valid cache look "future".

Stdlib only at import time (``requests`` is imported when the default HTTP
getter is first used).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Protocol
from urllib.parse import quote

LOGGER = logging.getLogger(__name__)

D86_CUTS = ["IBP", "5%", "10%", "20%", "30%", "50%", "70%", "80%", "90%", "95%", "FBP"]

# LEM method name -> D86 cut: the five methods production LEM maps on Agilent
# GC 1 and GC 2 (lem_machine_config, read 2026-09-28). The default for an
# instrument whose `correction_map` is NULL.
_LEM_METHOD = "ASTM D2887/D86 - Distillation in Petroleum Products, {}"
DEFAULT_LEM_CORRECTION_MAP: Dict[str, str] = {
    _LEM_METHOD.format("IBP"): "IBP",
    _LEM_METHOD.format("10% Recovery"): "10%",
    _LEM_METHOD.format("50% Recovery"): "50%",
    _LEM_METHOD.format("90% Recovery"): "90%",
    _LEM_METHOD.format("FBP"): "FBP",
}

# The phase-1 corrections file's test names -> D86 cut. Used ONLY by
# FileProvider. Mirrors distill._D86_CORRECTION_TEST_MAP (a test pins them).
PHASE1_FILE_MAP: Dict[str, str] = {
    "IBP - D86": "IBP",
    "5% - D86": "5%",
    "10% - D86": "10%",
    "20% - D86": "20%",
    "30% - D86": "30%",
    "50% - D86": "50%",
    "70% - D86": "70%",
    "80% - D86": "80%",
    "90% - D86": "90%",
    "95% - D86": "95%",
    "FBP - D86": "FBP",
}

KINDS = ("unreachable", "config")


@dataclass(frozen=True)
class Corrections:
    source: str            # 'lem' | 'cache' | 'file'
    fetched_at: str        # ISO with UTC offset: when the values were fetched from their source
    values: dict           # cut -> float, for every cut the map names (explicit 0.0 allowed)
    stale_reason: str = ""  # source == 'cache' only: why the source was not used


class CorrectionsUnavailable(Exception):
    """No trustworthy corrections: the sample goes ``pending_corrections``.

    ``kind`` is ``'unreachable'`` (the source could not be asked; retried) or
    ``'config'`` (a human must act: unknown machine, unmapped test, bad
    units, bad map, bad file, wrong LEM_URL).
    """

    def __init__(self, reason: str, kind: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"CorrectionsUnavailable kind must be one of {KINDS}, not {kind!r}")
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


class CorrectionsProvider(Protocol):
    def get(self, instrument: dict) -> Corrections: ...
    def refresh(self, instrument: dict) -> Corrections: ...
    def changed_since(self, instrument: dict, used: Corrections) -> bool: ...


def _config(reason: str) -> CorrectionsUnavailable:
    return CorrectionsUnavailable(reason, "config")


def _unreachable(reason: str) -> CorrectionsUnavailable:
    return CorrectionsUnavailable(reason, "unreachable")


# ── maps ─────────────────────────────────────────────────────────────────────

def parse_correction_map(raw) -> Dict[str, str]:
    """An instrument's ``correction_map`` (JSON text, or None/blank = the LEM
    default). Returns a fresh ``{test_name: cut}``. Anything doubtful raises
    ``config``: a repaired map applies a correction to the wrong temperature
    without anyone seeing it."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return dict(DEFAULT_LEM_CORRECTION_MAP)
    if not isinstance(raw, str):
        raise _config("The correction map must be stored as JSON text, not "
                      f"{type(raw).__name__}.")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise _config(f"The correction map is not valid JSON: {exc}.") from None
    if not isinstance(data, dict) or not data:
        raise _config("The correction map must be a non-empty JSON object of "
                      "LEM test name -> D86 cut.")
    out: Dict[str, str] = {}
    seen: Dict[str, str] = {}
    for name, cut in data.items():
        name = str(name).strip()
        if not name:
            raise _config("The correction map has a blank LEM test name.")
        if not isinstance(cut, str) or cut.strip() not in D86_CUTS:
            raise _config(f"The correction map sends {name!r} to {cut!r}, which is "
                          f"not a D86 cut ({', '.join(D86_CUTS)}).")
        cut = cut.strip()
        if cut in seen:
            raise _config(f"The correction map sends both {seen[cut]!r} and {name!r} "
                          f"to {cut}; one cut can take only one correction.")
        seen[cut] = name
        out[name] = cut
    return out


def map_key(mapping: Dict[str, str]) -> str:
    """A stable fingerprint of an effective map (order-free)."""
    canonical = json.dumps(sorted(mapping.items()), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── shared helpers ───────────────────────────────────────────────────────────

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    """Aware UTC. A naive datetime is taken as local time (as phase 1 wrote)."""
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _as_utc(dt).isoformat(timespec="seconds")


def _parse_iso(text) -> Optional[datetime]:
    try:
        return _as_utc(datetime.fromisoformat(str(text)))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _age(then: Optional[datetime], now: datetime) -> Optional[float]:
    return None if then is None else (_as_utc(now) - then).total_seconds()


def _finite_float(value) -> Optional[float]:
    """A real, finite number, or None. Booleans are not numbers here."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def values_differ(a: dict, b: dict) -> bool:
    """True if two cut->value maps would correct any result differently.
    A cut missing from either counts as 0.0, which is how the pipeline
    applies it."""
    a, b = dict(a or {}), dict(b or {})
    for cut in set(a) | set(b):
        x, y = _finite_float(a.get(cut, 0.0)), _finite_float(b.get(cut, 0.0))
        if x is None or y is None or abs(x - y) > 1e-9:
            return True
    return False


def _in_cut_order(values: Dict[str, float]) -> Dict[str, float]:
    return {cut: values[cut] for cut in D86_CUTS if cut in values}


class _Locks:
    """One lock per instrument id, created on demand."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}

    def __call__(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())


# ── the phase-1 file (2A1) ───────────────────────────────────────────────────

FILE_SECTION = "Agilent GC"
FILE_INSTRUMENT_ID = "gc1"


class FileProvider:
    """Phase-1 ``correction_factors_json``: ``{"Agilent GC": {test_name:
    {"correction_value": x}, ...}, ...}``. Serves only instrument ``gc1`` and
    always reads the file's own names (:data:`PHASE1_FILE_MAP`); the
    instrument's ``correction_map`` is for LEM and is ignored here.

    Stricter than ``distill.load_d86_corrections`` in exactly the ways that
    function hid a failure as "no correction": a missing file, bad JSON, a
    missing/non-object section, a section with no mapped test, or a mapped
    test without a finite ``correction_value`` is ``config``; any other OS
    error (share offline, permissions) is ``unreachable``. Kept from phase 1:
    a cut the file does not list is uncorrected, recorded as 0.0.
    """

    def __init__(self, json_path: str, *, clock: Optional[Clock] = None) -> None:
        self.json_path = str(json_path)
        self._clock = clock or _utc_now
        self._latest: Dict[str, Corrections] = {}
        self._lock = threading.Lock()

    def get(self, instrument: dict) -> Corrections:
        inst_id = str((instrument or {}).get("id") or "")
        if inst_id != FILE_INSTRUMENT_ID:
            raise _config(f"The corrections file serves only instrument "
                          f"{FILE_INSTRUMENT_ID!r}, not {inst_id!r}; set up LEM "
                          f"corrections for it.")
        with self._lock:
            fetched_at = _iso(self._clock())
            section = self._read_section()
            values: Dict[str, float] = {}
            found = 0
            for test_name, cut in PHASE1_FILE_MAP.items():
                if test_name not in section:
                    values[cut] = 0.0
                    continue
                entry = section[test_name]
                number = (_finite_float(entry.get("correction_value"))
                          if isinstance(entry, dict) else None)
                if number is None:
                    raise _config(f"The corrections file's {test_name!r} entry has no "
                                  f"usable correction_value ({entry!r}).")
                values[cut] = number
                found += 1
            if not found:
                raise _config(f"The {FILE_SECTION!r} section of {self.json_path} lists "
                              f"none of the phase-1 test names; this is not the "
                              f"corrections file.")
            result = Corrections(source="file", fetched_at=fetched_at,
                                 values=_in_cut_order(values))
            self._latest[inst_id] = result
            return result

    def refresh(self, instrument: dict) -> Corrections:
        return self.get(instrument)

    def changed_since(self, instrument: dict, used: Corrections) -> bool:
        latest = self._latest.get(str((instrument or {}).get("id") or ""))
        return latest is not None and values_differ(latest.values, used.values)

    def _read_section(self) -> dict:
        try:
            with open(self.json_path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            raise _config(f"The corrections file {self.json_path} does not exist.") from None
        except OSError as exc:
            raise _unreachable(f"The corrections file {self.json_path} could not be "
                               f"read: {exc}.") from None
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise _config(f"The corrections file {self.json_path} is not valid "
                          f"JSON: {exc}.") from None
        if not isinstance(data, dict):
            raise _config(f"The corrections file {self.json_path} is not a JSON object.")
        section = data.get(FILE_SECTION)
        if not isinstance(section, dict):
            raise _config(f"The corrections file {self.json_path} has no "
                          f"{FILE_SECTION!r} section.")
        return section


# ── the cache store ──────────────────────────────────────────────────────────
#
# ``save(instrument_id, values, methods, fetched_at, lem_machine_uid, map_key)``
# and ``load(instrument_id) -> dict | None`` returning ``{"values": {cut:
# float}, "methods": [str], "fetched_at": str, "lem_machine_uid": str,
# "map_key": str}``. Only a successful LEM fetch is saved. An entry is used
# only for the same LEM machine under the same map; one without those keys is
# treated as no cache.


class MemoryCacheStore:
    """In-memory cache store (tests, and anywhere persistence is not wanted)."""

    def __init__(self) -> None:
        self._rows: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def load(self, instrument_id: str) -> Optional[dict]:
        with self._lock:
            row = self._rows.get(str(instrument_id))
            if row is None:
                return None
            return dict(row, values=dict(row["values"]), methods=list(row["methods"]))

    def save(self, instrument_id: str, values: dict, methods: list, fetched_at: str,
             lem_machine_uid: str, map_key: str) -> None:
        with self._lock:
            self._rows[str(instrument_id)] = {
                "values": dict(values), "methods": list(methods),
                "fetched_at": str(fetched_at), "lem_machine_uid": str(lem_machine_uid),
                "map_key": str(map_key)}


# ── LEM (2C) ─────────────────────────────────────────────────────────────────

ACCEPTED_UNITS = ("°C", "C", "")
HttpGet = Callable[[str, float], tuple]


def requests_http_get(url: str, timeout: float) -> tuple:
    """The default ``http_get``: ``(status, content_type, body_bytes)``.
    Raises on a timeout or connection error (counted as unreachable)."""
    import requests  # deferred: keeps this module importable without it

    resp = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
    return resp.status_code, resp.headers.get("Content-Type", ""), resp.content


@dataclass
class _Attempt:
    at: datetime
    key: tuple                # (lem_machine_uid, map_key)
    outcome: object           # Corrections | CorrectionsUnavailable


class LemProvider:
    """Corrections from LEM: ``GET {lem_url}/api/machines/<uid>/corrections``.

    LEM answers ``200 {"corrections": [{"test_name", "correction", "units"}],
    "methods": [str]}`` (``LEM Web Server/web_app.py`` ``api_get_corrections``);
    an unknown uid is ``200`` with both lists empty, and a LabCore it cannot
    read is a 502/503 JSON body (``_labcore_unreadable``).

    - A 200 JSON answer is authoritative. The machine is known iff ``methods``
      is non-empty. Per mapped name: in ``corrections`` -> its value; in
      ``methods`` only -> 0.0; in neither -> ``config``. Units of a mapped
      test must be °C, C or blank. ``values`` holds only the mapped cuts.
    - Unreachable (falls back to a cache entry for the same machine and map,
      at most ``max_cache_age`` s old, returned with ``source='cache'`` and
      ``stale_reason``): a timeout or connection error, a non-JSON 200 (a
      sign-in page), 401/403 (a sign-in/proxy wall), 429, 5xx.
    - ``config`` (never the cache): any other status (404: wrong LEM_URL) or
      JSON that is not LEM's shape.
    - At most one fetch per ``fresh_seconds`` per instrument whatever the
      outcome. ``refresh()`` always fetches; if LEM cannot be asked it still
      answers from the cache, and ``source == 'cache'`` with ``stale_reason``
      is how the caller knows the refresh did not reach LEM.
    - Calls for one instrument are serialised by a per-instrument lock.
    """

    def __init__(self, lem_url: str, cache_store, *, timeout: float = 5.0,
                 fresh_seconds: float = 300, max_cache_age: float = 86400,
                 http_get: Optional[HttpGet] = None, clock: Optional[Clock] = None) -> None:
        self.lem_url = str(lem_url or "").strip().rstrip("/")
        self.cache_store = cache_store
        self.timeout = float(timeout)
        self.fresh_seconds = float(fresh_seconds)
        self.max_cache_age = float(max_cache_age)
        self._http_get = http_get or requests_http_get
        self._clock = clock or _utc_now
        self._attempts: Dict[str, _Attempt] = {}
        self._lock_for = _Locks()

    # ── public ──────────────────────────────────────────────────────────
    def get(self, instrument: dict) -> Corrections:
        inst_id, uid, mapping, key = self._resolve(instrument)
        with self._lock_for(inst_id):
            now = self._now()
            last = self._attempts.get(inst_id)
            if last is not None and last.key == key and self._is_fresh(last.at, now):
                return self._replay(inst_id, key, now, last.outcome)
            fresh = self._fresh_from_store(inst_id, key, now)
            if fresh is not None:
                self._attempts[inst_id] = _Attempt(_parse_iso(fresh.fetched_at), key, fresh)
                return fresh
            return self._fetch(inst_id, uid, mapping, key, now)

    def refresh(self, instrument: dict) -> Corrections:
        inst_id, uid, mapping, key = self._resolve(instrument)
        with self._lock_for(inst_id):
            return self._fetch(inst_id, uid, mapping, key, self._now())

    def changed_since(self, instrument: dict, used: Corrections) -> bool:
        """Do the latest values fetched for this instrument's current machine
        and map differ from ``used``? False when there is no such fetch."""
        try:
            inst_id, _uid, _mapping, key = self._resolve(instrument, need_url=False)
        except CorrectionsUnavailable:
            return False
        with self._lock_for(inst_id):
            entry = self._load(inst_id, key)
            latest = entry["values"] if entry is not None else None
            if latest is None:
                last = self._attempts.get(inst_id)
                if last is not None and last.key == key and isinstance(last.outcome, Corrections):
                    latest = last.outcome.values
        return latest is not None and values_differ(latest, used.values)

    # ── steps ───────────────────────────────────────────────────────────
    def _now(self) -> datetime:
        return _as_utc(self._clock())

    def _resolve(self, instrument: dict, need_url: bool = True):
        instrument = instrument or {}
        inst_id = str(instrument.get("id") or "").strip()
        if not inst_id:
            raise _config("The instrument has no id.")
        uid = str(instrument.get("lem_machine_uid") or "").strip()
        if not uid:
            raise _config(f"Instrument {inst_id!r} has no LEM machine uid; set it on "
                          f"the Instruments page.")
        if need_url and not self.lem_url:
            raise _config("LEM_URL is not set.")
        mapping = parse_correction_map(instrument.get("correction_map"))
        return inst_id, uid, mapping, (uid, map_key(mapping))

    def _is_fresh(self, then: Optional[datetime], now: datetime) -> bool:
        age = _age(then, now)
        return age is not None and 0 <= age < self.fresh_seconds

    def _replay(self, inst_id, key, now, outcome) -> Corrections:
        if isinstance(outcome, Corrections):
            return outcome
        if outcome.kind == "config":
            raise CorrectionsUnavailable(outcome.reason, outcome.kind)
        return self._fallback(inst_id, key, now, outcome)

    def _fetch(self, inst_id, uid, mapping, key, now) -> Corrections:
        try:
            result, methods = self._ask_lem(uid, mapping, now)
        except CorrectionsUnavailable as exc:
            self._attempts[inst_id] = _Attempt(now, key, exc)
            if exc.kind == "config":
                raise
            return self._fallback(inst_id, key, now, exc)
        self._attempts[inst_id] = _Attempt(now, key, result)
        try:
            self.cache_store.save(inst_id, dict(result.values), methods, result.fetched_at,
                                  lem_machine_uid=key[0], map_key=key[1])
        except Exception as exc:  # noqa: BLE001 - a lost cache row is not a lost answer
            LOGGER.warning("Could not cache corrections for %s: %s", inst_id, exc)
        return result

    def _ask_lem(self, uid: str, mapping: Dict[str, str], now: datetime):
        url = f"{self.lem_url}/api/machines/{quote(uid, safe='')}/corrections"
        try:
            status, content_type, body = self._http_get(url, self.timeout)
        except Exception as exc:  # noqa: BLE001 - timeout, refused, DNS, TLS: all "not asked"
            raise _unreachable(f"LEM did not answer ({type(exc).__name__}: {exc}).") from None
        content_type = str(content_type or "").lower()
        if status in (401, 403):
            raise _unreachable(f"LEM is behind a sign-in/proxy wall (HTTP {status}); the "
                               f"hub must reach LEM directly, without signing in.")
        if status == 429 or 500 <= status <= 599:
            raise _unreachable(f"LEM answered HTTP {status}{_lem_error(body)}.")
        if status != 200:
            raise _config(f"LEM answered HTTP {status} for {url}; check LEM_URL.")
        if "html" in content_type:
            raise _unreachable("LEM answered with a web page instead of data (a sign-in "
                               "page or a proxy error).")
        try:
            data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except (ValueError, UnicodeDecodeError, AttributeError, TypeError):
            raise _unreachable("LEM's answer was not JSON (a sign-in page or a proxy "
                               "error).") from None
        return _apply_lem_rules(uid, mapping, data, now)

    def _fallback(self, inst_id, key, now, exc) -> Corrections:
        entry = self._load(inst_id, key)
        if entry is None:
            raise _unreachable(f"{exc.reason} No cached corrections for this LEM machine "
                               f"and map to fall back on.")
        age = _age(_parse_iso(entry["fetched_at"]), now)
        if age is None or age < 0:
            raise _unreachable(f"{exc.reason} The cached corrections carry an unusable "
                               f"time ({entry['fetched_at']!r}).")
        if age > self.max_cache_age:
            raise _unreachable(f"{exc.reason} The cached corrections are from "
                               f"{entry['fetched_at']}, older than "
                               f"{self.max_cache_age / 3600:g} h.")
        return Corrections(source="cache", fetched_at=entry["fetched_at"],
                           values=_in_cut_order(entry["values"]), stale_reason=exc.reason)

    def _fresh_from_store(self, inst_id, key, now) -> Optional[Corrections]:
        entry = self._load(inst_id, key)
        if entry is None or not self._is_fresh(_parse_iso(entry["fetched_at"]), now):
            return None
        return Corrections(source="lem", fetched_at=entry["fetched_at"],
                           values=_in_cut_order(entry["values"]))

    def _load(self, inst_id: str, key: tuple) -> Optional[dict]:
        """The store's entry for this machine and map, validated; None for
        none, another machine or map, a broken store, or junk."""
        try:
            entry = self.cache_store.load(inst_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable cache is no cache
            LOGGER.warning("Could not read cached corrections for %s: %s", inst_id, exc)
            return None
        if not isinstance(entry, dict) or not isinstance(entry.get("values"), dict):
            return None
        if (str(entry.get("lem_machine_uid") or ""), str(entry.get("map_key") or "")) != key:
            return None
        values = {}
        for cut, value in entry["values"].items():
            number = _finite_float(value)
            if cut not in D86_CUTS or number is None:
                return None
            values[cut] = number
        fetched_at = str(entry.get("fetched_at") or "")
        if _parse_iso(fetched_at) is None:
            return None
        return {"values": values, "fetched_at": fetched_at}


def _apply_lem_rules(uid: str, mapping: Dict[str, str], data, now: datetime):
    """LEM's 200 JSON answer -> ``(Corrections, methods)``, or a config raise."""
    if (not isinstance(data, dict) or not isinstance(data.get("corrections"), list)
            or not isinstance(data.get("methods"), list)):
        raise _config("LEM's answer is not the corrections shape "
                      "({corrections: [...], methods: [...]}); check LEM_URL.")
    saved: Dict[str, dict] = {}
    for row in data["corrections"]:
        if not isinstance(row, dict):
            raise _config(f"LEM sent a correction that is not an object: {row!r}.")
        name = str(row.get("test_name") or "").strip()
        if name:
            saved[name] = row
    methods = [str(m).strip() for m in data["methods"] if str(m).strip()]
    if not methods:
        raise _config(f"LEM does not know machine {uid!r}: no methods mapped and no "
                      f"corrections saved.")
    known = set(methods)
    values: Dict[str, float] = {}
    missing = []
    for test_name, cut in mapping.items():
        if test_name in saved:
            row = saved[test_name]
            number = _finite_float(row.get("correction"))
            if number is None:
                raise _config(f"LEM's correction for {test_name!r} is not a number "
                              f"({row.get('correction')!r}).")
            units = str(row.get("units") or "").strip()
            if units not in ACCEPTED_UNITS:
                raise _config(f"LEM's correction for {test_name!r} is in {units!r}; D86 "
                              f"corrections must be in °C (°C, C or blank).")
            values[cut] = number
        elif test_name in known:
            values[cut] = 0.0          # LEM's rule: a missing correction is zero
        else:
            missing.append(test_name)
    if missing:
        raise _config(f"LEM machine {uid!r} reports none of: {', '.join(missing)}. Fix "
                      f"the instrument's correction map or the machine's mapping in LEM.")
    return Corrections(source="lem", fetched_at=_iso(now), values=_in_cut_order(values)), methods


def _lem_error(body) -> str:
    """LEM's own error sentence from a JSON error body, if there is one."""
    try:
        data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(data, dict) and data.get("error"):
        return f": {str(data['error'])[:300]}"
    return ""
