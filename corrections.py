"""D86 correction factors for the hub: where they come from, and when to refuse.

Contract: ``docs/superpowers/specs/2026-09-28-phase2-contracts.md`` §2.
Rules: the design's "LEM corrections (2C)".

A correction is added to a reported D86 temperature, so "no correction" is a
claim about the result, never a default for "could not find out". Every
provider here either returns a value for **every** cut in the instrument's
map (0.0 only where the source says zero) or raises
:class:`CorrectionsUnavailable`, which the pipeline turns into
``pending_corrections``. Nothing here ever returns an empty dict.

Stdlib only at import time (``requests`` is imported when the default HTTP
getter is first used), so the module is importable anywhere and unit-tested
without the web stack.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Optional, Protocol
from urllib.parse import quote

LOGGER = logging.getLogger(__name__)

D86_CUTS = ["IBP", "5%", "10%", "20%", "30%", "50%", "70%", "80%", "90%", "95%", "FBP"]

# LEM test_name -> D86 cut. Mirrors distill._D86_CORRECTION_TEST_MAP (a test
# pins the two together).
DEFAULT_CORRECTION_MAP: Dict[str, str] = {
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
    source: str        # 'lem' | 'cache' | 'file'
    fetched_at: str    # ISO, when the values were fetched from their source
    values: dict       # cut -> float, for EVERY cut in the map (explicit 0.0 allowed)


class CorrectionsUnavailable(Exception):
    """No trustworthy corrections: the sample goes ``pending_corrections``.

    ``kind`` is ``'unreachable'`` (the source could not be asked; retried) or
    ``'config'`` (the source answered, or the setup is such, that a human
    must act: unknown machine, unmapped test, bad units, bad map, bad file).
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


def _config(reason: str) -> CorrectionsUnavailable:
    return CorrectionsUnavailable(reason, "config")


def _unreachable(reason: str) -> CorrectionsUnavailable:
    return CorrectionsUnavailable(reason, "unreachable")


def parse_correction_map(raw) -> Dict[str, str]:
    """An instrument's ``correction_map`` (JSON string, or None = default).

    Returns a fresh dict ``{test_name: cut}``. Anything doubtful raises
    ``config``: it is never repaired, because a repaired map applies a
    correction to the wrong temperature without anyone seeing it.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return dict(DEFAULT_CORRECTION_MAP)
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


# ── shared helpers ───────────────────────────────────────────────────────────

Clock = Callable[[], datetime]


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


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
    """True if two cut->value maps would correct any result differently."""
    a, b = dict(a or {}), dict(b or {})
    if set(a) != set(b):
        return True
    for cut in a:
        x, y = _finite_float(a[cut]), _finite_float(b[cut])
        if x is None or y is None or abs(x - y) > 1e-9:
            return True
    return False


# ── the phase-1 file (2A1) ───────────────────────────────────────────────────

FILE_SECTION = "Agilent GC"
FILE_INSTRUMENT_ID = "gc1"


class FileProvider:
    """Phase-1 ``correction_factors_json``: ``{"Agilent GC": {test_name:
    {"correction_value": x}, ...}, ...}``. Serves only instrument ``gc1``.

    Stricter than ``distill.load_d86_corrections`` in exactly the ways that
    function hid a failure as "no correction":

    - missing file, unparseable JSON, a missing or non-object section, or a
      section with **no** mapped test -> ``config``;
    - a mapped test whose ``correction_value`` is not a finite number ->
      ``config`` (phase 1 dropped the whole file for this);
    - any other OS error (share offline, permissions) -> ``unreachable``.

    Kept from phase 1: a mapped cut the file does not list is uncorrected, so
    it is recorded as an explicit 0.0.
    """

    def __init__(self, json_path: str, *, clock: Optional[Clock] = None) -> None:
        self.json_path = str(json_path)
        self._clock = clock or datetime.now
        self._latest: Dict[str, Corrections] = {}

    def get(self, instrument: dict) -> Corrections:
        inst_id = str((instrument or {}).get("id") or "")
        if inst_id != FILE_INSTRUMENT_ID:
            raise _config(f"The corrections file serves only instrument "
                          f"{FILE_INSTRUMENT_ID!r}, not {inst_id!r}; set up LEM "
                          f"corrections for it.")
        mapping = parse_correction_map((instrument or {}).get("correction_map"))
        fetched_at = _iso(self._clock())
        section = self._read_section()
        values: Dict[str, float] = {}
        found = 0
        for test_name, cut in mapping.items():
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
                          f"none of the mapped tests ({', '.join(mapping)}); this "
                          f"is not the corrections file.")
        result = Corrections(source="file", fetched_at=fetched_at,
                             values=_in_cut_order(values))
        self._latest[inst_id] = result
        return result

    def refresh(self, instrument: dict) -> Corrections:
        return self.get(instrument)

    def changed_since(self, instrument_id: str, used: Corrections) -> bool:
        latest = self._latest.get(str(instrument_id))
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


def _in_cut_order(values: Dict[str, float]) -> Dict[str, float]:
    return {cut: values[cut] for cut in D86_CUTS if cut in values}


# ── the cache store ──────────────────────────────────────────────────────────
#
# Contract §2: ``load(instrument_id) -> dict | None`` and
# ``save(instrument_id, values, methods, fetched_at)``. ``load`` returns
# ``{"values": {cut: float}, "methods": [str], "fetched_at": str}``: the
# values per cut exactly as they were used, and LEM's ``methods`` for the
# record. Only a successful LEM fetch is ever saved. Lane A's SQLite store
# (table ``corrections_cache``) must return the same keys, and should drop the
# entry when an instrument's ``lem_machine_uid`` or ``correction_map`` changes
# (after a restart this module cannot tell such an entry from a current one).


class MemoryCacheStore:
    """In-memory cache store (tests, and anywhere persistence is not wanted)."""

    def __init__(self) -> None:
        self._rows: Dict[str, dict] = {}

    def load(self, instrument_id: str) -> Optional[dict]:
        row = self._rows.get(str(instrument_id))
        if row is None:
            return None
        return {"values": dict(row["values"]), "methods": list(row["methods"]),
                "fetched_at": row["fetched_at"]}

    def save(self, instrument_id: str, values: dict, methods: list, fetched_at: str) -> None:
        self._rows[str(instrument_id)] = {"values": dict(values), "methods": list(methods),
                                          "fetched_at": str(fetched_at)}


# ── LEM (2C) ─────────────────────────────────────────────────────────────────

ACCEPTED_UNITS = ("°C", "C", "")
HttpGet = Callable[[str, float], tuple]


def requests_http_get(url: str, timeout: float) -> tuple:
    """The default ``http_get``: ``(status, content_type, body_bytes)``.
    Raises on a timeout or connection error (the caller counts both as
    unreachable)."""
    import requests  # deferred: keeps this module importable without it

    resp = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
    return resp.status_code, resp.headers.get("Content-Type", ""), resp.content


@dataclass
class _Attempt:
    at: datetime
    key: tuple
    outcome: object      # Corrections | CorrectionsUnavailable


class LemProvider:
    """Corrections from LEM: ``GET {lem_url}/api/machines/<uid>/corrections``.

    LEM answers ``200 {"corrections": [{"test_name", "correction", "units"}],
    "methods": [str]}`` (``LEM Web Server/web_app.py`` ``api_get_corrections``);
    an unknown uid is ``200`` with both lists empty, and a LabCore it cannot
    read is a 502/503 JSON body (``_labcore_unreadable``), never an empty 200.

    - A 200 JSON answer is authoritative. The machine is known iff ``methods``
      is non-empty. Per mapped test name: in ``corrections`` -> its value; in
      ``methods`` only -> 0.0 (LEM's rule: a missing correction is zero); in
      neither -> ``config``. Units of a mapped test must be °C, C or empty.
    - A non-JSON 200 (a sign-in page), a timeout, a connection error, a 429
      or a 5xx -> ``unreachable``, answered from a cache entry at most
      ``max_cache_age`` seconds old (``source='cache'``), else raised.
    - Any other status, or JSON that is not LEM's shape -> ``config`` (a
      wrong ``LEM_URL`` or an auth wall; waiting will not fix it). A config
      error never falls back to the cache.
    - At most one fetch per ``fresh_seconds`` per instrument, whatever the
      outcome, so a backlog during an outage does not wait out the timeout
      once per sample. ``refresh()`` always fetches.
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
        self._clock = clock or datetime.now
        self._attempts: Dict[str, _Attempt] = {}

    # ── public ──────────────────────────────────────────────────────────
    def get(self, instrument: dict) -> Corrections:
        inst_id, uid, mapping, key = self._resolve(instrument)
        now = self._clock()
        last = self._attempts.get(inst_id)
        if last is not None and last.key == key and self._is_fresh(last.at, now):
            return self._replay(inst_id, mapping, now, last.outcome)
        if last is None or last.key == key:
            fresh = self._fresh_from_store(inst_id, mapping, now)
            if fresh is not None:
                self._attempts[inst_id] = _Attempt(_parse_iso(fresh.fetched_at), key, fresh)
                return fresh
        return self._fetch(inst_id, uid, mapping, key, now)

    def refresh(self, instrument: dict) -> Corrections:
        inst_id, uid, mapping, key = self._resolve(instrument)
        return self._fetch(inst_id, uid, mapping, key, self._clock())

    def changed_since(self, instrument_id: str, used: Corrections) -> bool:
        """Do the latest fetched values differ from the ones ``used``?
        False when nothing has been fetched yet: there is nothing to compare."""
        inst_id = str(instrument_id)
        entry = self._load(inst_id)
        latest = entry["values"] if entry is not None else None
        if latest is None:
            last = self._attempts.get(inst_id)
            if last is not None and isinstance(last.outcome, Corrections):
                latest = last.outcome.values
        if latest is None:
            return False
        return values_differ(latest, used.values)

    # ── steps ───────────────────────────────────────────────────────────
    def _resolve(self, instrument: dict):
        instrument = instrument or {}
        inst_id = str(instrument.get("id") or "").strip()
        if not inst_id:
            raise _config("The instrument has no id.")
        uid = str(instrument.get("lem_machine_uid") or "").strip()
        if not uid:
            raise _config(f"Instrument {inst_id!r} has no LEM machine uid; set it on "
                          f"the Instruments page.")
        if not self.lem_url:
            raise _config("LEM_URL is not set.")
        mapping = parse_correction_map(instrument.get("correction_map"))
        return inst_id, uid, mapping, (uid, tuple(sorted(mapping.items())))

    def _is_fresh(self, then: Optional[datetime], now: datetime) -> bool:
        age = _age(then, now)
        return age is not None and 0 <= age < self.fresh_seconds

    def _replay(self, inst_id, mapping, now, outcome) -> Corrections:
        if isinstance(outcome, Corrections):
            return outcome
        if outcome.kind == "config":
            raise CorrectionsUnavailable(outcome.reason, outcome.kind)
        return self._fallback(inst_id, mapping, now, outcome)

    def _fetch(self, inst_id, uid, mapping, key, now) -> Corrections:
        try:
            result, methods = self._ask_lem(uid, mapping, now)
        except CorrectionsUnavailable as exc:
            self._attempts[inst_id] = _Attempt(now, key, exc)
            if exc.kind == "config":
                raise
            return self._fallback(inst_id, mapping, now, exc)
        self._attempts[inst_id] = _Attempt(now, key, result)
        try:
            self.cache_store.save(inst_id, dict(result.values), methods, result.fetched_at)
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
        if status == 429 or 500 <= status <= 599:
            raise _unreachable(f"LEM answered HTTP {status}{_lem_error(body)}.")
        if status != 200:
            raise _config(f"LEM answered HTTP {status} for {url}; check LEM_URL and that "
                          f"the hub can reach LEM without signing in.")
        if "html" in content_type:
            raise _unreachable("LEM answered with a web page instead of data (a sign-in "
                               "page or a proxy error).")
        try:
            data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except (ValueError, UnicodeDecodeError, AttributeError, TypeError):
            raise _unreachable("LEM's answer was not JSON (a sign-in page or a proxy "
                               "error).") from None
        return _apply_lem_rules(uid, mapping, data, now)

    def _fallback(self, inst_id, mapping, now, exc) -> Corrections:
        entry = self._load(inst_id)
        if entry is None:
            raise _unreachable(f"{exc.reason} No cached corrections to fall back on.")
        age = _age(_parse_iso(entry["fetched_at"]), now)
        if age is None or age < 0:
            raise _unreachable(f"{exc.reason} The cached corrections carry an unusable "
                               f"time ({entry['fetched_at']!r}).")
        if age > self.max_cache_age:
            raise _unreachable(f"{exc.reason} The cached corrections are from "
                               f"{entry['fetched_at']}, older than "
                               f"{self.max_cache_age / 3600:g} h.")
        if set(entry["values"]) != set(mapping.values()):
            raise _unreachable(f"{exc.reason} The cached corrections were taken with a "
                               f"different correction map.")
        return Corrections(source="cache", fetched_at=entry["fetched_at"],
                           values=_in_cut_order(entry["values"]))

    def _fresh_from_store(self, inst_id, mapping, now) -> Optional[Corrections]:
        entry = self._load(inst_id)
        if entry is None or not self._is_fresh(_parse_iso(entry["fetched_at"]), now):
            return None
        if set(entry["values"]) != set(mapping.values()):
            return None
        return Corrections(source="lem", fetched_at=entry["fetched_at"],
                           values=_in_cut_order(entry["values"]))

    def _load(self, inst_id: str) -> Optional[dict]:
        """The store's entry, validated; None for none, a broken store, or junk."""
        try:
            entry = self.cache_store.load(inst_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable cache is no cache
            LOGGER.warning("Could not read cached corrections for %s: %s", inst_id, exc)
            return None
        if not isinstance(entry, dict) or not isinstance(entry.get("values"), dict):
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
        return {"values": values, "methods": list(entry.get("methods") or []),
                "fetched_at": fetched_at}


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


def _age(then: Optional[datetime], now: datetime) -> Optional[float]:
    return None if then is None else (now - then).total_seconds()


def _parse_iso(text) -> Optional[datetime]:
    """Naive local datetime from ISO text; an aware one is converted to local."""
    try:
        dt = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _lem_error(body) -> str:
    """LEM's own error sentence from a JSON error body, if there is one."""
    try:
        data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(data, dict) and data.get("error"):
        return f": {str(data['error'])[:300]}"
    return ""
